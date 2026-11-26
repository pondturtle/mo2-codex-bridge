import unittest

from mo2_codex_bridge.nexus_links import (
    NexusLink, managed_nexus_game, normalize_nexus_file, normalize_nexus_files, parse_nexus_link,
)


MOD = "https://www.nexusmods.com/skyrimspecialedition/mods/1234"


class ManagedGame:
    def gameShortName(self):
        return "SkyrimSE"

    def gameNexusName(self):
        return "skyrimspecialedition"

    def validShortNames(self):
        return ["Skyrim", "EnderalSE"]


class NexusLinkTests(unittest.TestCase):
    def test_mod_page_has_no_implicit_selection(self):
        link = parse_nexus_link(MOD + "?tab=description", ManagedGame())
        self.assertEqual(link, NexusLink("skyrimspecialedition", 1234, None, "SkyrimSE"))
        self.assertEqual(link.files_url, MOD + "?tab=files")
        self.assertEqual(link.file_page(765), MOD + "?tab=files&file_id=765")
        self.assertIsNone(link.file_url)
        self.assertIsNone(link.manual_download_url)

    def test_exact_file_and_manual_handoff_use_canonical_public_urls(self):
        link = parse_nexus_link(
            "HTTPS://NEXUSMODS.COM/SKYRIMSPECIALEDITION/mods/1234/?nmm=0&file_id=765"
            "&utm_source=example&tab=files", ManagedGame(), file_id=765,
        )
        self.assertEqual(link.file_id, 765)
        self.assertEqual(link.file_url, MOD + "?tab=files&file_id=765")
        self.assertEqual(link.manual_download_url, MOD + "?tab=files&file_id=765&nmm=1")

    def test_explicit_file_id_and_exact_files_tab_anchor(self):
        plain = parse_nexus_link(MOD, ManagedGame(), file_id=765)
        anchor = parse_nexus_link(MOD + "?tab=files#file-expander-header-765", ManagedGame())
        self.assertEqual(plain, anchor)

    def test_signed_or_disguised_links_are_rejected_without_echoing_inputs(self):
        for bad in (
            "nxm://skyrimspecialedition/mods/1234/files/765?key=private123",
            MOD + "?file_id=765&key=private123",
            MOD + "?file_id=765&%6bey=private123",
            MOD + "?file_id=765&expires=9999&user_id=123",
            MOD.replace("https:", "http:"),
            MOD.replace("www.nexusmods.com", "www.nexusmods.com.evil.example"),
            MOD.replace("www.nexusmods.com", "evil@www.nexusmods.com"),
            MOD.replace("www.nexusmods.com", "www.nexusmods.com:443"),
            MOD.replace("www.nexusmods.com", "www.nexusmods.com\\@evil.example"),
            MOD.replace("www", "www."),
            MOD.replace("/mods/", "/mods%2f"),
            " " + MOD, MOD + "\n", MOD + "\x00",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(ValueError) as refused:
                    parse_nexus_link(bad, ManagedGame())
                self.assertNotIn("private123", str(refused.exception))

    def test_malformed_duplicate_or_conflicting_selection_is_rejected(self):
        for query in (
            "?tab=files&file_id=7&file%5fid=7",
            "?tab=files&file_id=7&tab=files",
            "?tab=files&file_id=7&",
            "?tab=description&file_id=7",
            "?tab=files&file_id=7#file-expander-header-8",
            "?tab=files#arbitrary-section",
            "#file-expander-header-8",
            "?tab=files&file_id=0",
            "?tab=files&file_id=-7",
            "?tab=files&file_id=7%3bfile_id%3d8",
            "?tab=files&file_id=2147483648",
            "?tab=files&nmm=1",
            "?tab=files&file_id=7&nmm=2",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                parse_nexus_link(MOD + query, ManagedGame())
        with self.assertRaisesRegex(ValueError, "conflicts"):
            parse_nexus_link(MOD + "?file_id=765", ManagedGame(), file_id=766)

    def test_wrong_nexus_game_and_distinct_valid_short_names_are_never_aliases(self):
        for other in ("skyrim", "skyrimse", "enderalse", "fallout4"):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, "different game"):
                parse_nexus_link(MOD.replace("skyrimspecialedition", other), ManagedGame())
        self.assertEqual(managed_nexus_game(ManagedGame()), "skyrimspecialedition")
        with self.assertRaises(ValueError):
            parse_nexus_link(MOD, object())

    def test_strict_explicit_number_and_dataclass_validation(self):
        with self.assertRaises(ValueError):
            parse_nexus_link(MOD, ManagedGame(), file_id=True)
        with self.assertRaises(ValueError):
            parse_nexus_link(MOD, ManagedGame(), file_id="765")
        with self.assertRaises(ValueError):
            NexusLink("other.game/slug", 1, None, "SkyrimSE")
        with self.assertRaises(ValueError):
            parse_nexus_link(MOD.replace("1234", "0001"), ManagedGame())


class NexusFileMetadataTests(unittest.TestCase):
    def test_public_fields_are_preserved_exactly_and_other_fields_stay_private(self):
        raw = {"file_id": 7, "name": "Example β", "file_name": "Example-1_3.7z", "version": "1.3",
               "category_id": 3, "category_name": "OPTIONAL", "size_in_bytes": 200,
               "is_primary": False, "uploaded_timestamp": 1700000000,
               "uri": "https://cdn.example.com/export?key=private123",
               "changelog_html": "<p>untrusted</p>", "user_id": 12}
        expected = {key: raw[key] for key in raw if key not in {"uri", "changelog_html", "user_id"}}
        self.assertEqual(normalize_nexus_file(raw, requested_file_id=7), expected)

    def test_public_manager_hint_never_influences_normalized_file_metadata(self):
        reference = normalize_nexus_file({"file_id": 7})
        for unreliable in (None, False, True, 0, "1"):
            self.assertEqual(normalize_nexus_file({"file_id": 7, "mod_manager_download": unreliable}),
                             reference)

    def test_catalog_distinguishes_similar_files_without_auto_selecting(self):
        raw = {"files": [{"file_id": 7, "name": "Same Name", "version": "1.3", "category_name": "MAIN"},
                         {"file_id": 8, "name": "Same Name", "version": "1.2", "category_name": "OLD_VERSION"},
                         {"file_id": 9, "name": "Same Name", "version": "1.3", "category_name": "OPTIONAL"}]}
        self.assertIsNone(normalize_nexus_files(raw)["selected_file"])
        selected = normalize_nexus_files(raw, requested_file_id=8)
        self.assertEqual([x["category_name"] for x in selected["files"]], ["MAIN", "OLD_VERSION", "OPTIONAL"])
        self.assertEqual(selected["selected_file"]["version"], "1.2")

    def test_duplicate_missing_or_wrong_file_ids_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            normalize_nexus_files({"files": [{"file_id": 7}, {"file_id": 7}]}, requested_file_id=7)
        with self.assertRaisesRegex(ValueError, "not found"):
            normalize_nexus_files({"files": [{"file_id": 7}]}, requested_file_id=8)
        with self.assertRaisesRegex(ValueError, "different file"):
            normalize_nexus_file({"file_id": 7}, requested_file_id=8)

    def test_untrusted_paths_and_confusing_types_are_rejected(self):
        for raw in (
            {"file_id": False}, {"file_id": "42"}, {"file_id": 7, "file_name": "../archive.7z"},
            {"file_id": 7, "file_name": "bad.zip:hidden"}, {"file_id": 7, "file_name": "file.zip "},
            {"file_id": 7, "file_name": "archive.7z\n"}, {"file_id": 7, "name": "prompt\x1b[1A"},
            {"file_id": 7, "is_primary": 1}, {"file_id": 7, "size_in_bytes": True},
        ):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                normalize_nexus_file(raw)
        with self.assertRaises(ValueError):
            normalize_nexus_files({"files": [{"file_id": 1}] * 5001})


if __name__ == "__main__":
    unittest.main()
