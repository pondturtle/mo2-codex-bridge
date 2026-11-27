"""Install the MO2 component and its permanent, rollback-capable connector."""
import argparse
import ast
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from mo2_codex_bridge.common import VERSION

RECORD = '.bridge-install.json'
CONNECTOR = 'mo2_bridge.pyz'
CONNECTOR_RECORD = '.bridge-connector.json'
MAX_CONNECTOR_BYTES = 16 * 1024 * 1024
REQUIRED = {'__init__.py', 'common.py', 'dialogs.py', 'operations.py', 'persistence.py', 'plugin.py', 'transport.py',
            'config_recovery.py', 'mod_persistence.py', 'order_recovery.py', 'configuration_encoding.py',
            'managed_operations.py', 'job_store.py', 'running_apps.py', 'steam_bootstrap.py',
            'native_backups.py', 'profile_usage.py', 'nexus_links.py', 'nexus_catalog.py',
            'nexus_native.py', 'nexus_archive.py', 'nexus_downloads.py',
            'mod_change_previews.py', 'mod_placement.py', 'planned_profile_state.py',
            'mod_rename.py', 'native_rename_model.py'}


class SetupInstallError(RuntimeError):
    def __init__(self, message, details=None):
        self.details = dict(details or {}, message=message)
        super().__init__(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def safe_path(path, root):
    path, root = Path(path), Path(root).resolve()
    if not path.resolve().is_relative_to(root):
        raise ValueError('Setup path escapes its selected root')
    current = path
    while current != root:
        if linked(current):
            raise ValueError('Linked setup paths are refused')
        if current.parent == current:
            raise ValueError('Invalid setup path')
        current = current.parent
    if linked(root):
        raise ValueError('Linked setup roots are refused')
    return path


def validate_mo2(path):
    supplied = Path(path).expanduser().absolute()
    if linked(supplied) or linked(supplied / 'plugins'):
        raise ValueError('Linked MO2 installations are refused')
    mo2 = supplied.resolve()
    if not (mo2 / 'ModOrganizer.exe').is_file() or not (mo2 / 'plugins/plugin_python').is_dir():
        raise ValueError('Select an MO2 installation with ModOrganizer.exe and Python plugin support')
    return mo2


def ensure_mo2_closed():
    if os.name != 'nt':
        raise ValueError('The MO2 component requires Windows')
    try:
        # tasklist emits Windows OEM text, which can differ from Python's
        # default ANSI/UTF-8 encoding. Never discard undecodable bytes.
        result = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True,
                                text=True, encoding='oem', errors='strict',
                                timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as error:
        raise ValueError('Cannot verify that MO2 is closed: process query failed') from error
    if result.returncode != 0:
        raise ValueError('Cannot verify that MO2 is closed: process query failed')
    try:
        if not isinstance(result.stdout, str):
            raise ValueError('Process query did not return decoded text')
        rows = [row for row in csv.reader(io.StringIO(result.stdout), strict=True) if row]
        if not rows or any(
                len(row) != 5 or not row[0] or row[0] != row[0].strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in ''.join(row))
                or any(char in row[0] for char in '\\/:*<>|"')
                or re.fullmatch(r'[0-9]+', row[1]) is None
                or not row[2].strip()
                or re.fullmatch(r'[0-9]+', row[3]) is None
                or not row[4].strip()
                for row in rows):
            raise ValueError('Invalid process rows')
    except (csv.Error, ValueError) as error:
        raise ValueError('Cannot verify that MO2 is closed: invalid process query output') from error
    if any(row[0].casefold() == 'modorganizer.exe' for row in rows):
        raise ValueError('Close MO2 before installing or upgrading its bridge')


def inventory(root):
    root = Path(root)
    if not root.exists():
        return None
    safe_path(root, root.parent)
    if not root.is_dir():
        raise ValueError('Bridge component must be a directory')
    result = {}
    for path in sorted(root.rglob('*')):
        safe_path(path, root)
        if path.is_dir():
            if path.relative_to(root).as_posix() != '__pycache__':
                raise ValueError('Unexpected subdirectory in bridge component')
            continue
        relative = path.relative_to(root).as_posix()
        valid = (path.parent == root and (path.suffix == '.py' or path.name == RECORD)) or (
            path.parent == root / '__pycache__' and path.suffix == '.pyc')
        if not valid or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Unexpected file in bridge component; preserve and inspect it: ' + relative)
        raw = path.read_bytes()
        result[relative] = {'sha256': digest(raw), 'bytes': len(raw)}
    return result


def code_inventory(files):
    return {name: data['sha256'] for name, data in files.items() if '/' not in name and name.endswith('.py')}


def source_payload(source):
    root = Path(source) / 'mo2_codex_bridge'
    files = inventory(root)
    if files is None or not REQUIRED <= set(files):
        raise ValueError('Bridge release is missing required modules')
    payload = {}
    for name in code_inventory(files):
        raw = (root / name).read_bytes()
        ast.parse(raw, filename=name)
        payload[name] = raw
    for name, raw in payload.items():
        for node in ast.walk(ast.parse(raw, filename=name)):
            if isinstance(node, ast.ImportFrom) and node.level:
                if node.level != 1:
                    raise ValueError('Component uses an unsupported parent-package import: ' + name)
                needed = [node.module.split('.')[0]] if node.module else [alias.name for alias in node.names]
                if any(module + '.py' not in payload for module in needed):
                    raise ValueError('Bridge release is missing a required imported module: ' + name)
    if {n: digest(raw) for n, raw in payload.items()} != code_inventory(files) or inventory(root) != files:
        raise ValueError('Release component changed while it was being staged')
    common = ast.parse(payload['common.py'])
    versions = [ast.literal_eval(n.value) for n in common.body if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'VERSION' for t in n.targets)]
    if versions != [VERSION]:
        raise ValueError('Setup and component versions do not match')
    return payload


def expected_record(value):
    if not isinstance(value, dict) or not isinstance(value.get('version'), str) or not isinstance(value.get('files'), dict):
        raise ValueError('Invalid installed-component manifest')
    files = value['files']
    if not files or any(not isinstance(n, str) or '/' in n or '\\' in n or not n.endswith('.py')
                        or not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for n, h in files.items()):
        raise ValueError('Invalid installed-component hashes')
    return value


def authorize_existing(destination, current, payload, expected_current=None):
    if current is None:
        return
    actual = code_inventory(current)
    wanted = {name: digest(raw) for name, raw in payload.items()}
    if expected_current is not None:
        record = expected_record(expected_current)
    elif (destination / RECORD).is_file():
        record = expected_record(json.loads((destination / RECORD).read_text(encoding='utf-8')))
    elif actual == wanted:
        return  # Idempotent reconciliation does not discard any existing code.
    else:
        raise ValueError('Existing bridge has no trusted installation manifest. Preserve local repairs; supply an explicitly reviewed --expected-current-manifest before upgrading.')
    if record['files'] != actual:
        raise ValueError('Installed bridge contains local changes; upgrade refused to preserve them')
    old = record['version']
    if 'local' in old.casefold() and 'local' not in VERSION.casefold() and expected_current is None:
        raise ValueError('A local repaired build cannot be replaced by a public release without an explicitly reviewed manifest')
    old_number = tuple(map(int, re.match(r'^(\d+)\.(\d+)\.(\d+)', old).groups())) if re.match(r'^(\d+)\.(\d+)\.(\d+)', old) else None
    new_number = tuple(map(int, re.match(r'^(\d+)\.(\d+)\.(\d+)', VERSION).groups()))
    if old_number is not None and old_number > new_number and expected_current is None:
        raise ValueError('Refusing an implicit downgrade; supply an explicitly reviewed manifest')


def atomic_bytes(path, raw, *, before):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bridge-setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        current = digest(path.read_bytes()) if path.exists() else None
        if current != before:
            raise ValueError('Setup metadata changed while staging; newer work preserved')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    path = Path(path)
    before = digest(path.read_bytes()) if path.exists() else None
    atomic_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8'), before=before)


def remove_staged(root, expected, parent):
    safe_path(root, parent)
    if inventory(root) != expected:
        raise ValueError('Staged component changed; retained for inspection')
    for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        safe_path(path, root)
        if path.is_dir():
            path.rmdir()
        else:
            name = path.relative_to(root).as_posix()
            if digest(path.read_bytes()) != expected[name]['sha256']:
                raise ValueError('Staged file changed before removal; preserved')
            path.unlink()
    root.rmdir()


def install(mo2, source, *, expected_current=None, check_closed=None, payload=None):
    """Install only the plugin component. Never register a client as a side effect."""
    mo2, source = validate_mo2(mo2), Path(source).resolve()
    check_closed = check_closed or ensure_mo2_closed
    destination = safe_path(mo2 / 'plugins/mo2_codex_bridge', mo2)
    check_closed()
    if payload is None:
        payload = source_payload(source)
    before = inventory(destination)
    authorize_existing(destination, before, payload, expected_current)
    backup_root = safe_path(mo2 / 'plugins/data/mo2_codex_bridge/setup-backups', mo2)
    backup = backup_root / uuid.uuid4().hex
    stage = safe_path(backup_root.parent / 'setup-staging' / uuid.uuid4().hex, mo2)
    journal = {'version': VERSION, 'destination': str(destination), 'installed': False,
               'status': 'staging', 'backup': str(backup), 'original_component': before,
               'registration': 'not attempted by component installer', 'staging_path': str(stage),
               'retired_component': str(backup / 'retired-component')}
    stage.mkdir(parents=True)
    staged = None
    old_moved = False
    new_moved = False
    retired = backup / 'retired-component'
    try:
        for name, raw in payload.items():
            with (stage / name).open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        atomic_json(stage / RECORD, {'version': VERSION, 'files': {n: digest(raw) for n, raw in payload.items()}})
        staged = inventory(stage)
        if code_inventory(staged) != {n: digest(raw) for n, raw in payload.items()}:
            raise ValueError('Staged component bytes do not match the release')
        check_closed()
        if inventory(destination) != before:
            raise ValueError('Installed component changed during staging; newer work preserved')
        backup.mkdir(parents=True)
        if before is not None:
            shutil.copytree(destination, backup / 'component')
            if inventory(backup / 'component') != before:
                raise ValueError('Component recovery copy failed verification')
        journal['staged_component'] = staged
        journal['status'] = 'prepared; original recovery copy verified'
        atomic_json(backup / 'setup-journal.json', journal)
        check_closed()
        if inventory(destination) != before:
            raise ValueError('Installed component changed before commit; newer work preserved')
        if before is not None:
            os.replace(destination, retired)
            old_moved = True
        check_closed()
        if destination.exists():
            raise ValueError('Component destination appeared before commit; preserve it')
        os.replace(stage, destination)
        new_moved = True
        if inventory(destination) != staged:
            raise ValueError('Installed component failed readback verification')
        check_closed()
        journal.update(installed=True, status='installed and verified')
        atomic_json(backup / 'setup-journal.json', journal)
        # The retired original is itself retained beside the verified portable
        # recovery copy. Neither is under MO2's discoverable plugin directory.
        return dict(journal, installed_plugin=str(destination))
    except BaseException as error:
        recovery = 'no component changes'
        try:
            check_closed()
            # Reconcile observed directory state: an OS rename can have taken
            # effect even when its caller receives an exception/interruption.
            current = inventory(destination)
            retired_current = inventory(retired)
            if current == staged and staged is not None:
                os.replace(destination, backup / 'failed-component')
                current = None
                new_moved = True
            elif current != before and current is not None:
                raise ValueError('Newer installed-component changes preserved; automatic recovery stopped')
            if retired_current is not None:
                if retired_current != before:
                    raise ValueError('Recovery copy changed; automatic recovery stopped')
                if current is None:
                    os.replace(retired, destination)
                    old_moved = True
                elif current != before:
                    raise ValueError('Recovery target changed; automatic recovery stopped')
            if inventory(destination) != before:
                raise ValueError('Recovery verification failed; keep MO2 closed')
            recovery = 'original component restored and verified' if old_moved or new_moved else recovery
        except Exception as recovery_error:
            recovery = 'manual recovery required; keep MO2 closed: ' + str(recovery_error)
        message = str(error) or type(error).__name__
        journal.update(installed=False, status='installation failed', error=message, recovery=recovery)
        if backup.exists():
            try:
                atomic_json(backup / 'setup-journal.json', journal)
            except Exception:
                pass
        raise SetupInstallError(message, journal) from error
    finally:
        if stage.exists() and staged is not None:
            try:
                remove_staged(stage, staged, stage.parent)
            except Exception:
                pass  # Preserve an unfamiliar staged tree rather than delete it.


def connector_payload(mo2, source, component):
    files = {'mo2_codex_bridge/' + name: raw for name, raw in component.items()}
    for filename, member in (('mcp_server.py', '__main__.py'), ('client.py', 'client.py'), ('LICENSE', 'LICENSE')):
        path = safe_path(source / filename, source)
        if not path.is_file() or path.stat().st_size > MAX_CONNECTOR_BYTES:
            raise ValueError('Connector release is missing a valid ' + filename)
        files[member] = path.read_bytes()
        if member.endswith('.py'):
            ast.parse(files[member], filename=filename)
    record = {'format': 1, 'version': VERSION, 'mo2': str(mo2),
              'files': {name: digest(raw) for name, raw in files.items()}}
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as out:
        for name, raw in sorted(files.items()):
            out.writestr(zipfile.ZipInfo(name), raw, compress_type=zipfile.ZIP_DEFLATED)
        out.writestr(zipfile.ZipInfo(CONNECTOR_RECORD), json.dumps(record).encode('utf-8'))
    return archive.getvalue()


def connector_version(blob, mo2):
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if (len(entries) != len(names) or len(entries) > 256
                    or sum(entry.file_size for entry in entries) > MAX_CONNECTOR_BYTES):
                raise ValueError('Unexpected connector archive entries or size')
            record = json.loads(archive.read(CONNECTOR_RECORD))
            if (record['format'] != 1 or not isinstance(record['version'], str)
                    or not isinstance(record['mo2'], str)):
                raise ValueError('Unknown connector format')
            files = {name: digest(archive.read(name)) for name in names - {CONNECTOR_RECORD}}
            if files != record['files'] or not {'__main__.py', 'client.py', 'LICENSE',
                                                 'mo2_codex_bridge/common.py'} <= files.keys():
                raise ValueError('Connector package failed verification')
    except (OSError, RuntimeError, TypeError, KeyError, ValueError, zipfile.BadZipFile) as error:
        raise ValueError('Permanent connector contains unfamiliar or modified files; it was preserved') from error
    if Path(record['mo2']) != mo2:
        raise ValueError('The permanent connector belongs to a different MO2 installation')
    return record['version']


def read_connector(path, root):
    safe_path(path, root)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_CONNECTOR_BYTES:
        raise ValueError('Permanent connector file is invalid; it was preserved')
    return path.read_bytes()


@contextmanager
def connector_install_lock(root):
    import msvcrt

    lock = safe_path(root / 'connector-setup.lock', root)
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open('a+b') as stream:
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError('Another connector setup is running or the installation lock is unavailable') from error
        yield  # Closing the handle releases the lock, including on interruption.


def read_settings(path, root):
    safe_path(path, root)
    if not path.exists():
        return {}, None
    if not path.is_file() or path.stat().st_size > MAX_CONNECTOR_BYTES:
        raise ValueError('Unexpected configuration file: ' + path.name)
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Existing configuration is not an object: ' + path.name)
    return data, digest(raw)


def targets_connector(args, server, mo2):
    return (isinstance(args, list) and len(args) >= 3 and all(isinstance(value, str) for value in args)
            and Path(args[-3]) == server and args[-2] == '--mo2' and Path(args[-1]) == mo2)


def connector_configuration(mo2, source, config_path):
    config, config_hash = read_settings(config_path, config_path.parent)
    previous_mo2 = config.get('mo2_path')
    if previous_mo2 is not None and (not isinstance(previous_mo2, str) or Path(previous_mo2) != mo2):
        raise ValueError('Existing connector settings belong to a different MO2 installation')
    config['mo2_path'] = str(mo2)

    mcp_path = source / '.mcp.json'
    mcp, mcp_hash = read_settings(mcp_path, source)
    servers = mcp.setdefault('mcpServers', {})
    if not isinstance(servers, dict):
        raise ValueError('Existing MCP server configuration is not an object')
    server = source / CONNECTOR
    if 'mo2_bridge' in servers:
        entry = servers['mo2_bridge']
        if not isinstance(entry, dict) or not targets_connector(entry.get('args'), server, mo2):
            raise ValueError('Existing connector configuration uses another path; preserve it and migrate separately')
    else:
        servers['mo2_bridge'] = {'command': sys.executable, 'args': [str(server), '--mo2', str(mo2)]}

    plugin_path = source / '.codex-plugin/plugin.json'
    plugin, plugin_hash = read_settings(plugin_path, source)
    plugin.update(name='mo2-codex-bridge', version=VERSION,
                  description='Manage a running local Mod Organizer 2 through its plugin interface.',
                  author={'name': 'Project contributors'}, mcpServers='./.mcp.json',
                  interface={'displayName': 'MO2 Codex Bridge', 'shortDescription': 'Manage the running MO2',
                             'category': 'Productivity', 'capabilities': ['Read', 'Write', 'Interactive']})
    return [(config_path, config, config_hash), (mcp_path, mcp, mcp_hash), (plugin_path, plugin, plugin_hash)]


def install_with_connector(mo2, source, config_path, *, expected_current=None, register_codex=False):
    mo2, source, config_path = validate_mo2(mo2), Path(source).resolve(), Path(config_path)
    root = safe_path(config_path.parent, config_path.parent.parent)
    server = root / 'connector' / CONNECTOR
    component = source_payload(source)
    wanted = connector_payload(mo2, source, component)
    if connector_version(wanted, mo2) != VERSION:
        raise ValueError('Connector and MO2 component versions differ')
    ensure_mo2_closed()
    with connector_install_lock(root):
        before = read_connector(server, root)
        if before is not None:
            connector_version(before, mo2)
        settings = connector_configuration(mo2, server.parent, config_path)
        destination = safe_path(mo2 / 'plugins/mo2_codex_bridge', mo2)
        original_component = inventory(destination)
        authorize_existing(destination, original_component, component, expected_current)
        backup = None
        if before is not None and before != wanted:
            backup = safe_path(root / 'connector-backups' / uuid.uuid4().hex / CONNECTOR, root)
            atomic_bytes(backup, before, before=None)
            if read_connector(backup, root) != before:
                raise ValueError('Connector recovery copy failed verification')
        try:
            ensure_mo2_closed()
            if before != wanted:
                atomic_bytes(server, wanted, before=None if before is None else digest(before))
            if read_connector(server, root) != wanted:
                raise ValueError('Permanent connector failed readback verification')
            report = install(mo2, source, expected_current=expected_current, payload=component)
        except BaseException as error:
            recovery = 'connector unchanged'
            component_recovery = getattr(error, 'details', {}).get('recovery', '')
            try:
                original_component_verified = inventory(destination) == original_component
            except (OSError, ValueError):
                original_component_verified = False
            component_uncertain = (component_recovery.startswith('manual recovery required')
                                   or not original_component_verified)
            if component_uncertain:
                recovery = 'Connector and any rollback copy preserved while MO2 component state is uncertain'
            elif before != wanted:
                try:
                    observed = read_connector(server, root)
                    if observed == wanted:
                        if before is None:
                            server.unlink()
                        else:
                            atomic_bytes(server, before, before=digest(wanted))
                    if read_connector(server, root) != before:
                        raise ValueError('Connector changed after staging')
                    recovery = 'previous connector state verified'
                except Exception as recovery_error:
                    details = dict(getattr(error, 'details', {}), connector=str(server),
                                   connector_backup=str(backup) if backup else None,
                                   connector_recovery='Manual recovery required: ' + str(recovery_error))
                    raise SetupInstallError(str(error), details) from error
            if isinstance(error, SetupInstallError):
                error.details.update(connector=str(server), connector_backup=str(backup) if backup else None,
                                     connector_recovery=recovery)
            elif component_uncertain:
                message = str(error) or type(error).__name__
                raise SetupInstallError(message, {'status': 'component state uncertain', 'connector': str(server),
                                                  'connector_backup': str(backup) if backup else None,
                                                  'connector_recovery': recovery}) from error
            raise
        report.update(connector=str(server), connector_backup=str(backup) if backup else None)
        try:
            connector_verified = read_connector(server, root) == wanted
        except (OSError, ValueError):
            connector_verified = False
        if not connector_verified:
            report.update(configuration='not attempted', registration='not attempted',
                          error='Permanent connector changed during setup; inspect it before reopening MO2')
        else:
            report.update(configure(mo2, server.parent, config_path, register_codex=register_codex, settings=settings))
        return report


def codex_registration(codex, server, mo2):
    command = [codex, 'mcp', 'get', 'mo2_bridge', '--json']
    options = {'capture_output': True, 'text': True, 'encoding': 'utf-8', 'timeout': 30, 'cwd': server.parent,
               'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}

    def lookup():
        result = subprocess.run(command, **options)
        if result.returncode != 0:
            error = re.sub(r'\x1b\[[0-9;]*m', '', result.stderr or '')
            if result.returncode == 1 and error.strip() in {"No MCP server named 'mo2_bridge' found.",
                                                           "Error: No MCP server named 'mo2_bridge' found."}:
                return None
            raise ValueError('Codex registration could not be checked; existing settings were preserved')
        entry = json.loads(result.stdout)
        if not isinstance(entry, dict) or entry.get('name') != 'mo2_bridge':
            raise ValueError('Unexpected response while checking Codex registration')
        transport = entry.get('transport')
        if not isinstance(transport, dict):
            raise ValueError('Unexpected response while checking Codex registration')
        if transport.get('type') != 'stdio' or not targets_connector(transport.get('args'), server, mo2):
            raise ValueError('Existing Codex registration uses another path; preserve it and migrate separately')
        return 'preserved; disabled' if entry.get('enabled') is False else 'preserved'

    existing = lookup()
    if existing is not None:
        return existing
    add = [codex, 'mcp', 'add', 'mo2_bridge', '--', sys.executable, str(server), '--mo2', str(mo2)]
    try:
        subprocess.run(add, **options)
    except (OSError, subprocess.TimeoutExpired):
        pass  # Read back the registration before treating an ambiguous command as failed.
    if lookup() != 'preserved':
        raise ValueError('Codex registration could not be verified; inspect it before retrying')
    return 'registered'


def configure(mo2, source, config_path, *, register_codex=False, settings=None):
    source, config_path = Path(source).resolve(), Path(config_path)
    server = source / CONNECTOR
    result = {'configuration': 'in progress', 'registration': 'not requested', 'files_written': []}
    try:
        if not server.is_file():
            raise ValueError('Client server file is missing; component remains installed')
        if settings is None:
            settings = connector_configuration(mo2, source, config_path)
        for path, value, before in settings:
            root = config_path.parent if path == config_path else source
            safe_path(path, root)
            raw = (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
            atomic_bytes(path, raw, before=before)
            result['files_written'].append(str(path))
        result['configuration'] = 'written'
        if register_codex:
            result['registration'] = 'in progress'
            codex = shutil.which('codex')
            if codex is None:
                raise ValueError('Codex CLI is not on PATH; use the generated .mcp.json to register')
            result['registration'] = codex_registration(codex, server, mo2)
    except Exception as error:
        if result['configuration'] != 'written':
            result['configuration'] = 'failed'
        if register_codex:
            result['registration'] = 'failed' if result['registration'] == 'in progress' else 'not attempted'
        result['error'] = str(error)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mo2', required=True)
    parser.add_argument('--register-codex', action='store_true')
    parser.add_argument('--component-only', action='store_true', help='Install only the MO2 component; leave all client settings and registration untouched')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--expected-current-manifest', type=Path,
                        help='Explicitly reviewed version/files hashes for a legacy or locally repaired component')
    args = parser.parse_args(argv)
    if args.component_only and args.register_codex:
        parser.error('--component-only cannot be combined with --register-codex')
    mo2 = validate_mo2(args.mo2)
    source = Path(__file__).resolve().parent
    config_path = Path.home() / '.mo2-codex-bridge/config.json'
    if args.dry_run:
        print(json.dumps({'version': VERSION, 'mo2': str(mo2), 'configuration': str(config_path),
                          'server': str(config_path.parent / 'connector' / CONNECTOR), 'changes_made': False}))
        return 0
    expected = json.loads(args.expected_current_manifest.read_text(encoding='utf-8')) if args.expected_current_manifest else None
    try:
        if args.component_only:
            report = install(mo2, source, expected_current=expected)
        else:
            report = install_with_connector(mo2, source, config_path, expected_current=expected,
                                            register_codex=args.register_codex)
    except SetupInstallError as error:
        print(json.dumps(error.details, ensure_ascii=False))
        return 1
    if args.component_only:
        report.update(configuration='not attempted; component-only', registration='not attempted; component-only', installed_version=VERSION)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    report['installed_version'] = VERSION
    report['next_step'] = ('Open MO2 and start a fresh bridge client' if not report.get('error') else
                           'Component is installed; resolve client configuration/registration separately')
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report.get('error') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'installed': False, 'version': VERSION, 'error': str(error)}), file=sys.stderr)
        raise SystemExit(1)
