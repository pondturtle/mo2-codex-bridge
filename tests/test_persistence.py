"""Regression coverage for the MO2 v2.5.2 in-memory-only plugin API bug."""
import json
import types
import unittest
from pathlib import Path

import test_bridge
from mo2_codex_bridge.persistence import PluginPersistence


class PersistenceTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    change = test_bridge.BridgeTests.change
    def add_plugin(self, name='Other.esp'):
        pl = self.organizer.pl
        pl.order.append(name)
        pl.active.add(name)
        pl.refresh_indexes()
        pl.write_files()

    def test_negative_control_raw_api_leaves_files_and_indexes_stale(self):
        self.add_plugin()
        pl = self.organizer.pl
        old = (self.root / 'profile/loadorder.txt').read_bytes()
        pl.setLoadOrder(['Master.esm', 'Other.esp', 'Child.esp'])
        self.assertEqual(pl.loadOrder('Child.esp'), 1)
        self.assertEqual((self.root / 'profile/loadorder.txt').read_bytes(), old)
        self.assertNotEqual(pl.priority('Child.esp'), pl.loadOrder('Child.esp'))

    def test_order_commit_updates_indexes_and_both_files(self):
        self.add_plugin()
        order = ['Master.esm', 'Other.esp', 'Child.esp']
        result = self.change('set_plugin_order', plugins=order)
        self.assertTrue(result['persisted'])
        self.assertTrue(result['exact_match'])
        self.assertEqual(self.organizer.pl.order, order)
        self.assertEqual(self.organizer.pl.loadOrder('Child.esp'), 2)
        self.assertEqual(len(self.organizer.pl.commit_calls), 1)
        self.assertIn('Other.esp\nChild.esp', (self.root / 'profile/loadorder.txt').read_text())

    def test_activation_and_deactivation_commit(self):
        result = self.change('set_plugin_active', plugin='Child.esp', active=False)
        self.assertTrue(result['persisted'])
        self.assertEqual(self.organizer.pl.loadOrder('Child.esp'), -1)
        self.assertNotIn('*Child.esp', (self.root / 'profile/plugins.txt').read_text())
        result = self.change('set_plugin_active', plugin='Child.esp', active=True)
        self.assertTrue(result['persisted'])
        self.assertIn('*Child.esp', (self.root / 'profile/plugins.txt').read_text())

    def test_delayed_save_is_awaited(self):
        self.add_plugin()
        pl = self.organizer.pl
        pl.defer_save = True
        polls = []
        def wait(check, timeout):
            self.assertEqual(timeout, 5000)
            polls.append(check())
            pl.write_files()  # Models MO2's delayed writer when Qt events run.
            polls.append(check())
            return polls[-1]
        self.ops.wait_until = wait
        self.change('set_plugin_order', plugins=['Master.esm', 'Other.esp', 'Child.esp'])
        self.assertEqual(polls, [False, True])

    def test_save_timeout_is_failure_without_rollback(self):
        self.add_plugin()
        pl = self.organizer.pl
        pl.defer_save = True
        order = ['Master.esm', 'Other.esp', 'Child.esp']
        with self.assertRaisesRegex(RuntimeError, 'saving was not confirmed'):
            self.change('set_plugin_order', plugins=order)
        self.assertEqual(pl.order, order)
        self.assertEqual(len(pl.commit_calls), 1)
        errors = list((self.root / 'data/backups').glob('*/error.json'))
        self.assertEqual(len(errors), 1)

    def test_no_safe_anchor_fails_before_mutation(self):
        pl = self.organizer.pl
        pl.isMasterFlagged = lambda name: True
        with self.assertRaisesRegex(RuntimeError, 'No ordinary ESP'):
            self.change('set_plugin_active', plugin='Child.esp', active=False)
        self.assertIn('Child.esp', pl.active)
        self.assertEqual(pl.commit_calls, [])

    def test_missing_waiter_fails_before_mutation(self):
        self.ops.wait_until = None
        with self.assertRaisesRegex(RuntimeError, 'support unavailable'):
            self.change('set_plugin_active', plugin='Child.esp', active=False)
        self.assertIn('Child.esp', self.organizer.pl.active)

    def test_profile_switch_during_wait_is_failure(self):
        self.add_plugin()
        self.organizer.pl.defer_save = True
        def wait(check, timeout):
            self.organizer.profileName = lambda: 'Other profile'
            return check()
        self.ops.wait_until = wait
        with self.assertRaisesRegex(RuntimeError, 'Profile changed'):
            self.change('set_plugin_order', plugins=['Master.esm', 'Other.esp', 'Child.esp'])

    def test_concurrent_edit_during_wait_is_failure(self):
        self.add_plugin()
        self.organizer.pl.defer_save = True
        def wait(check, timeout):
            self.organizer.pl.active.discard('Child.esp')
            return check()
        self.ops.wait_until = wait
        with self.assertRaisesRegex(RuntimeError, 'changed during commit'):
            self.change('set_plugin_order', plugins=['Master.esm', 'Other.esp', 'Child.esp'])
        self.assertNotIn('Child.esp', self.organizer.pl.active)

    def test_missing_active_nonprimary_is_not_success(self):
        pl = self.organizer.pl
        pl.defer_save = True
        (self.root / 'profile/plugins.txt').write_text('# Missing Child.esp\n')
        with self.assertRaisesRegex(RuntimeError, 'missing an active plugin'):
            self.change('set_plugin_order', plugins=list(pl.order))

    def test_wrong_activation_file_is_not_success(self):
        pl = self.organizer.pl
        pl.defer_save = True
        with self.assertRaisesRegex(RuntimeError, 'activation differs'):
            self.change('set_plugin_active', plugin='Child.esp', active=False)

    def test_disk_casing_and_implicit_primary_are_supported(self):
        pl = self.organizer.pl
        pl.defer_save = True
        for name in ['plugins.txt', 'loadorder.txt']:
            path = self.root / 'profile' / name
            path.write_text(path.read_text().upper())
        self.assertTrue(self.change('set_plugin_order', plugins=list(pl.order))['persisted'])

    def test_duplicate_and_unknown_entries_are_not_success(self):
        pl = self.organizer.pl
        pl.defer_save = True
        for entries in ['*Child.esp\n*Child.esp\n', '*Child.esp\n*Unknown.esp\n']:
            (self.root / 'profile/plugins.txt').write_text(entries)
            with self.assertRaisesRegex(RuntimeError, 'duplicate or unexpected'):
                self.change('set_plugin_order', plugins=list(pl.order))

    def test_missing_profile_files_are_not_success(self):
        self.organizer.pl.defer_save = True
        (self.root / 'profile/plugins.txt').unlink()
        with self.assertRaisesRegex(RuntimeError, 'cannot be read'):
            self.change('set_plugin_order', plugins=list(self.organizer.pl.order))

    def test_inactive_entries_may_be_omitted(self):
        pl = self.organizer.pl
        pl.active.discard('Child.esp')
        pl.refresh_indexes()
        pl.write_files()
        pl.defer_save = True
        (self.root / 'profile/plugins.txt').write_text('# inactive omitted\n')
        self.assertTrue(self.change('set_plugin_order', plugins=list(pl.order))['persisted'])

    def test_case_insensitive_master_dependency_is_enforced(self):
        self.organizer.pl.masters = lambda n: ['MASTER.ESM'] if n == 'Child.esp' else []
        with self.assertRaisesRegex(ValueError, 'dependent'):
            self.change('set_plugin_active', plugin='Master.esm', active=False)
        with self.assertRaisesRegex(ValueError, 'master must load'):
            self.change('set_plugin_order', plugins=['Child.esp', 'Master.esm'])

    def test_restore_order_also_commits(self):
        self.add_plugin()
        result = self.change('set_plugin_order', plugins=['Master.esm', 'Other.esp', 'Child.esp'])
        self.change('set_plugin_active', plugin='Child.esp', active=False)
        restored = self.change('restore_order', backup_id=result['backup_id'])
        self.assertTrue(restored['persisted'])
        self.assertEqual(self.organizer.pl.order, ['Master.esm', 'Child.esp', 'Other.esp'])
        self.assertIn('Child.esp', self.organizer.pl.active)
        self.assertIn('*Child.esp', (self.root / 'profile/plugins.txt').read_text())

    def test_map_tail_and_flagged_light_esp_anchor_do_not_move(self):
        self.add_plugin('Example Map Tail.esp')
        self.add_plugin('Example Map Menu.esp')
        self.organizer.pl.isLightFlagged = lambda n: n == 'Child.esp'
        order = list(self.organizer.pl.order)
        self.assertTrue(self.change('set_plugin_order', plugins=order)['persisted'])
        self.assertEqual(self.organizer.pl.order, order)
        self.assertEqual(self.organizer.pl.commit_calls, [('Child.esp', 1)])

    def test_primary_plugin_cannot_be_disabled_without_dependents(self):
        self.organizer.pl.masters = lambda n: []
        with self.assertRaisesRegex(ValueError, 'Primary game plugins'):
            self.change('set_plugin_active', plugin='Master.esm', active=False)
        self.assertIn('Master.esm', self.organizer.pl.active)

    def test_rejected_commit_signal_is_failure(self):
        self.organizer.pl.setPriority = lambda *args: False
        with self.assertRaisesRegex(RuntimeError, 'rejected the save signal'):
            self.change('set_plugin_active', plugin='Child.esp', active=False)

    def test_derived_index_mismatch_is_failure(self):
        pl = self.organizer.pl
        pl.refresh_indexes = lambda: None
        with self.assertRaisesRegex(RuntimeError, 'derived plugin indexes'):
            self.change('set_plugin_active', plugin='Child.esp', active=False)


if __name__ == '__main__':
    unittest.main(verbosity=2)
