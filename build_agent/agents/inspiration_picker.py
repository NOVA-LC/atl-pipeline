"""Inspiration picker — deterministic selection of 3-5 refs from inspiration/.

Per SPEC §6 selection rules:
- industry_fit match (priority 1)
- vibe_tag distance from the brand's signals (priority 2)
- fingerprint diversity vs the last 5 builds (palette overlap < 50%)

Pure Python, no LLM. Reads all *.meta.json files from inspiration/, scores
each candidate, returns top 3-5 ref IDs.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from math import sqrt
from pathlib import Path
from typing import Any

# Resolve corpus path: prefer env var, fall back to <repo>/inspiration
REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = Path(os.environ.get("BUILD_AGENT_CORPUS_DIR", str(REPO_ROOT / "inspiration")))


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _color_distance(a: str, b: str) -> float:
    """Simple Euclidean RGB distance, 0-441."""
    try:
        ra, ga, ba = _hex_to_rgb(a)
        rb, gb, bb = _hex_to_rgb(b)
        return sqrt((ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2)
    except Exception:
        return 441.0  # max distance


def _palette_overlap(p1: list[str], p2: list[str], threshold: float = 60.0) -> float:
    """Return fraction of p1 colors that have a close match in p2."""
    if not p1 or not p2:
        return 0.0
    matches = 0
    for c1 in p1:
        nearest = min((_color_distance(c1, c2) for c2 in p2), default=441.0)
        if nearest < threshold:
            matches += 1
    return matches / len(p1)


def _load_corpus() -> list[dict[str, Any]]:
    if not CORPUS_DIR.exists():
        return []
    refs: list[dict[str, Any]] = []
    for path in sorted(CORPUS_DIR.glob("*.meta.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data.get("id"):
            data["id"] = path.stem.replace(".meta", "")
        data["_path"] = str(path)
        refs.append(data)
    return refs


# ─── vibe tag clustering (for distance scoring) ──────────────────────────────
# Group tags by family; tags within a family are "close." Used to score
# whether a ref's vibe aligns with the brief's inferred vibe.
VIBE_FAMILIES = {
    "trade":      {"rugged-editorial", "documentary-photographic", "process-led"},
    "trust":      {"family-legacy", "quiet-confidence", "warmly-handmade", "neighborhood-local"},
    "premium":    {"premium-craftsman", "high-touch-luxury", "typographic-statement"},
    "modern":     {"modern-tech", "technical-minimal"},
    "punchy":     {"aggressive-bold", "testimonial-led"},
    "warm-soft":  {"woman-owned-warm", "warmly-handmade"},
}


def _vibe_family(tag: str) -> str | None:
    for family, tags in VIBE_FAMILIES.items():
        if tag in tags:
            return family
    return None


def _infer_brief_vibe_families(brief: dict[str, Any]) -> set[str]:
    """Best-effort: map the brief's signals to vibe-family targets.

    Heuristic — refined as we collect calibration data.
    """
    families: set[str] = set()
    vertical = (brief.get("industry_context") or {}).get("vertical") or "general_trade"
    # Trade-heavy verticals: trust + trade
    if vertical in ("plumbing", "hvac", "roofing", "electrical", "tree_service", "mobile_mechanic"):
        families.update({"trust", "trade"})
    if vertical in ("auto", "painting"):
        families.update({"trade", "punchy"})
    if vertical in ("landscaping", "tree_service"):
        families.add("trust")
    if vertical in ("cleaning", "pest_control"):
        families.add("warm-soft")

    business = brief.get("business") or {}
    # High rating + lots of reviews → trust signals work
    if business.get("rating") and business["rating"] >= 4.7:
        families.add("trust")
    # Existing site has lots of paragraph copy → leans editorial
    voice_samples = (brief.get("owner") or {}).get("voice_samples") or []
    if len(voice_samples) >= 2:
        families.update({"trade", "trust"})

    return families or {"trust", "trade"}


# ─── public API ──────────────────────────────────────────────────────────────
def pick(
    research_brief: dict[str, Any],
    recent_build_fingerprints: list[dict[str, Any]] | None = None,
    min_refs: int = 3,
    max_refs: int = 5,
) -> list[dict[str, Any]]:
    """Return 3-5 inspiration refs (full dicts) ranked by fit, with diversity enforced.

    Returns full ref dicts (not just IDs) so the builder can read `what_works`,
    `type_observations`, etc. directly without re-loading from disk.
    """
    corpus = _load_corpus()
    if not corpus:
        return []

    vertical = (research_brief.get("industry_context") or {}).get("vertical") or "general_trade"
    target_families = _infer_brief_vibe_families(research_brief)
    recent_palettes = [bp.get("palette", []) for bp in (recent_build_fingerprints or [])]

    scored: list[tuple[float, dict[str, Any]]] = []
    for ref in corpus:
        score = 0.0

        # ── industry_fit match (priority 1, weight 50) ──
        industry_fits = set(ref.get("industry_fits") or [])
        if vertical in industry_fits:
            score += 50.0
        elif vertical == "general_trade" and industry_fits:
            score += 15.0  # general fit

        # ── vibe family alignment (priority 2, weight 30 max) ──
        ref_families = {fam for tag in (ref.get("vibe_tags") or []) if (fam := _vibe_family(tag))}
        family_overlap = len(target_families & ref_families)
        score += min(family_overlap * 15.0, 30.0)

        # ── diversity vs recent builds (penalty, -25 max) ──
        ref_palette = ref.get("palette_dominant") or []
        max_overlap = max((_palette_overlap(ref_palette, rp) for rp in recent_palettes), default=0.0)
        score -= max_overlap * 25.0

        # ── micro-bonus: refs that worked well in calibration ──
        score += float(ref.get("performed_well_count", 0)) * 2.0
        score -= float(ref.get("performed_poorly_count", 0)) * 5.0

        scored.append((score, ref))

    # Sort by score desc, take top max_refs while enforcing inter-pick diversity
    scored.sort(key=lambda x: -x[0])
    picked: list[dict[str, Any]] = []
    for score, ref in scored:
        if len(picked) >= max_refs:
            break
        # Skip if this ref's palette is too similar to one already picked
        already_picked_palettes = [p.get("palette_dominant") or [] for p in picked]
        too_similar = any(
            _palette_overlap(ref.get("palette_dominant") or [], pp) >= 0.7
            for pp in already_picked_palettes
        )
        if too_similar:
            continue
        picked.append(ref)

    # If diversity enforcement starved us, pad with next-best ignoring diversity
    if len(picked) < min_refs:
        for score, ref in scored:
            if ref in picked:
                continue
            picked.append(ref)
            if len(picked) >= min_refs:
                break

    return picked


def pick_ids(
    research_brief: dict[str, Any],
    recent_build_fingerprints: list[dict[str, Any]] | None = None,
) -> list[str]:
    """Convenience: just the IDs for storing in build_jobs.inspiration_ref_ids."""
    return [r["id"] for r in pick(research_brief, recent_build_fingerprints)]
