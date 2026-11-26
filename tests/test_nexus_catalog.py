from copy import deepcopy
import json
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

from mo2_codex_bridge import nexus_catalog
from mo2_codex_bridge.nexus_catalog import PublicCatalogError, fetch_public_files
from mo2_codex_bridge.nexus_links import normalize_nexus_files


GAME = {"id": "1704", "domainName": "skyrimspecialedition"}
MOD = {"modId": 1234, "adultContent": False, "directDownloadEnabled": False, "game": GAME}
FILE = {"modId": 1234, "game": GAME, "fileId": 111, "name": "Example", "version": "1.2",
        "category": "OPTIONAL", "categoryId": 3, "primary": 0, "manager": 1,
        "sizeInBytes": "12345", "date": 1700000000}


class PublicCatalogTests(unittest.TestCase):
    def _catalog(self, data=None, game=None):
        return patch.object(nexus_catalog, "_graph", side_effect=[
            {"game": GAME if game is None else game},
            {"mod": MOD, "modFiles": [FILE]} if data is None else data,
        ])

    def test_catalog_binds_each_row_to_exact_game_and_mod(self):
        second = {**FILE, "fileId": 112, "version": "1.1", "category": "OLD_VERSION"}
        # Only the whitelisted public fields cross to the normalized result.
        untrusted = {**FILE, "uri": "https://example.org/?key=fixture-secret"}
        with self._catalog({"mod": MOD, "modFiles": [untrusted, second]}) as request:
            payload = fetch_public_files("skyrimspecialedition", 1234)
        first, next_call = request.call_args_list
        self.assertEqual(first.args[1], {"domain": "skyrimspecialedition"})
        self.assertEqual(next_call.args[1], {"gameId": "1704", "modId": "1234"})
        self.assertEqual(first.args[2], next_call.args[2])
        self.assertNotIn("uri", next_call.args[0])
        self.assertNotIn("fixture-secret", json.dumps(payload))
        self.assertIs(payload["direct_download_enabled"], False)
        result = normalize_nexus_files(payload, requested_file_id=112)
        self.assertEqual(result["selected_file"]["version"], "1.1")
        self.assertEqual(result["files"][0]["size_in_bytes"], 12345)
        self.assertIsNone(result["files"][0]["file_name"])

    def test_public_catalog_ignores_the_flag_that_disagrees_with_nexus_download_ui(self):
        rows = [FILE, {**FILE, "fileId": 112, "manager": 0}, {**FILE, "fileId": 113, "manager": None}]
        with self._catalog({"mod": MOD, "modFiles": rows}) as request:
            result = normalize_nexus_files(fetch_public_files("skyrimspecialedition", 1234))
        self.assertNotIn("manager", request.call_args.args[0])
        self.assertEqual([file["file_id"] for file in result["files"]], [111, 112, 113])
        self.assertTrue(all("mod_manager_download" not in file for file in result["files"]))

    def test_wrong_game_or_mod_scope_fails_closed(self):
        wrong_game = {"id": "1705", "domainName": "skyrimspecialedition"}
        for data in (
            {"mod": {**MOD, "modId": 1235}, "modFiles": [FILE]},
            {"mod": {**MOD, "game": wrong_game}, "modFiles": [FILE]},
            {"mod": MOD, "modFiles": [{**FILE, "modId": 5678}]},
            {"mod": MOD, "modFiles": [{**FILE, "game": wrong_game}]},
            {"mod": MOD, "modFiles": [{**FILE, "game": {**GAME, "domainName": "skyrim"}}]},
        ):
            with self.subTest(data=data), self._catalog(data), self.assertRaises(PublicCatalogError):
                fetch_public_files("skyrimspecialedition", 1234)
        with self._catalog(game={**GAME, "domainName": "skyrim"}) as request:
            with self.assertRaises(PublicCatalogError):
                fetch_public_files("skyrimspecialedition", 1234)
            self.assertEqual(request.call_count, 1)

    def test_restricted_or_unclassified_mod_catalog_is_not_returned(self):
        for flag in (None, True, "false", 0):
            data = {"mod": {**MOD, "adultContent": flag}, "modFiles": [FILE]}
            with self.subTest(flag=flag), self._catalog(data), self.assertRaisesRegex(
                PublicCatalogError, "restricted or unclassified"
            ):
                fetch_public_files("skyrimspecialedition", 1234)

    def test_empty_catalog_is_valid_only_with_bound_mod(self):
        with self._catalog({"mod": MOD, "modFiles": []}):
            self.assertEqual(fetch_public_files("skyrimspecialedition", 1234),
                             {"files": [], "direct_download_enabled": False})
        with self._catalog({"mod": None, "modFiles": []}), self.assertRaises(PublicCatalogError):
            fetch_public_files("skyrimspecialedition", 1234)

    def test_optional_data_does_not_supply_hidden_defaults(self):
        data = {"mod": MOD, "modFiles": [{**FILE, "sizeInBytes": None}]}
        with self._catalog(data):
            files = normalize_nexus_files(fetch_public_files("skyrimspecialedition", 1234))
        self.assertIsNone(files["files"][0]["size_in_bytes"])
        self.assertIsNone(files["selected_file"])

    def test_direct_download_setting_is_an_explicit_boolean(self):
        data = {"mod": {**MOD, "directDownloadEnabled": True}, "modFiles": []}
        with self._catalog(data):
            self.assertIs(fetch_public_files("skyrimspecialedition", 1234)["direct_download_enabled"], True)
        data["mod"]["directDownloadEnabled"] = 1
        with self._catalog(data), self.assertRaisesRegex(PublicCatalogError, "invalid mod download setting"):
            fetch_public_files("skyrimspecialedition", 1234)


class PublicTransportTests(unittest.TestCase):
    def setUp(self):
        self.transport = MagicMock()
        self.connection = self.transport.return_value
        self.response = self.connection.getresponse.return_value.__enter__.return_value
        self.response.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.response.getheader.side_effect = lambda header, default=None: self.headers.get(header, default)
        self.response.read.return_value = b'{"data":{"quiet":true}}'
        timer = patch.object(nexus_catalog, "Timer")
        self.timer = timer.start()
        self.addCleanup(timer.stop)

    def _request(self, deadline=None):
        if deadline is None:
            deadline = monotonic() + 5
        with patch.object(nexus_catalog.http.client, "HTTPSConnection", self.transport):
            return nexus_catalog._graph("query T{modId}", {"foo": 42}, deadline)

    def test_fixed_https_post_content_with_app_headers_and_without_credentials(self):
        self.assertEqual(self._request(), {"quiet": True})
        self.transport.assert_called_once()
        self.assertEqual(self.transport.call_args.args, ("api.nexusmods.com",))
        self.assertGreater(self.transport.call_args.kwargs["timeout"], 0)
        method, path, request_body, headers = self.connection.request.call_args.args
        self.assertEqual((method, path), ("POST", "/v2/graphql"))
        self.assertEqual(json.loads(request_body), {"query": "query T{modId}", "variables": {"foo": 42}})
        self.assertEqual(set(headers), {"Accept", "Accept-Encoding", "Content-Type", "Application-Name", "Application-Version"})
        self.assertEqual(headers["Application-Name"], "MO2 Codex Bridge")
        self.timer.return_value.start.assert_called_once()
        self.timer.return_value.cancel.assert_called_once()
        self.connection.close.assert_called_once()

    def test_redirect_and_non_json_rejected_without_following_or_decoding(self):
        self.response.status = 302
        with self.assertRaises(PublicCatalogError):
            self._request()
        self.connection.request.assert_called_once()
        self.response.read.assert_not_called()
        self.response.status = 200
        self.headers = {"Content-Type": "text/html"}
        with self.assertRaises(PublicCatalogError):
            self._request()
        self.response.read.assert_not_called()

    def test_too_large_truncated_and_compressed_responses_do_not_escape(self):
        baseline = deepcopy(self.headers)
        for headers, body in (
            ({"Content-Length": "99999999"}, b""),
            ({"Content-Length": "1234"}, b'{"data":{}}'),
            ({"Content-Encoding": "gzip"}, b""),
            ({}, b"Z" * (nexus_catalog._MAX_BYTES + 1)),
        ):
            with self.subTest(headers=headers):
                self.headers = {**baseline, **headers}
                self.response.read.return_value = body
                with self.assertRaises(PublicCatalogError):
                    self._request()

    def test_duplicate_keys_and_graphql_partial_errors_never_escape(self):
        for body in (
            b'{"data":{},"data":{}}', b'{"data":{"id":NaN}}',
            b'{"errors":[{"message":"fixture-secret"}],"data":{"modFiles":[]}}',
        ):
            with self.subTest(body=body):
                self.response.read.return_value = body
                with self.assertRaises(PublicCatalogError) as refused:
                    self._request()
                self.assertNotIn("fixture-secret", str(refused.exception))

    def test_deadline_expiration_does_not_open_a_connection(self):
        with self.assertRaisesRegex(PublicCatalogError, "timed out"):
            self._request(monotonic() - 1)
        self.transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
