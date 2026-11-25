"""Verified, identity-bound configuration writes and reversible recovery.

This module never rolls back automatically. A post-write failure preserves the
verified inverse backup and reports observed state, avoiding newer external work.
"""
import json
import os
import re
from pathlib import Path

from .common import atomic_write, contained_path, digest
from .configuration_encoding import encode_configuration

LIMIT = 1024 * 1024
SCHEMA = 2
_HASH = re.compile(r'^[0-9a-f]{64}$')


def _absolute(value):
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError('Recovery identity requires an absolute path')
    return os.path.normcase(str(Path(value).resolve()))


def _recorded_path(value):
    """Normalize untrusted recorded paths lexically; never probe their targets."""
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ValueError('Recovery identity requires an absolute recorded path')
    return os.path.normcase(os.path.normpath(value))


def _descriptor(params):
    scope = params.get('scope', 'mod')
    if scope not in {'mod', 'profile', 'overwrite'}:
        raise ValueError('Unknown configuration scope')
    relative = params.get('path')
    if not isinstance(relative, str) or not relative:
        raise ValueError('A relative configuration path is required')
    mod = params.get('mod') if scope == 'mod' else None
    if scope == 'mod' and (not isinstance(mod, str) or not mod):
        raise ValueError('A mod name is required')
    return {'scope': scope, 'mod': mod, 'path': relative.replace('\\', '/')}


def _identity(ops, descriptor):
    destination = ops.config_path(descriptor)
    paths = {'mods': _absolute(ops.o.modsPath()),
             'downloads': _absolute(ops.o.downloadsPath()),
             'overwrite': _absolute(ops.o.overwritePath())}
    profile_path = _absolute(ops.o.profilePath())
    if descriptor['scope'] == 'profile':
        root = profile_path
    elif descriptor['scope'] == 'overwrite':
        root = paths['overwrite']
    else:
        root = _absolute(ops.mod(descriptor['mod'], writable=True).absolutePath())
    identity = {'profile': ops.o.profileName(), 'profile_path': profile_path,
                'paths': paths, 'root': root, 'destination': _absolute(str(destination)),
                'bridge_data_path': _absolute(str(ops.data)),
                'game': ops.o.managedGame().gameShortName().casefold()}
    if not isinstance(identity['profile'], str) or not identity['profile']:
        raise ValueError('Recovery identity requires a profile name')
    return identity, destination


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Recovery metadata contains duplicate keys')
        result[key] = value
    return result


def _backup_file(folder, name):
    return contained_path(folder, name)


def _read_json(folder, name, limit=16 * LIMIT):
    path = _backup_file(folder, name)
    if path.stat().st_size > limit:
        raise ValueError('Recovery metadata exceeds its size limit')
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs)
    if not isinstance(value, dict):
        raise ValueError('Recovery metadata must be an object')
    return value


def _validate_snapshot(ops, folder, descriptor, identity):
    """Legacy proof uses original physical context, never profile/mod name alone."""
    snapshot = _read_json(folder, 'before.json')
    revision = snapshot.get('revision')
    unhashed = dict(snapshot)
    unhashed.pop('revision', None)
    if not isinstance(revision, str) or not _HASH.fullmatch(revision) or digest(
            json.dumps(unhashed, sort_keys=True).encode()) != revision:
        raise ValueError('Recovery snapshot integrity check failed')
    if snapshot.get('profile') != identity['profile']:
        raise ValueError('The configuration backup belongs to another profile')
    if _recorded_path(snapshot.get('profile_path')) != identity['profile_path']:
        raise ValueError('The configuration backup belongs to another profile folder')
    recorded_paths = snapshot.get('paths')
    if not isinstance(recorded_paths, dict) or {
            key: _recorded_path(recorded_paths.get(key)) for key in identity['paths']} != identity['paths']:
        raise ValueError('Configured instance paths differ from the configuration backup')
    recorded_instance = snapshot.get('recovery_identity')
    if recorded_instance is not None:
        if (not isinstance(recorded_instance, dict)
                or _recorded_path(recorded_instance.get('bridge_data_path')) != identity['bridge_data_path']
                or not isinstance(recorded_instance.get('game'), str)
                or recorded_instance['game'].casefold() != identity['game']):
            raise ValueError('The configuration backup belongs to another bridge/game instance')
    if descriptor['scope'] == 'profile':
        root = _recorded_path(snapshot['profile_path'])
    elif descriptor['scope'] == 'overwrite':
        root = _recorded_path(recorded_paths['overwrite'])
    else:
        mods = snapshot.get('mods')
        if not isinstance(mods, list):
            raise ValueError('Recovery snapshot has no mod identity evidence')
        matches = [mod for mod in mods if isinstance(mod, dict) and mod.get('name') == descriptor['mod']]
        if len(matches) != 1:
            raise ValueError('Recovery snapshot mod identity is missing or ambiguous')
        root = _recorded_path(matches[0].get('path'))
        if not Path(root).is_relative_to(Path(identity['paths']['mods'])):
            raise ValueError('Recovery mod root is outside the configured Mods directory')
    if root != identity['root'] or _recorded_path(str(contained_path(
            root, descriptor['path'], text_only=True))) != identity['destination']:
        raise ValueError('Configuration recovery root or destination changed')


def _state(path):
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > LIMIT:
        raise ValueError('Configuration must be a regular file no larger than 1 MiB')
    data = path.read_bytes()
    if len(data) > LIMIT:
        raise ValueError('Configuration file exceeds the 1 MiB limit')
    return data


def _summary(data):
    return {'exists': data is not None, 'sha256': digest(data) if data is not None else None,
            'bytes': len(data) if data is not None else None}


def _check_hash(value, exists):
    if exists:
        if not isinstance(value, str) or not _HASH.fullmatch(value):
            raise ValueError('Recovery metadata contains an invalid file hash')
    elif value is not None:
        raise ValueError('An absent recovery file must not have a hash')


def _manifest(descriptor, identity, before, after):
    return {**descriptor, 'schema_version': SCHEMA, 'identity': identity,
            'existed': before is not None,
            'before_sha256': digest(before) if before is not None else None,
            'before_size': len(before) if before is not None else None,
            'after_exists': after is not None,
            'after_sha256': digest(after) if after is not None else None,
            'after_size': len(after) if after is not None else None}


def _validate_manifest(value, identity):
    version = value.get('schema_version')
    legacy_fields = {'scope', 'mod', 'path', 'existed', 'before_sha256', 'after_sha256'}
    if version is None and set(value) != legacy_fields:
        raise ValueError('Ambiguous legacy recovery metadata; original fields are required')
    if version is not None and (type(version) is not int or version != SCHEMA):
        raise ValueError('Unsupported configuration recovery schema')
    if type(value.get('existed')) is not bool:
        raise ValueError('Recovery metadata has no valid prior existence state')
    after_exists = value.get('after_exists') if version == SCHEMA else True
    if type(after_exists) is not bool:
        raise ValueError('Recovery metadata has no valid resulting existence state')
    if 'before_sha256' not in value or 'after_sha256' not in value:
        raise ValueError('Recovery metadata is missing file hashes')
    _check_hash(value['before_sha256'], value['existed'])
    _check_hash(value['after_sha256'], after_exists)
    if version == SCHEMA:
        if value.get('identity') != identity:
            raise ValueError('Configuration recovery identity differs from the current destination')
        for side, exists in [('before', value['existed']), ('after', after_exists)]:
            size = value.get(side + '_size')
            if (exists and (type(size) is not int or not 0 <= size <= LIMIT)) or (not exists and size is not None):
                raise ValueError('Recovery metadata contains an invalid file size')
    return after_exists


def _legacy_journal(folder, manifest, identity):
    """A full snapshot alone cannot prove which file the old write targeted."""
    journal = _read_json(folder, 'result.json', LIMIT)
    result = journal.get('result')
    if journal.get('action') != 'write_config' or not isinstance(result, dict):
        raise ValueError('Legacy recovery requires a successful original write journal')
    if (_recorded_path(result.get('path')) != identity['destination']
            or result.get('sha256') != manifest['after_sha256']):
        raise ValueError('Legacy recovery journal destination or hash does not match')
    size = result.get('bytes')
    if type(size) is not int or not 0 <= size <= LIMIT:
        raise ValueError('Legacy recovery journal has no valid resulting byte count')
    return size


def _payload(folder, manifest):
    path = _backup_file(folder, 'config-before.bin')
    if not manifest['existed']:
        if path.exists():
            raise ValueError('Unexpected recovery payload for an absent prior file')
        return None
    data = _state(path)
    if data is None or digest(data) != manifest['before_sha256']:
        raise ValueError('Configuration recovery payload integrity check failed')
    if manifest.get('schema_version') == SCHEMA and len(data) != manifest['before_size']:
        raise ValueError('Configuration recovery payload size differs from its manifest')
    return data


def _exclusive_write(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if path.read_bytes() != data:
        raise OSError('Saved configuration recovery bytes failed verification')


def _prepare(ops, backup, descriptor, identity, before, after):
    backup = Path(backup)
    expected = contained_path(ops.data / 'backups', backup.name)
    if expected != backup.resolve() or backup.parent.resolve() != (ops.data / 'backups').resolve():
        raise ValueError('Configuration recovery destination is not a bridge backup folder')
    _validate_snapshot(ops, backup, descriptor, identity)
    manifest = _manifest(descriptor, identity, before, after)
    manifest_path = _backup_file(backup, 'config.json')
    payload_path = _backup_file(backup, 'config-before.bin')
    if manifest_path.exists() or payload_path.exists():
        raise FileExistsError('Configuration recovery files already exist; refusing to overwrite them')
    if before is not None:
        _exclusive_write(payload_path, before)
    encoded = json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8')
    _exclusive_write(manifest_path, encoded)
    _verify_prepared(ops, backup, descriptor, identity, manifest, before)
    return manifest


def _verify_prepared(ops, backup, descriptor, identity, manifest, before):
    if _read_json(backup, 'config.json', LIMIT) != manifest:
        raise ValueError('Saved configuration recovery manifest changed')
    _validate_manifest(manifest, identity)
    _validate_snapshot(ops, backup, descriptor, identity)
    if _payload(backup, manifest) != before:
        raise ValueError('Saved configuration recovery payload changed')


def _guard(ops, descriptor, identity, path, expected):
    actual_identity, actual_path = _identity(ops, descriptor)
    if actual_identity != identity or actual_path != path:
        raise ValueError('Configuration identity changed during the operation')
    if _state(path) != expected:
        raise ValueError('Configuration file changed during the operation; newer work was preserved')


def _apply(ops, backup, descriptor, identity, path, before, after, manifest, progress):
    progress['stage'] = 'verify-recovery'
    _verify_prepared(ops, backup, descriptor, identity, manifest, before)
    progress['stage'] = 'last-target-check'
    _guard(ops, descriptor, identity, path, before)
    if before != after:
        progress.update(stage='apply-target', changes_may_have_occurred=True)
        if after is None:
            path.unlink()
        else:
            def before_replace():
                # Staging flush/fsync may take time: guard again immediately
                # before replacement, including the preserved recovery payload.
                _verify_prepared(ops, backup, descriptor, identity, manifest, before)
                _guard(ops, descriptor, identity, path, before)
            atomic_write(path, after, before_replace=before_replace)
        if (before is None) != (after is None):
            progress['stage'] = 'refresh'
            ops.refresh()
    progress['stage'] = 'verify-final-state'
    _guard(ops, descriptor, identity, path, after)
    progress['stage'] = 'complete'


def _annotate(error, progress, backup, identity=None, descriptor=None):
    details = {**progress, 'backup_id': Path(backup).name if backup is not None else None,
               'recovery_backup_path': str(backup) if backup is not None else None,
               'automatic_rollback_attempted': False}
    if identity is not None:
        details['target'] = identity['destination']
        try:
            path = contained_path(identity['root'], descriptor['path'], text_only=True)
            if _absolute(str(path)) != identity['destination']:
                raise ValueError('Original recovery destination changed')
            details['observed'] = _summary(_state(path))
        except Exception as observation_error:
            details['observation_error'] = str(observation_error)
    error.recovery_details = details


def write_configuration(ops, params, backup):
    progress = {'stage': 'validate-request', 'changes_may_have_occurred': False}
    identity, descriptor = None, None
    try:
        descriptor = _descriptor(params)
        identity, path = _identity(ops, descriptor)
        old = _state(path)
        if params.get('expected_sha256') != (digest(old) if old is not None else None):
            raise ValueError('Configuration file changed. Read it again before writing.')
        text = params['text']
        if not isinstance(text, str) or '\x00' in text:
            raise ValueError('Text must be a string without null characters')
        if path.suffix.lower() == '.json':
            json.loads(text)
        new, output_format = encode_configuration(old, text, params)
        if len(new) > LIMIT:
            raise ValueError('Configuration file exceeds the 1 MiB limit')
        progress.update(stage='preserve-current', before=_summary(old), requested_after=_summary(new))
        manifest = _prepare(ops, backup, descriptor, identity, old, new)
        _apply(ops, backup, descriptor, identity, path, old, new, manifest, progress)
        return {'path': str(path), 'sha256': digest(new), 'bytes': len(new), 'recovery_verified': True, **output_format}
    except Exception as error:
        _annotate(error, progress, backup, identity, descriptor)
        raise


def restore_configuration(ops, params, backup):
    progress = {'stage': 'validate-source', 'changes_may_have_occurred': False}
    identity, descriptor = None, None
    try:
        source = ops.get_backup(params['backup_id'])
        if Path(source).resolve() == Path(backup).resolve():
            raise ValueError('Source and destination recovery backups must differ')
        manifest = _read_json(source, 'config.json', LIMIT)
        descriptor = _descriptor(manifest)
        identity, path = _identity(ops, descriptor)
        _validate_snapshot(ops, source, descriptor, identity)
        after_exists = _validate_manifest(manifest, identity)
        journal_size = _legacy_journal(source, manifest, identity) if manifest.get('schema_version') is None else None
        desired = _payload(source, manifest)
        current = _state(path)
        if journal_size is not None and (current is None or len(current) != journal_size):
            raise ValueError('Current configuration byte count differs from the legacy journal')
        if (current is not None) != after_exists or (digest(current) if current is not None else None) != manifest['after_sha256']:
            raise ValueError('File changed since this backup; restore would overwrite later work')
        if manifest.get('schema_version') == SCHEMA and current is not None and len(current) != manifest['after_size']:
            raise ValueError('Current configuration size differs from the recovery manifest')
        progress.update(stage='preserve-current', before=_summary(current), requested_after=_summary(desired),
                        source_backup=source.name)
        inverse = _prepare(ops, backup, descriptor, identity, current, desired)
        _apply(ops, backup, descriptor, identity, path, current, desired, inverse, progress)
        return {'restored': str(path), 'source_backup': source.name,
                'exists': desired is not None, 'sha256': digest(desired) if desired is not None else None,
                'recovery_verified': True}
    except Exception as error:
        _annotate(error, progress, backup, identity, descriptor)
        raise
