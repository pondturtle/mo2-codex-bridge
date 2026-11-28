"""Read-only staged replacement forecast using disposable MO2 fixtures."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp_server import RPCError, dispatch
from mo2_codex_bridge import replacement_preview
from test_operation_fixes import OperationFixture, plugin_bytes


class ReplacementPreviewTests(OperationFixture):
    def setUp(self):
        super().setUp()
        self.stage = Path(self.organizer.createMod("Staged").absolutePath())
        self.registry["A"]["Optional.esp"] = {"masters": ["Master.esm"], "master": False, "light": False}
        self.registry["B"] = {
            "Library.esm": {"masters": ["Master.esm"], "master": True, "light": False},
            "Higher.esp": {"masters": ["Master.esm"], "master": False, "light": False},
        }
        self.refresh()
        self.organizer.pl.active.discard("Optional.esp")
        self.organizer.pl.refresh_indexes()
        self.organizer.pl.write_files()
        self.organizer.resolvePath = lambda name: str(
            self.root / "mods" / self.available[name]["origin"] / name)
        self.put("Child.esp", ("Master.esm", "Library.esm"))
        self.put("Optional.esp")

    def put(self, name, masters=("Master.esm",), **flags):
        (self.stage / name).write_bytes(plugin_bytes(masters=masters, **flags))

    def preview(self, previous="A", replacement="Staged"):
        return self.ops.dispatch("preview_mod_replacement", {"mod": previous, "replacement": replacement})

    @staticmethod
    def rows(result):
        return {(row["staged"] or row["before"])["name"]: row for row in result["plugins"]}

    def test_preview_reports_providers_masters_and_disabled_optional_state_through_mcp(self):
        self.put("Added.esp")
        self.put("Higher.esp", ("Master.esm", "Library.esm"))
        self.ops.running = {"SkyrimSE.exe"}
        before = self.ops.snapshot()
        saved = {path.name: path.read_bytes() for path in (self.root / "profile").iterdir()}
        client = SimpleNamespace(call=lambda action, params, timeout: self.ops.dispatch(action, params))
        request = {"method": "tools/call", "params": {"name": "mo2_inspect", "arguments": {
            "action": "preview_mod_replacement", "params": {"mod": "A", "replacement": "Staged"}}}}
        with (
            patch.object(self.organizer, "installMod", side_effect=AssertionError("preview called install")),
            patch.object(self.organizer.ml, "setActive", side_effect=AssertionError("preview toggled a mod")),
            patch.object(self.organizer.ml, "setPriority", side_effect=AssertionError("preview moved a mod")),
            patch.object(self.organizer.pl, "setState", side_effect=AssertionError("preview toggled a plugin")),
        ):
            response = dispatch(request, client)
        self.assertFalse(response["isError"], response)
        result = json.loads(response["content"][0]["text"])
        self.assertEqual(result["status"], "modeled", result["blockers"])
        self.assertEqual(result["revision"], before["revision"])
        self.assertEqual(result["projected_priority"], 0)
        self.assertNotIn("plan_token", result)
        rows = self.rows(result)
        self.assertEqual(rows["Child.esp"]["before"]["origin"], "A")
        self.assertEqual(rows["Child.esp"]["after"]["origin"], "Staged")
        self.assertEqual(rows["Child.esp"]["masters_added"], ["Library.esm"])
        self.assertFalse(rows["Optional.esp"]["after"]["active"])
        self.assertIsNone(rows["Added.esp"]["before"])
        self.assertIsNone(rows["Added.esp"]["after"]["active"])
        self.assertEqual(rows["Higher.esp"]["staged"]["masters"], ["Master.esm", "Library.esm"])
        self.assertEqual(rows["Higher.esp"]["after"]["origin"], "B")
        self.assertEqual(rows["Higher.esp"]["masters_added"], [])
        self.assertEqual(self.ops.snapshot(), before)
        self.assertEqual({path.name: path.read_bytes() for path in (self.root / "profile").iterdir()}, saved)
        self.assertFalse(self.ops.data.exists())
        request["params"]["name"] = "mo2_change"
        with self.assertRaises(RPCError):
            dispatch(request, client)

    def test_staged_mod_already_above_previous_keeps_same_relative_placement(self):
        self.organizer.ml.setPriority("Staged", 0)
        self.assertEqual(self.preview()["projected_priority"], 0)

    def test_omitted_old_plugin_reports_blocker_without_guessing_fallback(self):
        (self.stage / "Optional.esp").unlink()
        result = self.preview()
        self.assertEqual(result["previous_plugins_missing_from_replacement"], ["Optional.esp"])
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(all(row["after"] is None for row in result["plugins"]))
        self.assertTrue(any("omit" in item["reason"] for item in result["blockers"]))

    def test_missing_master_remains_a_blocker_even_for_new_plugin_defaults(self):
        self.put("Added.esp", ("Unknown.esm",))
        result = self.preview()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any("missing or disabled master" in row["reason"] for row in result["blockers"]))

    def test_ambiguous_current_provider_refuses_to_predict_switch(self):
        original = self.organizer.getFileOrigins
        self.organizer.getFileOrigins = lambda name: ["Other", "A"] if name == "Child.esp" else original(name)
        result = self.preview()
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(any(row["stage"] == "current_provider" for row in result["blockers"]))

    def test_winner_path_must_match_managed_origin(self):
        resolve = self.organizer.resolvePath
        wrong = str(self.root / "mods/B/Child.esp")
        self.organizer.resolvePath = lambda name: wrong if name == "Child.esp" else resolve(name)
        result = self.preview()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"][0]["plugin"], "Child.esp")

    def test_disabled_mod_cannot_be_current_provider(self):
        self.available["Higher.esp"]["origin"] = "Staged"
        with self.assertRaisesRegex(ValueError, "disabled replacement"):
            self.preview()

    def test_both_enabled_plan_must_pass_before_projecting_final_state(self):
        planner = replacement_preview.plan_activation

        def simulate(ops, before, current, name, headers, **options):
            if options["disabling"] is None:
                raise ValueError("Fixture intermediate conflict")
            return planner(ops, before, current, name, headers, **options)

        with patch.object(replacement_preview, "plan_activation", side_effect=simulate):
            result = self.preview()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["blockers"], [{"stage": "both_enabled", "reason": "Fixture intermediate conflict"}])
        self.assertTrue(all(row["after"] is None for row in result["plugins"]))

    def test_malformed_master_names_do_not_escape_the_preview(self):
        self.put("Child.esp", ("x" * 256 + ".esm",))
        with self.assertRaisesRegex(ValueError, "invalid or excessive master"):
            self.preview()

    def test_inconsistent_saved_profile_refuses_forecast(self):
        (self.root / "profile" / "modlist.txt").write_text("+Staged\n+B\n+A\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "saved modlist"):
            self.preview()

    def test_changed_staged_header_refuses_forecast(self):
        reader = replacement_preview.read_headers
        calls = 0

        def drift(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.put("Child.esp")
            return reader(*args, **kwargs)

        with patch.object(replacement_preview, "read_headers", side_effect=drift):
            with self.assertRaisesRegex(ValueError, "evidence changed"):
                self.preview()

    def test_wrong_case_or_active_replacement_refused(self):
        with self.assertRaises(ValueError):
            self.preview(replacement="staged")
        with self.assertRaisesRegex(ValueError, "different"):
            self.preview(replacement="A")
        with self.assertRaisesRegex(ValueError, "staged replacement disabled"):
            self.preview(replacement="B")

    def test_asset_only_replacement_has_no_plugin_claims(self):
        self.organizer.createMod("Assets only")
        result = self.preview(previous="B", replacement="Assets only")
        self.assertEqual(result["status"], "blocked")
        self.registry["B"].clear()
        self.refresh()
        self.organizer.pl.write_files()
        result = self.preview(previous="B", replacement="Assets only")
        self.assertEqual(result["status"], "modeled", result["blockers"])
        self.assertEqual(result["plugins"], [])

    def test_replacement_hard_link_is_refused_before_read(self):
        path = self.stage / "Optional.esp"
        try:
            os.link(path, self.root / "hardlink.esp")
        except OSError as error:
            self.skipTest(str(error))
        with self.assertRaisesRegex(ValueError, "hard-linked replacement plugin"):
            self.preview()

