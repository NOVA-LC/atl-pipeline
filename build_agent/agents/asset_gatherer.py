"""Asset gatherer — pulls real prospect images + extracts brand palette.

Hard rules per SPEC §3:
- Real-asset ratio must be ≥ 60% (track per file in assets_manifest.json).
- FLUX (Replicate) only as last resort for slots with no real asset.
- Cost ceiling: $0.50 per build.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from build_agent.tools import palette as palette_tool
from build_agent.tools import image_compress, flux

USER_AGENT = "Mozilla/5.0 (compatible; CloseAloneAssetBot/0.1; +https://gonenova.com/bots)"
DOWNLOAD_TIMEOUT_SEC = 25
MAX_REAL_ASSETS = 12
MAX_FLUX_ASSETS = 3
COST_CAP_USD = 0.50

# Slots a template typically uses. The gatherer tries to fill these.
ASSET_SLOTS = ["hero", "team", "process_detail", "behind_the_work", "neighborhood", "logo"]


def gather(research_brief: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    """Downloads real assets from the research brief + extracts brand palette.

    Writes images to out_dir/. Returns assets_manifest dict.

    Manifest shape:
    {
      "out_dir": "<abs path>",
      "assets": [
        {"slot": "hero", "filename": "...", "origin": "prospect", "source_url": "...",
         "size_bytes": ..., "width": ..., "height": ..., "alt": ...},
        ...
      ],
      "palette": {
        "primary": "#hex", "secondary": "#hex", "accent": "#hex",
        "source": "existing_site_css | logo_extract | van_photo | industry_fallback",
        "samples": ["#hex", ...]
      },
      "real_asset_ratio": 0.83,
      "cost_usd": 0.0,
      "fallbacks": ["..."]
    }
    """
    started = time.time()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "out_dir": str(out_dir),
        "assets": [],
        "palette": {},
        "real_asset_ratio": 0.0,
        "cost_usd": 0.0,
        "fallbacks": [],
        "started_at": _iso_now(),
    }

    if research_brief.get("build_unfit"):
        manifest["fallbacks"].append("build_unfit=True; no assets to gather")
        return manifest

    business = research_brief.get("business") or {}
    brand = research_brief.get("brand") or {}
    industry = (research_brief.get("industry_context") or {}).get("vertical") or "general_trade"

    # ── Step 1: download real photos from the research brief ────────────────
    photos = business.get("real_photos") or []
    real_count = 0
    for ph in photos[:MAX_REAL_ASSETS]:
        if manifest["cost_usd"] >= COST_CAP_USD:
            manifest["fallbacks"].append("cost cap reached during real-asset download")
            break
        url = ph.get("url")
        src = ph.get("source")
        if not url:
            continue
        filename = _slug_for_url(url) + ".jpg"
        out_path = out_dir / filename
        ok = _download_image(url, out_path)
        if not ok:
            manifest["fallbacks"].append(f"download failed: {url}")
            continue
        # Compress to target
        final_path = image_compress.compress(out_path, target_kb=200)
        dims = image_compress.get_dimensions(final_path) or (0, 0)
        manifest["assets"].append({
            "slot": _guess_slot(ph),
            "filename": final_path.name,
            "origin": "prospect",
            "source_url": src or url,
            "size_bytes": final_path.stat().st_size if final_path.exists() else 0,
            "width": dims[0],
            "height": dims[1],
            "alt": ph.get("alt"),
        })
        real_count += 1

    # ── Step 2: extract brand palette ───────────────────────────────────────
    extracted_palette: list[str] = []
    palette_source = None
    # Prefer existing-site CSS palette (from the research brief)
    brief_colors = [
        brand.get("primary_color"),
        brand.get("secondary_color"),
        brand.get("accent_color"),
    ]
    brief_colors = [c for c in brief_colors if c]
    if brief_colors:
        extracted_palette = palette_tool.merge_palettes(brief_colors)
        palette_source = brand.get("palette_source") or "existing_site_css"

    # If still short, try extracting from the logo image (if we downloaded one)
    if len(extracted_palette) < 3:
        logo_url = brand.get("logo_url")
        if logo_url:
            logo_path = out_dir / "_logo_for_palette.jpg"
            if _download_image(logo_url, logo_path):
                logo_palette = palette_tool.extract(logo_path, k=5)
                extracted_palette = palette_tool.merge_palettes(extracted_palette, logo_palette)
                if logo_palette and not palette_source:
                    palette_source = "logo_extract"

    # If still short, try the first downloaded prospect photo (van / shop)
    if len(extracted_palette) < 3 and real_count > 0:
        first_asset = next((a for a in manifest["assets"] if a["origin"] == "prospect"), None)
        if first_asset:
            photo_path = out_dir / first_asset["filename"]
            photo_palette = palette_tool.extract(photo_path, k=5)
            extracted_palette = palette_tool.merge_palettes(extracted_palette, photo_palette)
            if photo_palette and not palette_source:
                palette_source = "prospect_photo_extract"

    # Final fallback: industry default
    if len(extracted_palette) < 3:
        fallback = palette_tool.industry_fallback(industry)
        extracted_palette = palette_tool.merge_palettes(extracted_palette, fallback)
        if not palette_source:
            palette_source = "industry_fallback"
        manifest["fallbacks"].append(f"palette: filled gaps from industry_fallback ({industry})")

    extracted_palette = extracted_palette[:5]
    manifest["palette"] = {
        "primary":   extracted_palette[0] if len(extracted_palette) >= 1 else None,
        "secondary": extracted_palette[1] if len(extracted_palette) >= 2 else None,
        "accent":    extracted_palette[2] if len(extracted_palette) >= 3 else None,
        "source":    palette_source,
        "samples":   extracted_palette,
    }

    # ── Step 3: FLUX fallback for missing critical slots ────────────────────
    # We do NOT call FLUX for every empty slot — only for `hero` and `neighborhood`
    # which are visually load-bearing. The builder can choose to render slots
    # with no image as type-only sections.
    covered_slots = {a["slot"] for a in manifest["assets"]}
    flux_target_slots = ["hero"] if "hero" not in covered_slots else []
    flux_count = 0
    for slot in flux_target_slots[:MAX_FLUX_ASSETS]:
        if manifest["cost_usd"] + flux.estimate_cost(1) > COST_CAP_USD:
            manifest["fallbacks"].append(f"flux skipped for {slot}: cost cap")
            break
        prompt = _flux_prompt_for_slot(slot, research_brief)
        if not prompt:
            continue
        out_path = out_dir / f"_flux_{slot}.jpg"
        result = flux.generate(prompt, slot, out_path, aspect_ratio="16:9")
        if result:
            final_path = image_compress.compress(result, target_kb=250)
            dims = image_compress.get_dimensions(final_path) or (0, 0)
            manifest["assets"].append({
                "slot": slot,
                "filename": final_path.name,
                "origin": "flux",
                "source_url": "flux-schnell",
                "size_bytes": final_path.stat().st_size if final_path.exists() else 0,
                "width": dims[0],
                "height": dims[1],
                "alt": None,
                "flux_prompt": prompt,
            })
            manifest["cost_usd"] += flux.estimate_cost(1)
            flux_count += 1
        else:
            manifest["fallbacks"].append(f"flux failed for slot {slot}")

    # ── Final stats ─────────────────────────────────────────────────────────
    total = len(manifest["assets"])
    manifest["real_asset_ratio"] = round(real_count / total, 3) if total > 0 else 0.0
    manifest["cost_usd"] = round(manifest["cost_usd"], 4)
    manifest["finished_at"] = _iso_now()
    manifest["duration_sec"] = round(time.time() - started, 2)

    # Persist manifest to disk
    (out_dir / "assets_manifest.json").write_text(json.dumps(manifest, indent=2, default=str))
    return manifest


# ─── helpers ────────────────────────────────────────────────────────────────
def _download_image(url: str, out_path: Path) -> bool:
    """Download an image to disk. Returns True on success."""
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=DOWNLOAD_TIMEOUT_SEC,
            stream=True,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return False
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        # sanity check size
        if out_path.stat().st_size < 1024:
            out_path.unlink(missing_ok=True)
            return False
        return True
    except (requests.Timeout, requests.ConnectionError, OSError):
        return False


def _slug_for_url(url: str) -> str:
    """Stable filename from a URL — first 12 chars of sha1 + original basename if present."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    name = urlparse(url).path.rsplit("/", 1)[-1]
    base = "".join(c for c in name if c.isalnum() or c in "-_")[:20]
    return f"{h}_{base}" if base else h


def _guess_slot(photo: dict[str, Any]) -> str:
    """Heuristic slot assignment from photo metadata."""
    t = (photo.get("type") or "").lower()
    alt = (photo.get("alt") or "").lower()
    haystack = t + " " + alt
    if "team" in haystack or "owner" in haystack or "staff" in haystack:
        return "team"
    if "truck" in haystack or "van" in haystack:
        return "hero"
    if "before" in haystack or "after" in haystack or "job" in haystack:
        return "process_detail"
    if "exterior" in haystack or "office" in haystack or "shop" in haystack:
        return "behind_the_work"
    return "other"


def _flux_prompt_for_slot(slot: str, brief: dict[str, Any]) -> str | None:
    """Build a FLUX prompt for a specific slot from the research brief.
    Conservative — describes environment, never asks for faux text or logos."""
    biz = brief.get("business") or {}
    industry = (brief.get("industry_context") or {}).get("vertical", "general_trade")
    city = (biz.get("address") or "").split(",")[-3].strip() if biz.get("address") else "suburban Atlanta"

    if slot == "hero":
        return (
            f"Documentary photograph of a {industry.replace('_', ' ')} job site in {city}, "
            f"warm late-afternoon light, professional muted color grading, photographic, "
            f"no text, no logos, no faces, candid composition, depth of field."
        )
    if slot == "neighborhood":
        return (
            f"Atmospheric photograph of a residential street in {city} at dawn, "
            f"oak canopy, soft light through leaves, no people, no text, photographic, "
            f"35mm film aesthetic."
        )
    return None


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
