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


# ── _dispatch_agent_action — confidence + first-N-seconds gates ────────────
#
# Defense-in-depth: destructive actions (mark_voicemail, press_digit) must
# clear BOTH the agent-confidence threshold AND the call-grace window before
# the server emits them as auto-execute. Below-threshold actions get
# downgraded to suggestions so the JS client never auto-disconnects mid-call.

class TestDispatchAgentActionGates:
    def setup_method(self):
        # Reset trackers per test
        server.IVR_EVENTS.clear()
        server.IVR_SEQ.clear()
        server.IVR_SESSION_STARTED_AT.clear()

    def _events_for(self, sid):
        return list(server.IVR_EVENTS.get(sid, []))

    def test_mark_voicemail_in_grace_window_downgrades(self):
        # Even with high confidence, a mark_voicemail in the first 10s gets
        # downgraded — the call literally just connected.
        sid = "cs_test_grace"
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time()  # just now
        decision = {"action": "mark_voicemail", "confidence": 0.99, "reason": "vm"}
        server._dispatch_agent_action(sid, decision, "leave a message after the beep")
        events = self._events_for(sid)
        kinds = [e["kind"] for e in events]
        # Should be SUGGEST, not auto
        assert "mark_voicemail_suggest" in kinds
        assert "mark_voicemail" not in kinds

    def test_mark_voicemail_low_confidence_downgrades(self):
        sid = "cs_test_lowconf"
        # Past the grace window
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time() - 30.0
        decision = {"action": "mark_voicemail", "confidence": 0.5, "reason": "vm"}
        server._dispatch_agent_action(sid, decision, "leave a message after the beep")
        kinds = [e["kind"] for e in self._events_for(sid)]
        assert "mark_voicemail_suggest" in kinds
        assert "mark_voicemail" not in kinds

    def test_mark_voicemail_auto_when_all_gates_pass(self):
        sid = "cs_test_auto"
        # Past grace + high confidence + transcript matches voicemail rule
        # (classify_caller_party will return party=voicemail conf=0.95 from
        # the deterministic regex)
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time() - 30.0
        decision = {"action": "mark_voicemail", "confidence": 0.95, "reason": "vm"}
        server._dispatch_agent_action(sid, decision, "leave a message after the tone")
        kinds = [e["kind"] for e in self._events_for(sid)]
        assert "mark_voicemail" in kinds
        # The auto-fire path also records party_confidence
        ev = next(e for e in self._events_for(sid) if e["kind"] == "mark_voicemail")
        assert ev.get("party") == "voicemail"
        assert ev.get("auto") is True

    def test_mark_voicemail_ai_receptionist_downgrades(self):
        # Even with high agent confidence + past grace, if the second-opinion
        # classifier says it's an AI receptionist (not voicemail), the
        # auto-fire is blocked. This is the bug Tyler reported: "it wasn't
        # even a live human it was an ai".
        sid = "cs_test_aireception"
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time() - 30.0
        decision = {"action": "mark_voicemail", "confidence": 0.99, "reason": "vm?"}
        server._dispatch_agent_action(
            sid, decision,
            "Thank you for calling Joe's Plumbing, I'm an AI assistant — how can I help?"
        )
        kinds = [e["kind"] for e in self._events_for(sid)]
        assert "mark_voicemail_suggest" in kinds
        assert "mark_voicemail" not in kinds
        ev = next(e for e in self._events_for(sid) if e["kind"] == "mark_voicemail_suggest")
        assert ev.get("party") == "ai_receptionist"

    def test_alert_tyler_includes_party_verdict(self):
        sid = "cs_test_alert"
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time() - 30.0
        decision = {"action": "alert_tyler", "arg": "live person", "confidence": 0.9, "reason": "greeted"}
        server._dispatch_agent_action(
            sid, decision,
            "Thank you for calling Acme — I'm an AI assistant. How may I direct your call?"
        )
        events = self._events_for(sid)
        assert events and events[-1]["kind"] == "alert"
        assert events[-1].get("party") == "ai_receptionist"

    def test_unknown_action_records_error(self):
        sid = "cs_test_unknown"
        server._dispatch_agent_action(sid, {"action": "delete_database"}, "")
        kinds = [e["kind"] for e in self._events_for(sid)]
        assert "agent_error" in kinds

    def test_press_digit_low_confidence_downgrades_to_suggest(self):
        sid = "cs_test_press"
        server.IVR_SESSION_STARTED_AT[sid] = __import__("time").time() - 30.0
        prev_mode = server.IVR_AGENT_MODE
        server.IVR_AGENT_MODE = "auto"
        try:
            server._dispatch_agent_action(
                sid, {"action": "press_digit", "arg": "1", "confidence": 0.3, "reason": "menu"}, "for sales press 1"
            )
            ev = self._events_for(sid)[-1]
            assert ev["kind"] == "ivr_digit"
            assert ev["auto_press"] is False
            assert ev["mode"] == "suggest"
        finally:
            server.IVR_AGENT_MODE = prev_mode

    def test_wait_action_records_agent_wait(self):
        sid = "cs_test_wait"
        server._dispatch_agent_action(sid, {"action": "wait", "reason": "hold music"}, "[music]")
        ev = self._events_for(sid)[-1]
        assert ev["kind"] == "agent_wait"


# ── Twilio AMD (Answering Machine Detection) + voicemail drop ──────────────

class TestTwilioAMD:
    def setup_method(self):
        self._prev_enabled = server.AMD_ENABLED
        self._prev_base    = server.PUBLIC_BASE_URL
        self._prev_drop    = server.VOICEMAIL_DROP_URL
        server.IVR_EVENTS.clear()
        server.IVR_SEQ.clear()
        server.IVR_SESSION_STARTED_AT.clear()

    def teardown_method(self):
        server.AMD_ENABLED       = self._prev_enabled
        server.PUBLIC_BASE_URL   = self._prev_base
        server.VOICEMAIL_DROP_URL = self._prev_drop

    def test_voice_twiml_includes_machine_detection_when_amd_enabled(self, client):
        server.AMD_ENABLED     = True
        server.PUBLIC_BASE_URL = "https://example.test"
        r = client.post("/voice", data={"To": "+14045551234", "SessionId": "cs_abc"})
        assert r.status_code == 200
        body = r.data.decode("utf-8")
        # Twilio TwiML SDK emits attributes in camelCase
        assert "machineDetection" in body
        assert "DetectMessageEnd" in body
        assert "amdStatusCallback" in body
        assert "/twilio/amd" in body
        assert "session_id=cs_abc" in body

    def test_voice_twiml_omits_amd_when_disabled(self, client):
        server.AMD_ENABLED     = False
        server.PUBLIC_BASE_URL = "https://example.test"
        r = client.post("/voice", data={"To": "+14045551234"})
        assert r.status_code == 200
        assert "machineDetection" not in r.data.decode("utf-8")

    def test_voice_twiml_omits_amd_when_no_public_base(self, client):
        server.AMD_ENABLED     = True
        server.PUBLIC_BASE_URL = ""  # no public base → AMD callback URL would be broken
        r = client.post("/voice", data={"To": "+14045551234"})
        assert r.status_code == 200
        assert "machineDetection" not in r.data.decode("utf-8")

    def test_amd_callback_records_event_human(self, client):
        server.VOICEMAIL_DROP_URL = ""
        r = client.post("/twilio/amd?session_id=cs_h", data={
            "AnsweredBy": "human",
            "CallSid": "CA" + "0" * 32,
        })
        assert r.status_code == 200
        events = list(server.IVR_EVENTS.get("cs_h", []))
        assert events and events[-1]["kind"] == "amd_result"
        assert events[-1]["answered_by"] == "human"
        assert events[-1]["level"] == "ok"
        assert events[-1]["voicemail_drop_fired"] is False

    def test_amd_callback_records_event_machine(self, client):
        server.VOICEMAIL_DROP_URL = ""  # no drop URL → just record verdict
        r = client.post("/twilio/amd?session_id=cs_m", data={
            "AnsweredBy": "machine_end_beep",
            "CallSid": "CA" + "1" * 32,
            "MachineBehavior": "answering_machine",
        })
        assert r.status_code == 200
        ev = list(server.IVR_EVENTS.get("cs_m", []))[-1]
        assert ev["kind"] == "amd_result"
        assert ev["answered_by"] == "machine_end_beep"
        assert ev["level"] == "warn"
        assert ev["voicemail_drop_fired"] is False

    def test_amd_callback_attempts_drop_when_url_configured(self, client, monkeypatch):
        """Verify the REST-API path is invoked for machine_* with drop URL set."""
        server.VOICEMAIL_DROP_URL = "https://example.test/pitch.mp3"
        os.environ["TWILIO_AUTH_TOKEN"] = "fake-token-for-test"

        # Patch the TwilioClient before its first import inside the handler.
        # We replace the entire twilio.rest module's Client to record the call.
        calls_captured = []

        class FakeCallContext:
            def __init__(self, sid):
                self.sid = sid

            def update(self, twiml=None, **kw):
                calls_captured.append({"sid": self.sid, "twiml": twiml, **kw})
                return {"sid": self.sid}

        class FakeCalls:
            def __call__(self, sid):
                return FakeCallContext(sid)

        class FakeClient:
            def __init__(self, *a, **kw):
                self.calls = FakeCalls()

        import twilio.rest
        monkeypatch.setattr(twilio.rest, "Client", FakeClient)

        try:
            r = client.post("/twilio/amd?session_id=cs_d", data={
                "AnsweredBy": "machine_end_beep",
                "CallSid": "CA" + "2" * 32,
            })
            assert r.status_code == 200
            assert len(calls_captured) == 1
            sent = calls_captured[0]
            assert sent["sid"] == "CA" + "2" * 32
            assert "<Play>" in sent["twiml"]
            assert "https://example.test/pitch.mp3" in sent["twiml"]
            assert "<Hangup/>" in sent["twiml"]
            ev = list(server.IVR_EVENTS.get("cs_d", []))[-1]
            assert ev["voicemail_drop_fired"] is True
        finally:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)

    def test_amd_callback_skips_drop_for_human(self, client, monkeypatch):
        """Even with drop URL configured, AnsweredBy=human must not fire the drop."""
        server.VOICEMAIL_DROP_URL = "https://example.test/pitch.mp3"
        os.environ["TWILIO_AUTH_TOKEN"] = "fake"
        calls_captured = []

        class FakeClient:
            def __init__(self, *a, **kw): pass
            def calls(self, sid):
                calls_captured.append(sid)
                raise AssertionError("Drop must not fire on human")

        import twilio.rest
        monkeypatch.setattr(twilio.rest, "Client", FakeClient)
        try:
            r = client.post("/twilio/amd?session_id=cs_x", data={
                "AnsweredBy": "human",
                "CallSid": "CA" + "3" * 32,
            })
            assert r.status_code == 200
            assert calls_captured == []
        finally:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)

    def test_amd_callback_records_drop_error_when_no_auth_token(self, client):
        """When TWILIO_AUTH_TOKEN is missing, drop fails gracefully."""
        server.VOICEMAIL_DROP_URL = "https://example.test/pitch.mp3"
        os.environ.pop("TWILIO_AUTH_TOKEN", None)
        r = client.post("/twilio/amd?session_id=cs_e", data={
            "AnsweredBy": "machine_end_beep",
            "CallSid": "CA" + "4" * 32,
        })
        assert r.status_code == 200
        ev = list(server.IVR_EVENTS.get("cs_e", []))[-1]
        assert ev["voicemail_drop_fired"] is False
        assert "TWILIO_AUTH_TOKEN" in ev["voicemail_drop_error"]


# ── Call recording ─────────────────────────────────────────────────────────

class TestCallRecording:
    def setup_method(self):
        self._prev_rec  = server.RECORDING_ENABLED
        self._prev_base = server.PUBLIC_BASE_URL
        self._prev_amd  = server.AMD_ENABLED
        server.AMD_ENABLED = False  # isolate recording test from AMD assertions
        server.IVR_EVENTS.clear()
        server.IVR_SEQ.clear()

    def teardown_method(self):
        server.RECORDING_ENABLED = self._prev_rec
        server.PUBLIC_BASE_URL   = self._prev_base
        server.AMD_ENABLED       = self._prev_amd

    def test_voice_twiml_includes_record_when_enabled(self, client):
        server.RECORDING_ENABLED = True
        server.PUBLIC_BASE_URL   = "https://example.test"
        r = client.post("/voice", data={"To": "+14045551234", "SessionId": "cs_rec"})
        assert r.status_code == 200
        body = r.data.decode("utf-8")
        assert "record-from-answer-dual" in body
        assert "recordingStatusCallback" in body
        assert "session_id=cs_rec" in body

    def test_voice_twiml_omits_record_when_disabled(self, client):
        server.RECORDING_ENABLED = False
        server.PUBLIC_BASE_URL   = "https://example.test"
        r = client.post("/voice", data={"To": "+14045551234"})
        body = r.data.decode("utf-8")
        assert "record-from-answer-dual" not in body
        assert "recordingStatusCallback" not in body

    def test_recording_callback_persists_to_call_session(self, client, cleanup_demo):
        # Set up a call_session row first
        sid = client.post("/api/call/start", json={"lead_id": "test-rec"}).get_json()["call_session_id"]
        r = client.post(f"/twilio/recording?session_id={sid}", data={
            "RecordingUrl": "https://api.twilio.com/.../RE123.mp3",
            "RecordingSid": "RE" + "0" * 32,
            "RecordingDuration": "42",
            "RecordingChannels": "2",
        })
        assert r.status_code == 200
        with server._db() as db:
            row = db.execute(
                "SELECT recording_url, recording_sid, recording_duration_s, recording_channels "
                "FROM call_sessions WHERE call_session_id=?",
                (sid,),
            ).fetchone()
        assert row is not None
        assert row["recording_url"].endswith("RE123.mp3")
        assert row["recording_sid"] == "RE" + "0" * 32
        assert row["recording_duration_s"] == 42
        assert row["recording_channels"] == 2

    def test_recording_callback_logs_ivr_event(self, client, cleanup_demo):
        sid = client.post("/api/call/start", json={"lead_id": "test-rec2"}).get_json()["call_session_id"]
        client.post(f"/twilio/recording?session_id={sid}", data={
            "RecordingUrl": "https://api.twilio.com/x.mp3",
            "RecordingSid": "RE" + "1" * 32,
            "RecordingDuration": "10",
        })
        events = list(server.IVR_EVENTS.get(sid, []))
        assert events and events[-1]["kind"] == "recording_complete"
        assert events[-1]["duration_s"] == 10


# ── Voicemail-drop <Say> TTS fallback (no MP3 needed) ──────────────────────

class TestVoicemailDropRoute:
    """The dedicated /voicemail-drop.mp3 route serves the recorded MP3 to
    Twilio when AnsweredBy=machine_*. Bypasses Flask static-folder serving
    which doesn't pick up newly-added files on the running dev process."""

    def test_route_returns_mp3_when_file_present(self, client, tmp_path):
        # Create a small fake mp3 in HERE to verify the route resolves
        target = server.HERE / "voicemail-drop.mp3"
        existed = target.exists()
        if not existed:
            target.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00fake-mp3-body")
        try:
            r = client.get("/voicemail-drop.mp3")
            assert r.status_code == 200
            assert r.mimetype == "audio/mpeg"
            assert len(r.data) > 0
        finally:
            if not existed:
                target.unlink(missing_ok=True)

    def test_route_404_when_file_missing(self, client):
        target = server.HERE / "voicemail-drop.mp3"
        if target.exists():
            backup = target.read_bytes()
            target.unlink()
            try:
                r = client.get("/voicemail-drop.mp3")
                assert r.status_code == 404
            finally:
                target.write_bytes(backup)
        else:
            r = client.get("/voicemail-drop.mp3")
            assert r.status_code == 404


class TestVoicemailDropSayFallback:
    def setup_method(self):
        self._prev_url  = server.VOICEMAIL_DROP_URL
        self._prev_text = server.VOICEMAIL_DROP_TEXT
        server.IVR_EVENTS.clear()
        server.IVR_SEQ.clear()

    def teardown_method(self):
        server.VOICEMAIL_DROP_URL  = self._prev_url
        server.VOICEMAIL_DROP_TEXT = self._prev_text

    def test_drop_uses_say_when_only_text_configured(self, client, monkeypatch):
        server.VOICEMAIL_DROP_URL  = ""
        server.VOICEMAIL_DROP_TEXT = "Hey, this is Tyler with Nova. Call me back."
        os.environ["TWILIO_AUTH_TOKEN"] = "fake-test"
        captured = []

        class FakeCtx:
            def __init__(self, sid):
                self.sid = sid
            def update(self, twiml=None, **kw):
                captured.append({"sid": self.sid, "twiml": twiml})

        class FakeClient:
            def __init__(self, *a, **k): pass
            def calls(self, sid): return FakeCtx(sid)

        import twilio.rest
        monkeypatch.setattr(twilio.rest, "Client", FakeClient)

        try:
            r = client.post("/twilio/amd?session_id=cs_say", data={
                "AnsweredBy": "machine_end_beep",
                "CallSid": "CA" + "5" * 32,
            })
            assert r.status_code == 200
            assert len(captured) == 1
            twiml = captured[0]["twiml"]
            assert "<Say" in twiml
            assert "Polly" in twiml
            assert "Tyler" in twiml
            assert "<Play>" not in twiml
            assert "<Hangup/>" in twiml
        finally:
            os.environ.pop("TWILIO_AUTH_TOKEN", None)


# ── Best-time-to-call analytics ────────────────────────────────────────────

class TestComputeBestHours:
    def test_empty_returns_no_recommendation(self):
        r = server.compute_best_hours([])
        assert r["best_hour"] is None
        assert r["worst_hour"] is None
        assert r["sample_size"] == 0
        assert len(r["buckets"]) == 24

    def test_single_call_below_min_samples(self):
        # Min samples = 2; one call should NOT trigger a best-hour pick
        rows = [{"code": "interested", "at": "2026-05-18T15:00:00"}]
        r = server.compute_best_hours(rows)
        assert r["sample_size"] == 1
        # 11am ET = 15:00 UTC - 4 = 11
        bucket_11 = next(b for b in r["buckets"] if b["hour"] == 11)
        assert bucket_11["answered"] == 1
        # No recommendation yet — only 1 sample, below threshold
        assert r["best_hour"] is None

    def test_picks_highest_pickup_rate_hour(self):
        # Bucket 11am: 2 answered, 0 missed → 1.0 pickup
        # Bucket 14pm: 1 answered, 2 missed → 0.33 pickup
        rows = [
            {"code": "interested",   "at": "2026-05-18T15:00:00"},  # 11am ET
            {"code": "callback",     "at": "2026-05-18T15:30:00"},  # 11am ET
            {"code": "interested",   "at": "2026-05-18T18:00:00"},  # 2pm ET
            {"code": "voicemail",    "at": "2026-05-18T18:30:00"},  # 2pm ET
            {"code": "no_answer",    "at": "2026-05-18T18:45:00"},  # 2pm ET
        ]
        r = server.compute_best_hours(rows)
        assert r["best_hour"] == 11
        assert r["worst_hour"] == 14
        assert r["sample_size"] == 5

    def test_ignores_unknown_codes(self):
        rows = [{"code": "garbage_code", "at": "2026-05-18T15:00:00"}]
        r = server.compute_best_hours(rows)
        assert r["sample_size"] == 0

    def test_ms_int_timestamp_works(self):
        # 2026-05-18 15:00:00 UTC = 11am ET. ms epoch:
        ts_ms = 1779800400000  # approximately
        rows = [
            {"code": "interested", "at": ts_ms},
            {"code": "interested", "at": ts_ms + 60000},
        ]
        r = server.compute_best_hours(rows)
        assert r["sample_size"] == 2
        assert r["best_hour"] is not None  # crosses threshold


class TestBestHoursEndpoint:
    def setup_method(self):
        with server._db() as db:
            db.execute("DELETE FROM dispositions WHERE lead_id LIKE 'bh-test-%'")
            db.commit()

    def teardown_method(self):
        with server._db() as db:
            db.execute("DELETE FROM dispositions WHERE lead_id LIKE 'bh-test-%'")
            db.commit()

    def test_global_endpoint_returns_buckets(self, client):
        r = client.get("/api/leads/best-hours")
        assert r.status_code == 200
        body = r.get_json()
        assert body["scope"] == "global"
        assert len(body["buckets"]) == 24

    def test_lead_endpoint_falls_back_to_global_when_no_data(self, client):
        r = client.get("/api/leads/bh-test-nonexistent/best-hours")
        assert r.status_code == 200
        body = r.get_json()
        assert body["scope"] == "global"  # no data for this lead → global

    def test_lead_endpoint_uses_lead_scope_with_enough_data(self, client):
        with server._db() as db:
            for code in ("interested", "callback", "voicemail"):
                db.execute(
                    "INSERT INTO dispositions(lead_id, phone, code, note, pass, at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ("bh-test-A", "+14045551234", code, "", 1, "2026-05-18 15:00:00"),
                )
            db.commit()
        r = client.get("/api/leads/bh-test-A/best-hours")
        assert r.status_code == 200
        body = r.get_json()
        assert body["scope"] == "lead"
        assert body["sample_size"] == 3


# ── /api/_debug/server-log access control ──────────────────────────────────

class TestDebugLogEndpoint:
    def test_localhost_allowed_in_dev(self, client):
        # Flask test_client sets remote_addr to 127.0.0.1
        server.DIALER_AUTH_TOKEN = ""
        r = client.get("/api/_debug/server-log")
        assert r.status_code == 200
        body = r.get_json()
        assert "stdout" in body and "stderr" in body
