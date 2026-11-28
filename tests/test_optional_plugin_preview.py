"""Offline optional-plugin impact and connector tests."""
import json
import tempfile
import unittest
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import mcp_server
from mo2_codex_bridge.operations import Operations


class PluginState(Enum):
    ACTIVE = 1
    INACTIVE = 0
    MISSING = 3


class OptionalPluginTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.data = Path(work.name) / "bridge"
        self.plugin = "Extra.esp"
        self.master = "Base.esm"
        self.live_child = "Live Child.esp"
        self.disabled_child = "Disabled Child.esp"
        self.names = [self.plugin, self.master, self.live_child, self.disabled_child]
        self.states = {name: PluginState.ACTIVE for name in self.names}
        self.states[self.master] = self.states[self.disabled_child] = PluginState.INACTIVE
        self.masters = {self.plugin: ["base.ESM", "Missing.esl"], self.master: [],
                        self.live_child: ["EXTRA.ESP"], self.disabled_child: [self.plugin]}
        self.plugins = Mock(spec=["pluginNames", "origin", "state", "masters"])
        self.plugins.pluginNames.side_effect = lambda: list(self.names)
        self.plugins.origin.return_value = "Main Mod"
        self.plugins.state.side_effect = self.states.__getitem__
        self.plugins.masters.side_effect = self.masters.__getitem__
        self.game = Mock(spec=["primaryPlugins", "enabledPlugins"])
        self.game.enabledPlugins.return_value = []
        self.game.primaryPlugins.return_value = ["Game.esm"]
        self.organizer = Mock(spec=["profileName", "profilePath", "pluginList", "managedGame",
                                    "resolvePath", "getFileOrigins"])
        self.organizer.profileName.return_value = "Fixture"
        self.organizer.profilePath.return_value = str(Path(work.name) / "profiles/Fixture")
        self.organizer.pluginList.return_value = self.plugins
        self.organizer.managedGame.return_value = self.game
        self.organizer.resolvePath.return_value = str(Path(work.name) / "mods/Main Mod" / self.plugin)
        self.organizer.getFileOrigins.return_value = ["Main Mod", "Older Variant"]
        self.ops = Operations(self.organizer, SimpleNamespace(PluginState=PluginState),
                              self.data, Mock(side_effect=AssertionError("Read invoked a refresh")))

    def preview(self):
        return self.ops.dispatch("preview_optional_plugin", {"plugin": self.plugin})

    def test_exact_provider_alternatives_and_blocking_dependents_through_mcp(self):
        client = Mock()
        client.call.side_effect = lambda action, params, timeout: self.ops.dispatch(action, params)
        self.ops.running = {"SkyrimSE.exe"}
        request = {"method": "tools/call", "params": {"name": "mo2_inspect", "arguments": {
            "action": "preview_optional_plugin", "params": {"plugin": self.plugin}}}}
        response = mcp_server.dispatch(request, client)
        self.assertFalse(response["isError"])
        row = json.loads(response["content"][0]["text"])
        self.assertEqual(row["provider"]["reported_winner"], "Main Mod")
        self.assertEqual(row["provider"]["same_name_alternatives"], ["Older Variant"])
        self.assertEqual(row["active_dependents"], [self.live_child])
        self.assertEqual([master["state"] for master in row["masters"]], ["inactive", "absent"])
        self.assertTrue(row["active_dependent_evidence_complete"])
        self.assertEqual(row["deactivation"], {"status": "blocked", "blockers": ["active_dependents"]})
        self.assertFalse(self.data.exists())
        self.ops.refresh.assert_not_called()
        request["params"]["name"] = "mo2_change"
        with self.assertRaises(mcp_server.RPCError):
            mcp_server.dispatch(request, client)

    def test_primary_plugin_retains_block_even_with_no_active_dependents(self):
        self.states[self.live_child] = PluginState.INACTIVE
        self.game.primaryPlugins.return_value = [self.plugin.upper()]
        self.assertEqual(self.preview()["deactivation"]["blockers"], ["primary_game_plugin"])

    def test_nonprimary_game_enabled_plugin_is_blocked(self):
        self.states[self.live_child] = PluginState.INACTIVE
        self.game.enabledPlugins.return_value = [self.plugin.upper()]
        row = self.preview()
        self.assertTrue(row["force_enabled_by_game"])
        self.assertFalse(row["primary_game_plugin"])
        self.assertEqual(row["deactivation"]["blockers"], ["force_enabled_by_game"])

    def test_missing_game_enabled_evidence_is_unknown(self):
        self.states[self.live_child] = PluginState.INACTIVE
        self.game.enabledPlugins.side_effect = OSError()
        self.assertEqual(self.preview()["deactivation"]["status"], "unknown")

    def test_current_inactive_and_no_known_blocker_are_distinct(self):
        self.states[self.live_child] = PluginState.INACTIVE
        self.assertEqual(self.preview()["deactivation"]["status"], "no_known_plugin_list_blocker")
        self.states[self.plugin] = PluginState.INACTIVE
        self.assertEqual(self.preview()["deactivation"]["status"], "already_inactive")

    def test_unknown_active_dependent_prevents_clear_assessment(self):
        self.states[self.live_child] = PluginState.MISSING
        row = self.preview()
        self.assertFalse(row["active_dependent_evidence_complete"])
        self.assertEqual(row["active_dependents"], [])
        self.assertEqual(row["deactivation"]["status"], "unknown")

    def test_unknown_master_result_does_not_guess_about_dependents(self):
        del self.masters[self.live_child]
        row = self.preview()
        self.assertFalse(row["active_dependent_evidence_complete"])
        self.assertEqual(row["deactivation"]["status"], "unknown")

    def test_ambiguous_origin_is_reported_as_unknown(self):
        self.organizer.getFileOrigins.return_value = ["Older Variant", "Main Mod"]
        row = self.preview()["provider"]
        self.assertEqual(row["status"], "unknown")
        self.assertEqual(row["reported_winner"], "Older Variant")
        self.assertIsNone(row["same_name_alternatives"])

    def test_duplicate_origin_names_do_not_establish_a_winner(self):
        self.organizer.getFileOrigins.return_value = ["Main Mod", "main mod"]
        self.assertEqual(self.preview()["provider"]["status"], "unknown")

    def test_absent_resolved_path_does_not_call_native_origins(self):
        self.organizer.resolvePath.return_value = ""
        self.assertEqual(self.preview()["provider"]["status"], "unknown")
        self.organizer.getFileOrigins.assert_not_called()

    def test_invalid_hidden_unknown_or_wrong_case_target_refused(self):
        for name in ("../Extra.esp", "Extra.esp.mohidden", "extra.esp", "Absent.esp", "CON.esp", None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.ops.dispatch("preview_optional_plugin", {"plugin": name})
        self.organizer.resolvePath.assert_not_called()

    def test_case_alias_inventory_rejected(self):
        self.names.append(self.plugin.upper())
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.preview()

    def test_profile_switch_discards_evidence(self):
        self.organizer.profileName.side_effect = ["Fixture", "Changed"]
        with self.assertRaisesRegex(ValueError, "profile changed"):
            self.preview()


if __name__ == "__main__":
    unittest.main()

