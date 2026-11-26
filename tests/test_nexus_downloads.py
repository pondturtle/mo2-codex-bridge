"""Offline workflow tests using a disposable MO2 model and native sidecars."""
import json
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from mo2_codex_bridge.nexus_downloads import NexusDownloads
from mo2_codex_bridge.operations import Operations, READ_ACTIONS, WRITE_ACTIONS
from client import MUTATING_ACTIONS
from mcp_server import READ, WRITE, dispatch as mcp_dispatch
from test_bridge import ModState, Organizer, PluginState


MOD_URL = "https://www.nexusmods.com/skyrimspecialedition/mods/91"
FILE_URL = MOD_URL + "?tab=files&file_id=42"
CATALOG = {"files": [
    {"file_id": 41, "name": "Same name", "version": "1", "category_id": 4, "mod_manager_download": True},
    {"file_id": 42, "name": "Same name", "version": "2", "category_id": 1, "mod_manager_download": True},
    {"file_id": 43, "name": "Optional", "version": "2", "category_id": 3, "mod_manager_download": False},
]}


class InlineExecutor:
    def submit(self, function, *args, **kwargs):
        future = Future()
        try:
            future.set_result(function(*args, **kwargs))
        except Exception as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        pass


class FakeNative:
    def __init__(self):
        self.requests = []
        self.queued = []
        self.category = 1
        self.disallow = False
        self.crash = False
        self.delayed = None
        self.idle = True
        self.single_instance = True

    def download_queue_idle(self, game):
        return game == "skyrimspecialedition" and self.idle

    def only_live_mo2_instance(self):
        return self.single_instance

    def request_file(self, game, mod_id, file_id, success, failure):
        self.requests.append((game, mod_id, file_id))
        raw = {"file_id": file_id, "category_id": self.category, "name": "Archive", "size_in_bytes": 7}
        if self.delayed is None:
            success(raw)
        else:
            self.delayed.append(lambda: success(raw))
        return lambda: None

    def prepare_queue(self, game, mod_id, file_id):
        if self.disallow:
            raise RuntimeError("Premium required")

        def queue():
            self.queued.append((game, mod_id, file_id))
            if self.crash:
                raise RuntimeError("arbitrary response that must stay private")

        return queue


class DownloadsFixture(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.organizer = Organizer(self.root)
        self.organizer.managedGame = lambda: SimpleNamespace(
            gameShortName=lambda: "SkyrimSE", gameNexusName=lambda: "skyrimspecialedition",
            primaryPlugins=lambda: ["Master.esm"],
        )
        self.organizer.appVersion = lambda: SimpleNamespace(displayString=lambda *_: "2.5.2")
        self.downloads = Path(self.organizer.downloadsPath())
        self.data = self.root / "data"
        self.native = FakeNative()
        self.bridge = self.open_bridge()

    def open_bridge(self, **kwargs):
        native = kwargs.pop("native", self.native)
        engine = NexusDownloads(self.organizer, self.data, native=native, executor=InlineExecutor(),
                                catalog=lambda game, mod: CATALOG, **kwargs)
        self.addCleanup(engine.close)
        return engine

    def start(self, *, mode="manual", url=FILE_URL, bridge=None, revalidate=lambda: None, **params):
        engine = self.bridge if bridge is None else bridge
        return engine.start({"url": url, "mode": mode, **params}, revalidate=revalidate)

    def status(self, download_id, bridge=None):
        engine = self.bridge if bridge is None else bridge
        p = {"download_id": download_id}
        self.assertEqual(engine.status(p)["status"], "checking_archive")
        return engine.status(p)

    def retry(self, download_id, *, engine=None, revalidate=lambda: None):
        conn = self.bridge if engine is None else engine
        return conn.retry({"download_id": download_id, "expected_profile": "Test",
                           "expected_revision": "current"}, revalidate=revalidate)

    def make_download(self, basename, *, file_id=42, game="SkyrimSE", mod_id=91, incomplete=False):
        target = self.downloads / (basename + (".unfinished" if incomplete else ""))
        target.write_bytes(b"archive")
        Path(str(target) + ".meta").write_text(
            f"[General]\nrepository=Nexus\ngameName={game}\nmodID={mod_id}\nfileID={file_id}\n"
            f"paused={'true' if incomplete else 'false'}\nremoved=false\n"
            "url=private-url-that-must-never-leave-metadata\n", encoding="utf-8")
        return target

    def test_public_catalog_pages_and_consistent_exact_links(self):
        first = self.bridge.files({"url": MOD_URL, "limit": 1})
        self.assertEqual(first["status"], "available")
        self.assertEqual((first["total"], first["next_offset"]), (3, 1))
        second = self.bridge.files({"url": MOD_URL, "offset": 1, "limit": 1})
        self.assertEqual(second["files"][0]["url"], FILE_URL)
        self.assertEqual(second["files"][0]["version"], "2")
        self.assertEqual(self.native.requests, [])

    def test_catalog_is_not_a_download_and_exact_selection_is_required(self):
        with self.assertRaisesRegex(ValueError, "one exact"):
            self.start(url=MOD_URL)
        with self.assertRaises(ValueError):
            self.start(url=FILE_URL, file_id=41)
        with self.assertRaises(ValueError):
            self.start(url=FILE_URL.replace("skyrimspecialedition", "skyrim"))
        self.assertFalse(self.bridge.store.exists())

    def test_catalog_failure_can_only_supply_the_manual_nexus_page(self):
        def unavailable(*_):
            raise RuntimeError("discard any raw response")

        self.bridge.catalog = unavailable
        self.assertEqual(self.bridge.files({"url": MOD_URL}), {
            "status": "unavailable", "game": "skyrimspecialedition", "mod_id": 91,
            "file_id": None, "files_url": MOD_URL + "?tab=files",
            "message": "Public Nexus file metadata is unavailable. Use the Nexus Files page.",
        })
        with self.assertRaisesRegex(ValueError, "manual"):
            self.start(mode="premium")
        self.assertFalse(self.native.queued)
        self.assertEqual(self.start()["status"], "awaiting_browser")

    def test_manual_only_returns_canonical_browser_link_and_stable_id(self):
        first = self.start()
        other = self.start(url=MOD_URL, file_id=42, bridge=self.open_bridge())
        self.assertEqual(first["status"], "awaiting_browser")
        self.assertEqual(first["url"], FILE_URL + "&nmm=1")
        self.assertEqual(other["download_id"], first["download_id"])
        self.assertEqual(other["status"], "already_tracking")
        self.assertNotIn("url", other)
        self.assertEqual(self.native.queued, [])
        self.assertEqual(self.native.requests, [])

    def test_premium_only_submits_once_across_restart_and_mode_changes(self):
        first = self.start(mode="premium")
        second = self.start(mode="premium", bridge=self.open_bridge(), url=MOD_URL, file_id=42)
        third = self.start(mode="manual", bridge=self.open_bridge())
        self.assertEqual(first["status"], "tracking_native_request")
        self.assertEqual(first["download_id"], second["download_id"])
        self.assertEqual(third["mode"], "premium")
        self.assertEqual(self.native.queued, [("skyrimspecialedition", 91, 42)])
        self.assertEqual(self.native.requests, self.native.queued)

    def test_same_file_is_reserved_once_across_competing_instances(self):
        handles = [self.open_bridge(native=FakeNative()) for _ in range(2)]
        both = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as parallel:
            rows = list(parallel.map(
                lambda bridge: self.start(mode="premium", bridge=bridge, revalidate=lambda: both.wait(5)),
                handles,
            ))
        self.assertEqual(rows[0]["download_id"], rows[1]["download_id"])
        self.assertEqual(sum(len(bridge.native.queued) for bridge in handles), 1)

    def test_native_exception_preserves_reservation_and_does_not_leak_error(self):
        self.native.crash = True
        first = self.start(mode="premium")
        self.assertEqual(first["status"], "submission_uncertain")
        self.assertNotIn("arbitrary", str(first))
        second = self.start(mode="premium", bridge=self.open_bridge())
        self.assertEqual(first["download_id"], second["download_id"])
        self.assertEqual(len(self.native.queued), 1)

    def test_disallowed_account_category_or_stale_profile_does_not_reserve(self):
        self.native.disallow = True
        with self.assertRaisesRegex(RuntimeError, "Premium required"):
            self.start(mode="premium")
        self.native.disallow = False
        self.native.category = 6
        with self.assertRaisesRegex(ValueError, "category"):
            self.start(mode="premium", bridge=self.open_bridge())
        with self.assertRaisesRegex(ValueError, "stale"):
            self.start(revalidate=lambda: self.fail_stale())
        self.assertFalse(self.bridge.store.exists())
        self.assertFalse(self.native.queued)

    def test_nexus_ui_file_with_false_public_flag_can_reach_native_premium_queue(self):
        self.native.category = 3
        result = self.start(mode="premium", file_id=43, url=MOD_URL)
        self.assertEqual(result["status"], "tracking_native_request")
        self.assertEqual(self.native.queued, [("skyrimspecialedition", 91, 43)])

    def test_mismatched_native_category_still_never_enters_native_queue(self):
        self.native.category = 1
        with self.assertRaisesRegex(ValueError, "did not confirm the same file category"):
            self.start(mode="premium", file_id=43, url=MOD_URL)
        self.assertFalse(self.native.queued)
        self.assertFalse(self.bridge.store.exists())

    @staticmethod
    def fail_stale():
        raise ValueError("stale")

    def test_delayed_native_metadata_never_queues_by_itself(self):
        self.native.delayed = []
        first = self.start(mode="premium")
        self.assertEqual(first["status"], "file_lookup_pending")
        self.native.delayed.pop()()
        self.assertEqual(self.native.queued, [])
        result = self.start(mode="premium")
        self.assertEqual(result["download_id"], first["download_id"])
        self.assertEqual(len(self.native.queued), 1)

    def test_existing_exact_transfer_is_reported_without_submission(self):
        self.make_download("some-existing.zip", incomplete=True)
        first = self.start(mode="premium")
        self.assertEqual(first["status"], "preexisting")
        self.assertEqual(self.native.queued, [])

    def test_interruption_and_similar_names_return_only_exact_completed_file(self):
        start = self.start()
        self.make_download("similar (1).zip", file_id=43)
        self.make_download("similar.zip", incomplete=True)
        waiting = self.status(start["download_id"])
        self.assertEqual(waiting["status"], "incomplete")
        self.assertNotIn("archive", waiting)
        (self.downloads / "similar.zip.unfinished").unlink()
        path = self.make_download("similar.zip")
        resumed = self.status(start["download_id"], bridge=self.open_bridge())
        self.assertEqual(resumed["status"], "verified")
        self.assertEqual(Path(resumed["archive"]["path"]), path)
        self.assertEqual(resumed["archive"]["bytes"], 7)
        self.assertRegex(resumed["archive"]["sha256"], r"^[a-f0-9]{64}$")
        self.assertNotIn("_proof", resumed)
        self.assertNotIn("url", resumed)

    def test_archive_replaced_after_worker_result_is_not_returned(self):
        start = self.start()
        path = self.make_download("mutable.zip")
        params = {"download_id": start["download_id"]}
        self.assertEqual(self.bridge.status(params)["status"], "checking_archive")
        path.write_bytes(b"new demo data")
        result = self.bridge.status(params)
        self.assertEqual(result["status"], "unverified")
        self.assertNotIn("archive", result)

    def test_corrupt_durable_claim_and_new_download_root_are_refused(self):
        result = self.start()
        download_id = result["download_id"]
        other = self.root / "other-downloads"
        other.mkdir()
        default_path = self.organizer.downloadsPath
        self.organizer.downloadsPath = lambda: str(other)
        with self.assertRaisesRegex(ValueError, "another managed game"):
            self.start()
        self.organizer.downloadsPath = default_path
        self.bridge._receipt_path(download_id).write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unreadable"):
            self.start(mode="premium")
        self.assertFalse(self.native.queued)

    def test_dispatch_honors_write_preflight_and_creates_no_mod_backup(self):
        mobase = SimpleNamespace(ModState=ModState, PluginState=PluginState)
        ops = Operations(self.organizer, mobase, self.data, lambda: None)
        ops.nexus = self.bridge
        params = {"url": FILE_URL, "mode": "manual", "expected_profile": "Test",
                  "expected_revision": ops.snapshot()["revision"]}
        with self.assertRaisesRegex(ValueError, "Profile or state changed"):
            ops.dispatch("nexus_download", dict(params, expected_profile="Other"))
        ops.running.add("SkyrimSE.exe")
        params["expected_revision"] = ops.snapshot()["revision"]
        with self.assertRaisesRegex(ValueError, "Close the game"):
            ops.dispatch("nexus_download", params)
        ops.running.clear()
        params["expected_revision"] = ops.snapshot()["revision"]
        self.assertEqual(ops.dispatch("nexus_download", params)["status"], "awaiting_browser")
        self.assertFalse((self.data / "backups").exists())
        self.assertEqual(set(READ) - {"installer_state"}, READ_ACTIONS)
        self.assertEqual(set(WRITE) - {"installer_choose"}, WRITE_ACTIONS)
        self.assertIn("nexus_download", MUTATING_ACTIONS)

    def test_mcp_catalog_handoff_and_status_reach_the_normal_dispatch(self):
        ops = Operations(self.organizer, SimpleNamespace(ModState=ModState, PluginState=PluginState),
                         self.data, lambda: None)
        ops.nexus = self.bridge
        client = SimpleNamespace(call=lambda action, params, timeout: ops.dispatch(action, params))

        def invoke(tool, action, params):
            response = mcp_dispatch({"method": "tools/call", "params": {
                "name": tool, "arguments": {"action": action, "params": params},
            }}, client)
            self.assertFalse(response["isError"], response)
            return json.loads(response["content"][0]["text"])

        found = invoke("mo2_inspect", "nexus_files", {"url": MOD_URL})
        exact = found["files"][1]["url"]
        started = invoke("mo2_change", "nexus_download", {
            "url": exact, "mode": "manual", "expected_profile": "Test",
            "expected_revision": ops.snapshot()["revision"],
        })
        self.make_download("from-native-mock.zip")
        args = {"download_id": started["download_id"]}
        self.assertEqual(invoke("mo2_inspect", "nexus_download_status", args)["status"], "checking_archive")
        result = invoke("mo2_inspect", "nexus_download_status", args)
        self.assertEqual((result["status"], result["file_id"], result["archive"]["bytes"]),
                         ("verified", 42, 7))
        self.assertFalse((self.data / "backups").exists())

    def test_canceled_after_partial_has_clear_status_and_one_explicit_successor(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        part = self.make_download("fixture.zip", incomplete=True)
        self.assertEqual(self.status(download_id)["status"], "incomplete")
        part.unlink()
        Path(str(part) + ".meta").unlink()
        no_archive = self.status(download_id)
        self.assertEqual(no_archive["status"], "no_archive")
        self.assertNotIn("archive", no_archive)
        self.assertEqual(self.start(mode="premium")["download_id"], download_id)
        self.assertEqual(len(self.native.queued), 1)

        successor = self.retry(download_id)
        child_id = successor["download_id"]
        self.assertEqual(successor["previous_download_id"], download_id)
        self.assertNotEqual(child_id, download_id)
        self.assertEqual(self.retry(download_id, engine=self.open_bridge())["download_id"], child_id)
        self.assertEqual(len(self.native.queued), 2)
        stale = self.bridge.status({"download_id": download_id})
        self.assertEqual((stale["status"], stale["next_download_id"]), ("superseded", child_id))
        self.make_download("fixture.zip")
        final = self.status(child_id, bridge=self.open_bridge())
        self.assertEqual(final["status"], "verified")
        self.assertEqual(final["archive"]["bytes"], 7)
        self.assertNotIn("archive", self.bridge.status({"download_id": download_id}))

    def test_queue_evidence_is_required_even_when_there_is_no_disk_evidence(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        for sole, idle in ((True, False), (False, True)):
            with self.subTest(sole=sole, idle=idle):
                self.native.single_instance, self.native.idle = sole, idle
                self.assertEqual(self.status(download_id)["status"], "waiting")
                with self.assertRaisesRegex(ValueError, "confirmed idle"):
                    self.retry(download_id)
                self.assertIsNone(self.bridge._successor(self.bridge._read(download_id)))
        self.assertEqual(len(self.native.queued), 1)

    def test_paused_or_completed_payload_keeps_retry_blocked(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        for incomplete in (True, False):
            with self.subTest(incomplete=incomplete):
                name = "associated.zip"
                path = self.make_download(name, incomplete=incomplete)
                with self.assertRaises(ValueError):
                    self.retry(download_id)
                path.unlink()
                Path(str(path) + ".meta").unlink()
        self.assertEqual(len(self.native.queued), 1)

    def test_metadata_only_without_payload_requires_idle_and_can_reuse_old_stem(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        part = self.make_download("fresh.zip", incomplete=True)
        part.unlink()
        self.native.idle = False
        self.assertEqual(self.status(download_id)["status"], "metadata_only")
        with self.assertRaisesRegex(ValueError, "confirmed idle"):
            self.retry(download_id)
        self.native.idle = True
        self.assertEqual(self.status(download_id)["status"], "no_archive")
        next_attempt = self.retry(download_id)
        self.make_download("fresh.zip")
        self.assertEqual(self.status(next_attempt["download_id"])["status"], "verified")
        self.assertTrue(Path(str(part) + ".meta").is_file())

    def test_inflight_parent_observation_is_superseded_after_retry(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        path = self.make_download("verified.zip")
        self.assertEqual(self.bridge.status({"download_id": download_id})["status"], "checking_archive")
        path.unlink()
        Path(str(path) + ".meta").unlink()
        child = self.retry(download_id, engine=self.open_bridge())
        old = self.bridge.status({"download_id": download_id})
        self.assertEqual(old["next_download_id"], child["download_id"])
        self.assertNotIn("archive", old)

    def test_finished_file_appearing_after_worker_scan_overrides_stale_absence(self):
        download_id = self.start(mode="premium")["download_id"]
        p = {"download_id": download_id}
        self.assertEqual(self.bridge.status(p)["status"], "checking_archive")
        self.make_download("appeared.zip")
        self.assertEqual(self.bridge.status(p)["status"], "unverified")
        self.assertEqual(self.status(download_id)["status"], "verified")

    def test_in_place_metadata_change_prevents_reporting_stale_absence(self):
        download_id = self.start(mode="premium")["download_id"]
        path = self.make_download("renamed.zip", file_id=41)
        meta = Path(str(path) + ".meta")
        p = {"download_id": download_id}
        self.assertEqual(self.bridge.status(p)["status"], "checking_archive")
        self.assertTrue(meta.is_file())
        meta.write_bytes(meta.read_bytes().replace(b"fileID=41", b"fileID=42"))
        self.assertEqual(self.bridge.status(p)["status"], "unverified")
        self.assertEqual(self.status(download_id)["status"], "verified")

    def test_separate_clients_competing_for_one_retry_submit_at_most_once(self):
        original = self.start(mode="premium")
        download_id = original["download_id"]
        engines = [self.open_bridge(native=FakeNative()) for _ in range(2)]
        both = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as task:
            results = list(task.map(
                lambda engine: self.retry(download_id, engine=engine, revalidate=lambda: both.wait(5)),
                engines,
            ))
        self.assertEqual(results[0]["download_id"], results[1]["download_id"])
        self.assertEqual(sum(len(conn.native.queued) for conn in engines), 1)

    def test_queue_drift_after_successor_reservation_never_submits(self):
        original = self.start(mode="premium")
        with patch.object(self.native, "download_queue_idle", side_effect=[True, True, False]):
            blocked = self.retry(original["download_id"])
        self.assertEqual(blocked["status"], "submission_blocked")
        self.assertEqual(self.retry(original["download_id"])["download_id"], blocked["download_id"])
        self.assertEqual(len(self.native.queued), 1)

    def test_directory_drift_after_reservation_never_submits(self):
        original = self.start(mode="premium")
        reserve = self.bridge._reserve

        def after_reserve(record):
            result = reserve(record)
            self.make_download("appeared.zip")
            return result

        with patch.object(self.bridge, "_reserve", side_effect=after_reserve):
            blocked = self.retry(original["download_id"])
        self.assertEqual(blocked["status"], "submission_blocked")
        self.assertEqual(len(self.native.queued), 1)

    def test_in_place_sidecar_made_exact_after_reservation_never_submits(self):
        original = self.start(mode="premium")
        path = self.make_download("renamed.zip", file_id=41)
        meta = Path(str(path) + ".meta")
        reserve = self.bridge._reserve

        def after_reserve(record):
            result = reserve(record)
            meta.write_bytes(meta.read_bytes().replace(b"fileID=41", b"fileID=42"))
            return result

        with patch.object(self.bridge, "_reserve", side_effect=after_reserve):
            blocked = self.retry(original["download_id"])
        self.assertEqual(blocked["status"], "submission_blocked")
        self.assertEqual(len(self.native.queued), 1)

    def test_uncertain_native_retry_is_persisted_before_call_and_never_repeated(self):
        original = self.start(mode="premium")
        self.native.crash = True
        retry = self.retry(original["download_id"])
        self.assertEqual(retry["status"], "submission_uncertain")
        self.assertEqual(self.retry(original["download_id"], engine=self.open_bridge())["download_id"],
                         retry["download_id"])
        self.assertEqual(len(self.native.queued), 2)

    def test_manual_retry_preserves_mode_and_only_returns_the_new_browser_handoff(self):
        old = self.start()
        child = self.retry(old["download_id"])
        self.assertEqual(child["status"], "awaiting_browser")
        self.assertEqual(child["url"], FILE_URL + "&nmm=1")
        again = self.retry(old["download_id"])
        self.assertEqual(again["status"], "already_tracking")
        self.assertNotIn("url", again)
        self.assertEqual(self.native.queued, [])

    def test_mcp_retry_stays_on_normal_write_path_without_mod_backup(self):
        ops = Operations(self.organizer, SimpleNamespace(ModState=ModState, PluginState=PluginState),
                         self.data, lambda: None)
        ops.nexus = self.bridge
        orig = self.start(mode="premium")
        params = {"download_id": orig["download_id"], "expected_profile": "Other",
                  "expected_revision": ops.snapshot()["revision"]}
        with self.assertRaisesRegex(ValueError, "Profile or state changed"):
            ops.dispatch("nexus_download_retry", params)
        params["expected_profile"] = "Test"
        client = SimpleNamespace(call=lambda action, params, timeout: ops.dispatch(action, params))
        result = mcp_dispatch({"method": "tools/call", "params": {"name": "mo2_change", "arguments": {
            "action": "nexus_download_retry", "params": params,
        }}}, client)
        self.assertFalse(result["isError"], result)
        self.assertIn("previous_download_id", json.loads(result["content"][0]["text"]))
        self.assertIn("nexus_download_retry", MUTATING_ACTIONS)
        self.assertFalse((self.data / "backups").exists())

    def test_existing_archive_retry_is_clean_preflight_refusal_through_mcp(self):
        ops = Operations(self.organizer, SimpleNamespace(ModState=ModState, PluginState=PluginState),
                         self.data, lambda: None)
        ops.nexus = self.bridge
        original = self.start(mode="premium")
        self.make_download("completed.zip")
        params = {"download_id": original["download_id"], "expected_profile": "Test",
                  "expected_revision": ops.snapshot()["revision"]}
        client = SimpleNamespace(call=lambda action, args, timeout: ops.dispatch(action, args))
        reply = mcp_dispatch({"method": "tools/call", "params": {"name": "mo2_change", "arguments": {
            "action": "nexus_download_retry", "params": params,
        }}}, client)
        self.assertTrue(reply["isError"])
        failure = json.loads(reply["content"][0]["text"])
        self.assertIn("still has an archive", failure["message"])
        self.assertEqual(failure["phase"], "preflight")
        self.assertIs(failure["changes_may_have_occurred"], False)
        self.assertNotIn("observed_state", failure)
        self.assertNotIn("backup_id", failure)
        self.assertIsNone(self.bridge._successor(self.bridge._read(original["download_id"])))
        self.assertEqual(len(self.native.queued), 1)

    def test_initial_download_validation_is_clean_preflight_refusal(self):
        ops = Operations(self.organizer, SimpleNamespace(ModState=ModState, PluginState=PluginState),
                         self.data, lambda: None)
        ops.nexus = self.bridge
        self.native.disallow = True
        with self.assertRaisesRegex(RuntimeError, "Premium required") as error:
            ops.dispatch("nexus_download", {"url": FILE_URL, "mode": "premium", "expected_profile": "Test",
                                            "expected_revision": ops.snapshot()["revision"]})
        failure = error.exception.bridge_failure
        self.assertEqual(failure["phase"], "preflight")
        self.assertIs(failure["changes_may_have_occurred"], False)
        self.assertFalse(self.bridge.store.exists())
        self.assertEqual(self.native.queued, [])

    def test_error_after_retry_reservation_still_reports_possible_changes(self):
        ops = Operations(self.organizer, SimpleNamespace(ModState=ModState, PluginState=PluginState),
                         self.data, lambda: None)
        ops.nexus = self.bridge
        original = self.start(mode="premium")
        reserve = self.bridge._reserve

        def uncertain_claim(record):
            reserve(record)
            raise OSError("Simulated interruption after reservation")

        with patch.object(self.bridge, "_reserve", side_effect=uncertain_claim):
            with self.assertRaisesRegex(OSError, "after reservation") as error:
                ops.dispatch("nexus_download_retry", {"download_id": original["download_id"],
                    "expected_profile": "Test", "expected_revision": ops.snapshot()["revision"]})
        failure = error.exception.bridge_failure
        self.assertEqual(failure["phase"], "operation")
        self.assertIs(failure["changes_may_have_occurred"], True)
        self.assertEqual(failure["observed_state"]["profile"], "Test")
        self.assertIsNotNone(self.bridge._successor(self.bridge._read(original["download_id"])))
        self.assertEqual(len(self.native.queued), 1)
