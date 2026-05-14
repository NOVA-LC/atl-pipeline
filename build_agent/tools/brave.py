"""Brave Search API client.

API: GET https://api.search.brave.com/res/v1/web/search
Auth: X-Subscription-Token header

Per SPEC §8: timeout 15s, 2 retries, fallback = return empty list.
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

BRAVE_API = "https://api.search.brave.com/res/v1/web/search"
TIMEOUT_SEC = 15
RETRIES = 2
BACKOFF = (1, 4)


def _api_key() -> str | None:
    return os.environ.get("BRAVE_API_KEY") or None


def search(query: str, count: int = 10) -> list[dict[str, Any]]:
    """Returns list of result dicts: [{title, url, description, age, ...}].

    Returns [] on auth missing or persistent failure.
    """
    key = _api_key()
    if not key:
        return []
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                BRAVE_API,
                params={"q": query, "count": min(count, 20)},
                headers={
                    "X-Subscription-Token": key,
                    "Accept": "application/json",
                },
                timeout=TIMEOUT_SEC,
            )
            if r.status_code == 200:
                body = r.json()
                web = (body.get("web") or {}).get("results") or []
                # Normalize the shape we care about
                out = []
                for item in web[:count]:
                    out.append({
                        "title": item.get("title"),
                        "url": item.get("url"),
                        "description": item.get("description"),
                        "age": item.get("age"),
                    })
                return out
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = repr(e)
        if attempt < RETRIES:
            time.sleep(BACKOFF[attempt])
    return []


def estimate_cost(searches: int = 1) -> float:
    """Brave Search Basic tier: ~$0.0006 per query. Conservative."""
    return round(searches * 0.0006, 4)
