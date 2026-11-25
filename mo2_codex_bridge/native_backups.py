"""Read native mod-backup metadata without touching backup folders.

The association rule is pinned to MO2 2.5.2 restoreBackup().  It is an
inference from a name, not evidence that a backup was complete when created,
that its files exist now, or that restoring it would be safe.
"""
import hashlib
import json
import ntpath
import re


_MAX_MODS = 10000
_MAX_PAGE = 200
_RESTORE_NAME = re.compile(r"(.*)_backup[0-9]*\Z")
_BACKUP_NAME = re.compile(r".*backup[0-9]*\Z")
_RESERVED = {'CON', 'PRN', 'AUX', 'NUL',
             *(f'COM{i}' for i in range(10)), *(f'LPT{i}' for i in range(10)),
             'COM\u00b9', 'COM\u00b2', 'COM\u00b3', 'LPT\u00b9', 'LPT\u00b2', 'LPT\u00b3'}


def _call(owner, method, *args):
    function = getattr(owner, method, None)
    if not callable(function):
        raise RuntimeError('Native backup inventory is unsupported: missing ' + method)
    try:
        return function(*args)
    except Exception as error:
        raise RuntimeError('Native backup inventory is incomplete: ' + method + ' failed') from error


def _text(value, label, maximum=32767):
    if (not isinstance(value, str) or not value or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise RuntimeError('Native backup inventory is incomplete: invalid ' + label)
    try:
        value.encode('utf-8')
    except UnicodeError as error:
        raise RuntimeError('Native backup inventory is incomplete: invalid ' + label) from error
    return value


def _path_key(value):
    """Lexical Windows comparison only; deliberately performs no filesystem IO."""
    if not isinstance(value, str):
        return None
    value = value.replace('/', '\\')
    drive, tail = ntpath.splitdrive(value)
    if (not drive or not tail.startswith('\\')
            or value.startswith(('\\\\?\\', '\\\\.\\'))
            or any(part in {'.', '..'} for part in tail.split('\\'))):
        return None
    return ntpath.normcase(ntpath.normpath(value))


def _ordinary_name_problem(name):
    if (not name or name != name.strip() or name in {'.', '..'}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.endswith(('.', ' ')) or len(name.encode('utf-16-le')) > 510):
        return 'The derived name is not an ordinary supported Windows mod basename'
    if name.split('.')[0].upper() in _RESERVED:
        return 'The derived name is a reserved Windows basename'
    if _BACKUP_NAME.fullmatch(name) or name.endswith('_separator'):
        return 'The derived name identifies another backup or a separator'
    return None


def _read_model(organizer):
    version = _call(_call(organizer, 'appVersion'), 'displayString', 3)
    if version != '2.5.2':
        raise RuntimeError('Native backup name association supports MO2 2.5.2 only')
    root = _text(_call(organizer, 'modsPath'), 'Mods path')
    if _path_key(root) is None:
        raise RuntimeError('Native backup inventory is unsupported: Mods path is not an ordinary absolute Windows path')
    mod_list = _call(organizer, 'modList')
    names = _call(mod_list, 'allMods')
    if not isinstance(names, (list, tuple)) or len(names) > _MAX_MODS:
        raise RuntimeError('Native backup inventory is incomplete: unsupported or oversized allMods result')
    names = [_text(name, 'mod name', 512) for name in names]
    if len(set(names)) != len(names):
        raise RuntimeError('Native backup inventory is incomplete: duplicate mod names')
    entries = []
    for name in names:
        mod = _call(mod_list, 'getMod', name)
        if mod is None:
            raise RuntimeError('Native backup inventory is incomplete: a listed mod disappeared')
        entry = {'name': name}
        for flag in ('isBackup', 'isForeign', 'isOverwrite', 'isSeparator'):
            value = _call(mod, flag)
            if type(value) is not bool:
                raise RuntimeError('Native backup inventory is incomplete: invalid ' + flag + ' result')
            entry[flag] = value
        # allMods/getMod use internal names. Foreign names are localized for
        # display and their absolutePath can be empty; neither is a backup path.
        display = _text(_call(mod, 'name'), 'mod display name', 512)
        if _call(mod_list, 'displayName', name) != display:
            raise RuntimeError('Native backup inventory is incomplete: a listed mod changed identity')
        entry['display_name'] = display
        entry['path'] = (None if entry['isForeign'] else
                         _text(_call(mod, 'absolutePath'), 'mod path'))
        entries.append(entry)
    entries.sort(key=lambda item: (item['name'].casefold(), item['name']))
    return {'mo2_version': version, 'mods_root': root, 'entries': entries}


def _direct_child(entry, root):
    return _path_key(entry['path']) == ntpath.join(_path_key(root), entry['name'].lower())


def _associate(backup, names, root):
    result = {'status': 'unresolved', 'inferred_mod_name': None,
              'target_mod_name': None, 'target_mod_path': None,
              'basis': 'MO2 2.5.2 restore-name rule; metadata inference only'}
    match = _RESTORE_NAME.fullmatch(backup['name'])
    if match is None:
        return dict(result, reason='Native backup classification does not supply a restore-name mapping')
    inferred = match.group(1)
    result['inferred_mod_name'] = inferred
    problem = _ordinary_name_problem(inferred)
    if problem:
        return dict(result, status='invalid_mapping', reason=problem)
    if (backup['isForeign'] or backup['isOverwrite'] or backup['isSeparator']
            or not _direct_child(backup, root)):
        return dict(result, status='invalid_mapping', reason='Backup metadata does not identify a direct managed Mods child')
    candidates = names.get(inferred.casefold(), [])
    if not candidates:
        return dict(result, status='missing_target', reason='The inferred original is absent from the native mod model')
    if len(candidates) > 1:
        return dict(result, status='ambiguous', reason='Multiple native mod names compare alike',
                    candidate_target_names=[item['name'] for item in candidates[:20]],
                    candidate_count=len(candidates), candidates_truncated=len(candidates) > 20)
    target = candidates[0]
    if target['name'] != inferred:
        return dict(result, reason='Only a case-folded name candidate exists; exact association is unverified',
                    candidate_target_names=[target['name']])
    if (any(target[flag] for flag in ('isBackup', 'isForeign', 'isOverwrite', 'isSeparator'))
            or not _direct_child(target, root)):
        return dict(result, status='invalid_mapping', reason='The inferred target is not an ordinary direct managed Mods child')
    return dict(result, status='inferred', target_mod_name=target['name'], target_mod_path=target['path'],
                reason='Exact native model name and lexical path agree; contents and restore safety are unverified')


def _parameters(params):
    allowed = {'mod', 'offset', 'limit', 'expected_inventory_revision'}
    if not isinstance(params, dict) or set(params) - allowed:
        raise ValueError('Use only mod, offset, limit and expected_inventory_revision')
    mod = params.get('mod')
    if 'mod' in params and (not isinstance(mod, str) or not mod or len(mod) > 512
                            or any(ord(char) < 32 or ord(char) == 127 for char in mod)):
        raise ValueError('mod must be an exact nonempty native mod name')
    if mod is not None:
        try:
            mod.encode('utf-8')
        except UnicodeError as error:
            raise ValueError('mod must be an exact valid native mod name') from error
    offset, limit = params.get('offset', 0), params.get('limit', 100)
    if type(offset) is not int or not 0 <= offset <= _MAX_MODS:
        raise ValueError('offset must be an integer from 0 to 10000')
    if type(limit) is not int or not 1 <= limit <= _MAX_PAGE:
        raise ValueError('limit must be an integer from 1 to 200')
    revision = params.get('expected_inventory_revision')
    if 'expected_inventory_revision' in params and (not isinstance(revision, str)
            or re.fullmatch(r'[0-9a-f]{64}', revision) is None):
        raise ValueError('expected_inventory_revision must be a returned inventory revision')
    return mod, offset, limit, revision


def browse_native_mod_backups(organizer, params):
    """Return a stable page of native backup metadata; never read mod contents.

    ``mod`` is an exact name filter, including exact names of explicitly labelled
    association candidates.  A missing original can still have listed backups.
    Use ``expected_inventory_revision`` to refuse changed metadata between pages.
    """
    selected, offset, limit, expected_revision = _parameters(params)
    model = _read_model(organizer)
    if _read_model(organizer) != model:
        raise RuntimeError('Native backup inventory changed during inspection; no partial inventory returned')
    revision = hashlib.sha256(json.dumps(model, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')).hexdigest()
    if expected_revision is not None and expected_revision != revision:
        raise ValueError('Native backup inventory changed between pages; start again')
    names = {}
    for entry in model['entries']:
        names.setdefault(entry['name'].casefold(), []).append(entry)
    backups = []
    for entry in model['entries']:
        if entry['isBackup']:
            backups.append({'name': entry['name'], 'display_name': entry['display_name'],
                            'path': entry['path'], 'native_is_backup': True,
                            'association': _associate(entry, names, model['mods_root']),
                            'content_verified': False, 'historical_completeness': 'unknown',
                            'restore_ready': False})
    selection = {'mod': selected, 'status': 'not_requested'}
    matched = backups
    if selected is not None:
        exact = [entry for entry in model['entries'] if entry['name'] == selected]
        selection['status'] = 'resolved' if (exact and len(names[selected.casefold()]) == 1
            and not any(exact[0][flag] for flag in ('isBackup', 'isForeign', 'isOverwrite', 'isSeparator'))
            and not _ordinary_name_problem(selected) and _direct_child(exact[0], model['mods_root'])) else 'unresolved'
        selection['reason'] = ('Exact ordinary mod name exists in the native model; backup associations remain inferred'
                               if selection['status'] == 'resolved' else
                               'No exact ordinary original was resolved; matching inferred or candidate names may still be shown')
        def matches(entry):
            association = entry['association']
            inferred = association['inferred_mod_name']
            return (selected in (inferred, association['target_mod_name'])
                    or (inferred is not None and any(candidate['name'] == selected
                        for candidate in names.get(inferred.casefold(), []))))
        matched = [entry for entry in backups if matches(entry)]
    page = matched[offset:offset + limit]
    return {'read_only': True, 'scope': 'Native MO2 mod-content backup metadata; not bridge snapshots or external backups',
            'mo2_version': model['mo2_version'], 'mods_root': model['mods_root'],
            'model_inventory_complete': True, 'inventory_revision': revision,
            'consistency_check': 'Two matching complete metadata reads; not an atomic filesystem snapshot',
            'revision_scope': 'Native names, paths and classification only; excludes file contents and profile states',
            'content_checked': False, 'historical_backup_completeness': 'unknown', 'restore_supported': False,
            'path_verification': 'Lexical metadata comparison only; filesystem links and directory contents were not inspected',
            'filter': selection, 'total_backups': len(backups), 'matched_backups': len(matched),
            'offset': offset, 'limit': limit, 'returned': len(page),
            'next_offset': offset + len(page) if offset + len(page) < len(matched) else None,
            'backups': page}
