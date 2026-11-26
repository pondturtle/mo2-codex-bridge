"""Exact Nexus file selection, at-most-once handoff and offline archive proof."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .common import contained_path
from .nexus_archive import (
    capture_archive_baseline, capture_retry_baseline, inspect_nexus_archive,
    revalidate_verified_archive,
)
from .nexus_catalog import fetch_public_files
from .nexus_links import NexusLink, managed_nexus_game, normalize_nexus_file, normalize_nexus_files, parse_nexus_link
from .nexus_native import NexusNative


_LOOKUP_TIMEOUT = 40
_LOOKUP_TTL = 300
_MAX_LOOKUPS = 16
_MAX_CHECKS = 8
_MAX_RECEIPT = 4 * 1024 * 1024
_DOWNLOAD_ID = re.compile(r"[a-f0-9]{64}")


def _identity(game, mod_id, file_id):
    return hashlib.sha256(f"{game}/{mod_id}/{file_id}".encode("ascii")).hexdigest()


def _retry_identity(previous):
    if not isinstance(previous, str) or not _DOWNLOAD_ID.fullmatch(previous):
        raise ValueError("An exact prior Nexus download ID is required")
    return hashlib.sha256(("nexus-retry/" + previous).encode("ascii")).hexdigest()


class NexusDownloads:
    """Organizer calls stay on its GUI thread. Workers do network metadata or hashing."""

    def __init__(self, organizer, data, *, window=None, native=None, catalog=fetch_public_files,
                 executor=None, clock=time.monotonic):
        self.organizer = organizer
        self.store = Path(data) / "nexus-download-intents"
        self.native = NexusNative(organizer, window=window) if native is None else native
        self.catalog = catalog
        self.executor = (ThreadPoolExecutor(max_workers=2, thread_name_prefix="mo2-nexus")
                         if executor is None else executor)
        self.clock = clock
        self.lookups = {}
        self.checks = {}
        self.cancelled = threading.Event()

    def close(self):
        self.cancelled.set()
        for entry in self.lookups.values():
            cancel = entry.get("cancel")
            if cancel is not None:
                cancel()
        self.lookups.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)

    def _link(self, params, keys):
        if set(params) - keys:
            raise ValueError("Unsupported Nexus request parameters")
        return parse_nexus_link(params.get("url"), self.organizer.managedGame(),
                                file_id=params.get("file_id"))

    def _scope(self):
        try:
            supported = self.organizer.appVersion().displayString(3) == "2.5.2"
        except (AttributeError, RuntimeError):
            supported = False
        if not supported:
            raise ValueError("Nexus archive tracking is verified for MO2 2.5.2 only")
        managed = self.organizer.managedGame()
        return managed_nexus_game(managed), managed.gameShortName(), self.organizer.downloadsPath()

    def _receipt_path(self, download_id):
        if not isinstance(download_id, str) or not _DOWNLOAD_ID.fullmatch(download_id):
            raise ValueError("An exact Nexus download ID is required")
        if self.store.is_symlink() or self.store.is_junction():
            raise ValueError("Linked Nexus download receipt store refused")
        return contained_path(self.store, download_id + ".json")

    def _read_record(self, download_id):
        path = self._receipt_path(download_id)
        try:
            with path.open("rb") as stream:
                raw = stream.read(_MAX_RECEIPT + 1)
        except FileNotFoundError:
            return None
        try:
            if len(raw) > _MAX_RECEIPT:
                raise ValueError()
            record = json.loads(raw)
            target = record["target"]
            NexusLink(target["game"], target["mod_id"], target["file_id"], target["game_short"])
            schema = record["schema"]
            if (type(schema) is not int or schema not in {1, 2} or record["download_id"] != download_id
                    or record["mode"] not in ("manual", "premium") or target["file_id"] is None
                    or not isinstance(record["downloads_root"], str)
                    or not isinstance(record["baseline"], dict)):
                raise ValueError()
            if schema == 1:
                if ("previous_download_id" in record
                        or _identity(target["game"], target["mod_id"], target["file_id"]) != download_id):
                    raise ValueError()
            elif _retry_identity(record["previous_download_id"]) != download_id:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("Nexus download reservation is unreadable. Inspect the MO2 queue before any manual retry.") from None
        return record

    @staticmethod
    def _validate_parent(child, parent):
        if (parent is None or child["previous_download_id"] != parent["download_id"]
                or child["mode"] != parent["mode"]
                or Path(child["downloads_root"]) != Path(parent["downloads_root"])
                or any(child["target"][key] != parent["target"][key]
                       for key in ("game", "game_short", "mod_id", "file_id"))):
            raise ValueError("Nexus retry receipt does not match its original file and location")

    def _read(self, download_id):
        record = self._read_record(download_id)
        if record is not None and record["schema"] == 2:
            self._validate_parent(record, self._read_record(record["previous_download_id"]))
        return record

    def _successor(self, record):
        child = self._read_record(_retry_identity(record["download_id"]))
        if child is not None:
            if child["schema"] != 2:
                raise ValueError("Unexpected receipt at the Nexus retry ID")
            self._validate_parent(child, record)
        return child

    def _reserve(self, record):
        path = self._receipt_path(record["download_id"])
        encoded = json.dumps(record, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > _MAX_RECEIPT:
            raise ValueError("Nexus download baseline exceeds its receipt size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish a complete, flushed receipt without replacing a competing
        # reservation. No native submission has occurred before this point.
        fd, temporary = tempfile.mkstemp(prefix=".nexus-intent-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if os.name == "nt":
                    os.rename(temporary, path)  # Windows refuses an existing destination.
                else:
                    os.link(temporary, path)
            except FileExistsError:
                return False
            return True
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _lookup(self, link):
        now = self.clock()
        for key, item in list(self.lookups.items()):
            if now < item["expires"]:
                continue
            if item.get("future") is not None:
                continue
            if item.get("cancel") is not None:
                item["cancel"]()
            del self.lookups[key]
        key = (link.game, link.mod_id, link.file_id)
        item = self.lookups.get(key)
        if item is not None:
            return self._poll_catalog(item, now)
        if len(self.lookups) >= _MAX_LOOKUPS:
            previous = next((cached for cached, entry in self.lookups.items()
                             if entry["status"] != "pending" or
                             (entry.get("future") is not None and entry["future"].done())), None)
            if previous is None:
                raise ValueError("Too many Nexus lookups are pending. Check an existing lookup first.")
            del self.lookups[previous]
        item = {"status": "pending", "expires": now + _LOOKUP_TIMEOUT}
        self.lookups[key] = item

        def failed(_reason=None):
            if self.lookups.get(key) is item:
                item.update(status="unavailable", message="MO2 did not confirm metadata for the exact Nexus file.",
                            expires=self.clock() + _LOOKUP_TIMEOUT)

        def succeeded(raw):
            if self.lookups.get(key) is not item:
                return
            try:
                result = normalize_nexus_file(raw, requested_file_id=link.file_id)
            except (ValueError, TypeError):
                failed()
                return
            item.update(status="available", data={"file": result}, expires=self.clock() + _LOOKUP_TTL)

        try:
            if link.file_id is None:
                item["future"] = self.executor.submit(self.catalog, link.game, link.mod_id)
            else:
                item["cancel"] = self.native.request_file(link.game, link.mod_id, link.file_id, succeeded, failed)
        except Exception:
            failed()
        return self._poll_catalog(item, now)

    @staticmethod
    def _poll_catalog(item, now):
        future = item.get("future")
        if future is None or not future.done():
            return item
        item.pop("future")
        try:
            data = normalize_nexus_files(future.result())
            item.update(status="available", data=data, expires=now + _LOOKUP_TTL)
        except Exception:
            item.update(status="unavailable",
                        message="Public Nexus file metadata is unavailable. Use the Nexus Files page.",
                        expires=now + _LOOKUP_TIMEOUT)
        return item

    def files(self, params):
        link = self._link(params, {"url", "file_id", "offset", "limit"})
        offset, limit = params.get("offset", 0), params.get("limit", 30)
        if (type(offset) is not int or not 0 <= offset <= 5000
                or type(limit) is not int or not 1 <= limit <= 50
                or (link.file_id is not None and {"offset", "limit"} & params.keys())):
            raise ValueError("Use an offset from 0 to 5000 and a file-list limit from 1 to 50")
        info = self._lookup(link)
        result = {"status": info["status"], "game": link.game, "mod_id": link.mod_id,
                  "file_id": link.file_id, "files_url": link.files_url}
        if "message" in info:
            result["message"] = info["message"]
        if info["status"] == "available":
            data = info["data"]
            if "files" in data:
                total = len(data["files"])
                result.update(total=total, offset=offset, next_offset=offset + limit if offset + limit < total else None,
                              files=[dict(row, url=link.file_page(row["file_id"]))
                                     for row in data["files"][offset:offset + limit]])
            else:
                result["file"] = dict(data["file"], url=link.file_page(link.file_id))
        return result

    @staticmethod
    def _public(record):
        target = record["target"]
        result = {"download_id": record["download_id"], "mode": record["mode"],
                  "game": target["game"], "mod_id": target["mod_id"], "file_id": target["file_id"]}
        if record["schema"] == 2:
            result["previous_download_id"] = record["previous_download_id"]
        baseline = record["baseline"]
        if baseline.get("existing"):
            result.update(status="preexisting", message="MO2 already has evidence for this exact file. Inspect its existing download before any further action.")
        elif record["mode"] == "manual":
            link = NexusLink(target["game"], target["mod_id"], target["file_id"], target["game_short"])
            result.update(status="awaiting_browser", url=link.manual_download_url,
                          message="Use Mod Manager Download on this exact Nexus file, then check this download ID.")
        else:
            result.update(status="tracking_native_request",
                          message="This ID is reserved. Check its status or the MO2 Downloads pane. It will not be submitted again.")
        return result

    def _existing(self, record):
        result = self._public(record)
        if result["status"] == "awaiting_browser":
            result.pop("url")
            result.update(status="already_tracking",
                          message="Check this download ID before taking any further action in the browser or MO2.")
        child = self._successor(record)
        if child is not None:
            result.update(status="superseded", next_download_id=child["download_id"],
                          message="This attempt has an explicit retry. Check the successor download ID.")
        return result

    def _premium_info(self, link):
        catalog = self._lookup(NexusLink(link.game, link.mod_id, None, link.game_short))
        info = self._lookup(link)
        if "pending" in (catalog["status"], info["status"]):
            return None
        if info["status"] != "available" or catalog["status"] != "available":
            raise ValueError("MO2 could not confirm this exact Nexus file. Use the manual Mod Manager Download workflow.")
        file_info = info["data"]["file"]
        if file_info.get("category_id") not in {1, 2, 3, 4, 5}:
            raise ValueError("This file is not confirmed as a direct-download category. Use its Nexus Files page.")
        public_row = next((row for row in catalog["data"]["files"] if row["file_id"] == link.file_id), None)
        if public_row is None or public_row.get("category_id") != file_info["category_id"]:
            raise ValueError("The public catalog and MO2 did not confirm the same file category. Inspect its Files page.")
        # Native MO2 and Nexus authorize the transfer after the catalog and
        # native metadata have bound us to the same exact file.
        sizes = {size for size in (file_info.get("size_in_bytes"), public_row.get("size_in_bytes"))
                 if type(size) is int and size > 0}
        if len(sizes) > 1:
            raise ValueError("Nexus metadata disagrees on the exact file size. Refresh its Files page.")
        return {"size_bytes": next(iter(sizes), None), "expected_name": file_info.get("file_name")}

    def _claim_and_submit(self, record, scope, native_call, *, ready=None, before_claim=None):
        if before_claim is not None:
            before_claim()
        if not self._reserve(record):
            prior = self._read(record["download_id"])
            if prior is None:
                raise ValueError("A competing Nexus reservation could not be read. Inspect the MO2 queue.")
            self._ensure_scope(prior, *scope)
            return self._existing(prior)
        result = self._public(record)
        if ready is not None and not ready():
            result.pop("url", None)
            result.update(status="submission_blocked",
                          message="MO2's queue or Downloads directory changed after reserving this attempt. Check this new ID before taking action.")
            return result
        if native_call is not None:
            try:
                native_call()
            except Exception:
                result.update(status="submission_uncertain",
                              message="MO2 may have received the request. Check this ID and the Downloads pane before taking action.")
        return result

    def start(self, params, *, revalidate, before_claim=None):
        link = self._link(params, {"url", "file_id", "mode", "expected_profile", "expected_revision"})
        if link.file_id is None:
            raise ValueError("Select one exact Nexus file before requesting a download")
        mode = params.get("mode")
        if mode not in ("manual", "premium"):
            raise ValueError("Select manual browser handoff or Premium MO2 download")
        game, short_name, root = self._scope()
        if (link.game, link.game_short) != (game, short_name):
            raise ValueError("MO2's managed game changed during Nexus selection")
        download_id = _identity(game, link.mod_id, link.file_id)
        prior = self._read(download_id)
        if prior is not None:
            self._ensure_scope(prior, game, short_name, root)
            return self._existing(prior)

        target = {"game": game, "game_short": short_name, "mod_id": link.mod_id, "file_id": link.file_id}
        expected_name = None
        if mode == "premium":
            info = self._premium_info(link)
            if info is None:
                return {"status": "file_lookup_pending", "download_id": download_id,
                        "message": "Nexus file eligibility is being checked. Repeat this exact request once metadata arrives."}
            if info["size_bytes"] is not None:
                target["size_bytes"] = info["size_bytes"]
            expected_name = info["expected_name"]
        baseline = capture_archive_baseline(root, target)
        native_call = None
        if mode == "premium" and not baseline.get("existing"):
            native_call = self.native.prepare_queue(game, link.mod_id, link.file_id)
        revalidate()
        if self._scope() != (game, short_name, root):
            raise ValueError("The managed game or Downloads directory changed before the Nexus request")
        record = {"schema": 1, "download_id": download_id, "mode": mode,
                  "target": target, "downloads_root": str(Path(root).absolute()), "baseline": baseline,
                  "expected_name": expected_name}
        return self._claim_and_submit(record, (game, short_name, root), native_call, before_claim=before_claim)

    def _queue_idle(self, game):
        return (self.native.only_live_mo2_instance() is True
                and self.native.download_queue_idle(game) is True)

    def retry(self, params, *, revalidate, before_claim=None):
        if set(params) != {"download_id", "expected_profile", "expected_revision"}:
            raise ValueError("Retry requires the exact prior download ID and the current profile and revision")
        prior = self._read(params["download_id"])
        if prior is None:
            raise ValueError("No tracked Nexus download exists for this ID")
        scope = self._scope()
        game, short_name, root = scope
        self._ensure_scope(prior, *scope)
        existing = self._successor(prior)
        if existing is not None:
            return self._existing(existing)
        if not self._queue_idle(game):
            raise ValueError("MO2's download queue must be confirmed idle before retrying. Finish or cancel unfinished downloads first.")

        target = dict(prior["target"])
        link = NexusLink(game, target["mod_id"], target["file_id"], short_name)
        native_call = None
        name = prior.get("expected_name")
        if prior["mode"] == "premium":
            info = self._premium_info(link)
            if info is None:
                return {"status": "file_lookup_pending", "download_id": prior["download_id"],
                        "message": "Nexus metadata is being checked. Repeat the retry with the same original ID once it arrives."}
            size = info["size_bytes"]
            name = info["expected_name"] or name
            if size is not None:
                if target.get("size_bytes", size) != size:
                    raise ValueError("The exact Nexus file's byte count disagrees with the original request")
                target["size_bytes"] = size
            native_call = self.native.prepare_queue(game, link.mod_id, link.file_id)
        baseline = capture_retry_baseline(root, target, prior["baseline"], expected_name=name)
        revalidate()
        if self._scope() != scope or not self._queue_idle(game):
            raise ValueError("MO2's download queue or managed location changed before the retry")
        record = {"schema": 2, "download_id": _retry_identity(prior["download_id"]),
                  "previous_download_id": prior["download_id"], "mode": prior["mode"],
                  "downloads_root": prior["downloads_root"], "target": target, "baseline": baseline,
                  "expected_name": name}

        def ready():
            try:
                if self._scope() != scope or not self._queue_idle(game):
                    return False
                # In-place changes to sidecars can leave the directory time
                # unchanged. Re-read the identifying metadata before calling MO2.
                current = capture_retry_baseline(root, target, prior["baseline"], expected_name=name)
                return current == baseline and self._queue_idle(game)
            except (OSError, ValueError, RuntimeError):
                return False

        return self._claim_and_submit(record, scope, native_call, ready=ready, before_claim=before_claim)

    @staticmethod
    def _ensure_scope(record, game, short_name, root):
        target = record["target"]
        if (target["game"] != game or target["game_short"] != short_name
                or Path(record["downloads_root"]) != Path(root).absolute()):
            raise ValueError("This Nexus download belongs to another managed game or Downloads directory")

    def status(self, params):
        if set(params) != {"download_id"}:
            raise ValueError("Check one exact Nexus download ID")
        download_id = params["download_id"]
        record = self._read(download_id)
        if record is None:
            raise ValueError("No tracked Nexus download exists for this ID")
        self._ensure_scope(record, *self._scope())
        result = self._existing(record)
        if result["status"] == "superseded":
            abandoned = self.checks.pop(download_id, None)
            if abandoned is not None:
                abandoned.cancel()
            return result
        future = self.checks.get(download_id)
        if future is not None and future.done():
            del self.checks[download_id]
            try:
                evidence = future.result()
                if evidence["status"] == "verified" and not revalidate_verified_archive(
                        record["downloads_root"], record["target"], record["baseline"], evidence):
                    evidence = {"status": "unverified", "message": "The archive changed after verification. Check this same download ID again."}
            except Exception:
                evidence = {"status": "unverified",
                            "message": "The download evidence could not be verified. Inspect the same MO2 download before retrying."}
            result = self._existing(record)
            if result["status"] == "superseded":
                return result
            if evidence["status"] in {"waiting", "metadata_only"} and self._queue_idle(record["target"]["game"]):
                try:
                    current = capture_retry_baseline(record["downloads_root"], record["target"],
                                                     record["baseline"], expected_name=record.get("expected_name"))
                    same_directory = (current["_directory_proof"] == evidence.get("_directory_proof")
                                      and self._queue_idle(record["target"]["game"]))
                except (OSError, ValueError, RuntimeError):
                    same_directory = False
                if same_directory:
                    evidence = {
                        "status": "no_archive",
                        "message": "The MO2 download queue is idle and no archive or partial file matches this request. An explicit retry can recheck eligibility.",
                    }
                else:
                    evidence = {"status": "unverified",
                                "message": "The bridge could not confirm the archive is absent. Check the same ID or inspect MO2 Downloads."}
            return dict(result, **{key: value for key, value in evidence.items() if not key.startswith("_")})
        if future is None:
            if len(self.checks) >= _MAX_CHECKS:
                done = next((key for key, work in self.checks.items() if work.done()), None)
                if done is None:
                    raise ValueError("Nexus archive checks are busy; check the existing download ID again later")
                del self.checks[done]
            self.checks[download_id] = self.executor.submit(
                inspect_nexus_archive, record["downloads_root"], record["target"], record["baseline"],
                cancelled=self.cancelled.is_set)
        return dict(result, status="checking_archive", message="Checking this exact file in MO2's Downloads directory.")
