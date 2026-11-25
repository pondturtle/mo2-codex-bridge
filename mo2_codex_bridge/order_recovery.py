"""Conservative preflight for restoring order through the native MO2 APIs.

This module never calls a mutation API. Header/dependency facts always come from
MO2's CURRENT visible inventory, never from the older backup. An enabled-state
change is supported only for a regular mod proved to have no root plugins.
A priority change requires unambiguous current plugin-provider evidence.
"""
import hashlib
import json
import ntpath
from pathlib import Path

from .common import contained_path, digest

PLUGIN_SUFFIXES = {'.esp', '.esm', '.esl'}

def read_order_backup(folder):
    """Read bounded, unlinked recovery JSON and reject duplicate metadata keys."""
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Recovery metadata contains duplicate keys')
            value[key] = item
        return value
    path = contained_path(folder, 'before.json')
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('Recovery snapshot exceeds its size limit')
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=pairs)
    validate_backup_snapshot(value)
    return value


def validate_backup_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('Recovery snapshot must be an object')
    revision = snapshot.get('revision')
    unhashed = dict(snapshot)
    unhashed.pop('revision', None)
    if (not isinstance(revision, str) or len(revision) != 64
            or any(character not in '0123456789abcdef' for character in revision)
            or digest(json.dumps(unhashed, sort_keys=True).encode()) != revision):
        raise ValueError('Recovery snapshot integrity check failed')


def _path_key(value):
    if not isinstance(value, str) or not value or '\x00' in value or not ntpath.isabs(value):
        raise ValueError('Recovery requires an exact absolute physical context path')
    return ntpath.normcase(ntpath.normpath(value))


def recovery_context(snapshot):
    """Keep legacy compatibility only within identical physical profile/roots."""
    try:
        profile = snapshot['profile']
        if not isinstance(profile, str) or not profile:
            raise ValueError('Recovery profile identity is unavailable')
        result = {'profile': profile, 'profile_path': _path_key(snapshot['profile_path'])}
        for name in ('mods', 'downloads', 'overwrite'):
            result[name] = _path_key(snapshot['paths'][name])
        extra = snapshot.get('recovery_identity')
        if extra is not None:
            if not isinstance(extra, dict) or not isinstance(extra.get('game'), str) or not extra['game']:
                raise ValueError('Recovery instance identity is unavailable')
            result['bridge_data_path'] = _path_key(extra['bridge_data_path'])
            result['game'] = extra['game'].casefold()
        return result
    except (KeyError, TypeError) as error:
        raise ValueError('Backup lacks the exact profile/root identity needed for safe recovery') from error


def _same_context(saved, current):
    # A legacy backup has no bridge-data/game identity. It still must match
    # EVERY stored physical root and profile path, never just a display name.
    if any(current.get(key) != value for key, value in saved.items()):
        raise ValueError('The backup belongs to another profile, root or MO2 instance')


def _named(items, label):
    if not isinstance(items, list):
        raise ValueError('Invalid ' + label + ' inventory')
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('name'), str) or not item['name']:
            raise ValueError('Invalid ' + label + ' name')
        key = item['name'].casefold()
        if key in result:
            raise ValueError('Duplicate case-insensitive ' + label + ' identity: ' + item['name'])
        if type(item.get('priority')) is not int:
            raise ValueError('Invalid ' + label + ' priority: ' + item['name'])
        if type(item.get('active')) is not bool:
            raise ValueError('Invalid ' + label + ' enabled state: ' + item['name'])
        result[key] = item
    return result


def _ordered(items, label):
    values = list(items.values())
    priorities = [item['priority'] for item in values]
    if len(priorities) != len(set(priorities)):
        raise ValueError('Duplicate ' + label + ' priorities')
    return sorted(values, key=lambda item: item['priority'])


def _managed(mod):
    for flag in ('foreign', 'overwrite', 'separator'):
        if type(mod.get(flag)) is not bool:
            raise ValueError('Incomplete mod identity: ' + mod['name'])
    return not mod['foreign'] and not mod['overwrite']


def root_plugin_files(mod, mods_root):
    """Read only a validated regular mod's top-level physical filenames.

    No recursion, archive loading, plugin parsing or external process is used.
    A link/junction at the root or among its children makes proof unavailable.
    This function is called only for an authorized restore at execution time.
    """
    path = contained_path(mods_root, mod['name'])
    if _path_key(str(path)) != _path_key(mod['path']) or not path.is_dir():
        raise ValueError('Mod folder identity cannot be proved: ' + mod['name'])
    plugins = []
    for entry in path.iterdir():
        if entry.is_symlink() or (hasattr(entry, 'is_junction') and entry.is_junction()):
            raise ValueError('Linked mod contents prevent safe recovery: ' + mod['name'])
        if entry.suffix.casefold() in PLUGIN_SUFFIXES:
            if not entry.is_file():
                raise ValueError('Ambiguous plugin entry prevents safe recovery: ' + entry.name)
            plugins.append(entry.name)
    return tuple(plugins)


def _plugin_fingerprint(plugins, *, headers_only=False):
    facts = []
    for item in (sorted(plugins.values(), key=lambda value: value['name'].casefold())
                 if headers_only else _ordered(plugins, 'plugin')):
        masters = item.get('masters')
        if not isinstance(masters, list) or any(not isinstance(n, str) or not n for n in masters):
            raise ValueError('Current plugin master information is unavailable: ' + item['name'])
        if not isinstance(item.get('origin'), str) or not item['origin']:
            raise ValueError('Current plugin provider information is unavailable: ' + item['name'])
        if type(item.get('master')) is not bool or type(item.get('light')) is not bool:
            raise ValueError('Current plugin header flags are unavailable: ' + item['name'])
        fact = {'name': item['name'].casefold(), 'origin': item['origin'].casefold(),
                'masters': [n.casefold() for n in masters],
                'master': item['master'], 'light': item['light']}
        if not headers_only:
            fact.update(priority=item['priority'], active=item['active'])
        facts.append(fact)
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode('utf-8')).hexdigest()


def validate_plugin_state(plugins, order, states, primary_plugins, current_order):
    """Validate complete desired state against current visible header facts."""
    names = _named(plugins, 'plugin')
    order_keys = [name.casefold() for name in order]
    if len(order_keys) != len(set(order_keys)) or set(order_keys) != set(names):
        raise ValueError('Restore must contain every currently visible plugin exactly once')
    enabled = {name.casefold(): active for name, active in states.items()}
    if set(enabled) != set(names) or any(type(value) is not bool for value in enabled.values()):
        raise ValueError('Restore must specify every current plugin enabled state')
    positions = {name: index for index, name in enumerate(order_keys)}
    current_positions = {name.casefold(): index for index, name in enumerate(current_order)}
    primary = [name.casefold() for name in primary_plugins]
    if len(primary) != len(set(primary)):
        raise ValueError('Primary game plugin identity is ambiguous')
    for name in primary:
        # Skyrim's primaryPlugins catalog includes optional Skyrim.ccc entries
        # even when they are not installed. Only current visible primaries are
        # load-order anchors; required masters are checked independently below.
        if name not in names:
            continue
        if not enabled[name]:
            raise ValueError('Primary game plugins must be present and enabled: ' + name)
        if positions[name] != current_positions.get(name):
            raise ValueError('Primary game plugin anchors cannot be moved during recovery: ' + names[name]['name'])
    for name, item in names.items():
        masters = item.get('masters')
        if not isinstance(masters, list) or any(not isinstance(n, str) or not n for n in masters):
            raise ValueError('Current plugin master information is unavailable: ' + item['name'])
        for master in masters:
            key = master.casefold()
            if enabled[name] and (key not in names or not enabled[key]):
                raise ValueError('Restore has a missing or disabled master: ' + item['name'] + ' -> ' + master)
            if key in positions and positions[key] >= positions[name]:
                raise ValueError('A master must load before its dependent during recovery: ' + item['name'] + ' -> ' + master)


def _provider_evidence(plugins, plugin_origins):
    if not callable(plugin_origins):
        raise ValueError('Plugin provider proof is unavailable; no order changes made')
    result = {}
    for item in plugins.values():
        try:
            origins = plugin_origins(item['name'])
        except Exception as error:
            raise ValueError('Plugin provider proof is unavailable: ' + item['name']) from error
        if not isinstance(origins, (list, tuple)) or any(not isinstance(n, str) or not n for n in origins):
            raise ValueError('Plugin provider proof is unavailable: ' + item['name'])
        keys = {name.casefold() for name in origins}
        if len(keys) != 1 or item['origin'].casefold() not in keys:
            raise ValueError('Ambiguous plugin providers prevent safe mod priority recovery: ' + item['name'])
        result[item['name'].casefold()] = sorted(keys)
    return result


def build_restore_plan(before, current, primary_plugins, *, plugin_origins=None,
                       plugin_files=None):
    """Return a mutation-free plan, or refuse before ANY mod/plugin setter.

    plugin_files is an injectable proof reader for isolated fixtures. Production
    defaults to the confined root_plugin_files reader above. Enabling/disabling
    a plugin-bearing mod is deliberately unsupported until prospective provider
    inventory and headers can be proved without toggling the live state.
    """
    validate_backup_snapshot(before)
    original_context = recovery_context(before)
    context = recovery_context(current)
    _same_context(original_context, context)
    old_mods = _named(before.get('mods'), 'backup mod')
    mods = _named(current.get('mods'), 'mod')
    old_plugins = _named(before.get('plugins'), 'backup plugin')
    plugins = _named(current.get('plugins'), 'plugin')
    baseline_fingerprint = _plugin_fingerprint(plugins)
    current_plugins = _ordered(plugins, 'plugin')
    old_order = _ordered(old_plugins, 'backup plugin')
    if any(key not in plugins for key in old_plugins):
        raise ValueError('Backup refers to plugins absent from current visible inventory; no changes made')
    for key, old in old_mods.items():
        if key not in mods:
            raise ValueError('Backup refers to removed mods; restore them first')
        mod = mods[key]
        if _managed(old) != _managed(mod) or any(old[flag] != mod[flag] for flag in ('foreign', 'overwrite', 'separator')):
            raise ValueError('Mod kind changed since backup: ' + mod['name'])
        if _path_key(old['path']) != _path_key(mod['path']):
            raise ValueError('Mod folder changed since backup: ' + mod['name'])
    managed = [item for item in _ordered(old_mods, 'backup mod') if _managed(item)]
    for mod in mods.values():
        _managed(mod)
        if _managed(mod):
            # Reject an API mod object resolving outside its configured directory.
            # This is lexical identity; the actual changed-folder reader additionally
            # rejects linked roots/components through contained_path.
            expected = ntpath.join(context['mods'], mod['name'])
            if _path_key(mod['path']) != _path_key(expected):
                raise ValueError('Mod path is outside its exact configured identity: ' + mod['name'])
    model_order = [mod['name'] for mod in _ordered(mods, 'mod')]
    moves = []
    for old in managed:
        name = mods[old['name'].casefold()]['name']
        priority = old['priority']
        if not 0 <= priority < len(model_order):
            raise ValueError('Backup mod priority is not available: ' + name)
        if model_order.index(name) != priority:
            moves.append((name, priority))
            model_order.remove(name)
            model_order.insert(priority, name)
    target_active = {}
    changed_states = []
    extra_disabled = []
    for key, mod in mods.items():
        if not _managed(mod) or mod['separator']:
            continue
        desired = old_mods[key]['active'] if key in old_mods else False
        target_active[mod['name']] = desired
        if desired != mod['active']:
            try:
                files = (plugin_files(mod) if plugin_files is not None
                         else root_plugin_files(mod, current['paths']['mods']))
            except Exception as error:
                raise ValueError('Prospective plugin inventory cannot be proved for mod: ' + mod['name']) from error
            if not isinstance(files, (tuple, list)) or any(not isinstance(name, str) for name in files):
                raise ValueError('Prospective plugin inventory cannot be proved for mod: ' + mod['name'])
            if files:
                raise ValueError('Plugin-bearing mod enabled-state recovery needs prospective headers; no changes made: ' + mod['name'])
            # Even a claimed plugin-free proof must agree with the current model.
            if any(item['origin'].casefold() == key for item in plugins.values()):
                raise ValueError('Mod plugin inventory disagrees with visible providers: ' + mod['name'])
            changed_states.append((mod['name'], desired))
            if key not in old_mods and not desired:
                extra_disabled.append(mod['name'])
    order = [plugins[item['name'].casefold()]['name'] for item in old_order]
    order += [item['name'] for item in current_plugins if item['name'].casefold() not in old_plugins]
    states = {item['name']: item['active'] for item in current_plugins}
    for key, old in old_plugins.items():
        states[plugins[key]['name']] = old['active']
    validate_plugin_state(current['plugins'], order, states, primary_plugins,
                          [item['name'] for item in current_plugins])
    providers = _provider_evidence(plugins, plugin_origins) if moves else None
    return {'context': context, 'order': order, 'states': states,
            'mod_moves': moves, 'mod_states': changed_states,
            'mod_order': model_order, 'target_mod_active': target_active,
            'mod_identities': {key: {'path': _path_key(mod['path']),
                                    'foreign': mod['foreign'], 'overwrite': mod['overwrite'],
                                    'separator': mod['separator']}
                               for key, mod in mods.items()},
            'plugin_fingerprint': baseline_fingerprint,
            'plugin_headers': _plugin_fingerprint(plugins, headers_only=True),
            'provider_evidence': providers, 'primary_plugins': list(primary_plugins),
            'extra_mods_disabled': extra_disabled,
            'legacy_context': 'recovery_identity' not in before}


def revalidate_restore_plan(plan, current, *, plugin_origins=None, primary_plugins=None):
    """Recheck after mod refresh and before raw plugin order/state setters.

    A mismatch may occur after the authorized mod steps. The caller must surface
    the operation's backup/partial-result information, never pretend no changes
    occurred or automatically undo valid concurrent user edits.
    """
    if primary_plugins is not None and [name.casefold() for name in primary_plugins] != [name.casefold() for name in plan['primary_plugins']]:
        raise ValueError('Primary game plugin identity changed during recovery')
    if recovery_context(current) != plan['context']:
        raise ValueError('Profile, root or instance changed during recovery')
    mods = _named(current.get('mods'), 'mod')
    if set(mods) != set(plan['mod_identities']):
        raise ValueError('Mod inventory changed during recovery')
    for key, mod in mods.items():
        expected = plan['mod_identities'][key]
        if any(mod.get(flag) != expected[flag] for flag in ('foreign', 'overwrite', 'separator')) or _path_key(mod['path']) != expected['path']:
            raise ValueError('Mod identity changed during recovery: ' + mod['name'])
    if [item['name'] for item in _ordered(mods, 'mod')] != plan['mod_order']:
        raise ValueError('MO2 did not apply the planned complete mod order')
    for name, active in plan['target_mod_active'].items():
        if mods[name.casefold()]['active'] != active:
            raise ValueError('MO2 did not apply the planned mod enabled state: ' + name)
    plugins = _named(current.get('plugins'), 'plugin')
    if _plugin_fingerprint(plugins) != plan['plugin_fingerprint']:
        raise ValueError('Plugin inventory, headers, provider or state changed during recovery')
    if plan['provider_evidence'] is not None:
        if _provider_evidence(plugins, plugin_origins) != plan['provider_evidence']:
            raise ValueError('Plugin providers changed during recovery')
    validate_plugin_state(current['plugins'], plan['order'], plan['states'],
                          plan['primary_plugins'], [item['name'] for item in _ordered(plugins, 'plugin')])


def verify_restore_result(plan, current, *, plugin_origins=None, primary_plugins=None):
    """Verify final mod and plugin state after the persistence event loop.

    Treat the intended plugin order/states as the new baseline while retaining
    the original independently captured provider/header fingerprint.
    """
    plugins = _named(current.get('plugins'), 'plugin')
    if _plugin_fingerprint(plugins, headers_only=True) != plan['plugin_headers']:
        raise ValueError('Plugin inventory, headers or provider changed during recovery')
    actual_order = [item['name'] for item in _ordered(plugins, 'plugin')]
    if actual_order != plan['order'] or any(plugins[name.casefold()]['active'] != active
                                            for name, active in plan['states'].items()):
        raise ValueError('Final plugin order or enabled states differ from the recovery plan')
    final_plan = dict(plan, plugin_fingerprint=_plugin_fingerprint(plugins))
    revalidate_restore_plan(final_plan, current, plugin_origins=plugin_origins,
                            primary_plugins=primary_plugins)

