"""Only a trustworthy Retry-After from the public Nexus response becomes a wait."""
from datetime import datetime, timezone
from time import monotonic
import unittest
from unittest.mock import MagicMock, patch

from mo2_codex_bridge import nexus_catalog
from mo2_codex_bridge.nexus_catalog import PublicCatalogBackoff, PublicCatalogError


DATE = "Tue, 15 Nov 1994 08:12:31 GMT"
LATER = "Tue, 15 Nov 1994 08:12:45 GMT"
NOW = datetime(1994, 11, 15, 8, 12, 31, tzinfo=timezone.utc).timestamp()


class PublicCatalogBackoffTests(unittest.TestCase):
    def setUp(self):
        self.response = MagicMock()
        self.response.__enter__.return_value = self.response
        self.transport = MagicMock()
        self.connection = self.transport.return_value
        self.connection.getresponse.return_value = self.response
        transport_patch = patch.object(nexus_catalog.http.client, "HTTPSConnection", self.transport)
        transport_patch.start()
        self.addCleanup(transport_patch.stop)
        timer_patch = patch.object(nexus_catalog, "Timer")
        timer_patch.start()
        self.addCleanup(timer_patch.stop)

    def request(self, status, headers):
        self.response.status = status
        self.response.getheaders.return_value = headers
        return nexus_catalog._graph("query Fixture{modId}", {}, monotonic() + 5)

    def test_supported_error_statuses_expose_decimal_seconds(self):
        for status, value, seconds in ((429, "0", 0), (429, " 42\t", 42), (503, "3600", 3600)):
            with self.subTest(status=status, value=value):
                with self.assertRaises(PublicCatalogBackoff) as error:
                    self.request(status, [("rEtRy-AfTeR", value)])
                self.assertIs(type(error.exception.retry_after_seconds), int)
                self.assertEqual(error.exception.retry_after_seconds, seconds)
                self.assertEqual(str(error.exception),
                                 "Nexus asked the bridge to wait before requesting its public file catalog again")
        self.assertEqual(self.connection.request.call_count, 3)
        self.response.read.assert_not_called()

    def test_http_date_uses_rounded_up_local_wait(self):
        with patch.object(nexus_catalog, "time", return_value=NOW + 10.25):
            with self.assertRaises(PublicCatalogBackoff) as error:
                self.request(503, [("Retry-After", LATER)])
        self.assertEqual(error.exception.retry_after_seconds, 4)

    def test_server_date_keeps_fast_local_clock_from_cutting_interval_short(self):
        with patch.object(nexus_catalog, "time", return_value=NOW + 40):
            with self.assertRaises(PublicCatalogBackoff) as error:
                self.request(429, [("Retry-After", LATER), ("Date", DATE)])
        self.assertEqual(error.exception.retry_after_seconds, 14)

    def test_past_http_date_is_a_zero_wait(self):
        with patch.object(nexus_catalog, "time", return_value=NOW + 40):
            with self.assertRaises(PublicCatalogBackoff) as error:
                self.request(429, [("Retry-After", LATER)])
        self.assertEqual(error.exception.retry_after_seconds, 0)

    def test_missing_malformed_duplicate_and_other_status_headers_keep_old_failure(self):
        examples = (
            (429, []),
            (429, [("Retry-After", "fixture-secret")]),
            (429, [("Retry-After", "-2")]),
            (429, [("Retry-After", "+2")]),
            (429, [("Retry-After", "1.5")]),
            (429, [("Retry-After", "1, 2")]),
            (429, [("Retry-After", "31"), ("retry-after", "31")]),
            (429, [("Retry-After", "Tue"), ("Retry-After", "15 Nov 1994 08:12:45 GMT")]),
            (503, [("Retry-After", "Wed, 15 Nov 1994 08:12:45 GMT")]),
            (503, [("Retry-After", "Tue, 15 Nov 1994 08:12:45 UTC")]),
            (503, [("Retry-After", "Tue, 32 Nov 1994 08:12:45 GMT")]),
            (500, [("Retry-After", "30")]),
            (302, [("Retry-After", "30")]),
        )
        for status, headers in examples:
            with self.subTest(status=status, headers=headers):
                with self.assertRaises(PublicCatalogError) as error:
                    self.request(status, headers)
                self.assertIs(type(error.exception), PublicCatalogError)
                self.assertEqual(str(error.exception), "Nexus did not return a public file catalog")
        self.assertEqual(self.connection.request.call_count, len(examples))
        self.response.read.assert_not_called()

    def test_catalog_does_not_run_second_query_or_retry_after_first_query_backoff(self):
        self.response.status = 429
        self.response.getheaders.return_value = [("Retry-After", "100")]
        with self.assertRaises(PublicCatalogBackoff) as error:
            nexus_catalog.fetch_public_files("skyrimspecialedition", 1234)
        self.assertEqual(error.exception.retry_after_seconds, 100)
        self.connection.request.assert_called_once()
        self.response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
