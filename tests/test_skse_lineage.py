"""Independent pure process-generation graph checks; no Windows or MO2 calls."""
import unittest

from mo2_codex_bridge.running_apps import _ProcessLineage


OWNER = 10


def baseline():
    # Opaque IDs intentionally do not sort in birth order. Explorer/service are
    # outside MO2; 11 and 12 are preexisting descendants of its exact owner.
    return {1: (0, 9000), 2: (1, 2), OWNER: (1, 700),
            11: (OWNER, 88), 12: (11, 500)}


class ProcessLineageTests(unittest.TestCase):
    def setUp(self):
        self.base = baseline()
        self.graph = _ProcessLineage(OWNER, self.base)

    def rows(self, extra=None, absent=()):
        result = {p: v for p, v in self.base.items() if p not in absent}
        result.update(extra or {})
        return result

    def test_unchanged_baseline_is_not_new_even_under_owner(self):
        self.assertEqual(self.graph.observe(self.rows()), set())

    def test_direct_external_child_is_certified(self):
        self.assertEqual(self.graph.observe(self.rows({20: (1, 3)})), set())

    def test_child_of_a_stable_external_service_is_certified(self):
        self.assertEqual(self.graph.observe(self.rows({20: (2, 3)})), set())

    def test_later_external_child_and_grandchild_are_certified(self):
        first = {20: (1, 3)}
        self.assertEqual(self.graph.observe(self.rows(first)), set())
        second = dict(first)
        second[21] = (20, 1)
        self.assertEqual(self.graph.observe(self.rows(second)), set())
        third = dict(second)
        third[22] = (21, 42)
        self.assertEqual(self.graph.observe(self.rows(third)), set())

    def test_cofirst_unrelated_parent_and_child_do_not_link_to_launch(self):
        current = self.rows({20: (1, 3), 21: (20, 1)})
        self.assertEqual(self.graph.observe(current), set())

    def test_repeating_cofirst_snapshot_cannot_reconstruct_birth_order(self):
        current = self.rows({20: (1, 3), 21: (20, 1)})
        for _ in range(4):
            self.assertEqual(self.graph.observe(current), set())

    def test_uncertified_child_cannot_certify_a_later_grandchild(self):
        current = self.rows({20: (1, 3), 21: (20, 1)})
        self.assertEqual(self.graph.observe(current), set())
        current[22] = (21, 42)
        self.assertEqual(self.graph.observe(current), set())

    def test_owner_cannot_certify_new_children(self):
        self.assertEqual(self.graph.observe(self.rows({20: (OWNER, 3)})), {20})

    def test_owner_direct_baseline_child_cannot_certify(self):
        self.assertEqual(self.graph.observe(self.rows({20: (11, 3)})), {20})

    def test_owner_baseline_grandchild_cannot_certify(self):
        self.assertEqual(self.graph.observe(self.rows({20: (12, 3)})), {20})

    def test_new_unsafe_branch_never_gains_certification_by_waiting(self):
        first = {20: (OWNER, 3)}
        self.assertEqual(self.graph.observe(self.rows(first)), {20})
        first[21] = (20, 1)
        self.assertEqual(self.graph.observe(self.rows(first)), {20, 21})
        self.assertEqual(self.graph.observe(self.rows(first)), {20, 21})

    def test_unconnected_survivor_does_not_block_among_external_children(self):
        extra = {p: (1, 100 + p) for p in range(20, 70)}
        extra[80] = (999, 77)
        self.assertEqual(self.graph.observe(self.rows(extra)), set())

    def test_new_orphan_is_unknown(self):
        self.assertEqual(self.graph.observe(self.rows({20: (999, 3)})), set())

    def test_new_zero_parent_is_unknown(self):
        self.assertEqual(self.graph.observe(self.rows({20: (0, 3)})), set())

    def test_baseline_orphan_itself_does_not_count_as_new(self):
        initial = self.rows({30: (999, 77)})
        graph = _ProcessLineage(OWNER, initial)
        self.assertEqual(graph.observe(initial), set())

    def test_exact_prelaunch_orphan_can_certify_a_new_external_child(self):
        initial = self.rows({30: (999, 77)})
        graph = _ProcessLineage(OWNER, initial)
        current = dict(initial)
        current[31] = (30, 4)
        # This exact baseline generation predates the future SKSE session;
        # missing historical ancestry does not make it that session's child.
        self.assertEqual(graph.observe(current), set())

    def test_new_cycle_is_never_certified(self):
        current = self.rows({20: (21, 3), 21: (20, 1)})
        self.assertEqual(self.graph.observe(current), set())
        self.assertEqual(self.graph.observe(current), set())

    def test_self_parent_is_never_certified(self):
        self.assertEqual(self.graph.observe(self.rows({20: (20, 3)})), set())

    def test_prelaunch_stale_ppid_cycle_does_not_taint_existing_generations(self):
        initial = self.rows({30: (31, 77), 31: (30, 4)})
        graph = _ProcessLineage(OWNER, initial)
        current = dict(initial)
        current[32] = (30, 9)
        # Stale prelaunch PPIDs can form a cycle despite all these exact
        # generations already predating the new SKSE root. New cycles remain
        # uncertified in the separate new-cycle tests above.
        self.assertEqual(graph.observe(current), set())

    def test_certified_child_stays_external_after_parent_exits(self):
        self.assertEqual(self.graph.observe(self.rows({20: (1, 3)})), set())
        current = self.rows({20: (1, 3), 21: (20, 1)})
        self.assertEqual(self.graph.observe(current), set())
        current.pop(20)
        self.assertEqual(self.graph.observe(current), set())
        current[22] = (21, 42)
        self.assertEqual(self.graph.observe(current), set())

    def test_child_of_a_missing_unrelated_parent_has_no_launch_link(self):
        self.assertEqual(self.graph.observe(self.rows({20: (1, 3)})), set())
        self.assertEqual(self.graph.observe(self.rows({21: (20, 1)})), set())

    def test_reused_parent_generation_does_not_rebind_a_certified_old_child(self):
        self.assertEqual(self.graph.observe(self.rows({20: (1, 3)})), set())
        old_child = {20: (1, 3), 21: (20, 1)}
        self.assertEqual(self.graph.observe(self.rows(old_child)), set())
        current = self.rows({20: (OWNER, 5), 21: (20, 1)})
        self.assertEqual(self.graph.observe(current), {20})

    def test_reused_baseline_parent_and_cofirst_child_are_not_baseline_certified(self):
        current = self.rows({1: (0, 3), 20: (1, 1)})
        self.assertEqual(self.graph.observe(current), set())

    def test_pid_reuse_is_new_even_with_same_parent_tuple(self):
        self.assertEqual(self.graph.observe(self.rows({20: (1, 3)})), set())
        self.assertEqual(self.graph.observe(self.rows()), set())
        self.assertEqual(self.graph.observe(self.rows({20: (OWNER, 1)})), {20})

    def test_mo2_child_that_exits_no_longer_blocks(self):
        self.assertEqual(self.graph.observe(self.rows({20: (OWNER, 3)})), {20})
        self.assertEqual(self.graph.observe(self.rows()), set())

    def test_same_identity_disappearing_and_reappearing_is_contradictory(self):
        self.graph.observe(self.rows({20: (1, 3)}))
        self.graph.observe(self.rows())
        with self.assertRaises((ValueError, TypeError)):
            self.graph.observe(self.rows({20: (1, 3)}))

    def test_same_identity_cannot_change_parent(self):
        self.graph.observe(self.rows({20: (1, 3)}))
        with self.assertRaises((ValueError, TypeError)):
            self.graph.observe(self.rows({20: (2, 3)}))

    def test_one_sequence_cannot_move_to_another_pid(self):
        self.graph.observe(self.rows({20: (1, 3)}))
        with self.assertRaises((ValueError, TypeError)):
            self.graph.observe(self.rows({21: (1, 3)}))

    def test_duplicate_sequence_for_distinct_pids_is_invalid(self):
        with self.assertRaises((ValueError, TypeError)):
            self.graph.observe(self.rows({20: (1, 3), 21: (1, 3)}))

    def test_owner_missing_or_replaced_is_invalid(self):
        for change in ({}, {OWNER: (1, 701)}, {OWNER: (2, 700)}):
            with self.subTest(change=change):
                graph = _ProcessLineage(OWNER, self.base)
                current = self.rows(absent={OWNER})
                current.update(change)
                with self.assertRaises((ValueError, TypeError)):
                    graph.observe(current)

    def test_malformed_identity_types_are_rejected(self):
        for pid, entry in [(True, (1, 3)), (-1, (1, 3)), (20, (True, 3)),
                           (20, (-1, 3)), (20, (1, True)), (20, (1, -1)),
                           (20, (1, "opaque")), (20, (1,))]:
            with self.subTest(pid=pid, entry=entry):
                graph = _ProcessLineage(OWNER, self.base)
                current = self.rows()
                if type(pid) is bool:
                    # bool and int keys compare equal in Python; remove the
                    # integer key so this fixture really contains a bool key.
                    current.pop(int(pid), None)
                current[pid] = entry
                with self.assertRaises((ValueError, TypeError)):
                    graph.observe(current)

    def test_input_dictionary_changes_do_not_modify_saved_baseline(self):
        supplied = self.rows()
        graph = _ProcessLineage(OWNER, supplied)
        supplied[OWNER] = (1, 701)
        self.assertEqual(graph.observe(self.rows()), set())

    def test_related_parent_detection_retracts_its_descendant_certificates(self):
        current = self.rows({20: (1, 3)})
        self.assertEqual(self.graph.observe(current), set())
        current[21] = (20, 1)
        self.assertEqual(self.graph.observe(current), set())
        current[22] = (21, 42)
        self.assertEqual(self.graph.observe(current), set())
        self.assertEqual(self.graph.observe(current, related={20: 3}), {20, 21, 22})
        current.pop(20)
        self.assertEqual(self.graph.observe(current), {21, 22})

    def test_baseline_parent_related_detection_retracts_only_new_children(self):
        current = self.rows({20: (1, 3)})
        self.assertEqual(self.graph.observe(current), set())
        self.assertEqual(self.graph.observe(current, related={1: 9000}), {20})

    def test_related_detection_also_covers_a_historical_parent_generation(self):
        current = self.rows({20: (1, 3)})
        self.graph.observe(current)
        current[21] = (20, 1)
        self.graph.observe(current)
        current.pop(20)
        self.assertEqual(self.graph.observe(current), set())
        self.assertEqual(self.graph.observe(current, related={20: 3}), {21})

    def test_sequence_numeric_order_does_not_decide_ancestry(self):
        for sequence in (1, 5, 2**63 + 7):
            with self.subTest(sequence=sequence):
                graph = _ProcessLineage(OWNER, self.base)
                self.assertEqual(graph.observe(self.rows({20: (1, sequence)})), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
