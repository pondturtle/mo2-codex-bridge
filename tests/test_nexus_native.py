"""Contract tests for the pinned native MO2 2.5.2 Nexus adapter."""

import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from mo2_codex_bridge.nexus_native import NexusNative


DOMAIN = "skyrimspecialedition"
# The separator is the literal UTF-8 sequence in MO2's v2.5.2 source.
TITLE = (b"Skyrim Special Edition \xe2\x80\x93 Mod Organizer v2.5.2").decode("utf-8")


class Signal:
    def __init__(self):
        self.callbacks = []
        self.connect_error = False

    def connect(self, callback):
        if self.connect_error:
            raise RuntimeError("private foreign diagnostic")
        self.callbacks.append(callback)

    def disconnect(self, callback):
        self.callbacks.remove(callback)

    def emit(self, *args):
        for callback in self.callbacks[:]:
            callback(*args)


class FakeOrganizer:
    def __init__(self):
        self.api_version = "2.5.2"
        self.domain = DOMAIN
        self.short_name = "SkyrimSE"
        self.queued = []
        self.metadata = []
        self.repository_count = 0
        self.ready = Signal()
        self.failure = Signal()
        self.deleted = False
        self.object = SimpleNamespace(
            fileInfoAvailable=self.ready, requestFailed=self.failure,
            deleteLater=self.delete_repository,
        )
        self.manager = SimpleNamespace(startDownloadNexusFile=self.queue)
        self.repository = SimpleNamespace(
            _object=lambda: self.object, requestFileInfo=self.metadata_request,
        )

    def appVersion(self):
        return SimpleNamespace(displayString=lambda count: self.api_version)

    def managedGame(self):
        return SimpleNamespace(
            gameShortName=lambda: self.short_name,
            gameNexusName=lambda: self.domain,
            displayGameName=lambda: "Skyrim Special Edition",
        )

    def downloadManager(self):
        return self.manager

    def createNexusBridge(self):
        self.repository_count += 1
        return self.repository

    def delete_repository(self):
        self.deleted = True

    def metadata_request(self, game, mod_id, file_id, token):
        self.metadata.append((game, mod_id, file_id, token))

    def queue(self, mod_id, file_id):
        self.queued.append((mod_id, file_id))
        return 17  # Predicted active row, deliberately irrelevant to identity.


class NativeNexusTests(unittest.TestCase):
    def setUp(self):
        self.organizer = FakeOrganizer()
        self.title = f"{TITLE} (FixtureAccount42*)"
        self.window_type = "MainWindow"
        self.window = SimpleNamespace(
            windowTitle=lambda: self.title,
            metaObject=lambda: SimpleNamespace(className=lambda: self.window_type),
        )
        self.native = NexusNative(self.organizer, window=lambda: self.window)
        self.successes = []
        self.failures = []

    def request(self, file_id=17, on_success=None, on_failure=None):
        return self.native.request_file(
            DOMAIN, 76, file_id,
            on_success or self.successes.append, on_failure or self.failures.append,
        )

    def callback_token(self):
        return self.organizer.metadata[-1][-1]

    def test_premium_queue_is_deferred_one_shot_and_discards_predicted_row(self):
        queue = self.native.prepare_queue(DOMAIN, 76, 18)
        self.assertEqual(self.organizer.queued, [])
        self.assertIsNone(queue())
        self.assertEqual(self.organizer.queued, [(76, 18)])
        with self.assertRaisesRegex(RuntimeError, "already used"):
            queue()
        self.assertEqual(self.organizer.queued, [(76, 18)])

    def test_queue_refuses_free_unknown_or_unrecognized_account_evidence(self):
        for title in (
            f"{TITLE} (FixtureAccount42)", TITLE, f"{TITLE} (FixtureAccount42**)",
            f"{TITLE} (FixtureAccount42*) copied", "other (FixtureAccount42*)",
            f"{TITLE} (Fixture Account*)",
        ):
            with self.subTest(title=title):
                self.title = title
                with self.assertRaisesRegex(RuntimeError, "Mod Manager Download"):
                    self.native.prepare_queue(DOMAIN, 76, 18)
        self.title = f"{TITLE} (FixtureAccount42*)"
        self.window_type = "OtherWindow"
        with self.assertRaisesRegex(RuntimeError, "Mod Manager Download"):
            self.native.prepare_queue(DOMAIN, 76, 18)
        self.assertEqual(self.organizer.queued, [])

    def test_game_and_account_are_rechecked_before_queue(self):
        queue = self.native.prepare_queue(DOMAIN, 76, 18)
        self.title = f"{TITLE} (FixtureAccount42)"
        with self.assertRaisesRegex(RuntimeError, "changed before queueing"):
            queue()
        self.title = f"{TITLE} (FixtureAccount42*)"
        with self.assertRaisesRegex(RuntimeError, "already used"):
            queue()
        self.assertEqual(self.organizer.queued, [])

    def test_premium_marker_requires_the_actual_mo2_title_separator(self):
        actual = self.title
        self.title = actual.replace("\u2013", "\u2014")
        with self.assertRaisesRegex(RuntimeError, "Mod Manager Download"):
            self.native.prepare_queue(DOMAIN, 76, 18)
        self.title = actual
        self.native.prepare_queue(DOMAIN, 76, 18)()
        self.assertEqual(self.organizer.queued, [(76, 18)])

    def test_wrong_game_version_or_non_integer_ids_fail_before_native_calls(self):
        with self.assertRaisesRegex(ValueError, "managed game"):
            self.native.prepare_queue("skyrim", 76, 18)
        self.organizer.api_version = "2.5.0"
        with self.assertRaisesRegex(RuntimeError, "2.5.2"):
            self.request()
        self.organizer.api_version = "2.5.2"
        for value in (False, True, 0, -1, 2_147_483_648, "17", 17.0):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "positive Nexus"):
                    self.native.prepare_queue(DOMAIN, 76, value)
                with self.assertRaisesRegex(ValueError, "positive Nexus"):
                    self.request(value)
        self.assertEqual(self.organizer.queued, [])
        self.assertEqual(self.organizer.repository_count, 0)

    def test_native_exception_cannot_leak_or_trigger_another_launch(self):
        def unreliable(mod_id, file_id):
            self.organizer.queue(mod_id, file_id)
            raise OSError("private URL with secret token")

        self.organizer.manager.startDownloadNexusFile = unreliable
        queue = self.native.prepare_queue(DOMAIN, 76, 18)
        with self.assertRaisesRegex(RuntimeError, "same request") as failure:
            queue()
        self.assertNotIn("secret", str(failure.exception))
        with self.assertRaisesRegex(RuntimeError, "already used"):
            queue()
        self.assertEqual(self.organizer.queued, [(76, 18)])

    def test_free_account_can_request_exact_file_info(self):
        self.title = f"{TITLE} (FixtureAccount42)"
        cancel = self.request()
        short, mod_id, file_id, token = self.organizer.metadata[-1]
        self.assertEqual((short, mod_id, file_id), ("SkyrimSE", 76, 17))
        self.assertIsInstance(token, str)
        self.organizer.ready.emit(
            "SkyrimSpecialEdition", 76, 17, token,
            {"file_id": 17, "name": "Example Optional", "version": "2.0",
             "file_name": "fixture-optional.7z", "category_id": 3,
             "uri": "https://example.invalid/private", "apikey": "fixture-only",
             "description": "untrusted fixture text", "size_in_bytes": 125},
        )
        self.assertEqual(self.successes, [{
            "file_id": 17, "name": "Example Optional", "version": "2.0",
            "file_name": "fixture-optional.7z", "category_id": 3, "size_in_bytes": 125,
        }])
        self.assertFalse(cancel())

    def test_guest_file_info_refuses_before_mo2_can_prompt_for_login(self):
        self.title = TITLE
        with self.assertRaisesRegex(RuntimeError, "Sign in to Nexus in MO2"):
            self.request()
        self.assertEqual(self.organizer.repository_count, 0)

    def test_reordered_and_duplicate_callbacks_remain_bound_to_exact_token(self):
        self.request(17)
        first = self.callback_token()
        self.request(18)
        second = self.callback_token()
        self.assertNotEqual(first, second)
        self.assertEqual(self.organizer.repository_count, 1)

        self.organizer.ready.emit(DOMAIN, 76, 17, "unrelated", {"file_id": 17})
        self.organizer.ready.emit(DOMAIN, 76, 18, second, {"file_id": 18})
        self.organizer.ready.emit(DOMAIN, 76, 17, first, {"file_id": 17})
        self.organizer.ready.emit(DOMAIN, 76, 18, second, {"file_id": 18})
        self.assertEqual(self.successes, [{"file_id": 18}, {"file_id": 17}])
        self.assertEqual(self.failures, [])

    def test_wrong_callback_identity_on_matching_token_fails(self):
        for game, mod_id, file_id in (("skyrim", 76, 17), (DOMAIN, 77, 17), (DOMAIN, 76, 18)):
            with self.subTest(game=game, mod_id=mod_id, file_id=file_id):
                self.request(17)
                token = self.callback_token()
                self.organizer.ready.emit(game, mod_id, file_id, token, {"file_id": file_id})
                self.organizer.ready.emit(DOMAIN, 76, 17, token, {"file_id": 17})
        self.assertEqual(self.successes, [])
        self.assertEqual(len(self.failures), 3)
        self.assertTrue(all("different file" in message for message in self.failures))

    def test_missing_or_conflicting_file_id_inside_native_payload_fails(self):
        for payload in ([], {}, {"file_id": 18}, {"file_id": "17"}):
            with self.subTest(payload=payload):
                self.request(17)
                self.organizer.ready.emit(DOMAIN, 76, 17, self.callback_token(), payload)
        self.assertEqual(self.successes, [])
        self.assertEqual(len(self.failures), 4)

    def test_native_failure_does_not_return_private_detail(self):
        self.request()
        self.organizer.failure.emit(DOMAIN, 76, 17, self.callback_token(), 403,
                                    "private URL with secret token")
        self.assertEqual(self.successes, [])
        self.assertEqual(self.failures, ["MO2 could not retrieve this Nexus file's metadata."])

    def test_cancellation_disregards_late_response(self):
        cancel = self.request()
        self.assertTrue(cancel())
        self.assertFalse(cancel())
        self.organizer.ready.emit(DOMAIN, 76, 17, self.callback_token(), {"file_id": 17})
        self.assertEqual(self.successes, [])
        self.assertEqual(self.failures, [])

    def test_missing_callback_is_closed_after_partial_connection(self):
        self.organizer.failure.connect_error = True
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "callbacks are unavailable"):
                self.request()
        self.assertEqual(self.organizer.ready.callbacks, [])
        self.assertEqual(self.organizer.repository_count, 1)
        self.assertTrue(self.organizer.deleted)
        self.assertEqual(self.organizer.metadata, [])

    def test_synchronous_native_return_and_raised_consumer_are_safe(self):
        def immediate(game, mod_id, file_id, token):
            self.organizer.ready.emit(DOMAIN, mod_id, file_id, token, {"file_id": file_id})

        def bad_consumer(data):
            raise RuntimeError("private callback diagnostic")

        self.organizer.repository.requestFileInfo = immediate
        cancel = self.request(on_success=bad_consumer)
        self.assertFalse(cancel())
        self.assertEqual(self.failures, ["The MO2 Nexus file metadata could not be processed."])

    def test_worker_thread_cannot_invoke_organizer(self):
        errors = []

        def worker():
            try:
                self.native.prepare_queue(DOMAIN, 76, 18)
            except RuntimeError as error:
                errors.append(str(error))

        child = threading.Thread(target=worker)
        child.start()
        child.join()
        self.assertEqual(errors, ["Nexus actions must run on the MO2 GUI thread."])
        self.assertEqual(self.organizer.queued, [])


class FixtureDownloadList:
    def __init__(self, rows=(), name="DownloadList", columns=8):
        self.rows = rows
        self.name = name
        self.columns = columns
        self.reads = []
        self.valid_index = True
        self.count = None

    def metaObject(self):
        return SimpleNamespace(className=lambda: self.name)

    def columnCount(self):
        return self.columns

    def rowCount(self):
        return len(self.rows) if self.count is None else self.count

    def index(self, row, column):
        return SimpleNamespace(row=row, column=column, isValid=lambda: self.valid_index)

    def data(self, index, role):
        if index.column != 1 or role != 0:
            raise AssertionError("The guard must only read native status cells")
        self.reads.append(index.row)
        return self.rows[index.row]


class FixtureProxy(FixtureDownloadList):
    def __init__(self, source=None):
        # An empty filtered view can hide unsettled native rows.
        super().__init__(name="FixtureProxy")
        self.source = source
        self.unwrap_calls = 0

    def sourceModel(self):
        self.unwrap_calls += 1
        return self.source


class FixtureTreeView:
    pass


class DownloadQueueIdleTests(unittest.TestCase):
    def setUp(self):
        self.organizer = FakeOrganizer()
        self.source = FixtureDownloadList()
        self.view = FixtureTreeView()
        self.view.objectName = lambda: "downloadView"
        self.view.metaObject = lambda: SimpleNamespace(className=lambda: "DownloadListView")
        self.view.model = lambda: self.source
        self.views = [self.view]
        self.window = SimpleNamespace(
            metaObject=lambda: SimpleNamespace(className=lambda: "MainWindow"),
            findChildren=self.find_views,
        )
        self.native = NexusNative(self.organizer, window=lambda: self.window)
        self.widget_reads = 0
        self.translations = {}

        qt = ModuleType("PyQt6")
        qt.__path__ = []
        core = ModuleType("PyQt6.QtCore")
        widgets = ModuleType("PyQt6.QtWidgets")
        core.QAbstractItemModel = FixtureDownloadList
        core.QAbstractProxyModel = FixtureProxy
        core.QCoreApplication = SimpleNamespace(translate=self.translate)
        core.Qt = SimpleNamespace(ItemDataRole=SimpleNamespace(DisplayRole=0))
        widgets.QTreeView = FixtureTreeView
        self.enterContext(patch.dict(sys.modules, {"PyQt6": qt, "PyQt6.QtCore": core, "PyQt6.QtWidgets": widgets}))

    def find_views(self, kind, name):
        self.assertIs(kind, FixtureTreeView)
        self.assertEqual(name, "downloadView")
        self.widget_reads += 1
        return self.views

    def translate(self, context, text):
        self.assertEqual(context, "DownloadList")
        return self.translations.get(text, f"localized:{text}")

    def test_empty_native_model_is_idle(self):
        self.assertTrue(self.native.download_queue_idle(DOMAIN))
        self.assertEqual(self.source.reads, [])

    def test_only_native_localized_terminal_cells_are_read(self):
        self.source.rows = ("localized:Downloaded", "localized:Installed", "localized:Uninstalled")
        self.view.model = lambda: FixtureProxy(FixtureProxy(self.source))
        self.assertTrue(self.native.download_queue_idle(DOMAIN))
        self.assertEqual(self.source.reads, [0, 1, 2])

    def test_filtered_view_cannot_hide_unsettled_source_rows(self):
        for unsettled in (None, "", "localized:Pending", "localized:Paused", "localized:Canceled",
                          "localized:Canceling", "localized:Pausing", "localized:Started",
                          "localized:Fetching Info", "localized:Error", "Unknown", 0):
            with self.subTest(status=unsettled):
                self.source.rows = ("localized:Installed", unsettled)
                self.view.model = lambda: FixtureProxy(self.source)
                self.assertFalse(self.native.download_queue_idle(DOMAIN))

    def test_translation_collision_cannot_make_pending_look_terminal(self):
        self.translations["Pending"] = "localized:Downloaded"
        self.source.rows = ("localized:Downloaded",)
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.assertEqual(self.source.reads, [])

    def test_ambiguous_view_wrong_model_or_changed_shape_fails_closed(self):
        self.views.append(self.view)
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.views.pop()
        self.view.metaObject = lambda: SimpleNamespace(className=lambda: "QTreeView")
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.view.metaObject = lambda: SimpleNamespace(className=lambda: "DownloadListView")
        self.source.name = "ForeignModel"
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.source.name = "DownloadList"
        self.source.columns = 7
        self.assertFalse(self.native.download_queue_idle(DOMAIN))

    def test_proxy_without_source_cycle_or_excessive_depth_fails_closed(self):
        orphan = FixtureProxy()
        cycle = FixtureProxy()
        cycle.source = cycle
        deep = self.source
        for _ in range(5):
            deep = FixtureProxy(deep)
        for proxy in (orphan, cycle, deep):
            with self.subTest(proxy=proxy):
                self.view.model = lambda: proxy
                self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.assertLessEqual(cycle.unwrap_calls, 4)

    def test_invalid_index_or_row_count_fails_closed(self):
        self.source.rows = ("localized:Installed",)
        self.source.valid_index = False
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.source.valid_index = True
        for count in (-1, 8193, True, "1"):
            with self.subTest(count=count):
                self.source.count = count
                self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.assertEqual(self.source.reads, [])

    def test_changed_row_count_during_observation_fails_closed(self):
        self.source.rows = ("localized:Installed",)
        counts = iter((1, 2))
        self.source.rowCount = lambda: next(counts)
        self.assertFalse(self.native.download_queue_idle(DOMAIN))

    def test_wrong_version_game_or_thread_never_reads_the_window(self):
        self.assertFalse(self.native.download_queue_idle("skyrim"))
        self.organizer.api_version = "2.5.1"
        self.assertFalse(self.native.download_queue_idle(DOMAIN))
        self.organizer.api_version = "2.5.2"
        worker = []
        child = threading.Thread(target=lambda: worker.append(self.native.download_queue_idle(DOMAIN)))
        child.start()
        child.join()
        self.assertEqual(worker, [False])
        self.assertEqual(self.widget_reads, 0)


class OnlyLiveMo2InstanceTests(unittest.TestCase):
    def setUp(self):
        self.native = NexusNative(FakeOrganizer())
        observer = self.enterContext(patch("mo2_codex_bridge.running_apps.WindowsProcessObserver"))
        self.snapshot = observer.return_value.snapshot
        self.enterContext(patch("mo2_codex_bridge.nexus_native.os.getpid", return_value=110))

    def test_positive_snapshot_contains_only_the_current_mo2_image(self):
        self.snapshot.return_value = {
            0: (0, "[System Process]"),
            109: (0, "explorer.exe"),
            110: (109, "ModOrganizer.exe"),
            111: (110, "nxmhandler.exe"),
        }
        self.assertTrue(self.native.only_live_mo2_instance())

    def test_other_mo2_is_rejected_regardless_of_parent_or_case(self):
        for parent in (0, 110):
            with self.subTest(parent=parent):
                self.snapshot.return_value = {
                    110: (109, "ModOrganizer.exe"), 120: (parent, "MODORGANIZER.EXE"),
                }
                self.assertFalse(self.native.only_live_mo2_instance())

    def test_other_process_name_or_pid_cannot_stand_in_for_own_mo2(self):
        for rows in ({111: (109, "ModOrganizer.exe")},
                     {110: (109, "ModOrganizer.exe.backup")}, {}):
            with self.subTest(rows=rows):
                self.snapshot.return_value = rows
                self.assertFalse(self.native.only_live_mo2_instance())

    def test_unavailable_or_malformed_process_observation_fails_closed(self):
        self.snapshot.side_effect = OSError("private process capture diagnostic")
        self.assertFalse(self.native.only_live_mo2_instance())
        self.snapshot.side_effect = None
        for rows in (None, {110: ["109", "ModOrganizer.exe"]},
                     {110: (109, "ModOrganizer.exe"), 111: (109, "")},
                     {110: (109, "ModOrganizer.exe"), "111": (109, "other.exe")},
                     {110: (109, "ModOrganizer.exe"), 111: (-1, "other.exe")}):
            with self.subTest(rows=rows):
                self.snapshot.return_value = rows
                self.assertFalse(self.native.only_live_mo2_instance())
        with patch("mo2_codex_bridge.nexus_native._MAX_PROCESS_ROWS", 1):
            self.snapshot.return_value = {
                109: (0, "explorer.exe"), 110: (109, "ModOrganizer.exe"),
            }
            self.assertFalse(self.native.only_live_mo2_instance())

    def test_worker_thread_does_not_observe_processes(self):
        results = []
        child = threading.Thread(target=lambda: results.append(self.native.only_live_mo2_instance()))
        child.start()
        child.join()
        self.assertEqual(results, [False])
        self.snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
