"""Researcher — builds research_brief.json from GBP + existing website only.

Hard rules per SPEC §3:
- No FB / IG / LinkedIn scraping.
- Every fact recorded MUST have a source URL or be null.
- Cost ceiling: $1.00 per lead.
- Timeout: 30s per tool, 2 retries.

Step 1 STATUS: stub. Implemented in Step 2.
"""
from __future__ import annotations

from typing import Any


def research(lead: dict[str, Any]) -> dict[str, Any]:
    """Input: {lead_id, business_name, city, phone}.
    Output: research_brief.json validated against schemas/research_brief.schema.json.
    Returns {"build_unfit": true} if no GBP and no existing website (pre-filter)."""
    raise NotImplementedError("Step 2 deliverable")
