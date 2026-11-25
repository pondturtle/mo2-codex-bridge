"""Independent final-persistence regression; all files are guarded fake profiles."""
import unittest

import test_bridge


class FinalOrderPersistenceTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    change = test_bridge.BridgeTests.change

    def test_plugin_disk_change_during_later_modlist_wait_is_not_success(self):
        previous = self.change('set_plugin_active', plugin='Child.esp', active=False)
        waits = []
        def wait(check, timeout):
            waits.append(len(waits) + 1)
            if len(waits) == 2:
                # Native plugin save already completed. A later Qt event modifies
                # disk state while the modlist verifier is still processing work.
                (self.root / 'profile' / 'plugins.txt').write_text('# External edit\nChild.esp\n', encoding='utf-8')
            return check()
        self.ops.wait_until = wait
        with self.assertRaises((RuntimeError, ValueError),
                               msg='Restore reported verified success after its plugin file changed during the later modlist wait'):
            self.change('restore_order', backup_id=previous['backup_id'])
        self.assertEqual(waits, [1, 2])
        self.assertIn('Child.esp', self.organizer.pl.active)
        self.assertNotIn('*Child.esp', (self.root / 'profile' / 'plugins.txt').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
