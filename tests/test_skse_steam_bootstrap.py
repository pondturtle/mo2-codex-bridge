"""Steam bootstrap survivor tests using fictional Windows process generations."""

import ntpath
import unittest

from mo2_codex_bridge.running_apps import RunningApplications, _ProcessLineage
from mo2_codex_bridge.steam_bootstrap import SteamBootstrap
from test_skse_exit import GAME, LOADER, TICKS, FakeGame, FakeObserver, key


BOOT = 7
STEAM = r"d:\steam fixture\steam.exe"
WEB = r"d:\steam fixture\bin\cef\cef.win64\steamwebhelper.exe"
TOOL = r"d:\steam fixture\steamapps\common\tool.exe"
ROOT = 10
BROWSER = 20
RENDERER = 21
LOADER_PID = 30
GAME_PID = 40


class SteamObserver(FakeObserver):
    def __init__(self):
        super().__init__()
        self.registry_path = STEAM
        self.boot = BOOT
        self.birth_overrides = {}
        self.image_overrides = {}
        self.has_interrupt_clock = True
        self.has_loader_birth = True

    @staticmethod
    def interrupt_value(wall):
        return wall - TICKS + 20000

    def interrupt_clock(self):
        return self.interrupt_value(self.now) if self.has_interrupt_clock else None

    def steam_executable(self):
        return self.registry_path

    def birth_stamp(self, pid, sequence):
        process = self.live.get(pid)
        if process is None or process.sequence != sequence:
            return None
        default = self.boot, self.interrupt_value(process.created)
        return self.birth_overrides.get(pid, default)

    def pinned_birth(self, handle):
        if self.has_loader_birth:
            return self.boot, self.interrupt_value(handle.created)
        return None

    def process_image(self, pid, sequence):
        process = self.live.get(pid)
        if process is None or process.sequence != sequence:
            return None
        return self.image_overrides.get(pid, key(process.executable))


class SteamBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.observer = SteamObserver()
        self.bootstrap = SteamBootstrap(self.observer)

    def new_window(self):
        graph = _ProcessLineage(
            self.observer.owner_pid, self.observer.identities(), self.observer.birth_stamp
        )
        return self.bootstrap.capture_start(graph), graph

    def spawn_steam(self, path=STEAM, *, parent=None, helpers=True):
        if parent is None:
            parent = self.observer.owner_pid
        root = self.observer.spawn(ROOT, parent, path)
        if helpers:
            self.observer.spawn(BROWSER, ROOT, WEB)
            self.observer.spawn(RENDERER, BROWSER, WEB)
        return root

    def pinned_loader(self):
        process = self.observer.spawn(LOADER_PID, self.observer.owner_pid, LOADER)
        loader = self.observer.pin(LOADER_PID)
        self.addCleanup(loader.close)
        self.observer.exit(process)
        return loader

    def classify(self, window, loader, *, protected=()):
        start, graph = window
        unresolved = graph.observe(self.observer.identities(), protected, recover=True)
        excluded = self.bootstrap.excluded(graph, self.observer.snapshot(), loader, start)
        return unresolved, excluded

    def test_cold_start_excludes_only_exact_client_and_known_helpers(self):
        window = self.new_window()
        self.spawn_steam()
        loader = self.pinned_loader()
        unresolved, excluded = self.classify(window, loader, protected=(LOADER_PID,))
        self.assertTrue({ROOT, BROWSER, RENDERER}.issubset(unresolved))
        self.assertEqual(excluded, {ROOT, BROWSER, RENDERER})
        # The existing process-lineage decisions must remain unchanged.
        self.assertEqual(self.classify(window, loader, protected=(LOADER_PID,))[0], unresolved)

    def test_stock_cef_layouts_are_accepted(self):
        for layout in ("cef.win64", "cef.win7x64", "cef.win7"):
            with self.subTest(layout=layout):
                self.setUp()
                window = self.new_window()
                self.spawn_steam(helpers=False)
                helper = ntpath.join(ntpath.dirname(STEAM), "bin", "cef", layout, "steamwebhelper.exe")
                self.observer.spawn(BROWSER, ROOT, helper)
                _, excluded = self.classify(window, self.pinned_loader())
                self.assertEqual(excluded, {ROOT, BROWSER})

    def test_unknown_steam_child_and_recognized_grandchild_stay_guarded(self):
        window = self.new_window()
        self.spawn_steam()
        self.observer.spawn(50, BROWSER, TOOL)
        self.observer.spawn(60, 50, WEB)
        unresolved, excluded = self.classify(window, self.pinned_loader())
        self.assertEqual(excluded, {ROOT, BROWSER, RENDERER})
        self.assertTrue({50, 60}.issubset(unresolved - excluded))

    def test_lookalikes_do_not_authorize_the_root(self):
        for path in (r"d:\other steam\steam.exe", r"d:\steam fixture\steam.exe.bak"):
            with self.subTest(path=path):
                self.setUp()
                window = self.new_window()
                self.spawn_steam(path)
                _, excluded = self.classify(window, self.pinned_loader())
                self.assertEqual(excluded, set())

    def test_helper_with_unrecognized_path_and_its_descendant_stay_guarded(self):
        for path in (
            r"d:\other steam\bin\cef\cef.win64\steamwebhelper.exe",
            r"d:\steam fixture\steamapps\common\steamwebhelper.exe",
            r"d:\steam fixture\bin\cef\custom\steamwebhelper.exe",
        ):
            with self.subTest(path=path):
                self.setUp()
                window = self.new_window()
                self.spawn_steam(helpers=False)
                self.observer.spawn(BROWSER, ROOT, path)
                self.observer.spawn(RENDERER, BROWSER, WEB)
                _, excluded = self.classify(window, self.pinned_loader())
                self.assertEqual(excluded, {ROOT})

    def test_root_must_be_direct_child_of_mo2(self):
        window = self.new_window()
        self.observer.spawn(50, self.observer.owner_pid, TOOL)
        self.spawn_steam(parent=50)
        _, excluded = self.classify(window, self.pinned_loader())
        self.assertEqual(excluded, set())

    def test_root_born_at_or_after_loader_stays_guarded(self):
        for equal_birth in (False, True):
            with self.subTest(equal_birth=equal_birth):
                self.setUp()
                window = self.new_window()
                loader = self.pinned_loader()
                self.spawn_steam()
                if equal_birth:
                    self.observer.birth_overrides[ROOT] = self.observer.pinned_birth(loader)
                _, excluded = self.classify(window, loader)
                self.assertEqual(excluded, set())

    def test_missing_or_conflicting_root_evidence_stays_guarded(self):
        for missing in ("birth", "image", "boot", "loader", "installation"):
            with self.subTest(missing=missing):
                self.setUp()
                window = self.new_window()
                root = self.spawn_steam()
                if missing == "birth":
                    self.observer.birth_overrides[ROOT] = None
                if missing == "image":
                    self.observer.image_overrides[ROOT] = None
                if missing == "boot":
                    self.observer.birth_overrides[ROOT] = (BOOT + 1, self.observer.interrupt_value(root.created))
                if missing == "loader":
                    self.observer.has_loader_birth = False
                if missing == "installation":
                    self.observer.registry_path = r"d:\other steam\steam.exe"
                _, excluded = self.classify(window, self.pinned_loader())
                self.assertEqual(excluded, set())

    def test_no_start_window_proof_means_no_exemption(self):
        self.observer.has_interrupt_clock = False
        window = self.new_window()
        self.assertIsNone(window[0])
        self.spawn_steam()
        _, excluded = self.classify(window, self.pinned_loader())
        self.assertEqual(excluded, set())

    def test_baseline_steam_without_a_prior_certificate_is_not_exempt(self):
        self.spawn_steam(helpers=False)
        window = self.new_window()
        self.observer.spawn(BROWSER, ROOT, WEB)
        _, excluded = self.classify(window, self.pinned_loader())
        self.assertNotIn(BROWSER, excluded)

    def test_two_matching_new_roots_are_ambiguous(self):
        window = self.new_window()
        self.spawn_steam()
        self.observer.spawn(50, self.observer.owner_pid, STEAM)
        _, excluded = self.classify(window, self.pinned_loader())
        self.assertEqual(excluded, set())

    def test_protected_generations_override_bootstrap_evidence(self):
        for protected, expected in ((ROOT, set()), (BROWSER, {ROOT}), (RENDERER, {ROOT, BROWSER})):
            with self.subTest(protected=protected):
                self.setUp()
                window = self.new_window()
                self.spawn_steam()
                _, excluded = self.classify(window, self.pinned_loader(), protected=(protected,))
                self.assertEqual(excluded, expected)

    def test_reused_parent_pid_cannot_authorize_its_earlier_child(self):
        window = self.new_window()
        self.spawn_steam()
        browser = self.observer.live[BROWSER]
        # The renderer first appeared under the original browser generation.
        _, graph = window
        graph.observe(self.observer.identities())
        self.observer.exit(browser)
        self.observer.spawn(BROWSER, ROOT, WEB)
        _, excluded = self.classify(window, self.pinned_loader())
        self.assertEqual(excluded, {ROOT, BROWSER})

    def test_warm_launch_can_exclude_a_child_of_a_baseline_helper(self):
        cold = self.new_window()
        self.spawn_steam()
        loader = self.pinned_loader()
        self.assertEqual(self.classify(cold, loader)[1], {ROOT, BROWSER, RENDERER})
        self.observer.exit(self.observer.live[RENDERER])

        warm = self.new_window()
        self.observer.spawn(RENDERER, BROWSER, WEB)
        unresolved, excluded = self.classify(warm, self.pinned_loader())
        self.assertIn(RENDERER, unresolved)
        self.assertNotIn(RENDERER, unresolved - excluded)

    def test_warm_certificate_cannot_authorize_a_reused_root_pid(self):
        cold = self.new_window()
        root = self.spawn_steam()
        loader = self.pinned_loader()
        self.assertEqual(self.classify(cold, loader)[1], {ROOT, BROWSER, RENDERER})
        for process in (root, self.observer.live[BROWSER], self.observer.live[RENDERER]):
            self.observer.exit(process)
        self.spawn_steam(helpers=False)
        warm = self.new_window()
        self.observer.spawn(BROWSER, ROOT, WEB)
        _, excluded = self.classify(warm, self.pinned_loader())
        self.assertNotIn(BROWSER, excluded)


class SteamGuardIntegrationTests(unittest.TestCase):
    def launch(
        self, *, version="2.5.2", unknown_child=False, finish_while_running=False, explicit_steam_launch=False
    ):
        observer = SteamObserver()
        guard = RunningApplications(observer=observer, mo2_version=version)
        guard.managed_game = FakeGame
        guard.starting(LOADER)
        if explicit_steam_launch:
            guard.starting(STEAM)
        observer.spawn(ROOT, observer.owner_pid, STEAM)
        observer.spawn(BROWSER, ROOT, WEB)
        loader = observer.spawn(LOADER_PID, observer.owner_pid, LOADER)
        game = observer.spawn(GAME_PID, LOADER_PID, GAME)
        observer.advance()
        if unknown_child:
            observer.spawn(50, BROWSER, TOOL)
        self.assertTrue(guard)
        observer.exit(loader)
        self.assertTrue(guard)
        if not finish_while_running:
            observer.exit(game)
            self.assertTrue(guard)
        guard.finished(LOADER, 0)
        return observer, guard

    def test_writes_remain_guarded_until_exit_callback_then_steam_can_stay_open(self):
        observer, guard = self.launch()
        self.assertFalse(guard)
        self.assertEqual(set(observer.live), {observer.owner_pid, ROOT, BROWSER})

    def test_unknown_steam_child_remains_guarded_until_it_exits(self):
        observer, guard = self.launch(unknown_child=True)
        self.assertTrue(guard)
        state, = guard.describe()["launches"]
        self.assertEqual(state["completion_survivor_pids"], [50])
        observer.exit(observer.live[50])
        self.assertFalse(guard)
        self.assertIn(ROOT, observer.live)

    def test_early_finish_does_not_clear_running_skyrim(self):
        observer, guard = self.launch(finish_while_running=True)
        self.assertTrue(guard)
        state, = guard.describe()["launches"]
        self.assertEqual(state["completion_state"], "restart_required")
        self.assertIn(GAME_PID, observer.live)

    def test_other_mo2_version_keeps_original_steam_guard(self):
        _, guard = self.launch(version="2.5.1")
        self.assertTrue(guard)
        state, = guard.describe()["launches"]
        self.assertEqual(state["completion_survivor_pids"], [ROOT, BROWSER])

    def test_separately_tracked_steam_launch_keeps_the_application_guard(self):
        _, guard = self.launch(explicit_steam_launch=True)
        self.assertTrue(guard)
        states = guard.describe()["launches"]
        steam = next(state for state in states if key(state["executable"]) == STEAM)
        self.assertEqual(steam["pending_attempts"], 1)
        self.assertEqual([instance["pid"] for instance in steam["instances"]], [ROOT])


if __name__ == "__main__":
    unittest.main()
