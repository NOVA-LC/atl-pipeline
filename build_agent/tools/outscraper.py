"""Outscraper API client — Google Business Profile lookups.

We use SYNC mode (async=false) for single-business research — much faster
than the bulk async flow used by atl_pipeline/scraper.py.

API: GET https://api.outscraper.cloud/maps/search-v3?query=...&async=false
Auth: X-API-KEY header
Cost: ~$0.003 per result (~10 results per dollar)

Per SPEC §8: timeout 30s, 2 retries with backoff (1s, 4s), fallback = return None.

References: atl_pipeline/scraper.py (existing async usage).
"""
from __future__ import annotations

import os
import time
from typing import Any

import requests

OUTSCRAPER_API = "https://api.outscraper.cloud"
TIMEOUT_SEC = 30
RETRIES = 2
BACKOFF = (1, 4)


def _api_key() -> str | None:
    return os.environ.get("OUTSCRAPER_API_KEY") or None


def _request_sync(params: dict[str, Any]) -> dict[str, Any] | None:
    """One HTTP call with retries. Returns parsed JSON or None on persistent failure."""
    key = _api_key()
    if not key:
        return None
    last_err = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                f"{OUTSCRAPER_API}/maps/search-v3",
                params={**params, "async": "false"},
                headers={"X-API-KEY": key},
                timeout=TIMEOUT_SEC,
            )
            if r.status_code == 200:
                return r.json()
            # 202 indicates job was switched to async despite request — treat as failure for sync path
            last_err = f"http {r.status_code}: {r.text[:200]}"
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = repr(e)
        if attempt < RETRIES:
            time.sleep(BACKOFF[attempt])
    return None


def fetch_gbp(business_name: str, city: str, state: str = "GA") -> dict[str, Any] | None:
    """Look up a single business on Google Maps. Returns a flat record dict or None.

    Record fields (from Outscraper response, filtered to what we need):
      name, full_address, city, state, postal_code, country, phone, site,
      rating, reviews, photos_count, google_id, place_id, latitude, longitude,
      type, subtypes, categories, business_status, working_hours, owner_id,
      logo, photo (cover image url)
    """
    query = f"{business_name}, {city}, {state}, USA"
    body = _request_sync({"query": query, "limit": 1, "language": "en", "region": "US"})
    if not body:
        return None
    # Outscraper sync returns {"data": [[place, ...]]} — first query's first result
    data = body.get("data") or []
    if not data or not data[0]:
        return None
    first = data[0]
    if not isinstance(first, list):
        return None
    if not first:
        return None
    return first[0]


def fetch_gbp_photos(place_id: str, max_photos: int = 20) -> list[str]:
    """Fetch GBP photo URLs for a specific place_id.

    Outscraper endpoint: /maps/photos-v3
    Returns a list of photo URL strings (the response shape is a list of dicts;
    we extract the highest-resolution URL from each).
    """
    key = _api_key()
    if not key:
        return []
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                f"{OUTSCRAPER_API}/maps/photos-v3",
                params={"query": place_id, "limit": max_photos, "async": "false"},
                headers={"X-API-KEY": key},
                timeout=TIMEOUT_SEC,
            )
            if r.status_code == 200:
                data = r.json().get("data") or []
                if not data:
                    return []
                first = data[0] if isinstance(data[0], list) else data
                urls = []
                for p in first[:max_photos]:
                    if isinstance(p, dict):
                        url = p.get("photo_url") or p.get("original_photo_url") or p.get("photo")
                        if url:
                            urls.append(url)
                    elif isinstance(p, str):
                        urls.append(p)
                return urls
        except (requests.Timeout, requests.ConnectionError):
            pass
        if attempt < RETRIES:
            time.sleep(BACKOFF[attempt])
    return []


def estimate_cost(places_fetched: int = 1, photos_fetched: int = 0) -> float:
    """Outscraper pricing: ~$0.003 per Maps record, ~$0.001 per Photos record.
    Conservative estimate."""
    return round(places_fetched * 0.003 + photos_fetched * 0.001, 4)
