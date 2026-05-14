"""Existing-website scraper — pulls palette, services, reviews, photos from a
prospect's current website if they have one.

Timeout: 30s. Retries: 1. Fallback: skip, lean on GBP.

Step 1 STATUS: stub. Implemented in Step 2.
"""
from __future__ import annotations

from typing import Any


def scrape(url: str) -> dict[str, Any] | None:
    """Returns {palette, services[], reviews[], photos[], copy_samples[]} or None."""
    raise NotImplementedError("Step 2 deliverable")
