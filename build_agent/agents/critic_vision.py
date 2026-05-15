"""Vision critic — Sonnet vision applies the explicit 6-axis rubric in SPEC §4.

Receives 3 screenshots (mobile 375 / tablet 768 / desktop 1440) + the research
brief + the inspiration refs the builder used. Returns weighted-axis rubric.

LOCKED rubric weights — do not change without SPEC update.
"""
from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "build_agent" / "prompts"

MODEL = os.environ.get("VISION_CRITIC_MODEL", "claude-sonnet-4-5-20250929")
MAX_TOKENS = 2000

RUBRIC_WEIGHTS = {
    "originality": 0.25,
    "composition": 0.20,
    "type":        0.15,
    "color":       0.15,
    "photography": 0.15,
    "craft":       0.10,
}

VISION_FLOOR = 7.5

SONNET_INPUT_USD_PER_M = 3.00
SONNET_OUTPUT_USD_PER_M = 15.00
SONNET_IMAGE_USD = 0.0048  # ~ Sonnet vision per image (rough)


def _load_system_prompt() -> str:
    return (PROMPTS_DIR / "critic_vision.system.md").read_text(encoding="utf-8")


def _client() -> "Anthropic":
    if Anthropic is None:
        raise RuntimeError("anthropic package not installed")
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _encode_image(path: Path) -> dict[str, Any]:
    """Encode a JPEG screenshot for Anthropic vision input."""
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
    }


def weighted_final(rubric: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for axis, weight in RUBRIC_WEIGHTS.items():
        score = (rubric.get(axis) or {}).get("score", 0)
        try:
            total += float(score) * weight
        except Exception:
            pass
    return round(total, 2)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    s = text.find("{")
    e = text.rfind("}")
    if s < 0 or e < 0:
        return None
    try:
        return json.loads(text[s : e + 1])
    except Exception:
        return None


def grade(
    screenshot_paths: dict[int, Path] | dict[str, Path],
    research_brief: dict[str, Any],
    inspiration_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Returns rubric verdict plus final weighted score.

    screenshot_paths can be keyed by viewport width (int) or name (str).
    """
    inspiration_refs = inspiration_refs or []

    # Normalize keys + collect images
    images: list[dict[str, Any]] = []
    labels: list[str] = []
    # Prefer 375 / 768 / 1440 if present
    pairs: list[tuple[Any, Path]] = []
    seen: set = set()
    for label_key in (375, "375", "mobile", 768, "768", "tablet", 1440, "1440", "desktop"):
        if label_key in screenshot_paths and label_key not in seen:
            pairs.append((label_key, Path(screenshot_paths[label_key])))
            seen.add(label_key)
    # If still empty, take whatever's provided
    if not pairs:
        for k, v in screenshot_paths.items():
            pairs.append((k, Path(v)))
    pairs = pairs[:3]  # cap at 3 screenshots
    for label, path in pairs:
        if not path.exists():
            continue
        labels.append(str(label))
        images.append({"type": "text", "text": f"\n## Screenshot @ {label}px\n"})
        images.append(_encode_image(path))

    if not images:
        return {
            "error": "no screenshots provided",
            "final_weighted": 0.0,
            "must_fixes": ["render screenshots before grading"],
        }

    # Compose context block (research brief excerpt + inspiration refs)
    biz = research_brief.get("business") or {}
    industry = (research_brief.get("industry_context") or {}).get("vertical")
    brief_excerpt = {
        "name":      biz.get("name"),
        "vertical":  industry,
        "rating":    biz.get("rating"),
        "real_photo_count": len((biz.get("real_photos") or [])),
    }
    refs_summary = []
    for r in inspiration_refs[:5]:
        refs_summary.append({
            "id": r.get("id"),
            "vibe_tags": r.get("vibe_tags"),
            "what_works": r.get("what_works"),
        })

    context_text = (
        "# CONTEXT\n\n"
        "## Research brief (excerpt)\n"
        f"```json\n{json.dumps(brief_excerpt, indent=2)}\n```\n\n"
        "## Inspiration refs the builder used\n"
        f"```json\n{json.dumps(refs_summary, indent=2)}\n```\n\n"
        "# SCREENSHOTS BELOW — grade per the 6-axis rubric. Return strict JSON.\n"
    )

    system = _load_system_prompt()

    user_content: list[dict[str, Any]] = [{"type": "text", "text": context_text}] + images

    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    parsed = _extract_json(text) or {}

    # Compute weighted final
    parsed["final_weighted"] = weighted_final(parsed)

    # Token-based cost approx
    usage = resp.usage
    in_t = getattr(usage, "input_tokens", 0)
    out_t = getattr(usage, "output_tokens", 0)
    cost = round(
        in_t * SONNET_INPUT_USD_PER_M / 1_000_000
        + out_t * SONNET_OUTPUT_USD_PER_M / 1_000_000
        + len(images) * SONNET_IMAGE_USD,
        4,
    )
    parsed["_meta"] = {
        "model": MODEL,
        "input_tokens": in_t,
        "output_tokens": out_t,
        "image_count": sum(1 for it in images if it.get("type") == "image"),
        "cost_usd": cost,
    }
    return parsed
