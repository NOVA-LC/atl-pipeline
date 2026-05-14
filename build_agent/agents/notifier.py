"""Notifier — pings the REP (not the lead) when a build is ready.

The rep gates the lead-side SMS. Auto-SMS to lead is forbidden — SPEC §5.

Step 1 STATUS: stub. Implemented in Step 7.
"""
from __future__ import annotations

from typing import Any


def notify_rep(build_state: dict[str, Any], preview_url: str) -> None:
    """Send to rep's dialer (SSE / WebSocket / Twilio depending on what's wired)."""
    raise NotImplementedError("Step 7 deliverable")


def send_lead_sms(lead: dict[str, Any], preview_url: str) -> dict[str, Any]:
    """Called ONLY after rep clicks 'Send to Lead' in dialer. Never automated."""
    raise NotImplementedError("Step 7 deliverable")
