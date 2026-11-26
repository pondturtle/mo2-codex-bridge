"""Offline fixtures for MO2's paired Nexus archive and metadata files."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from mo2_codex_bridge import nexus_archive as archive


TARGET = {"game": "skyrimspecialedition", "game_short": "skyrimse", "mod_id": 734, "file_id": 319}
DATA = b"fixture only\x00\xff"


class NexusArchiveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name) / "downloads"
        self.root.mkdir()
        self.baseline = self.capture()

    def capture(self):
        result = archive.capture_archive_baseline(self.root, TARGET)
        return json.loads(json.dumps(result))

    def inspect(self, target=TARGET):
        return archive.inspect_nexus_archive(self.root, target, self.baseline)

    def metadata(self, filename, **values):
        fields = {"gameName": "SkyrimSE", "modID": "734", "fileID": "319",
                  "repository": "Nexus", "paused": "false", "removed": "false",
                  "url": "https://invalid.example/?token=never-return-this"}
        fields.update(values)
        path = self.root / (filename + ".meta")
        path.write_text("[General]\n" + "\n".join(f"{key}={value}" for key, value in fields.items()) + "\n",
                        encoding="utf-8")
        return path

    def download(self, filename="Example.7z", *, data=DATA, **values):
        (self.root / filename).write_bytes(data)
        self.metadata(filename, **values)
        return self.root / filename

    def assert_status(self, expected, target=TARGET):
        status = self.inspect(target)
        self.assertEqual(status["status"], expected, status)
        self.assertEqual("archive" in status, expected == "verified", status)
        self.assertNotIn("never-return-this", json.dumps(status))
        return status

    def test_exact_ids_choose_the_archive_among_similar_filenames(self):
        self.download("Example.7z", fileID="318")
        self.download("Example copy.7z", modID="733")
        selected = self.download("1_Example.7z", gameName="skyrimspecialedition")
        result = self.assert_status("verified")["archive"]
        self.assertEqual(result, {
            "name": selected.name, "path": str(selected), "bytes": len(DATA),
            "sha256": hashlib.sha256(DATA).hexdigest(),
        })

    def test_filename_is_not_identity(self):
        (self.root / "skyrimspecialedition-734-319.7z").write_bytes(DATA)
        self.download("Another.7z", gameName="skyrim")
        self.download("Copied.7z", repository="Other")
        self.assert_status("waiting")

    def test_file_ids_use_canonical_numbers(self):
        self.download("Bad.7z", modID="+734", fileID="0319")
        self.assert_status("waiting")
        with self.assertRaises(ValueError):
            self.inspect(dict(TARGET, mod_id=True))

    def test_unfinished_payload_never_handed_off(self):
        self.download("Example.7z.unfinished")
        self.assert_status("incomplete")
        self.assertEqual(self.capture()["existing"][0]["state"], "incomplete")
        (self.root / "Example.7z").write_bytes(DATA)
        self.metadata("Example.7z")
        self.assert_status("incomplete")

    def test_orphaned_unfinished_sidecar_does_not_obscure_finished_archive(self):
        self.metadata("Example.7z.unfinished")
        self.assert_status("metadata_only")
        self.download()
        self.assert_status("verified")

    def test_final_sidecar_without_payload_is_metadata_only(self):
        self.metadata("Example.7z")
        self.assert_status("metadata_only")

    def test_paused_or_hidden_sidecar_never_handed_off(self):
        self.download(paused="true")
        self.assert_status("incomplete")
        self.metadata("Example.7z", removed="true")
        self.assert_status("incomplete")

    def test_two_exact_archives_are_ambiguous(self):
        self.download("First.7z")
        self.download("Second.zip")
        self.assert_status("ambiguous")
        self.assertEqual(len(self.capture()["existing"]), 2)

    def test_second_incomplete_exact_file_is_ambiguous(self):
        self.download()
        self.download("Second.7z.unfinished")
        self.assert_status("ambiguous")

    def test_replacing_pre_request_file_does_not_create_new_evidence(self):
        existing = self.download(gameName="another")
        self.baseline = self.capture()
        self.assertFalse(self.baseline["existing"])
        existing.unlink()
        self.download(data=b"new-contents")
        self.assert_status("preexisting")

    def test_pre_request_partial_reserves_final_name(self):
        old = self.download("Example.7z.unfinished")
        self.baseline = self.capture()
        old.rename(self.root / "Example.7z")
        self.metadata("Example.7z")
        self.assert_status("preexisting")

    def test_pre_request_completed_file_is_reported_without_exposing_archive(self):
        self.download()
        self.baseline = self.capture()
        self.assertEqual(self.baseline["existing"], [{"name": "Example.7z", "state": "complete"}])
        self.assert_status("preexisting")

    def test_retry_accepts_only_a_new_exact_unfinished_sidecar_without_touching_it(self):
        other = self.download("Unrelated.zip", fileID="42")
        old = self.metadata("Example.7z.unfinished")
        original = old.read_bytes()
        ordinary = self.capture()
        self.assertIn("example.7z", ordinary["names"])

        retry = archive.capture_retry_baseline(self.root, TARGET, self.baseline, expected_name="Example.7z")
        self.assertEqual(retry["names"], ["unrelated.zip"])
        self.assertEqual(retry["existing"], [])
        self.assertEqual(old.read_bytes(), original)
        self.assertTrue(other.exists())
        self.assertEqual(archive.inspect_nexus_archive(self.root, TARGET, retry)["status"], "metadata_only")

        self.download()
        self.assertEqual(archive.inspect_nexus_archive(self.root, TARGET, ordinary)["status"], "preexisting")
        self.assertEqual(archive.inspect_nexus_archive(self.root, TARGET, retry)["status"], "verified")

    def test_retry_checks_that_the_previous_root_was_not_replaced(self):
        self.root.rename(self.root.with_name("prior"))
        self.root.mkdir()
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_retry_refuses_paused_payload_and_complete_archive(self):
        partial = self.download("Example.7z.unfinished", paused="true")
        with self.assertRaisesRegex(ValueError, "still has"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)
        partial.unlink()
        self.download()
        with self.assertRaisesRegex(ValueError, "still has"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_retry_refuses_multiple_same_identity_sidecars(self):
        self.metadata("Example.7z.unfinished")
        self.metadata("Different.zip.unfinished")
        with self.assertRaisesRegex(ValueError, "Multiple downloads"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_retry_refuses_final_sidecar_even_if_an_unfinished_sidecar_remains(self):
        self.metadata("Example.7z.unfinished")
        self.metadata("Example.7z", fileID="42")
        with self.assertRaisesRegex(ValueError, "still has"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_retry_refuses_an_orphan_which_predates_the_request(self):
        self.metadata("Example.7z.unfinished")
        baseline = self.capture()
        with self.assertRaisesRegex(ValueError, "still has"):
            archive.capture_retry_baseline(self.root, TARGET, baseline)

    def test_retry_refuses_a_new_unidentified_alias(self):
        (self.root / "Unidentified.7z.unfinished").write_bytes(DATA)
        with self.assertRaisesRegex(ValueError, "no readable identifying metadata"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)
        path = self.metadata("Unidentified.7z.unfinished", fileID="0")
        with self.assertRaisesRegex(ValueError, "could not be identified"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)
        self.assertTrue(path.exists())

    def test_retry_refuses_new_bare_archive_and_preserves_one_seen_before_request(self):
        path = self.root / "Local.7z"
        path.write_bytes(DATA)
        with self.assertRaisesRegex(ValueError, "no readable identifying metadata"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline, expected_name="Expected.7z")
        earlier = self.capture()
        retry = archive.capture_retry_baseline(self.root, TARGET, earlier, expected_name="Expected.7z")
        self.assertEqual(retry["names"], ["local.7z"])
        self.assertEqual(path.read_bytes(), DATA)

    def test_retry_requires_identification_of_the_actual_unfinished_payload(self):
        self.download("Unidentified.7z", fileID="42")
        (self.root / "Unidentified.7z.unfinished").write_bytes(DATA)
        with self.assertRaisesRegex(ValueError, "no readable identifying metadata"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_retry_refuses_a_known_filename_with_unreadable_preexisting_metadata(self):
        self.download("Example.7z", fileID="42")
        initial = self.capture()
        original = archive._read_meta

        def read_meta(path, budget):
            if path.name == "Example.7z.meta":
                raise PermissionError("fixture")
            return original(path, budget)

        with patch.object(archive, "_read_meta", side_effect=read_meta):
            with self.assertRaisesRegex(ValueError, "could not be identified"):
                archive.capture_retry_baseline(self.root, TARGET, initial, expected_name="Example.7z")

    def test_retry_refuses_a_native_filename_collision_or_unsafe_name(self):
        self.download("Example.7z", fileID="42")
        with self.assertRaisesRegex(ValueError, "already present"):
            archive.capture_retry_baseline(self.root, TARGET, self.baseline, expected_name="Example.7Z")
        for name in ("../Example.7z", "archive.txt", "Example.zip.unfinished", 123):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "unsafe or unsupported"):
                archive.capture_retry_baseline(self.root, TARGET, self.baseline, expected_name=name)

    def test_retry_refuses_a_directory_change_during_validation(self):
        self.metadata("Example.7z.unfinished")
        original = archive._scan

        def scan(root, selector, **kwargs):
            result = original(root, selector, **kwargs)
            time.sleep(.025)
            self.download("Changed.7z", fileID="42")
            return result

        with patch.object(archive, "_scan", side_effect=scan):
            with self.assertRaisesRegex(ValueError, "changed during retry"):
                archive.capture_retry_baseline(self.root, TARGET, self.baseline)

    def test_duplicate_identity_or_general_section_is_refused(self):
        path = self.download()
        meta = self.root / (path.name + ".meta")
        with meta.open("a", encoding="utf-8") as stream:
            stream.write("FILEID=319\n")
        self.assert_status("waiting")
        self.metadata(path.name)
        with meta.open("a", encoding="utf-8") as stream:
            stream.write("[General]\n")
        self.assert_status("waiting")

    def test_alternate_section_does_not_supply_identity(self):
        self.download()
        meta = self.root / "Example.7z.meta"
        meta.write_text(meta.read_text(encoding="utf-8").replace("[General]", "[Other]"),
                        encoding="utf-8")
        self.assert_status("waiting")

    def test_wrong_nexus_size_or_empty_file_never_succeeds(self):
        self.download()
        self.assert_status("unverified", dict(TARGET, size_bytes=len(DATA) + 1))
        self.assert_status("verified", dict(TARGET, size_bytes=len(DATA)))
        self.download(data=b"")
        self.assert_status("unverified")

    def test_replaced_directory_breaks_request_identity(self):
        self.root.rename(self.root.with_name("prior"))
        self.root.mkdir()
        self.download()
        with self.assertRaisesRegex(ValueError, "no longer matches"):
            self.inspect()

    def test_nonabsolute_or_escape_directory_is_refused(self):
        with self.assertRaisesRegex(ValueError, "absolute local path"):
            archive.capture_archive_baseline("relative-downloads")
        with self.assertRaisesRegex(ValueError, "absolute local path"):
            archive.capture_archive_baseline(self.root / ".." / "downloads")

    def test_hard_links_in_archive_or_sidecar_fail_closed(self):
        path = self.download()
        # The managed Windows workspace may deny hard-link creation. Cover
        # the same inspection boundary synthetically there, and use actual
        # NTFS hard links when the offline native fixture allows them.
        outside = self.root.parent / "outside"
        try:
            os.link(path, outside)
        except OSError:
            original = archive._file_info
            with patch.object(archive, "_file_info", side_effect=lambda p: (
                    self.fail_if_opened() if p == path else original(p))):
                self.assert_status("unverified")
            return
        self.assert_status("unverified")
        outside.unlink()
        os.link(self.root / "Example.7z.meta", outside)
        self.assert_status("waiting")

    def test_competing_result_arriving_during_hash_is_refused(self):
        self.download()
        original = archive._scan
        count = 0

        def scan(root, target, *, cancelled=None):
            nonlocal count
            count += 1
            if count == 2:
                self.download("Competing.zip")
            return original(root, target, cancelled=cancelled)

        with patch.object(archive, "_scan", side_effect=scan):
            self.assert_status("unverified")
        self.assert_status("ambiguous")

    def test_payload_replaced_between_inspection_and_handle_read(self):
        path = self.download()
        original = archive._read_handle

        def open_file(candidate):
            if candidate == path:
                path.rename(path.with_name("retired"))
                path.write_bytes(DATA)
            return original(candidate)

        with patch.object(archive, "_read_handle", side_effect=open_file):
            self.assert_status("unverified")

    def test_a_windows_writer_cannot_be_mistaken_for_stable_file_bytes(self):
        path = self.download()
        if os.name == "nt":
            with path.open("r+b"):
                self.assert_status("unverified")
        self.assert_status("verified")

    def test_publish_revalidation_can_confirm_without_hashing_or_rescanning(self):
        self.download()
        evidence = self.assert_status("verified")
        with (patch.object(archive, "_scan", side_effect=AssertionError("GUI re-scanned all files")),
              patch.object(archive.hashlib, "sha256", side_effect=AssertionError("GUI re-hashed archive"))):
            self.assertTrue(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, evidence))

    def test_publish_revalidation_refuses_archive_mutations_with_original_size_and_mtime(self):
        path = self.download()
        before = path.stat()
        evidence = self.assert_status("verified")
        time.sleep(.025)
        path.write_bytes(b"z" * len(DATA))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertFalse(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, evidence))

    def test_publish_revalidation_refuses_changed_or_new_metadata(self):
        self.download()
        evidence = self.assert_status("verified")
        self.metadata("Example.7z", fileID="318")
        self.assertFalse(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, evidence))
        self.metadata("Example.7z")
        evidence = self.assert_status("verified")
        time.sleep(.025)
        self.download("Competing.zip")
        self.assertFalse(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, evidence))

    def test_publish_revalidation_refuses_outside_path_and_missing_evidence(self):
        self.download()
        evidence = self.assert_status("verified")
        evidence["archive"]["path"] = str(self.root.parent / "Example.7z")
        self.assertFalse(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, evidence))
        self.assertFalse(archive.revalidate_verified_archive(self.root, TARGET, self.baseline, {}))

    def test_archive_verification_cancels_without_touching_files_if_already_stopped(self):
        self.download()
        with patch.object(archive, "_scan", side_effect=AssertionError("Cancelled job scanned the download folder")):
            result = archive.inspect_nexus_archive(self.root, TARGET, self.baseline, cancelled=lambda: True)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("archive", result)

    def test_archive_verification_releases_handles_on_cancellation_between_chunks(self):
        path = self.download()
        chunks = []

        class StopAtFirstChunk:
            def update(self, data):
                chunks.append(data)

        with (patch.object(archive, "_ARCHIVE_CHUNK", 2),
              patch.object(archive.hashlib, "sha256", return_value=StopAtFirstChunk())):
            result = archive.inspect_nexus_archive(self.root, TARGET, self.baseline, cancelled=lambda: bool(chunks))
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("archive", result)
        self.assertEqual(chunks, [DATA[:2]])
        path.unlink()
        (self.root / "Example.7z.meta").unlink()

    @staticmethod
    def fail_if_opened():
        raise ValueError("Fixture: linked archive refused")


if __name__ == "__main__":
    unittest.main()
