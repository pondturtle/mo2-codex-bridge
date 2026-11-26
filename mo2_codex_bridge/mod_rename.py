"""Plan and verify a single MO2 2.5.2 native directory-name edit."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

from .common import safe_name, write_json
from .mod_change_previews import ModChangePlan
from .mod_placement import checked_mod_folder, ordered_mods, require_same_context
from .native_rename_model import rename_in_mod_model
from .planned_profile_state import (
    PROFILE_FILES, check_saved_plugins, expected_modlist, keep_profile_files,
    modlist_rows, profile_file_hashes, read_profile_files,
)
from .profile_usage import (
    MAX_PROFILES, MAX_PROFILE_ENTRIES, MAX_TOTAL_BYTES, _aliases, _bounded, _lexical,
    _plain_name, _read_saved, _safe_stat, _signature,
)

MAX_RENAME_ENTRIES = 12000
MAX_RENAME_BYTES = 512 * 1024 * 1024
PGLITE_RULES = 'SKSE/Plugins/PGLite/modrules.json'


def checked_destination(ops, folder, name, new_name):
    new_name = safe_name(new_name)
    key = new_name.casefold()
    if name.casefold() == key:
        raise ValueError('Use a distinct mod name; case-only renames are unavailable')
    first_component = new_name.split('.')[0].rstrip().upper()
    if (' '.join(new_name.split()) != new_name or key == 'overwrite'
            or key.endswith('_separator') or re.search(r'backup[0-9]*\Z', key)
            or first_component in {'CONIN$', 'CONOUT$'}
            or re.fullmatch(r'(COM|LPT)[¹²³]', first_component)):
        raise ValueError('The new name is reserved or would be normalized by MO2')
    destination = folder.with_name(new_name)
    if len(str(destination).encode('utf-16-le')) // 2 >= 248:
        raise ValueError('The destination exceeds the supported Windows shell path length')
    names = _bounded(ops.o.modList().allMods(), MAX_RENAME_ENTRIES, 'Native mod inventory')
    if not all(isinstance(entry, str) for entry in names):
        raise ValueError('Unknown mod inventory identity')
    if any(entry.casefold() == key for entry in names):
        raise ValueError('The new mod name is already in use')
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    raise ValueError('The destination mod folder already exists')


def mod_tree(root, destination, *, include_hash=True):
    """Do not follow reparse points, and bind both file names and file contents."""
    manifest, remaining = {}, MAX_RENAME_BYTES
    pending = [root]
    while pending:
        path = pending.pop()
        before = _safe_stat(path, directory=True)
        with os.scandir(path) as stream:
            entries = _bounded(stream, MAX_RENAME_ENTRIES - len(manifest), 'Mod contents')
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(root).as_posix()
            # Windows DirEntry.stat omits inode/device/link count. lstat
            # supplies the filesystem identity used to guard the open.
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Linked files and directories are not supported for native rename')
            if len(str(destination / relative).encode('utf-16-le')) // 2 >= 260:
                raise ValueError('A renamed mod file would exceed the supported Windows path length')
            signature = _signature(info)
            entry_data = {'signature': signature}
            if stat.S_ISDIR(info.st_mode):
                entry_data['type'] = 'directory'
                pending.append(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                entry_data['type'] = 'file'
                remaining -= info.st_size
                if remaining < 0:
                    raise ValueError('Mod contents exceed the bounded rename verification size')
                if include_hash:
                    with child.open('rb') as stream:
                        if _signature(os.fstat(stream.fileno())) != signature:
                            raise ValueError('A mod file changed before it could be verified')
                        entry_data['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
                        if _signature(os.fstat(stream.fileno())) != signature:
                            raise ValueError('A mod file changed during verification')
                    if _signature(child.lstat()) != signature:
                        raise ValueError('A mod file changed during verification')
            else:
                raise ValueError('Nonregular or hard-linked mod files cannot be verified for rename')
            manifest[relative] = entry_data
        if _signature(path.lstat()) != _signature(before):
            raise ValueError('A mod folder changed during verification')
    return manifest


def read_profiles(before):
    current = Path(before['profile_path'])
    root = current.parent
    _safe_stat(root, directory=True)
    # Never enumerate a profile root that could include the instance's Mods,
    # Overwrite or downloads directory among its supposed profile directories.
    for path in before['paths'].values():
        if Path(_lexical(path)).is_relative_to(Path(_lexical(root))):
            raise ValueError('The native profiles root overlaps another MO2 storage root')
    with os.scandir(root) as stream:
        entries = _bounded(stream, MAX_PROFILE_ENTRIES, 'Saved profile root')
    profiles, seen = {}, set()
    budget = [MAX_TOTAL_BYTES]
    for entry in sorted(entries, key=lambda item: item.name.casefold()):
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('A linked profile entry prevents safe all-profile verification')
        if not stat.S_ISDIR(info.st_mode):
            continue
        key = entry.name.casefold()
        if len(profiles) >= MAX_PROFILES or key in seen or not _plain_name(entry.name):
            raise ValueError('Saved profile names are ambiguous or exceed the verification limit')
        seen.add(key)
        folder = Path(entry.path)
        is_current = _lexical(folder) == _lexical(current)
        files = list(PROFILE_FILES) if is_current else ['modlist.txt']
        if not is_current:
            for file in PROFILE_FILES[1:]:
                try:
                    (folder / file).lstat()
                except FileNotFoundError:
                    continue
                files.append(file)
        profiles[entry.name] = read_profile_files(folder, files, budget)
    if current.name not in profiles:
        raise ValueError('The active profile was not found at its exact saved path')
    return profiles


def planned_profile_rows(saved, name, new_name):
    rows = modlist_rows(saved)
    old_key, new_key = name.casefold(), new_name.casefold()
    aliases = _aliases(name) | _aliases(new_name)
    for marker, entry in rows:
        key = entry.casefold()
        if key == new_key or key in aliases or (key == old_key and (entry != name or marker == '*')):
            raise ValueError('A saved profile has an ambiguous or existing destination mod name')
    return [(marker, new_name if entry == name else entry) for marker, entry in rows]


def verify_owned_plugins(ops, folder, mod_name, owned):
    for plugin, relative in owned.items():
        winner = ops.o.resolvePath(plugin)
        origins = list(ops.o.getFileOrigins(plugin))
        if (not isinstance(winner, str) or not Path(winner).is_absolute()
                or _lexical(winner) != _lexical(folder / relative)
                or not origins or origins[0] != mod_name):
            raise RuntimeError('The effective plugin provider could not be verified: ' + plugin)


def unique_rule_keys(items):
    result = dict(items)
    if len(result) != len(items):
        raise ValueError('PGLite file contains duplicate JSON keys')
    return result


def known_references(ops, name, new_name):
    current = {'path': PGLITE_RULES, 'scope': 'Active profile virtual Data only'}
    evidence = None
    try:
        winner = ops.o.resolvePath(PGLITE_RULES)
        if not winner:
            current['status'] = 'not_resolved'
        else:
            path = Path(winner)
            mods = Path(ops.o.modsPath())
            overwrite = Path(ops.o.overwritePath())
            if not path.is_absolute():
                raise ValueError('Resolved PGLite configuration is not an absolute path')
            root = Path(_lexical(path))
            if root.is_relative_to(Path(_lexical(mods))):
                parts = root.relative_to(Path(_lexical(mods))).parts
                expected = Path(_lexical(mods / parts[0] / PGLITE_RULES)) if parts else None
            else:
                expected = Path(_lexical(overwrite / PGLITE_RULES))
            if root != expected:
                raise ValueError('Effective PGLite path is outside supported managed storage')
            data = _read_saved(path, [MAX_TOTAL_BYTES])
            rules = json.loads(data.decode('utf-8-sig'), object_pairs_hook=unique_rule_keys)
            if not isinstance(rules, dict):
                raise ValueError('Invalid PGLite rule object')
            current.update(status='checked', exact_old_name=name in rules,
                           old_name_case_aliases=[key for key in rules if key != name and key.casefold() == name.casefold()],
                           destination_name_in_use=any(key.casefold() == new_name.casefold() for key in rules))
            evidence = profile_file_hashes({'pglite_rules': data})['pglite_rules']
    except (OSError, ValueError):
        current['status'] = 'unknown'
    return {
        'pglite': current,
        'other_tools': 'Exact name references may require review. Check PGPatcher cfg/modrules.json '
                       'and settings or scripts that use the old mod name. No external references are edited.',
    }, evidence


def verify_renamed_profile_files(profiles, original, expected):
    if profiles.keys() != original.keys():
        raise RuntimeError('Saved profiles changed during the rename')
    complete = True
    for profile, files in profiles.items():
        old_data = original[profile]['modlist.txt']
        old_rows = modlist_rows(old_data)
        if files.keys() != original[profile].keys() or any(
                data != original[profile][file] for file, data in files.items() if file != 'modlist.txt'):
            raise RuntimeError('Other profile lists changed during the rename: ' + profile)
        if old_rows == expected[profile] and files['modlist.txt'] != original[profile]['modlist.txt']:
            raise RuntimeError('An unrelated profile modlist changed during the rename: ' + profile)
        if modlist_rows(files['modlist.txt']) != expected[profile]:
            if files['modlist.txt'] != old_data:
                raise RuntimeError('Saved modlist did not match the rename plan: ' + profile)
            complete = False
    return complete


def build_rename(ops, params, before):
    if set(params) != {'mod', 'new_name'}:
        raise ValueError('Rename requires mod and new_name')
    if ops.o.appVersion().displayString(3) != '2.5.2':
        raise ValueError('Native mod renaming is supported on MO2 2.5.2 only')
    name, new_name = params['mod'], params['new_name']
    folder, folder_identity = checked_mod_folder(ops, name)
    destination = checked_destination(ops, folder, name, new_name)
    mods = ordered_mods(before)
    if name not in {row['name'] for row in mods}:
        raise ValueError('The selected mod is missing from the current profile')
    profiles = read_profiles(before)
    current_profile = Path(before['profile_path']).name
    current_files = profiles[current_profile]
    if modlist_rows(current_files['modlist.txt']) != expected_modlist(mods):
        raise ValueError('Saved modlist differs from the current MO2 profile; wait for MO2 to save')
    check_saved_plugins(ops.o, current_files, before['plugins'])
    expected = {profile: planned_profile_rows(files['modlist.txt'], name, new_name)
                for profile, files in profiles.items()}
    affected = [profile for profile, files in profiles.items()
                if modlist_rows(files['modlist.txt']) != expected[profile]]
    contents = mod_tree(folder, destination)
    root_files = {item.casefold(): item for item, details in contents.items()
                  if '/' not in item and details['type'] == 'file'}
    owned = {plugin['name']: root_files.get(plugin['name'].casefold())
             for plugin in before['plugins'] if plugin['origin'] == name}
    if any(relative is None for relative in owned.values()):
        raise ValueError('Some plugin files attributed to the mod are unavailable for verification')
    verify_owned_plugins(ops, folder, name, owned)
    references, reference_hash = known_references(ops, name, new_name)
    recovery = {'method': 'Inspect the folder and every profile list before recovery. '
                          'The backup contains exact pre-change profile files and a mod contents manifest.',
                'reverse_rename': {'mod': new_name, 'new_name': name}}
    summary = {
        'mod': name, 'new_name': new_name, 'no_op': False, 'shared_mod_folder': True,
        'affected_profiles': affected, 'checked_profiles': list(profiles),
        'file_count': sum(row['type'] == 'file' for row in contents.values()),
        'external_references': references, 'recovery': recovery,
    }
    evidence = {
        'profiles': {profile: profile_file_hashes(files) for profile, files in profiles.items()},
        'folder_identity': folder_identity, 'folder_manifest': contents, 'reference_hash': reference_hash,
    }
    expected_mods = [{**row, 'name': new_name, 'display_name': new_name, 'path': _lexical(destination)}
                     if row['name'] == name else row for row in mods]
    expected_plugins = [{**row, 'origin': new_name} if row['origin'] == name else row
                        for row in before['plugins']]

    def apply(backup):
        entered_native = False
        try:
            if not callable(ops.wait_until):
                raise RuntimeError('Saved profile verification is unavailable')
            for profile, files in profiles.items():
                keep_profile_files(backup / 'saved-profiles' / profile, files)
            write_json(backup / 'planned-change.json', {'action': 'rename_mod', **summary, **evidence})
            current = require_same_context(ops, before)
            content_signatures = {rel: {key: value for key, value in item.items() if key != 'sha256'}
                                  for rel, item in contents.items()}
            if (current['revision'] != before['revision'] or read_profiles(before) != profiles
                    or checked_mod_folder(ops, name) != (folder, folder_identity)
                    or checked_destination(ops, folder, name, new_name) != destination
                    or mod_tree(folder, destination, include_hash=False) != content_signatures
                    or known_references(ops, name, new_name) != (references, reference_hash)):
                raise ValueError('The rename preview became stale before the change')
            if require_same_context(ops, before)['revision'] != before['revision']:
                raise ValueError('MO2 state changed before the native rename')
            verify_owned_plugins(ops, folder, name, owned)
            entered_native = True
            if rename_in_mod_model(ops.o, name, new_name) is not True:
                raise RuntimeError('MO2 did not confirm its native mod-name edit')
            try:
                folder.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError('The old mod folder still exists after the native rename')
            actual_path, actual_identity = checked_mod_folder(ops, new_name)
            if _lexical(actual_path) != _lexical(destination) or actual_identity != folder_identity:
                raise RuntimeError('The renamed mod does not match the original folder identity')
            if mod_tree(destination, destination) != contents:
                raise RuntimeError('Mod contents differ after the native rename')

            def verify(*, allow_cached_origin):
                observed = require_same_context(ops, before)
                observed_mods = [{**row, 'path': _lexical(row['path'])} if row['name'] == new_name else row
                                 for row in observed['mods']]
                observed_plugins = observed['plugins']
                if allow_cached_origin:
                    # The rename signal updates VFS origin names but the 2.5.2
                    # plugin list retains its cached origin until refresh.
                    observed_plugins = [
                        {**row, 'origin': new_name}
                        if row['name'] in owned and row['origin'] == name else row
                        for row in observed_plugins
                    ]
                if observed_mods != expected_mods or observed_plugins != expected_plugins:
                    raise RuntimeError('Live mod order, activation or plugin state differs after the native rename')
                if not allow_cached_origin:
                    verify_owned_plugins(ops, destination, new_name, owned)
                return verify_renamed_profile_files(read_profiles(before), profiles, expected)

            def saved_ready():
                return verify(allow_cached_origin=True)

            if not ops.wait_until(saved_ready, 5000) or not saved_ready():
                raise RuntimeError('Saved modlist did not match the rename plan before verification timed out')
            # Confirm the native rewrite before a refresh can discard evidence
            # of a partial edit. Refresh only when the renamed mod owns plugins.
            if owned:
                ops.refresh()
            if not verify(allow_cached_origin=False):
                raise RuntimeError('Saved modlist no longer matches the rename plan after verification')
            if mod_tree(destination, destination, include_hash=False) != content_signatures:
                raise RuntimeError('Mod contents changed while waiting for saved profile verification')
        except Exception as error:
            error.recovery_details = {**recovery, 'changes_may_have_occurred': entered_native,
                                      'automatic_rollback_attempted': False}
            raise
        return {**summary, 'changed': True, 'verified_profiles': list(profiles),
                'mod_files_verified': True, 'plugin_state_verified': True}

    return ModChangePlan(summary, evidence, apply)

