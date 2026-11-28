"""Exercise the permanent installer path without contacting live MO2 or Codex."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('stable_connector_setup_under_test', ROOT / 'setup.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class StableConnectorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.mo2 = self.make_mo2('Fixture MO2')
        self.home = self.root / 'fixture-home'
        self.home.mkdir()
        self.config = self.home / '.mo2-codex-bridge/config.json'
        self.server = self.config.parent / 'connector' / setup.CONNECTOR
        self.mcp = self.server.parent / '.mcp.json'
        self.source = self.make_release('versioned release 1')
        close_check = patch.object(setup, 'ensure_mo2_closed')
        close_check.start()
        self.addCleanup(close_check.stop)

    def make_mo2(self, name):
        mo2 = self.root / name
        (mo2 / 'plugins/plugin_python').mkdir(parents=True)
        (mo2 / 'ModOrganizer.exe').touch()
        return mo2

    def make_release(self, name, *, version=None):
        folder = self.root / name
        component = folder / 'mo2_codex_bridge'
        component.mkdir(parents=True)
        for module in setup.REQUIRED:
            (component / module).write_text('# fixture\n', encoding='utf-8')
        (component / 'common.py').write_text(f'VERSION = {(version or setup.VERSION)!r}\n', encoding='utf-8')
        for name in ('mcp_server.py', 'client.py', 'LICENSE'):
            shutil.copyfile(ROOT / name, folder / name)
        return folder

    def install(self, source=None, **kwargs):
        return setup.install_with_connector(self.mo2, source or self.source, self.config, **kwargs)

    def entry(self, *, server=None, enabled=True):
        return {
            'name': 'mo2_bridge', 'enabled': enabled,
            'transport': {'type': 'stdio', 'command': 'fixture-python.exe',
                          'args': ['-B', str(server or self.server), '--mo2', str(self.mo2)],
                          'env': {'FIXTURE_SETTING': 'preserve'}},
        }

    def run_result(self, *, entry=None, returncode=0, stderr=''):
        return types.SimpleNamespace(returncode=returncode, stderr=stderr, stdout=json.dumps(entry))

    def missing(self):
        return self.run_result(returncode=1, stderr="Error: No MCP server named 'mo2_bridge' found.")

    def test_dry_run_reports_permanent_path_without_writes(self):
        with patch.object(Path, 'home', return_value=self.home), patch('builtins.print') as output:
            self.assertEqual(setup.main(['--mo2', str(self.mo2), '--dry-run']), 0)
        report = json.loads(output.call_args.args[0])
        self.assertEqual(Path(report['server']), self.server)
        self.assertFalse(report['changes_made'])
        self.assertFalse(self.config.parent.exists())

    def test_install_and_upgrade_from_different_folders_reuse_registration(self):
        publish = [self.missing(), self.run_result(), self.run_result(entry=self.entry())]
        local_metadata = self.source / '.mcp.json'
        local_metadata.write_bytes(b'original source-side registration')
        with (patch.object(setup.shutil, 'which', return_value='fixture-codex.exe'),
              patch.object(setup.subprocess, 'run', side_effect=publish) as run):
            result = self.install(register_codex=True)
        self.assertEqual(result['registration'], 'registered')
        self.assertEqual(run.call_args_list[1].args[0][-3:], [str(self.server), '--mo2', str(self.mo2)])
        self.assertEqual(local_metadata.read_bytes(), b'original source-side registration')
        before = self.server.read_bytes()
        generated = json.loads(self.mcp.read_text(encoding='utf-8'))
        generated['mcpServers']['another'] = {'command': 'preserve-me'}
        generated['mcpServers']['mo2_bridge']['env'] = {'FIXTURE_SETTING': 'preserve'}
        generated['mcpServers']['mo2_bridge']['args'].insert(0, '-B')
        self.mcp.write_text(json.dumps(generated), encoding='utf-8')

        future_version = '0.1.3-local.8-candidate.999'
        release_two = self.make_release('versioned release 2', version=future_version)
        with (patch.object(setup, 'VERSION', future_version),
              patch.object(setup.shutil, 'which', return_value='fixture-codex.exe'),
              patch.object(setup.subprocess, 'run', return_value=self.run_result(entry=self.entry())) as verify):
            upgraded = self.install(release_two, register_codex=True)
        self.assertEqual(upgraded['registration'], 'preserved')
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(Path(upgraded['connector']), self.server)
        self.assertEqual(Path(upgraded['connector_backup']).read_bytes(), before)
        self.assertEqual(setup.connector_version(self.server.read_bytes(), self.mo2), future_version)
        installed = json.loads((self.mo2 / 'plugins/mo2_codex_bridge/.bridge-install.json').read_text())
        self.assertEqual(installed['version'], future_version)
        new_settings = json.loads(self.mcp.read_text(encoding='utf-8'))
        self.assertEqual(new_settings['mcpServers']['mo2_bridge'], generated['mcpServers']['mo2_bridge'])
        self.assertEqual(new_settings['mcpServers']['another'], {'command': 'preserve-me'})

    def test_exact_installed_zipapp_advertises_download_and_preview_actions(self):
        self.install()
        request = '\n'.join([
            json.dumps(dict(jsonrpc='2.0', id=1, method='initialize', params={})),
            json.dumps(dict(jsonrpc='2.0', id=2, method='tools/list', params={})),
        ]) + '\n'
        reply = subprocess.run([sys.executable, '-s', '-B', str(self.server), '--mo2', str(self.mo2)],
                               executable=sys.executable, input=request, encoding='utf-8', capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertEqual(reply.stderr, '')
        init, listed = map(json.loads, reply.stdout.splitlines())
        self.assertEqual(init['result']['serverInfo']['version'], setup.VERSION)
        schemas = {tool['name']: tool['inputSchema'] for tool in listed['result']['tools']}
        self.assertLessEqual({'nexus_files', 'nexus_download_status', 'nexus_find_archive',
                              'preview_optional_plugin', 'preview_mod_replacement'},
                             set(schemas['mo2_inspect']['properties']['action']['enum']))
        self.assertLessEqual({'nexus_download', 'nexus_download_retry'},
                             set(schemas['mo2_change']['properties']['action']['enum']))

    def test_legacy_registration_and_launcher_are_preserved(self):
        original = self.source / 'mcp_server.py'
        before = original.read_bytes()
        with (patch.object(setup.shutil, 'which', return_value='fixture-codex.exe'),
              patch.object(setup.subprocess, 'run', return_value=self.run_result(entry=self.entry(server=original))) as run):
            report = self.install(register_codex=True)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(report['registration'], 'failed')
        self.assertIn('migrate separately', report['error'])
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(json.loads(self.mcp.read_text())['mcpServers']['mo2_bridge']['args'][0], str(self.server))

    def test_disabled_stable_registration_is_preserved(self):
        with patch.object(setup.subprocess, 'run', return_value=self.run_result(entry=self.entry(enabled=False))) as run:
            self.assertEqual(setup.codex_registration('fixture-codex.exe', self.server, self.mo2), 'preserved; disabled')
        self.assertEqual(run.call_count, 1)

    def test_registration_timeout_reconciles_instead_of_adding_twice(self):
        results = [self.missing(), subprocess.TimeoutExpired('fixture', 30), self.run_result(entry=self.entry())]
        with patch.object(setup.subprocess, 'run', side_effect=results) as run:
            self.assertEqual(setup.codex_registration('fixture-codex.exe', self.server, self.mo2), 'registered')
        self.assertEqual(sum(call.args[0][2] == 'add' for call in run.call_args_list), 1)

    def test_first_component_failure_restores_connector_absence(self):
        with patch.object(setup, 'install', side_effect=setup.SetupInstallError('fixture rejected')):
            with self.assertRaises(setup.SetupInstallError) as result:
                self.install()
        self.assertFalse(self.server.exists())
        self.assertFalse(self.mcp.exists())
        self.assertEqual(result.exception.details['connector_recovery'], 'previous connector state verified')

    def test_component_failure_restores_prior_bundle_and_verifies_backup(self):
        self.install()
        before = self.server.read_bytes()
        prior_config = self.mcp.read_bytes()
        next_version = '0.1.3-local.8-candidate.999'
        release_two = self.make_release('versioned update', version=next_version)
        failure = setup.SetupInstallError('fixture rejected', {'installed': False})
        with patch.object(setup, 'VERSION', next_version), patch.object(setup, 'install', side_effect=failure):
            with self.assertRaises(setup.SetupInstallError) as result:
                self.install(release_two)
        self.assertEqual(self.server.read_bytes(), before)
        self.assertEqual(self.mcp.read_bytes(), prior_config)
        self.assertEqual(Path(result.exception.details['connector_backup']).read_bytes(), before)

    def test_uncertain_component_state_preserves_connector_and_rollback(self):
        self.install()
        before = self.server.read_bytes()
        next_version = '0.1.3-local.8-candidate.999'
        release_two = self.make_release('versioned update', version=next_version)
        failure = setup.SetupInstallError('fixture interrupted',
                                          {'recovery': 'manual recovery required; keep MO2 closed'})
        with patch.object(setup, 'VERSION', next_version), patch.object(setup, 'install', side_effect=failure):
            with self.assertRaises(setup.SetupInstallError) as result:
                self.install(release_two)
        self.assertEqual(setup.connector_version(self.server.read_bytes(), self.mo2), next_version)
        self.assertEqual(Path(result.exception.details['connector_backup']).read_bytes(), before)
        self.assertIn('component state is uncertain', result.exception.details['connector_recovery'])

    def test_interruption_in_component_swap_restores_matching_pair(self):
        self.install()
        before = self.server.read_bytes()
        component = self.mo2 / 'plugins/mo2_codex_bridge'
        previous_files = setup.inventory(component)
        next_version = '0.1.3-local.8-candidate.999'
        release_two = self.make_release('versioned update', version=next_version)
        replace = setup.os.replace

        def interrupt_after_rename(source, destination):
            replace(source, destination)
            if Path(destination) == component and 'setup-staging' in Path(source).parts:
                raise KeyboardInterrupt

        with patch.object(setup, 'VERSION', next_version), patch.object(setup.os, 'replace', side_effect=interrupt_after_rename):
            with self.assertRaises(setup.SetupInstallError) as result:
                self.install(release_two)
        self.assertEqual(setup.inventory(component), previous_files)
        self.assertEqual(self.server.read_bytes(), before)
        self.assertEqual(result.exception.details['error'], 'KeyboardInterrupt')
        self.assertIn('restored and verified', result.exception.details['recovery'])

    def test_interruption_after_component_commit_preserves_matching_connector(self):
        self.install()
        before = self.server.read_bytes()
        version = '0.1.3-local.8-candidate.999'
        release_two = self.make_release('versioned update', version=version)
        install = setup.install

        def install_then_interrupt(*args, **kwargs):
            install(*args, **kwargs)
            raise KeyboardInterrupt

        with patch.object(setup, 'VERSION', version), patch.object(setup, 'install', side_effect=install_then_interrupt):
            with self.assertRaises(setup.SetupInstallError) as result:
                self.install(release_two)
        installed = json.loads((self.mo2 / 'plugins/mo2_codex_bridge/.bridge-install.json').read_text())
        self.assertEqual(installed['version'], version)
        self.assertEqual(setup.connector_version(self.server.read_bytes(), self.mo2), version)
        self.assertEqual(Path(result.exception.details['connector_backup']).read_bytes(), before)
        self.assertIn('component state is uncertain', result.exception.details['connector_recovery'])

    def test_ambiguous_activation_rename_is_reconciled(self):
        replace = setup.os.replace

        def activate_then_fail(source, destination):
            replace(source, destination)
            if Path(destination) == self.server:
                raise OSError('fixture interrupted activation')

        with patch.object(setup.os, 'replace', side_effect=activate_then_fail):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                self.install()
        self.assertFalse(self.server.exists())
        self.assertFalse((self.mo2 / 'plugins/mo2_codex_bridge').exists())

    def test_unknown_connector_bytes_are_preserved(self):
        self.server.parent.mkdir(parents=True)
        self.server.write_bytes(b'unfamiliar connector')
        with self.assertRaisesRegex(ValueError, 'unfamiliar'):
            self.install()
        self.assertEqual(self.server.read_bytes(), b'unfamiliar connector')
        self.assertFalse((self.mo2 / 'plugins/mo2_codex_bridge').exists())

    def test_another_mo2_cannot_take_over_permanent_connector(self):
        self.install()
        before = self.server.read_bytes()
        other = self.make_mo2('Other MO2')
        with self.assertRaisesRegex(ValueError, 'different MO2 installation'):
            setup.install_with_connector(other, self.source, self.config)
        self.assertEqual(self.server.read_bytes(), before)
        self.assertFalse((other / 'plugins/mo2_codex_bridge').exists())

    def test_another_mo2_cannot_take_over_legacy_settings(self):
        self.config.parent.mkdir()
        current = self.make_mo2('Other MO2')
        self.config.write_text(json.dumps({'mo2_path': str(current)}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'different MO2 installation'):
            self.install()
        self.assertFalse(self.server.exists())
        self.assertEqual(json.loads(self.config.read_text())['mo2_path'], str(current))

    def test_concurrent_setup_is_refused(self):
        with setup.connector_install_lock(self.config.parent):
            with self.assertRaisesRegex(ValueError, 'Another connector setup'):
                self.install()
        self.assertFalse(self.server.exists())
        self.assertTrue(self.install()['installed'])

    def test_unrecognized_active_settings_are_preserved_before_component_write(self):
        self.mcp.parent.mkdir(parents=True)
        self.mcp.write_text('{"mcpServers": []}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'MCP server configuration'):
            self.install()
        self.assertFalse(self.server.exists())
        self.assertFalse((self.mo2 / 'plugins/mo2_codex_bridge').exists())
        self.assertEqual(self.mcp.read_text(), '{"mcpServers": []}')

    def test_configuration_change_during_component_install_is_not_overwritten(self):
        install = setup.install

        def install_and_set_preferences(*args, **kwargs):
            result = install(*args, **kwargs)
            self.config.write_text('{"preference": "newer"}', encoding='utf-8')
            return result

        with patch.object(setup, 'install', side_effect=install_and_set_preferences):
            report = self.install()
        self.assertEqual(report['configuration'], 'failed')
        self.assertIn('metadata changed', report['error'])
        self.assertTrue(report['installed'])
        self.assertEqual(self.config.read_text(), '{"preference": "newer"}')
        self.assertEqual(setup.connector_version(self.server.read_bytes(), self.mo2), setup.VERSION)

    def test_external_connector_change_after_component_install_is_reported_and_preserved(self):
        install = setup.install

        def install_and_replace_connector(*args, **kwargs):
            result = install(*args, **kwargs)
            self.server.write_bytes(b'newer external change')
            return result

        with patch.object(setup, 'install', side_effect=install_and_replace_connector):
            report = self.install()
        self.assertTrue(report['installed'])
        self.assertEqual(report['configuration'], 'not attempted')
        self.assertIn('connector changed during setup', report['error'])
        self.assertEqual(self.server.read_bytes(), b'newer external change')


if __name__ == '__main__':
    unittest.main()
