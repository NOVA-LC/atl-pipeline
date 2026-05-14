"""Call Agent — Haiku-powered live phone-call reasoning.

Listens to live transcripts from Deepgram and decides what to do at every tick:
press a DTMF digit, wait, alert Tyler, mark voicemail, or take a note.

This is wired into server.py's `_handle_ivr_transcript()`. When
`IVR_COPILOT_MODE` is `agent` (or `auto` which we treat as agent + auto-press),
the agent's decision drives the call. Regex is the fallback when the agent
errors out or returns nothing useful.

Cost: roughly $0.0001 per tick at Haiku 4.5 prices. Capped at ~1 tick / 1.5s.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # type: ignore

MODEL = os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001")
MIN_TICK_INTERVAL = float(os.environ.get("AGENT_MIN_TICK_SECONDS", "1.5"))
TRANSCRIPT_WINDOW_CHARS = 1400  # last ~5-6 sentences

# Per-session state. Keyed by Twilio call SID / session_id.
_STATE_LOCK = threading.Lock()
_STATE: dict[str, dict[str, Any]] = {}

# Active lead set by the dialer frontend via POST /calls/active. One at a time.
_ACTIVE_LEAD_LOCK = threading.Lock()
_ACTIVE_LEAD: dict[str, Any] | None = None


SYSTEM_PROMPT = """You are the Call Agent for Tyler Brown's cold-call dialer at Nova in Atlanta. Tyler builds free website previews for local trade businesses (plumbers, HVAC, landscapers, roofers, etc.) and cold-calls owners to show them. He's already on the line — you're his copilot navigating phone systems so he can stay focused on talking to humans.

You hear a LIVE phone call via Deepgram transcript. Every ~1.5 seconds you get the latest speech and decide one action.

GOAL HIERARCHY (decide based on what's happening RIGHT NOW):

1. LIVE HUMAN detected (greeting, "Hello", "Joey's Plumbing, this is Linda") → **alert_tyler** so he stops navigating and starts pitching. Do NOT press anything.

2. GATEKEEPER (receptionist, "office of...", "may I ask who's calling") → **alert_tyler** with note about who answered. Tyler will ask for the owner.

3. IVR MENU heard → pick the option that gets to a decision-maker / new customer / sales / operator. Prefer in this order:
   - "new customers" / "sales" / "estimates" / "quotes" / "schedule service"
   - "operator" / "speak to someone" / "representative" / "all other inquiries"
   - "0" if nothing else fits
   NEVER pick: billing, accounts, payments, tech support, hours, location, espanol/spanish (unless mid-call user request).

4. VOICEMAIL ("leave a message", "after the tone", "you've reached the voicemail") → **mark_voicemail**.

5. HOLD MUSIC, "please hold", silence → **wait**. Don't press anything.

6. AMBIGUOUS or already-acted-upon → **wait**. Most ticks should be wait.

TOOLS (output EXACTLY one as strict JSON, no prose, no code fences):

{"action":"press_digit","arg":"<single char 0-9 * #>","reason":"<one sentence>"}
{"action":"wait","reason":"<one sentence>"}
{"action":"alert_tyler","arg":"<short message Tyler will see>","reason":"<one sentence>"}
{"action":"mark_voicemail","reason":"<one sentence>"}
{"action":"note","arg":"<note text>","reason":"<one sentence>"}

RULES:
- Never repeat an action you just took. Check "ACTIONS YOU'VE TAKEN" — if you pressed 2 already, don't press 2 again unless the menu has clearly changed.
- If the transcript is only a partial sentence ("...for billing, press —") wait for more.
- One tool call per response. JSON only. No markdown. No prose.
- When in doubt, wait."""


def set_active_lead(lead: dict[str, Any] | None) -> None:
    with _ACTIVE_LEAD_LOCK:
        global _ACTIVE_LEAD
        _ACTIVE_LEAD = lead


def get_active_lead() -> dict[str, Any] | None:
    with _ACTIVE_LEAD_LOCK:
        return dict(_ACTIVE_LEAD) if _ACTIVE_LEAD else None


def _session(session_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        s = _STATE.get(session_id)
        if not s:
            s = {
                "transcript": "",
                "actions": [],            # [{action, arg, reason, at}]
                "last_tick_at": 0.0,
                "stop": False,
            }
            _STATE[session_id] = s
        return s


def reset_session(session_id: str) -> None:
    with _STATE_LOCK:
        _STATE.pop(session_id, None)


def is_available() -> bool:
    return Anthropic is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


def _client() -> Any:
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _extract_json(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of model output, even if wrapped in fences."""
    if not text:
        return None
    text = text.strip()
    # Strip ```json ... ``` fences if present
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    # First {...} block
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


def think(session_id: str, transcript_chunk: str) -> dict[str, Any] | None:
    """Run one agent tick. Returns parsed action dict, or None to skip.

    Caller should rate-limit + dispatch the returned action via existing
    IVR event channel.
    """
    if not is_available():
        return None

    chunk = (transcript_chunk or "").strip()
    if not chunk:
        return None

    s = _session(session_id)
    now = time.time()
    if now - s["last_tick_at"] < MIN_TICK_INTERVAL:
        return None
    s["last_tick_at"] = now

    # Accumulate transcript (cap window)
    if chunk not in s["transcript"]:
        s["transcript"] = (s["transcript"] + " " + chunk).strip()[-TRANSCRIPT_WINDOW_CHARS:]

    lead = get_active_lead() or {}
    biz = lead.get("business_name", "(unknown business)")
    cat = lead.get("category", "")
    city = lead.get("city", "")
    owner = lead.get("owner_name", "unknown")
    phone = lead.get("phone", "")

    user_msg = (
        f"LEAD: {biz}"
        + (f" — {cat}" if cat else "")
        + (f" — {city}, GA" if city else "")
        + f"\nOWNER: {owner}"
        + (f"\nPHONE: {phone}" if phone else "")
        + "\n\nFULL TRANSCRIPT (most recent at end):\n"
        + (s["transcript"] or "(silence so far)")
        + "\n\nLATEST CHUNK (just heard):\n"
        + chunk
        + "\n\nACTIONS YOU'VE TAKEN THIS CALL:\n"
        + (json.dumps(s["actions"][-6:], indent=1) if s["actions"] else "(none)")
        + "\n\nWhat one tool do you call? JSON only."
    )

    try:
        resp = _client().messages.create(
            model=MODEL,
            max_tokens=180,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    except Exception as e:
        # Surface as a soft error event upstream, but don't crash
        return {"action": "error", "reason": f"agent call failed: {e}"}

    parsed = _extract_json(text)
    if not parsed or "action" not in parsed:
        return {"action": "error", "reason": f"agent returned unparseable output: {text[:200]}"}

    parsed["raw_model_output"] = text
    s["actions"].append({
        "action": parsed.get("action"),
        "arg": parsed.get("arg"),
        "reason": parsed.get("reason"),
        "at": int(now),
    })
    s["actions"] = s["actions"][-12:]
    return parsed
