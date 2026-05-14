"""Brave Search API client.

Timeout: 15s. Retries: 2. Fallback: skip and rely on GBP / existing site.

Step 1 STATUS: stub. Implemented in Step 2.
"""
from __future__ import annotations

from typing import Any


def search(query: str, count: int = 10) -> list[dict[str, Any]]:
    """Returns [{title, url, description, ...}]."""
    raise NotImplementedError("Step 2 deliverable")
