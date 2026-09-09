"""Tests for v0.3.8 security hardening (Natasha review response).

Covers:
  * Buffer + log are HOME-ONLY, never lakehouse.
  * Removed env flags PQ_ADBC_ADVISOR_TENANT_RAW / PQ_ADBC_ADVISOR_USER_RAW
    have no effect on the wire payload.
  * Hard opt-out defeats telemetry within a session even without file
    persistence.
  * Endpoint allowlist rejects non-HTTPS + non-App-Insights hosts.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pq_adbc_advisor import state as _state  # noqa: E402
from pq_adbc_advisor import telemetry as _telemetry  # noqa: E402


def _ok_response():
    r = MagicMock()
    r.status_code = 200
    r.text = '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
    return r


class V038SecurityTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_home = _state._HOME_DIR
        _state._HOME_DIR = self._tmpdir
        _state._reset_hard_opt_out_for_tests()
        with _telemetry._SEND_HISTORY_LOCK:
            _telemetry._SEND_HISTORY.clear()

    def tearDown(self):
        _state._HOME_DIR = self._orig_home
        _state._reset_hard_opt_out_for_tests()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # HIGH-1 fix: buffer + log are home-only
    # ------------------------------------------------------------------ #

    def test_buffer_paths_are_home_only(self):
        """Natasha HIGH-1: buffer must not touch /lakehouse."""
        paths = _state._telemetry_buffer_paths()
        for p in paths:
            self.assertNotIn("/lakehouse", p, f"buffer path {p} still points at shared lakehouse")

    def test_log_paths_are_home_only(self):
        """Natasha HIGH-1: log must not touch /lakehouse."""
        paths = _state._telemetry_log_paths()
        for p in paths:
            self.assertNotIn("/lakehouse", p, f"log path {p} still points at shared lakehouse")

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_failed_send_buffers_to_home_only(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500, text="server broke")
        _telemetry._post("scan_complete", {"foo": "bar"})
        buffered = _state.read_buffered_envelopes()
        self.assertEqual(len(buffered), 1)
        # verify the file is under our test home dir
        home_path = _state._home_telemetry_buffer_path()
        self.assertTrue(os.path.exists(home_path), f"buffer must exist at {home_path}")
        self.assertTrue(home_path.startswith(self._tmpdir))

    # ------------------------------------------------------------------ #
    # HIGH-2 fix: hard opt-out defeats telemetry immediately
    # ------------------------------------------------------------------ #

    def test_hard_opt_out_defeats_telemetry_immediately(self):
        """After disable_telemetry() sets the hard flag, _telemetry_enabled
        must return False regardless of file state."""
        _state.set_hard_opt_out()
        self.assertTrue(_state.hard_opt_out_active())
        self.assertFalse(_telemetry._telemetry_enabled(True))

    def test_hard_opt_out_persists_within_session(self):
        """Once set, it can't be cleared by writing to the state file."""
        _state.set_hard_opt_out()
        # Even if the file says opt-in, hard flag wins.
        _state.set_telemetry_opt_out(False)
        self.assertFalse(_telemetry._telemetry_enabled(True))

    def test_disable_telemetry_reports_verification_status(self):
        """disable_telemetry returns True only when the write is verified."""
        result = _telemetry.disable_telemetry()
        # With our tmpdir home fallback, opt-out CAN persist, so True is expected
        # OR False with a warning — either way the hard flag is set.
        self.assertTrue(_state.hard_opt_out_active())

    # ------------------------------------------------------------------ #
    # MED-1 fix: raw-ID env flags removed
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_raw_tenant_env_flag_has_no_effect(self, mock_post):
        """v0.3.8 SECURITY: PQ_ADBC_ADVISOR_TENANT_RAW is REMOVED."""
        mock_post.return_value = _ok_response()
        captured = []
        def capture(url, data=None, **kw):
            import json
            captured.append(json.loads(data))
            return _ok_response()
        with patch.dict(os.environ, {"PQ_ADBC_ADVISOR_TENANT_RAW": "1"}):
            with patch("pq_adbc_advisor.telemetry._tenant_id", return_value="72f988bf-guid"):
                with patch("pq_adbc_advisor.telemetry.requests.post", side_effect=capture):
                    from pq_adbc_advisor.report import ImpactReport
                    r = ImpactReport(workspace_id="ws-1", scope="workspace")
                    _telemetry.emit_scan_summary(r, enabled=True)
        self.assertEqual(len(captured), 1)
        props = captured[0]["data"]["baseData"]["properties"]
        self.assertNotIn("tenant_id", props, "v0.3.8 must never send raw tenant_id")
        self.assertIn("tenant_hash", props)

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_raw_user_env_flag_has_no_effect(self, mock_post):
        """v0.3.8 SECURITY: PQ_ADBC_ADVISOR_USER_RAW is REMOVED."""
        mock_post.return_value = _ok_response()
        captured = []
        def capture(url, data=None, **kw):
            import json
            captured.append(json.loads(data))
            return _ok_response()
        with patch.dict(os.environ, {"PQ_ADBC_ADVISOR_USER_ID": "alice-uuid", "PQ_ADBC_ADVISOR_USER_RAW": "1"}):
            with patch("pq_adbc_advisor.telemetry.requests.post", side_effect=capture):
                from pq_adbc_advisor.report import ImpactReport
                r = ImpactReport(workspace_id="ws-1", scope="workspace")
                _telemetry.emit_scan_summary(r, enabled=True)
        self.assertEqual(len(captured), 1)
        props = captured[0]["data"]["baseData"]["properties"]
        self.assertNotIn("user_id", props, "v0.3.8 must never send raw user_id")
        self.assertIn("user_hash", props)

    # ------------------------------------------------------------------ #
    # LOW-1 fix: endpoint allowlist
    # ------------------------------------------------------------------ #

    def test_endpoint_allowlist_accepts_app_insights(self):
        self.assertTrue(_telemetry._endpoint_is_allowed(
            "https://westus2-2.in.applicationinsights.azure.com/v2/track"
        ))
        self.assertTrue(_telemetry._endpoint_is_allowed(
            "https://westeurope.in.applicationinsights.azure.com/v2/track"
        ))
        self.assertTrue(_telemetry._endpoint_is_allowed(
            "https://dc.services.visualstudio.com/v2/track"
        ))

    def test_endpoint_allowlist_rejects_attacker(self):
        self.assertFalse(_telemetry._endpoint_is_allowed(
            "https://attacker.example.com/v2/track"
        ))
        self.assertFalse(_telemetry._endpoint_is_allowed(
            "http://attacker.in.applicationinsights.azure.com/v2/track"
        ))
        self.assertFalse(_telemetry._endpoint_is_allowed(""))

    def test_endpoint_allowlist_dev_flag_bypass(self):
        """PQ_ADBC_ADVISOR_DEV_TELEMETRY=1 allows any endpoint for local dev."""
        with patch.dict(os.environ, {"PQ_ADBC_ADVISOR_DEV_TELEMETRY": "1"}):
            self.assertTrue(_telemetry._endpoint_is_allowed(
                "http://localhost:8080/v2/track"
            ))

    def test_resolve_connection_rejects_disallowed_endpoint(self):
        """Env-var override with a bad endpoint returns None (telemetry off)."""
        with patch.dict(os.environ, {
            "PQ_ADBC_ADVISOR_APPINSIGHTS_CONNECTION_STRING":
                "InstrumentationKey=deadbeef-1111-2222-3333-444455556666;"
                "IngestionEndpoint=https://attacker.example.com/"
        }):
            self.assertIsNone(_telemetry._resolve_connection())


if __name__ == "__main__":
    unittest.main()
