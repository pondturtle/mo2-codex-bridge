"""Install the bridge component with verified staging and guarded recovery."""
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
from pathlib import Path

from mo2_codex_bridge.common import VERSION

RECORD = '.bridge-install.json'
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


def atomic_json(path, value):
    path = Path(path)
    before = digest(path.read_bytes()) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.bridge-setup-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write((json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        current = digest(path.read_bytes()) if path.exists() else None
        if current != before:
            raise ValueError('Setup metadata changed while staging; newer work preserved')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


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


def install(mo2, source, *, expected_current=None, check_closed=None):
    """Install only the plugin component. Never register a client as a side effect."""
    mo2, source = validate_mo2(mo2), Path(source).resolve()
    check_closed = check_closed or ensure_mo2_closed
    destination = safe_path(mo2 / 'plugins/mo2_codex_bridge', mo2)
    check_closed()
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
    except Exception as error:
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
        journal.update(installed=False, status='installation failed', error=str(error), recovery=recovery)
        if backup.exists():
            try:
                atomic_json(backup / 'setup-journal.json', journal)
            except Exception:
                pass
        raise SetupInstallError(str(error), journal) from error
    finally:
        if stage.exists() and staged is not None:
            try:
                remove_staged(stage, staged, stage.parent)
            except Exception:
                pass  # Preserve an unfamiliar staged tree rather than delete it.


def configure(mo2, source, config_path, *, register_codex=False):
    source, config_path = Path(source).resolve(), Path(config_path)
    server = source / 'mcp_server.py'
    result = {'configuration': 'in progress', 'registration': 'not requested', 'files_written': []}
    writes = [config_path, source / '.mcp.json', source / '.codex-plugin/plugin.json']
    try:
        if not server.is_file():
            raise ValueError('Client server file is missing; component remains installed')
        for path in writes:
            root = config_path.parent if path == config_path else source
            safe_path(path, root)
        config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
        if not isinstance(config, dict):
            raise ValueError('Existing bridge configuration is not an object')
        config['mo2_path'] = str(mo2)
        atomic_json(config_path, config)
        result['files_written'].append(str(config_path))
        path = source / '.mcp.json'
        mcp = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(mcp, dict) or not isinstance(mcp.get('mcpServers', {}), dict):
            raise ValueError('Existing MCP configuration is not an object')
        mcp.setdefault('mcpServers', {})['mo2_bridge'] = {'command': sys.executable, 'args': [str(server), '--mo2', str(mo2)]}
        atomic_json(path, mcp)
        result['files_written'].append(str(path))
        path = source / '.codex-plugin/plugin.json'
        plugin = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if not isinstance(plugin, dict):
            raise ValueError('Existing plugin registration is not an object')
        plugin.update(name='mo2-codex-bridge', version=VERSION,
                      description='Manage a running local Mod Organizer 2 through its plugin interface.',
                      author={'name': 'Project contributors'}, mcpServers='./.mcp.json',
                      interface={'displayName': 'MO2 Codex Bridge', 'shortDescription': 'Manage the running MO2',
                                 'category': 'Productivity', 'capabilities': ['Read', 'Write', 'Interactive']})
        atomic_json(path, plugin)
        result['files_written'].append(str(path))
        result['configuration'] = 'written'
        if register_codex:
            result['registration'] = 'in progress'
            codex = shutil.which('codex')
            if codex is None:
                raise ValueError('Codex CLI is not on PATH; use the generated .mcp.json to register')
            process = subprocess.run([codex, 'mcp', 'add', 'mo2_bridge', '--', sys.executable, str(server), '--mo2', str(mo2)],
                                     capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if process.returncode != 0:
                raise ValueError('Codex registration failed (exit ' + str(process.returncode) + ')')
            result['registration'] = 'registered'
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
                          'server': str(source / 'mcp_server.py'), 'changes_made': False}))
        return 0
    expected = json.loads(args.expected_current_manifest.read_text(encoding='utf-8')) if args.expected_current_manifest else None
    try:
        report = install(mo2, source, expected_current=expected)
    except SetupInstallError as error:
        print(json.dumps(error.details, ensure_ascii=False))
        return 1
    if args.component_only:
        report.update(configuration='not attempted; component-only', registration='not attempted; component-only', installed_version=VERSION)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    configuration = configure(mo2, source, config_path, register_codex=args.register_codex)
    report.update(configuration)
    report['installed_version'] = VERSION
    report['next_step'] = ('Open MO2 and start a fresh bridge client' if not configuration.get('error') else
                           'Component is installed; resolve client configuration/registration separately')
    print(json.dumps(report, ensure_ascii=False))
    return 2 if configuration.get('error') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'installed': False, 'version': VERSION, 'error': str(error)}), file=sys.stderr)
        raise SystemExit(1)
