"""Builder — Sonnet writes a complete HTML+CSS site from scratch per SPEC §3.

Hard rules:
- Only render facts present in research_brief (every claim has a source).
- Reference the inspiration refs' compositions/treatments — never copy code verbatim.
- Follow design_system/primitives.css tokens + rules.md constraints.
- Real prospect images only — empty slot rather than stock photo.
- Mobile-first; hero headline fits on one line at 375px.

Step 1 STATUS: stub. Implemented in Step 4.
"""
from __future__ import annotations

from typing import Any


def build_html(
    research_brief: dict[str, Any],
    assets_manifest: dict[str, Any],
    inspiration_ref_ids: list[str],
) -> str:
    """Returns a single self-contained HTML file."""
    raise NotImplementedError("Step 4 deliverable")


def regenerate_section(
    current_html: str,
    section: str,
    must_fix: str,
    research_brief: dict[str, Any],
    assets_manifest: dict[str, Any],
) -> str:
    """Targeted re-render of one section based on a critic must_fix."""
    raise NotImplementedError("Step 6 dispatch — uses this from Step 4 builder")
