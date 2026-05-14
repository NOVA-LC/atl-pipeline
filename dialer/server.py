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


def _handle_ivr_transcript(session_id, transcript, seen):
    cleaned = re.sub(r"\s+", " ", transcript or "").strip()
    if not cleaned:
        return
    key = cleaned.lower()
    if key in seen:
        return
    seen.add(key)
    _add_ivr_event(session_id, kind="transcript", message=f"IVR heard: {cleaned}", transcript=cleaned)
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
        message=f"IVR Copilot {'auto-pressing' if mode == 'auto' else 'suggests pressing'} {digit}: \"{cleaned}\"",
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
    })


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
    }
    with BUILD_LOCK:
        BUILD_JOBS[job_id] = job

    thread = threading.Thread(target=_run_build_job, args=(job_id, lead), daemon=True)
    thread.start()
    return jsonify(job)


@app.route("/build/<job_id>")
def build_status(job_id):
    with BUILD_LOCK:
        job = BUILD_JOBS.get(job_id)
        if not job:
            return jsonify({"error": "build job not found"}), 404
        return jsonify(dict(job))


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
