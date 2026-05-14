"""Outscraper API client — fetch Google Business Profile data.

Timeout: 30s. Retries: 2 with backoff (1s, 4s). Fallback: skip GBP, lean on existing-site scrape.

Step 1 STATUS: stub. Implemented in Step 2.
"""
from __future__ import annotations

from typing import Any


def fetch_gbp(business_name: str, city: str, state: str = "GA") -> dict[str, Any] | None:
    """Returns parsed GBP record or None if not found / API fails after retries."""
    raise NotImplementedError("Step 2 deliverable")


def fetch_gbp_photos(place_id: str, max_photos: int = 20) -> list[str]:
    """Returns list of photo URLs from the GBP listing."""
    raise NotImplementedError("Step 2 deliverable")
