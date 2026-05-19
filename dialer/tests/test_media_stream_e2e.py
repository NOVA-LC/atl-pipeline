"""End-to-end test of /media WebSocket → Deepgram → /ivr/events flow,
WITHOUT requiring Twilio to actually place a call.

Acts as a fake Twilio Media Streams client:
  1. Opens a WebSocket to wss://<host>/media
  2. Sends 'connected' and 'start' events with a session_id customParameter
  3. Streams a real mulaw audio fixture (the voicemail-drop recording at
     8 kHz mono mulaw) in 20ms chunks like Twilio does
  4. Sends 'stop' to flush
  5. Polls /ivr/events?session_id=... to see what transcripts/events
     the server logged

This lets us confirm:
  - flask-sock + gunicorn gthread actually accept WebSocket frames
  - The /media route reads frames correctly
  - The handler decodes base64 mulaw + forwards to Deepgram
  - Deepgram responds with transcripts
  - Transcripts surface as IVR events

Skipped automatically when running the test suite normally — only run
when DIALER_E2E_HOST is set to a target host (e.g. http://127.0.0.1:5050
or https://dialer-production-586c.up.railway.app).

Usage:
  # Local Flask
  DIALER_E2E_HOST=http://127.0.0.1:5050 pytest tests/test_media_stream_e2e.py -v -s

  # Railway production
  DIALER_E2E_HOST=https://dialer-production-586c.up.railway.app \
    pytest tests/test_media_stream_e2e.py -v -s
"""
import base64
import json
import os
import time
import uuid
from pathlib import Path

import pytest

try:
    import websocket  # websocket-client
except Exception:
    websocket = None

try:
    import requests
except Exception:
    requests = None


HOST = os.environ.get("DIALER_E2E_HOST", "").rstrip("/")
AUTH_TOKEN = os.environ.get("DIALER_AUTH_TOKEN", "")
FIXTURE = Path(__file__).parent / "_fixtures" / "voicemail-drop.ulaw"


pytestmark = pytest.mark.skipif(
    not HOST or not FIXTURE.exists() or websocket is None or requests is None,
    reason="set DIALER_E2E_HOST + ensure fixtures + websocket-client + requests installed",
)


def _ws_url(http_host: str) -> str:
    if http_host.startswith("https://"):
        return "wss://" + http_host[len("https://"):] + "/media"
    if http_host.startswith("http://"):
        return "ws://" + http_host[len("http://"):] + "/media"
    return http_host + "/media"


def _stream_mulaw_to_media(session_id: str, ulaw_bytes: bytes, max_seconds: float = 12.0):
    """Open /media WebSocket and send Twilio-format events."""
    ws = websocket.create_connection(_ws_url(HOST), timeout=15)
    try:
        # connected event
        ws.send(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        # start event with session_id custom parameter
        ws.send(json.dumps({
            "event": "start",
            "sequenceNumber": "1",
            "start": {
                "accountSid": "ACfake",
                "streamSid": "MZ" + uuid.uuid4().hex,
                "callSid": "CA" + uuid.uuid4().hex,
                "tracks": ["outbound"],
                "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                "customParameters": {"session_id": session_id},
            },
        }))

        # Stream audio in 160-byte chunks (= 20ms at 8 kHz mulaw, which is
        # what Twilio actually sends). Up to max_seconds of audio.
        chunk_size = 160
        chunks_per_sec = 50
        max_chunks = int(chunks_per_sec * max_seconds)
        seq = 2
        for i, offset in enumerate(range(0, len(ulaw_bytes), chunk_size)):
            if i >= max_chunks:
                break
            chunk = ulaw_bytes[offset:offset + chunk_size]
            if len(chunk) < chunk_size:
                break
            ws.send(json.dumps({
                "event": "media",
                "sequenceNumber": str(seq),
                "media": {
                    "track": "outbound",
                    "chunk": str(i + 1),
                    "timestamp": str(i * 20),
                    "payload": base64.b64encode(chunk).decode("ascii"),
                },
            }))
            seq += 1
            time.sleep(0.02)  # 20ms cadence — Twilio rate

        # stop event flushes
        ws.send(json.dumps({"event": "stop", "sequenceNumber": str(seq),
                            "stop": {"accountSid": "ACfake", "callSid": "CAfake"}}))
        time.sleep(2)  # let Deepgram finalize + handler write events
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _auth_headers():
    return {"X-Dialer-Token": AUTH_TOKEN} if AUTH_TOKEN else {}


def _poll_events(session_id: str, since: int = 0, max_wait_s: float = 6.0):
    """Poll /ivr/events until we either see a transcript event or time out."""
    url = f"{HOST}/ivr/events?session_id={session_id}&since={since}"
    end = time.time() + max_wait_s
    last = {"events": []}
    while time.time() < end:
        r = requests.get(url, headers=_auth_headers(), timeout=5)
        if r.status_code == 200:
            last = r.json()
            if any(e.get("kind") == "transcript" for e in last.get("events", [])):
                return last
        time.sleep(0.5)
    return last


def test_media_stream_produces_transcript_events():
    """The biggest one: real audio in → real transcript out."""
    session_id = f"e2e_{uuid.uuid4().hex[:8]}"
    ulaw = FIXTURE.read_bytes()
    print(f"\n[E2E] streaming {len(ulaw)} bytes ({len(ulaw)/8000:.1f}s) of mulaw to {_ws_url(HOST)} as session {session_id}")

    _stream_mulaw_to_media(session_id, ulaw, max_seconds=12)
    events = _poll_events(session_id, max_wait_s=8)

    kinds = [e.get("kind") for e in events.get("events", [])]
    print(f"[E2E] events received ({len(kinds)}): {kinds}")
    for e in events.get("events", []):
        if e.get("kind") == "transcript":
            print(f"[E2E] TRANSCRIPT: {e.get('transcript')}")

    assert "status" in kinds, "Expected 'status' event indicating stream connected"
    assert "transcript" in kinds, (
        f"Expected at least one transcript event but got: {kinds}. "
        "Means either /media didn't accept frames OR Deepgram bridge is broken."
    )


def test_media_stream_handles_empty_audio_gracefully():
    """Send start + stop with no media frames — handler should not crash."""
    session_id = f"e2e_empty_{uuid.uuid4().hex[:8]}"
    _stream_mulaw_to_media(session_id, b"", max_seconds=0)
    # No assertions — we just verify nothing throws / connection hangs.
