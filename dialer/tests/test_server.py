"""Tests for server.py Flask routes — auth gate, /api/booking validation,
/api/call/* lifecycle, secret redaction.

Run with: cd dialer && pytest tests/test_server.py -v
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Make sure no API keys leak into tests via env
os.environ.pop("ANTHROPIC_API_KEY", None)
os.environ.pop("DIALER_AUTH_TOKEN", None)

import server


@pytest.fixture
def client():
    server.DIALER_AUTH_TOKEN = ""
    return server.app.test_client()


@pytest.fixture
def cleanup_demo():
    """Wipe any test rows after each test."""
    yield
    with server._db() as db:
        db.execute("DELETE FROM bookings WHERE lead_id LIKE 'test-%'")
        db.execute("DELETE FROM call_sessions WHERE lead_id LIKE 'test-%'")
        db.execute("DELETE FROM call_session_entries WHERE call_session_id LIKE 'cs_test-%'")
        db.execute("DELETE FROM call_session_summaries WHERE lead_id LIKE 'test-%'")
        db.commit()


# ── _redact_log_line — secret scrubbing for /api/_debug/server-log ─────────

class TestRedactLogLine:
    def test_anthropic_key(self):
        out = server._redact_log_line("using sk-ant-api03-AbcDef0123456789abcdef")
        assert "sk-ant-api03-Abc" not in out

    def test_twilio_account_sid(self):
        # Fake-but-shape-matching SID (not a real account)
        fake_sid = "AC" + "0" * 32
        out = server._redact_log_line(f"TWILIO_ACCOUNT_SID={fake_sid}")
        assert fake_sid not in out

    def test_twilio_api_key_sid(self):
        fake_key = "SK" + "0" * 32
        out = server._redact_log_line(f"API_KEY={fake_key}")
        assert fake_key not in out

    def test_jwt(self):
        out = server._redact_log_line("Authorization: Bearer eyJhbGciOiJIUzI1NiI.eyJzdWIiOiIxMjM0NTY3.SflKxwRJSMeKKF")
        assert "eyJhbGciOiJIUzI1NiI.eyJzdWIiOiIxMjM0NTY3" not in out

    def test_deepgram_40hex(self):
        out = server._redact_log_line("DEEPGRAM_API_KEY=ccd99767bf3571774326742f0bccd4495903bc6d")
        assert "ccd99767bf3571774326742f0bccd4495903bc6d" not in out

    def test_bearer_token(self):
        out = server._redact_log_line("Authorization: Bearer abcdef1234567890ABCDEF1234567890")
        assert "abcdef1234567890ABCDEF1234567890" not in out

    def test_innocuous_line_unchanged(self):
        out = server._redact_log_line("INFO 2026-05-18 16:32:11 GET /healthz 200")
        assert "GET /healthz 200" in out


# ── /api/* auth gate ───────────────────────────────────────────────────────

class TestAuthGate:
    def test_no_token_configured_allows_through(self, client):
        # Default dev mode: DIALER_AUTH_TOKEN unset → endpoints open
        r = client.post("/api/call/start", json={"lead_id": "test-x"})
        assert r.status_code == 200
        # cleanup
        sid = r.get_json()["call_session_id"]
        with server._db() as db:
            db.execute("DELETE FROM call_sessions WHERE call_session_id=?", (sid,))
            db.commit()

    def test_token_configured_rejects_no_header(self, client):
        server.DIALER_AUTH_TOKEN = "secret123"
        try:
            r = client.post("/api/call/start", json={"lead_id": "test-x"})
            assert r.status_code == 401
        finally:
            server.DIALER_AUTH_TOKEN = ""

    def test_token_configured_rejects_wrong_header(self, client):
        server.DIALER_AUTH_TOKEN = "secret123"
        try:
            r = client.post("/api/call/start", json={"lead_id": "test-x"},
                            headers={"X-Dialer-Token": "wrong"})
            assert r.status_code == 401
        finally:
            server.DIALER_AUTH_TOKEN = ""

    def test_token_configured_accepts_correct_header(self, client, cleanup_demo):
        server.DIALER_AUTH_TOKEN = "secret123"
        try:
            r = client.post("/api/call/start", json={"lead_id": "test-x"},
                            headers={"X-Dialer-Token": "secret123"})
            assert r.status_code == 200
        finally:
            server.DIALER_AUTH_TOKEN = ""

    def test_non_api_routes_not_gated(self, client):
        server.DIALER_AUTH_TOKEN = "secret123"
        try:
            r = client.get("/healthz")
            assert r.status_code == 200  # /healthz outside /api/*
        finally:
            server.DIALER_AUTH_TOKEN = ""


# ── /api/booking validation ────────────────────────────────────────────────

class TestBooking:
    def test_required_fields(self, client):
        r = client.post("/api/booking", json={})
        assert r.status_code == 400

    def test_hostile_gcal_url_rejected(self, client, cleanup_demo):
        r = client.post("/api/booking", json={
            "lead_id": "test-1", "type": "callback", "title": "T",
            "start_iso": "2026-05-19T15:00:00-04:00",
            "gcal_url": "javascript:alert(1)",
        })
        assert r.status_code == 400
        body = r.get_json()
        assert "gcal_url" in body.get("error", "")

    def test_good_gcal_url_accepted(self, client, cleanup_demo):
        r = client.post("/api/booking", json={
            "lead_id": "test-1", "type": "callback", "title": "T",
            "start_iso": "2026-05-19T15:00:00-04:00",
            "gcal_url": "https://calendar.google.com/eventedit?text=foo",
        })
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert "id" in body


# ── /api/call/* lifecycle ──────────────────────────────────────────────────

class TestCallLifecycle:
    def test_start_returns_session_id(self, client, cleanup_demo):
        r = client.post("/api/call/start", json={"lead_id": "test-a"})
        assert r.status_code == 200
        sid = r.get_json()["call_session_id"]
        assert sid.startswith("cs_")

    def test_start_requires_lead_id(self, client):
        r = client.post("/api/call/start", json={})
        assert r.status_code == 400

    def test_entry_pre_end_works(self, client, cleanup_demo):
        sid = client.post("/api/call/start", json={"lead_id": "test-b"}).get_json()["call_session_id"]
        r = client.post(f"/api/call/{sid}/entry", json={"role": "agent", "text": "hi"})
        assert r.status_code == 200

    def test_entry_post_end_rejected_410(self, client, cleanup_demo):
        sid = client.post("/api/call/start", json={"lead_id": "test-c"}).get_json()["call_session_id"]
        client.post(f"/api/call/{sid}/end", json={"outcome": "no_answer"})
        # Late-arriving entry must be rejected
        r = client.post(f"/api/call/{sid}/entry", json={"role": "prospect", "text": "late"})
        assert r.status_code == 410

    def test_entry_unknown_sid_rejected_404(self, client):
        r = client.post("/api/call/cs_bogus_xxxxxxxx/entry", json={"role": "agent", "text": "x"})
        assert r.status_code == 404

    def test_end_idempotent(self, client, cleanup_demo):
        sid = client.post("/api/call/start", json={"lead_id": "test-d"}).get_json()["call_session_id"]
        end1 = client.post(f"/api/call/{sid}/end", json={"outcome": "voicemail"})
        end2 = client.post(f"/api/call/{sid}/end", json={"outcome": "should_not_overwrite"})
        b1 = end1.get_json()
        b2 = end2.get_json()
        assert b2.get("already_ended") is True
        # Outcome from second call must NOT overwrite the first
        assert b2["outcome"] == "voicemail"
        assert b2["summary"] == b1["summary"]

    def test_history_lists_calls(self, client, cleanup_demo):
        sid = client.post("/api/call/start", json={"lead_id": "test-e"}).get_json()["call_session_id"]
        client.post(f"/api/call/{sid}/end", json={"outcome": "no_answer"})
        r = client.get("/api/call/lead/test-e/history")
        assert r.status_code == 200
        calls = r.get_json()["calls"]
        assert any(c["call_session_id"] == sid for c in calls)


# ── /api/_debug/server-log access control ──────────────────────────────────

class TestDebugLogEndpoint:
    def test_localhost_allowed_in_dev(self, client):
        # Flask test_client sets remote_addr to 127.0.0.1
        server.DIALER_AUTH_TOKEN = ""
        r = client.get("/api/_debug/server-log")
        assert r.status_code == 200
        body = r.get_json()
        assert "stdout" in body and "stderr" in body
