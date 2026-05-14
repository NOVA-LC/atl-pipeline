"""Inspiration picker — selects 3-5 refs from inspiration/ per SPEC §6.

Selection rules:
- industry_fit match (priority 1)
- vibe_tag distance from the brand's signals (priority 2)
- fingerprint diversity vs the last 5 builds (palette overlap < 50%)

Step 1 STATUS: stub. Implemented in Step 4. Requires Step 0 corpus.
"""
from __future__ import annotations

from typing import Any


def pick(research_brief: dict[str, Any], recent_build_fingerprints: list[dict[str, Any]]) -> list[str]:
    """Returns list of inspiration ref IDs (e.g. ['awwwards-001', 'mindsparkle-014'])."""
    raise NotImplementedError("Step 4 deliverable")
