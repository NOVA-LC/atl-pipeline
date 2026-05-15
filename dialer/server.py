"""Brain-off dialer backend.

Endpoints:
  GET  /             → single-number Twilio test page
  GET  /single       → single-number Twilio test page
  GET  /queue        → dialer dashboard
  GET  /token        → Twilio Voice JWT access token
  POST /voice        → TwiML (Twilio calls this when browser initiates dial)
  GET  /leads        → JSON queue of leads to dial (deduped phones per lead)
  POST /disposition  → log a disposition for a lead+phone
  GET  /dispositions → list of all logged dispositions (current session)
  GET  /healthz      → health check

Lead source:
  - Mock data by default (10 hardcoded ATL local-business leads)
  - Default now uses DIALER_LEAD_SOURCE=dashboard to pull the live Vercel dashboard leads
  - Set DIALER_LEAD_SOURCE=railway to pull from atl-pipeline DB (requires railway login)

Run:
  python server.py
"""
import os
import json
import re
import sqlite3
import datetime
import sys
import threading
import traceback
import uuid
import base64
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import requests
from flask import Flask, request, send_from_directory, jsonify
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from twilio.twiml.voice_response import VoiceResponse, Dial
from dotenv import load_dotenv

try:
    from flask_sock import Sock
except Exception:
    Sock = None

try:
    import websocket
except Exception:
    websocket = None

ROOT_ENV = Path(__file__).resolve().parent.parent.parent / "claude code" / ".env"
load_dotenv(ROOT_ENV, override=True)

ACCOUNT_SID    = os.environ["TWILIO_ACCOUNT_SID"]
API_KEY_SID    = os.environ["TWILIO_API_KEY_SID"]
API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
FROM_NUMBER    = os.environ["TWILIO_FROM_NUMBER"]
TWIML_APP_SID  = os.environ.get("TWILIO_TWIML_APP_SID", "")

HERE     = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_DATA_ROOT = Path(os.environ.get("DIALER_DATA_DIR", str(HERE)))
_DATA_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH  = _DATA_ROOT / "dialer.db"
BUILD_DIR = _DATA_ROOT / "builds"
LEAD_SRC = os.environ.get("DIALER_LEAD_SOURCE", "call_dashboard")  # mock | call_dashboard | dashboard | railway
ROOT_PAGE = os.environ.get("DIALER_ROOT_PAGE", "queue").lower()  # single | queue
DIALER_CALL_DASHBOARD_URL = os.environ.get(
    "DIALER_CALL_DASHBOARD_URL",
    "https://tyler-call-dashboard.vercel.app/",
)
DASHBOARD_URL = os.environ.get(
    "DIALER_DASHBOARD_URL",
    "https://atlanta-demos.vercel.app/dashboard/",
)
IVR_COPILOT_MODE = os.environ.get("IVR_COPILOT_MODE", "suggest").lower()  # off | suggest | auto
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "")

# Agent mode: when "agent" or "auto", Haiku reasons about each transcript chunk and
# decides actions (press digit, alert Tyler, mark voicemail, etc). "regex" is the
# legacy pattern-matching path. Default to "agent" if Anthropic credentials present.
try:
    import agent as call_agent
except Exception as _ae:
    call_agent = None
IVR_AGENT_MODE = os.environ.get(
    "IVR_AGENT_MODE",
    "agent" if (call_agent is not None and os.environ.get("ANTHROPIC_API_KEY")) else "regex",
).lower()  # agent | regex | off


def _media_stream_url():
    explicit = os.environ.get("DIALER_MEDIA_STREAM_URL", "").strip()
    if explicit:
        return explicit
    public_base = os.environ.get("DIALER_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base:
        return ""
    if public_base.startswith("https://"):
        return "wss://" + public_base[len("https://"):] + "/media"
    if public_base.startswith("http://"):
        return "ws://" + public_base[len("http://"):] + "/media"
    return public_base + "/media"


MEDIA_STREAM_URL = _media_stream_url()

app = Flask(__name__, static_folder=str(HERE), static_url_path="")
sock = Sock(app) if Sock else None
BUILD_DIR.mkdir(exist_ok=True)
BUILD_JOBS = {}
BUILD_LOCK = threading.Lock()
IVR_LOCK = threading.Lock()
IVR_EVENTS = {}
IVR_SEQ = {}


# ─── local SQLite for dispositions ────────────────────────────────────────────
def _db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE IF NOT EXISTS dispositions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lead_id TEXT NOT NULL,
        phone TEXT NOT NULL,
        code TEXT NOT NULL,
        note TEXT,
        at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        pass INTEGER DEFAULT 1
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS lead_notes (
        lead_id TEXT PRIMARY KEY,
        phone TEXT,
        email TEXT,
        callback_at TEXT,
        note TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Per-call transcripts captured from Deepgram. One row per call session.
    c.execute("""CREATE TABLE IF NOT EXISTS call_transcripts (
        session_id     TEXT PRIMARY KEY,
        lead_id        TEXT,
        business_name  TEXT,
        phone          TEXT,
        transcript     TEXT NOT NULL DEFAULT '',
        chunk_count    INTEGER DEFAULT 0,
        started_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at       TIMESTAMP
    )""")
    # AI summary generated post-call from the transcript.
    c.execute("""CREATE TABLE IF NOT EXISTS call_summaries (
        session_id          TEXT PRIMARY KEY,
        lead_id             TEXT,
        outcome             TEXT,
        sentiment           TEXT,
        key_objections      TEXT,    -- JSON list
        follow_up_actions   TEXT,    -- JSON list
        key_quotes          TEXT,    -- JSON list
        suggested_disposition TEXT,
        confidence          REAL,
        model               TEXT,
        cost_usd            REAL,
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # Cached pre-call AI briefings — 3-bullet intel per lead, generated once
    c.execute("""CREATE TABLE IF NOT EXISTS briefings (
        lead_id       TEXT PRIMARY KEY,
        bullets       TEXT NOT NULL,   -- JSON list
        model         TEXT,
        cost_usd      REAL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    return c


# ─── phone helpers ────────────────────────────────────────────────────────────
def normalize_phone(raw):
    """Return E.164 if valid US number, else empty string."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    if digits[0] in "01":
        return ""
    if digits[:3] in {"000", "111", "555", "943"}:
        return ""
    return "+1" + digits


def dedupe_phones(phones):
    """Take list of raw phones, return list of unique E.164 phones in order."""
    seen = set()
    out = []
    for p in phones or []:
        e = normalize_phone(p)
        if e and e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _slugify(value):
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return (slug or "lead")[:60]


def _now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _job_update(job_id, **fields):
    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if not job:
            return None
        job.update(fields)
        job["updated_at"] = _now_iso()
        return dict(job)


def _lead_for_build(raw):
    """Normalize a dialer lead into the shape atl_pipeline.generate expects."""
    raw = raw or {}
    business_name = (raw.get("business_name") or raw.get("name") or "Untitled Lead").strip()
    phones = raw.get("phones") or raw.get("phones_raw") or [raw.get("phone", "")]
    phone = normalize_phone(phones[0] if isinstance(phones, list) and phones else phones)
    return {
        "id": str(raw.get("id") or _slugify(business_name)),
        "slug": _slugify(raw.get("slug") or business_name),
        "business_name": business_name,
        "owner_name": raw.get("owner_name") or "",
        "category": raw.get("category") or "Local Service",
        "city": raw.get("city") or "Atlanta",
        "state": raw.get("state") or "GA",
        "phone": phone,
        "phones": dedupe_phones(phones if isinstance(phones, list) else [phones]),
        "address": raw.get("address") or "",
        "rating": raw.get("rating"),
        "reviews": raw.get("reviews") or 0,
        "google_maps_url": raw.get("google_maps_url") or raw.get("gmaps") or "",
        "talking_points": raw.get("talking_points") or [],
        "source_url": raw.get("source_url") or DIALER_CALL_DASHBOARD_URL,
    }


def _fallback_research(lead):
    points = [p for p in (lead.get("talking_points") or []) if p]
    specialties = []
    for point in points:
        cleaned = re.sub(r"^(open|pitch|if interested|carrier|ask for):\s*", "", point, flags=re.I)
        cleaned = cleaned.split(".")[0].strip(" -:")
        if cleaned and len(cleaned) <= 60:
            specialties.append(cleaned)
    if not specialties:
        specialties = [lead.get("category") or "local service", "same-day help", "clear communication"]
    return {
        "owner_name": lead.get("owner_name") or "unknown",
        "brand_colors": [],
        "tagline_options": [
            f"{lead['business_name']} for {lead.get('city') or 'Atlanta'} homeowners.",
            "Clear work. Fast response. No weird surprises.",
        ],
        "vibe": "Built from the call sheet while the research agent gathers more context.",
        "specialties": specialties[:6],
        "wow_facts": points[:3],
        "real_reviews": [],
    }


def _run_build_job(job_id, lead):
    """Background website build: research first, then render a local preview."""
    try:
        _job_update(job_id, status="researching", message="Deep research running. This can take a few minutes.")
        research_payload = None
        if os.environ.get("DIALER_BUILD_SKIP_RESEARCH") == "1":
            research_payload = {"_error": "DIALER_BUILD_SKIP_RESEARCH=1"}
        else:
            try:
                from atl_pipeline.research import research_lead
                research_payload = research_lead(lead)
            except Exception as e:
                research_payload = {"_error": str(e)}

        if not research_payload or research_payload.get("_error") or research_payload.get("_parse_error"):
            msg = research_payload.get("_error") if isinstance(research_payload, dict) else ""
            _job_update(job_id, message=f"Research fallback active. {msg}".strip())
            research_payload = _fallback_research(lead)

        _job_update(job_id, status="rendering", message="Rendering website preview.")
        from atl_pipeline.generate import render_demo

        html = render_demo(lead, research_payload)
        out_dir = BUILD_DIR / lead["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        out_path.write_text(html, encoding="utf-8")

        url = f"/builds/{lead['slug']}/index.html"
        _job_update(
            job_id,
            status="done",
            message="Website preview ready.",
            url=url,
            output_path=str(out_path),
            finished_at=_now_iso(),
        )
    except Exception as e:
        _job_update(
            job_id,
            status="error",
            message=str(e),
            error=traceback.format_exc(limit=8),
            finished_at=_now_iso(),
        )


# ─── lead sources ─────────────────────────────────────────────────────────────
def _add_ivr_event(session_id, **event):
    if not session_id:
        return
    with IVR_LOCK:
        seq = IVR_SEQ.get(session_id, 0) + 1
        IVR_SEQ[session_id] = seq
        event.setdefault("level", "")
        event.setdefault("at", _now_iso())
        event["seq"] = seq
        IVR_EVENTS.setdefault(session_id, []).append(event)
        IVR_EVENTS[session_id] = IVR_EVENTS[session_id][-100:]


def _score_ivr_window(window):
    text = window.lower()
    score = 0
    if "all other" in text or "other inquir" in text or "general" in text:
        score += 100
    if "new customer" in text or "new client" in text or "sales" in text or "estimate" in text or "quote" in text:
        score += 85
    if "representative" in text or "operator" in text or "speak" in text or "someone" in text:
        score += 75
    if "service" in text or "support" in text:
        score += 45
    if "emergency" in text or "immediate" in text or "urgent" in text:
        score -= 40
    if "billing" in text or "accounting" in text or "payment" in text:
        score -= 20
    return score


def detect_ivr_digit(transcript):
    """Return the best IVR digit from a transcript chunk, or None."""
    if not transcript:
        return None
    text = transcript.lower()
    number_words = {
        "zero": "0", "oh": "0", "one": "1", "two": "2", "too": "2",
        "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
        "eight": "8", "nine": "9", "star": "*", "pound": "#", "hash": "#",
    }
    for word, digit in number_words.items():
        text = re.sub(rf"\b{word}\b", digit, text)
    matches = []
    verbs = r"(?:press|dial|select|choose|hit|push|enter)"
    digit_pat = r"([0-9*#])"
    for m in re.finditer(rf"(?:for|if|to)\s+([^.;,]{{0,90}}?){verbs}\s+{digit_pat}", text):
        window = m.group(1).strip()
        matches.append({"digit": m.group(2), "window": window, "score": _score_ivr_window(window)})
    for m in re.finditer(rf"{verbs}\s+{digit_pat}\s+(?:for|if|to)\s+([^.;,]{{0,90}})", text):
        window = m.group(2).strip()
        matches.append({"digit": m.group(1), "window": window, "score": _score_ivr_window(window)})
    if not matches:
        for m in re.finditer(rf"{verbs}\s+{digit_pat}", text):
            window = text[max(0, m.start() - 50):m.end() + 50]
            matches.append({"digit": m.group(1), "window": window, "score": _score_ivr_window(window)})
    if not matches:
        return None
    best = sorted(matches, key=lambda m: m["score"], reverse=True)[0]
    if best["score"] < 20 and len(matches) > 1:
        return None
    return best


def _persist_transcript_chunk(session_id, chunk):
    """Append a Deepgram transcript chunk to call_transcripts. Best-effort."""
    if not session_id or not chunk:
        return
    try:
        active = (call_agent.get_active_lead() if call_agent else {}) or {}
        with _db() as c:
            row = c.execute("SELECT transcript, chunk_count FROM call_transcripts WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO call_transcripts (session_id, lead_id, business_name, phone, transcript, chunk_count) "
                    "VALUES (?, ?, ?, ?, ?, 1)",
                    (session_id, active.get("lead_id"), active.get("business_name"), active.get("phone"), chunk),
                )
            else:
                existing = (row["transcript"] or "").rstrip()
                joined = (existing + " " + chunk).strip()
                c.execute(
                    "UPDATE call_transcripts SET transcript = ?, chunk_count = chunk_count + 1 WHERE session_id = ?",
                    (joined, session_id),
                )
            c.commit()
    except Exception:
        pass


def _finalize_transcript(session_id):
    """Mark the transcript as ended_at NOW + trigger async post-call summary."""
    if not session_id:
        return
    try:
        with _db() as c:
            c.execute("UPDATE call_transcripts SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ? AND ended_at IS NULL", (session_id,))
            c.commit()
    except Exception:
        return
    threading.Thread(target=_generate_call_summary, args=(session_id,), daemon=True).start()


def _generate_call_summary(session_id):
    """Read the transcript, ask Haiku for a structured post-call summary.
    Persists to call_summaries. Idempotent — skips if a row already exists."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return
    try:
        with _db() as c:
            t = c.execute("SELECT transcript, lead_id, business_name FROM call_transcripts WHERE session_id = ?", (session_id,)).fetchone()
            existing = c.execute("SELECT 1 FROM call_summaries WHERE session_id = ?", (session_id,)).fetchone()
        if not t or existing:
            return
        transcript = (t["transcript"] or "").strip()
        if len(transcript) < 40:
            return  # too short to summarize
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = os.environ.get("CALL_SUMMARY_MODEL", "claude-haiku-4-5-20251001")
        system = (
            "You analyze cold-call phone transcripts for Tyler Brown's website-build cold outreach.\n"
            "Tyler is on the line; the other speaker is a small business owner or gatekeeper.\n"
            "Return STRICT JSON only — no prose, no fences."
        )
        user = (
            "Transcript (verbatim Deepgram, may have ASR errors):\n"
            f"```\n{transcript[:6000]}\n```\n\n"
            "Business name (if known): " + (t["business_name"] or "unknown") + "\n\n"
            "Return JSON with these keys exactly:\n"
            "  outcome           : one of [booked, interested, callback, not_interested, dnc, voicemail, gatekeeper, no_decision]\n"
            "  sentiment         : one of [positive, neutral, negative]\n"
            "  key_objections    : list of strings, verbatim or close paraphrase\n"
            "  follow_up_actions : list of imperative bullets ('text demo link Friday', 'email Joe at joe@...')\n"
            "  key_quotes        : list of strings — the 1-3 most useful verbatim quotes\n"
            "  suggested_disposition : one of [no_answer, voicemail, callback, not_interested, interested, dnc, skip]\n"
            "  confidence        : float 0-1"
        )
        resp = client.messages.create(model=model, max_tokens=600, system=system,
                                       messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        # strip fences if present
        if text.startswith("```"):
            text = re.sub(r"^```\w*\s*", "", text).rsplit("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except Exception:
            return
        usage = resp.usage
        cost = round(getattr(usage, "input_tokens", 0) * 1.0 / 1_000_000 + getattr(usage, "output_tokens", 0) * 5.0 / 1_000_000, 4)
        with _db() as c:
            c.execute(
                "INSERT OR REPLACE INTO call_summaries (session_id, lead_id, outcome, sentiment, key_objections, follow_up_actions, key_quotes, suggested_disposition, confidence, model, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, t["lead_id"], data.get("outcome"), data.get("sentiment"),
                 json.dumps(data.get("key_objections") or []),
                 json.dumps(data.get("follow_up_actions") or []),
                 json.dumps(data.get("key_quotes") or []),
                 data.get("suggested_disposition"),
                 float(data.get("confidence") or 0),
                 model, cost),
            )
            c.commit()
    except Exception:
        pass


def _handle_ivr_transcript(session_id, transcript, seen):
    cleaned = re.sub(r"\s+", " ", transcript or "").strip()
    if not cleaned:
        return
    key = cleaned.lower()
    if key in seen:
        return
    seen.add(key)
    _add_ivr_event(session_id, kind="transcript", message=f"Heard: {cleaned}", transcript=cleaned)
    _persist_transcript_chunk(session_id, cleaned)

    # Agent path — Haiku reasons about the call. Falls through to regex on error.
    if IVR_AGENT_MODE in ("agent", "auto") and call_agent and call_agent.is_available():
        try:
            decision = call_agent.think(session_id, cleaned)
        except Exception as e:
            decision = {"action": "error", "reason": f"agent crashed: {e}"}
        if decision and decision.get("action") not in (None, "error"):
            _dispatch_agent_action(session_id, decision, cleaned)
            return
        elif decision and decision.get("action") == "error":
            _add_ivr_event(
                session_id,
                kind="agent_error",
                level="warn",
                message=f"Agent error, falling back to regex: {decision.get('reason')}",
            )
        # else: agent rate-limited or returned nothing — fall through

    # Regex fallback (legacy behavior, also used when IVR_AGENT_MODE=regex)
    choice = detect_ivr_digit(cleaned)
    if not choice:
        return
    digit = choice["digit"]
    choice_key = f"{digit}:{choice['window'][:80]}"
    if choice_key in seen:
        return
    seen.add(choice_key)
    mode = "auto" if IVR_COPILOT_MODE == "auto" else "suggest"
    _add_ivr_event(
        session_id,
        kind="ivr_digit",
        digit=digit,
        auto_press=(mode == "auto"),
        mode=mode,
        transcript=cleaned,
        reason=choice["window"],
        level="ok" if mode == "auto" else "warn",
        message=f"IVR Copilot {'auto-pressing' if mode == 'auto' else 'suggests pressing'} {digit} (regex): \"{cleaned}\"",
    )


def _dispatch_agent_action(session_id, decision, transcript):
    """Translate an agent decision into an IVR event the frontend will act on."""
    action = decision.get("action")
    arg    = decision.get("arg") or ""
    reason = decision.get("reason") or ""

    if action == "press_digit" and arg in tuple("0123456789*#"):
        # Treat agent decisions as auto-press by default (it has higher confidence than regex).
        auto = (IVR_AGENT_MODE == "auto") or (IVR_COPILOT_MODE == "auto")
        _add_ivr_event(
            session_id,
            kind="ivr_digit",
            digit=arg,
            auto_press=auto,
            mode="auto" if auto else "suggest",
            transcript=transcript,
            reason=reason,
            level="ok",
            message=f"Agent {'auto-pressing' if auto else 'suggests pressing'} {arg}: {reason}",
        )
    elif action == "wait":
        # No-op event for the agent log so Tyler can see it's thinking.
        _add_ivr_event(
            session_id,
            kind="agent_wait",
            transcript=transcript,
            reason=reason,
            message=f"Agent: waiting — {reason}",
        )
    elif action == "alert_tyler":
        _add_ivr_event(
            session_id,
            kind="alert",
            level="alert",
            transcript=transcript,
            reason=reason,
            message=arg or reason or "Agent alert",
        )
    elif action == "mark_voicemail":
        _add_ivr_event(
            session_id,
            kind="mark_voicemail",
            level="warn",
            transcript=transcript,
            reason=reason,
            message=f"Agent detected voicemail: {reason}",
        )
    elif action == "note":
        _add_ivr_event(
            session_id,
            kind="agent_note",
            transcript=transcript,
            reason=reason,
            message=f"Agent note: {arg}",
        )
    else:
        _add_ivr_event(
            session_id,
            kind="agent_error",
            level="warn",
            message=f"Agent returned unknown action: {decision}",
        )


def _deepgram_url():
    return (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3&encoding=mulaw&sample_rate=8000&channels=1"
        "&interim_results=false&smart_format=true&endpointing=400"
    )


def _bridge_twilio_to_deepgram(ws):
    """Receive Twilio Media Streams frames and feed Deepgram live STT."""
    if not DEEPGRAM_API_KEY or websocket is None:
        ws.close()
        return

    session_id = ""
    seen = set()
    dg = None
    stop = threading.Event()

    def read_deepgram():
        while not stop.is_set() and dg is not None:
            try:
                msg = dg.recv()
                if not msg:
                    continue
                data = json.loads(msg)
                alt = (data.get("channel", {}).get("alternatives") or [{}])[0]
                transcript = (alt.get("transcript") or "").strip()
                if transcript:
                    _handle_ivr_transcript(session_id, transcript, seen)
            except Exception as e:
                if session_id:
                    _add_ivr_event(session_id, kind="error", level="err", message=f"Deepgram stream ended: {e}")
                break

    try:
        dg = websocket.create_connection(
            _deepgram_url(),
            header=[f"Authorization: Token {DEEPGRAM_API_KEY}"],
            timeout=10,
        )
        threading.Thread(target=read_deepgram, daemon=True).start()
        while True:
            raw = ws.receive()
            if raw is None:
                break
            msg = json.loads(raw)
            event = msg.get("event")
            if event == "start":
                params = msg.get("start", {}).get("customParameters") or {}
                session_id = params.get("session_id") or params.get("SessionId") or ""
                _add_ivr_event(session_id, kind="status", level="ok", message="IVR Copilot audio stream connected.")
            elif event == "media" and dg is not None:
                payload = msg.get("media", {}).get("payload")
                if payload:
                    dg.send_binary(base64.b64decode(payload))
            elif event == "stop":
                _add_ivr_event(session_id, kind="status", message="IVR Copilot audio stream stopped.")
                _finalize_transcript(session_id)
                break
    except Exception as e:
        _add_ivr_event(session_id, kind="error", level="err", message=f"IVR Copilot failed: {e}")
    finally:
        stop.set()
        if dg is not None:
            try:
                dg.close()
            except Exception:
                pass
        if call_agent and session_id:
            try:
                call_agent.reset_session(session_id)
            except Exception:
                pass


MOCK_LEADS = [
    {
        "id": "mock-1",
        "business_name": "Peach State Plumbing",
        "owner_name": "Joey",
        "category": "Plumber",
        "city": "Marietta", "state": "GA",
        "phones_raw": ["(770) 555-0101", "770-555-0101", "+17705550102"],  # dup + extra
        "vercel_url": "https://atlanta-demos.vercel.app/peach-state-plumbing",
        "rating": 4.8, "reviews": 127,
        "talking_points": ["Owner: Joey · 31 yrs in Marietta", "Slab leak specialty",
                          "Flat-rate pricing — no surprise quotes"],
    },
    {
        "id": "mock-2",
        "business_name": "Messiah Heating & Air",
        "owner_name": "Ben",
        "category": "HVAC Contractor",
        "city": "Decatur", "state": "GA",
        "phones_raw": ["(404) 555-0202"],
        "vercel_url": "https://atlanta-demos.vercel.app/messiah-heating",
        "rating": 4.6, "reviews": 45,
        "talking_points": ["Owner answers his own phone since 2008", "Quoted-price guarantee",
                          "Open Sunday — Decatur HVAC"],
    },
    {
        "id": "mock-3",
        "business_name": "Atlanta Lawn Pros",
        "owner_name": "Marcus",
        "category": "Landscaper",
        "city": "Atlanta", "state": "GA",
        "phones_raw": ["404-555-0303", "404.555.0303"],
        "vercel_url": "https://atlanta-demos.vercel.app/atlanta-lawn-pros",
        "rating": 4.9, "reviews": 89,
        "talking_points": ["Weekly + bi-weekly route in Buckhead", "5★ for 4 yrs running"],
    },
    {
        "id": "mock-4",
        "business_name": "Druid Hills Auto Repair",
        "owner_name": "Hassan",
        "category": "Auto Mechanic",
        "city": "Druid Hills", "state": "GA",
        "phones_raw": ["+14045550404"],
        "vercel_url": "https://atlanta-demos.vercel.app/druid-hills-auto",
        "rating": 4.7, "reviews": 203,
        "talking_points": ["Family-owned · 22 yrs", "AAA-approved · honest diagnostic"],
    },
    {
        "id": "mock-5",
        "business_name": "East Lake Roofing",
        "owner_name": "Travis",
        "category": "Roofer",
        "city": "East Lake", "state": "GA",
        "phones_raw": ["(404) 555-0505"],
        "vercel_url": "https://atlanta-demos.vercel.app/east-lake-roofing",
        "rating": 4.5, "reviews": 67,
        "talking_points": ["Storm-damage specialist", "Insurance-claim help"],
    },
    {
        "id": "mock-6",
        "business_name": "Kirkwood Cleaners",
        "owner_name": "Linh",
        "category": "Dry Cleaner",
        "city": "Kirkwood", "state": "GA",
        "phones_raw": ["404-555-0606"],
        "vercel_url": "https://atlanta-demos.vercel.app/kirkwood-cleaners",
        "rating": 4.4, "reviews": 38,
        "talking_points": ["Pickup + delivery within 3mi", "Open 7 days"],
    },
    {
        "id": "mock-7",
        "business_name": "Tucker Tree Service",
        "owner_name": "Devon",
        "category": "Tree Service",
        "city": "Tucker", "state": "GA",
        "phones_raw": ["770-555-0707", "(770) 555-0707", "+17705550708"],
        "vercel_url": "https://atlanta-demos.vercel.app/tucker-tree",
        "rating": 4.9, "reviews": 156,
        "talking_points": ["Emergency 24/7 storm response", "Insured · bonded"],
    },
    {
        "id": "mock-8",
        "business_name": "Oakhurst Pest Control",
        "owner_name": "Renée",
        "category": "Pest Control",
        "city": "Oakhurst", "state": "GA",
        "phones_raw": ["404-555-0808"],
        "vercel_url": "https://atlanta-demos.vercel.app/oakhurst-pest",
        "rating": 4.6, "reviews": 92,
        "talking_points": ["Quarterly service plans", "Pet-safe treatments"],
    },
    {
        "id": "mock-9",
        "business_name": "Avondale Estate Painting",
        "owner_name": "Phillip",
        "category": "Painter",
        "city": "Avondale Estates", "state": "GA",
        "phones_raw": ["+14045550909"],
        "vercel_url": "https://atlanta-demos.vercel.app/avondale-painting",
        "rating": 4.8, "reviews": 71,
        "talking_points": ["Historic-home specialist", "Color consult included"],
    },
    {
        "id": "mock-10",
        "business_name": "Marietta Mobile Mechanic",
        "owner_name": "Carlos",
        "category": "Mobile Mechanic",
        "city": "Marietta", "state": "GA",
        "phones_raw": ["(770) 555-1010"],
        "vercel_url": "https://atlanta-demos.vercel.app/marietta-mobile-mechanic",
        "rating": 4.7, "reviews": 134,
        "talking_points": ["Comes to your driveway", "Diagnostics on-site"],
    },
]


def _load_dashboard_leads(url=DASHBOARD_URL):
    """Read the generated public dashboard and map its embedded JSON to dialer rows."""
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    marker = "const LEADS = "
    start = resp.text.find(marker)
    if start < 0:
        raise RuntimeError(f"Could not find embedded LEADS JSON in {url}")

    raw = resp.text[start + len(marker):]
    leads, _ = json.JSONDecoder().raw_decode(raw)
    out = []
    for row in leads:
        if not row.get("phone_valid"):
            continue
        phone = normalize_phone(row.get("phone_e164") or row.get("phone_display"))
        if not phone:
            continue

        points = [p for p in (row.get("talking_points") or []) if p and p != "unknown"]
        if row.get("sms_body"):
            points.append("SMS opener is already written in the dashboard.")

        out.append({
            "id": str(row.get("id") or row.get("business") or phone),
            "business_name": row.get("business") or "Unknown business",
            "owner_name": (row.get("owner_first") or "").strip(),
            "category": row.get("category") or "",
            "city": row.get("city") or "",
            "state": row.get("state") or "GA",
            "phones_raw": [phone],
            "vercel_url": row.get("demo") or "",
            "rating": row.get("rating"),
            "reviews": row.get("reviews") or 0,
            "talking_points": points,
        })
    return out


def _strip_html(value):
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _first(pattern, value, default=""):
    m = re.search(pattern, value or "", re.S | re.I)
    return m.group(1).strip() if m else default


def _script_context(category):
    cat = (category or "").lower()
    if any(x in cat for x in ["straw", "mulch", "landscap", "garden", "lawn", "tree"]):
        return {
            "buyer": "homeowners, property managers, and landscapers",
            "job": "pine straw, mulch, delivery, and install jobs",
            "pain": "customers are making a fast trust decision before they call or order",
            "proof": "materials, delivery radius, pricing cues, photos, reviews, and a simple quote/order path",
        }
    if any(x in cat for x in ["plumb", "septic", "drain", "water"]):
        return {
            "buyer": "homeowners with urgent repair problems",
            "job": "leak, drain, water heater, and emergency calls",
            "pain": "urgent customers often call the first company that looks credible enough at that moment",
            "proof": "service areas, emergency availability, reviews, pricing cues, and a tap-to-call layout",
        }
    if any(x in cat for x in ["hvac", "heating", "air", "cool"]):
        return {
            "buyer": "homeowners with comfort problems",
            "job": "repair, maintenance, and replacement calls",
            "pain": "customers compare trust signals before they ever talk to a contractor",
            "proof": "repair/replacement pages, financing cues, reviews, service area, and owner credibility",
        }
    if any(x in cat for x in ["roof", "gutter"]):
        return {
            "buyer": "homeowners with leak or storm concerns",
            "job": "roof repair, replacement, gutter, and inspection calls",
            "pain": "people researching expensive exterior work need proof before they trust the first call",
            "proof": "before/after photos, warranty language, reviews, insurance help, and service area pages",
        }
    if any(x in cat for x in ["auto", "mechanic", "tire", "collision"]):
        return {
            "buyer": "drivers who need a fast, trustworthy answer",
            "job": "repair, diagnostic, tire, and emergency service calls",
            "pain": "customers compare shops fast and skip the ones that do not answer basic questions online",
            "proof": "services, hours, review proof, location, repair categories, and click-to-call",
        }
    return {
            "buyer": "local customers",
            "job": "new customer calls",
            "pain": "people compare a few options quickly and usually contact the one that feels easiest to trust",
        "proof": "reviews, photos, services, service area, and a simple way to ask for a quote",
    }


def _ei_talking_points(lead, source_points=None):
    """Build a cold-call script using Tyler's Emotional Intelligence methodology.

    Structure: Permission → Grounding (their current reality, honestly) →
    Elevation (their lived desired reality) → Resolution (the Peace Bridge).
    The salesperson is the Peace Architect — not a closer.

    Reference: docs/EMOTIONAL_INTELLIGENCE_METHODOLOGY.md
    """
    ctx = _script_context(lead.get("category"))
    name = lead.get("business_name") or "your business"
    owner = (lead.get("owner_name") or "").strip()
    first_name = owner.split()[0] if owner else ""
    city = lead.get("city") or "your area"
    category = (lead.get("category") or "").strip()
    rating = lead.get("rating")
    reviews = lead.get("reviews")

    proof_bits = []
    if rating and reviews:
        proof_bits.append(f"{rating}★ on Google · {reviews} reviews")
    elif rating:
        proof_bits.append(f"{rating}★ on Google")
    if city:
        proof_bits.append(city)
    if category:
        proof_bits.append(category)
    proof = " · ".join(proof_bits)

    # Filter and keep only signal-bearing source points (no labels, no boilerplate)
    extras = []
    for p in source_points or []:
        if not p:
            continue
        if re.match(r"(ask|carrier|open|pitch|if interested|sms follow-up|nepq)\b", p, re.I):
            continue
        if re.search(r"plumbers? without a site|nighttime calls|build websites", p, re.I):
            continue
        if p not in extras:
            extras.append(p)

    you = f"{first_name}," if first_name else ""
    biz_short = first_name or name

    points = [
        # — Permission (not a pitch)
        (f"Permission: Hey {you} this is Tyler with Nova here in Atlanta — I know this is out "
         f"of the blue. Before I take any more of your time, are you the right person to talk "
         f"to about how new customers find {name}?"),

        # — Research cue the rep can glance at
        f"Lead context: {proof or city}",

        # — GROUNDING (their current reality, honestly)
        (f"Grounding · Depth: Where are most of your new {ctx['job']} actually coming from "
         f"right now? Repeat customers, referrals, Google, or somewhere else?"),
        (f"Grounding · Depth (layer 2): When someone in {city} Googles a {category or 'local pro'} "
         f"at 9 at night with a problem they need fixed — walk me through what they see for {biz_short}."),
        (f"Grounding · Mirror: [Reflect their exact words back, then pause.] \"So you said "
         f"{{their words}} — tell me more about that.\""),

        # — ELEVATION (their lived future, not corporate goals)
        (f"Elevation · Lived future: If every new lead this week could find you in five "
         f"seconds and saw your reviews, photos, and a way to call instantly — walk me through "
         f"what a normal Tuesday morning looks like."),
        (f"Elevation · Absence test: If the online trust piece just vanished — if your site "
         f"looked as good as your actual work — what's the first thing that's different about "
         f"your week?"),

        # — RESOLUTION (the Peace Bridge — not a close)
        (f"Resolution · Alignment summary: You told me {{their current reality, their words}}. "
         f"You want {{their desired reality, their words}}. The reason I called: I already "
         f"built a preview of what your site could look like — pulled from your public Google "
         f"info. Want to see it?"),
        (f"Resolution · Peace check: How does that feel?  ← If peace, ask for email + send. "
         f"If tension, return to Grounding — there's more honest inventory to do."),
        (f"Resolution · Pathway: I'll text the link right now to this number. Take a look "
         f"while we're on. If it feels right, we go from there. If it doesn't, I appreciate "
         f"your time and I move on."),

        # — Recovery line if they're rushed (still EI: peace, not pressure)
        (f"If they're rushed: Totally fair. Before I let you go — is getting more new "
         f"customer calls from people Googling at night even something you'd want right now? "
         f"If yes, when's a quieter five minutes? If no, that's a real answer and I respect it."),
    ]

    if extras:
        points.insert(2, "Public proof: " + " · ".join(extras[:3]))
    return points


# Back-compat alias — preserves the existing call site
_nepq_talking_points = _ei_talking_points


def _load_call_dashboard_leads(url=DIALER_CALL_DASHBOARD_URL):
    """Read Tyler's public call-sheet dashboard and map cards into dialer rows."""
    resp = requests.get(url, timeout=20)
    resp.raise_for_status()
    cards = re.findall(r'<details class="card\b.*?</details>', resp.text, re.S | re.I)
    out = []

    for idx, card in enumerate(cards, start=1):
        tel = _first(r'href="tel:([^"]+)"', card)
        phone = normalize_phone(tel or _first(r'data-phone="([^"]+)"', card))
        if not phone:
            continue

        biz = _strip_html(_first(r'<div class="biz">(.*?)</div>', card)) or phone
        category = unescape(_first(r'data-cat="([^"]+)"', card))
        city = unescape(_first(r'data-city="([^"]+)"', card))
        owner = _strip_html(_first(r'<div class="ask">Ask for <strong>(.*?)</strong>', card))
        ask = _strip_html(_first(r'<div class="ask[^"]*">(.*?)</div>', card))
        sig = _strip_html(_first(r'<div class="sig">(.*?)</div>', card))
        carrier = _strip_html(_first(r'<span class="carrier[^"]*">(.*?)</span>', card))

        rating = None
        reviews = 0
        rating_text = _strip_html(_first(r'<span class="rating">(.*?)</span>', card))
        m = re.search(r"([0-9](?:\.[0-9])?)\s*[^\d]+\s*([0-9,]+)", rating_text)
        if m:
            rating = float(m.group(1))
            reviews = int(m.group(2).replace(",", ""))

        script_points = []
        for label, body in re.findall(
            r'<div class="sc-step">\s*<div class="sc-label">(.*?)</div>\s*<p>(.*?)</p>',
            card,
            re.S | re.I,
        ):
            label_text = _strip_html(label)
            body_text = _strip_html(body)
            if body_text:
                script_points.append(f"{label_text}: {body_text}" if label_text else body_text)

        sms_href = _first(r'href="(sms:[^"]*)"', card)
        sms_body = ""
        if sms_href:
            parsed = urlparse(unescape(sms_href))
            sms_body = parse_qs(parsed.query).get("body", [""])[0]

        source_points = []
        if ask:
            source_points.append(ask)
        if sig:
            source_points.append(sig)
        if carrier:
            source_points.append(f"Carrier: {carrier}")
        source_points.extend(script_points)
        if sms_body:
            source_points.append("SMS follow-up is prewritten on the call dashboard.")

        lead = {
            "id": f"call-{idx}-{phone[-10:]}",
            "business_name": biz,
            "owner_name": owner,
            "category": category,
            "city": city,
            "state": "GA",
            "phones_raw": [phone],
            "vercel_url": "",
            "rating": rating,
            "reviews": reviews,
        }
        lead["talking_points"] = _nepq_talking_points(lead, source_points)

        out.append(lead)
    return out


def build_queue(source="mock", limit=None):
    """Return a list of lead dicts with deduped E.164 phones, in dial order.

    Each lead: {
      id, business_name, owner_name, category, city, state,
      phones: [+1XXXXXXXXXX, ...],  (deduped, valid only)
      vercel_url, rating, reviews, talking_points: [str, ...],
    }
    """
    if source == "call_dashboard":
        try:
            raw = _load_call_dashboard_leads()
        except Exception as e:
            print(f"WARN: call dashboard lead source failed ({e}). Using mock.")
            raw = MOCK_LEADS
    elif source == "dashboard":
        try:
            raw = _load_dashboard_leads()
        except Exception as e:
            print(f"WARN: dashboard lead source failed ({e}). Using mock.")
            raw = MOCK_LEADS
    elif source == "railway":
        # Pull from atl-pipeline Railway DB. Not yet wired — requires `railway login` + railway run.
        # For now, fall through to mock and log a warning.
        # Implementation plan: shell out to `railway run python -m atl_pipeline.cli export-leads`
        # which would dump JSON we read here. Or use libsql/sqlite over Railway proxy.
        print("WARN: DIALER_LEAD_SOURCE=railway but pull not yet implemented. Using mock.")
        raw = MOCK_LEADS
    else:
        raw = MOCK_LEADS

    out = []
    for r in raw[: limit or len(raw)]:
        phones = dedupe_phones(r.get("phones_raw") or [])
        if not phones:
            continue
        out.append({
            "id": r["id"],
            "business_name": r["business_name"],
            "owner_name": r.get("owner_name", ""),
            "category": r.get("category", ""),
            "city": r.get("city", ""),
            "state": r.get("state", ""),
            "phones": phones,
            "vercel_url": r.get("vercel_url", ""),
            "rating": r.get("rating"),
            "reviews": r.get("reviews"),
            "talking_points": r.get("talking_points") or _nepq_talking_points(r),
        })
    return out


# ─── routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if ROOT_PAGE in {"queue", "dashboard", "index"}:
        return send_from_directory(str(HERE), "index.html")
    return send_from_directory(str(HERE), "single.html")


@app.route("/queue")
@app.route("/dashboard")
def queue():
    return send_from_directory(str(HERE), "index.html")


@app.route("/single")
def single():
    return send_from_directory(str(HERE), "single.html")


@app.route("/healthz")
def healthz():
    return jsonify({
        "ok": True,
        "from": FROM_NUMBER,
        "twiml_app": bool(TWIML_APP_SID),
        "lead_source": LEAD_SRC,
        "root_page": ROOT_PAGE,
        "ivr_copilot": {
            "mode": IVR_COPILOT_MODE,
            "deepgram_key": bool(DEEPGRAM_API_KEY),
            "stream_url": bool(MEDIA_STREAM_URL),
            "websocket_server": bool(sock),
        },
        "agent": {
            "mode": IVR_AGENT_MODE,
            "available": bool(call_agent and call_agent.is_available()),
            "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        },
    })


@app.route("/status")
def status_aggregator():
    """Aggregated tool health for the dialer's status strip.
    Each tool reports: {ok: bool, degraded: bool, msg: str, detail: str}."""
    out = {}
    out["lead_source"] = LEAD_SRC

    # Twilio: account creds + from number + twiml app set
    twilio_ok = all([ACCOUNT_SID, API_KEY_SID, API_KEY_SECRET, FROM_NUMBER, TWIML_APP_SID])
    out["twilio"] = {
        "ok": twilio_ok,
        "degraded": False,
        "msg": "Twilio Voice",
        "detail": f"FROM {FROM_NUMBER}" if twilio_ok else "missing credentials",
    }

    # Deepgram: key + media stream URL configured
    out["deepgram"] = {
        "ok": bool(DEEPGRAM_API_KEY and MEDIA_STREAM_URL and sock),
        "degraded": bool(DEEPGRAM_API_KEY) and not bool(MEDIA_STREAM_URL and sock),
        "msg": "Deepgram STT",
        "detail": "configured" if (DEEPGRAM_API_KEY and MEDIA_STREAM_URL) else "missing key or public URL",
    }

    # Anthropic: probe a 1-token call to verify capacity (cached 5min)
    out["anthropic"] = _probe_anthropic()

    # Outscraper: probe (cached 5min) — known to be past-due
    out["outscraper"] = _probe_outscraper()

    return jsonify(out)


# Tiny LRU cache for probes — re-check each tool at most every 5 min
_PROBE_CACHE = {}
_PROBE_TTL = 300  # seconds


def _probe_anthropic():
    import time as _t
    key = "anthropic"
    cached = _PROBE_CACHE.get(key)
    if cached and _t.time() - cached["at"] < _PROBE_TTL:
        return cached["data"]
    data = {"ok": False, "degraded": False, "msg": "Anthropic", "detail": "no key"}
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _PROBE_CACHE[key] = {"at": _t.time(), "data": data}
        return data
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=api_key)
        # 1-token cheap probe to claude-haiku
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4,
            messages=[{"role": "user", "content": "ok"}],
        )
        if resp and resp.content:
            data = {"ok": True, "degraded": False, "msg": "Anthropic", "detail": "Sonnet+Haiku reachable"}
    except Exception as e:
        err = str(e)[:120]
        # 529 overload → degraded (not dead)
        if "529" in err or "overload" in err.lower():
            data = {"ok": False, "degraded": True, "msg": "Anthropic", "detail": "overloaded (529)"}
        else:
            data = {"ok": False, "degraded": False, "msg": "Anthropic", "detail": err}
    _PROBE_CACHE[key] = {"at": _t.time(), "data": data}
    return data


def _probe_outscraper():
    import time as _t
    key = "outscraper"
    cached = _PROBE_CACHE.get(key)
    if cached and _t.time() - cached["at"] < _PROBE_TTL:
        return cached["data"]
    data = {"ok": False, "degraded": False, "msg": "Outscraper", "detail": "no key"}
    api_key = os.environ.get("OUTSCRAPER_API_KEY")
    if not api_key:
        _PROBE_CACHE[key] = {"at": _t.time(), "data": data}
        return data
    try:
        r = requests.get(
            "https://api.outscraper.cloud/maps/search-v3",
            params={"query": "Acme, Atlanta, GA, USA", "limit": 1, "async": "false"},
            headers={"X-API-KEY": api_key},
            timeout=10,
        )
        body = r.json() if r.headers.get("Content-Type", "").startswith("application/json") else {}
        if r.status_code == 200 and not body.get("error"):
            data = {"ok": True, "degraded": False, "msg": "Outscraper", "detail": "API reachable"}
        elif "past-due" in (body.get("errorMessage", "") or "").lower() or "credit" in (body.get("errorMessage", "") or "").lower():
            data = {"ok": False, "degraded": True, "msg": "Outscraper", "detail": "account past-due — top up"}
        else:
            data = {"ok": False, "degraded": True, "msg": "Outscraper", "detail": (body.get("errorMessage") or f"http {r.status_code}")[:80]}
    except Exception as e:
        data = {"ok": False, "degraded": False, "msg": "Outscraper", "detail": str(e)[:120]}
    _PROBE_CACHE[key] = {"at": _t.time(), "data": data}
    return data


# ─── pre-call AI briefing ───────────────────────────────────────────────────
@app.route("/briefing", methods=["POST"])
def briefing():
    """Generate / fetch a 3-bullet pre-call briefing for a lead.
    Cached in `briefings` table — first call is ~$0.001, repeats are free."""
    data = request.get_json(force=True) or {}
    lead_id = data.get("lead_id") or (data.get("lead") or {}).get("id")
    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400
    # Cached?
    with _db() as c:
        row = c.execute("SELECT bullets, created_at FROM briefings WHERE lead_id = ?", (lead_id,)).fetchone()
    if row:
        try:
            return jsonify({"bullets": json.loads(row["bullets"]), "cached": True})
        except Exception:
            pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return jsonify({"bullets": [], "error": "no anthropic key"}), 200

    lead = data.get("lead") or {}
    biz = lead.get("business_name") or ""
    cat = lead.get("category") or ""
    city = lead.get("city") or ""
    rating = lead.get("rating")
    reviews = lead.get("reviews")
    talking = lead.get("talking_points") or []
    owner = lead.get("owner_name") or ""

    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        model = os.environ.get("BRIEFING_MODEL", "claude-haiku-4-5-20251001")
        system = (
            "You write tight 3-bullet pre-call briefings for Tyler Brown, a cold-call rep at Nova, who builds free demo websites for local trade businesses in Atlanta. "
            "Tyler is about to dial. He has 5 seconds to read your bullets before the line rings. Pull intel from the data given; do NOT invent. "
            "Each bullet is one specific sentence: a hook, a leverage point, a thing to say. No generic advice."
        )
        user = (
            f"BUSINESS: {biz}\n"
            f"CATEGORY: {cat}\n"
            f"LOCATION: {city}\n"
            f"RATING: {rating} ({reviews} reviews)\n"
            f"OWNER (if surfaced): {owner}\n"
            f"EXISTING TALKING POINTS (raw): {talking[:8]}\n\n"
            "Return JSON only: {\"bullets\": [\"...\", \"...\", \"...\"]} — exactly 3 bullets, max 14 words each."
        )
        resp = client.messages.create(model=model, max_tokens=250, system=system,
                                       messages=[{"role": "user", "content": user}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        if text.startswith("```"):
            text = re.sub(r"^```\w*\s*", "", text).rsplit("```", 1)[0].strip()
        try:
            parsed = json.loads(text)
        except Exception:
            return jsonify({"bullets": [], "error": f"parse failed: {text[:120]}"}), 200
        bullets = parsed.get("bullets") or []
        bullets = [str(b).strip() for b in bullets if b and isinstance(b, (str,))][:3]
        if not bullets:
            return jsonify({"bullets": []}), 200
        usage = resp.usage
        cost = round(getattr(usage, "input_tokens", 0) * 1.0 / 1_000_000 + getattr(usage, "output_tokens", 0) * 5.0 / 1_000_000, 5)
        with _db() as c:
            c.execute(
                "INSERT OR REPLACE INTO briefings (lead_id, bullets, model, cost_usd) VALUES (?, ?, ?, ?)",
                (lead_id, json.dumps(bullets), model, cost),
            )
            c.commit()
        return jsonify({"bullets": bullets, "cached": False, "cost": cost})
    except Exception as e:
        return jsonify({"bullets": [], "error": str(e)[:200]}), 200


# ─── transcript + summary endpoints ─────────────────────────────────────────
@app.route("/transcripts")
def list_transcripts():
    with _db() as c:
        rows = c.execute(
            "SELECT session_id, lead_id, business_name, phone, chunk_count, started_at, ended_at, length(transcript) AS len "
            "FROM call_transcripts ORDER BY started_at DESC LIMIT 50"
        ).fetchall()
    return jsonify({"transcripts": [dict(r) for r in rows]})


@app.route("/transcript/<session_id>")
def get_transcript(session_id):
    with _db() as c:
        row = c.execute("SELECT * FROM call_transcripts WHERE session_id = ?", (session_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        summary = c.execute("SELECT * FROM call_summaries WHERE session_id = ?", (session_id,)).fetchone()
    out = dict(row)
    if summary:
        s = dict(summary)
        for k in ("key_objections", "follow_up_actions", "key_quotes"):
            try: s[k] = json.loads(s[k] or "[]")
            except Exception: s[k] = []
        out["summary"] = s
    return jsonify(out)


@app.route("/summary/<session_id>")
def get_summary(session_id):
    with _db() as c:
        row = c.execute("SELECT * FROM call_summaries WHERE session_id = ?", (session_id,)).fetchone()
    if not row:
        return jsonify({"error": "no summary yet"}), 404
    out = dict(row)
    for k in ("key_objections", "follow_up_actions", "key_quotes"):
        try: out[k] = json.loads(out[k] or "[]")
        except Exception: out[k] = []
    return jsonify(out)


@app.route("/calls/active", methods=["POST"])
def calls_active():
    """Frontend tells us which lead is being dialed so the agent has context.
    Body: {lead_id, business_name, owner_name, category, city, phone}
    """
    data = request.get_json(force=True, silent=True) or {}
    if call_agent:
        call_agent.set_active_lead(data or None)
    return jsonify({"ok": True, "active": data})


@app.route("/calls/clear", methods=["POST"])
def calls_clear():
    if call_agent:
        call_agent.set_active_lead(None)
    return jsonify({"ok": True})


@app.route("/token")
def token():
    if not TWIML_APP_SID:
        return jsonify({"error": "TWILIO_TWIML_APP_SID not set in .env"}), 500
    tok = AccessToken(ACCOUNT_SID, API_KEY_SID, API_KEY_SECRET, identity="dialer", ttl=3600)
    tok.add_grant(VoiceGrant(outgoing_application_sid=TWIML_APP_SID, incoming_allow=False))
    return jsonify({"token": tok.to_jwt(), "identity": "dialer"})


@app.route("/voice", methods=["POST"])
def voice():
    to   = request.values.get("To", "")
    session_id = request.values.get("SessionId", "")
    resp = VoiceResponse()
    if not to:
        resp.say("No destination number provided.")
        return str(resp), 200, {"Content-Type": "text/xml"}
    if IVR_COPILOT_MODE != "off" and MEDIA_STREAM_URL and DEEPGRAM_API_KEY:
        start = resp.start()
        stream = start.stream(url=MEDIA_STREAM_URL, track="outbound_track")
        if session_id:
            stream.parameter(name="session_id", value=session_id)
    dial = Dial(caller_id=FROM_NUMBER, answer_on_bridge=True, time_limit=3600)
    dial.number(to)
    resp.append(dial)
    return str(resp), 200, {"Content-Type": "text/xml"}


@app.route("/leads")
def leads():
    """Returns the dial queue with deduped phones per lead.

    Filters out leads that have a 'do not contact' disposition recorded
    in the local dispositions DB.
    """
    q = build_queue(source=LEAD_SRC)
    with _db() as c:
        dnc = {r["lead_id"] for r in c.execute(
            "SELECT DISTINCT lead_id FROM dispositions WHERE code='dnc'"
        )}
        # also pull existing dispositions to surface in UI
        rows = c.execute(
            "SELECT lead_id, phone, code, at FROM dispositions ORDER BY at"
        ).fetchall()
        note_rows = c.execute(
            "SELECT lead_id, phone, email, callback_at, note, updated_at FROM lead_notes"
        ).fetchall()
    dispositions = {}
    for r in rows:
        dispositions.setdefault(r["lead_id"], []).append(dict(r))
    notes = {r["lead_id"]: dict(r) for r in note_rows}
    q = [l for l in q if l["id"] not in dnc]
    return jsonify({
        "leads": q,
        "dispositions": dispositions,
        "notes": notes,
        "source": LEAD_SRC,
        "from_number": FROM_NUMBER,
    })


@app.route("/disposition", methods=["POST"])
def disposition():
    data    = request.get_json(force=True)
    lead_id = data.get("lead_id")
    phone   = data.get("phone")
    code    = data.get("code")  # no_answer | voicemail | callback | not_interested | interested | dnc | skip
    note    = data.get("note", "")
    pass_n  = int(data.get("pass", 1))
    if not (lead_id and phone and code):
        return jsonify({"error": "lead_id, phone, code required"}), 400
    with _db() as c:
        c.execute(
            "INSERT INTO dispositions (lead_id, phone, code, note, pass) VALUES (?,?,?,?,?)",
            (lead_id, phone, code, note, pass_n),
        )
        c.commit()
    return jsonify({"ok": True})


@app.route("/dispositions")
def dispositions_list():
    with _db() as c:
        rows = c.execute(
            "SELECT lead_id, phone, code, note, at, pass FROM dispositions ORDER BY at DESC"
        ).fetchall()
    return jsonify({"dispositions": [dict(r) for r in rows]})


@app.route("/ivr/events")
def ivr_events():
    session_id = request.args.get("session_id", "")
    since = int(request.args.get("since", 0) or 0)
    with IVR_LOCK:
        events = [e for e in IVR_EVENTS.get(session_id, []) if int(e.get("seq", 0)) > since]
    return jsonify({
        "events": events,
        "mode": IVR_COPILOT_MODE,
        "agent_mode": IVR_AGENT_MODE,
        "agent_available": bool(call_agent and call_agent.is_available()),
        "enabled": bool(DEEPGRAM_API_KEY and MEDIA_STREAM_URL and sock),
        "stream_url": bool(MEDIA_STREAM_URL),
        "deepgram_key": bool(DEEPGRAM_API_KEY),
        "websocket_server": bool(sock),
    })


@app.route("/ivr/test", methods=["POST"])
def ivr_test():
    data = request.get_json(force=True) or {}
    transcript = data.get("transcript", "")
    return jsonify({"choice": detect_ivr_digit(transcript)})


if sock:
    @sock.route("/media")
    def media_stream(ws):
        _bridge_twilio_to_deepgram(ws)


@app.route("/note", methods=["POST"])
def save_note():
    data = request.get_json(force=True) or {}
    lead_id = data.get("lead_id")
    if not lead_id:
        return jsonify({"error": "lead_id required"}), 400

    phone = data.get("phone", "")
    email = (data.get("email") or "").strip()
    callback_at = (data.get("callback_at") or "").strip()
    note = (data.get("note") or "").strip()
    with _db() as c:
        c.execute(
            """INSERT INTO lead_notes (lead_id, phone, email, callback_at, note, updated_at)
               VALUES (?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(lead_id) DO UPDATE SET
                 phone=excluded.phone,
                 email=excluded.email,
                 callback_at=excluded.callback_at,
                 note=excluded.note,
                 updated_at=CURRENT_TIMESTAMP""",
            (lead_id, phone, email, callback_at, note),
        )
        c.commit()
        row = c.execute(
            "SELECT lead_id, phone, email, callback_at, note, updated_at FROM lead_notes WHERE lead_id=?",
            (lead_id,),
        ).fetchone()
    return jsonify({"ok": True, "note": dict(row)})


@app.route("/notes")
def notes_list():
    with _db() as c:
        rows = c.execute(
            "SELECT lead_id, phone, email, callback_at, note, updated_at FROM lead_notes ORDER BY updated_at DESC"
        ).fetchall()
    return jsonify({"notes": [dict(r) for r in rows]})


@app.route("/build", methods=["POST"])
def build_site():
    """Triggered by the dialer's B = interested + build button.

    Routes to the build_agent orchestrator (Steps 1-6 of SPEC) when available,
    falls back to the legacy _run_build_job() when build_agent isn't installed
    on this host (e.g. Railway prod, which currently doesn't ship Playwright).
    """
    data = request.get_json(force=True) or {}
    lead = _lead_for_build(data.get("lead") or data)
    if not lead["business_name"]:
        return jsonify({"error": "lead.business_name required"}), 400

    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "lead_id": lead["id"],
        "slug": lead["slug"],
        "business_name": lead["business_name"],
        "status": "queued",
        "message": "Queued website build.",
        "url": None,
        "error": None,
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
        "events": [],         # streaming progress events for the dialer UI
        "preview_url": None,
        "rep_approved": False,
        "rep_approved_at": None,
        "feels_like_score": None,
        "use_build_agent": _try_use_build_agent(),
    }
    with BUILD_LOCK:
        BUILD_JOBS[job_id] = job

    if job["use_build_agent"]:
        target = _run_build_agent_job
    else:
        target = _run_build_job
    thread = threading.Thread(target=target, args=(job_id, lead), daemon=True)
    thread.start()
    return jsonify(job)


@app.route("/build/<job_id>")
def build_status(job_id):
    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "build job not found"}), 404
        return jsonify(dict(job))


@app.route("/build/<job_id>/approve", methods=["POST"])
def build_approve(job_id):
    """Rep clicks 'Send to Lead' — gates the lead-side SMS. Per SPEC §5."""
    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "build job not found"}), 404
        if not job.get("preview_url"):
            return jsonify({"error": "no preview_url yet — build not complete"}), 400
        job["rep_approved"] = True
        job["rep_approved_at"] = _now_iso()
    # Optional: send SMS to lead now. Reuses existing Twilio config.
    try:
        from twilio.rest import Client as TwilioClient
        lead_phone = (job.get("lead_phone") or "").strip()
        if FROM_NUMBER and lead_phone:
            owner_first = (job.get("owner_first") or "there").strip()
            body = (
                f"hey {owner_first} — tyler with nova. built this preview while we "
                f"were talking: {job['preview_url']} — take a look while we're on."
            )
            tw = TwilioClient(ACCOUNT_SID, os.environ.get("TWILIO_AUTH_TOKEN", ""))
            sent = tw.messages.create(to=lead_phone, from_=FROM_NUMBER, body=body)
            job["lead_sms_sid"] = sent.sid
    except Exception as e:
        job["lead_sms_error"] = str(e)
    return jsonify(job)


@app.route("/build/<job_id>/calibration", methods=["POST"])
def build_calibration(job_id):
    """Rep's 1-5 'feels like theirs' rating per SPEC §2. THE feedback engine."""
    data = request.get_json(force=True) or {}
    score = data.get("score")
    note = (data.get("note") or "").strip()
    try:
        score_int = int(score)
        assert 1 <= score_int <= 5
    except Exception:
        return jsonify({"error": "score must be int 1-5"}), 400

    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "build job not found"}), 404
        job["feels_like_score"] = score_int
        job["feels_like_note"] = note

    # Also write to build_agent's calibration table if the build_agent DB exists
    try:
        import sqlite3
        from pathlib import Path as _P
        cal_db = _P(os.environ.get("BUILD_AGENT_DB", str(_P(__file__).resolve().parents[1] / "build_agent" / "_data" / "build_agent.db")))
        if cal_db.exists():
            with sqlite3.connect(cal_db) as cc:
                cc.execute(
                    "INSERT OR REPLACE INTO build_calibration "
                    "(build_id, lead_id, feels_like_score, feels_like_note, rated_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (job.get("agent_job_id") or job_id, job.get("lead_id", ""), score_int, note),
                )
    except Exception as e:
        job["calibration_db_error"] = str(e)
    return jsonify(job)


def _try_use_build_agent() -> bool:
    """Decide whether build_agent.orchestrator is importable + dependencies are met."""
    if os.environ.get("DIALER_USE_LEGACY_BUILD") == "1":
        return False
    try:
        import build_agent.orchestrator  # noqa: F401
        return True
    except Exception:
        return False


def _run_build_agent_job(job_id, lead):
    """Hand the lead to build_agent.orchestrator with streaming progress callback."""
    try:
        import sys as _sys
        atl_root = str(Path(__file__).resolve().parent.parent)
        if atl_root not in _sys.path:
            _sys.path.insert(0, atl_root)
        from build_agent import orchestrator  # type: ignore
    except Exception as e:
        _job_update(job_id, status="error", message=f"build_agent unavailable: {e}", finished_at=_now_iso())
        return

    # Capture lead context for SMS-to-lead later (we don't auto-send; rep approves)
    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if job:
            job["lead_phone"] = lead.get("phone")
            job["owner_first"] = (lead.get("owner_name") or "").split(" ")[0]

    def progress(event: str, payload: dict):
        with BUILD_LOCK:
            j = BUILD_JOBS.get(job_id)
            if not j:
                return
            evt = {"at": _now_iso(), "event": event, **payload}
            j.setdefault("events", []).append(evt)
            j["events"] = j["events"][-50:]
            j["updated_at"] = _now_iso()
            j["status"] = event
            j["message"] = _pretty_event_message(event, payload)

    try:
        result = orchestrator.build(lead, progress=progress)
        with BUILD_LOCK:
            j = BUILD_JOBS.get(job_id) or {}
            j["agent_job_id"] = result.get("job_id")
            j["agent_result"] = result
            if result.get("error"):
                j["status"] = "error"
                j["error"] = result["error"]
                j["message"] = f"build failed: {result['error']}"
            else:
                # Preview URL — for v1 we serve from the local build_agent _data dir
                j["preview_url"] = f"/build_agent_preview/{result['slug']}/index.html"
                j["status"] = "completed"
                j["message"] = (
                    f"Site ready · code={result.get('code_score')} · "
                    f"vision={result.get('vision_score')} · "
                    f"${result.get('budget_used')} in {result.get('duration_sec')}s · "
                    f"awaiting rep approval"
                )
                j["url"] = j["preview_url"]
            j["finished_at"] = _now_iso()
    except Exception as e:
        _job_update(job_id, status="error", message=str(e), error=traceback.format_exc(limit=8), finished_at=_now_iso())


def _pretty_event_message(event: str, payload: dict) -> str:
    if event == "queued":
        return "Queued."
    if event == "researching":
        return f"Researching {payload.get('business_name','...')} via GBP + existing site..."
    if event == "gathering_assets":
        return "Downloading prospect photos + extracting palette..."
    if event == "picking_inspiration":
        return "Picking inspiration refs from corpus..."
    if event == "inspiration_picked":
        return f"Picked refs: {', '.join(payload.get('ref_ids', []))}"
    if event == "building_first_draft":
        return "Sonnet writing first draft..."
    if event == "critic_done":
        return (
            f"Iter {payload.get('iteration')} · code {payload.get('code_score')}/100 "
            f"· vision {payload.get('vision_score')}/10 · "
            f"${payload.get('budget_remaining'):.2f} left"
        )
    if event == "done":
        return (
            f"Done · code={payload.get('code_score')} vision={payload.get('vision_score')} "
            f"({payload.get('ship_reason')})"
        )
    return event


@app.route("/build_agent_preview/<path:filename>")
def build_agent_preview(filename):
    """Serve the built site (from build_agent/_data/builds/<slug>/...)."""
    base = Path(os.environ.get("BUILD_AGENT_BUILDS_DIR", str(Path(__file__).resolve().parent.parent / "build_agent" / "_data" / "builds")))
    return send_from_directory(str(base), filename)


@app.route("/builds/<path:filename>")
def built_preview(filename):
    return send_from_directory(str(BUILD_DIR), filename)


PORT = int(os.environ.get("PORT", "5050"))

if __name__ == "__main__":
    print(f"FROM:          {FROM_NUMBER}")
    print(f"TwiML App SID: {TWIML_APP_SID or '(not set)'}")
    print(f"Lead source:   {LEAD_SRC}")
    print(f"Root page:     {ROOT_PAGE}")
    print(f"DB:            {DB_PATH}")
    print(f"Listening:     0.0.0.0:{PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
