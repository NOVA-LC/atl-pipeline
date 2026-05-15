"""Existing-website scraper — pulls palette, services, reviews, photos, fonts,
and copy samples from a prospect's current website if they have one.

Pure HTTP + BeautifulSoup. No API spend. No JS rendering (Puppeteer is reserved
for Step 5 technical-gates use only — for research we trust the server-rendered
HTML).

Per SPEC §8: timeout 30s, 1 retry, fallback = return None.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

TIMEOUT_SEC = 30
RETRIES = 1
USER_AGENT = (
    "Mozilla/5.0 (compatible; CloseAloneResearchBot/0.1; +https://gonenova.com/bots)"
)


def _fetch(url: str) -> str | None:
    """Single GET with retry. Returns HTML string or None."""
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                timeout=TIMEOUT_SEC,
                allow_redirects=True,
            )
            if r.status_code == 200 and "text/html" in r.headers.get("Content-Type", ""):
                return r.text
        except (requests.Timeout, requests.ConnectionError):
            pass
    return None


def scrape(url: str) -> dict[str, Any] | None:
    """Top-level scrape — fetches the page + parses out research-relevant signals.

    Returns dict with: {palette[], services[], reviews[], photos[], fonts[],
                        copy_samples[], meta_description, og_image, source_url}
    or None if fetch fails.
    """
    if not url:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        # bs4 not installed; return raw HTML wrapper so researcher can still operate
        html = _fetch(url)
        if not html:
            return None
        return {"source_url": url, "raw_html": html, "_warning": "bs4 not installed; only raw_html returned"}

    html = _fetch(url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # ── meta ──────────────────────────────────────────────────────────────────
    meta_description = ""
    md = soup.find("meta", attrs={"name": "description"})
    if md and md.get("content"):
        meta_description = md["content"].strip()
    og_image = ""
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        og_image = urljoin(url, og["content"])

    # ── fonts (best-effort from <link> + inline @font-face) ───────────────────
    fonts: list[str] = []
    for link in soup.find_all("link"):
        href = (link.get("href") or "").lower()
        if "fonts.googleapis.com" in href or "fonts.gstatic.com" in href:
            # extract family name
            m = re.search(r"family=([A-Za-z0-9+_]+)", href)
            if m:
                fonts.append(m.group(1).replace("+", " "))
    for style in soup.find_all("style"):
        for m in re.finditer(r"font-family:\s*['\"]?([A-Za-z0-9 _-]{3,40})['\"]?", style.get_text()):
            f = m.group(1).strip()
            if f.lower() not in ("inherit", "initial", "unset") and f not in fonts:
                fonts.append(f)
    fonts = fonts[:5]

    # ── photos (img src + og:image) ───────────────────────────────────────────
    photos: list[dict[str, str]] = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(url, src)
        # filter obvious icons
        if any(x in absolute.lower() for x in ("icon", "favicon", "sprite", ".svg")):
            continue
        photos.append({
            "url": absolute,
            "alt": (img.get("alt") or "").strip(),
        })
    if og_image and not any(p["url"] == og_image for p in photos):
        photos.insert(0, {"url": og_image, "alt": "og:image"})
    photos = photos[:30]

    # ── services (h2/h3 + list items in main content) ────────────────────────
    services: list[str] = []
    for h in soup.find_all(["h2", "h3"]):
        t = h.get_text(strip=True)
        if t and 4 < len(t) < 80 and not t.lower().startswith(("about", "contact", "testimonial")):
            services.append(t)
    services = list(dict.fromkeys(services))[:15]  # dedupe, cap

    # ── reviews / testimonials (best-effort) ─────────────────────────────────
    reviews: list[str] = []
    for blockquote in soup.find_all("blockquote"):
        t = blockquote.get_text(strip=True)
        if 20 < len(t) < 500:
            reviews.append(t)
    for el in soup.select("[class*='testimonial'], [class*='review'], [class*='quote']"):
        t = el.get_text(strip=True)
        if 20 < len(t) < 500 and t not in reviews:
            reviews.append(t)
    reviews = reviews[:10]

    # ── copy samples (longest paragraph blocks — for voice extraction) ───────
    copy_samples: list[str] = []
    for p in soup.find_all("p"):
        t = p.get_text(strip=True)
        if 60 < len(t) < 400:
            copy_samples.append(t)
    copy_samples = copy_samples[:8]

    # ── palette (extract inline color values from <style> blocks) ────────────
    palette: list[str] = []
    hex_re = re.compile(r"#([0-9a-fA-F]{6})\b")
    for style in soup.find_all("style"):
        palette.extend(m.group(0).lower() for m in hex_re.finditer(style.get_text()))
    style_attrs = soup.find_all(attrs={"style": True})
    for el in style_attrs:
        palette.extend(m.group(0).lower() for m in hex_re.finditer(el["style"]))
    # most-frequent first, dedupe, top 5
    counter = Counter(palette)
    palette = [c for c, _ in counter.most_common(8)]
    # filter near-white / near-black extremes (probably not "brand" colors)
    def _is_extreme(hex_color: str) -> bool:
        h = hex_color.lstrip("#")
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        avg = (r + g + b) / 3
        return avg < 20 or avg > 235
    palette = [c for c in palette if not _is_extreme(c)][:5]

    return {
        "source_url": url,
        "meta_description": meta_description,
        "og_image": og_image,
        "fonts": fonts,
        "photos": photos,
        "services": services,
        "reviews": reviews,
        "copy_samples": copy_samples,
        "palette": palette,
    }
