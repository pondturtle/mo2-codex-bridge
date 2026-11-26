"""Late generation proof for the SKSE survivor guard, using only synthetic process data."""
import unittest

from mo2_codex_bridge.running_apps import _ProcessLineage


OWNER = 10
EXPLORER = 15
DESKTOP = 20
WORKER = 21
CONSOLE = 22
LATE_DESKTOP = 23
GAME = 30
GAME_HELPER = 31
LATE_GAME_HELPER = 32
UNKNOWN_PARENT = 99
BOOT = 3

BASE = {1: (0, 700), OWNER: (EXPLORER, 900), EXPLORER: (1, 400)}
# Put children before parents. Neither the row order nor the sequence values
# express birth order. One first-seen snapshot can contain several generations.
DESKTOP_BURST = {CONSOLE: (WORKER, 42), WORKER: (DESKTOP, 1), DESKTOP: (EXPLORER, 516)}
DESKTOP_BIRTHS = {
    (EXPLORER, 400): (BOOT, 100),
    (DESKTOP, 516): (BOOT, 700),
    (WORKER, 1): (BOOT, 710),
    (CONSOLE, 42): (BOOT, 715),
}


class BirthStamps:
    def __init__(self, stamps=None):
        self.stamps = dict(stamps or {})
        self.calls = []

    def __call__(self, pid, sequence):
        self.calls.append((pid, sequence))
        return self.stamps.get((pid, sequence))


class LateAncestryTests(unittest.TestCase):
    def setUp(self):
        self.stamps = BirthStamps(DESKTOP_BIRTHS)
        self.graph = _ProcessLineage(OWNER, BASE, birth_stamp=self.stamps)

    def test_simultaneous_desktop_generations_clear_with_exact_birth_proof(self):
        current = {**BASE, **DESKTOP_BURST}
        # The baseline logic cannot tell whether the co-first PPID is the
        # true parent. Repeating an ordinary scan must not create evidence.
        self.assertEqual(self.graph.observe(current), {WORKER, CONSOLE})
        self.assertEqual(self.graph.observe(current), {WORKER, CONSOLE})
        self.assertEqual(self.stamps.calls, [])
        self.assertEqual(self.graph.observe(current, recover=True), set())

    def test_proof_may_arrive_on_a_later_poll_of_the_same_generations(self):
        current = {**BASE, **DESKTOP_BURST}
        del self.stamps.stamps[(WORKER, 1)]
        self.assertEqual(self.graph.observe(current, recover=True), {WORKER, CONSOLE})
        self.stamps.stamps[(WORKER, 1)] = (BOOT, 710)
        self.assertEqual(self.graph.observe(current, recover=True), set())

    def test_late_desktop_child_clears_while_late_game_child_keeps_blocking(self):
        current = {
            **BASE,
            **DESKTOP_BURST,
            GAME: (OWNER, 77),
            GAME_HELPER: (GAME, 78),
        }
        self.stamps.stamps.update({
            (GAME, 77): (BOOT, 200),
            (GAME_HELPER, 78): (BOOT, 705),
            (LATE_GAME_HELPER, 80): (BOOT, 900),
            (LATE_DESKTOP, 81): (BOOT, 901),
        })
        self.assertEqual(self.graph.observe(current, related_pids={GAME}, recover=True), {GAME, GAME_HELPER})
        del current[GAME]
        current[LATE_GAME_HELPER] = (GAME_HELPER, 80)
        current[LATE_DESKTOP] = (WORKER, 81)
        self.assertEqual(self.graph.observe(current, recover=True), {GAME_HELPER, LATE_GAME_HELPER})
        del current[GAME_HELPER]
        self.assertEqual(self.graph.observe(current, recover=True), {LATE_GAME_HELPER})
        del current[LATE_GAME_HELPER]
        self.assertEqual(self.graph.observe(current, recover=True), set())

    def test_late_child_of_unresolved_ancestor_cannot_become_external(self):
        current = {**BASE, GAME_HELPER: (UNKNOWN_PARENT, 78)}
        self.stamps.stamps[(GAME_HELPER, 78)] = (BOOT, 800)
        self.assertEqual(self.graph.observe(current, recover=True), {GAME_HELPER})
        current[LATE_GAME_HELPER] = (GAME_HELPER, 80)
        self.stamps.stamps[(LATE_GAME_HELPER, 80)] = (BOOT, 900)
        self.assertEqual(self.graph.observe(current, recover=True), {GAME_HELPER, LATE_GAME_HELPER})
        del current[GAME_HELPER]
        self.assertEqual(self.graph.observe(current, recover=True), {LATE_GAME_HELPER})

    def test_late_appearance_of_a_previously_missing_parent_cannot_certify_child(self):
        without_parent = {**BASE, WORKER: (DESKTOP, 1)}
        self.assertEqual(self.graph.observe(without_parent, recover=True), {WORKER})
        # Even superficially compatible birth data cannot turn the missing
        # generation from the first observation into this later one.
        with_parent = {**without_parent, DESKTOP: (EXPLORER, 516)}
        self.assertEqual(self.graph.observe(with_parent, recover=True), {WORKER})

    def test_recycled_parent_pid_cannot_replace_the_first_observed_generation(self):
        first = {**BASE, DESKTOP: (EXPLORER, 516), WORKER: (DESKTOP, 1)}
        del self.stamps.stamps[(DESKTOP, 516)]
        self.assertEqual(self.graph.observe(first, recover=True), {WORKER})
        replacement = {**first, DESKTOP: (EXPLORER, 517)}
        self.stamps.stamps[(DESKTOP, 517)] = (BOOT, 720)
        self.assertEqual(self.graph.observe(replacement, recover=True), {WORKER})

    def test_stale_birth_stamps_do_not_authorize_reused_pids(self):
        current = {**BASE, **DESKTOP_BURST}
        self.assertEqual(self.graph.observe(current, recover=True), set())
        self.assertEqual(self.graph.observe(BASE, recover=True), set())
        replacement = {**BASE, DESKTOP: (EXPLORER, 600), WORKER: (DESKTOP, 601)}
        self.assertEqual(self.graph.observe(replacement, recover=True), {WORKER})

    def test_same_poll_parent_born_after_child_cannot_certify_it(self):
        current = {**BASE, DESKTOP: (EXPLORER, 516), WORKER: (DESKTOP, 1)}
        self.stamps.stamps[(DESKTOP, 516)] = (BOOT, 711)
        self.assertEqual(self.graph.observe(current, recover=True), {WORKER})

    def test_equal_birth_values_do_not_prove_parent_precedes_child(self):
        current = {**BASE, DESKTOP: (EXPLORER, 516), WORKER: (DESKTOP, 1)}
        self.stamps.stamps[(DESKTOP, 516)] = self.stamps.stamps[(WORKER, 1)]
        self.assertEqual(self.graph.observe(current, recover=True), {WORKER})

    def test_birth_stamps_from_different_boot_contexts_do_not_certify_an_edge(self):
        current = {**BASE, DESKTOP: (EXPLORER, 516), WORKER: (DESKTOP, 1)}
        self.stamps.stamps[(DESKTOP, 516)] = (BOOT + 1, 700)
        self.assertEqual(self.graph.observe(current, recover=True), {WORKER})

    def test_late_related_generation_revokes_proof_through_the_whole_chain(self):
        current = {**BASE, **DESKTOP_BURST}
        self.assertEqual(self.graph.observe(current, recover=True), set())
        self.assertEqual(self.graph.observe(current, related_pids={DESKTOP}, recover=True),
                         {DESKTOP, WORKER, CONSOLE})
        del current[DESKTOP]
        self.assertEqual(self.graph.observe(current, recover=True), {WORKER, CONSOLE})


if __name__ == "__main__":
    unittest.main()

