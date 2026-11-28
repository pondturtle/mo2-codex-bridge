"""MO2 command wrapper lifecycle with Windows process-generation fixtures."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mo2_codex_bridge.operations import Operations
from mo2_codex_bridge.running_apps import RunningApplications
from test_skse_exit import FakeHandle, FakeObserver, OTHER

CMD = r"C:\Windows\System32\cmd.exe"
REQTIFICATOR = r"C:\fixture\Reqtificator.exe"


class CommandHandle(FakeHandle):
    def exit_code(self):
        if self.closed or self.instance.ended is None:
            raise OSError("No terminal exit on the pinned process")
        return getattr(self.instance, "exit_code", 0)


class CommandObserver(FakeObserver):
    def pin(self, pid):
        handle = super().pin(pid)
        command_handle = CommandHandle(handle.instance, self)
        self.handles[-1] = command_handle
        return command_handle


class CommandWrapperTests(unittest.TestCase):
    def setUp(self):
        self.observer = CommandObserver()
        self.guard = RunningApplications(observer=self.observer, mo2_version="2.5.2")
        self.desktop = self.observer.spawn(80, 0, OTHER)
        self.unrelated = [self.observer.spawn(pid, self.desktop.pid, CMD) for pid in range(81, 86)]
        self.unrelated_child = self.observer.spawn(89, self.unrelated[0].pid, REQTIFICATOR)

    def launch(self):
        self.guard.starting(CMD)
        root = self.observer.spawn(20, self.observer.owner_pid, CMD)
        child = self.observer.spawn(21, root.pid, REQTIFICATOR)
        self.assertTrue(self.guard)
        return root, child

    def test_verified_wrapper_releases_write_with_five_baseline_and_one_new_external_prompt(self):
        root, child = self.launch()
        new_prompt = self.observer.spawn(86, self.desktop.pid, CMD)
        self.observer.spawn(87, new_prompt.pid, REQTIFICATOR)

        with tempfile.TemporaryDirectory(prefix="offline-command-guard-") as folder:
            data = Path(folder) / "bridge"
            ops = Operations(None, None, data, None)
            ops.running = self.guard
            ops.snapshot = Mock(return_value={"profile": "Fixture", "revision": "exact"})
            ops.install_archive = Mock(return_value={"simulated": True})
            params = {"expected_profile": "Fixture", "expected_revision": "exact"}
            with self.assertRaisesRegex(ValueError, "Close the game") as refusal:
                ops.dispatch("install_archive", params)
            self.assertEqual(refusal.exception.bridge_failure["phase"], "preflight")
            self.assertFalse(refusal.exception.bridge_failure["changes_may_have_occurred"])
            self.assertFalse(data.exists())
            ops.install_archive.assert_not_called()

            self.observer.exit(child)
            self.observer.exit(root)
            self.guard.finished(CMD, 0)
            self.assertFalse(self.guard, "The verified wrapper must clear with unrelated prompts still open")
            self.assertEqual(self.guard.describe()["launches"], [])
            self.assertIsNone(new_prompt.ended)
            self.assertIsNone(self.unrelated_child.ended)
            self.assertTrue(all(prompt.ended is None for prompt in self.unrelated))
            self.assertTrue(ops.dispatch("install_archive", params)["simulated"])
            ops.install_archive.assert_called_once()
        self.assertEqual([p.pid for p in self.observer.handles], [root.pid])
        self.assertTrue(self.observer.handles[0].closed)

    def test_observed_req_child_and_nested_shell_still_block_after_wrapper_exit(self):
        root, child = self.launch()
        nested = self.observer.spawn(50, child.pid, CMD)
        self.assertTrue(self.guard)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        waiting = self.guard.describe()["launches"][0]
        self.assertEqual(waiting["pending_attempts"], 0)
        self.assertEqual(waiting["instances"], [])
        self.assertEqual(waiting["completion_state"], "checking_survivors")
        self.assertEqual(waiting["completion_survivor_count"], 2)
        self.observer.exit(child)
        self.assertTrue(self.guard)
        self.observer.exit(nested)
        self.assertFalse(self.guard)

    def test_late_direct_descendant_first_seen_at_finish_still_blocks(self):
        root, child = self.launch()
        self.observer.exit(child)
        late = self.observer.spawn(55, root.pid, OTHER)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        self.assertEqual(self.guard.describe()["launches"][0]["completion_survivor_count"], 1)
        self.observer.exit(late)
        self.assertFalse(self.guard)

    def test_orphan_under_unseen_intermediary_preserves_uncertainty(self):
        root, tool = self.launch()
        intermediary = self.observer.spawn(55, tool.pid, OTHER)
        survivor = self.observer.spawn(56, intermediary.pid, OTHER)
        self.observer.exit(intermediary)
        self.observer.exit(tool)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        observed = self.guard.describe()["launches"][0]["completion_survivor_details"]
        self.assertEqual(observed[0]["pid"], survivor.pid)
        self.assertIn("no observed parent", observed[0]["reason"])
        child = self.observer.spawn(57, survivor.pid, OTHER)
        self.assertTrue(self.guard)
        self.observer.exit(survivor)
        self.assertTrue(self.guard)
        self.observer.exit(child)
        self.assertFalse(self.guard)

    def test_preexisting_orphan_does_not_hold_verified_wrapper(self):
        orphan = self.observer.spawn(70, 999, OTHER)
        root, tool = self.launch()
        self.observer.exit(tool)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertIsNone(orphan.ended)
        self.assertFalse(self.guard)

    def test_snapshot_loss_after_match_never_auto_clears(self):
        root, child = self.launch()
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.observer.identities_error = OSError("Unavailable fixture information")
        self.assertTrue(self.guard)
        self.observer.identities_error = None
        self.observer.exit(child)
        self.assertTrue(self.guard)
        self.assertEqual(self.guard.describe()["launches"][0]["completion_state"], "restart_required")

    def test_unobserved_wrapper_not_replaced_by_late_unrelated_prompt(self):
        self.guard.starting(CMD)
        external = self.observer.spawn(22, self.desktop.pid, CMD)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        record = self.guard.describe()["launches"][0]
        self.assertEqual(record["instances"], [])
        self.assertFalse(record["observed_after_launch"])
        self.assertIn("not pinned", record["finish_uncertainty"])
        self.assertNotIn(external.pid, [handle.pid for handle in self.observer.handles])

    def test_early_zero_finish_fails_even_if_wrapper_exits_while_scanning(self):
        root, child = self.launch()
        original = self.observer.identities

        def exit_during_scan():
            if root.ended is None:
                self.observer.exit(child)
                self.observer.exit(root)
            return original()

        self.observer.identities = exit_during_scan
        self.guard.finished(CMD, 0)
        if root.ended is None:
            self.observer.exit(child)
            self.observer.exit(root)
        self.assertTrue(self.guard)
        self.assertIn("before", self.guard.describe()["launches"][0]["finish_uncertainty"])

    def test_exit_code_mismatch_is_unresolved(self):
        root, child = self.launch()
        root.exit_code = 7
        self.observer.exit(child)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        self.assertIn("exit code", self.guard.describe()["launches"][0]["finish_uncertainty"])

    def test_matching_nonzero_terminal_exit_can_close_proved_tree(self):
        root, child = self.launch()
        root.exit_code = 7
        self.observer.exit(child)
        self.observer.exit(root)
        self.guard.finished(CMD, 7)
        self.assertFalse(self.guard)

    def test_two_matching_direct_roots_are_ambiguous(self):
        root, child = self.launch()
        second = self.observer.spawn(22, self.observer.owner_pid, CMD)
        self.assertTrue(self.guard)
        self.observer.exit(child)
        self.observer.exit(root)
        self.observer.exit(second)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        self.assertIn("Multiple exact command", self.guard.describe()["launches"][0]["finish_uncertainty"])

    def test_duplicated_start_cannot_be_cleared_by_matching_later_exits(self):
        root, child = self.launch()
        self.guard.starting(CMD)
        self.observer.exit(child)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.guard.finished(CMD, 0)
        self.assertTrue(self.guard)
        state = self.guard.describe()["launches"][0]
        self.assertEqual(state["pending_attempts"], 0)
        self.assertIn("earlier one was unresolved", state["finish_uncertainty"])

    def test_reused_child_pid_outside_launch_does_not_hold_guard(self):
        root, child = self.launch()
        self.observer.exit(child)
        self.observer.spawn(child.pid, self.desktop.pid, OTHER)
        self.observer.exit(root)
        self.guard.finished(CMD, 0)
        self.assertFalse(self.guard)

    def test_new_launch_after_proved_completion_collects_its_own_root(self):
        for _ in range(2):
            root, child = self.launch()
            self.observer.exit(child)
            self.observer.exit(root)
            self.guard.finished(CMD, 0)
            self.assertFalse(self.guard)
        self.assertEqual(len(self.observer.handles), 2)
        self.assertTrue(all(handle.close_calls == 1 for handle in self.observer.handles))


if __name__ == "__main__":
    unittest.main()

