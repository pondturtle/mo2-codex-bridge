"""Configuration recovery regressions using fake profiles and disposable files."""
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import test_bridge
from mo2_codex_bridge import config_recovery as recovery
from mo2_codex_bridge.common import digest, write_json


class ConfigurationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parents[2] / 'config-recovery-test-fixtures'
        self.fixture_parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='config-', dir=self.fixture_parent)
        self.root = Path(self.temporary.name)
        self.organizer = test_bridge.Organizer(self.root)
        self.ops = test_bridge.Operations(self.organizer, types.SimpleNamespace(
            ModState=test_bridge.ModState, PluginState=test_bridge.PluginState, GuessedString=str),
            self.root / 'data', lambda: None, wait_until=lambda check, timeout: check())
        self.path = self.root / 'mods/A/options.ini'
        self.path.write_bytes(b'[Settings]\r\nValue=old\r\n')

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.fixture_parent.resolve()))
        self.temporary.cleanup()

    change = test_bridge.BridgeTests.change

    def write(self, path=None, **overrides):
        path = path or self.path
        params = {'mod': 'A', 'path': path.relative_to(self.root / 'mods/A').as_posix(),
                  'expected_sha256': digest(path.read_bytes()) if path.exists() else None,
                  'text': '[Settings]\r\nValue=new\r\n'}
        params.update(overrides)
        return self.change('write_config', **params)

    def backup_path(self, result):
        return self.ops.data / 'backups' / result['backup_id']

    def legacy(self, result):
        folder = self.backup_path(result)
        manifest = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
        legacy = {key: manifest[key] for key in (
            'scope', 'mod', 'path', 'existed', 'before_sha256', 'after_sha256')}
        write_json(folder / 'config.json', legacy)
        # Real pre-fix snapshots lack recovery_identity; rehash after removing it.
        before = json.loads((folder / 'before.json').read_text(encoding='utf-8'))
        before.pop('recovery_identity', None)
        before.pop('revision')
        before['revision'] = digest(json.dumps(before, sort_keys=True).encode())
        write_json(folder / 'before.json', before)
        return folder

    def test_write_restore_and_undo_restore_roundtrip_exact_bytes(self):
        original = self.path.read_bytes()
        written = self.write()
        modified = self.path.read_bytes()
        restored = self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(restored['recovery_verified'])
        redo = self.change('restore_config', backup_id=restored['backup_id'])
        self.assertEqual(self.path.read_bytes(), modified)
        self.change('restore_config', backup_id=redo['backup_id'])
        self.assertEqual(self.path.read_bytes(), original)

    def test_creation_deletion_and_undo_absence_are_all_reversible_and_refreshed(self):
        self.path.unlink()
        refreshes = []
        self.ops.refresh = lambda: refreshes.append(self.path.exists())
        written = self.write()
        after = self.path.read_bytes()
        restored = self.change('restore_config', backup_id=written['backup_id'])
        self.assertFalse(self.path.exists())
        deleted_manifest = json.loads((self.backup_path(restored) / 'config.json').read_text())
        self.assertFalse(deleted_manifest['after_exists'])
        redo = self.change('restore_config', backup_id=restored['backup_id'])
        self.assertEqual(self.path.read_bytes(), after)
        self.change('restore_config', backup_id=redo['backup_id'])
        self.assertFalse(self.path.exists())
        self.assertEqual(refreshes, [True, False, True, False])

    def test_absent_state_restore_refuses_new_file_collision(self):
        self.path.unlink()
        written = self.write()
        restored = self.change('restore_config', backup_id=written['backup_id'])
        self.path.write_bytes(b'newer external work')
        with self.assertRaisesRegex(ValueError, 'File changed') as caught:
            self.change('restore_config', backup_id=restored['backup_id'])
        self.assertEqual(self.path.read_bytes(), b'newer external work')
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_legacy_backup_exact_original_context_is_supported(self):
        original = self.path.read_bytes()
        written = self.write()
        self.legacy(written)
        restored = self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(self.path.read_bytes(), original)
        self.change('restore_config', backup_id=restored['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_profile_backup_refuses_other_profile_with_identical_contents(self):
        profile_ini = self.root / 'profile/skyrim.ini'
        profile_ini.write_bytes(b'old profile A')
        written = self.change('write_config', scope='profile', path='skyrim.ini',
                              expected_sha256=digest(profile_ini.read_bytes()), text='same current contents')
        other = self.root / 'profile-B'
        other.mkdir()
        (other / 'skyrim.ini').write_bytes(profile_ini.read_bytes())
        self.organizer.profileName = lambda: 'Profile B'
        self.organizer.profilePath = lambda: str(other)
        with self.assertRaisesRegex(ValueError, 'another profile'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual((other / 'skyrim.ini').read_bytes(), b'same current contents')
        self.assertEqual(profile_ini.read_bytes(), b'same current contents')

    def test_legacy_profile_backup_refuses_other_profile(self):
        written = self.write()
        self.legacy(written)
        self.organizer.profileName = lambda: 'Other'
        with self.assertRaisesRegex(ValueError, 'another profile'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_same_profile_name_with_changed_physical_profile_root_is_refused(self):
        written = self.write()
        other = self.root / 'profile-other'
        other.mkdir()
        self.organizer.profilePath = lambda: str(other)
        with self.assertRaisesRegex(ValueError, 'another profile folder'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_same_mod_name_with_changed_mod_root_is_refused(self):
        written = self.write()
        mod = self.organizer.ml.mods['A']
        mod.root = self.root / 'mods/relocated'
        (mod.root / 'A').mkdir(parents=True)
        relocated = mod.root / 'A/options.ini'
        relocated.write_bytes(self.path.read_bytes())
        with self.assertRaisesRegex(ValueError, 'root or destination changed'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(relocated.read_bytes(), self.path.read_bytes())

    def test_changed_instance_paths_are_refused(self):
        written = self.write()
        elsewhere = self.root / 'different-downloads'
        elsewhere.mkdir()
        self.organizer.downloadsPath = lambda: str(elsewhere)
        with self.assertRaisesRegex(ValueError, 'instance paths differ'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_corrupt_source_payload_is_refused_before_target_change(self):
        written = self.write()
        (self.backup_path(written) / 'config-before.bin').write_bytes(b'corrupted')
        current = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'payload integrity') as caught:
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(self.path.read_bytes(), current)
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_missing_source_payload_and_missing_backup_leave_target_intact(self):
        written = self.write()
        (self.backup_path(written) / 'config-before.bin').unlink()
        current = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'payload integrity'):
            self.change('restore_config', backup_id=written['backup_id'])
        with self.assertRaisesRegex(ValueError, 'Unknown backup'):
            self.change('restore_config', backup_id='nonexistent-backup')
        self.assertEqual(self.path.read_bytes(), current)

    def test_missing_legacy_snapshot_fails_closed(self):
        written = self.write()
        self.legacy(written)
        (self.backup_path(written) / 'before.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_snapshot_corruption_fails_integrity_check(self):
        written = self.write()
        folder = self.backup_path(written)
        snapshot = json.loads((folder / 'before.json').read_text())
        snapshot['profile_path'] = str(self.root / 'forged-profile')
        write_json(folder / 'before.json', snapshot)
        with self.assertRaisesRegex(ValueError, 'snapshot integrity'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_corrupt_manifest_fields_fail_closed(self):
        written = self.write()
        folder = self.backup_path(written)
        original = json.loads((folder / 'config.json').read_text())
        for patch in [{'schema_version': 999}, {'existed': 1}, {'before_sha256': 'bad'},
                      {'after_exists': False}, {'before_size': True}, {'identity': {}},
                      {'after_sha256': None}]:
            with self.subTest(patch=patch):
                write_json(folder / 'config.json', dict(original, **patch))
                with self.assertRaises(ValueError):
                    self.change('restore_config', backup_id=written['backup_id'])
                self.assertIn(b'Value=new', self.path.read_bytes())
        write_json(folder / 'config.json', original)

    def test_duplicate_metadata_keys_are_refused(self):
        written = self.write()
        (self.backup_path(written) / 'config.json').write_text('{"scope":"mod","scope":"profile"}')
        with self.assertRaisesRegex(ValueError, 'duplicate keys'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_corrupt_new_backup_is_detected_before_target_write(self):
        original = self.path.read_bytes()
        real_prepare = recovery._prepare
        def corrupt(*args):
            manifest = real_prepare(*args)
            (Path(args[1]) / 'config-before.bin').write_bytes(b'damaged recovery')
            return manifest
        with mock.patch.object(recovery, '_prepare', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'payload integrity') as caught:
                self.write()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_new_backup_collision_is_not_overwritten(self):
        original = self.path.read_bytes()
        backup = self.ops.data / 'backups/collision'
        backup.mkdir(parents=True)
        write_json(backup / 'before.json', self.ops.snapshot())
        (backup / 'config.json').write_bytes(b'preexisting recovery evidence')
        with self.assertRaises(FileExistsError):
            recovery.write_configuration(self.ops, {'mod':'A','path':'options.ini',
                'expected_sha256':digest(original),'text':'new'}, backup)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual((backup / 'config.json').read_bytes(), b'preexisting recovery evidence')

    def test_external_edit_after_backup_is_preserved(self):
        real_write = recovery._exclusive_write
        def external_edit(path, data):
            real_write(path, data)
            if path.name == 'config.json':
                self.path.write_bytes(b'newer external work')
        with mock.patch.object(recovery, '_exclusive_write', side_effect=external_edit):
            with self.assertRaisesRegex(ValueError, 'newer work was preserved') as caught:
                self.write()
        self.assertEqual(self.path.read_bytes(), b'newer external work')
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_profile_switch_after_backup_is_detected_before_target_write(self):
        original = self.path.read_bytes()
        real_write = recovery._exclusive_write
        def switch(path, data):
            real_write(path, data)
            if path.name == 'config.json':
                self.organizer.profileName = lambda: 'Changed during write'
        with mock.patch.object(recovery, '_exclusive_write', side_effect=switch):
            with self.assertRaisesRegex(ValueError, 'identity changed') as caught:
                self.write()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_target_write_failure_preserves_original_and_verified_recovery(self):
        original = self.path.read_bytes()
        with mock.patch.object(recovery, 'atomic_write', side_effect=PermissionError('target locked')):
            with self.assertRaises(PermissionError) as caught:
                self.write()
        self.assertEqual(self.path.read_bytes(), original)
        evidence = caught.exception.recovery_details
        self.assertTrue(evidence['changes_may_have_occurred'])
        self.assertFalse(evidence['automatic_rollback_attempted'])
        self.assertEqual((Path(evidence['recovery_backup_path']) / 'config-before.bin').read_bytes(), original)

    def test_replace_then_error_is_reported_with_recovery_without_rollback(self):
        original = self.path.read_bytes()
        real_write = recovery.atomic_write
        def replace_then_fail(path, data, **kwargs):
            real_write(path, data, **kwargs)
            raise OSError('failure after replacement')
        with mock.patch.object(recovery, 'atomic_write', side_effect=replace_then_fail):
            with self.assertRaises(OSError) as caught:
                self.write()
        self.assertIn(b'Value=new', self.path.read_bytes())
        evidence = caught.exception.recovery_details
        self.assertEqual(evidence['observed']['sha256'], digest(self.path.read_bytes()))
        self.assertEqual((Path(evidence['recovery_backup_path']) / 'config-before.bin').read_bytes(), original)

    def test_creation_refresh_failure_retains_inverse_recovery_and_observation(self):
        self.path.unlink()
        self.ops.refresh = mock.Mock(side_effect=RuntimeError('refresh timed out'))
        with self.assertRaisesRegex(RuntimeError, 'refresh timed out') as caught:
            self.write()
        self.assertTrue(self.path.exists())
        self.assertEqual(caught.exception.recovery_details['stage'], 'refresh')
        self.assertTrue(caught.exception.recovery_details['observed']['exists'])
        manifest = json.loads((Path(caught.exception.recovery_details['recovery_backup_path']) / 'config.json').read_text())
        self.assertFalse(manifest['existed'])

    def test_deletion_refresh_failure_retains_complete_inverse(self):
        self.path.unlink()
        written = self.write()
        current = self.path.read_bytes()
        self.ops.refresh = mock.Mock(side_effect=RuntimeError('refresh timed out'))
        with self.assertRaises(RuntimeError) as caught:
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertFalse(self.path.exists())
        evidence = caught.exception.recovery_details
        self.assertFalse(evidence['observed']['exists'])
        self.assertEqual((Path(evidence['recovery_backup_path']) / 'config-before.bin').read_bytes(), current)
        self.ops.refresh = lambda: None
        self.change('restore_config', backup_id=evidence['backup_id'])
        self.assertEqual(self.path.read_bytes(), current)

    def test_external_edit_during_refresh_is_never_rolled_back(self):
        self.path.unlink()
        self.ops.refresh = lambda: self.path.write_bytes(b'new work during refresh')
        with self.assertRaisesRegex(ValueError, 'newer work was preserved') as caught:
            self.write()
        self.assertEqual(self.path.read_bytes(), b'new work during refresh')
        self.assertTrue(caught.exception.recovery_details['changes_may_have_occurred'])
        self.assertFalse(caught.exception.recovery_details['automatic_rollback_attempted'])

    def test_empty_file_is_preserved_as_existing_in_inverse(self):
        self.path.write_bytes(b'')
        written = self.write(text='filled')
        restored = self.change('restore_config', backup_id=written['backup_id'])
        self.assertTrue(self.path.exists())
        self.assertEqual(self.path.read_bytes(), b'')
        self.change('restore_config', backup_id=restored['backup_id'])
        self.assertEqual(self.path.read_bytes(), b'filled')

    def test_legacy_recovery_requires_original_success_journal(self):
        written = self.write()
        folder = self.legacy(written)
        (folder / 'result.json').unlink()
        current = self.path.read_bytes()
        with self.assertRaises(FileNotFoundError) as caught:
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(self.path.read_bytes(), current)
        self.assertFalse(caught.exception.recovery_details['changes_may_have_occurred'])

    def test_legacy_journal_action_destination_hash_and_byte_count_are_checked(self):
        written = self.write()
        folder = self.legacy(written)
        original = json.loads((folder / 'result.json').read_text())
        variants = [dict(original, action='restore_config')]
        for field, bad in [('path', str(self.path.parent / 'different.ini')),
                           ('sha256', '0' * 64), ('bytes', -1),
                           ('bytes', len(self.path.read_bytes()) + 1)]:
            variants.append(dict(original, result=dict(original['result'], **{field: bad})))
        for invalid in variants:
            with self.subTest(journal=invalid):
                write_json(folder / 'result.json', invalid)
                with self.assertRaises(ValueError):
                    self.change('restore_config', backup_id=written['backup_id'])
                self.assertIn(b'Value=new', self.path.read_bytes())

    def test_legacy_descriptor_cannot_redirect_to_other_identical_file(self):
        written = self.write()
        folder = self.legacy(written)
        other = self.root / 'mods/B/options.ini'
        other.write_bytes(self.path.read_bytes())
        manifest = json.loads((folder / 'config.json').read_text())
        manifest['mod'] = 'B'
        write_json(folder / 'config.json', manifest)
        with self.assertRaisesRegex(ValueError, 'journal destination'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(other.read_bytes(), self.path.read_bytes())

    def test_schema_removal_does_not_downgrade_identity_validation(self):
        written = self.write()
        folder = self.backup_path(written)
        manifest = json.loads((folder / 'config.json').read_text())
        manifest.pop('schema_version')
        write_json(folder / 'config.json', manifest)
        with self.assertRaisesRegex(ValueError, 'Ambiguous legacy'):
            self.change('restore_config', backup_id=written['backup_id'])
        self.assertIn(b'Value=new', self.path.read_bytes())

    def test_recorded_external_metadata_path_is_rejected_without_resolving_it(self):
        written = self.write()
        folder = self.backup_path(written)
        snapshot = json.loads((folder / 'before.json').read_text())
        snapshot['profile_path'] = 'X:/untrusted-recorded-metadata'
        snapshot.pop('revision')
        snapshot['revision'] = digest(json.dumps(snapshot, sort_keys=True).encode())
        write_json(folder / 'before.json', snapshot)
        real_resolve = Path.resolve
        attempts = []
        def refuse_external(path, *args, **kwargs):
            if str(path).lower().startswith('x:'):
                attempts.append(str(path))
                raise AssertionError('Untrusted recorded path was resolved')
            return real_resolve(path, *args, **kwargs)
        with mock.patch.object(Path, 'resolve', refuse_external):
            with self.assertRaisesRegex(ValueError, 'another profile folder'):
                self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(attempts, [])

    def test_external_edit_during_restore_staging_is_preserved(self):
        from mo2_codex_bridge import common
        written = self.write()
        external = b'newer external work during restore staging'
        real_mkstemp = common.tempfile.mkstemp
        def external_edit(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            if Path(kwargs.get('dir', '')).resolve() == self.path.parent.resolve():
                self.path.write_bytes(external)
            return result
        with mock.patch.object(common.tempfile, 'mkstemp', side_effect=external_edit):
            with self.assertRaisesRegex(ValueError, 'newer work was preserved'):
                self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(self.path.read_bytes(), external)
        self.assertEqual(list(self.path.parent.glob('.bridge-*')), [])

    def test_recovery_corruption_during_target_staging_is_caught_before_replace(self):
        from mo2_codex_bridge import common
        original = self.path.read_bytes()
        real_mkstemp = common.tempfile.mkstemp
        def corrupt(*args, **kwargs):
            result = real_mkstemp(*args, **kwargs)
            if Path(kwargs.get('dir', '')).resolve() == self.path.parent.resolve():
                folders = list((self.ops.data / 'backups').iterdir())
                self.assertEqual(len(folders), 1)
                (folders[0] / 'config-before.bin').write_bytes(b'corrupt during staging')
            return result
        with mock.patch.object(common.tempfile, 'mkstemp', side_effect=corrupt):
            with self.assertRaisesRegex(ValueError, 'payload integrity'):
                self.write()
        self.assertEqual(self.path.read_bytes(), original)

    def test_windows_relative_separators_are_normalized(self):
        nested = self.path.parent / 'nested/options.ini'
        nested.parent.mkdir()
        nested.write_bytes(b'nested old')
        written = self.change('write_config', mod='A', path='nested\\options.ini',
                              expected_sha256=digest(nested.read_bytes()), text='nested new')
        manifest = json.loads((self.backup_path(written) / 'config.json').read_text())
        self.assertEqual(manifest['path'], 'nested/options.ini')
        self.change('restore_config', backup_id=written['backup_id'])
        self.assertEqual(nested.read_bytes(), b'nested old')


if __name__ == '__main__':
    unittest.main(verbosity=2)
