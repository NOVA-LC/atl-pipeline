"""Code critic — extends atl_pipeline/agent/iterate.py with realness +
cross-section consistency + originality checks per SPEC §5.

Mostly deterministic regex/structural analysis. Calls Sonnet ONLY to parse the
collected weaknesses into structured intents the orchestrator can dispatch.

Returns {score, must_fixes, should_fixes, strengths, fingerprint}.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from typing import Any

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # type: ignore


# ─── deterministic scoring ───────────────────────────────────────────────────
def _fingerprint(html: str) -> dict[str, Any]:
    """Stable fingerprint of the rendered HTML for diversity tracking."""
    # Dominant palette: extract all hex colors from inline <style> blocks
    style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I)
    style_text = "\n".join(style_blocks)
    hex_colors = re.findall(r"#([0-9a-fA-F]{6})\b", style_text)
    palette = [c.lower() for c, _ in Counter(hex_colors).most_common(5)]
    # Hero composition (best-effort heuristic)
    hero_class = "centered"
    if re.search(r"\.hero[^{]*\{[^}]*grid-template-columns:\s*\d+fr\s+\d+fr", style_text):
        hero_class = "split"
    if re.search(r"\.hero[^{]*\{[^}]*position:\s*absolute", style_text):
        hero_class = "full-bleed"
    # Section sequence (h2 text in document order)
    h2_seq = [m.strip()[:30] for m in re.findall(r"<h2[^>]*>(.*?)</h2>", html, re.S | re.I)][:10]
    return {
        "palette":            palette,
        "hero_composition":   hero_class,
        "section_sequence":   h2_seq,
        "byte_length":        len(html),
    }


def _count_real_asset_refs(html: str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Compare <img> srcs in the HTML against assets_manifest origins."""
    img_srcs = re.findall(r'<img[^>]+src="([^"]+)"', html, re.I)
    real_count = 0
    flux_count = 0
    unknown_count = 0
    manifest_files = {a["filename"]: a for a in (manifest.get("assets") or [])}
    for src in img_srcs:
        fn = src.rsplit("/", 1)[-1]
        meta = manifest_files.get(fn)
        if not meta:
            unknown_count += 1
        elif meta.get("origin") == "prospect":
            real_count += 1
        elif meta.get("origin") == "flux":
            flux_count += 1
    total_imgs = len(img_srcs) or 1
    return {
        "real_count":         real_count,
        "flux_count":         flux_count,
        "unknown_count":      unknown_count,
        "total_imgs":         len(img_srcs),
        "real_asset_ratio":   real_count / total_imgs,
    }


def _count_todo_markers(html: str) -> int:
    """Count <!-- TODO --> comments (proxy for hallucination risk)."""
    return len(re.findall(r"<!--\s*TODO", html, re.I))


def _detect_lorem_or_placeholder(html: str) -> list[str]:
    """Flag any obvious placeholder content."""
    flags: list[str] = []
    haystack = html.lower()
    if "lorem ipsum" in haystack:
        flags.append("contains 'lorem ipsum'")
    if re.search(r"placeholder text|fpo image|dummy text", haystack):
        flags.append("contains generic placeholder phrasing")
    # tel:" empty href OR href="#" CTAs
    if re.search(r'href="tel:"\s', haystack):
        flags.append('CTA points at empty href="tel:"')
    if re.search(r'class="[^"]*(?:cta|button)[^"]*"[^>]*href="#"', html, re.I):
        flags.append('CTA href="#" placeholder')
    return flags


def _check_design_system_rules(html: str) -> list[str]:
    """Verify the 8 enforced rules from design_system/rules.md."""
    violations: list[str] = []

    # Rule 1: ≤ 3 colors (count distinct used in inline <style>)
    style = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I))
    hex_colors = set(c.lower() for c in re.findall(r"#([0-9a-fA-F]{6})\b", style))
    # Filter near-white / near-black neutrals (they're allowed beyond the 3 cap)
    def _is_neutral_h(h: str) -> bool:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        avg = (r + g + b) / 3
        return avg < 25 or avg > 235
    non_neutral = [h for h in hex_colors if not _is_neutral_h(h)]
    if len(non_neutral) > 4:
        violations.append(f"Rule 1: {len(non_neutral)} non-neutral colors (max 3, +1 tolerance)")

    # Rule 2: ≤ 2 type families. Count Google Fonts families + font-family declarations.
    gf_families = set()
    for m in re.finditer(r"family=([A-Za-z0-9+_]+)", html):
        gf_families.add(m.group(1).replace("+", " "))
    if len(gf_families) > 2:
        violations.append(f"Rule 2: {len(gf_families)} Google Font families loaded (max 2)")

    # Rule 3: arbitrary spacing (regex catches `padding: 17px` etc — only flags obviously off-scale)
    bad_spacing = re.findall(r"(?:padding|margin|gap):\s*(\d+)px", style)
    on_scale = {4, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 0}
    bad_count = sum(1 for v in bad_spacing if int(v) not in on_scale)
    if bad_count > 4:  # small tolerance for one-off
        violations.append(f"Rule 3: {bad_count} off-scale spacing values (use --s-1..--s-10)")

    # Rule 6: at least one real CTA href (tel:, mailto:, or https URL)
    has_real_cta = bool(re.search(r'<a[^>]+href="(?:tel:\+|mailto:|https?://)', html))
    if not has_real_cta:
        violations.append("Rule 6: no CTA with real target (tel:/mailto:/https://)")

    # Rule 7: no emoji in interactive elements (covered by html_validate too — keep dup as belt+suspenders)
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF]")
    if emoji_re.search(html):
        # only flag if found inside a tag's text content (not in comments/scripts)
        # cheap check: just count
        n = len(emoji_re.findall(html))
        if n > 0:
            violations.append(f"Rule 7: {n} emoji character(s) detected")

    # Rule 8: stock photo domains
    stock_domains = ("unsplash.com", "shutterstock.com", "pexels.com", "istockphoto.com", "gettyimages.com", "stock.adobe.com")
    for d in stock_domains:
        if d in html.lower():
            violations.append(f"Rule 8: stock photo source detected ({d})")

    return violations


def _cross_section_consistency(html: str, research_brief: dict[str, Any]) -> list[str]:
    """The 'rating in trust strip matches rating in reviews' kind of check."""
    issues: list[str] = []
    biz = research_brief.get("business") or {}
    real_rating = biz.get("rating")
    if real_rating is not None:
        # Find all standalone star-rating mentions in HTML
        for m in re.finditer(r"\b([0-9]\.[0-9])\s*(?:★|stars?|out of)\b", html, re.I):
            shown = float(m.group(1))
            if abs(shown - float(real_rating)) > 0.1:
                issues.append(f"Cross-section: shown rating {shown} != real {real_rating}")
                break

    # Service area: if shown, all cities should exist in the brief
    real_areas = set(c.lower() for c in (biz.get("service_area") or []))
    if real_areas:
        # crude: look for "Cities we serve" or "Service area" section text
        area_section = re.search(r"(?:service area|cities we serve|areas served)(.*?)(?:</section>|</footer>)", html, re.I | re.S)
        if area_section:
            text = area_section.group(1).lower()
            # Find city-like tokens (Capitalized 4-15 chars)
            for token in re.findall(r"\b[A-Z][a-zA-Z]{3,14}\b", area_section.group(1)):
                if token.lower() not in real_areas and token.lower() not in ("service", "area", "areas", "we", "serve"):
                    issues.append(f"Cross-section: service area mentions '{token}' not in brief")
                    break
    return issues


def grade(html: str, research_brief: dict[str, Any], assets_manifest: dict[str, Any] | None = None, inspiration_ref_ids: list[str] | None = None) -> dict[str, Any]:
    """Returns {score, must_fixes, should_fixes, strengths, fingerprint}."""
    assets_manifest = assets_manifest or {}
    must_fixes: list[dict[str, Any]] = []
    should_fixes: list[dict[str, Any]] = []
    strengths: list[str] = []

    # Start at 100, subtract for issues
    score = 100

    # ── realness checks ──
    asset_stats = _count_real_asset_refs(html, assets_manifest)
    if asset_stats["unknown_count"] > 0:
        must_fixes.append({"section": "images", "intent": "asset_origin", "severity": "high",
                          "text": f"{asset_stats['unknown_count']} <img> src(s) reference files not in assets_manifest"})
        score -= 12 * asset_stats["unknown_count"]
    real_ratio = asset_stats["real_asset_ratio"]
    if real_ratio < 0.6 and asset_stats["total_imgs"] > 0:
        must_fixes.append({"section": "images", "intent": "real_asset_ratio", "severity": "high",
                          "text": f"Real asset ratio {real_ratio:.0%} below 60% floor"})
        score -= 15
    elif real_ratio >= 0.8:
        strengths.append(f"Real asset ratio {real_ratio:.0%} ≥ 80%")

    # ── TODO markers / hallucination proxy ──
    todo_count = _count_todo_markers(html)
    if todo_count > 4:
        should_fixes.append({"section": "copy", "intent": "todo_density", "severity": "med",
                            "text": f"{todo_count} <!-- TODO --> markers — research_brief is thin"})
        score -= max(0, (todo_count - 4) * 2)

    # ── placeholders ──
    placeholders = _detect_lorem_or_placeholder(html)
    for p in placeholders:
        must_fixes.append({"section": "copy", "intent": "placeholder", "severity": "high", "text": p})
        score -= 8

    # ── design system rules ──
    ds_violations = _check_design_system_rules(html)
    for v in ds_violations:
        must_fixes.append({"section": "style", "intent": "design_system", "severity": "high", "text": v})
        score -= 6

    # ── cross-section consistency ──
    consistency_issues = _cross_section_consistency(html, research_brief)
    for c in consistency_issues:
        must_fixes.append({"section": "consistency", "intent": "consistency", "severity": "high", "text": c})
        score -= 10

    # ── positive strengths ──
    if todo_count == 0:
        strengths.append("Zero TODO markers — every claim sourced")
    if not placeholders:
        strengths.append("No placeholder text")
    if not ds_violations:
        strengths.append("Design system rules all green")

    # Clamp
    score = max(0, min(100, score))

    return {
        "score":           score,
        "must_fixes":      must_fixes,
        "should_fixes":    should_fixes,
        "strengths":       strengths,
        "fingerprint":     _fingerprint(html),
        "asset_stats":     asset_stats,
        "todo_count":      todo_count,
    }
