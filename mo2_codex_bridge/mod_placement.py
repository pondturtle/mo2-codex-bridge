"""Plan and verify one named change to current-profile left-pane priority."""
from pathlib import Path

from .common import write_json
from .mod_change_previews import ModChangePlan
from .order_recovery import recovery_context
from .planned_profile_state import (
    check_saved_plugins, expected_modlist, keep_profile_files, modlist_rows, profile_file_hashes,
    read_profile_files,
)
from .profile_usage import _lexical, _plain_name, _safe_stat


def checked_mod_folder(ops, name):
    if not _plain_name(name):
        raise ValueError('Use the exact regular mod name')
    mod = ops.mod(name, writable=True)
    if mod.name() != name or mod.isSeparator():
        raise ValueError('Use the exact name of a regular MO2-managed mod')
    root = Path(ops.o.modsPath())
    path = Path(mod.absolutePath())
    if not root.is_absolute() or not path.is_absolute() or _lexical(path) != _lexical(root / name):
        raise ValueError('The mod is not a direct child of the configured Mods folder')
    info = _safe_stat(path, directory=True)
    return path, (info.st_dev, info.st_ino)


def ordered_mods(snapshot):
    mods = snapshot['mods']
    names = [row['name'].casefold() for row in mods]
    if (len(set(names)) != len(names) or any(row['overwrite'] for row in mods)
            or [row['priority'] for row in mods] != list(range(len(mods)))):
        raise ValueError('MO2 priority order or mod identities are ambiguous')
    return mods


def require_same_context(ops, before):
    current = ops.snapshot()
    if ops.running:
        raise RuntimeError('A game or MO2-launched tool is running; no further changes applied')
    if recovery_context(current) != recovery_context(before):
        raise RuntimeError('MO2 profile or instance changed during the operation')
    return current


def placement_provider_evidence(ops, before, mod, crossed):
    if not crossed or not mod['active']:
        return {}
    crossed = {row['name'].casefold() for row in crossed if row['active']}
    if not crossed:
        return {}
    relevant = {mod['name'].casefold()} | crossed
    primaries = {name.casefold() for name in ops.o.managedGame().primaryPlugins()}
    evidence = {}
    for plugin in before['plugins']:
        origins = list(ops.o.getFileOrigins(plugin['name']))
        if not all(isinstance(origin, str) and origin for origin in origins):
            raise ValueError('MO2 plugin provider evidence is unavailable')
        if not origins and (plugin['name'].casefold() not in primaries
                            or plugin['origin'].casefold() in relevant):
            raise ValueError('MO2 plugin provider evidence is unavailable: ' + plugin['name'])
        names = {origin.casefold() for origin in origins}
        if mod['name'].casefold() in names and crossed & names:
            raise ValueError('This placement could change a plugin provider: ' + plugin['name'])
        if relevant & names or plugin['origin'].casefold() in relevant:
            if plugin['origin'].casefold() not in names and plugin['origin']:
                raise ValueError('MO2 plugin origin disagrees with provider evidence: ' + plugin['name'])
            evidence[plugin['name']] = [origins[0], sorted(origins[1:], key=str.casefold)]
    return evidence


def build_placement(ops, params, before):
    if set(params) != {'mod', 'anchor', 'position'}:
        raise ValueError('Placement requires mod, anchor and position')
    name, anchor, position = params['mod'], params['anchor'], params['position']
    if (not isinstance(anchor, str) or not anchor or len(anchor) > 1024
            or any(ord(char) < 32 for char in anchor) or position not in ('before', 'after')):
        raise ValueError('Use an exact anchor name and position before or after')
    folder, folder_identity = checked_mod_folder(ops, name)
    mods = ordered_mods(before)
    by_name = {row['name']: row for row in mods}
    if name == anchor or name not in by_name or anchor not in by_name:
        raise ValueError('The mod and anchor must be distinct exact entries in the current profile')
    old = by_name[name]['priority']
    planned = mods.copy()
    moved = planned.pop(old)
    new = planned.index(by_name[anchor]) + (position == 'after')
    planned.insert(new, moved)
    planned = [{**row, 'priority': priority} for priority, row in enumerate(planned)]
    crossed = [row for row in mods[min(old, new):max(old, new) + 1] if row['name'] != name]
    profile = Path(before['profile_path'])
    files = read_profile_files(profile)
    if modlist_rows(files['modlist.txt']) != expected_modlist(mods):
        raise ValueError('Saved modlist differs from the current MO2 profile; wait for MO2 to save')
    check_saved_plugins(ops.o, files, before['plugins'])
    providers = placement_provider_evidence(ops, before, moved, crossed)
    reverse = ({'mod': name, 'anchor': mods[old + 1]['name'], 'position': 'before'}
               if old + 1 < len(mods) else
               {'mod': name, 'anchor': mods[old - 1]['name'], 'position': 'after'})
    summary = {
        'mod': name, 'anchor': anchor, 'position': position, 'no_op': old == new,
        'affected_profiles': [before['profile']],
        'priority_before': old, 'priority_after': new,
        'crossed_mods': [row['name'] for row in crossed],
        'effect': 'Left-pane priority only. File winners between enabled mods may change.',
        'recovery': {'method': 'Inspect current state, then preview the original placement',
                     'reverse_placement': reverse},
    }
    evidence = {'profile_files': profile_file_hashes(files), 'folder_identity': folder_identity,
                'providers': providers}

    def apply(backup):
        changes_may_have_occurred = False
        try:
            if old != new:
                if not callable(ops.wait_until):
                    raise RuntimeError('Saved mod-list verification is unavailable')
                keep_profile_files(backup / 'saved-profile', files)
                write_json(backup / 'planned-change.json', {'action': 'place_mod', **summary, **evidence})
            current = require_same_context(ops, before)
            if (current['revision'] != before['revision'] or read_profile_files(profile) != files
                    or checked_mod_folder(ops, name) != (folder, folder_identity)
                    or placement_provider_evidence(ops, before, moved, crossed) != providers):
                raise ValueError('The placement preview became stale before the change')
            if old != new:
                if require_same_context(ops, before)['revision'] != before['revision']:
                    raise ValueError('MO2 state changed before the native placement')
                changes_may_have_occurred = True
                ops.move_mod(name, new)
                require_same_context(ops, before)
                ops.refresh()

            def verify():
                current = require_same_context(ops, before)
                if current['mods'] != planned or current['plugins'] != before['plugins']:
                    raise RuntimeError('Observed mod order, activation or plugin state differs from the placement plan')
                if placement_provider_evidence(ops, current, moved, crossed) != providers:
                    raise RuntimeError('Observed plugin providers differ from the placement plan')
                saved = read_profile_files(profile)
                if any(saved[key] != files[key] for key in ('plugins.txt', 'loadorder.txt')):
                    raise RuntimeError('Saved plugin state changed during the mod placement')
                return modlist_rows(saved['modlist.txt']) == expected_modlist(planned)

            if (old != new and not ops.wait_until(verify, 5000)) or not verify():
                raise RuntimeError('MO2 saved mod order was not confirmed. Inspect the existing job and recovery files.')
        except Exception as error:
            error.recovery_details = {**summary['recovery'], 'changes_may_have_occurred': changes_may_have_occurred,
                                      'automatic_rollback_attempted': False}
            raise
        return {**summary, 'changed': old != new, 'modlist_persisted': True,
                'verified_files': ['modlist.txt', 'plugins.txt', 'loadorder.txt']}

    return ModChangePlan(summary, evidence, apply)

