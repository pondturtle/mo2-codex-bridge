"""Failure injection for candidate-only recovery reporting; never connects to MO2."""
import json
import unittest
from unittest.mock import patch

import test_bridge
import mo2_codex_bridge.operations as operations


class FailureReportingTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    change = test_bridge.BridgeTests.change

    def failure(self, callback, expected=Exception):
        with self.assertRaises(expected) as captured:
            callback()
        return captured.exception, captured.exception.bridge_failure

    def test_stale_revision_is_rejected_before_recovery_or_mutation(self):
        error, failure = self.failure(lambda: self.ops.dispatch('set_mod_active', {
            'mod': 'A', 'active': False, 'expected_profile': 'Test', 'expected_revision': 'stale'}), ValueError)
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertNotIn('backup_id', failure)
        self.assertFalse((self.root / 'data' / 'backups').exists())
        self.assertTrue(self.ops.is_active('A'))

    def test_running_guard_reports_no_mutation(self):
        self.ops.running.add('fake-game.exe')
        _, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), ValueError)
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertNotIn('backup_id', failure)
        self.assertTrue(self.ops.is_active('A'))

    def test_backup_directory_failure_preserves_original_exception(self):
        with patch('pathlib.Path.mkdir', side_effect=PermissionError('fake permission denial')):
            error, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), PermissionError)
        self.assertEqual(str(error), 'fake permission denial')
        self.assertEqual(failure['phase'], 'backup')
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertNotIn('backup_id', failure)
        self.assertTrue(self.ops.is_active('A'))

    def test_before_snapshot_failure_retains_created_backup_id(self):
        original = operations.write_json
        def write(path, value):
            if path.name == 'before.json':
                raise OSError('fake snapshot write failure')
            return original(path, value)
        with patch.object(operations, 'write_json', side_effect=write):
            _, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), OSError)
        self.assertEqual(failure['phase'], 'backup')
        self.assertFalse(failure['changes_may_have_occurred'])
        backup = self.root / 'data' / 'backups' / failure['backup_id']
        self.assertTrue((backup / 'error.json').is_file())
        self.assertTrue(self.ops.is_active('A'))

    def test_partial_operation_retains_observed_state_and_recovery_link(self):
        def partial(params, backup):
            self.organizer.ml.active.discard('A')
            raise RuntimeError('fake failure after state change')
        self.ops.set_mod_active = partial
        error, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), RuntimeError)
        self.assertEqual(str(error), 'fake failure after state change')
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(failure['observed_state']['profile'], 'Test')
        self.assertFalse(self.ops.is_active('A'))
        saved = json.loads((self.root / 'data' / 'backups' / failure['backup_id'] / 'error.json').read_text())
        self.assertEqual(saved['backup_id'], failure['backup_id'])
        self.assertTrue(saved['changes_may_have_occurred'])

    def test_operation_preflight_can_prove_no_changes(self):
        def rejected(params, backup):
            error = ValueError('fake recovery preflight refusal')
            error.recovery_details = {'stage': 'identity', 'changes_may_have_occurred': False}
            raise error
        self.ops.set_mod_active = rejected
        _, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), ValueError)
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertEqual(failure['recovery_details']['stage'], 'identity')
        self.assertTrue(self.ops.is_active('A'))

    def test_result_record_failure_exposes_completed_result(self):
        original = operations.write_json
        def write(path, value):
            if path.name == 'result.json':
                raise OSError('fake result record failure')
            return original(path, value)
        with patch.object(operations, 'write_json', side_effect=write):
            _, failure = self.failure(lambda: self.change('set_mod_priority', mod='A', priority=1), OSError)
        self.assertTrue(failure['changes_may_have_occurred'])
        self.assertEqual(failure['phase'], 'result_record')
        self.assertEqual(failure['completed_result']['priority'], 1)
        self.assertEqual(failure['completed_result']['backup_id'], failure['backup_id'])
        self.assertEqual(self.organizer.ml.order, ['B', 'A'])

    def test_error_record_failure_does_not_mask_partial_operation(self):
        def partial(params, backup):
            self.organizer.ml.active.discard('A')
            raise RuntimeError('original operation failure')
        self.ops.set_mod_active = partial
        original = operations.write_json
        def write(path, value):
            if path.name == 'error.json':
                raise OSError('fake full disk')
            return original(path, value)
        with patch.object(operations, 'write_json', side_effect=write):
            error, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), RuntimeError)
        self.assertEqual(str(error), 'original operation failure')
        self.assertFalse(failure['error_recorded'])
        self.assertEqual(failure['error_record_error'], 'fake full disk')
        self.assertIn('backup_id', failure)
        self.assertFalse(self.ops.is_active('A'))

    def test_failed_observation_does_not_mask_original_failure(self):
        original_snapshot = self.ops.snapshot
        def partial(params, backup):
            self.organizer.ml.active.discard('A')
            self.ops.snapshot = lambda: (_ for _ in ()).throw(OSError('fake observation failure'))
            raise RuntimeError('original operation failure')
        self.ops.set_mod_active = partial
        error, failure = self.failure(lambda: self.change('set_mod_active', mod='A', active=False), RuntimeError)
        self.ops.snapshot = original_snapshot
        self.assertEqual(str(error), 'original operation failure')
        self.assertEqual(failure['observation_error'], 'fake observation failure')
        self.assertTrue(failure['changes_may_have_occurred'])

    def test_read_failure_does_not_claim_mutation_or_create_backup(self):
        self.ops.status = lambda params, backup: (_ for _ in ()).throw(ValueError('fake read failure'))
        _, failure = self.failure(lambda: self.ops.dispatch('status', {}), ValueError)
        self.assertFalse(failure['changes_may_have_occurred'])
        self.assertNotIn('backup_id', failure)
        self.assertFalse((self.root / 'data' / 'backups').exists())

    def test_snapshot_has_physical_recovery_identity(self):
        snapshot = self.ops.snapshot()
        self.assertEqual(snapshot['recovery_identity']['bridge_data_path'], str((self.root / 'data').resolve()))
        self.assertEqual(snapshot['recovery_identity']['game'], 'SkyrimSE')


if __name__ == '__main__':
    unittest.main(verbosity=2)
