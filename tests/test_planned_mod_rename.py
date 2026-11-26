"""Shared-folder rename integration using disposable MO2 model and disk fixtures."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mcp_server
import test_bridge


class PlannedModRenameTests(unittest.TestCase):
    setUp_base = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    native_target = "mo2_codex_bridge.mod_rename.rename_in_mod_model"

    def setUp(self):
        self.setUp_base()
        self.ml = self.organizer.ml
        self.pl = self.organizer.pl
        self.organizer.appVersion = lambda: SimpleNamespace(displayString=lambda *_: "2.5.2")
        self.profiles = self.root / "profiles"
        self.profiles.mkdir()
        self.current = self.profiles / "Current"
        (self.root / "profile").rename(self.current)
        self.ml.profile = self.pl.profile = self.current
        self.organizer.profileName = lambda: self.current.name
        self.organizer.profilePath = lambda: str(self.current)
        self.secondary = self.profiles / "Secondary"
        self.secondary.mkdir()
        (self.secondary / "modlist.txt").write_bytes(b"\xef\xbb\xbf# Native fixture\r\n-A\r\n+B\r\n")
        (self.secondary / "plugins.txt").write_bytes(b"# Fixture\nChild.esp\n")
        (self.secondary / "loadorder.txt").write_bytes(b"# Fixture\nMaster.esm\nChild.esp\n")
        self.unaffected = self.profiles / "Unrelated"
        self.unaffected.mkdir()
        (self.unaffected / "modlist.txt").write_bytes(b"# Fixture\n+B\n")

        self.old_name = "A"
        self.new_name = "A Updated"
        self.mods = self.root / "mods"
        self.original = self.mods / self.old_name
        self.destination = self.mods / self.new_name
        (self.original / "Child.esp").write_bytes(b"Disposable plugin bytes")
        (self.original / "nested").mkdir()
        (self.original / "nested" / "settings.ini").write_bytes(b"[Fixture]\r\nKey=Value\r\n")
        (self.original / "meta.ini").write_bytes(b"[General]\r\nversion=1.0\r\n")
        (self.mods / "B" / "other.txt").write_bytes(b"Independent file")
        self.child_provider = self.old_name
        self.pl.origin = lambda name: self.child_provider if name == "Child.esp" else "Data"
        self.organizer.getFileOrigins = lambda path: [self.child_provider] if path == "Child.esp" else ["Data"]
        self.organizer.resolvePath = lambda path: str(self.mods / self.child_provider / path)
        self.intent = {"mod": self.old_name, "new_name": self.new_name}

    def preview(self, **overrides):
        return self.ops.dispatch(
            "preview_mod_change", {"action": "rename_mod", **self.intent, **overrides}
        )

    def apply_params(self, plan):
        return {
            **self.intent,
            "plan_token": plan["plan_token"],
            "expected_profile": plan["expected_profile"],
            "expected_revision": plan["expected_revision"],
            "acknowledge_shared_folder": True,
        }

    def apply(self, plan, **overrides):
        return self.ops.dispatch("rename_mod", {**self.apply_params(plan), **overrides})

    @staticmethod
    def rename_rows(data, old_name, new_name):
        old = old_name.encode()
        new = new_name.encode()
        return b"".join(
            row[:1] + new + row[len(old) + 1:]
            if row[:1] in b"+-*" and row[1:].rstrip(b"\r\n") == old else row
            for row in data.splitlines(keepends=True)
        )

    def fake_native_rename(self, organizer, old_name, new_name, profiles_to_skip=()):
        assert organizer is self.organizer
        assert (old_name, new_name) == (self.old_name, self.new_name)
        self.original.rename(self.destination)
        self.ml.mods.pop(old_name)
        self.ml.mods[new_name] = test_bridge.Mod(self.mods, new_name)
        self.ml.order = [new_name if name == old_name else name for name in self.ml.order]
        if old_name in self.ml.active:
            self.ml.active.remove(old_name)
            self.ml.active.add(new_name)
        self.child_provider = new_name
        for path in self.profiles.glob("*/modlist.txt"):
            if path.parent in profiles_to_skip:
                continue
            before = path.read_bytes()
            after = self.rename_rows(before, old_name, new_name)
            if before != after:
                path.write_bytes(after)
        return True

    @staticmethod
    def file_bytes(root):
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()
        }

    def assert_preflight_failure(self, error, profiles_before, mod_files_before):
        self.assertFalse(error.bridge_failure["changes_may_have_occurred"])
        self.assertEqual(self.file_bytes(self.profiles), profiles_before)
        self.assertEqual(self.file_bytes(self.mods), mod_files_before)
        self.assertEqual(self.ml.order, ["A", "B"])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list((self.root / "data" / "backups").glob("*")), [])

    def assert_uncertain_result(self, error):
        failure = error.bridge_failure
        self.assertTrue(failure["changes_may_have_occurred"])
        backup = self.root / "data" / "backups" / failure["backup_id"]
        self.assertTrue((backup / "before.json").is_file())
        self.assertTrue((backup / "error.json").is_file())
        self.assertFalse((backup / "result.json").exists())
        self.assertIsInstance(failure.get("recovery_details"), dict)

    @staticmethod
    def mcp(client, tool, action, params):
        return mcp_server.dispatch(
            {"method": "tools/call", "params": {
                "name": tool, "arguments": {"action": action, "params": params}
            }},
            client,
        )

    def test_read_only_preview_and_mcp_rename_preserve_contents_order_and_profiles(self):
        before = self.file_bytes(self.profiles)
        content = self.file_bytes(self.original)
        other_content = self.file_bytes(self.mods / "B")
        plugins = self.ops.snapshot()["plugins"]
        client = Mock()
        client.call.side_effect = lambda action, params, timeout: self.ops.dispatch(action, params)
        with patch(self.native_target, side_effect=self.fake_native_rename) as native:
            response = self.mcp(
                client, "mo2_inspect", "preview_mod_change", {"action": "rename_mod", **self.intent}
            )
            self.assertFalse(response["isError"], response)
            plan = json.loads(response["content"][0]["text"])
            self.assertEqual(plan["action"], "rename_mod")
            self.assertTrue(plan["plan_token"])
            self.assertIn("affected_profiles", plan)
            native.assert_not_called()
            self.assertEqual(self.file_bytes(self.profiles), before)
            self.assertEqual(self.file_bytes(self.original), content)
            self.assertFalse((self.root / "data").exists())
            result = self.mcp(client, "mo2_change", "rename_mod", self.apply_params(plan))
            self.assertFalse(result["isError"], result)
            native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        after = json.loads(result["content"][0]["text"])
        self.assertTrue(after["backup_id"])
        self.assertIn("affected_profiles", after)
        self.assertFalse(self.original.exists())
        self.assertEqual(self.file_bytes(self.destination), content)
        self.assertEqual(self.file_bytes(self.mods / "B"), other_content)
        self.assertEqual(self.ml.order, ["A Updated", "B"])
        self.assertEqual(self.ml.active, {"A Updated", "B"})
        after_plugins = [
            {**plugin, "origin": self.new_name} if plugin["origin"] == self.old_name else plugin
            for plugin in plugins
        ]
        self.assertEqual(self.ops.snapshot()["plugins"], after_plugins)
        self.assertEqual(self.file_bytes(self.profiles), {
            name: self.rename_rows(data, self.old_name, self.new_name)
            if name.endswith("/modlist.txt") else data
            for name, data in before.items()
        })

    def test_rename_requires_explicit_shared_folder_acknowledgement(self):
        plan = self.preview()
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        with patch(self.native_target) as native:
            params = self.apply_params(plan)
            del params["acknowledge_shared_folder"]
            rejected = [params]
            for value in (False, 1, "true", None):
                rejected.append({**self.apply_params(plan), "acknowledge_shared_folder": value})
            for attempt in rejected:
                with self.subTest(acknowledgement=attempt.get("acknowledge_shared_folder")):
                    with self.assertRaises((ValueError, RuntimeError)) as failure:
                        self.ops.dispatch("rename_mod", attempt)
                    self.assert_preflight_failure(failure.exception, before, mod_files)
        native.assert_not_called()

    def test_rename_rejects_source_mismatch_collision_and_reserved_destination(self):
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        cases = [
            {"mod": "a"}, {"mod": "Missing"},
            {"new_name": "a"}, {"new_name": "A"}, {"new_name": "b"},
            {"new_name": "Folder\\File"}, {"new_name": "COM1"}, {"new_name": "End. "},
            {"new_name": "Overwrite"}, {"new_name": "Name_separator"},
            {"new_name": "Name_backup2"}, {"new_name": "Extra  Spaces"},
        ]
        with patch(self.native_target) as native:
            for overrides in cases:
                with self.subTest(overrides=overrides), self.assertRaises((ValueError, RuntimeError)) as failure:
                    self.preview(**overrides)
                self.assert_preflight_failure(failure.exception, before, mod_files)
        native.assert_not_called()

    def test_destination_directory_collision_appearing_after_preview_blocks_apply(self):
        plan = self.preview()
        self.destination.mkdir()
        (self.destination / "Existing.txt").write_bytes(b"Another owner")
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        with patch(self.native_target) as native, self.assertRaises((ValueError, RuntimeError)) as failure:
            self.apply(plan)
        native.assert_not_called()
        self.assertFalse(failure.exception.bridge_failure["changes_may_have_occurred"])
        self.assertEqual(self.file_bytes(self.profiles), before)
        self.assertEqual(self.file_bytes(self.mods), mod_files)
        self.assertEqual(self.ml.order, ["A", "B"])

    def test_source_content_change_after_preview_invalidates_plan(self):
        plan = self.preview()
        self.assertEqual(self.ops.snapshot()["revision"], plan["expected_revision"])
        settings = self.original / "nested" / "settings.ini"
        settings.write_bytes(b"[Fixture]\r\nKey=Other\r\n")
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        self.assertEqual(self.ops.snapshot()["revision"], plan["expected_revision"])
        with patch(self.native_target) as native, self.assertRaises((ValueError, RuntimeError)) as failure:
            self.apply(plan)
        native.assert_not_called()
        self.assert_preflight_failure(failure.exception, before, mod_files)

    def test_saved_other_profile_change_invalidates_plan(self):
        plan = self.preview()
        path = self.secondary / "modlist.txt"
        path.write_bytes(path.read_bytes().replace(b"-A", b"+A"))
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        self.assertEqual(self.ops.snapshot()["revision"], plan["expected_revision"])
        with patch(self.native_target) as native, self.assertRaises((ValueError, RuntimeError)) as failure:
            self.apply(plan)
        native.assert_not_called()
        self.assert_preflight_failure(failure.exception, before, mod_files)

    def test_token_from_named_placement_cannot_authorize_rename(self):
        placement = self.ops.dispatch(
            "preview_mod_change", {"action": "place_mod", "mod": "A", "anchor": "B", "position": "after"}
        )
        self.assertTrue(placement["plan_token"])
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        with patch(self.native_target) as native, self.assertRaises((ValueError, RuntimeError)) as failure:
            self.apply(placement)
        native.assert_not_called()
        self.assert_preflight_failure(failure.exception, before, mod_files)

    def test_unchanged_native_state_is_never_success_even_if_native_returns_true(self):
        plan = self.preview()
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        with patch(self.native_target, return_value=True) as native, self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertEqual(self.file_bytes(self.profiles), before)
        self.assertEqual(self.file_bytes(self.mods), mod_files)
        self.assertEqual(self.ml.order, ["A", "B"])
        self.assertTrue(failure.exception.bridge_failure["backup_id"])

    def test_explicit_native_false_is_never_success_and_is_never_retried(self):
        plan = self.preview()
        before = self.file_bytes(self.profiles)
        mod_files = self.file_bytes(self.mods)
        with patch(self.native_target, return_value=False) as native, self.assertRaises((ValueError, RuntimeError)):
            self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertEqual(self.file_bytes(self.profiles), before)
        self.assertEqual(self.file_bytes(self.mods), mod_files)

    def test_preview_flags_effective_pglite_exact_name_and_never_edits_rules(self):
        relative = 'SKSE/Plugins/PGLite/modrules.json'
        path = self.mods / 'B' / relative
        path.parent.mkdir(parents=True)
        rules = b'{"A":{"priority":42},"a":{"priority":99},"A Updated":{"enabled":true}}'
        path.write_bytes(rules)
        original_resolve = self.organizer.resolvePath
        self.organizer.resolvePath = lambda name: str(path) if name == relative else original_resolve(name)
        plan = self.preview()
        report = plan['external_references']['pglite']
        self.assertEqual(report['status'], 'checked')
        self.assertTrue(report['exact_old_name'])
        self.assertEqual(report['old_name_case_aliases'], ['a'])
        self.assertTrue(report['destination_name_in_use'])
        with patch(self.native_target, side_effect=self.fake_native_rename):
            self.apply(plan)
        self.assertEqual(path.read_bytes(), rules)

    def test_refreshes_cached_plugin_origin_only_after_saved_rename_is_confirmed(self):
        plan = self.preview()

        def edit(*args):
            result = self.fake_native_rename(*args)
            self.pl.origin = lambda name: self.old_name if name == 'Child.esp' else 'Data'
            return result

        def refresh():
            self.assertIn(self.new_name, (self.secondary / 'modlist.txt').read_text(encoding='utf-8-sig'))
            self.pl.origin = lambda name: self.new_name if name == 'Child.esp' else 'Data'

        self.ops.refresh = Mock(side_effect=refresh)
        with patch(self.native_target, side_effect=edit):
            result = self.apply(plan)
        self.ops.refresh.assert_called_once_with()
        self.assertTrue(result['plugin_state_verified'])

    def test_saved_profiles_may_complete_during_native_wait(self):
        plan = self.preview()
        secondary_list = self.secondary / "modlist.txt"
        old_bytes = secondary_list.read_bytes()
        expected_bytes = self.rename_rows(old_bytes, self.old_name, self.new_name)
        observed = []

        def wait(check, timeout):
            self.assertGreater(timeout, 0)
            observed.append(check())
            secondary_list.write_bytes(expected_bytes)
            observed.append(check())
            return observed[-1]

        def edit(*args):
            return self.fake_native_rename(*args, profiles_to_skip=(self.secondary,))

        self.ops.wait_until = wait
        with patch(self.native_target, side_effect=edit) as native:
            result = self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertEqual(observed, [False, True])
        self.assertEqual(secondary_list.read_bytes(), expected_bytes)
        self.assertTrue(result["backup_id"])


    def test_partial_profile_rewrite_is_never_success_and_preserves_recovery(self):
        plan = self.preview()
        source_manifest = self.file_bytes(self.original)
        secondary_before = (self.secondary / "modlist.txt").read_bytes()

        def edit(*args):
            return self.fake_native_rename(*args, profiles_to_skip=(self.secondary,))

        with patch(self.native_target, side_effect=edit) as native, self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertFalse(self.original.exists())
        self.assertEqual(self.file_bytes(self.destination), source_manifest)
        self.assertEqual((self.secondary / "modlist.txt").read_bytes(), secondary_before)
        self.assert_uncertain_result(failure.exception)

    def test_native_error_after_partial_rename_is_reported_as_uncertain(self):
        plan = self.preview()

        def edit(*args):
            self.fake_native_rename(*args, profiles_to_skip=(self.secondary,))
            raise RuntimeError("Injected failure after the native folder rename")

        with patch(self.native_target, side_effect=edit) as native, self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertFalse(self.original.exists())
        self.assertTrue(self.destination.is_dir())
        self.assert_uncertain_result(failure.exception)

    def test_unrelated_plugin_activation_mutation_is_never_success(self):
        plan = self.preview()

        def edit(*args):
            self.fake_native_rename(*args)
            self.pl.active.discard("Child.esp")
            self.pl.refresh_indexes()
            self.pl.write_files()
            return True

        with patch(self.native_target, side_effect=edit) as native, self.assertRaises(RuntimeError) as failure:
            self.apply(plan)
        native.assert_called_once_with(self.organizer, self.old_name, self.new_name)
        self.assertNotIn("Child.esp", self.pl.active)
        self.assert_uncertain_result(failure.exception)

