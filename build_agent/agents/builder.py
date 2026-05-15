"""Builder — Sonnet 4.6 writes a complete HTML+CSS site from scratch.

Reads:
- research_brief.json (facts only — every claim must trace back to a source)
- assets_manifest.json (real prospect images + extracted palette)
- 3-5 inspiration refs from the corpus
- design_system/primitives.css tokens + rules.md constraints

Writes ONE self-contained HTML file. The orchestrator handles iteration +
regeneration; this module just does first-pass synthesis (and targeted
re-renders via regenerate_section).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from anthropic import Anthropic
except Exception:
    Anthropic = None  # type: ignore

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "build_agent" / "prompts"
DESIGN_SYSTEM_DIR = REPO_ROOT / "design_system"

MODEL = os.environ.get("BUILDER_MODEL", "claude-sonnet-4-5-20250929")  # SPEC: Sonnet 4.6
MAX_TOKENS = int(os.environ.get("BUILDER_MAX_TOKENS", "8000"))

# Cost approx for budget tracking — actual cost reported by API response usage block
SONNET_INPUT_USD_PER_M = 3.00
SONNET_OUTPUT_USD_PER_M = 15.00


def _client() -> "Anthropic":
    if Anthropic is None:
        raise RuntimeError("anthropic package not installed")
    return Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _load_prompt() -> str:
    base = (PROMPTS_DIR / "builder.system.md").read_text(encoding="utf-8")
    rules = (DESIGN_SYSTEM_DIR / "rules.md").read_text(encoding="utf-8")
    # Inline the design system rules at the end so the model can't miss them
    return base + "\n\n---\n\n## Design system rules (verbatim)\n\n" + rules


def _format_inspiration_refs(refs: list[dict[str, Any]]) -> str:
    if not refs:
        return "(no inspiration refs available — work from the brief and design system only)"
    out = []
    for r in refs:
        out.append(
            f"### {r.get('id')} ({r.get('source_url', '?')})\n"
            f"- vibe_tags: {', '.join(r.get('vibe_tags', []))}\n"
            f"- what_works: {r.get('what_works', '')}\n"
            f"- what_does_not_translate: {r.get('what_does_not_translate', '')}\n"
            f"- palette: {r.get('palette_dominant', [])}\n"
            f"- type: {r.get('type_observations', '')}\n"
        )
    return "\n".join(out)


def _format_assets(manifest: dict[str, Any]) -> str:
    if not manifest:
        return "(no assets — render type-only sections)"
    lines = [f"PALETTE: {json.dumps(manifest.get('palette', {}))}"]
    lines.append(f"REAL_ASSET_RATIO: {manifest.get('real_asset_ratio', 0)}")
    lines.append("ASSETS (use ONLY these — no other <img> sources allowed):")
    for a in manifest.get("assets", []):
        slot = a.get("slot")
        fn = a.get("filename")
        origin = a.get("origin")
        dims = f"{a.get('width','?')}×{a.get('height','?')}"
        alt = a.get("alt") or ""
        lines.append(f"  - slot={slot} file=./assets/{fn} origin={origin} dims={dims}{(' alt=' + alt) if alt else ''}")
    return "\n".join(lines)


def _strip_html_from_markdown(response_text: str) -> str:
    """Sonnet often wraps HTML in ```html fences. Extract the inner HTML."""
    text = response_text.strip()
    # Look for ```html ... ``` or just ``` ... ```
    if text.startswith("```"):
        # find first newline (after opening fence), and last ```
        first_nl = text.find("\n")
        last_fence = text.rfind("```")
        if first_nl != -1 and last_fence > first_nl:
            return text[first_nl + 1 : last_fence].strip()
    # If no fences, but model added prose preamble, find <!DOCTYPE or <html
    for marker in ("<!DOCTYPE", "<!doctype", "<html"):
        idx = text.find(marker)
        if idx > 0:
            return text[idx:].strip()
    return text


def build_html(
    research_brief: dict[str, Any],
    assets_manifest: dict[str, Any],
    inspiration_refs: list[dict[str, Any]],
) -> dict[str, Any]:
    """First-pass HTML synthesis. Returns {html, cost_usd, model, usage}."""
    system = _load_prompt()

    user_msg = f"""Build a complete single-page HTML+CSS site for the prospect below.

# RESEARCH BRIEF (sourced facts only — do not invent data)

```json
{json.dumps(research_brief, indent=2, default=str)}
```

# ASSETS MANIFEST (use ONLY these images, by filename)

{_format_assets(assets_manifest)}

# INSPIRATION REFERENCES (study composition + treatment; never copy code)

{_format_inspiration_refs(inspiration_refs)}

# OUTPUT FORMAT

Return ONE complete HTML file. Self-contained:
- Inline `<style>` block (do NOT link to /design_system/primitives.css — copy the relevant tokens inline).
- `<link rel="preconnect">` and `<link rel="stylesheet">` to Google Fonts allowed (max 2 families).
- `<img>` src must point at `./assets/<filename>` from the manifest. No external URLs.
- No external JS. No frameworks.

Return ONLY the HTML, no commentary. Start with `<!DOCTYPE html>`."""

    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )

    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    html = _strip_html_from_markdown(text)

    # Cost from usage block
    usage = resp.usage
    in_tokens = getattr(usage, "input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", 0)
    cost = round(
        (in_tokens * SONNET_INPUT_USD_PER_M / 1_000_000)
        + (out_tokens * SONNET_OUTPUT_USD_PER_M / 1_000_000),
        4,
    )

    return {
        "html": html,
        "cost_usd": cost,
        "model": MODEL,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
    }


def regenerate_section(
    current_html: str,
    section: str,
    must_fix: str,
    research_brief: dict[str, Any],
    assets_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Targeted re-render of one section based on a critic must_fix.

    Sends the current HTML + the must_fix + scoped instructions to Sonnet,
    asks it to return the full HTML with only the targeted section changed.
    """
    system = _load_prompt() + (
        "\n\n## REGENERATION MODE\n"
        "You are receiving an existing site and a single must_fix. Return the FULL\n"
        "HTML with ONLY the indicated section changed. Preserve everything else\n"
        "byte-for-byte. The change must directly address the must_fix."
    )

    user_msg = f"""# CURRENT HTML

```html
{current_html}
```

# MUST_FIX (from critic)

Section: {section}
Fix: {must_fix}

# CONTEXT (for reference)

Research brief: {json.dumps(research_brief.get('business', {}), default=str)[:1200]}
Available assets: {[a['filename'] for a in (assets_manifest.get('assets') or [])]}

Return the full updated HTML, no commentary. Start with `<!DOCTYPE html>`."""

    client = _client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    html = _strip_html_from_markdown(text)
    usage = resp.usage
    in_tokens = getattr(usage, "input_tokens", 0)
    out_tokens = getattr(usage, "output_tokens", 0)
    cost = round(
        (in_tokens * SONNET_INPUT_USD_PER_M / 1_000_000)
        + (out_tokens * SONNET_OUTPUT_USD_PER_M / 1_000_000),
        4,
    )
    return {
        "html": html,
        "cost_usd": cost,
        "model": MODEL,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "section": section,
        "must_fix": must_fix,
    }
