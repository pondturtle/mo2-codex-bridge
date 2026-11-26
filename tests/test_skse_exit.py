"""Independent deterministic SKSE lifecycle acceptance; no live process APIs.

The observer below is a fictional process table. Tests never instantiate the
Windows observer, contact MO2, launch a process, or read a game/mod/profile file.
"""
import ntpath
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mo2_codex_bridge.running_apps import RunningApplications

GAME_DIR = r"C:\offline-lifecycle-fixture\Skyrim Special Edition"
LOADER = GAME_DIR + r"\skse64_loader.exe"
GAME = GAME_DIR + r"\SkyrimSE.exe"
OTHER = r"C:\unrelated-fixture\worker.exe"
TICKS = 134400000000000000


def key(path):
    return ntpath.normcase(ntpath.normpath(path))


class FakeGame:
    def __init__(self, directory=GAME_DIR, short_name="SkyrimSE"):
        self.directory, self.short_name = directory, short_name

    def gameShortName(self):
        return self.short_name

    def gameDirectory(self):
        return self

    def absolutePath(self):
        return self.directory


class FakeInstance:
    def __init__(self, pid, ppid, executable, created):
        self.pid, self.ppid = pid, ppid
        self.executable, self.created = executable, created
        self.ended = None
        self.sequence = 0
        self.exit_error = None


class FakeHandle:
    def __init__(self, instance, observer):
        self.instance, self.observer = instance, observer
        self.pid = instance.pid
        self.executable = instance.executable
        self.created = instance.created
        self.closed = False
        self.close_calls = 0

    def exit_time(self):
        if self.closed:
            raise RuntimeError("An owned handle was queried after close")
        if self.instance.exit_error:
            raise self.instance.exit_error
        return self.instance.ended

    def close(self):
        self.close_calls += 1
        if self.closed:
            raise RuntimeError("Owned handle closed more than once")
        self.closed = True


class FakeObserver:
    def __init__(self):
        self.now = TICKS
        self.owner_pid = 1
        self.live = {1: FakeInstance(1, 0, r"C:\offline-lifecycle-fixture\host.exe", TICKS - 10000)}
        self.handles = []
        self.retained = {}
        self.live[1].sequence = 1
        self.next_sequence = 2
        self.identities_error = None
        self.snapshot_error = None
        self.clock_error = None
        self.pin_errors = {}
        self.snapshot_override = None
        self.legacy_override = None
        self.calls = []

    def clock(self):
        self.calls.append("clock")
        if self.clock_error:
            raise self.clock_error
        return self.now

    def snapshot(self):
        self.calls.append("snapshot")
        if self.snapshot_error:
            raise self.snapshot_error
        if self.snapshot_override is not None:
            return self.snapshot_override
        return {pid: (p.ppid, ntpath.basename(p.executable))
                for pid, p in self.live.items() if p.ended is None}

    def identities(self):
        self.calls.append("identities")
        if self.identities_error:
            raise self.identities_error
        return {pid: (p.ppid, p.sequence) for pid, p in self.live.items() if p.ended is None and pid > 0}

    def pin_exited(self, pid, expected_path):
        return self.pin(pid)

    def pin(self, pid):
        self.calls.append(("pin", pid))
        if pid in self.pin_errors:
            raise self.pin_errors[pid]
        instance = self.live.get(pid) or self.retained.get(pid)
        if instance is None:
            raise ProcessLookupError("Fictional process departed before pin")
        handle = FakeHandle(instance, self)
        self.handles.append(handle)
        return handle

    def __call__(self, executable):
        self.calls.append("legacy")
        if self.legacy_override is not None:
            return self.legacy_override
        alive = [p for p in self.live.values() if p.ended is None]
        return [{"pid": p.pid, "created": p.created, "executable": p.executable,
                 "children_observed": any(c.ppid == p.pid for c in alive)}
                for p in alive if key(p.executable) == key(executable)]

    def advance(self, ticks=10000):
        self.now += ticks
        return self.now

    def spawn(self, pid, ppid, executable, created=None):
        if created is None:
            created = self.advance()
        instance = FakeInstance(pid, ppid, executable, created)
        instance.sequence = self.next_sequence
        self.next_sequence += 1
        self.live[pid] = instance
        return instance

    def exit(self, instance):
        instance.ended = self.advance()
        if self.live.get(instance.pid) is instance:
            del self.live[instance.pid]


class SkseExitTests(unittest.TestCase):
    def setUp(self):
        self.observer = FakeObserver()
        self.guard = RunningApplications(observer=self.observer)
        self.guard.managed_game = lambda: FakeGame()

    def observe(self):
        return bool(self.guard)

    def launch(self, root_pid=10, game_pid=20):
        self.guard.starting(LOADER)
        root = self.observer.spawn(root_pid, 1, LOADER)
        self.assertTrue(self.observe(), "Observed live loader must block")
        game = self.observer.spawn(game_pid, root.pid, GAME)
        self.assertTrue(self.observe(), "Observed Skyrim child must block")
        return root, game

    def finish_root(self, root, code=0):
        self.observer.exit(root)
        self.guard.finished(LOADER, code)

    def close_tree(self, root, game):
        self.observer.exit(root)
        self.assertTrue(self.observe(), "A surviving game is still guarded")
        self.observer.exit(game)
        self.assertTrue(self.observe(), "The finish callback must still be reconciled")
        self.guard.finished(LOADER, 0)
        self.assertFalse(self.observe(), "Proved normal SKSE/game completion must clear")


    def first_seen_game(self):
        """An external holder preserves an exited root's queryable object."""
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        game = self.observer.spawn(20, root.pid, GAME)
        self.observer.retained[root.pid] = root
        self.observer.exit(root)
        return root, game

    def finish_first_seen(self, root, game):
        self.assertTrue(self.observe())
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        return self.observe()

    def test_fast_loader_recovered_from_held_exited_parent_proves_normal_exit(self):
        root, game = self.first_seen_game()
        self.assertFalse(self.finish_first_seen(root, game))
        self.assertTrue(any(h.pid == root.pid for h in self.observer.handles))
        self.assertTrue(all(h.closed and h.close_calls == 1 for h in self.observer.handles))

    def test_fast_loader_missing_parent_object_keeps_uncertainty(self):
        root, game = self.first_seen_game()
        self.observer.retained.clear()
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_denied_parent_handle_keeps_uncertainty(self):
        root, game = self.first_seen_game()
        self.observer.pin_errors[root.pid] = PermissionError("parent denied")
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_live_parent_hidden_from_snapshot_is_not_exit_proof(self):
        root, game = self.first_seen_game()
        root.ended = None
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_reused_parent_pid_birth_cannot_prove_ancestry(self):
        root, game = self.first_seen_game()
        reused = FakeInstance(root.pid, 1, LOADER, game.created + 1)
        reused.ended = root.ended
        self.observer.retained[root.pid] = reused
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_other_executable_parent_cannot_prove_ancestry(self):
        root, game = self.first_seen_game()
        root.executable = OTHER
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_preexisting_parent_birth_cannot_prove_new_launch(self):
        root, game = self.first_seen_game()
        root.created = TICKS - 1
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_parent_exit_before_child_birth_is_not_valid_ancestry(self):
        root, game = self.first_seen_game()
        root.ended = game.created - 1
        self.assertTrue(self.finish_first_seen(root, game))

    def test_fast_loader_parent_exit_query_failure_keeps_uncertainty(self):
        root, game = self.first_seen_game()
        root.exit_error = OSError("parent exit unreadable")
        self.assertTrue(self.finish_first_seen(root, game))

    def test_normal_skse_exit_clears_only_after_game_finishes(self):
        root, game = self.launch()
        self.close_tree(root, game)
        self.assertFalse(list(self.guard))
        self.assertFalse(self.guard.describe()["launches"])

    def test_game_finishing_before_loader_still_waits_for_loader(self):
        root, game = self.launch()
        self.observer.exit(game)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.assertFalse(self.observe())

    def test_callback_before_actual_exit_does_not_unblock_live_tree(self):
        root, game = self.launch()
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.assertTrue(self.observe())
        self.observer.exit(game)
        self.assertTrue(self.observe(), "Early finish remains unsafe even after eventual exits")

    def test_startup_helper_can_exit_before_normal_skse_completion(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        helper = self.observer.spawn(30, root.pid, OTHER)
        self.assertTrue(self.observe(), "The loader and startup helper are still active")
        game = self.observer.spawn(20, root.pid, GAME)
        self.assertTrue(self.observe())
        self.observer.exit(helper)
        self.close_tree(root, game)

    def test_external_owner_and_verified_new_descendants_do_not_block_at_finish(self):
        browser = self.observer.spawn(40, 0, OTHER)
        root, game = self.launch()
        browser_worker = self.observer.spawn(41, browser.pid, OTHER)
        self.assertTrue(self.observe(), "The exact tracked game still blocks")
        extension_worker = self.observer.spawn(42, browser_worker.pid, OTHER)
        self.assertTrue(self.observe())
        self.close_tree(root, game)
        self.assertIsNone(browser_worker.ended)
        self.assertIsNone(extension_worker.ended)

    def test_late_identification_of_exact_loader_invalidates_external_proof(self):
        browser = self.observer.spawn(40, 0, OTHER)
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, browser.pid, LOADER)
        # Toolhelp did not initially list the loader; the native sequence scan did.
        self.observer.snapshot_override = {1: (0, "host.exe"), 40: (0, "fixture.exe")}
        self.assertTrue(self.observe())
        helper = self.observer.spawn(30, root.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.snapshot_override = None
        game = self.observer.spawn(20, root.pid, GAME)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "A certified chain through the tracked loader must be invalidated")
        self.assertIsNone(helper.ended)

    def test_direct_startup_helper_surviving_normal_callback_still_blocks(self):
        root, game = self.launch()
        helper = self.observer.spawn(30, root.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "The final snapshot still contains a new helper identity")
        self.assertIsNone(helper.ended)

    def test_live_grandchild_cannot_be_hidden_by_game_and_loader_exit(self):
        root, game = self.launch()
        helper = self.observer.spawn(30, game.pid, OTHER)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe(), "A live observed descendant must block")
        self.observer.exit(helper)
        self.observe()  # Completion may remain conservative for unknown helpers.

    def test_child_spawned_after_loader_exit_is_not_guessed_into_complete_tree(self):
        root, game = self.launch()
        self.finish_root(root)
        late = self.observer.spawn(30, game.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.exit(game)
        self.assertTrue(self.observe(), "A live later descendant remains a blocker")
        self.observer.exit(late)

    def test_new_detached_helper_cannot_be_ignored_at_normal_completion(self):
        root, game = self.launch()
        helper = self.observer.spawn(40, 999, OTHER)
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "A new unexplained detached process prevents complete-tree proof")
        self.assertIsNone(helper.ended)

    def test_preexisting_unconnected_orphan_does_not_prevent_normal_completion(self):
        background = self.observer.spawn(40, 999, OTHER)
        root, game = self.launch()
        self.close_tree(root, game)
        self.assertIsNone(background.ended)

    def test_detached_helper_appearing_in_final_rescan_blocks_completion(self):
        root, game = self.launch()
        self.observer.exit(root)
        self.observer.exit(game)
        original = self.observer.snapshot
        calls = [0]
        def race():
            calls[0] += 1
            if calls[0] == 2:
                # The process was born before this observation, but only the
                # second global snapshot exposes its already-detached parent.
                self.observer.spawn(40, 999, OTHER, created=self.observer.now - 1)
            return original()
        with mock.patch.object(self.observer, "snapshot", side_effect=race):
            self.guard.finished(LOADER, 0)
        self.assertGreaterEqual(calls[0], 2)
        self.assertTrue(self.observe(), "Final rescan must not discard a new detached helper")

    def test_reused_prelaunch_orphan_pid_does_not_hide_a_new_detached_process(self):
        background = self.observer.spawn(40, 999, OTHER)
        root, game = self.launch()
        self.observer.exit(background)
        replacement = self.observer.spawn(40, 999, OTHER)
        self.assertGreater(replacement.created, background.created)
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "Equal PID/parent/basename is not prelaunch creation identity")

    def test_dispatch_rejects_write_before_proof_and_enters_fake_write_after_proof(self):
        from mo2_codex_bridge.operations import Operations
        root, game = self.launch()
        with tempfile.TemporaryDirectory(prefix="offline-skse-dispatch-") as folder:
            data = Path(folder)
            ops = Operations(None, None, data, lambda: None)
            ops.running = self.guard
            ops.snapshot = mock.Mock(return_value={"profile": "Offline Test Profile", "revision": "unchanged-fixture"})
            ops.write_config = mock.Mock(return_value={"simulated": True})
            params = {"expected_profile": "Offline Test Profile", "expected_revision": "unchanged-fixture"}
            with self.assertRaisesRegex(ValueError, "Close the game") as caught:
                ops.dispatch("write_config", params)
            self.assertFalse(caught.exception.bridge_failure["changes_may_have_occurred"])
            self.assertEqual(caught.exception.bridge_failure["phase"], "preflight")
            ops.write_config.assert_not_called()
            self.assertFalse(list(data.iterdir()), "Active-game preflight must not create even a fixture backup")
            self.close_tree(root, game)
            result = ops.dispatch("write_config", params)
            self.assertTrue(result["simulated"])
            ops.write_config.assert_called_once()
            # Only synthetic before/result journals exist in this temporary
            # workspace; no configuration target is read or written.
            files = sorted(q.name for q in (data / "backups" / result["backup_id"]).iterdir())
            self.assertEqual(files, ["before.json", "result.json"])

    def test_missed_intermediary_parent_pid_reuse_cannot_hide_live_helper(self):
        self.observer.spawn(2, 1, r"C:\offline-lifecycle-fixture\explorer.exe")
        root, game = self.launch()
        self.observer.exit(root)
        intermediary = self.observer.spawn(30, game.pid, OTHER)
        helper = self.observer.spawn(40, intermediary.pid, OTHER + ".child.exe")
        self.observer.exit(intermediary)
        replacement_parent = self.observer.spawn(30, 2, OTHER + ".unrelated.exe")
        self.assertGreater(replacement_parent.created, helper.created)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "A reused parent PID is not proof the new helper is unrelated")
        self.assertIsNone(helper.ended)

    def test_unsupported_native_identity_api_fails_closed(self):
        self.observer.identities_error = OSError("SystemBasicProcessInformation is unsupported")
        self.guard.starting(LOADER)
        self.observer.identities_error = None
        root = self.observer.spawn(10, 1, LOADER)
        game = self.observer.spawn(20, root.pid, GAME)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "Missing prelaunch native identity evidence is never guessed")

    def test_final_native_identity_query_failure_fails_closed(self):
        root, game = self.launch()
        self.observer.exit(root)
        self.observer.exit(game)
        self.observer.identities_error = PermissionError("native identity query denied")
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe())

    def test_new_unrelated_surviving_process_still_blocks_conservative_completion(self):
        root, game = self.launch()
        unrelated = self.observer.spawn(40, 1, OTHER)
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe(), "Unproved new process identity prevents complete exclusion of detached descendants")
        self.assertIsNone(unrelated.ended)

    def test_same_path_second_launch_is_still_blocked_after_one_tree_finishes(self):
        root, game = self.launch()
        self.guard.starting(LOADER)
        root2 = self.observer.spawn(11, 1, LOADER)
        self.assertTrue(self.observe())
        game2 = self.observer.spawn(21, root2.pid, GAME)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())
        self.assertIsNone(root2.ended)
        self.assertIsNone(game2.ended)

    def test_repeated_sequential_sessions_clear_without_handle_growth(self):
        for i in range(8):
            root, game = self.launch(10 + i, 30 + i)
            self.close_tree(root, game)
        self.assertTrue(self.observer.handles, "The proof requires owned identities")
        self.assertTrue(all(h.closed and h.close_calls == 1 for h in self.observer.handles))

    def test_snapshot_failure_is_not_a_completed_tree(self):
        root, game = self.launch()
        self.observer.snapshot_error = PermissionError("snapshot denied")
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_descendant_pin_failure_is_not_absence(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        self.assertTrue(self.observe())
        game = self.observer.spawn(20, 10, GAME)
        self.observer.pin_errors[game.pid] = PermissionError("query denied")
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe(), "Unobserved denied child is not proved closed")

    def test_root_pin_failure_retains_unconfirmed_launch(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        self.observer.pin_errors[root.pid] = PermissionError("root denied")
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.assertTrue(self.observe())

    def test_pinned_game_exit_query_failure_blocks_after_empty_snapshot(self):
        root, game = self.launch()
        self.finish_root(root)
        game.exit_error = OSError("exit query denied")
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_clock_failure_is_not_launch_or_exit_proof(self):
        self.observer.clock_error = OSError("clock unavailable")
        self.guard.starting(LOADER)
        self.observer.clock_error = None
        root = self.observer.spawn(10, 1, LOADER)
        game = self.observer.spawn(20, 10, GAME)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_preexisting_loader_cannot_impersonate_a_new_launch(self):
        root = self.observer.spawn(10, 1, LOADER)
        game = self.observer.spawn(20, 10, GAME)
        self.guard.starting(LOADER)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_preexisting_game_cannot_be_claimed_by_new_loader(self):
        game = self.observer.spawn(20, 999, GAME)
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_future_birth_is_not_accepted_from_a_racing_observation(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER, created=self.observer.now + 100000)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.assertTrue(self.observe())

    def test_old_child_with_reused_parent_pid_is_not_a_proved_skse_child(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        self.assertTrue(self.observe())
        game = self.observer.spawn(20, 10, GAME, created=root.created - 1)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_child_born_after_proved_parent_exit_is_not_a_trusted_edge(self):
        root, game = self.launch()
        self.finish_root(root)
        # Pin-time identity is immutable. A fresh table occupant with that PID
        # is an unrelated later process, not the previously pinned game.
        self.observer.exit(game)
        replacement = self.observer.spawn(20, root.pid, GAME)
        self.assertTrue(self.observe())
        self.assertIsNone(replacement.ended)

    def test_reused_game_pid_cannot_make_a_live_new_game_look_closed(self):
        root, game = self.launch()
        self.observer.exit(game)
        replacement = self.observer.spawn(game.pid, root.pid, GAME)
        self.assertNotEqual(game.created, replacement.created)
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.assertTrue(self.observe())

    def test_same_basename_outside_game_directory_is_not_the_expected_game(self):
        self.guard.starting(LOADER)
        root = self.observer.spawn(10, 1, LOADER)
        self.assertTrue(self.observe())
        imposter = self.observer.spawn(20, root.pid, r"C:\different-game\SkyrimSE.exe")
        self.assertTrue(self.observe())
        self.finish_root(root)
        self.observer.exit(imposter)
        self.assertTrue(self.observe())

    def test_relative_or_different_loader_location_does_not_gain_skse_exception(self):
        for path in ("skse64_loader.exe", r"C:\elsewhere\skse64_loader.exe"):
            with self.subTest(path=path):
                observer = FakeObserver()
                guard = RunningApplications(observer=observer)
                guard.managed_game = lambda: FakeGame()
                guard.starting(path)
                guard.finished(path, 0)
                self.assertTrue(guard)

    def test_wrong_managed_game_does_not_enable_skse_special_case(self):
        self.guard.managed_game = lambda: FakeGame(short_name="EnderalSE")
        root, game = self.launch()
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_unavailable_managed_game_does_not_enable_skse_special_case(self):
        self.guard.managed_game = mock.Mock(side_effect=RuntimeError("game unavailable"))
        root, game = self.launch()
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_invalid_finish_codes_never_clear_after_observed_tree_exit(self):
        for code in (None, True, False, 259, -1, 0xffffffff, "0"):
            with self.subTest(code=code):
                self.setUp()
                root, game = self.launch()
                self.finish_root(root, code)
                self.observer.exit(game)
                self.assertTrue(self.observe())

    def test_unseen_fast_launch_and_finish_remain_uncertain(self):
        self.guard.starting(LOADER)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe())
        self.observer.advance(10**12)
        self.assertTrue(self.observe(), "Elapsed time never discharges an unseen launch")

    def test_missing_finish_callback_keeps_attempt_unresolved(self):
        root, game = self.launch()
        self.observer.exit(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_extra_unobserved_attempt_is_not_discharged_by_one_finish(self):
        root, game = self.launch()
        self.guard.starting(LOADER)
        self.finish_root(root)
        self.observer.exit(game)
        self.assertTrue(self.observe())

    def test_manual_discard_cannot_erase_a_live_tracked_tree(self):
        root, game = self.launch()
        self.guard.discard(LOADER)
        self.assertTrue(self.observe())
        self.close_tree(root, game)

    def test_iteration_describe_and_poll_do_not_release_an_active_game(self):
        root, game = self.launch()
        self.observer.exit(root)
        self.assertTrue(list(self.guard))
        self.assertTrue(self.guard.describe()["launches"])
        with mock.patch("mo2_codex_bridge.running_apps.time.monotonic", return_value=10**6):
            self.guard.poll()
        self.assertTrue(self.observe())
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertFalse(self.observe())

    def finish_with_survivor(self, parent=None):
        root, game = self.launch()
        helper = self.observer.spawn(40, game.pid if parent is None else parent, OTHER)
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        return helper

    def test_verified_roots_wait_for_helper_then_release_without_another_callback(self):
        helper = self.finish_with_survivor()
        waiting = self.guard.describe()["launches"][0]
        self.assertEqual(waiting["pending_attempts"], 0)
        self.assertEqual(waiting["instances"], [])
        self.assertEqual(waiting["completion_state"], "checking_survivors")
        self.assertEqual(waiting["completion_survivor_pids"], [helper.pid])
        self.assertEqual(len(waiting["callback_exit_evidence"]), 2)
        self.assertTrue(all(not handle.closed for handle in self.observer.handles))
        self.assertTrue(self.guard)
        self.observer.exit(helper)
        with mock.patch("mo2_codex_bridge.running_apps.time.monotonic", return_value=10**6):
            self.guard.poll()
        self.assertEqual(self.guard.describe()["launches"], [])
        self.assertTrue(all(handle.closed and handle.close_calls == 1 for handle in self.observer.handles))

    def test_first_helpers_exit_does_not_hide_a_later_descendant(self):
        helper = self.finish_with_survivor(parent=999)
        child = self.observer.spawn(41, helper.pid, OTHER)
        self.observer.exit(helper)
        waiting = self.guard.describe()["launches"][0]
        self.assertEqual(waiting["completion_survivor_pids"], [child.pid])
        self.assertTrue(self.guard)
        self.observer.exit(child)
        self.assertFalse(self.guard)

    def enable_exact_birth_probes(self):
        def birth_stamp(pid, sequence):
            self.observer.calls.append(("birth", pid))
            process = self.observer.live.get(pid)
            if process is not None and process.sequence == sequence:
                return 7, process.created - (TICKS - 100000)
            return None
        self.observer.birth_stamp = birth_stamp

    def test_matching_exit_excludes_a_verified_same_poll_desktop_tree(self):
        self.enable_exact_birth_probes()
        desktop = self.observer.spawn(2, 0, OTHER)
        root, game = self.launch()
        parent = self.observer.spawn(30, desktop.pid, OTHER)
        self.observer.spawn(31, parent.pid, OTHER)
        self.assertTrue(self.observe(), "Writes remain guarded while the game is running")
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "birth"
                             for call in self.observer.calls))
        self.close_tree(root, game)
        self.assertEqual(self.guard.describe()["launches"], [])
        self.assertIn(("birth", 31), self.observer.calls)

    def test_late_related_child_blocks_while_late_desktop_child_clears(self):
        self.enable_exact_birth_probes()
        desktop = self.observer.spawn(2, 0, OTHER)
        root, game = self.launch()
        parent = self.observer.spawn(30, desktop.pid, OTHER)
        desktop_child = self.observer.spawn(31, parent.pid, OTHER)
        helper = self.observer.spawn(40, game.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        waiting = self.guard.describe()["launches"][0]
        self.assertEqual(waiting["completion_survivor_pids"], [helper.pid])
        self.assertEqual(waiting["completion_survivor_details"][0]["pid"], helper.pid)
        late = self.observer.spawn(41, helper.pid, OTHER)
        unrelated = self.observer.spawn(32, desktop_child.pid, OTHER)
        self.assertEqual(self.guard.describe()["launches"][0]["completion_survivor_pids"],
                         [helper.pid, late.pid])
        self.observer.exit(helper)
        self.assertEqual(self.guard.describe()["launches"][0]["completion_survivor_pids"], [late.pid])
        self.observer.exit(late)
        self.assertFalse(self.observe())
        self.assertIn(unrelated.pid, self.observer.live)

    def test_exact_skse_recovery_leaves_independent_wrapper_guard(self):
        self.enable_exact_birth_probes()
        desktop = self.observer.spawn(2, 0, OTHER)
        wrapper = r"C:\offline-lifecycle-fixture\cmd.exe"
        self.guard.starting(wrapper)
        shell = self.observer.spawn(60, 1, wrapper)
        child = self.observer.spawn(61, shell.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.exit(child)
        self.observer.exit(shell)
        self.guard.finished(wrapper, 0)
        root, game = self.launch()
        parent = self.observer.spawn(30, desktop.pid, OTHER)
        self.observer.spawn(31, parent.pid, OTHER)
        self.assertTrue(self.observe())
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        records = self.guard.describe()["launches"]
        self.assertEqual([item["executable"] for item in records], [wrapper])
        self.assertTrue(records[0]["finish_uncertainty"])
        self.assertTrue(self.observe())

    def test_new_start_reconciles_old_completion_before_new_baseline(self):
        helper = self.finish_with_survivor()
        self.assertTrue(self.guard)
        first_handles = list(self.observer.handles)
        self.observer.exit(helper)
        # No poll or status call between exit and the next AboutToRun.
        root, game = self.launch(root_pid=11, game_pid=21)
        self.assertTrue(all(handle.closed for handle in first_handles))
        self.assertEqual(self.guard.describe()["launches"][0]["launch_number"], 2)
        self.close_tree(root, game)

    def test_new_start_cannot_make_retained_survivors_safe_by_rebaselining(self):
        helper = self.finish_with_survivor()
        first = self.guard.describe()["launches"][0]
        self.guard.starting(LOADER)
        self.observer.exit(helper)
        root = self.observer.spawn(11, 1, LOADER)
        game = self.observer.spawn(21, root.pid, GAME)
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        final = self.guard.describe()["launches"][0]
        self.assertEqual(final["completion_state"], "restart_required")
        self.assertEqual(final["first_uncertainty"], first["first_uncertainty"])
        self.assertEqual(final["callback_exit_evidence"], first["callback_exit_evidence"])
        self.assertIn("previous launch was still unresolved", final["finish_uncertainty"])
        self.assertTrue(self.guard)

    def test_later_sequential_start_preserves_an_earlier_terminal_reason(self):
        root, game = self.launch()
        self.guard.finished(LOADER, 0)
        first = self.guard.describe()["launches"][0]
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.starting(LOADER)
        self.guard.finished(LOADER, 0)
        final = self.guard.describe()["launches"][0]
        self.assertEqual(final["pending_attempts"], 0)
        self.assertEqual(final["first_uncertainty"], first["first_uncertainty"])
        self.assertEqual(final["finish_uncertainty"], first["finish_uncertainty"])
        self.assertEqual(final["last_observed_processes"], first["last_observed_processes"])
        self.assertIn("before SKSE and Skyrim exits", final["finish_uncertainty"])

    def test_game_exit_during_finish_observation_cannot_validate_early_callback(self):
        root, game = self.launch()
        self.observer.exit(root)
        original = self.observer.snapshot
        def exit_after_callback_entered():
            if game.ended is None:
                self.observer.exit(game)
            return original()
        with mock.patch.object(self.observer, "snapshot", side_effect=exit_after_callback_entered):
            self.guard.finished(LOADER, 0)
        self.assertTrue(self.guard)
        observed = self.guard.describe()["launches"][0]
        self.assertEqual(observed["completion_state"], "restart_required")
        self.assertEqual(observed["callback_exit_evidence"], [])

    def test_early_finish_remains_blocked_if_clock_moves_back_during_scan(self):
        root, game = self.launch()
        self.observer.exit(root)
        self.observer.advance(30000)
        original = self.observer.snapshot

        def rollback_then_exit():
            if game.ended is None:
                self.observer.advance(-20000)
                self.observer.exit(game)
            return original()

        with mock.patch.object(self.observer, "snapshot", side_effect=rollback_then_exit):
            self.guard.finished(LOADER, 0)
            if game.ended is None:
                self.observer.exit(game)
        row = self.guard.describe()["launches"][0]
        self.assertEqual(row["completion_state"], "restart_required")
        self.assertEqual(row["callback_exit_evidence"], [])
        self.assertTrue(self.guard)

    def test_snapshot_failure_while_waiting_remains_blocked_after_api_recovers(self):
        helper = self.finish_with_survivor()
        self.observer.identities_error = OSError("incomplete fictional ancestry view")
        self.assertTrue(self.guard)
        self.observer.identities_error = None
        self.observer.exit(helper)
        row = self.guard.describe()["launches"][0]
        self.assertEqual(row["completion_state"], "restart_required")
        self.assertIn("incomplete fictional ancestry view", row["observation_error"])
        self.assertEqual(len(row["callback_exit_evidence"]), 2)
        self.assertTrue(self.guard)

    def test_duplicate_finish_while_waiting_does_not_grant_completion(self):
        helper = self.finish_with_survivor()
        self.guard.finished(LOADER, 0)
        self.observer.exit(helper)
        row = self.guard.describe()["launches"][0]
        self.assertEqual(row["pending_attempts"], 0)
        self.assertEqual(row["completion_state"], "restart_required")
        self.assertTrue(self.guard)

    def test_recovery_preserves_an_independently_launched_tool(self):
        helper = self.finish_with_survivor()
        self.guard.starting(OTHER)
        self.observer.exit(helper)
        self.assertTrue(self.guard)
        rows = self.guard.describe()["launches"]
        self.assertEqual([row["executable"] for row in rows], [OTHER])

    def test_active_survivor_prevents_write_preflight_until_observed_exit(self):
        from mo2_codex_bridge.operations import Operations
        helper = self.finish_with_survivor()
        with tempfile.TemporaryDirectory(prefix="offline-skse-survivor-") as folder:
            ops = Operations(None, None, Path(folder), lambda: None)
            ops.running = self.guard
            ops.snapshot = mock.Mock(return_value={"profile": "Fixture", "revision": "fixture"})
            ops.write_config = mock.Mock(return_value={"mock_write": True})
            params = {"expected_profile": "Fixture", "expected_revision": "fixture"}
            with self.assertRaisesRegex(ValueError, "Close the game") as caught:
                ops.dispatch("write_config", params)
            self.assertEqual(caught.exception.bridge_failure["phase"], "preflight")
            self.assertFalse(caught.exception.bridge_failure["changes_may_have_occurred"])
            ops.write_config.assert_not_called()
            self.assertFalse(list(Path(folder).iterdir()))
            self.observer.exit(helper)
            self.assertTrue(ops.dispatch("write_config", params)["mock_write"])
            ops.write_config.assert_called_once()

    def test_tracking_events_preserve_origin_and_recovery_with_no_poll_spam(self):
        events = []
        self.guard = RunningApplications(observer=self.observer, on_skse_event=events.append)
        self.guard.managed_game = lambda: FakeGame()
        helper = self.finish_with_survivor()
        for _ in range(3):
            self.assertTrue(self.guard)
        self.observer.exit(helper)
        self.assertFalse(self.guard)
        self.assertEqual([event["event"] for event in events],
                         ["start", "finish", "waiting_for_survivors", "recovered"])
        self.assertEqual(len({event["session_id"] for event in events}), 1)
        self.assertEqual(len({event["launch_number"] for event in events}), 1)
        self.assertEqual(events[1]["exit_code"], 0)
        self.assertEqual(events[1]["pending_before"], 1)
        self.assertEqual(events[1]["pending_attempts"], 0)
        self.assertEqual(events[2]["first_uncertainty"], events[3]["first_uncertainty"])
        self.assertEqual(events[3]["callback_exit_evidence"], events[1]["callback_exit_evidence"])

    def test_diagnostic_write_failure_cannot_change_guard_decisions(self):
        audit = mock.Mock(side_effect=OSError("Audit unavailable"))
        self.guard = RunningApplications(observer=self.observer, on_skse_event=audit)
        self.guard.managed_game = lambda: FakeGame()
        helper = self.finish_with_survivor()
        self.assertTrue(self.guard)
        self.observer.exit(helper)
        self.assertFalse(self.guard)
        self.assertEqual(audit.call_count, 4)

    def test_diagnostic_consumer_cannot_mutate_retained_exit_proof(self):
        def mutate_event(event):
            event["last_observed_processes"].clear()
            event["callback_exit_evidence"].clear()
            event["survivor_pids"].clear()
            raise OSError("Synthetic diagnostic fault")

        self.guard = RunningApplications(observer=self.observer, on_skse_event=mutate_event)
        self.guard.managed_game = lambda: FakeGame()
        helper = self.finish_with_survivor()
        view = self.guard.describe()["launches"][0]
        self.assertEqual(len(view["callback_exit_evidence"]), 2)
        view["callback_exit_evidence"].clear()
        view["last_observed_processes"].clear()
        self.assertEqual(len(self.guard.describe()["launches"][0]["callback_exit_evidence"]), 2)
        self.observer.exit(helper)
        self.assertFalse(self.guard)

    def test_global_uncertainty_remains_after_a_proved_tree_finishes(self):
        root, game = self.launch()
        self.guard.global_uncertainty = "Required callback registration failed"
        self.observer.exit(root)
        self.observer.exit(game)
        self.guard.finished(LOADER, 0)
        self.assertTrue(self.observe())
        self.assertIn("registration failed", " ".join(self.guard))

    def test_observer_without_tree_interface_retains_legacy_wrapper_refusal(self):
        rows = []
        guard = RunningApplications(observer=lambda _: list(rows))
        guard.managed_game = lambda: FakeGame()
        guard.starting(LOADER)
        rows.append({"pid": 10, "created": 1, "executable": LOADER,
                     "children_observed": True})
        self.assertTrue(guard)
        rows.clear()
        guard.finished(LOADER, 0)
        self.assertTrue(guard)

    def test_legacy_direct_leaf_completion_still_clears(self):
        path = r"C:\offline-lifecycle-fixture\direct-tool.exe"
        rows = []
        guard = RunningApplications(observer=lambda _: list(rows))
        guard.starting(path)
        rows.append({"pid": 10, "created": 1, "executable": path})
        self.assertTrue(guard)
        rows.clear()
        guard.finished(path, 0)
        self.assertFalse(guard)



if __name__ == "__main__":
    unittest.main(verbosity=2)
