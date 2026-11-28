"""Read-only fixture tests for exact MO2 transfer-start evidence."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mo2_codex_bridge import nexus_archive as archive


TARGET = {"game": "skyrimspecialedition", "game_short": "SkyrimSE", "mod_id": 91, "file_id": 42}
PARTIAL = "Exact.7z.unfinished"


class NexusTransferStartTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)
        self.baseline = archive.capture_archive_baseline(self.root, TARGET)

    def observe(self, target=TARGET, **kwargs):
        return archive.observe_nexus_transfer_start(self.root, target, self.baseline, **kwargs)

    def sidecar(self, name, *, file_id=42, paused="false", removed="false"):
        (self.root / (name + ".meta")).write_text(
            f"[General]\nrepository=Nexus\ngameName=SkyrimSE\nmodID=91\nfileID={file_id}\n"
            f"paused={paused}\nremoved={removed}\n", encoding="utf-8")

    def file(self, name, **metadata):
        path = self.root / name
        path.write_bytes(b"payload")
        self.sidecar(name, **metadata)
        return path

    def test_exact_partial_proves_start_without_opening_payload_or_hashing(self):
        self.file(PARTIAL)
        self.file("Similar.7z.unfinished", file_id=43)
        with (patch.object(archive, "_read_handle", wraps=archive._read_handle) as read_handle,
              patch.object(archive.hashlib, "sha256", side_effect=AssertionError("Payload hashed"))):
            self.assertTrue(self.observe())
        self.assertTrue(read_handle.call_args_list)
        for call in read_handle.call_args_list:
            self.assertEqual(call.args[0].suffix, ".meta")

    def test_exact_finished_archive_accepts_known_or_unknown_size_and_old_partial_sidecar(self):
        self.sidecar(PARTIAL)
        self.file("Exact.7z")
        self.assertTrue(self.observe())
        self.assertTrue(self.observe(dict(TARGET, size_bytes=7)))
        self.assertFalse(self.observe(dict(TARGET, size_bytes=8)))
        (self.root / "Exact.7z").write_bytes(b"")
        self.assertFalse(self.observe())

    def test_requires_actual_positive_payload_and_exact_current_sidecar(self):
        self.sidecar(PARTIAL)
        self.assertFalse(self.observe())
        payload = self.root / PARTIAL
        payload.touch()
        self.assertFalse(self.observe())
        payload.write_bytes(b"payload")
        for fields in ({"paused": "true"}, {"removed": "true"}, {"file_id": 43}):
            with self.subTest(fields=fields):
                self.sidecar(PARTIAL, **fields)
                self.assertFalse(self.observe())
        self.sidecar(PARTIAL)
        self.sidecar("Exact.7z")
        self.assertFalse(self.observe())

    def test_existing_alias_and_two_exact_transfers_are_not_new_unique_evidence(self):
        self.file(PARTIAL, file_id=43)
        self.baseline = archive.capture_archive_baseline(self.root, TARGET)
        self.sidecar(PARTIAL)
        self.assertFalse(self.observe())
        self.file("Duplicate.zip")
        self.assertFalse(self.observe())

    def test_hardlinked_or_cancelled_evidence_is_rejected(self):
        payload = self.file(PARTIAL)
        self.assertFalse(self.observe(cancelled=lambda: True))
        lstat = Path.lstat

        def linked_lstat(path, *args, **kwargs):
            info = lstat(path, *args, **kwargs)
            if path == payload:
                return SimpleNamespace(st_mode=info.st_mode, st_ino=info.st_ino, st_nlink=2)
            return info

        with patch.object(Path, "lstat", linked_lstat):
            self.assertFalse(self.observe())

    def test_partial_can_grow_without_changing_filesystem_identity(self):
        payload = self.file(PARTIAL)
        scan = archive._scan

        def grow_on_rescan(*args, **kwargs):
            result = scan(*args, **kwargs)
            if observer.call_count == 2:
                with payload.open("ab") as stream:
                    stream.write(b"more")
            return result

        with patch.object(archive, "_scan", side_effect=grow_on_rescan) as observer:
            self.assertTrue(self.observe())

    def test_refuses_directory_change_and_competing_metadata_rewrite(self):
        self.file(PARTIAL)
        self.file("Other.7z", file_id=43)
        scan = archive._scan

        def changed_on_scan(*args, **kwargs):
            result = scan(*args, **kwargs)
            if observer.call_count == 1:
                self.sidecar("Other.7z")
            return result

        with patch.object(archive, "_scan", side_effect=changed_on_scan) as observer:
            self.assertFalse(self.observe())

        self.sidecar("Other.7z", file_id=43)

        def paused_during_scan(*args, **kwargs):
            result = scan(*args, **kwargs)
            if observer.call_count == 2:
                self.sidecar(PARTIAL, paused="true")
            return result

        with patch.object(archive, "_scan", side_effect=paused_during_scan) as observer:
            self.assertFalse(self.observe())

        self.sidecar(PARTIAL)

        def changed_directory(*args, **kwargs):
            result = scan(*args, **kwargs)
            if observer.call_count == 2:
                (self.root / "New file").touch()
            return result

        with patch.object(archive, "_scan", side_effect=changed_directory) as observer:
            self.assertFalse(self.observe())
