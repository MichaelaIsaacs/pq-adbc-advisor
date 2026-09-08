"""Tests for v0.3.7 telemetry hardening.

Covers:
  * Response body validation — HTTP 200 with 0 items accepted is NOT success.
  * Response body validation — HTTP 200 with itemsAccepted == itemsReceived IS success.
  * Response body validation — HTTP 200 with an `errors` array is NOT success.
  * Captive-portal detection — HTML body in a 2xx is NOT success.
  * Envelope buffer — failed sends are persisted to the state file.
  * Envelope buffer — a successful send flushes previously buffered envelopes.
  * `send_canary()` fires a `canary_ping` event with the correct properties.
  * `telemetry_health()` returns pipeline diagnostics.
  * 15-second timeout — verifies the timeout on the requests.post call.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure the source tree is importable regardless of pip install state.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from pq_adbc_advisor import telemetry as _telemetry  # noqa: E402
from pq_adbc_advisor import state as _state  # noqa: E402


def _fake_response(status: int, body: str):
    r = MagicMock()
    r.status_code = status
    r.text = body
    return r


class TelemetryHardeningTests(unittest.TestCase):

    def setUp(self):
        # Isolate persistent state to a per-test tempdir.
        self._tmpdir = tempfile.mkdtemp()
        self._orig_home = _state._HOME_DIR
        _state._HOME_DIR = self._tmpdir
        # Clear in-memory ring buffer.
        with _telemetry._SEND_HISTORY_LOCK:
            _telemetry._SEND_HISTORY.clear()

    def tearDown(self):
        _state._HOME_DIR = self._orig_home
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------ #
    # HTTP 200 alone is NOT success — must check itemsAccepted
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_http_200_with_zero_items_accepted_is_failure(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":0,"errors":[{"index":0,"statusCode":400}]}'
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        history = _telemetry.get_send_history()
        self.assertTrue(len(history) >= 1)
        self.assertNotIn(history[-1]["status"], ("ok", "retried_ok"))

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_http_200_with_items_accepted_is_success(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        history = _telemetry.get_send_history()
        self.assertEqual(history[-1]["status"], "ok")

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_http_200_with_errors_array_is_failure(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[{"index":0,"statusCode":429}]}'
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        history = _telemetry.get_send_history()
        self.assertNotIn(history[-1]["status"], ("ok", "retried_ok"))

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_captive_portal_html_body_is_failure(self, mock_post):
        mock_post.return_value = _fake_response(
            200, "<!DOCTYPE html><html><body>Please sign in</body></html>"
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        history = _telemetry.get_send_history()
        self.assertEqual(history[-1]["status"], "captive_portal")

    # ------------------------------------------------------------------ #
    # Buffer + flush
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_failed_send_buffers_envelope(self, mock_post):
        mock_post.return_value = _fake_response(500, "server broke")
        _telemetry._post("scan_complete", {"foo": "bar"})
        buffered = _state.read_buffered_envelopes()
        self.assertEqual(len(buffered), 1)
        self.assertEqual(buffered[0]["event_name"], "scan_complete")

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_successful_send_flushes_previously_buffered_envelopes(self, mock_post):
        # First: buffer one envelope by simulating a 500.
        mock_post.return_value = _fake_response(500, "boom")
        _telemetry._post("scan_complete", {"foo": "bar"})
        self.assertEqual(len(_state.read_buffered_envelopes()), 1)

        # Now: a subsequent successful send should flush the buffer.
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
        )
        _telemetry._post("scan_complete", {"foo": "baz"})
        # Buffer should be empty (previous failed envelope was flushed).
        self.assertEqual(len(_state.read_buffered_envelopes()), 0)

    # ------------------------------------------------------------------ #
    # Timeout was bumped from 5s to 15s
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_timeout_is_15_seconds(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        # First call is the _flush check (against no buffer, returns fast).
        # Real POST is the second call — verify timeout.
        for call in mock_post.call_args_list:
            self.assertEqual(call.kwargs.get("timeout"), 15)

    # ------------------------------------------------------------------ #
    # Public canary API
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_send_canary_emits_canary_ping_event(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
        )
        outcome = _telemetry.send_canary(source="unit-test")
        self.assertTrue(outcome["accepted"])
        # Verify the envelope carried name=canary_ping and source=unit-test.
        posted_body = mock_post.call_args_list[-1].kwargs["data"]
        envelope = json.loads(posted_body)
        self.assertEqual(envelope["data"]["baseData"]["name"], "canary_ping")
        self.assertEqual(envelope["data"]["baseData"]["properties"]["source"], "unit-test")
        self.assertEqual(envelope["data"]["baseData"]["properties"]["canary"], "true")

    # ------------------------------------------------------------------ #
    # Health diagnostic
    # ------------------------------------------------------------------ #

    @patch("pq_adbc_advisor.telemetry.requests.post")
    def test_telemetry_health_reports_connection_and_history(self, mock_post):
        mock_post.return_value = _fake_response(
            200, '{"itemsReceived":1,"itemsAccepted":1,"errors":[]}'
        )
        _telemetry._post("scan_complete", {"foo": "bar"})
        health = _telemetry.telemetry_health()
        self.assertIsNotNone(health["connection"])
        self.assertGreaterEqual(len(health["recent_attempts"]), 1)
        self.assertEqual(health["buffered_envelopes"], 0)
        self.assertIsNotNone(health["last_ok_at"])
        self.assertIsNone(health["last_failure"])

    def test_telemetry_health_when_disabled(self):
        with patch("pq_adbc_advisor.telemetry._resolve_connection", return_value=None):
            health = _telemetry.telemetry_health()
            self.assertIsNone(health["connection"])


if __name__ == "__main__":
    unittest.main()
