"""Preview and named priority changes through fake MO2 and real fixture files."""
import json
import unittest
from unittest.mock import Mock, patch

import mcp_server
import test_bridge
from client import BridgeClient, BridgePendingError, MUTATING_ACTIONS
from mo2_codex_bridge.operations import READ_ACTIONS, WRITE_ACTIONS


class PlannedModChangeTests(unittest.TestCase):
    setUp_base = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown

    def setUp(self):
        self.setUp_base()
        self.organizer.createMod("Group_separator")
        self.organizer.createMod("C")
        self.modlist = self.organizer.ml
        self.saved_modlist = self.root / "profile" / "modlist.txt"
        self.other_profile = self.root / "OtherProfile"
        self.other_profile.mkdir()
        self.other_saved = self.other_profile / "modlist.txt"
        self.other_saved.write_bytes(b"# Fixture\n-C\n+B\n-Group_separator\n-A\n")
        self.original_order = list(self.modlist.order)
        self.original_active = set(self.modlist.active)
        self.intent = {"mod": "A", "anchor": "C", "position": "after"}

    def preview(self, **overrides):
        return self.ops.dispatch(
            "preview_mod_change", {"action": "place_mod", **self.intent, **overrides}
        )

    def params_for(self, plan, **intent_overrides):
        return {
            **self.intent,
            **intent_overrides,
            "plan_token": plan["plan_token"],
            "expected_profile": plan["expected_profile"],
            "expected_revision": plan["expected_revision"],
        }

    def apply(self, plan, **overrides):
        return self.ops.dispatch("place_mod", {**self.params_for(plan), **overrides})

    def profile_bytes(self):
        return {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in [*(self.root / "profile").glob("*.txt"), *self.other_profile.glob("*.txt")]
        }

    def assert_no_change(self, error, original_bytes):
        self.assertFalse(error.bridge_failure["changes_may_have_occurred"])
        self.assertEqual(self.modlist.order, self.original_order)
        self.assertEqual(self.profile_bytes(), original_bytes)
        self.assertEqual(list((self.root / "data" / "backups").glob("*")), [])

    def assert_saved_order(self, ascending_order):
        lines = [line for line in self.saved_modlist.read_text().splitlines() if not line.startswith("#")]
        expected = [
            ("+" if name in self.original_active else "-") + name for name in reversed(ascending_order)
        ]
        self.assertEqual(lines, expected)

    def assert_recoverable_failure(self, error):
        failure = error.bridge_failure
        self.assertTrue(failure["changes_may_have_occurred"])
        backup = self.root / "data" / "backups" / failure["backup_id"]
        self.assertTrue((backup / "before.json").is_file())
        self.assertTrue((backup / "error.json").is_file())
        self.assertFalse((backup / "result.json").exists())

    @staticmethod
    def tool_call(client, tool, action, params):
        return mcp_server.dispatch(
            {"method": "tools/call", "params": {
                "name": tool, "arguments": {"action": action, "params": params}
            }},
            client,
        )

    def test_recovery_does_not_treat_pending_placement_or_rename_as_a_read(self):
        self.assertEqual(MUTATING_ACTIONS, WRITE_ACTIONS | {'installer_choose'})
        client = BridgeClient(str(self.root / 'never-live'))
        for action in ('place_mod', 'rename_mod'):
            with self.subTest(action=action):
                client.request = Mock(side_effect=TimeoutError('lost mutation receipt'))
                with self.assertRaises(BridgePendingError) as failure:
                    client.submit(action, {}, request_id='fixture-job')
                self.assertEqual(failure.exception.job_id, 'fixture-job')
                self.assertTrue(failure.exception.failure['changes_may_have_occurred'])
                self.assertEqual(client.request.call_count, 1)

    def test_preview_is_read_only_and_matches_live_revision(self):
        saved_before = self.profile_bytes()
        snapshot = self.ops.snapshot()
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move in preview")),
            patch.object(self.modlist, "setActive", side_effect=AssertionError("Activation in preview")),
            patch.object(self.ops, "refresh", side_effect=AssertionError("Refresh in preview")),
            patch.object(self.ops, "profile_selector", side_effect=AssertionError("Profile switch in preview")),
        ):
            plan = self.preview()
        self.assertEqual(plan["action"], "place_mod")
        self.assertEqual(plan["expected_profile"], snapshot["profile"])
        self.assertEqual(plan["expected_revision"], snapshot["revision"])
        self.assertIsInstance(plan["plan_token"], str)
        self.assertTrue(plan["plan_token"])
        self.assertEqual(self.modlist.order, self.original_order)
        self.assertEqual(self.profile_bytes(), saved_before)
        self.assertFalse((self.root / "data").exists())

    def test_tool_schema_dispatch_and_full_round_trip(self):
        tools = mcp_server.dispatch({"method": "tools/list"}, None)["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        inspect = by_name["mo2_inspect"]
        change = by_name["mo2_change"]
        self.assertTrue(inspect["annotations"]["readOnlyHint"])
        self.assertIn("preview_mod_change", inspect["inputSchema"]["properties"]["action"]["enum"])
        self.assertIn("place_mod", change["inputSchema"]["properties"]["action"]["enum"])
        self.assertIn("preview_mod_change", READ_ACTIONS)
        self.assertNotIn("preview_mod_change", WRITE_ACTIONS)
        self.assertIn("place_mod", WRITE_ACTIONS)
        self.assertNotIn("place_mod", READ_ACTIONS)
        client = Mock()
        client.call.side_effect = lambda action, params, timeout: self.ops.dispatch(action, params)
        for tool, action in (("mo2_change", "preview_mod_change"), ("mo2_inspect", "place_mod")):
            with self.subTest(tool=tool, action=action), self.assertRaises(mcp_server.RPCError):
                self.tool_call(client, tool, action, {})
        client.call.assert_not_called()
        preview = self.tool_call(
            client, "mo2_inspect", "preview_mod_change", {"action": "place_mod", **self.intent}
        )
        self.assertFalse(preview["isError"])
        plan = json.loads(preview["content"][0]["text"])
        applied = self.tool_call(client, "mo2_change", "place_mod", self.params_for(plan))
        self.assertFalse(applied["isError"], applied)
        self.assert_saved_order(["B", "Group_separator", "C", "A"])

    def test_moves_higher_and_lower_without_other_profile_or_plugin_edits(self):
        before = self.profile_bytes()
        pl = self.organizer.pl
        plugin_state = (list(pl.order), set(pl.active))
        with patch.object(self.modlist, "setPriority", wraps=self.modlist.setPriority) as move:
            applied = self.apply(self.preview())
            move.assert_called_once_with("A", 3)
            self.assertTrue(applied["backup_id"])
            self.assertEqual(self.modlist.order, ["B", "Group_separator", "C", "A"])
            self.assert_saved_order(self.modlist.order)

            move.reset_mock()
            other_intent = {"mod": "A", "anchor": "B", "position": "before"}
            self.apply(self.preview(**other_intent), **other_intent)
            move.assert_called_once_with("A", 0)
        self.assertEqual(self.modlist.order, self.original_order)
        self.assertEqual(self.modlist.active, self.original_active)
        self.assertEqual((pl.order, pl.active), plugin_state)
        self.assertEqual(self.profile_bytes(), before)

    def test_adjacent_target_noop_never_invokes_native_move(self):
        before = self.profile_bytes()
        intent = {"anchor": "B", "position": "before"}
        with patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move for no-op")):
            self.apply(self.preview(**intent), **intent)
        self.assertEqual(self.modlist.order, self.original_order)
        self.assertEqual(self.profile_bytes(), before)

    def test_saved_plugin_list_drift_prevents_preview_before_native_changes(self):
        plugin_list = self.root / 'profile' / 'plugins.txt'
        plugin_list.write_bytes(b'# Fixture\nChild.esp\n')
        original = self.profile_bytes()
        with self.assertRaisesRegex(ValueError, 'Saved plugin state differs') as failure:
            self.preview()
        self.assert_no_change(failure.exception, original)

    def test_provider_switch_is_refused_before_mutation(self):
        self.organizer.getFileOrigins = lambda path: ['A', 'B']
        original = self.profile_bytes()
        with self.assertRaisesRegex(ValueError, 'could change a plugin provider') as failure:
            self.preview()
        self.assert_no_change(failure.exception, original)

    def test_expired_preview_is_rejected_with_same_live_and_saved_state(self):
        original = self.profile_bytes()
        with patch('mo2_codex_bridge.mod_change_previews.time.monotonic', return_value=1000):
            plan = self.preview()
        with (
            patch('mo2_codex_bridge.mod_change_previews.time.monotonic', return_value=1600),
            self.assertRaisesRegex(ValueError, 'preview expired') as failure,
        ):
            self.apply(plan)
        self.assert_no_change(failure.exception, original)

    def test_preview_rejects_bad_intent_before_creating_recovery(self):
        before = self.profile_bytes()
        for params in (
            {"action": "set_mod_priority"}, {"position": "below"}, {"position": True},
            {"mod": "a"}, {"mod": "Group_separator"}, {"mod": "Overwrite"},
            {"anchor": "c"}, {"anchor": "Missing"}, {"anchor": "A"},
            {"priority": 3}, {"anchor": ["C"]},
        ):
            with self.subTest(params=params), self.assertRaises((ValueError, RuntimeError)) as failure:
                self.preview(**params)
            self.assert_no_change(failure.exception, before)

    def test_token_is_bound_to_exact_intent_and_rejects_malformed_values(self):
        before = self.profile_bytes()
        plan = self.preview()
        with patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move with bad token")):
            for params in (
                {"anchor": "B"}, {"position": "before"}, {"mod": "B"},
                {"plan_token": None}, {"plan_token": True}, {"plan_token": []},
                {"plan_token": plan["plan_token"] + "x"}, {"unexpected": 1},
            ):
                with self.subTest(params=params), self.assertRaises((ValueError, RuntimeError)) as failure:
                    self.apply(plan, **params)
                self.assert_no_change(failure.exception, before)
            without_token = self.params_for(plan)
            del without_token["plan_token"]
            with self.assertRaises((ValueError, RuntimeError)) as failure:
                self.ops.dispatch("place_mod", without_token)
            self.assert_no_change(failure.exception, before)

    def test_previous_bridge_instance_cannot_authorize_apply(self):
        plan = self.preview()
        before = self.profile_bytes()
        restarted = type(self.ops)(
            self.organizer, self.ops.m, self.ops.data, self.ops.refresh,
            wait_until=self.ops.wait_until,
        )
        self.assertEqual(restarted.snapshot()["revision"], plan["expected_revision"])
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move with old token")),
            self.assertRaises((ValueError, RuntimeError)) as failure,
        ):
            restarted.dispatch("place_mod", self.params_for(plan))
        self.assert_no_change(failure.exception, before)


    def test_fresh_revision_cannot_reauthorize_an_old_plan(self):
        plan = self.preview()
        self.modlist.setActive("B", False)
        current = self.ops.snapshot()
        self.assertNotEqual(current["revision"], plan["expected_revision"])
        before = self.profile_bytes()
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move for stale plan")),
            self.assertRaises((ValueError, RuntimeError)) as failure,
        ):
            self.apply(plan, expected_revision=current["revision"])
        self.assert_no_change(failure.exception, before)

    def test_saved_change_alone_invalidates_a_plan(self):
        plan = self.preview()
        self.saved_modlist.write_bytes(b"# Fixture\n-C\n-Group_separator\n-B\n+A\n")
        self.assertEqual(self.ops.snapshot()["revision"], plan["expected_revision"])
        before = self.profile_bytes()
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move with stale saved list")),
            self.assertRaises((ValueError, RuntimeError)) as failure,
        ):
            self.apply(plan)
        self.assert_no_change(failure.exception, before)

    def test_current_profile_identity_is_required_even_with_a_fresh_revision(self):
        plan = self.preview()
        for name in ('modlist.txt', 'plugins.txt', 'loadorder.txt'):
            (self.other_profile / name).write_bytes((self.root / 'profile' / name).read_bytes())
        self.organizer.profileName = lambda: "OtherProfile"
        self.organizer.profilePath = lambda: str(self.other_profile)
        current = self.ops.snapshot()
        before = self.profile_bytes()
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move in wrong profile")),
            self.assertRaises((ValueError, RuntimeError)) as failure,
        ):
            self.apply(plan, expected_profile=current["profile"], expected_revision=current["revision"])
        self.assert_no_change(failure.exception, before)

    def test_game_guard_blocks_apply_and_preview_cannot_authorize_a_write(self):
        previous = self.preview()
        self.ops.running.add("SkyrimSE.exe")
        running_plan = self.preview()
        self.assertIsNone(running_plan["plan_token"])
        before = self.profile_bytes()
        with (
            patch.object(self.modlist, "setPriority", side_effect=AssertionError("Native move with game running")),
            self.assertRaises((ValueError, RuntimeError)) as failure,
        ):
            self.apply(
                previous,
                expected_profile=running_plan["expected_profile"],
                expected_revision=running_plan["expected_revision"],
            )
        self.assert_no_change(failure.exception, before)

    def test_waits_for_delayed_native_saved_list(self):
        plan = self.preview()
        self.modlist.defer_save = True
        observed = []

        def wait(check, timeout):
            self.assertGreater(timeout, 0)
            observed.append(check())
            self.modlist.write_files()
            observed.append(check())
            return observed[-1]

        self.ops.wait_until = wait
        with patch.object(self.modlist, "setPriority", wraps=self.modlist.setPriority) as move:
            self.apply(plan)
        self.assertEqual(observed, [False, True])
        move.assert_called_once_with("A", 3)
        self.assert_saved_order(["B", "Group_separator", "C", "A"])

    def test_timeout_reports_recovery_and_preserves_native_observed_state(self):
        plan = self.preview()
        self.modlist.defer_save = True
        before = self.profile_bytes()
        with (
            patch.object(self.modlist, "setPriority", wraps=self.modlist.setPriority) as move,
            self.assertRaises(RuntimeError) as failure,
        ):
            self.apply(plan)
        move.assert_called_once_with("A", 3)
        self.assertEqual(self.modlist.order, ["B", "Group_separator", "C", "A"])
        self.assertEqual(self.profile_bytes(), before)
        self.assert_recoverable_failure(failure.exception)

    def test_native_success_with_wrong_live_target_is_a_recoverable_failure(self):
        plan = self.preview()
        original_move = self.modlist.setPriority

        def wrong_target(name, priority):
            return original_move(name, priority - 1)

        with (
            patch.object(self.modlist, "setPriority", side_effect=wrong_target) as move,
            self.assertRaises(RuntimeError) as failure,
        ):
            self.apply(plan)
        move.assert_called_once_with("A", 3)
        self.assertEqual(self.modlist.order, ["B", "Group_separator", "A", "C"])
        self.assert_recoverable_failure(failure.exception)

    def test_profile_switch_during_saved_verification_is_never_success(self):
        plan = self.preview()
        other_before = self.other_saved.read_bytes()
        self.modlist.defer_save = True

        def wait(check, timeout):
            self.organizer.profileName = lambda: "OtherProfile"
            self.organizer.profilePath = lambda: str(self.other_profile)
            return check()

        self.ops.wait_until = wait
        with self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        self.assertEqual(self.other_saved.read_bytes(), other_before)
        self.assert_recoverable_failure(failure.exception)


    def test_other_live_activation_change_during_wait_is_never_success(self):
        plan = self.preview()
        self.modlist.defer_save = True

        def wait(check, timeout):
            self.modlist.active.discard("B")
            self.modlist.write_files()
            return check()

        self.ops.wait_until = wait
        with self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        self.assertNotIn("B", self.modlist.active)
        self.assert_recoverable_failure(failure.exception)

