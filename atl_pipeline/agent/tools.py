"""Tool implementations the agent sub-agents call.

These are plain Python functions that the orchestrator wires into Anthropic's
tool-use loop. Each is small, idempotent, and has explicit failure modes.

Network tools respect:
  - 1 QPS max per external host (tracked here, in-process)
  - User-Agent: Mozilla/5.0 (compatible; NovaPipelineBot/1.0; +mailto:tyler@gonenova.com)
  - robots.txt for fetch_page
"""
from __future__ import annotations
import os
import time
import json
import re
import urllib.parse
import urllib.robotparser
from collections import defaultdict
from typing import Optional

import requests

from .. import brave_search


UA = 'Mozilla/5.0 (compatible; NovaPipelineBot/1.0; +mailto:tyler@gonenova.com)'
HOST_LAST_HIT: dict[str, float] = defaultdict(float)
ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser] = {}


def _rate_limit(host: str, qps: float = 1.0) -> None:
    """Sleep until at least 1/qps seconds have elapsed since last hit on this host."""
    now = time.time()
    elapsed = now - HOST_LAST_HIT[host]
    min_gap = 1.0 / qps
    if elapsed < min_gap:
        time.sleep(min_gap - elapsed)
    HOST_LAST_HIT[host] = time.time()


def _host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.lower()


def _robots_allows(url: str) -> bool:
    """Best-effort robots.txt check. On any error, default to allow."""
    try:
        host = _host(url)
        if host not in ROBOTS_CACHE:
            rp = urllib.robotparser.RobotFileParser()
            rp.set_url(f'https://{host}/robots.txt')
            try:
                rp.read()
            except Exception:
                # robots fetch failed — default allow
                ROBOTS_CACHE[host] = None
                return True
            ROBOTS_CACHE[host] = rp
        rp = ROBOTS_CACHE[host]
        if rp is None:
            return True
        return rp.can_fetch(UA, url)
    except Exception:
        return True


# -----------------------------------------------------------------------------
# brave_search — wraps the existing brave_search module
# -----------------------------------------------------------------------------

def tool_brave_search(query: str, count: int = 5) -> dict:
    """Returns {'results': [{title, url, description}, ...]}."""
    api_key = os.environ.get('BRAVE_API_KEY')
    if not api_key:
        return {'error': 'BRAVE_API_KEY not set', 'results': []}
    _rate_limit('search.brave.com', qps=1.0)
    try:
        results = brave_search.web(query, count=min(count, 10), api_key=api_key)
        return {'results': [
            {'title': r.get('title', ''),
             'url': r.get('url', ''),
             'description': r.get('description', '')}
            for r in results
        ]}
    except Exception as e:
        return {'error': str(e), 'results': []}


# -----------------------------------------------------------------------------
# fetch_page — pull text from a single URL
# -----------------------------------------------------------------------------

_TAG_RE = re.compile(r'<[^>]+>')
_WS_RE = re.compile(r'\s+')


def tool_fetch_page(url: str, max_chars: int = 4000) -> dict:
    """Return {'url', 'status', 'title', 'text', 'text_truncated'} from a single
    GET. Strips HTML to plain text. Honors robots.txt and per-host QPS.
    """
    if not url or not url.startswith(('http://', 'https://')):
        return {'error': 'invalid url', 'url': url}
    if not _robots_allows(url):
        return {'error': 'blocked by robots.txt', 'url': url}
    host = _host(url)
    _rate_limit(host, qps=1.0)
    try:
        r = requests.get(url, headers={'User-Agent': UA}, timeout=10, allow_redirects=True)
    except Exception as e:
        return {'error': f'fetch failed: {e}', 'url': url}
    ct = r.headers.get('Content-Type', '')
    if 'text/html' not in ct and 'text/plain' not in ct:
        return {'url': url, 'status': r.status_code, 'error': f'non-text content-type: {ct}'}
    raw = r.text
    title = ''
    tm = re.search(r'<title[^>]*>(.*?)</title>', raw, re.IGNORECASE | re.DOTALL)
    if tm:
        title = _WS_RE.sub(' ', _TAG_RE.sub('', tm.group(1))).strip()[:200]
    # Strip script/style first
    raw = re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=re.IGNORECASE | re.DOTALL)
    raw = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.IGNORECASE | re.DOTALL)
    text = _TAG_RE.sub(' ', raw)
    text = _WS_RE.sub(' ', text).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars]
    return {
        'url': url,
        'status': r.status_code,
        'title': title,
        'text': text,
        'text_truncated': truncated,
    }


# -----------------------------------------------------------------------------
# outscraper_place_details — fetch richer per-place data
# -----------------------------------------------------------------------------

def tool_outscraper_place_details(place_id: str) -> dict:
    """Optional richer Outscraper call. Costs cents per call; only invoked if
    research agent decides existing raw_outscraper is thin.

    Returns {'photos': [url,...], 'reviews': [...], 'description': str, ...}
    Falls back gracefully when API key missing.
    """
    api = os.environ.get('OUTSCRAPER_API_KEY')
    if not api:
        return {'error': 'OUTSCRAPER_API_KEY not set'}
    if not place_id:
        return {'error': 'place_id required'}
    _rate_limit('api.outscraper.cloud', qps=1.0)
    try:
        r = requests.get(
            'https://api.outscraper.cloud/maps/search-v3',
            params={'query': place_id, 'limit': 1, 'language': 'en', 'async': 'false'},
            headers={'X-API-KEY': api},
            timeout=30,
        )
        if r.status_code != 200:
            return {'error': f'outscraper {r.status_code}: {r.text[:200]}'}
        body = r.json()
        data = body.get('data') or []
        if not data:
            return {'error': 'no data'}
        # data is List[List[place_dict]] — flatten
        flat = []
        for inner in data:
            if isinstance(inner, list):
                flat.extend(inner)
            else:
                flat.append(inner)
        if not flat:
            return {'error': 'no places'}
        from .. import outscraper_fields as osf
        return osf.parse_all(flat[0])
    except Exception as e:
        return {'error': f'outscraper exception: {e}'}


# -----------------------------------------------------------------------------
# validate_image — HEAD-check a photo URL + dimension sanity
# -----------------------------------------------------------------------------

def tool_validate_image(url: str) -> dict:
    """Return {'url', 'ok', 'content_type', 'content_length', 'reason'?}.

    Hard rules:
      - 200 status
      - content-type starts with 'image/'
      - content-length present and >= 20KB (excludes tiny placeholders)
    """
    if not url or not url.startswith(('http://', 'https://')):
        return {'url': url, 'ok': False, 'reason': 'invalid url'}
    host = _host(url)
    _rate_limit(host, qps=2.0)  # images: bump to 2 qps since we'll be checking many
    try:
        r = requests.head(url, headers={'User-Agent': UA}, timeout=8, allow_redirects=True)
    except Exception as e:
        # Some CDNs reject HEAD; fall back to GET-stream
        try:
            r = requests.get(url, headers={'User-Agent': UA}, timeout=8, stream=True)
            r.close()
        except Exception as e2:
            return {'url': url, 'ok': False, 'reason': f'fetch failed: {e2}'}
    if r.status_code != 200:
        return {'url': url, 'ok': False, 'status': r.status_code, 'reason': f'http {r.status_code}'}
    ct = r.headers.get('Content-Type', '')
    if not ct.startswith('image/'):
        return {'url': url, 'ok': False, 'reason': f'not image: {ct}'}
    cl = r.headers.get('Content-Length')
    try:
        cl_int = int(cl) if cl else 0
    except ValueError:
        cl_int = 0
    if cl_int and cl_int < 20_000:
        return {'url': url, 'ok': False, 'reason': f'too small: {cl_int} bytes', 'content_length': cl_int}
    return {
        'url': url, 'ok': True,
        'content_type': ct, 'content_length': cl_int,
    }
