"""Exercise the birth checks for branches tied to an observed MO2 launch."""
import unittest

from mo2_codex_bridge.running_apps import _ProcessLineage


OWNER = 10
EXPLORER = 15
PARENT = 20
CHILD = 21
GRANDCHILD = 22
BOOT = 3
BASE = {1: (0, 700), OWNER: (EXPLORER, 900), EXPLORER: (1, 400)}
TREE = {GRANDCHILD: (CHILD, 42), CHILD: (PARENT, 1), PARENT: (OWNER, 516)}
BIRTHS = {(PARENT, 516): (BOOT, 700), (CHILD, 1): (BOOT, 710),
          (GRANDCHILD, 42): (BOOT, 720)}


class BirthStamps:
    def __init__(self):
        self.stamps = dict(BIRTHS)
        self.calls = []

    def __call__(self, pid, sequence):
        self.calls.append((pid, sequence))
        return self.stamps.get((pid, sequence))


class LateAncestryTests(unittest.TestCase):
    def setUp(self):
        self.stamps = BirthStamps()
        self.graph = _ProcessLineage(OWNER, BASE, self.stamps)

    def test_unrelated_process_burst_uses_no_birth_probes(self):
        rows = {**BASE, **TREE, PARENT: (EXPLORER, 516), 23: (999, 6)}
        self.assertEqual(self.graph.observe(rows, recover=True), set())
        self.assertEqual(self.stamps.calls, [])

    def test_late_birth_proof_can_confirm_an_already_guarded_child(self):
        rows = {**BASE, **TREE}
        birth = self.stamps.stamps.pop((CHILD, 1))
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD, GRANDCHILD})
        self.assertNotIn(1, self.graph.parent_links)
        self.stamps.stamps[(CHILD, 1)] = birth
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD, GRANDCHILD})
        self.assertEqual(self.graph.parent_links[1], 516)
        self.assertEqual(self.graph.parent_links[42], 1)

    def test_mixed_branches_keep_only_launch_descendants(self):
        rows = {**BASE, **TREE, 30: (EXPLORER, 77), 31: (30, 78)}
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD, GRANDCHILD})
        self.assertTrue(all(pid not in {30, 31} for pid, _ in self.stamps.calls))
        rows.pop(PARENT)
        rows.pop(CHILD)
        self.assertEqual(self.graph.observe(rows, recover=True), {GRANDCHILD})
        rows.pop(GRANDCHILD)
        self.assertEqual(self.graph.observe(rows, recover=True), set())

    def test_first_observed_child_of_departed_launch_parent_stays_guarded(self):
        self.graph.observe({**BASE, PARENT: TREE[PARENT]})
        rows = {**BASE, CHILD: TREE[CHILD]}
        self.assertEqual(self.graph.observe(rows, recover=True), {CHILD})
        rows[GRANDCHILD] = TREE[GRANDCHILD]
        self.assertEqual(self.graph.observe(rows, recover=True), {CHILD, GRANDCHILD})
        rows.pop(CHILD)
        self.assertEqual(self.graph.observe(rows, recover=True), {GRANDCHILD})

    def test_later_pid_appearance_cannot_reparent_preexisting_child(self):
        rows = {**BASE, CHILD: TREE[CHILD]}
        self.assertEqual(self.graph.observe(rows, recover=True), set())
        rows[PARENT] = TREE[PARENT]
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT})
        self.assertNotIn((CHILD, 1), self.stamps.calls)

    def test_replacement_parent_cannot_resolve_existing_ambiguous_launch_link(self):
        rows = {**BASE, PARENT: TREE[PARENT], CHILD: TREE[CHILD]}
        self.stamps.stamps.pop((PARENT, 516))
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD})
        rows[PARENT] = (EXPLORER, 517)
        self.stamps.stamps[(PARENT, 517)] = (BOOT, 730)
        self.assertEqual(self.graph.observe(rows, recover=True), {CHILD})

    def test_new_child_is_tied_to_reused_parent_only_if_birth_matches(self):
        self.graph.observe({**BASE, PARENT: TREE[PARENT]})
        rows = {**BASE, PARENT: (EXPLORER, 517), CHILD: TREE[CHILD]}
        # Previous linked generation and current external generation are
        # candidates. The exact external generation was older than the child.
        self.stamps.stamps[(PARENT, 517)] = (BOOT, 705)
        self.assertEqual(self.graph.observe(rows), {CHILD})
        self.assertEqual(self.graph.observe(rows, recover=True), set())
        self.assertEqual(self.graph.parent_links[1], 517)

    def test_younger_current_parent_does_not_create_a_launch_link(self):
        rows = {**BASE, PARENT: TREE[PARENT], CHILD: TREE[CHILD]}
        self.stamps.stamps[(PARENT, 516)] = (BOOT, 711)
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT})

    def test_equal_birth_values_do_not_disprove_an_ambiguous_link(self):
        rows = {**BASE, PARENT: TREE[PARENT], CHILD: TREE[CHILD]}
        self.stamps.stamps[(PARENT, 516)] = self.stamps.stamps[(CHILD, 1)]
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD})
        self.assertNotIn(1, self.graph.parent_links)

    def test_different_boot_contexts_leave_a_targeted_link_unresolved(self):
        rows = {**BASE, PARENT: TREE[PARENT], CHILD: TREE[CHILD]}
        self.stamps.stamps[(PARENT, 516)] = (BOOT + 1, 700)
        self.assertEqual(self.graph.observe(rows, recover=True), {PARENT, CHILD})
        self.assertNotIn(1, self.graph.parent_links)

    def test_pinned_exited_root_can_scope_a_newly_observed_child(self):
        rows = {**BASE, CHILD: TREE[CHILD], GRANDCHILD: TREE[GRANDCHILD]}
        self.assertEqual(self.graph.observe(rows, related={PARENT: 516}, recover=True), {CHILD, GRANDCHILD})
        rows.pop(CHILD)
        self.assertEqual(self.graph.observe(rows, related={PARENT: 516}, recover=True), {GRANDCHILD})


if __name__ == "__main__":
    unittest.main()

