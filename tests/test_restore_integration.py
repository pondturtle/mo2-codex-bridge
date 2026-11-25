"""Full restoration path: dependency refusal, delayed saves and partial failures."""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_bridge
from mo2_codex_bridge.mod_persistence import ModListPersistence


class RestoreIntegrationTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    change = test_bridge.BridgeTests.change

    def checkpoint(self):
        return self.change('set_mod_priority', mod='A', priority=1)['backup_id']

    def tracked_mutators(self):
        return [patch.object(target, name, wraps=getattr(target, name)) for target, name in (
            (self.organizer.ml, 'setPriority'), (self.organizer.ml, 'setActive'),
            (self.organizer.pl, 'setLoadOrder'), (self.organizer.pl, 'setState'))]

    def test_missing_master_refused_before_every_mutation_api(self):
        backup_id = self.checkpoint()
        self.organizer.pl.masters = lambda name: ['Master.esm', 'Missing.esm'] if name == 'Child.esp' else []
        self.organizer.pl.active.discard('Child.esp')
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        original = (self.root / 'profile/modlist.txt').read_bytes()
        patches = self.tracked_mutators()
        mocks = [item.start() for item in patches]
        try:
            with self.assertRaisesRegex(ValueError, 'missing or disabled master') as caught:
                self.change('restore_order', backup_id=backup_id)
            self.assertTrue(all(item.call_count == 0 for item in mocks))
        finally:
            for item in reversed(patches): item.stop()
        self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertEqual((self.root / 'profile/modlist.txt').read_bytes(), original)
        self.assertNotIn('Child.esp', self.organizer.pl.active)

    def test_new_required_master_after_dependent_refused_before_mod_move(self):
        backup_id = self.checkpoint()
        pl = self.organizer.pl
        pl.order = ['Master.esm', 'NewMaster.esp', 'Child.esp']
        pl.active.add('NewMaster.esp')
        pl.masters = lambda name: ['Master.esm', 'NewMaster.esp'] if name == 'Child.esp' else []
        pl.refresh_indexes()
        pl.write_files()
        with patch.object(self.organizer.ml, 'setPriority', wraps=self.organizer.ml.setPriority) as moved:
            with self.assertRaisesRegex(ValueError, 'master must load before'):
                self.change('restore_order', backup_id=backup_id)
            moved.assert_not_called()
        self.assertEqual(self.organizer.ml.order, ['B', 'A'])

    def test_valid_restore_verifies_all_three_native_profile_lists(self):
        backup_id = self.checkpoint()
        result = self.change('restore_order', backup_id=backup_id)
        self.assertTrue(result['persisted'])
        self.assertTrue(result['modlist_persisted'])
        self.assertEqual(set(result['verified_files']), {'loadorder.txt', 'plugins.txt', 'modlist.txt'})
        self.assertEqual(self.organizer.ml.order, ['A', 'B'])
        self.assertEqual((self.root / 'profile/modlist.txt').read_text(), '# MO2\n+B\n+A\n')

    def test_modlist_delayed_write_is_awaited(self):
        backup_id = self.checkpoint()
        ml = self.organizer.ml
        ml.defer_save = True
        polls = []
        def wait(check, timeout):
            if check(): return True
            polls.append('delayed save')
            ml.write_files()
            return check()
        self.ops.wait_until = wait
        result = self.change('restore_order', backup_id=backup_id)
        self.assertTrue(result['modlist_persisted'])
        self.assertEqual(polls, ['delayed save'])

    def test_modlist_timeout_retains_live_change_and_recovery_details(self):
        backup_id = self.checkpoint()
        ml = self.organizer.ml
        old_saved = (self.root / 'profile/modlist.txt').read_bytes()
        ml.defer_save = True
        with self.assertRaisesRegex(RuntimeError, 'saved modlist.txt was not confirmed') as caught:
            self.change('restore_order', backup_id=backup_id)
        failure = caught.exception.bridge_failure
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(failure['recovery_details']['stage'], 'verify-mod-save')
        self.assertFalse(failure['recovery_details']['automatic_rollback_attempted'])
        self.assertEqual(ml.order, ['A', 'B'])
        self.assertEqual((self.root / 'profile/modlist.txt').read_bytes(), old_saved)
        error_path = self.root / 'data/backups' / failure['backup_id'] / 'error.json'
        self.assertTrue(error_path.is_file())

    def test_missing_modlist_refused_before_setters(self):
        backup_id = self.checkpoint()
        (self.root / 'profile/modlist.txt').unlink()
        with patch.object(self.organizer.pl, 'setLoadOrder', wraps=self.organizer.pl.setLoadOrder) as set_order:
            with self.assertRaisesRegex(RuntimeError, 'modlist.txt is unavailable') as caught:
                self.change('restore_order', backup_id=backup_id)
            set_order.assert_not_called()
        self.assertFalse(caught.exception.bridge_failure['changes_may_have_occurred'])
        self.assertEqual(self.organizer.ml.order, ['B', 'A'])

    def test_plugin_bearing_mod_enable_refused_without_prospective_headers(self):
        # Backup wants B enabled. Current B is disabled; current visible plugin
        # list cannot prove the headers that would appear after enabling B.
        backup_id = self.checkpoint()
        (self.root / 'mods/B/Additional.esp').write_bytes(b'fake placeholder not parsed')
        self.organizer.ml.setActive('B', False)
        with patch.object(self.organizer.ml, 'setActive', wraps=self.organizer.ml.setActive) as activate:
            with self.assertRaisesRegex(ValueError, 'Plugin-bearing mod'):
                self.change('restore_order', backup_id=backup_id)
            activate.assert_not_called()
        self.assertNotIn('B', self.organizer.ml.active)

    def test_verified_plugin_free_mod_enable_is_supported(self):
        backup_id = self.checkpoint()
        (self.root / 'mods/B/options.ini').write_bytes(b'preserved configuration')
        self.organizer.ml.setActive('B', False)
        result = self.change('restore_order', backup_id=backup_id)
        self.assertTrue(result['modlist_persisted'])
        self.assertIn('B', self.organizer.ml.active)
        self.assertEqual((self.root / 'mods/B/options.ini').read_bytes(), b'preserved configuration')

    def test_corrupted_order_backup_refused_before_mod_setter(self):
        backup_id = self.checkpoint()
        before_path = self.root / 'data/backups' / backup_id / 'before.json'
        value = json.loads(before_path.read_text())
        value['mods'][0]['active'] = False
        before_path.write_text(json.dumps(value))
        with patch.object(self.organizer.ml, 'setPriority', wraps=self.organizer.ml.setPriority) as move:
            with self.assertRaisesRegex(ValueError, 'integrity'):
                self.change('restore_order', backup_id=backup_id)
            move.assert_not_called()
        self.assertEqual(self.organizer.ml.order, ['B', 'A'])


class ModListVerifierTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def test_foreign_and_separator_rows_use_native_markers(self):
        mods = [dict(name='Base DLC', priority=0, active=True, foreign=True, overwrite=False),
                dict(name='Separator_separator', priority=1, active=False, foreign=False, overwrite=False),
                dict(name='Regular', priority=2, active=True, foreign=False, overwrite=False)]
        path = self.root / 'profile/modlist.txt'
        path.write_bytes(b'# MO2\r\n+Regular\r\n-Separator_separator\r\n*Base DLC\r\n')
        verifier = ModListPersistence(self.organizer, lambda predicate, timeout: predicate())
        self.assertTrue(verifier.verify(mods, lambda: None)['modlist_persisted'])

    def test_duplicate_or_invalid_saved_rows_never_verify(self):
        mods = self.ops.snapshot()['mods']
        verifier = ModListPersistence(self.organizer, lambda predicate, timeout: predicate())
        for content in ['+B\n+A\n+A\n', '+B\n?A\n', '+A\n+B\n', '+B\n-A\n']:
            with self.subTest(content=content):
                (self.root / 'profile/modlist.txt').write_text(content)
                with self.assertRaisesRegex(RuntimeError, 'was not confirmed'):
                    verifier.verify(mods, lambda: None)


if __name__ == '__main__':
    unittest.main(verbosity=2)
