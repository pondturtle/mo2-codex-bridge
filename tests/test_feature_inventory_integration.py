"""Read-only inspection contracts shared by the MCP, native dispatch and setup."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mcp_server
import setup
from client import MUTATING_ACTIONS
import test_client_recovery as existing
from mo2_codex_bridge import operations

ACTIONS = {"native_mod_backups", "mod_profile_usage", "preview_optional_plugin",
           "nexus_find_archive", "preview_mod_replacement"}


class FeatureInspectionTests(unittest.TestCase):
    def test_capability_inventory_matches_read_only_mcp_schema(self):
        schema = mcp_server.dispatch({"method": "tools/list"}, None)["tools"]
        inspect = next(item for item in schema if item["name"] == "mo2_inspect")
        change = next(item for item in schema if item["name"] == "mo2_change")
        read = set(inspect["inputSchema"]["properties"]["action"]["enum"])
        write = set(change["inputSchema"]["properties"]["action"]["enum"])
        self.assertTrue(ACTIONS <= read)
        self.assertTrue(ACTIONS.isdisjoint(write))
        self.assertTrue(ACTIONS.isdisjoint(operations.WRITE_ACTIONS))
        self.assertTrue(ACTIONS.isdisjoint(MUTATING_ACTIONS))
        self.assertEqual(read - {"installer_state"}, operations.READ_ACTIONS)
        self.assertTrue(inspect["annotations"]["readOnlyHint"])

    def test_inspection_routes_through_dispatch_even_while_game_is_running(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            organizer = Mock()
            refresh = Mock(side_effect=AssertionError("Inspection must not refresh"))
            select = Mock(side_effect=AssertionError("Inspection must not switch profiles"))
            bridge = operations.Operations(organizer, None, data, refresh, select)
            bridge.running = {"SkyrimSE.exe"}
            bridge.snapshot = Mock(side_effect=AssertionError("Inspection must not require a mutation snapshot"))

            client = Mock()
            client.call.side_effect = lambda action, params, timeout: bridge.dispatch(action, params)
            with (
                patch.object(operations, "browse_native_mod_backups", return_value={"backups": []}) as backups,
                patch.object(operations, "report_mod_profile_usage", return_value={"profiles": []}) as usage,
                patch.object(operations, "write_json", side_effect=AssertionError("Inspection must not write recovery files")),
            ):
                for action, params, result in (
                    ("native_mod_backups", {}, {"backups": []}),
                    ("mod_profile_usage", {"mod": "Fixture"}, {"profiles": []}),
                ):
                    with self.subTest(action=action):
                        response = mcp_server.dispatch({"method": "tools/call", "params": {
                            "name": "mo2_inspect", "arguments": {"action": action, "params": params}}}, client)
                        self.assertFalse(response["isError"])
                        self.assertEqual(json.loads(response["content"][0]["text"]), result)
                backups.assert_called_once_with(organizer, {})
                usage.assert_called_once_with(bridge, {"mod": "Fixture"})
            self.assertEqual(list(data.iterdir()), [])
            refresh.assert_not_called()
            select.assert_not_called()
            bridge.snapshot.assert_not_called()
            self.assertFalse(organizer.method_calls)

    def test_new_inspections_are_refused_by_mutation_tool(self):
        client = Mock()
        for action in ACTIONS:
            with self.subTest(action=action), self.assertRaises(mcp_server.RPCError):
                mcp_server.dispatch({"method": "tools/call", "params": {"name": "mo2_change",
                    "arguments": {"action": action, "params": {"expected_profile": "Fixture",
                                                             "expected_revision": "current"}}}},
                    client)
        client.call.assert_not_called()

    def test_failed_inspection_is_reported_without_recovery_or_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            bridge = operations.Operations(None, None, data, None)
            client = Mock()
            client.call.side_effect = lambda action, params, timeout: bridge.dispatch(action, params)
            with patch.object(operations, "report_mod_profile_usage", side_effect=ValueError("Unknown fixture mod")):
                response = mcp_server.dispatch({"method": "tools/call", "params": {
                    "name": "mo2_inspect", "arguments": {"action": "mod_profile_usage", "params": {"mod": "Fixture"}}}},
                    client)
            self.assertTrue(response["isError"])
            failure = json.loads(response["content"][0]["text"])
            self.assertFalse(failure["changes_may_have_occurred"])
            self.assertNotIn("backup_id", failure)
            self.assertEqual(list(data.iterdir()), [])

    def test_component_setup_includes_all_inspection_modules(self):
        candidate = Path(__file__).resolve().parents[1]
        payload = setup.source_payload(candidate)
        for name in ("native_backups.py", "profile_usage.py",
                     "optional_plugin_preview.py", "replacement_preview.py"):
            self.assertIn(name, setup.REQUIRED)
            self.assertEqual(payload[name], (candidate / "mo2_codex_bridge" / name).read_bytes())


class FeatureReadQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        existing.PluginRecoveryTests.setUpClass.__func__(cls)
        cls.plugin_type.tick.__globals__["WRITE_ACTIONS"] = operations.WRITE_ACTIONS

    setUp = existing.PluginRecoveryTests.setUp
    tearDown = existing.PluginRecoveryTests.tearDown

    def test_queue_uses_read_receipts_and_only_normal_audit(self):
        owner = self.owner
        owner.organizer.pluginSetting = Mock(side_effect=AssertionError("Read consulted write permission"))
        owner.ops = operations.Operations(None, None, owner.data, None)
        owner.ops.running = {"SkyrimSE.exe"}
        owner.ops.nexus = Mock()
        owner.ops.nexus.find_archive.return_value = {"status": "checking_archive"}
        with (
            patch.object(operations, "browse_native_mod_backups", return_value={"backups": []}),
            patch.object(operations, "report_mod_profile_usage", return_value={"profiles": []}),
            patch.object(operations, "preview_optional_plugin", return_value={"plugin": "Fixture.esp"}),
            patch.object(operations, "preview_mod_replacement", return_value={"status": "modeled"}),
        ):
            for action in sorted(ACTIONS):
                ident = action.replace("_", "-")
                params = {
                    "mod_profile_usage": {"mod": "Example Mod"},
                    "preview_optional_plugin": {"plugin": "Fixture.esp"},
                    "preview_mod_replacement": {"mod": "Example Mod", "replacement": "Staged"},
                    "nexus_find_archive": {"url": "https://www.nexusmods.com/skyrimspecialedition/mods/1?file_id=2"},
                }.get(action, {})
                self.assertEqual(owner.enqueue({"action": action, "params": params,
                                                "request_id": ident})["status"], "queued")
                self.assertFalse(owner.jobs[ident]["_mutation"])
                owner.tick()
                self.assertEqual(owner.get_job(ident)["status"], "done")
        self.assertEqual([p.name for p in owner.data.iterdir()], ["audit.jsonl"])
        audit = [json.loads(line) for line in (owner.data / "audit.jsonl").read_text().splitlines()]
        self.assertEqual({row["action"] for row in audit}, ACTIONS)
        self.assertTrue(all(row["status"] == "done" and row["backup_id"] is None for row in audit))
