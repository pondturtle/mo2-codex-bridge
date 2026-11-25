"""B15 setup fault tests; every installation is an isolated fake directory."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('bridge_setup_under_test', ROOT / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class SetupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mo2 = self.root / 'MO2'
        (self.mo2 / 'plugins/plugin_python').mkdir(parents=True)
        (self.mo2 / 'ModOrganizer.exe').write_bytes(b'fake executable; never run')
        self.source = self.root / 'release'
        self.package = self.source / 'mo2_codex_bridge'
        self.package.mkdir(parents=True)
        for name in setup.REQUIRED:
            (self.package / name).write_text("VERSION = " + repr(setup.VERSION) + '\n' if name == 'common.py' else '# new release ' + name + '\n', encoding='utf-8')
        (self.source / 'mcp_server.py').write_text('# fake server; never run\n')
        self.destination = self.mo2 / 'plugins/mo2_codex_bridge'
        self.closed_calls = 0
    def closed(self):
        self.closed_calls += 1
    def install(self, **kwargs):
        return setup.install(self.mo2, self.source, check_closed=self.closed, **kwargs)
    def legacy(self):
        self.destination.mkdir()
        for name in setup.REQUIRED | {'retired_module.py'}:
            (self.destination / name).write_text('# installed older ' + name + '\n')
        return {'version': '0.1.2-local.1-candidate.2', 'files': setup.code_inventory(setup.inventory(self.destination))}
    def assert_original(self, expected):
        self.assertEqual(setup.code_inventory(setup.inventory(self.destination)), expected['files'])
    def test_clean_install_whole_component_and_record(self):
        report = self.install()
        self.assertTrue(report['installed'])
        record = json.loads((self.destination / setup.RECORD).read_text())
        self.assertEqual(record['version'], setup.VERSION)
        self.assertEqual(record['files'], setup.code_inventory(setup.inventory(self.destination)))
        self.assertGreaterEqual(self.closed_calls, 4)
    def test_upgrade_removes_retired_module_and_preserves_verified_backup(self):
        expected = self.legacy()
        report = self.install(expected_current=expected)
        self.assertFalse((self.destination / 'retired_module.py').exists())
        saved = Path(report['backup']) / 'component'
        self.assertEqual(setup.code_inventory(setup.inventory(saved)), expected['files'])
        self.assertEqual(setup.inventory(saved), setup.inventory(Path(report['backup']) / 'retired-component'))
    def test_unmanaged_local_repair_refused_without_writes(self):
        expected = self.legacy()
        with self.assertRaisesRegex(ValueError, 'no trusted installation manifest'):
            self.install()
        self.assert_original(expected)
        self.assertFalse((self.mo2 / 'plugins/data').exists())
    def test_expected_manifest_does_not_override_later_local_change(self):
        expected = self.legacy()
        (self.destination / 'plugin.py').write_text('# newer repair')
        with self.assertRaisesRegex(ValueError, 'local changes'):
            self.install(expected_current=expected)
        self.assertEqual((self.destination / 'plugin.py').read_text(), '# newer repair')
    def test_managed_local_change_is_preserved(self):
        self.install()
        (self.destination / 'plugin.py').write_text('# local repair')
        with self.assertRaisesRegex(ValueError, 'local changes'):
            self.install()
        self.assertEqual((self.destination / 'plugin.py').read_text(), '# local repair')
    def test_explicit_manifest_supports_existing_manual_local_build(self):
        expected = self.legacy()
        self.assertTrue(self.install(expected_current=expected)['installed'])
    def test_second_install_is_coherent_and_no_retired_stale_modules(self):
        self.install()
        result = self.install()
        self.assertTrue(result['installed'])
        self.assertEqual(setup.code_inventory(setup.inventory(self.destination)),
                         {n: setup.digest(raw) for n, raw in setup.source_payload(self.source).items()})
    def test_staging_failure_leaves_original_and_no_discoverable_partial_component(self):
        expected = self.legacy()
        original_json = setup.atomic_json
        def fail(path, value):
            if Path(path).name == setup.RECORD:
                raise OSError('injected staging failure')
            return original_json(path, value)
        with patch.object(setup, 'atomic_json', fail), self.assertRaises(setup.SetupInstallError):
            self.install(expected_current=expected)
        self.assert_original(expected)
        self.assertEqual(sorted(p.name for p in (self.mo2 / 'plugins').iterdir() if p.is_dir()),
                         ['data', 'mo2_codex_bridge', 'plugin_python'])
    def test_backup_copy_failure_leaves_original(self):
        expected = self.legacy()
        with patch.object(setup.shutil, 'copytree', side_effect=OSError('injected backup failure')):
            with self.assertRaises(setup.SetupInstallError) as error:
                self.install(expected_current=expected)
        self.assert_original(expected)
        self.assertIn('no component changes', error.exception.details['recovery'])
    def test_corrupt_backup_is_detected_before_swap(self):
        expected = self.legacy()
        real = setup.shutil.copytree
        def corrupt(source, destination):
            answer = real(source, destination)
            (Path(destination) / 'plugin.py').write_text('# damaged recovery')
            return answer
        with patch.object(setup.shutil, 'copytree', corrupt), self.assertRaises(setup.SetupInstallError):
            self.install(expected_current=expected)
        self.assert_original(expected)
    def test_commit_rename_failure_restores_whole_original(self):
        expected = self.legacy()
        real = setup.os.replace
        def fail(source, destination):
            if Path(source).parent.name == 'setup-staging' and Path(destination) == self.destination:
                raise OSError('injected second directory rename failure')
            return real(source, destination)
        with patch.object(setup.os, 'replace', fail), self.assertRaises(setup.SetupInstallError) as error:
            self.install(expected_current=expected)
        self.assert_original(expected)
        self.assertEqual(error.exception.details['recovery'], 'original component restored and verified')
    def test_failure_after_installed_rename_restores_original(self):
        expected = self.legacy()
        real = setup.os.replace
        fired = []
        def fail(source, destination):
            result = real(source, destination)
            if Path(source).parent.name == 'setup-staging' and Path(destination) == self.destination:
                fired.append(True)
                raise OSError('injected ambiguous rename outcome')
            return result
        with patch.object(setup.os, 'replace', fail), self.assertRaises(setup.SetupInstallError) as error:
            self.install(expected_current=expected)
        # This case must reconcile the filesystem, not trust a boolean assigned
        # only after os.replace returns.
        self.assertTrue(fired)
        self.assert_original(expected)
        self.assertIn('restored', error.exception.details['recovery'])
    def test_ambiguous_original_retire_rename_restores_original(self):
        expected = self.legacy()
        real = setup.os.replace
        def fail(source, destination):
            result = real(source, destination)
            if Path(source) == self.destination and Path(destination).name == 'retired-component':
                raise OSError('injected after retiring original')
            return result
        with patch.object(setup.os, 'replace', fail), self.assertRaises(setup.SetupInstallError) as error:
            self.install(expected_current=expected)
        self.assert_original(expected)
        self.assertIn('restored', error.exception.details['recovery'])
    def test_ambiguous_first_install_rename_restores_absence(self):
        real = setup.os.replace
        def fail(source, destination):
            result = real(source, destination)
            if Path(source).parent.name == 'setup-staging' and Path(destination) == self.destination:
                raise OSError('injected after first install rename')
            return result
        with patch.object(setup.os, 'replace', fail), self.assertRaises(setup.SetupInstallError):
            self.install()
        self.assertFalse(self.destination.exists())
    def test_local_build_cannot_be_silently_replaced_by_public_release(self):
        expected = self.legacy()
        (self.destination / setup.RECORD).write_text(json.dumps(expected))
        payload = setup.source_payload(self.source)
        with patch.object(setup, 'VERSION', '0.2.0'), self.assertRaisesRegex(ValueError, 'local repaired build'):
            setup.authorize_existing(self.destination, setup.inventory(self.destination), payload)
        self.assert_original(expected)
    def test_implicit_downgrade_is_refused(self):
        expected = self.legacy()
        expected['version'] = '9.0.0'
        (self.destination / setup.RECORD).write_text(json.dumps(expected))
        with self.assertRaisesRegex(ValueError, 'implicit downgrade'):
            self.install()
        self.assert_original(expected)
    def test_linked_destination_is_refused(self):
        expected = self.legacy()
        actual = setup.linked
        def linked(path): return Path(path) == self.destination or actual(path)
        with patch.object(setup, 'linked', linked), self.assertRaisesRegex(ValueError, 'Linked'):
            self.install(expected_current=expected)
        self.assert_original(expected)
    def test_registration_nonzero_exit_is_reported_without_echoing_output(self):
        process = types.SimpleNamespace(returncode=5, stdout='never expose arbitrary client output', stderr='private fixture text')
        with patch.object(setup.shutil, 'which', return_value='fake-codex-never-run'), patch.object(setup.subprocess, 'run', return_value=process):
            result = setup.configure(self.mo2, self.source, self.root / 'settings/config.json', register_codex=True)
        self.assertEqual(result['registration'], 'failed')
        self.assertIn('exit 5', result['error'])
        self.assertNotIn('private', json.dumps(result))
    def test_missing_client_server_does_not_generate_broken_registration(self):
        (self.source / 'mcp_server.py').unlink()
        result = setup.configure(self.mo2, self.source, self.root / 'settings/config.json')
        self.assertEqual(result['configuration'], 'failed')
        self.assertEqual(result['files_written'], [])

    def test_final_journal_failure_restores_original(self):
        expected = self.legacy()
        real = setup.atomic_json
        def fail(path, value):
            if value.get('status') == 'installed and verified':
                raise OSError('injected final journal failure')
            return real(path, value)
        with patch.object(setup, 'atomic_json', fail), self.assertRaises(setup.SetupInstallError):
            self.install(expected_current=expected)
        self.assert_original(expected)
    def test_external_change_during_staging_is_preserved(self):
        expected = self.legacy()
        def closed():
            self.closed_calls += 1
            if self.closed_calls == 2:
                (self.destination / 'plugin.py').write_text('# external newer work')
        with self.assertRaises(setup.SetupInstallError):
            setup.install(self.mo2, self.source, expected_current=expected, check_closed=closed)
        self.assertEqual((self.destination / 'plugin.py').read_text(), '# external newer work')
    def test_mo2_opens_after_old_directory_moved_stops_recovery_and_retains_copy(self):
        expected = self.legacy()
        def closed():
            self.closed_calls += 1
            if self.closed_calls >= 4:
                raise ValueError('MO2 opened')
        with self.assertRaises(setup.SetupInstallError) as error:
            setup.install(self.mo2, self.source, expected_current=expected, check_closed=closed)
        self.assertIn('manual recovery required; keep MO2 closed', error.exception.details['recovery'])
        backup = Path(error.exception.details['backup'])
        self.assertEqual(setup.code_inventory(setup.inventory(backup / 'retired-component')), expected['files'])
    def test_newer_installed_component_during_failure_is_preserved(self):
        expected = self.legacy()
        def closed():
            self.closed_calls += 1
            if self.closed_calls == 5:
                (self.destination / 'plugin.py').write_text('# newer installed edit')
                raise ValueError('injected after-commit fault')
        with self.assertRaises(setup.SetupInstallError) as error:
            setup.install(self.mo2, self.source, expected_current=expected, check_closed=closed)
        self.assertEqual((self.destination / 'plugin.py').read_text(), '# newer installed edit')
        self.assertIn('manual recovery required', error.exception.details['recovery'])
    def test_unknown_component_data_refused_not_deleted(self):
        expected = self.legacy()
        (self.destination / 'session.json').write_text('{"token":"fixture only"}')
        with self.assertRaisesRegex(ValueError, 'Unexpected file'):
            self.install(expected_current=expected)
        self.assertTrue((self.destination / 'session.json').exists())
    def test_missing_module_or_syntax_error_blocks_install(self):
        (self.package / 'transport.py').unlink()
        with self.assertRaisesRegex(ValueError, 'missing required'):
            self.install()
        self.assertFalse(self.destination.exists())
        (self.package / 'transport.py').write_text('not valid Python @@@')
        with self.assertRaises(SyntaxError):
            self.install()
        self.assertFalse(self.destination.exists())
    def test_missing_new_critical_helper_refused_before_any_component_change(self):
        expected = self.legacy()
        (self.package / 'managed_operations.py').unlink()
        with self.assertRaisesRegex(ValueError, 'missing required'):
            self.install(expected_current=expected)
        self.assert_original(expected)
        self.assertFalse((self.mo2 / 'plugins/data').exists())
    def test_missing_future_relative_import_is_detected_before_install(self):
        (self.package / 'plugin.py').write_text('from .new_critical_helper import SafeThing\n')
        with self.assertRaisesRegex(ValueError, 'required imported module'):
            self.install()
        self.assertFalse(self.destination.exists())

    def test_process_query_failure_and_malformed_output_fail_closed(self):
        for result in [types.SimpleNamespace(returncode=1, stdout=''), types.SimpleNamespace(returncode=0, stdout=''),
                       types.SimpleNamespace(returncode=0, stdout='unexpected output')]:
            with self.subTest(result=result), patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
                with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                    setup.ensure_mo2_closed()
    def test_process_query_recognizes_mo2_case_insensitively(self):
        result = types.SimpleNamespace(returncode=0, stdout='"MODORGANIZER.EXE","1234","Console","1","50 K"\n')
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Close MO2'):
                setup.ensure_mo2_closed()
    def test_process_query_success(self):
        result = types.SimpleNamespace(returncode=0, stdout='"python.exe","1234","Console","1","50 K"\n')
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            setup.ensure_mo2_closed()
    def test_process_query_uses_strict_oem_decoding_for_localized_output(self):
        # Byte 0x81 represents u-umlaut in OEM CP852 but is undefined in CP1250.
        # Model a Czech Windows tasklist pipe without querying actual processes.
        raw = ('"M\u00fcller.exe","1234","Konzole","1","50\u00a0K"\r\n'
               .encode('cp852'))
        with self.assertRaises(UnicodeDecodeError):
            raw.decode('cp1250')
        def run(command, **kwargs):
            self.assertEqual(command, ['tasklist', '/FO', 'CSV', '/NH'])
            self.assertEqual(kwargs, {'capture_output': True, 'text': True,
                                     'encoding': 'oem', 'errors': 'strict',
                                     'timeout': 10, 'creationflags': subprocess.CREATE_NO_WINDOW})
            return types.SimpleNamespace(returncode=0, stdout=raw.decode('cp852', kwargs['errors']))
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', side_effect=run):
            setup.ensure_mo2_closed()

    def test_process_query_localized_rows_do_not_hide_running_mo2(self):
        output = ('"M\u00fcller.exe","42","Konzole","1","50\u00a0K"\r\n'
                  '"mOdOrGaNiZeR.ExE","1234","Konzole","1","1\u00a0234 K"\r\n')
        result = types.SimpleNamespace(returncode=0, stdout=output)
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Close MO2'):
                setup.ensure_mo2_closed()

    def test_process_query_requires_exact_image_name_for_mo2_match(self):
        output = ('"ModOrganizer.exe.helper.exe","42","Console","1","50 K"\n'
                  '"Some ModOrganizer.exe","43","Console","1","50 K"\n'
                  '"System Idle Process","0","Services","0","8 K"\n')
        result = types.SimpleNamespace(returncode=0, stdout=output)
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            setup.ensure_mo2_closed()

    def test_process_query_rejects_malformed_or_missing_process_fields(self):
        outputs = [
            None,
            '\r\n',
            '"python.exe","1234","Console","1"\n',
            '"python.exe","1234","Console","1","50 K","unexpected"\n',
            '"python.exe","1234","Console","1","50 K\n',
            '"","1234","Console","1","50 K"\n',
            '"ModOrganizer.exe ","1234","Console","1","50 K"\n',
            '"ModOrganizer.exe\x00","1234","Console","1","50 K"\n',
            '"C:\\ModOrganizer.exe","1234","Console","1","50 K"\n',
            '"python.exe","","Console","1","50 K"\n',
            '"python.exe","\uff11\uff12","Console","1","50 K"\n',
            '"python.exe","1234","","1","50 K"\n',
            '"python.exe","1234","Console","","50 K"\n',
            '"python.exe","1234","Console","x","50 K"\n',
            '"python.exe","1234","Console","1",""\n',
        ]
        for output in outputs:
            with self.subTest(output=repr(output)):
                result = types.SimpleNamespace(returncode=0, stdout=output)
                with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
                    with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                        setup.ensure_mo2_closed()

    def test_process_query_refuses_partial_valid_rows_followed_by_error(self):
        output = '"python.exe","1234","Console","1","50 K"\nERROR: partial query\n'
        result = types.SimpleNamespace(returncode=0, stdout=output)
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                setup.ensure_mo2_closed()

    def test_process_query_refuses_undecoded_bytes(self):
        result = types.SimpleNamespace(returncode=0, stdout=b'"python.exe","1234","Console","1","50 K"\n')
        with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                setup.ensure_mo2_closed()

    def test_process_query_execution_and_decoding_errors_fail_closed(self):
        errors = [OSError('fixture query failure'), subprocess.TimeoutExpired('tasklist', 10),
                  UnicodeDecodeError('fixture-oem', b'\x81', 0, 1, 'fixture invalid byte')]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                with patch.object(setup.os, 'name', 'nt'), patch.object(setup.subprocess, 'run', side_effect=error):
                    with self.assertRaisesRegex(ValueError, 'Cannot verify'):
                        setup.ensure_mo2_closed()

    def test_configuration_preserves_other_servers_and_uses_central_version(self):
        config = self.root / 'settings/config.json'
        (self.source / '.mcp.json').write_text(json.dumps({'mcpServers': {'another': {'command': 'keep-me'}}}))
        report = setup.configure(self.mo2, self.source, config)
        self.assertEqual(report['configuration'], 'written')
        data = json.loads((self.source / '.mcp.json').read_text())
        self.assertEqual(data['mcpServers']['another']['command'], 'keep-me')
        self.assertEqual(json.loads((self.source / '.codex-plugin/plugin.json').read_text())['version'], setup.VERSION)
    def test_registration_failure_reports_installed_component_separately(self):
        installed = self.install()
        with patch.object(setup.shutil, 'which', return_value=None):
            result = setup.configure(self.mo2, self.source, self.root / 'settings/config.json', register_codex=True)
        self.assertTrue(installed['installed'])
        self.assertEqual(result['configuration'], 'written')
        self.assertEqual(result['registration'], 'failed')
        self.assertEqual(setup.inventory(self.destination), installed['staged_component'])
    def test_configuration_write_failure_does_not_rollback_component(self):
        installed = self.install()
        with patch.object(setup, 'atomic_json', side_effect=PermissionError('fixture lock')):
            result = setup.configure(self.mo2, self.source, self.root / 'settings/config.json', register_codex=True)
        self.assertEqual(result['configuration'], 'failed')
        self.assertEqual(result['registration'], 'not attempted')
        self.assertEqual(setup.inventory(self.destination), installed['staged_component'])
    def test_component_only_never_calls_configuration(self):
        with patch.object(setup, 'install', return_value={'installed': True}), patch.object(setup, 'configure') as configure:
            self.assertEqual(setup.main(['--mo2', str(self.mo2), '--component-only']), 0)
        configure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
