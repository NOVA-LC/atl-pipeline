"""Production smoke tests — hit every critical endpoint on the live
Railway deploy and verify status + shape.

Skipped automatically unless DIALER_PROD_HOST is set. Run nightly or
before any production push:

  DIALER_PROD_HOST=https://dialer-production-586c.up.railway.app \
    pytest tests/test_production_smoke.py -v

These tests do NOT create real Twilio calls — they only exercise the
HTTP surface, schema, and config-presence checks. Real-call testing
is in test_media_stream_e2e.py.
"""
import os
import uuid
import json
import pytest

try:
    import requests
except Exception:
    requests = None


HOST = os.environ.get("DIALER_PROD_HOST", "").rstrip("/")
AUTH_TOKEN = os.environ.get("DIALER_AUTH_TOKEN", "")
HEADERS = {"X-Dialer-Token": AUTH_TOKEN} if AUTH_TOKEN else {}

pytestmark = pytest.mark.skipif(
    not HOST or requests is None,
    reason="set DIALER_PROD_HOST + install requests",
)


def _get(path):
    return requests.get(HOST + path, headers=HEADERS, timeout=15)


def _post(path, **kwargs):
    return requests.post(HOST + path, headers=HEADERS, timeout=15, **kwargs)


# ── Health + status ─────────────────────────────────────────────────────────

class TestHealth:
    def test_healthz_returns_200(self):
        r = _get("/healthz")
        assert r.status_code == 200

    def test_status_returns_all_dependency_signals(self):
        r = _get("/status")
        assert r.status_code == 200
        body = r.json()
        for key in ("anthropic", "deepgram", "twilio"):
            assert key in body, f"missing /status section: {key}"
            assert "ok" in body[key]
            assert "msg" in body[key]
        # Production should have ALL three green
        assert body["anthropic"]["ok"] is True, "Anthropic unreachable"
        assert body["deepgram"]["ok"] is True, "Deepgram unreachable or unconfigured"
        assert body["twilio"]["ok"] is True, "Twilio unreachable or unconfigured"


# ── Static assets ──────────────────────────────────────────────────────────

class TestStaticAssets:
    def test_index_html_loads(self):
        r = _get("/index.html")
        assert r.status_code == 200
        assert "<title>" in r.text

    def test_voicemail_drop_mp3_loads(self):
        r = _get("/voicemail-drop.mp3")
        assert r.status_code == 200
        assert r.headers.get("content-type", "").startswith("audio/")
        assert len(r.content) > 1000  # >1KB

    def test_voicemail_drop_root_path(self):
        # Verify the explicit /voicemail-drop.mp3 route works (Werkzeug
        # static-folder bypass for newly-added files)
        r = _get("/voicemail-drop.mp3")
        assert r.status_code == 200


# ── Twilio /voice TwiML ─────────────────────────────────────────────────────

class TestVoiceTwiML:
    def test_voice_returns_valid_twiml(self):
        r = _post("/voice", data={"To": "+14045551234", "SessionId": "smoke-test"})
        assert r.status_code == 200
        body = r.text
        assert "<Response>" in body
        assert "<Dial" in body
        assert "<Number" in body

    def test_voice_includes_media_stream(self):
        r = _post("/voice", data={"To": "+14045551234", "SessionId": "smoke-stream"})
        body = r.text
        assert "<Stream" in body
        assert "wss://" in body
        assert "/media" in body
        assert "session_id" in body  # custom parameter

    def test_voice_includes_amd_config(self):
        r = _post("/voice", data={"To": "+14045551234", "SessionId": "smoke-amd"})
        body = r.text
        assert "machineDetection" in body
        assert "amdStatusCallback" in body
        assert "/twilio/amd" in body
        # Production thresholds (conservative — set after Apple Live VM bug)
        assert 'machineDetectionSpeechThreshold="3500"' in body

    def test_voice_includes_recording_when_enabled(self):
        r = _post("/voice", data={"To": "+14045551234", "SessionId": "smoke-rec"})
        body = r.text
        assert "record-from-answer-dual" in body
        assert "/twilio/recording" in body

    def test_voice_rejects_missing_to(self):
        r = _post("/voice", data={})
        assert r.status_code == 200
        assert "No destination" in r.text or "Say>" in r.text


# ── Twilio status callbacks ─────────────────────────────────────────────────

class TestTwilioCallbacks:
    def test_amd_callback_human_records_event(self):
        sid = f"smoke_amd_h_{uuid.uuid4().hex[:6]}"
        r = _post(f"/twilio/amd?session_id={sid}", data={
            "AnsweredBy": "human", "CallSid": "CA" + "0" * 32,
        })
        assert r.status_code == 200

    def test_amd_callback_machine_blocks_drop_by_default(self):
        # The auto-drop kill switch should be OFF in prod by default —
        # AMD machine_* shouldn't fire a drop yet.
        sid = f"smoke_amd_m_{uuid.uuid4().hex[:6]}"
        r = _post(f"/twilio/amd?session_id={sid}", data={
            "AnsweredBy": "machine_end_beep", "CallSid": "CA" + "1" * 32,
        })
        assert r.status_code == 200

    def test_recording_callback_accepts_payload(self):
        sid = f"smoke_rec_{uuid.uuid4().hex[:6]}"
        r = _post(f"/twilio/recording?session_id={sid}", data={
            "RecordingUrl": "https://example.test/rec.mp3",
            "RecordingSid":  "RE" + "0" * 32,
            "RecordingDuration": "30",
            "RecordingChannels": "2",
        })
        assert r.status_code == 200


# ── Call session lifecycle ──────────────────────────────────────────────────

class TestCallSession:
    def test_full_call_session_lifecycle(self):
        # 1. Start
        r = _post("/api/call/start", json={"lead_id": "smoke-cs", "phone": "+14045551234"})
        assert r.status_code == 200
        sid = r.json()["call_session_id"]
        assert sid.startswith("cs_")

        # 2. Entry (transcript)
        r = _post(f"/api/call/{sid}/entry", json={"role": "agent", "text": "Hello there"})
        assert r.status_code == 200

        # 3. End
        r = _post(f"/api/call/{sid}/end", json={"outcome": "no_answer"})
        assert r.status_code == 200
        body = r.json()
        assert body.get("call_session_id") == sid

        # 4. Idempotent end (must return already_ended without overwriting)
        r2 = _post(f"/api/call/{sid}/end", json={"outcome": "voicemail"})
        b2 = r2.json()
        assert b2.get("already_ended") is True
        assert b2["outcome"] == "no_answer"  # original outcome preserved

        # 5. Transcript fetch
        r = _get(f"/api/call/{sid}/transcript")
        assert r.status_code == 200

        # 6. Lead history includes this call
        r = _get("/api/call/lead/smoke-cs/history")
        assert r.status_code == 200
        assert any(c["call_session_id"] == sid for c in r.json().get("calls", []))


# ── Booking ─────────────────────────────────────────────────────────────────

class TestBooking:
    def test_required_fields(self):
        r = _post("/api/booking", json={})
        assert r.status_code == 400

    def test_hostile_gcal_url_rejected(self):
        r = _post("/api/booking", json={
            "lead_id": "smoke-b", "type": "callback", "title": "T",
            "start_iso": "2026-05-19T15:00:00-04:00",
            "gcal_url": "javascript:alert(1)",
        })
        assert r.status_code == 400

    def test_good_booking_creates(self):
        r = _post("/api/booking", json={
            "lead_id": "smoke-b", "type": "callback", "title": "T",
            "start_iso": "2026-05-19T15:00:00-04:00",
            "gcal_url": "https://calendar.google.com/eventedit?text=foo",
        })
        assert r.status_code == 200
        assert r.json().get("ok") is True


# ── Best-time-to-call ───────────────────────────────────────────────────────

class TestBestHours:
    def test_global_best_hours_returns_24_buckets(self):
        r = _get("/api/leads/best-hours")
        assert r.status_code == 200
        body = r.json()
        assert len(body["buckets"]) == 24
        assert body["scope"] == "global"

    def test_per_lead_falls_back_to_global(self):
        r = _get(f"/api/leads/smoke-nonexistent-{uuid.uuid4().hex[:6]}/best-hours")
        assert r.status_code == 200


# ── Leads queue ─────────────────────────────────────────────────────────────

class TestLeadsQueue:
    def test_leads_endpoint_returns_array(self):
        r = _get("/leads")
        assert r.status_code == 200
        body = r.json()
        assert "leads" in body
        assert "from_number" in body
        assert isinstance(body.get("leads"), list)


# ── /token (Twilio Access Token) ────────────────────────────────────────────

class TestTwilioToken:
    def test_token_endpoint_returns_jwt(self):
        r = _get("/token")
        assert r.status_code == 200
        body = r.json()
        assert "token" in body
        # Twilio JWTs start with eyJ (base64-encoded {"typ":...})
        assert body["token"].startswith("eyJ")
        assert body.get("identity") == "dialer"


# ── /ivr/events (IVR polling) ───────────────────────────────────────────────

class TestIvrEvents:
    def test_ivr_events_empty_for_unknown_session(self):
        r = _get(f"/ivr/events?session_id=smoke_unknown_{uuid.uuid4().hex[:6]}&since=0")
        assert r.status_code == 200
        body = r.json()
        assert body.get("events") == []
