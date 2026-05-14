"""Palette extraction via k-means on RGB pixels.

Pure deterministic (PIL + numpy, no API). Fallback: industry-default neutral palette.

Per SPEC §11 known unknown #8 — track which builds used real-extracted vs
industry-fallback palette. The asset_manifest records `palette_source`.
"""
from __future__ import annotations

from collections import Counter
from io import BytesIO
from pathlib import Path
from typing import Iterable


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _is_neutral(rgb: tuple[int, int, int], white_thresh: int = 240, black_thresh: int = 18) -> bool:
    r, g, b = rgb
    avg = (r + g + b) / 3
    return avg < black_thresh or avg > white_thresh


def _is_gray(rgb: tuple[int, int, int], max_chroma: int = 15) -> bool:
    r, g, b = rgb
    return (max(r, g, b) - min(r, g, b)) < max_chroma


def extract(image_path: Path | str | BytesIO, k: int = 5, drop_neutrals: bool = True) -> list[str]:
    """K-means-style palette extraction via PIL's quantize.

    Returns hex colors sorted by visual weight, neutrals filtered out by default.
    Returns [] on any error (caller falls back to industry_fallback).
    """
    try:
        from PIL import Image
    except ImportError:
        return []
    try:
        img = Image.open(image_path)
        img = img.convert("RGB")
        # Downsample first — kmeans on a 1000x1000 image is wasteful
        img.thumbnail((400, 400))
        # Quantize to a reasonable palette
        q = img.quantize(colors=k * 3, method=Image.Quantize.MEDIANCUT)
        palette = q.getpalette()
        color_counts = Counter()
        for px in q.getdata():
            color_counts[px] += 1
        results: list[tuple[tuple[int, int, int], int]] = []
        for idx, count in color_counts.most_common(k * 3):
            rgb = (palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2])
            if drop_neutrals and (_is_neutral(rgb) or _is_gray(rgb)):
                continue
            results.append((rgb, count))
            if len(results) >= k:
                break
        return [_rgb_to_hex(rgb) for rgb, _ in results]
    except Exception:
        return []


# ─── industry fallback palettes ──────────────────────────────────────────────
# Conservative neutrals + one warm/cool accent per vertical. These are used
# only when palette extraction fails. Each palette is 3 colors: bg, fg, accent.
# Sources: industry conventions, not invented. Recorded as palette_source.
INDUSTRY_FALLBACK = {
    "plumbing":         ["#0d2240", "#ffffff", "#c73b1e"],  # navy + white + red
    "hvac":             ["#1c3d5a", "#f7f4ef", "#d97706"],  # deep blue + cream + amber
    "landscaping":      ["#2d4a2b", "#f0ead6", "#a67c52"],  # forest + parchment + earth
    "roofing":          ["#1c1c1c", "#f5f0e8", "#c73b1e"],  # black + bone + red
    "auto":             ["#0f1419", "#e8e3dc", "#dc2626"],  # near-black + sand + red
    "electrical":       ["#10172a", "#f3f4f6", "#fbbf24"],  # navy + light + amber
    "painting":         ["#3d2f5a", "#f5f0e8", "#c87f2e"],  # plum + cream + ochre
    "cleaning":         ["#1c4e6f", "#ffffff", "#5fb3a1"],  # teal + white + mint
    "pest_control":     ["#2d3e2d", "#f5f1e8", "#6a8f48"],  # forest + parchment + sage
    "tree_service":     ["#3a2f1a", "#e8e0c8", "#7a8d3a"],  # bark + linen + leaf
    "mobile_mechanic":  ["#1c1c1c", "#e8e3dc", "#ef6c00"],  # black + sand + orange
    "general_trade":    ["#1c1c1c", "#f5f0e8", "#c73b1e"],  # neutral default
}


def industry_fallback(vertical: str) -> list[str]:
    """Return [bg, fg, accent] for a vertical, falling back to general_trade."""
    return INDUSTRY_FALLBACK.get(vertical, INDUSTRY_FALLBACK["general_trade"])


def merge_palettes(*sources: Iterable[str]) -> list[str]:
    """Combine palettes from multiple sources, preserving first-seen order, deduping.
    Filters out neutrals + grays."""
    seen: set[str] = set()
    out: list[str] = []
    for src in sources:
        for hex_color in src:
            hc = hex_color.lower()
            if hc in seen:
                continue
            rgb = _hex_to_rgb(hc)
            if _is_neutral(rgb) or _is_gray(rgb):
                continue
            seen.add(hc)
            out.append(hc)
    return out
