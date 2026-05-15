"""Researcher — builds research_brief.json from GBP + existing website only.

Hard rules per SPEC §3:
- No FB / IG / LinkedIn scraping.
- Every fact recorded MUST have a source URL or be null.
- Cost ceiling: $1.00 per lead.
- Timeout: 30s per tool, 2 retries.
- Returns {"build_unfit": true} if no GBP AND no existing website.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

from build_agent.tools import outscraper, brave, existing_site_scraper

# Anthropic import is optional at module load — error handled per call
try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # type: ignore

MODEL = os.environ.get("RESEARCHER_MODEL", "claude-haiku-4-5-20251001")
COST_CAP_USD = 1.00
ANTHROPIC_INPUT_COST_PER_M = 1.00   # haiku 4.5 input ≈ $1/M tokens
ANTHROPIC_OUTPUT_COST_PER_M = 5.00  # output ≈ $5/M tokens


# ─── verticals + winning conversion patterns (sourced from internal playbook) ──
INDUSTRY_VERTICALS = {
    "plumbing":         {"keywords": ["plumber", "plumbing", "drain", "leak", "septic", "water heater"]},
    "hvac":             {"keywords": ["hvac", "heating", "cooling", "air conditioning", "ac repair", "furnace"]},
    "landscaping":      {"keywords": ["landscap", "lawn", "mulch", "pine straw", "sod", "garden"]},
    "roofing":          {"keywords": ["roof", "gutter", "shingle", "storm damage"]},
    "auto":             {"keywords": ["auto repair", "mechanic", "tire", "collision", "transmission"]},
    "electrical":       {"keywords": ["electrician", "electrical", "wiring", "panel", "ev charger"]},
    "painting":         {"keywords": ["painting", "painter", "house painting", "pressure wash"]},
    "cleaning":         {"keywords": ["cleaning", "maid", "janitorial", "carpet"]},
    "pest_control":     {"keywords": ["pest control", "exterminator", "termite", "rodent"]},
    "tree_service":     {"keywords": ["tree service", "tree removal", "stump", "arborist"]},
    "mobile_mechanic":  {"keywords": ["mobile mechanic"]},
}

CONVERSION_PATTERNS = {
    "plumbing": [
        "Sticky emergency phone CTA top-right",
        "Flat-rate-pricing badge near the hero",
        "Owner-answers-the-phone trust signal",
        "Service-area page for local SEO",
    ],
    "hvac": [
        "Financing badge above the fold",
        "Same-day repair availability",
        "License # and bonded/insured statement in footer",
        "Comfort-club / membership program CTA",
    ],
    "landscaping": [
        "Before/after gallery is the primary social proof",
        "Service area map (zip codes covered)",
        "Seasonal package CTA (spring cleanup, fall leaf removal)",
        "Property-type filter (residential / commercial)",
    ],
    "roofing": [
        "Before/after of past jobs in the hero or just below",
        "Insurance-claim assistance trust signal",
        "Warranty length called out (e.g. 25-year)",
        "Storm-damage emergency CTA",
    ],
    "auto": [
        "Services list with pricing transparency",
        "Customer reviews near the hero",
        "Hours of operation prominent (mobile vs walk-in)",
        "ASE-certified or AAA-approved trust badges",
    ],
    "electrical": [
        "Licensed/bonded statement in footer",
        "24/7 emergency CTA",
        "EV charger / smart home upsell opportunity",
        "Before/after photos of panel upgrades",
    ],
    "painting": [
        "Before/after gallery is the hero",
        "Free quote CTA in 3+ places",
        "Color consultation mention",
        "Interior / exterior split",
    ],
    "cleaning": [
        "Online booking flow above the fold",
        "Recurring service discount badge",
        "Background-check + bonded language",
        "Eco-friendly / pet-safe trust signals",
    ],
    "pest_control": [
        "Pest-by-type pages for local SEO",
        "Quarterly service plan CTA",
        "Pet-safe + child-safe treatments callout",
        "Free inspection offer",
    ],
    "tree_service": [
        "Emergency storm response CTA",
        "Insured + bonded callout (high-liability work)",
        "Before/after of removals + stump grinding",
        "Free estimate offer",
    ],
    "mobile_mechanic": [
        "Service area map",
        "Same-day on-site repair CTA",
        "Pricing transparency for common services",
        "Insurance + warranty trust signals",
    ],
    "general_trade": [
        "Phone CTA in the top-right of every page",
        "Real reviews with first names + locations",
        "Service area list for local SEO",
    ],
}


def _classify_vertical(categories: list[str] | None, business_name: str = "", services: list[str] | None = None) -> str:
    """Pick a vertical from {INDUSTRY_VERTICALS keys} based on GBP categories + services."""
    haystack = " ".join((categories or []) + (services or []) + [business_name]).lower()
    for vertical, info in INDUSTRY_VERTICALS.items():
        for kw in info["keywords"]:
            if kw in haystack:
                return vertical
    return "general_trade"


# ─── public API ──────────────────────────────────────────────────────────────
def research(lead: dict[str, Any]) -> dict[str, Any]:
    """Top-level entry. Returns research_brief dict or {"build_unfit": true, ...}.

    Lead schema: {lead_id, business_name, city, phone, state?, existing_url?}
    """
    started = time.time()
    cost_estimate = 0.0
    tools_used: list[str] = []
    fallbacks: list[str] = []

    business_name = lead.get("business_name") or ""
    city = lead.get("city") or "Atlanta"
    state = lead.get("state") or "GA"
    phone = lead.get("phone") or ""
    existing_url_hint = lead.get("existing_url") or ""
    # Option C: accept pre-existing Outscraper data so we never re-pay for the
    # same place. Sources:
    #   1. lead["gbp"] / lead["raw_outscraper"] passed from /build callers
    #   2. parsed_outscraper.json on disk (if the dialer dropped it there)
    #   3. fall back to live Outscraper API
    cached_gbp: dict[str, Any] | None = None
    for cache_key in ("gbp", "raw_outscraper", "outscraper"):
        v = lead.get(cache_key)
        if isinstance(v, dict) and v:
            cached_gbp = v
            break
        if isinstance(v, str) and v.strip():
            try:
                cached_gbp = json.loads(v)
                break
            except Exception:
                pass

    # ── Step A: GBP lookup ──────────────────────────────────────────────────
    gbp: dict[str, Any] | None = None
    if cached_gbp:
        gbp = cached_gbp
        tools_used.append("cached_gbp (no Outscraper call)")
    elif business_name:
        gbp = outscraper.fetch_gbp(business_name, city, state)
        if gbp:
            tools_used.append("outscraper.fetch_gbp")
            cost_estimate += outscraper.estimate_cost(places_fetched=1)
        else:
            fallbacks.append("outscraper.fetch_gbp returned None")

    # ── Step B: existing site URL ──────────────────────────────────────────
    site_url = ""
    if gbp:
        site_url = (gbp.get("site") or gbp.get("website") or "").strip()
    if not site_url and existing_url_hint:
        site_url = existing_url_hint

    # ── Step C: existing site scrape ────────────────────────────────────────
    existing = None
    if site_url:
        existing = existing_site_scraper.scrape(site_url)
        if existing:
            tools_used.append("existing_site_scraper.scrape")
        else:
            fallbacks.append(f"existing_site_scraper.scrape({site_url}) returned None")

    # ── Pre-filter: build_unfit if no GBP AND no existing site ─────────────
    if not gbp and not existing:
        return {
            "build_unfit": True,
            "business": {"name": business_name, "phone": phone, "rating": None, "review_count": None},
            "owner": {},
            "brand": {},
            "industry_context": {"vertical": "general_trade"},
            "_meta": {
                "researched_at": _now_iso(),
                "research_cost_usd": round(cost_estimate, 4),
                "tools_used": tools_used,
                "fallbacks": fallbacks + ["build_unfit: no GBP and no existing website"],
            },
        }

    # ── Step D: GBP photos ──────────────────────────────────────────────────
    photo_urls: list[str] = []
    # First check the cached_gbp for embedded photos (raw_outscraper from pipeline.db)
    if cached_gbp:
        embedded = cached_gbp.get("photos") or cached_gbp.get("photos_sample") or []
        if isinstance(embedded, list):
            for ph in embedded[:15]:
                if isinstance(ph, str):
                    photo_urls.append(ph)
                elif isinstance(ph, dict):
                    url = ph.get("photo_url") or ph.get("original_photo_url") or ph.get("photo") or ph.get("url")
                    if url:
                        photo_urls.append(url)
        if photo_urls:
            tools_used.append("cached_gbp_photos (no Outscraper call)")
    # Fall back to live Outscraper fetch only if cached payload had none
    if not photo_urls:
        place_id = (gbp or {}).get("place_id") or (gbp or {}).get("google_id")
        if place_id:
            photo_urls = outscraper.fetch_gbp_photos(place_id, max_photos=15)
            if photo_urls:
                tools_used.append("outscraper.fetch_gbp_photos")
                cost_estimate += outscraper.estimate_cost(photos_fetched=len(photo_urls))

    # ── Step E: classify vertical ──────────────────────────────────────────
    gbp_categories = []
    if gbp:
        gbp_categories = gbp.get("categories") or gbp.get("subtypes") or []
        if isinstance(gbp_categories, str):
            gbp_categories = [gbp_categories]
    services = (existing or {}).get("services") or []
    vertical = _classify_vertical(gbp_categories, business_name, services)
    winning_patterns = CONVERSION_PATTERNS.get(vertical, CONVERSION_PATTERNS["general_trade"])

    # ── Step F: assemble research_brief deterministically ──────────────────
    # We do NOT call Anthropic here — every field is sourced or null, and the LLM
    # would only invite hallucination. The schema + tool outputs cover us.
    brief = _assemble_brief(
        lead=lead,
        gbp=gbp,
        existing=existing,
        photo_urls=photo_urls,
        vertical=vertical,
        winning_patterns=winning_patterns,
        site_url=site_url,
    )
    brief["_meta"] = {
        "researched_at": _now_iso(),
        "research_cost_usd": round(cost_estimate, 4),
        "tools_used": tools_used,
        "fallbacks": fallbacks,
        "duration_sec": round(time.time() - started, 2),
    }
    return brief


# ─── helpers ─────────────────────────────────────────────────────────────────
def _assemble_brief(
    *,
    lead: dict[str, Any],
    gbp: dict[str, Any] | None,
    existing: dict[str, Any] | None,
    photo_urls: list[str],
    vertical: str,
    winning_patterns: list[str],
    site_url: str,
) -> dict[str, Any]:
    """Deterministic assembly. Every claim has a source or is null."""
    business_name = lead.get("business_name") or (gbp or {}).get("name") or ""
    phone = lead.get("phone") or (gbp or {}).get("phone") or ""
    rating = (gbp or {}).get("rating")
    reviews_count = (gbp or {}).get("reviews") or (gbp or {}).get("reviews_count")

    # ── business object ──
    gbp_url = ""
    if gbp:
        gbp_url = (gbp.get("location_link") or gbp.get("link") or "") or _gbp_link_from_place_id(gbp.get("place_id"))

    # Reviews: only real ones from GBP, verbatim, sourced
    real_reviews: list[dict[str, Any]] = []
    for rev in (gbp or {}).get("reviews_data") or []:
        text = (rev.get("review_text") or "").strip()
        if not text or len(text) < 20:
            continue
        real_reviews.append({
            "text": text,
            "author": (rev.get("author_title") or "").split(" ")[0] or None,
            "rating": rev.get("review_rating"),
            "source": gbp_url or "GBP",
        })
        if len(real_reviews) >= 5:
            break

    # Real photos: prefer GBP photo_urls, fall back to existing site photos
    real_photos: list[dict[str, Any]] = []
    for url in photo_urls[:10]:
        real_photos.append({
            "url": url,
            "type": "other",  # type inference is Step 3's job
            "alt": None,
            "source": gbp_url or "GBP",
        })
    if existing:
        for ph in (existing.get("photos") or [])[:10]:
            if ph["url"] not in {p["url"] for p in real_photos}:
                real_photos.append({
                    "url": ph["url"],
                    "type": "other",
                    "alt": ph.get("alt") or None,
                    "source": existing["source_url"],
                })

    # Service area: GBP's working_in / address_city list, else existing site
    service_area: list[str] = []
    if gbp:
        for f in ("working_in", "service_area", "subtypes"):
            v = gbp.get(f)
            if isinstance(v, list):
                service_area.extend(str(x) for x in v if x)

    # Services list
    services_list: list[str] = []
    services_source = None
    if gbp:
        cats = gbp.get("categories") or gbp.get("subtypes") or []
        if isinstance(cats, list):
            services_list.extend(str(c) for c in cats if c)
            services_source = gbp_url or "GBP"
    if not services_list and existing and existing.get("services"):
        services_list = existing["services"][:10]
        services_source = existing["source_url"]

    # Owner voice samples — from existing site copy or GBP review responses
    voice_samples: list[dict[str, Any]] = []
    if existing:
        for sample in (existing.get("copy_samples") or [])[:3]:
            voice_samples.append({"text": sample, "source": existing["source_url"]})

    # Brand palette
    palette = (existing or {}).get("palette") or []
    palette_source = existing["source_url"] if existing and palette else None
    primary = palette[0] if len(palette) >= 1 else None
    secondary = palette[1] if len(palette) >= 2 else None
    accent = palette[2] if len(palette) >= 3 else None

    return {
        "build_unfit": False,
        "business": {
            "name": business_name,
            "founded_year": None,
            "source_founded": None,
            "services": services_list[:12],
            "source_services": services_source,
            "service_area": list(dict.fromkeys(service_area))[:12],
            "source_service_area": gbp_url or None,
            "license_number": None,
            "source_license": None,
            "real_reviews": real_reviews,
            "real_photos": real_photos,
            "phone": phone,
            "email": (gbp or {}).get("email_1") or None,
            "address": (gbp or {}).get("full_address") or None,
            "rating": rating,
            "review_count": reviews_count,
        },
        "owner": {
            "name": (gbp or {}).get("owner_title") or None,
            "source_name": gbp_url or None,
            "voice_samples": voice_samples,
        },
        "brand": {
            "primary_color": primary,
            "secondary_color": secondary,
            "accent_color": accent,
            "palette_source": palette_source,
            "logo_url": (existing or {}).get("og_image") or (gbp or {}).get("logo") or None,
            "source_logo": (existing or {}).get("source_url") or gbp_url or None,
            "existing_site_url": site_url or None,
            "fonts_in_use": (existing or {}).get("fonts") or [],
        },
        "industry_context": {
            "vertical": vertical,
            "buyer_persona": None,
            "winning_conversion_patterns": winning_patterns,
        },
    }


def _gbp_link_from_place_id(place_id: str | None) -> str:
    if not place_id:
        return ""
    return f"https://www.google.com/maps/place/?q=place_id:{place_id}"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
