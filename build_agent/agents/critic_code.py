"""Code critic — extends atl_pipeline/agent/iterate.py with realness, cross-section
consistency, and originality checks per SPEC §5.

Deterministic-leaning: prefers regex / static analysis where possible, calls Sonnet
only to parse must_fixes into structured intents.

Step 1 STATUS: stub. Implemented in Step 5.
"""
from __future__ import annotations

from typing import Any


def grade(html: str, research_brief: dict[str, Any], inspiration_ref_ids: list[str]) -> dict[str, Any]:
    """Returns {score, must_fixes, should_fixes, strengths, fingerprint}."""
    raise NotImplementedError("Step 5 deliverable")
