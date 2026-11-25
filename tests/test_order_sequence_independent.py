"""Independent event-loop and native-callback recovery sequence checks."""
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_bridge
from mo2_codex_bridge.mod_persistence import ModListPersistence


class OrderRestoreSequenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture_parent = Path(__file__).resolve().parents[2] / 'independent-order-fixtures'
        self.fixture_parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='sequence-', dir=self.fixture_parent)
        self.root = Path(self.temporary.name)
        self.organizer = test_bridge.Organizer(self.root)
        self.ops = test_bridge.Operations(self.organizer, types.SimpleNamespace(
            ModState=test_bridge.ModState, PluginState=test_bridge.PluginState, GuessedString=str),
            self.root/'data', lambda:None, wait_until=lambda check,timeout:check())

    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to(self.fixture_parent.resolve()))
        self.temporary.cleanup()

    change = test_bridge.BridgeTests.change

    def test_running_tool_after_refresh_blocks_plugin_setters(self):
        source = self.change('set_mod_priority', mod='A', priority=1)
        self.ops.refresh = lambda:self.ops.running.add('tracked-game.exe')
        plugin_setter = Mock(wraps=self.organizer.pl.setLoadOrder)
        self.organizer.pl.setLoadOrder = plugin_setter
        rejection = None
        try:
            self.change('restore_order', backup_id=source['backup_id'])
        except (ValueError, RuntimeError) as error:
            rejection = error
        self.assertEqual(plugin_setter.call_count,0,'Plugin mutation continued after a tracked application started')
        self.assertIsNotNone(rejection)
        self.assertIn('tracked-game.exe',self.ops.running)

    def test_profile_switch_from_set_load_order_blocks_next_setter(self):
        source = self.change('set_mod_priority', mod='A', priority=1)
        real_order = self.organizer.pl.setLoadOrder
        real_state = self.organizer.pl.setState
        writes_after_switch = []
        def order_then_switch(order):
            real_order(order)
            self.organizer.profileName = lambda:'Other profile'
        def record_state(name,state):
            if self.organizer.profileName() == 'Other profile':
                writes_after_switch.append(name)
            return real_state(name,state)
        self.organizer.pl.setLoadOrder = order_then_switch
        self.organizer.pl.setState = record_state
        rejection = None
        try:
            self.change('restore_order', backup_id=source['backup_id'])
        except (ValueError,RuntimeError) as error:
            rejection = error
        self.assertEqual(writes_after_switch,[],'State setters ran after a native callback switched profile')
        self.assertIsNotNone(rejection)

    def test_modlist_changed_after_waiter_success_is_not_confirmed(self):
        disk = self.root/'profile/modlist.txt'
        def wait_then_external_change(check,timeout):
            self.assertTrue(check())
            disk.write_text('# newer file edit after predicate success\n-A\n-B\n',encoding='utf-8')
            return True
        verifier = ModListPersistence(self.organizer,wait_then_external_change)
        rejection = None
        try:
            verifier.verify(self.ops.snapshot()['mods'],lambda:None)
        except RuntimeError as error:
            rejection = error
        self.assertIsNotNone(rejection,'Saved modlist was confirmed without final disk readback')
        self.assertIn('newer file edit',disk.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
