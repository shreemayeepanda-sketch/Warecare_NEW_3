"""Tests for the optional update checker: pure helpers and mocked network paths."""
from __future__ import annotations

import io
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from scanner import updater as u

URLOPEN = "scanner.updater.urllib.request.urlopen"


def _ctx_urlopen(body: bytes) -> MagicMock:
    """Build a MagicMock usable as urlopen(): supports `with ... as resp` and `resp.read()`."""
    resp = MagicMock()
    resp.read.return_value = body
    cm = MagicMock()
    cm.__enter__.return_value = resp
    cm.__exit__.return_value = False
    return cm


class TestPureHelpers(unittest.TestCase):
    def test_parse_tag_strips_lowercase_v(self):
        self.assertEqual(u.parse_tag("v1.2.3"), "1.2.3")

    def test_parse_tag_strips_uppercase_v(self):
        self.assertEqual(u.parse_tag("V2.0"), "2.0")

    def test_parse_tag_strips_whitespace(self):
        self.assertEqual(u.parse_tag(" 1.0 "), "1.0")

    def test_parse_tag_none_and_non_string(self):
        self.assertEqual(u.parse_tag(None), "")
        self.assertEqual(u.parse_tag(123), "")
        self.assertEqual(u.parse_tag(["1.0"]), "")

    def test_update_check_defaults(self):
        result = u.UpdateCheck()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.latest, "")
        self.assertEqual(result.current, u.APP_VERSION)
        self.assertEqual(result.detail, "")

    def test_compare_versions_equal(self):
        self.assertEqual(u.compare_versions("1.2.3", "1.2.3"), 0)

    def test_compare_versions_less(self):
        self.assertEqual(u.compare_versions("1.2.3", "1.2.4"), -1)

    def test_compare_versions_uses_numeric_order(self):
        self.assertEqual(u.compare_versions("1.2.10", "1.2.9"), 1)

    def test_compare_versions_pads_missing_components(self):
        self.assertEqual(u.compare_versions("1.2", "1.2.0"), 0)

    def test_compare_versions_major_gap(self):
        self.assertEqual(u.compare_versions("1.0", "0.9"), 1)

    def test_compare_versions_non_numeric_is_undecidable(self):
        self.assertEqual(u.compare_versions("abc", "1.0"), 0)

    def test_compare_versions_non_string_is_undecidable(self):
        self.assertEqual(u.compare_versions(None, "1.0"), 0)


class TestCheckForUpdate(unittest.TestCase):
    def _check(self, body: bytes):
        with patch(URLOPEN, return_value=_ctx_urlopen(body)) as mocked:
            result = u.check_for_update()
        return result, mocked

    def test_update_available_on_newer_tag(self):
        result, mocked = self._check(b'{"tag_name": "v1.1.0"}')
        self.assertEqual(result.status, "update_available")
        self.assertEqual(result.latest, "1.1.0")
        self.assertEqual(result.current, u.APP_VERSION)
        self.assertEqual(result.detail, "update available: 1.1.0 (current 1.0.0).")
        # context manager was used, so the response is always closed
        self.assertTrue(mocked.return_value.__exit__.called)

    def test_up_to_date_on_same_tag(self):
        result, _ = self._check(b'{"tag_name": "v1.0.0"}')
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.latest, "1.0.0")

    def test_local_newer_reports_up_to_date_with_detail(self):
        result, _ = self._check(b'{"tag_name": "v0.9.0"}')
        self.assertEqual(result.status, "up_to_date")
        self.assertEqual(result.detail, "local version is newer (pre-release?)")

    def test_request_carries_user_agent_and_timeout(self):
        with patch(URLOPEN, return_value=_ctx_urlopen(b'{"tag_name": "v1.0.0"}')) as mocked:
            u.check_for_update()
        args, kwargs = mocked.call_args
        req = args[0]
        headers = {key.lower(): value for key, value in req.headers.items()}
        self.assertEqual(headers.get("user-agent"),
                         f"SmartHardwareDiagnosticScanner/{u.APP_VERSION}")
        self.assertEqual(kwargs.get("timeout"), 4.0)

    def test_http_error_404_is_unavailable(self):
        error = urllib.error.HTTPError(u.DEFAULT_RELEASES_URL, 404, "Not Found", None,
                                       io.BytesIO(b""))
        self.addCleanup(error.close)  # avoid an unclosed-resource ResourceWarning
        with patch(URLOPEN, side_effect=error):
            result = u.check_for_update()
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.latest, "")
        self.assertIn("404", result.detail)

    def test_urlerror_is_unavailable(self):
        with patch(URLOPEN, side_effect=urllib.error.URLError("connection refused")):
            result = u.check_for_update()
        self.assertEqual(result.status, "unavailable")
        self.assertIn("unreachable", result.detail)

    def test_socket_timeout_is_unavailable(self):
        with patch(URLOPEN, side_effect=socket.timeout("read timed out")):
            result = u.check_for_update(timeout=2.5)
        self.assertEqual(result.status, "unavailable")
        self.assertIn("timed out", result.detail)

    def test_non_json_body_is_unavailable(self):
        result, _ = self._check(b"<html>Gone fishing</html>")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("JSON", result.detail)

    def test_json_scalar_is_unavailable(self):
        result, _ = self._check(b'"just a string"')
        self.assertEqual(result.status, "unavailable")

    def test_missing_tag_name_is_unavailable(self):
        result, _ = self._check(b'{"name": "Release 1.1.0"}')
        self.assertEqual(result.status, "unavailable")
        self.assertIn("tag_name", result.detail)

    def test_failure_does_not_invent_a_latest_version(self):
        result, _ = self._check(b"{not json")
        self.assertEqual(result.latest, "")
        self.assertEqual(result.current, u.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
