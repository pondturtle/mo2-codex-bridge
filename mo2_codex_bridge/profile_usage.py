"""Read-only saved profile usage and explicitly current active-plugin evidence.

MO2 2.5.2 profile.cpp doWriteModlist/refreshModStatus write UTF-8 and read
trimmed + (enabled), - (disabled), * (foreign) and legacy bare names (enabled).
No profile object is constructed, switched, refreshed, saved or flushed here.
"""
import hashlib
import os
import stat
from itertools import islice
from pathlib import Path

MAX_PROFILES = 128
MAX_PROFILE_ENTRIES = 256
MAX_MODLIST_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_LINES = 50000
MAX_MODS = 10000
MAX_PLUGINS = 4096
MAX_REPORTED_PLUGINS = 128
MAX_DEPENDENTS = 128
_REPARSE = 0x400


def _bounded(values, maximum, label):
    result = list(islice(iter(values), maximum + 1))
    if len(result) > maximum:
        raise ValueError(label + ' exceeds the report limit')
    return result


def _plain_name(value):
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= 255 and value not in {'.', '..'}
            and not any(char in '<>:"/\\|?*\x00' or ord(char) < 32 for char in value)
            and not value.endswith(('.', ' ')))


def _lexical(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(str(path))))


def _safe_stat(path, directory=False):
    """Inspect components before opening; never resolve a discovered link."""
    path = Path(path).absolute()
    for part in reversed((path, *path.parents)):
        value = part.lstat()
        if (stat.S_ISLNK(value.st_mode)
                or getattr(value, 'st_file_attributes', 0) & _REPARSE):
            raise ValueError('Linked or reparse paths are not read')
    value = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode):
        raise ValueError('Expected a regular ' + ('directory' if directory else 'file'))
    return value


def _signature(value):
    # Windows path/handle st_ctime semantics differ across Python versions.
    # Compare identity, size, mtime and link count consistently instead.
    return (value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_nlink)


def _read_saved(path, budget):
    before = _safe_stat(path)
    if before.st_nlink != 1:
        raise ValueError('Saved modlist hard links are not read')
    if before.st_size > MAX_MODLIST_BYTES:
        raise ValueError('Saved modlist exceeds the per-file size limit')
    if before.st_size > budget[0]:
        raise ValueError('Saved modlist exceeds the remaining total read budget')
    # A bounded read, followed by handle/path identity and metadata checks. These
    # are observations, not a lock or an atomic snapshot against other writers.
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if opened.st_nlink != 1 or _signature(opened) != _signature(before):
            raise ValueError('Saved modlist changed before it could be read')
        data = stream.read(min(MAX_MODLIST_BYTES, budget[0]) + 1)
        budget[0] -= len(data)
        after_handle = os.fstat(stream.fileno())
    after_path = _safe_stat(path)
    if (len(data) != before.st_size or _signature(after_handle) != _signature(before)
            or _signature(after_path) != _signature(before)):
        raise ValueError('Saved modlist changed while being read')
    return data


def _aliases(name):
    result = set()
    for source, target in [('cp1252', 'utf-8'), ('utf-8', 'cp1252')]:
        try:
            value = name.encode(source).decode(target)
            if value != name:
                result.add(value.casefold())
        except UnicodeError:
            pass
    return result


def _saved_state(data, target):
    rows = data.split(b'\n')
    if len(rows) > MAX_LINES:
        raise ValueError('Saved modlist exceeds the line limit')
    entries = {}
    for index, raw in enumerate(rows):
        line = raw.strip(b' \t\r\n\v\f')
        if line.startswith(b'\xef\xbb\xbf'):
            # The native writer has no BOM. A BOM on the initial comment line
            # cannot establish any state; a BOM before data remains uncertain.
            tail = line[3:].strip(b' \t\r\n\v\f')
            if index == 0 and (not tail or tail.startswith(b'#')):
                continue
            raise ValueError('A byte order mark before saved data is not verified')
        if not line or line.startswith(b'#'):
            continue
        marker = chr(line[0]) if line[0] in b'+-*' else None
        encoded = (line[1:] if marker else line).strip(b' \t\r\n\v\f')
        name = encoded.decode('utf-8', errors='strict')
        # Foreign labels may contain a colon, and a native name may itself
        # begin with + or *. Do not apply regular-folder validation to them.
        if not name or len(name) > 1024 or any(ord(char) < 32 for char in name):
            raise ValueError('Saved modlist contains an invalid or empty name')
        key = name.casefold()
        if key in entries:
            raise ValueError('Saved modlist contains duplicate or case-alias names')
        entries[key] = (name, marker)
    target_key = target.casefold()
    target_aliases = _aliases(target)
    if any(key in target_aliases or target_key in _aliases(name)
           for key, (name, _) in entries.items()):
        raise ValueError('Saved mod name has an unproved encoding alias')
    if target_key not in entries:
        return {'state': 'absent', 'marker': None}
    name, marker = entries[target_key]
    if name != target:
        raise ValueError('Saved mod name differs in case from the exact native identity')
    if marker == '*':
        raise ValueError('Saved foreign marker conflicts with the selected regular mod')
    return {'state': 'disabled' if marker == '-' else 'enabled',
            'marker': marker, 'legacy_unmarked': marker is None}


def _unknown(reason):
    return {'state': 'unknown', 'reason': str(reason)}


def _current_plugins(ops, target, root):
    result = {'status': 'complete', 'provided_by_mod': [], 'unknown': [],
              'scope': 'Current visible ACTIVE plugins only; current VFS winner and API masters; '
                       'no inactive-plugin, inactive-profile or on-disk header verification'}
    try:
        configured = Path(ops.o.modsPath())
        if not configured.is_absolute() or _lexical(root) != _lexical(configured / target):
            raise ValueError('Native regular mod path is not its configured direct Mods child')
        _safe_stat(root, directory=True)
        pl = ops.o.pluginList()
        names = _bounded(pl.pluginNames(), MAX_PLUGINS, 'Visible plugin inventory')
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError('Visible plugin identities are ambiguous')
        active = []
        for name in names:
            if pl.state(name) == ops.m.PluginState.ACTIVE:
                active.append(name)
        facts = {}
        for name in active:
            try:
                if not _plain_name(name) or Path(name).suffix.casefold() not in {'.esp', '.esm', '.esl'}:
                    raise ValueError('Invalid active plugin name')
                masters = _bounded(pl.masters(name), MAX_PLUGINS, 'Active plugin masters')
                if not all(_plain_name(master) for master in masters):
                    raise ValueError('Invalid active plugin master name')
                facts[name] = {'master_keys': {master.casefold() for master in masters},
                               'origin': str(pl.origin(name))}
            except Exception as error:
                result['unknown'].append({'plugin': str(name), 'reason': str(error)})
        reverse_masters = {}
        for child, row in facts.items():
            for master in row['master_keys']:
                reverse_masters.setdefault(master, []).append(child)
        provided = []
        for name in active:
            try:
                if name not in facts:
                    continue
                winner = ops.o.resolvePath(name)
                if not isinstance(winner, str) or not Path(winner).is_absolute():
                    raise ValueError('Current VFS winner is unavailable')
                winner_matches = _lexical(winner) == _lexical(root / name)
                origin_matches = facts[name]['origin'].casefold() == target.casefold()
                if winner_matches != origin_matches:
                    raise ValueError('Native origin and current VFS winner disagree for the selected mod')
                if not winner_matches:
                    continue
                _safe_stat(Path(winner))
                dependents = [other for other in reverse_masters.get(name.casefold(), []) if other != name]
                if len(provided) >= MAX_REPORTED_PLUGINS:
                    raise ValueError('Provided active plugins exceed the response limit')
                provided.append({'name': name, 'active': True, 'native_origin': facts[name]['origin'],
                                 'resolved_path': str(winner), 'provider_evidence': 'current resolvePath exact mod child',
                                 'active_dependents': sorted(dependents, key=str.casefold)[:MAX_DEPENDENTS],
                                 'dependents_complete': len(dependents) <= MAX_DEPENDENTS and len(facts) == len(active)})
            except Exception as error:
                result['unknown'].append({'plugin': str(name), 'reason': str(error)})
        result['provided_by_mod'] = provided
    except Exception as error:
        result['unknown'].append({'reason': str(error)})
    if result['unknown']:
        result['status'] = 'partial' if result['provided_by_mod'] else 'unknown'
    result['unknown_count'] = len(result['unknown'])
    result['unknown_truncated'] = len(result['unknown']) > MAX_REPORTED_PLUGINS
    result['unknown'] = result['unknown'][:MAX_REPORTED_PLUGINS]
    return result


def report_mod_profile_usage(ops, params):
    """Report saved usage without changing or instantiating any MO2 profile."""
    if not isinstance(params, dict) or set(params) != {'mod'} or not _plain_name(params['mod']):
        raise ValueError('Parameters must contain only an exact native mod name: mod')
    target = params['mod']
    ml = ops.o.modList()
    names = _bounded(ml.allMods(), MAX_MODS, 'Native mod inventory')
    matches = [name for name in names if isinstance(name, str) and name.casefold() == target.casefold()]
    if matches != [target]:
        raise ValueError('An unambiguous exact native mod name is required')
    mod = ml.getMod(target)
    if (mod is None or mod.name() != target or mod.isForeign() or mod.isBackup()
            or mod.isOverwrite() or mod.isSeparator()):
        raise ValueError('Profile usage requires an exact regular MO2-managed mod')
    context = (str(ops.o.profileName()), str(ops.o.profilePath()))
    current_path = Path(context[1])
    if not context[0] or not current_path.is_absolute():
        raise ValueError('Current native profile identity is unavailable')
    root = current_path.parent
    result = {'mod': target, 'profiles_root': str(root), 'profiles': [],
              'discovery': {'complete': True, 'method': 'direct directories beside current profilePath'},
              'current_profile': {'name': context[0], 'path': context[1]},
              'scope': 'Saved modlist evidence for other profiles; live state belongs only to the current profile',
              'limitations': ['A saved absent entry is not proof the mod would remain disabled when a profile loads.',
                             'The mod folder is shared across profiles; this report does not authorize a mutation.',
                             'Reads are bounded observations, not an atomic snapshot against external writers.']}
    try:
        live = 'enabled' if bool(ml.state(target) & ops.m.ModState.ACTIVE) else 'disabled'
    except Exception as error:
        live = 'unknown'
        result['current_profile']['live_reason'] = str(error)
    result['current_profile']['live_state'] = live
    budget = [MAX_TOTAL_BYTES]
    try:
        _safe_stat(root, directory=True)
        with os.scandir(root) as iterator:
            entries = _bounded(iterator, MAX_PROFILE_ENTRIES, 'Profile root directory entries')
        directories = []
        for entry in entries:
            try:
                value = entry.stat(follow_symlinks=False)
                linked = stat.S_ISLNK(value.st_mode) or getattr(value, 'st_file_attributes', 0) & _REPARSE
                if stat.S_ISDIR(value.st_mode) or linked:
                    directories.append((entry.name, linked))
            except OSError:
                directories.append((entry.name, True))
        if len(directories) > MAX_PROFILES:
            raise ValueError('Profile directories exceed the report limit')
        duplicate_names = {name.casefold() for name, _ in directories
                           if sum(other.casefold() == name.casefold() for other, _ in directories) > 1}
        for name, linked in sorted(directories, key=lambda item: item[0].casefold()):
            path = root / name
            row = {'name': name, 'path': str(path), 'current': _lexical(path) == _lexical(current_path)}
            try:
                if linked or name.casefold() in duplicate_names or not _plain_name(name):
                    raise ValueError('Linked or ambiguous profile directory is not read')
                _safe_stat(path, directory=True)
                data = _read_saved(path / 'modlist.txt', budget)
                saved = _saved_state(data, target)
                saved.update(source='modlist.txt', encoding='utf-8', bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                row['saved'] = saved
            except (OSError, ValueError, UnicodeError) as error:
                row['saved'] = _unknown(error)
            result['profiles'].append(row)
    except (OSError, ValueError) as error:
        result['discovery'].update(complete=False, reason=str(error))
    current_rows = [row for row in result['profiles'] if row['current']]
    saved = current_rows[0]['saved'] if len(current_rows) == 1 else _unknown('Current profile was not uniquely discovered')
    result['current_profile']['saved'] = saved
    result['current_profile']['live_saved_agree'] = (live == saved['state']
        if live in {'enabled', 'disabled'} and saved['state'] in {'enabled', 'disabled'} else None)
    result['current_active_plugins'] = _current_plugins(ops, target, Path(mod.absolutePath()))
    if (str(ops.o.profileName()), str(ops.o.profilePath())) != context:
        result['discovery'].update(complete=False, reason='Current profile changed during observation')
        result['current_profile']['live_state'] = 'unknown'
        result['current_profile']['live_saved_agree'] = None
        result['current_active_plugins'] = {'status': 'unknown', 'provided_by_mod': [],
                                            'reason': 'Current profile changed during observation'}
    result['saved_usage_complete'] = (result['discovery']['complete'] and len(current_rows) == 1
                                      and saved['state'] != 'unknown' and bool(result['profiles'])
                                      and all(row['saved']['state'] != 'unknown' for row in result['profiles']))
    return result