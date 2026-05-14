"""FLUX image generation via Replicate — LAST-RESORT only.

Hard rule: never used when a real prospect asset exists for that slot.

Timeout: 90s. Retries: 1. Fallback: leave slot imageless rather than degrade to stock.

Step 1 STATUS: stub. Implemented in Step 3.
"""
from __future__ import annotations

from pathlib import Path


def generate(prompt: str, slot: str, out_path: Path) -> Path | None:
    """Returns path to generated image or None on failure."""
    raise NotImplementedError("Step 3 deliverable")
