"""Palette extraction via k-means on RGB pixels.

Pure deterministic (no API). Fallback: industry-default neutral palette.

Step 1 STATUS: stub. Implemented in Step 3.
"""
from __future__ import annotations

from pathlib import Path


def extract(image_path: Path, k: int = 5) -> list[str]:
    """Returns list of hex colors sorted by visual weight."""
    raise NotImplementedError("Step 3 deliverable")


def industry_fallback(vertical: str) -> list[str]:
    """Industry-default palette when extraction fails (per SPEC §11 known unknown #8)."""
    raise NotImplementedError("Step 3 deliverable")
