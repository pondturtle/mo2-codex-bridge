"""Scoped MO2 2.5.2 repository and download calls on the GUI thread."""

from collections.abc import Mapping
import os
import re
import threading
import uuid


_MO2_VERSION = "2.5.2"
_MAX_PENDING = 32
_MAX_DOWNLOAD_PROXY_LAYERS = 4
_MAX_DOWNLOAD_ROWS = 8192
_MAX_PROCESS_ROWS = 65536
_TERMINAL_DOWNLOAD_LABELS = ("Downloaded", "Installed", "Uninstalled")
_OTHER_DOWNLOAD_LABELS = ("Started", "Canceling", "Pausing", "Canceled", "Paused",
                          "Error", "Fetching Info", "Pending")
_FILE_FIELDS = (
    "file_id", "name", "version", "mod_version", "file_name", "category_id",
    "category_name", "size", "size_kb", "size_in_bytes", "is_primary",
)


def _positive_id(value):
    return type(value) is int and 0 < value <= 2_147_483_647


class NexusNative:
    """Use MO2's existing account without reading credentials or download URLs."""

    def __init__(self, organizer, window=None):
        self.organizer = organizer
        self.window = window
        self._owner = threading.get_ident()
        self._repository = None
        self._repository_unavailable = False
        self._pending = {}

    def _game(self, domain):
        if threading.get_ident() != self._owner:
            raise RuntimeError("Nexus actions must run on the MO2 GUI thread.")
        try:
            version = self.organizer.appVersion().displayString(3)
            managed = self.organizer.managedGame()
            actual_domain = managed.gameNexusName().casefold()
            short_name = managed.gameShortName()
        except Exception:
            raise RuntimeError("The managed MO2 game and version could not be verified.") from None
        if version != _MO2_VERSION:
            raise RuntimeError("Native Nexus actions are supported on MO2 2.5.2.")
        if not isinstance(domain, str) or not domain or domain.casefold() != actual_domain:
            raise ValueError("The Nexus link must be for this MO2 instance's managed game.")
        if not isinstance(short_name, str) or not short_name:
            raise RuntimeError("The managed MO2 game has no native Nexus identifier.")
        return managed, short_name

    def _main_window(self):
        window = self.window() if callable(self.window) else self.window
        if window is not None and window.metaObject().className() == "MainWindow":
            return window
        return None

    def _account(self, game):
        # 2.5.2's MainWindow::updateWindowTitle marks Premium with an asterisk
        # after the authenticated username. Nexus usernames are alphanumeric.
        # Use only the Boolean result. Never save or return the window title.
        try:
            window = self._main_window()
            if window is None:
                return None
            # MainWindow::updateWindowTitle uses UTF-8 E2 80 93 in 2.5.2.
            prefix = f"{game.displayGameName()} \N{EN DASH} Mod Organizer v{_MO2_VERSION} "
            title = window.windowTitle()
            if title.startswith(prefix):
                match = re.fullmatch(r"\([A-Za-z0-9]+(\*)?\)", title[len(prefix):])
                if match is not None:
                    return "premium" if match[1] else "regular"
        except Exception:
            pass
        return None

    def download_queue_idle(self, game):
        """Prove MO2's unfiltered download list contains only terminal rows."""
        try:
            self._game(game)
            from PyQt6.QtCore import QAbstractItemModel, QAbstractProxyModel, QCoreApplication, Qt
            from PyQt6.QtWidgets import QTreeView

            window = self._main_window()
            if window is None:
                return False
            views = window.findChildren(QTreeView, "downloadView")
            if len(views) != 1:
                return False
            view = views[0]
            if (view.objectName() != "downloadView"
                    or view.metaObject().className() != "DownloadListView"):
                return False
            # 2.5.2's DownloadsTab can filter out pending or active rows.
            # Only the source DownloadList includes both MO2 queues.
            model = view.model()
            for _ in range(_MAX_DOWNLOAD_PROXY_LAYERS):
                if not isinstance(model, QAbstractProxyModel):
                    break
                model = model.sourceModel()
            if (not isinstance(model, QAbstractItemModel)
                    or isinstance(model, QAbstractProxyModel)
                    or model.metaObject().className() != "DownloadList"
                    or model.columnCount() != 8):
                return False

            rows = model.rowCount()
            if type(rows) is not int or not 0 <= rows <= _MAX_DOWNLOAD_ROWS:
                return False
            if rows:
                translate = QCoreApplication.translate
                terminal = {translate("DownloadList", label) for label in _TERMINAL_DOWNLOAD_LABELS}
                other = {translate("DownloadList", label) for label in _OTHER_DOWNLOAD_LABELS}
                if terminal & other or any(not isinstance(label, str) or not label.strip()
                                           for label in terminal | other):
                    return False
                # A running transfer may have no DisplayRole text because
                # the native delegate draws its progress instead.
                for row in range(rows):
                    index = model.index(row, 1)
                    if not index.isValid() or model.data(index, Qt.ItemDataRole.DisplayRole) not in terminal:
                        return False
            return model.rowCount() == rows
        except Exception:
            return False

    def only_live_mo2_instance(self):
        """Verify the current MO2 is the sole observed ModOrganizer process."""
        if threading.get_ident() != self._owner:
            return False
        try:
            from .running_apps import WindowsProcessObserver

            owner = os.getpid()
            processes = WindowsProcessObserver().snapshot()
            if type(processes) is not dict or not 0 < len(processes) <= _MAX_PROCESS_ROWS:
                return False
            owns_mo2 = False
            for pid, row in processes.items():
                if (type(pid) is not int or not 0 <= pid <= 0xFFFFFFFF
                        or type(row) is not tuple or len(row) != 2):
                    return False
                parent, image = row
                if (type(parent) is not int or not 0 <= parent <= 0xFFFFFFFF
                        or type(image) is not str or not 0 < len(image) < 260 or "\0" in image):
                    return False
                if image.casefold() == "modorganizer.exe":
                    if pid != owner:
                        return False
                    owns_mo2 = True
            return owns_mo2
        except Exception:
            return False

    def prepare_queue(self, game, mod_id, file_id):
        """Validate the route before recording a durable intent; return a one-use call."""
        if not _positive_id(mod_id) or not _positive_id(file_id):
            raise ValueError("A positive Nexus mod ID and file ID are required.")
        managed, short_name = self._game(game)
        if self._account(managed) != "premium":
            raise RuntimeError(
                "MO2 could not confirm Premium. Use the Nexus site's Mod Manager Download button."
            )
        try:
            start = self.organizer.downloadManager().startDownloadNexusFile
        except Exception:
            raise RuntimeError("The native MO2 download action is unavailable.") from None
        if not callable(start):
            raise RuntimeError("The native MO2 download action is unavailable.")
        used = False

        def queue():
            nonlocal used
            if used:
                raise RuntimeError("This prepared download was already used. Check its existing request.")
            used = True
            current, current_name = self._game(game)
            if current_name != short_name or self._account(current) != "premium":
                raise RuntimeError("The MO2 game or Premium evidence changed before queueing.")
            try:
                # On 2.5.2 this returns the predicted active row before
                # asynchronous Nexus resolution. The row cannot identify a file.
                start(mod_id, file_id)
            except Exception:
                raise RuntimeError("MO2 did not confirm submission. Check this same request in MO2.") from None

        return queue

    def _connect_repository(self):
        if self._repository is not None:
            return self._repository
        message = "MO2's native Nexus file-info callbacks are unavailable."
        if self._repository_unavailable:
            raise RuntimeError(message)
        signals = None
        connected = []
        try:
            repository = self.organizer.createNexusBridge()
            signals = repository._object()
            for signal, callback in (
                (signals.fileInfoAvailable, self._file_reply),
                (signals.requestFailed, self._request_failed),
            ):
                if signal.connect(callback) is False:
                    raise RuntimeError("Callback connection failed.")
                connected.append((signal, callback))
        except Exception:
            self._repository_unavailable = True
            for signal, callback in connected:
                try:
                    signal.disconnect(callback)
                except Exception:
                    pass
            if signals is not None:
                try:
                    signals.deleteLater()
                except Exception:
                    pass
            raise RuntimeError(message) from None
        self._repository = repository
        return repository

    def request_file(self, game, mod_id, file_id, on_success, on_failure):
        """Start one exact file-info request and return a callback cancellation function."""
        if not _positive_id(mod_id) or not _positive_id(file_id):
            raise ValueError("A positive Nexus mod ID and file ID are required.")
        managed, short_name = self._game(game)
        if self._account(managed) is None:
            # Avoid starting MO2's sign-in prompt via NexusInterface::nextRequest.
            raise RuntimeError("Sign in to Nexus in MO2 before asking MO2 for file metadata.")
        if len(self._pending) >= _MAX_PENDING:
            raise RuntimeError("Too many MO2 Nexus metadata requests are pending.")
        repository = self._connect_repository()
        token = uuid.uuid4().hex
        identity = (game.casefold(), mod_id, file_id)
        self._pending[token] = (identity, on_success, on_failure)
        try:
            repository.requestFileInfo(short_name, mod_id, file_id, token)
        except Exception:
            if self._pending.pop(token, None) is not None:
                raise RuntimeError("MO2 could not start the Nexus file-info request.") from None

        def cancel():
            # The native HTTP request may still finish. Its reply will be ignored.
            if threading.get_ident() != self._owner:
                raise RuntimeError("Nexus actions must run on the MO2 GUI thread.")
            return self._pending.pop(token, None) is not None

        return cancel

    @staticmethod
    def _fail(callback, message):
        try:
            callback(message)
        except Exception:
            # Never propagate a consumer exception through a native Qt signal.
            pass

    def _take(self, game, mod_id, file_id, token):
        if threading.get_ident() != self._owner or not isinstance(token, str):
            return None
        request = self._pending.pop(token, None)
        if request is None:
            return None
        identity, on_success, on_failure = request
        if (not isinstance(game, str) or not _positive_id(mod_id) or not _positive_id(file_id)
                or (game.casefold(), mod_id, file_id) != identity):
            self._fail(on_failure, "MO2 returned Nexus metadata for a different file.")
            return None
        return on_success, on_failure

    def _file_reply(self, game, mod_id, file_id, token, data):
        callbacks = self._take(game, mod_id, file_id, token)
        if callbacks is None:
            return
        on_success, on_failure = callbacks
        try:
            if not isinstance(data, Mapping) or not _positive_id(data.get("file_id")) or data["file_id"] != file_id:
                self._fail(on_failure, "MO2 did not verify the requested Nexus file ID.")
                return
            # Use selected scalar metadata only. In particular, never relay URLs,
            # arbitrary fields, descriptions, or the native reply body.
            on_success({key: data[key] for key in _FILE_FIELDS
                        if key in data and type(data[key]) in {int, str, bool}})
        except Exception:
            self._fail(on_failure, "The MO2 Nexus file metadata could not be processed.")

    def _request_failed(self, game, mod_id, file_id, token, code, message):
        callbacks = self._take(game, mod_id, file_id, token)
        if callbacks is not None:
            # Native error text can include arbitrary server or application data.
            self._fail(callbacks[1], "MO2 could not retrieve this Nexus file's metadata.")
