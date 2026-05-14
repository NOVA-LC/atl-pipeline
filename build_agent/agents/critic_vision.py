"""Vision critic — Sonnet vision applies the explicit 6-axis rubric in SPEC §4.

Axes + weights (LOCKED, do not change without SPEC update):
  Originality   0.25
  Composition   0.20
  Type          0.15
  Color         0.15
  Photography   0.15
  Craft         0.10

System prompt MUST include the 5 cold-start anchor examples in
prompts/vision_critic_examples/. Without anchors the critic drifts.

Floor for ship: 7.5/10 weighted final.

Step 1 STATUS: stub. Implemented in Step 5.
"""
from __future__ import annotations

from typing import Any


RUBRIC_WEIGHTS = {
    "originality": 0.25,
    "composition": 0.20,
    "type": 0.15,
    "color": 0.15,
    "photography": 0.15,
    "craft": 0.10,
}

VISION_FLOOR = 7.5


def grade(screenshot_paths: dict[str, str], research_brief: dict[str, Any], inspiration_ref_ids: list[str]) -> dict[str, Any]:
    """Input: {'mobile': path, 'tablet': path, 'desktop': path} screenshots.
    Output: {composition, type, color, photography, originality, craft, final_weighted, must_fixes}.
    """
    raise NotImplementedError("Step 5 deliverable")


def weighted_final(rubric: dict[str, dict[str, Any]]) -> float:
    """Combine per-axis scores into final 1-10 weighted value."""
    total = 0.0
    for axis, weight in RUBRIC_WEIGHTS.items():
        score = rubric.get(axis, {}).get("score", 0)
        total += score * weight
    return round(total, 2)
