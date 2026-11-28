"""Read-only feature dispatch while a tracked MO2 tool tree blocks writes."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_operation_fixes import OperationFixture, plugin_bytes
from test_command_wrapper import CommandObserver
from test_direct_tool_guard import DYNDOLOD, LODGEN
from test_nexus_downloads import FILE_URL, FakeNative, InlineExecutor
from mcp_server import dispatch as mcp_dispatch
from mo2_codex_bridge.nexus_downloads import NexusDownloads
from mo2_codex_bridge.running_apps import RunningApplications


class LaunchReadOnlyIntegrationTests(OperationFixture):
    def setUp(self):
        super().setUp()
        self.organizer.managedGame = lambda: SimpleNamespace(
            gameShortName=lambda: "SkyrimSE", gameNexusName=lambda: "skyrimspecialedition",
            primaryPlugins=lambda: ["Master.esm"], enabledPlugins=lambda: [],
        )
        self.organizer.appVersion = lambda: SimpleNamespace(displayString=lambda *_: "2.5.2")
        self.organizer.resolvePath = lambda name: str(
            self.root / "mods" / self.available[name]["origin"] / name)
        staged = self.organizer.createMod("Staged")
        (Path(staged.absolutePath()) / "Child.esp").write_bytes(plugin_bytes())

        self.archive_data = b"offline archive fixture"
        self.local_archive = self.root / "downloads" / "Fixture.7z"
        self.local_archive.write_bytes(self.archive_data)
        self.local_archive.with_suffix(".7z.meta").write_text(
            "[General]\nrepository=Nexus\ngameName=SkyrimSE\nmodID=91\nfileID=42\n"
            "paused=false\nremoved=false\n",
            encoding="utf-8",
        )

        self.native = FakeNative()
        self.catalog = Mock(side_effect=AssertionError("Local archive lookup contacted Nexus"))
        self.ops.nexus = NexusDownloads(self.organizer, self.ops.data, native=self.native,
                                        catalog=self.catalog, executor=InlineExecutor())
        self.addCleanup(self.ops.nexus.close)
        self.observer = CommandObserver()
        self.guard = RunningApplications(observer=self.observer, mo2_version="2.5.2")
        self.ops.running = self.guard
        self.client = SimpleNamespace(call=lambda action, params, timeout: self.ops.dispatch(action, params))

    def inspect(self, action, params):
        result = mcp_dispatch({"method": "tools/call", "params": {
            "name": "mo2_inspect", "arguments": {"action": action, "params": params},
        }}, self.client)
        self.assertFalse(result["isError"], result)
        return json.loads(result["content"][0]["text"])

    def check_reports(self):
        before = self.ops.snapshot()
        optional = self.inspect("preview_optional_plugin", {"plugin": "Child.esp"})
        self.assertEqual(optional["provider"]["reported_winner"], "A")
        self.assertEqual(optional["deactivation"]["status"], "no_known_plugin_list_blocker")

        forecast = self.inspect("preview_mod_replacement", {"mod": "A", "replacement": "Staged"})
        self.assertEqual(forecast["status"], "modeled", forecast["blockers"])
        self.assertEqual(forecast["revision"], before["revision"])
        self.assertEqual(forecast["plugins"][0]["after"]["origin"], "Staged")

        request = {"url": FILE_URL}
        self.assertEqual(self.inspect("nexus_find_archive", request)["status"], "checking_archive")
        archive = self.inspect("nexus_find_archive", request)
        self.assertEqual(archive["status"], "verified", archive)
        self.assertEqual(archive["file_id"], 42)
        self.assertEqual(archive["archive"]["path"], str(self.local_archive))
        self.assertEqual(archive["archive"]["sha256"], hashlib.sha256(self.archive_data).hexdigest())
        self.assertEqual(self.ops.snapshot(), before)

    def check_write_refusal(self):
        with self.assertRaisesRegex(ValueError, "Close the game") as error:
            self.install_change()
        failure = error.exception.bridge_failure
        self.assertEqual(failure["phase"], "preflight")
        self.assertFalse(failure["changes_may_have_occurred"])
        self.assertNotIn("backup_id", failure)

    def fixture_entries(self):
        return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob("*")}

    def test_three_read_actions_work_while_direct_tool_and_descendant_block_install(self):
        original = self.fixture_entries()
        idle_snapshot = self.ops.snapshot()
        self.guard.starting(DYNDOLOD)
        tool = self.observer.spawn(20, self.observer.owner_pid, DYNDOLOD)
        child = self.observer.spawn(21, tool.pid, LODGEN)

        with patch.object(self.ops, "refresh", side_effect=AssertionError("Read report refreshed MO2")):
            self.assertTrue(self.guard)
            self.assertEqual(self.guard.describe()["launches"][0]["tracking"], "direct_tool")
            self.check_reports()
            self.check_write_refusal()

            self.observer.exit(tool)
            self.guard.finished(DYNDOLOD, 0)
            self.assertTrue(self.guard)
            launch = self.guard.describe()["launches"][0]
            self.assertEqual(launch["completion_state"], "checking_survivors")
            self.assertEqual([row["pid"] for row in launch["completion_survivor_details"]], [child.pid])
            self.check_reports()
            self.check_write_refusal()

            self.observer.exit(child)
            self.assertFalse(self.guard)
            self.assertEqual(self.guard.describe()["launches"], [])
            self.check_reports()

        self.assertEqual(self.ops.snapshot(), idle_snapshot)
        self.assertEqual(self.fixture_entries(), original)
        self.assertFalse(self.ops.data.exists())
        self.assertEqual(self.install_calls, [])
        self.assertEqual(self.native.requests + self.native.queued, [])
        self.catalog.assert_not_called()
