"""Awwwards-vs-template zero-shot classifier.

Runs after final HTML render. Reads the rendered HTML excerpt + the design
fingerprint (palette name, type pair, sections used, motion presets active)
and returns a tier verdict from Claude Sonnet.

Output schema:
  {
    'tier': 'agency' | 'mid' | 'template',
    'score': 0-100,
    'top_strengths': [<= 3 strings],
    'top_weaknesses': [<= 3 strings],
    'must_fixes': [<= 3 actionable fixes; empty if tier == 'agency'],
    'one_line_verdict': '<short summary, < 140 chars>',
    'cost_cents': <int>,
  }

Cost: ~2¢/lead at our typical HTML size (24KB → ~6K input tokens) on Sonnet
4.6. Prompt-cached across the run so subsequent classifications hit the
cache at 0.1x rate.

Never raises. On API failure returns {'tier': 'unknown', 'score': 50, ...}
and an entry in 'errors'. Caller can choose to gate publish on tier ==
'template' or just log the verdict.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# Conservative model pick — Sonnet 4.6 has the right design taste for ~2¢/call.
# Switch to Haiku 4.5 if cost ceiling tightens (~5x cheaper, ~70% the taste).
DEFAULT_MODEL = 'claude-sonnet-4-6'

SYSTEM_PROMPT = """You are a senior agency creative director reviewing a marketing site for a local-services SMB (HVAC, plumbing, auto shop, landscaping). Your job is to rate it on the Awwwards-template axis.

YOU ARE NOT SCORING:
- Whether the copy is good (a separate critic owns that)
- Whether the SEO is dialed (out of scope)
- Whether the page converts (we ship CRO basics; assume those are in place)

YOU ARE SCORING DESIGN FIDELITY:
Tier definitions:
  'agency'   — Looks like a paid agency made it. Distinctive typography, committed palette, intentional motion, photo grading that matches the palette, hierarchy that earns the scroll. A prospect WOULD believe this cost $4-8K.
  'mid'      — Above-template but not premium. Reads as competent but generic. Has at least 2 distinctive choices but converges on safe defaults elsewhere.
  'template' — Reads as Wix/Squarespace/Webflow template + drop-in copy. Generic fonts, undecided palette, no motion, photos slammed unaltered, hero looks like 100 other plumbers' sites.

THE 7 TELLS THAT INSTANTLY PUSH TO 'template':
  1. Inter/Roboto/Arial used as the DISPLAY/headline face (Inter as body is fine)
  2. Generic gradient hero (purple→pink, blue→teal) instead of a committed palette
  3. Stock icon set in service cards (Lucide/Heroicons drop-ins with no styling)
  4. Photo aspect ratios and grading don't match (3 GMB photos in 3 different palettes/tones)
  5. Zero motion or motion on every element (rhythm rule broken)
  6. Service tiles read as bullet-list ("Repair · Install · Replace") with no specifics
  7. Footer is plain copyright line with no license #, no neighborhoods, no last-updated

THE 7 TELLS THAT EARN 'agency':
  1. Display font is a confident editorial/sans-serif pick that matches the trade vibe
  2. Palette commits — one dominant + a tension accent, no timid evenly-distributed mush
  3. Hierarchy varies: hero h1 dwarfs everything else, h2s clearly subordinate
  4. At least one section breaks the rectangle — asymmetric grid, full-bleed image, sticky-caption
  5. Photos look graded (consistent palette tint across all of them)
  6. Service tiles have specifics: "from $189", "hydro-jet with camera, recording emailed", "Joey hand-installs"
  7. Owner-voice signals visible: first name in CTA, "what we don't do" callout, dated last-updated

Output a single JSON object — no prose, no markdown fences. Schema:
{
  "tier": "agency" | "mid" | "template",
  "score": <0-100, where 80+ = agency, 50-79 = mid, <50 = template>,
  "top_strengths": ["<short string>", ...],   // max 3
  "top_weaknesses": ["<short string>", ...],  // max 3
  "must_fixes": ["<actionable, specific>"],   // max 3; empty if tier == 'agency'
  "one_line_verdict": "<< 140 chars, what an AD would say in Slack>"
}
"""


def _extract_visible_text(html: str, max_chars: int = 3500) -> str:
    """Strip HTML tags, return visible text up to max_chars. Used to give the
    classifier a sense of the actual copy without flooding it with markup."""
    no_scripts = re.sub(r'<script\b[^>]*>.*?</script>', ' ', html, flags=re.S | re.I)
    no_style = re.sub(r'<style\b[^>]*>.*?</style>', ' ', no_scripts, flags=re.S | re.I)
    text = re.sub(r'<[^>]+>', ' ', no_style)
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars]


def _design_fingerprint_for_critic(composed: dict, fp_inputs: dict, html: str) -> dict:
    """Distill the design choices into a JSON object the critic can chew on."""
    return {
        'palette': fp_inputs.get('palette') or composed.get('palette'),
        'type_pair': fp_inputs.get('type_pair') or composed.get('type_pair'),
        'sections_used': (fp_inputs.get('sections')
                          or composed.get('sections')
                          or {}),
        'has_motion_attributes': 'data-motion=' in html,
        'has_license_number': bool(re.search(r'License\s*#', html)),
        'has_neighborhoods_strip': 'Areas we serve' in html,
        'has_what_we_dont_do': "What we don't do" in html or 'What we don&#39;t do' in html,
        'has_guarantee': 'Our guarantee' in html,
        'has_last_updated': 'Last updated' in html,
        'has_sticky_cta': 'sticky-cta' in html or 'sticky_cta' in html,
        'has_trust_strip': 'trust-strip' in html,
        'html_bytes': len(html),
        'distinct_service_price_signals': len(re.findall(r'(?:flat|from)\s*\$\d', html, flags=re.I)),
    }


def classify(
    composed: dict,
    fp_inputs: dict,
    html: str,
    tracker,
    model: str = DEFAULT_MODEL,
    client: 'Optional[object]' = None,
) -> dict:
    """Run the zero-shot classifier. Always returns a dict, never raises."""
    out_default = {
        'tier': 'unknown',
        'score': 50,
        'top_strengths': [],
        'top_weaknesses': [],
        'must_fixes': [],
        'one_line_verdict': 'classifier did not run',
        'cost_cents': 0,
        'errors': [],
    }

    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (KeyError, ImportError) as e:
            out_default['errors'].append(f'no API client available: {e}')
            return out_default

    visible_text = _extract_visible_text(html)
    design_fp = _design_fingerprint_for_critic(composed, fp_inputs, html)

    user_msg = (
        f"DESIGN FINGERPRINT (compose's choices + assembler's signal landings):\n"
        f"{json.dumps(design_fp, indent=2)}\n\n"
        f"VISIBLE TEXT (first {len(visible_text)} chars of the rendered page):\n"
        f"{visible_text}\n\n"
        f"Return your verdict as a single JSON object per the schema."
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=900,
            system=[{
                'type': 'text',
                'text': SYSTEM_PROMPT,
                'cache_control': {'type': 'ephemeral'},
            }],
            messages=[{'role': 'user', 'content': user_msg}],
        )
        # Cost tracking — Sonnet 4.6 rates per million tokens:
        # input $3, cache-write $3.75, cache-read $0.30, output $15
        usage = resp.usage
        input_tokens = getattr(usage, 'input_tokens', 0) or 0
        cache_write = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
        output_tokens = getattr(usage, 'output_tokens', 0) or 0
        cost_cents = int(
            (input_tokens * 0.3 + cache_write * 0.375 + cache_read * 0.03 + output_tokens * 1.5)
            / 1000
        )
        if hasattr(tracker, 'add_cents'):
            tracker.add_cents(cost_cents)
        elif hasattr(tracker, 'per_lead_spent_cents'):
            tracker.per_lead_spent_cents += cost_cents

        raw = resp.content[0].text if resp.content else '{}'
    except Exception as e:
        log.exception('awwwards classifier API call failed')
        out_default['errors'].append(f'api failed: {e!r}')
        return out_default

    # Parse JSON — tolerate markdown fence wrappers
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', raw.strip(), flags=re.S)
    try:
        verdict = json.loads(cleaned)
    except json.JSONDecodeError as e:
        out_default['errors'].append(f'parse failed: {e}; raw[:200]={cleaned[:200]!r}')
        return out_default

    tier = verdict.get('tier')
    if tier not in ('agency', 'mid', 'template'):
        out_default['errors'].append(f'invalid tier: {tier!r}')
        return out_default

    try:
        score = int(verdict.get('score', 50))
    except (TypeError, ValueError):
        score = 50
    score = max(0, min(100, score))

    return {
        'tier': tier,
        'score': score,
        'top_strengths': [str(s) for s in (verdict.get('top_strengths') or [])][:3],
        'top_weaknesses': [str(s) for s in (verdict.get('top_weaknesses') or [])][:3],
        'must_fixes': [str(s) for s in (verdict.get('must_fixes') or [])][:3],
        'one_line_verdict': str(verdict.get('one_line_verdict', ''))[:200],
        'cost_cents': cost_cents,
        'errors': [],
    }
