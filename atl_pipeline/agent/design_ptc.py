"""PTC-lite (Probabilistic Tree of Candidates) at the design level.

Instead of running one expensive compose call and praying it lands on a
design that's distinctive vs neighbors, we:

  1. Run a small Haiku 4.5 'designer' N times with temperature=0.9 to get
     N candidate design tuples (palette, type_pair, sections_list).
  2. Score each candidate algorithmically by:
       - Anti-clone: Jaccard distance from each neighbor demo's design fp
       - Vertical fit: does type_pair.fits_verticals include this vertical?
       - Doctrine compliance: palette is committed (no purple-on-white),
         display font is not Inter
  3. Pick the winner, pass it to compose() as a `_design_hint` that pins
     the design decisions so compose can focus on copy.

Cost: ~$0.005/lead (Haiku 4.5, 4 candidates × 200 output tokens).

Why design-level and not full-page PTC?
- Design JSON is tiny (~200 output tokens). Cheap to enumerate.
- Copy generation is the expensive call (~4K tokens). We only do it once,
  on the winning design.
- Anti-clone scoring is straightforward at the design level (compare
  palette × type × section_set against neighbor fingerprints). Scoring
  copy diversity at the page level would require a heavier rubric.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_MODEL = 'claude-haiku-4-5'
DEFAULT_N = 4
TEMPERATURE = 0.9


SYSTEM_TPL = """You are a senior art director picking the DESIGN SYSTEM (palette + type pair + section layouts) for ONE specific local-services business marketing site. You are NOT writing copy — a separate copywriter handles that. Your one job: pick choices that look agency-tier and don't clone the neighbor demos.

Output: a single JSON object — no prose, no markdown fences.

AVAILABLE PALETTES (you MUST pick from this list, by name):
{palette_list}

AVAILABLE TYPE PAIRS (pick by name; each has fits_verticals tags):
{type_pair_list}

AVAILABLE SECTION VARIANTS (pick one per section slot):
- hero: {hero_variants}
- services: {services_variants}
- gallery: {gallery_variants}
- reviews: {reviews_variants}
- cta: {cta_variants}

CONSTRAINTS:
1. NEVER pick a palette named 'generic-blue-purple', 'pastel-gradient', or anything described as 'safe default' — commit to a palette with tension.
2. NEVER pick Inter, Roboto, or Arial as the DISPLAY face. Inter is body-only.
3. Pick a type_pair whose fits_verticals INCLUDES the business vertical when possible.
4. Diversify against the NEIGHBOR DEMOS section — your output must differ from all of them in at least 2 of {{palette, type_pair, hero_variant}}.

DOCTRINE (verbatim from Anthropic's frontend-aesthetics cookbook):
Avoid generic AI aesthetics: overused fonts (Inter/Roboto/Arial), clichéd schemes (purple gradients on white), predictable layouts. You converge on Space Grotesk — DON'T pick anything that smells like it. Think outside the box.

Output schema:
{{
  "palette": "<name from list above>",
  "type_pair": "<name from list above>",
  "sections": {{
    "hero": "<variant>",
    "services": "<variant>",
    "gallery": "<variant>",
    "reviews": "<variant>",
    "cta": "<variant>"
  }},
  "rationale": "<1-2 sentence why these choices fit THIS business and differ from neighbors>"
}}
"""


def _format_palette_list(cat: dict) -> str:
    palettes = cat.get('palettes', {})
    rows = []
    for name, p in sorted(palettes.items())[:14]:
        accent = p.get('accent', '')
        ink = p.get('ink', '')
        mood = p.get('mood', '') or p.get('description', '')
        rows.append(f'  - {name}  (ink {ink}, accent {accent}) — {mood}'[:120])
    return '\n'.join(rows)


def _format_type_pair_list(cat: dict) -> str:
    pairs = cat.get('type_pairs', {})
    rows = []
    for name, p in sorted(pairs.items()):
        verticals = p.get('fits_verticals', [])
        vibes = p.get('vibe_tags', [])[:3]
        rows.append(f'  - {name}  verticals={verticals} vibes={vibes}'[:140])
    return '\n'.join(rows)


def _variants_for(cat: dict, section: str) -> list[str]:
    return sorted((cat.get('sections', {}).get(section) or {}).keys())


def _build_system_prompt(cat: dict) -> str:
    return SYSTEM_TPL.format(
        palette_list=_format_palette_list(cat),
        type_pair_list=_format_type_pair_list(cat),
        hero_variants=_variants_for(cat, 'hero'),
        services_variants=_variants_for(cat, 'services'),
        gallery_variants=_variants_for(cat, 'gallery'),
        reviews_variants=_variants_for(cat, 'reviews'),
        cta_variants=_variants_for(cat, 'cta'),
    )


def _build_user_msg(lead: dict, voice_card: dict, neighbor_fps: list[dict],
                    real_photo_count: int = 0) -> str:
    vertical = (lead.get('category') or '').lower()
    rating = lead.get('rating')
    reviews = lead.get('reviews')
    voice_summary = ''
    if isinstance(voice_card, dict):
        voice_summary = (
            f"register: {voice_card.get('register', 'unknown')}, "
            f"tone: {voice_card.get('tone_axis', 'unknown')}"
        )
    neighbor_section = ''
    if neighbor_fps:
        n_summaries = []
        for fp in neighbor_fps[:6]:
            n_summaries.append(
                f"  - palette={fp.get('palette')}, type_pair={fp.get('type_pair')}, "
                f"hero={(fp.get('sections') or {}).get('hero')}"
            )
        neighbor_section = (
            "\nNEIGHBOR DEMOS already in this batch (you MUST differ from these):\n"
            + '\n'.join(n_summaries)
        )

    # Photo-availability is the #1 "looks AI" tell. Tell the designer
    # explicitly so they can route to a type-forward hero/no-gallery design
    # instead of picking a photo-heavy variant that will fall back to stock.
    if real_photo_count >= 6:
        photo_guidance = (
            f"REAL PHOTOS AVAILABLE: {real_photo_count} (strong). "
            f"Photo-forward heroes (full-bleed-photo, split-photo-copy, stats-band) "
            f"and 3-col-grid/masonry galleries are all fair game."
        )
    elif real_photo_count >= 3:
        photo_guidance = (
            f"REAL PHOTOS AVAILABLE: {real_photo_count} (mid). "
            f"Prefer split-photo-copy or single-feature gallery — don't pick "
            f"variants that need 5+ photos or you'll get stock padding."
        )
    else:
        photo_guidance = (
            f"REAL PHOTOS AVAILABLE: {real_photo_count} (NONE / weak). "
            f"You MUST pick a type-forward hero (minimal-type or rugged-trade — "
            f"NEVER full-bleed-photo / split-photo-copy / stats-band) and "
            f"single-feature gallery only if photos exist. Photo-heavy variants "
            f"will fall back to Unsplash stock which screams 'AI-generated'."
        )

    return (
        f"BUSINESS: {lead.get('business_name')} ({vertical or 'unknown vertical'}) "
        f"in {lead.get('city', '')}.\n"
        f"Rating: {rating} stars across {reviews} reviews.\n"
        f"Voice: {voice_summary or 'no voice card'}.\n"
        f"{photo_guidance}\n"
        f"{neighbor_section}\n\n"
        "Pick the design tuple per the schema."
    )


def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    return len(a & b) / max(len(a | b), 1)


def _score_candidate(cand: dict, vertical: str, neighbor_fps: list[dict], cat: dict,
                     real_photo_count: int = 0,
                     rating: float | None = None,
                     reviews: int | None = None) -> tuple[float, list[str]]:
    """Score 0-100. Higher is better. Returns (score, reasons).

    real_photo_count is the number of trusted business-source photos available
    for this lead (Google Business Profile / Outscraper). Picking a hero or
    gallery variant whose `requires.min_real_photos` exceeds this count means
    the assembler will fall back to stock Unsplash, which is THE biggest
    "looks AI" tell. We penalize those candidates hard so PTC routes the lead
    to a type-forward / no-photo design when the business has weak imagery.
    """
    reasons = []
    score = 50

    palette = cand.get('palette')
    type_pair = cand.get('type_pair')
    sections = cand.get('sections', {}) or {}

    # === Vertical fit on type pair ===
    pair_data = cat.get('type_pairs', {}).get(type_pair) or {}
    fits_verticals = pair_data.get('fits_verticals', [])
    if vertical and fits_verticals and vertical in fits_verticals:
        score += 12
        reasons.append(f'type_pair fits vertical ({vertical})')
    elif fits_verticals:
        score -= 8
        reasons.append(f'type_pair does NOT fit vertical')

    # === Doctrine: no Inter/Roboto as display ===
    display_fam = (pair_data.get('display_family') or '').lower()
    if 'inter' in display_fam.split(',')[0] or 'roboto' in display_fam.split(',')[0] or 'arial' in display_fam.split(',')[0]:
        score -= 30
        reasons.append('Inter/Roboto/Arial as display — doctrine violation')

    # === Anti-clone vs neighbors ===
    if neighbor_fps:
        neighbor_palette_set = {fp.get('palette') for fp in neighbor_fps if fp.get('palette')}
        neighbor_typepair_set = {fp.get('type_pair') for fp in neighbor_fps if fp.get('type_pair')}
        neighbor_hero_set = {(fp.get('sections') or {}).get('hero') for fp in neighbor_fps}
        differs = 0
        if palette and palette not in neighbor_palette_set: differs += 1
        if type_pair and type_pair not in neighbor_typepair_set: differs += 1
        if sections.get('hero') and sections.get('hero') not in neighbor_hero_set: differs += 1
        if differs >= 2:
            score += 18
            reasons.append(f'differs from neighbors on {differs}/3 design axes')
        elif differs == 1:
            score -= 5
            reasons.append('differs from neighbors on only 1 axis — near-clone risk')
        else:
            score -= 25
            reasons.append('matches neighbors on ALL 3 design axes — clone')

    # === Validity (palette + type pair exist in catalog) ===
    if palette not in (cat.get('palettes') or {}):
        score -= 50; reasons.append(f'invalid palette {palette!r}')
    if type_pair not in (cat.get('type_pairs') or {}):
        score -= 50; reasons.append(f'invalid type_pair {type_pair!r}')

    # === Photo-availability gating (the #1 "looks AI" axis) ===
    # If the variant requires real photos and the lead doesn't have them, the
    # assembler will pad with stock Unsplash. That's the failure mode. Penalize
    # hard so PTC picks a type-forward design instead.
    def _req(section_kind: str, variant_name: str) -> dict:
        sect = (cat.get('sections') or {}).get(section_kind) or {}
        entry = sect.get(variant_name) or {}
        return (entry.get('metadata') or {}).get('requires') or {}

    hero_variant = sections.get('hero')
    hero_req = _req('hero', hero_variant) if hero_variant else {}
    min_hero_photos = int(hero_req.get('min_real_photos') or 0)
    if min_hero_photos > 0 and real_photo_count < min_hero_photos:
        # Hard penalty: this candidate WILL render stock photos in the hero.
        score -= 35
        reasons.append(
            f'hero {hero_variant!r} requires {min_hero_photos} real photo(s); '
            f'lead has {real_photo_count} — would fall back to stock')
    elif min_hero_photos == 0:
        # Mild bonus for picking a no-photo-required hero when imagery is thin.
        if real_photo_count < 4:
            score += 6
            reasons.append(f'hero {hero_variant!r} is type-forward (no photos needed)')

    # stats-band hero requires rating + review thresholds, not photos.
    if hero_variant == 'stats-band':
        min_rating = float(hero_req.get('min_rating') or 0)
        min_reviews = int(hero_req.get('min_reviews') or 0)
        if (rating is None) or (rating < min_rating) or (reviews is None) or (reviews < min_reviews):
            score -= 25
            reasons.append(
                f'hero stats-band requires rating>={min_rating} and reviews>={min_reviews}; '
                f'lead has rating={rating} reviews={reviews}')

    gallery_variant = sections.get('gallery')
    gallery_req = _req('gallery', gallery_variant) if gallery_variant else {}
    min_gallery_photos = int(gallery_req.get('min_photos') or 0)
    if min_gallery_photos > 0 and real_photo_count < min_gallery_photos:
        # The gallery slot will pad with stock. Penalty scales with the gap.
        gap = min_gallery_photos - real_photo_count
        penalty = min(20, 4 + gap * 3)
        score -= penalty
        reasons.append(
            f'gallery {gallery_variant!r} requires {min_gallery_photos} photos; '
            f'lead has {real_photo_count} — assembler will pad with stock')

    return float(max(0, min(100, score))), reasons


def _parse_candidate(raw: str) -> Optional[dict]:
    cleaned = re.sub(r'^```(?:json)?\s*|\s*```\s*$', '', raw.strip(), flags=re.S)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return None


def pick_design(
    lead: dict,
    voice_card: dict | None,
    neighbor_fps: Iterable[dict],
    tracker,
    full_catalog: dict,
    n: int = DEFAULT_N,
    model: str = DEFAULT_MODEL,
    client: 'Optional[object]' = None,
    real_photo_count: int = 0,
) -> dict:
    """Generate N design candidates, score, return winner with rationale.

    On any failure (no API key, all candidates invalid, etc.) returns
    {'design_hint': None, 'errors': [...]} so the caller can fall through
    to compose's own design picking.
    """
    out = {
        'design_hint': None,
        'winner_score': 0,
        'rejected_candidates': [],
        'cost_cents': 0,
        'errors': [],
    }

    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (KeyError, ImportError) as e:
            out['errors'].append(f'no API client: {e}')
            return out

    system_prompt = _build_system_prompt(full_catalog)
    neighbor_list = list(neighbor_fps)[:6]
    user_msg = _build_user_msg(lead, voice_card or {}, neighbor_list, real_photo_count)
    vertical = (lead.get('category') or '').lower()
    # Normalize vertical to match fits_verticals tags
    for v in ('plumber', 'hvac', 'radiator', 'landscape', 'septic', 'auto'):
        if v in vertical:
            vertical = v
            break

    candidates: list[tuple[float, dict, list[str]]] = []
    total_cost_cents = 0

    for i in range(n):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=500,
                temperature=TEMPERATURE,
                system=[{'type': 'text', 'text': system_prompt, 'cache_control': {'type': 'ephemeral'}}],
                messages=[{'role': 'user', 'content': user_msg}],
            )
            usage = resp.usage
            input_t = getattr(usage, 'input_tokens', 0) or 0
            cw = getattr(usage, 'cache_creation_input_tokens', 0) or 0
            cr = getattr(usage, 'cache_read_input_tokens', 0) or 0
            ot = getattr(usage, 'output_tokens', 0) or 0
            # Haiku 4.5 rates per million: input $1, cache-write $1.25, cache-read $0.10, output $5
            cost_cents = int(
                (input_t * 0.1 + cw * 0.125 + cr * 0.01 + ot * 0.5)
                / 1000
            )
            total_cost_cents += cost_cents
            raw = resp.content[0].text if resp.content else '{}'
        except Exception as e:
            out['errors'].append(f'candidate {i} api failed: {e!r}')
            continue

        cand = _parse_candidate(raw)
        if not cand:
            out['errors'].append(f'candidate {i} parse failed')
            continue

        score, reasons = _score_candidate(
            cand, vertical, neighbor_list, full_catalog,
            real_photo_count=real_photo_count,
            rating=lead.get('rating'),
            reviews=lead.get('reviews'),
        )
        candidates.append((score, cand, reasons))

    if hasattr(tracker, 'add_cents'):
        tracker.add_cents(total_cost_cents)
    elif hasattr(tracker, 'per_lead_spent_cents'):
        tracker.per_lead_spent_cents += total_cost_cents
    out['cost_cents'] = total_cost_cents

    if not candidates:
        out['errors'].append('no valid candidates generated')
        return out

    candidates.sort(key=lambda x: x[0], reverse=True)
    winner_score, winner_cand, winner_reasons = candidates[0]
    out['design_hint'] = winner_cand
    out['design_hint']['_ptc_reasons'] = winner_reasons
    out['winner_score'] = winner_score
    out['rejected_candidates'] = [
        {'score': s, 'palette': c.get('palette'), 'type_pair': c.get('type_pair'),
         'reasons': r[:3]}
        for s, c, r in candidates[1:]
    ]
    return out
