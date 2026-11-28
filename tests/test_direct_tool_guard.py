"""Launch and completion evidence for tools run directly by MO2 2.5.2."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from mo2_codex_bridge.operations import Operations
from mo2_codex_bridge.running_apps import RunningApplications, _ProcessSequencesUnsupported
from test_command_wrapper import CommandObserver
from test_skse_exit import OTHER


DYNDOLOD = r"C:\fixture\DynDOLODx64.exe"
LODGEN = r"C:\fixture\Edit Scripts\LODGenx64.exe"
TEXGEN = r"C:\fixture\TexGenx64.exe"
TEXCONV = r"C:\fixture\Edit Scripts\Texconvx64.exe"
SSEEDIT = r"C:\fixture\SSEEdit.exe"
MO2 = r"C:\fixture\ModOrganizer.exe"


class DirectToolGuardTests(unittest.TestCase):
    def setUp(self):
        self.observer = CommandObserver()
        self.guard = RunningApplications(observer=self.observer, mo2_version="2.5.2")
        self.desktop = self.observer.spawn(80, 0, OTHER)

    def launch(self, executable, pid=20):
        self.guard.starting(executable)
        root = self.observer.spawn(pid, self.observer.owner_pid, executable)
        self.assertTrue(self.guard)
        self.assertEqual(self.records()[executable]["tracking"], "direct_tool")
        return root

    def records(self):
        return {record["executable"]: record for record in self.guard.describe()["launches"]}

    def test_dyndolod_and_texgen_release_after_their_workers_exit(self):
        for executable, worker in ((DYNDOLOD, LODGEN), (TEXGEN, TEXCONV)):
            with self.subTest(executable=executable):
                old = self.observer.spawn(91, self.observer.owner_pid, executable)
                old_child = self.observer.spawn(92, old.pid, worker)
                root = self.launch(executable)
                related = self.observer.spawn(21, root.pid, worker)
                late_unrelated = self.observer.spawn(93, self.desktop.pid, executable)
                self.assertTrue(self.guard)
                self.assertEqual([item["pid"] for item in self.records()[executable]["instances"]],
                                 [root.pid])

                self.observer.exit(root)
                self.guard.finished(executable, 0)
                state = self.records()[executable]
                self.assertEqual(state["completion_state"], "checking_survivors")
                self.assertEqual([item["pid"] for item in state["completion_survivor_details"]],
                                 [related.pid])
                self.assertTrue(self.guard)
                self.observer.exit(related)
                self.assertFalse(self.guard)
                self.assertEqual(self.records(), {})
                for unrelated in (old, old_child, late_unrelated):
                    self.assertIsNone(unrelated.ended)
                    self.observer.exit(unrelated)
        self.assertEqual([handle.close_calls for handle in self.observer.handles], [1, 1])

    def test_concurrent_direct_tools_track_separate_worker_trees(self):
        first = self.launch(DYNDOLOD, 20)
        second = self.launch(TEXGEN, 30)
        first_worker = self.observer.spawn(21, first.pid, LODGEN)
        second_worker = self.observer.spawn(31, second.pid, TEXCONV)
        self.assertTrue(self.guard)
        grandchild = self.observer.spawn(22, first_worker.pid, OTHER)
        self.assertTrue(self.guard)
        self.observer.exit(first_worker)
        self.observer.exit(first)
        self.guard.finished(DYNDOLOD, 0)
        state = self.records()[DYNDOLOD]
        self.assertEqual([item["pid"] for item in state["completion_survivor_details"]],
                         [grandchild.pid])

        self.observer.exit(grandchild)
        self.assertTrue(self.guard)
        self.assertEqual(list(self.records()), [TEXGEN])
        self.observer.exit(second)
        self.guard.finished(TEXGEN, 0)
        self.assertTrue(self.guard)
        state = self.records()[TEXGEN]
        self.assertEqual([item["pid"] for item in state["completion_survivor_details"]],
                         [second_worker.pid])
        self.observer.exit(second_worker)
        self.assertFalse(self.guard)

    def test_write_preflight_waits_for_the_last_linked_worker(self):
        root = self.launch(DYNDOLOD)
        child = self.observer.spawn(21, root.pid, LODGEN)
        self.assertTrue(self.guard)
        self.observer.exit(root)
        self.guard.finished(DYNDOLOD, 0)
        with tempfile.TemporaryDirectory(prefix="offline-direct-tool-guard-") as folder:
            data = Path(folder) / "bridge"
            operations = Operations(None, None, data, None)
            operations.running = self.guard
            operations.snapshot = Mock(return_value={"profile": "Fixture", "revision": "exact"})
            operations.install_archive = Mock(return_value={"simulated": True})
            params = {"expected_profile": "Fixture", "expected_revision": "exact"}
            with self.assertRaisesRegex(ValueError, "Close the game") as refusal:
                operations.dispatch("install_archive", params)
            self.assertEqual(refusal.exception.bridge_failure["phase"], "preflight")
            self.assertFalse(refusal.exception.bridge_failure["changes_may_have_occurred"])
            self.assertFalse(data.exists())
            operations.install_archive.assert_not_called()

            self.observer.exit(child)
            self.assertTrue(operations.dispatch("install_archive", params)["simulated"])
            operations.install_archive.assert_called_once()

    def test_ambiguous_same_path_roots_keep_guard_after_both_exit(self):
        root = self.launch(SSEEDIT)
        rival = self.observer.spawn(21, self.observer.owner_pid, SSEEDIT)
        self.assertTrue(self.guard)
        self.observer.exit(root)
        self.observer.exit(rival)
        self.guard.finished(SSEEDIT, 0)
        self.assertTrue(self.guard)
        self.assertEqual(self.records()[SSEEDIT]["completion_state"], "restart_required")
        self.assertTrue(all(handle.close_calls == 1 for handle in self.observer.handles))

    def test_early_success_callback_does_not_clear_after_the_actual_exit(self):
        root = self.launch(SSEEDIT)
        self.guard.finished(SSEEDIT, 0)
        self.assertTrue(self.guard)
        self.observer.exit(root)
        state = self.records()[SSEEDIT]
        self.assertEqual(state["completion_state"], "restart_required")
        self.assertFalse(state["callback_exit_evidence"])
        self.assertTrue(self.guard)

    def test_unseen_short_lived_root_is_not_replaced_by_an_external_exact_path(self):
        self.guard.starting(SSEEDIT)
        short_lived = self.observer.spawn(20, self.observer.owner_pid, SSEEDIT)
        self.observer.exit(short_lived)
        outsider = self.observer.spawn(21, self.desktop.pid, SSEEDIT)
        self.guard.finished(SSEEDIT, 0)
        state = self.records()[SSEEDIT]
        self.assertEqual(state["completion_state"], "restart_required")
        self.assertFalse(state["observed_after_launch"])
        self.assertEqual(state["instances"], [])
        self.assertNotIn(outsider.pid, [handle.pid for handle in self.observer.handles])
        self.assertTrue(self.guard)

    def test_callback_with_same_basename_but_different_path_cannot_verify_exit(self):
        root = self.launch(SSEEDIT)
        self.observer.exit(root)
        self.guard.finished(r"C:\different-fixture\SSEEdit.exe", 0)
        state = self.records()[SSEEDIT]
        self.assertEqual(state["pending_attempts"], 1)
        self.assertFalse(state["callback_exit_evidence"])
        self.assertTrue(self.guard)
        self.guard.finished(SSEEDIT, 0)
        self.assertFalse(self.guard)

    def test_virtualized_callback_path_does_not_verify_original_tool(self):
        requested = r"C:\fixture\mods\Example Tool\SSEEdit.exe"
        effective = r"C:\fixture\Skyrim\Data\SSEEdit.exe"
        self.guard.starting(requested)
        intermediary = self.observer.spawn(20, self.observer.owner_pid, MO2)
        tool = self.observer.spawn(21, intermediary.pid, effective)
        self.assertTrue(self.guard)
        self.observer.exit(tool)
        self.observer.exit(intermediary)
        # MO2 2.5.2 emits the original path before adjustForVirtualized and
        # the replacement executable's path at finish.
        self.guard.finished(MO2, 0)
        state = self.records()[requested]
        self.assertEqual(state["pending_attempts"], 1)
        self.assertFalse(state["observed_after_launch"])
        self.assertFalse(state.get("callback_exit_evidence"))
        self.assertTrue(self.guard)

    def test_other_mo2_versions_keep_the_legacy_child_guard(self):
        self.guard = RunningApplications(observer=self.observer, mo2_version="2.5.1")
        self.guard.starting(DYNDOLOD)
        root = self.observer.spawn(20, self.observer.owner_pid, DYNDOLOD)
        worker = self.observer.spawn(21, root.pid, LODGEN)
        self.assertTrue(self.guard)
        self.observer.exit(worker)
        self.observer.exit(root)
        self.guard.finished(DYNDOLOD, 0)
        self.assertTrue(self.guard)
        self.assertTrue(self.records()[DYNDOLOD]["tree_uncertainty"])
        self.assertEqual(self.observer.handles, [])

    def test_unsupported_process_sequences_at_start_use_legacy_observation(self):
        self.observer.identities_error = _ProcessSequencesUnsupported("Unsupported test kernel")
        self.guard.starting(SSEEDIT)
        root = self.observer.spawn(20, self.observer.owner_pid, SSEEDIT)
        self.assertTrue(self.guard)
        self.assertEqual(self.records()[SSEEDIT]["instances"][0]["pid"], root.pid)
        self.assertIn("legacy", self.observer.calls)
        self.observer.exit(root)
        self.guard.finished(SSEEDIT, 0)
        self.assertFalse(self.guard)
        self.assertEqual(self.observer.handles, [])

    def test_unsupported_process_sequences_after_start_cannot_downgrade_proof(self):
        root = self.launch(SSEEDIT)
        self.observer.identities_error = _ProcessSequencesUnsupported("Lost test process sequences")
        self.assertTrue(self.guard)
        self.observer.identities_error = None
        self.observer.exit(root)
        self.guard.finished(SSEEDIT, 0)
        self.assertTrue(self.guard)
        self.assertEqual(self.records()[SSEEDIT]["completion_state"], "restart_required")
        self.assertNotIn("legacy", self.observer.calls)
        self.assertEqual(self.observer.handles[0].close_calls, 1)


if __name__ == "__main__":
    unittest.main()
