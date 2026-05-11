"""Composition sub-agent — given a research brief, picks palette + type +
spacing + section variants and writes the copy for each section.

Single-shot structured output: the catalog goes in the system prompt, the
research brief goes in the user message, the model returns a single
composed_page JSON. Cheaper than a tool-use loop; the assembler's fallback
chain handles any invalid choices.
"""
from __future__ import annotations
import json
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import catalog, cost, banned


SYSTEM_TPL = """You are a senior web designer composing a website for ONE specific business. Your output is a JSON 'composed_page' describing exactly which layout sections, palette, typography, and copy to use.

Your job:
1. Read the research brief — note the vibe, customer segment, buyer psychology, photos available, review volume, years in business.
2. Pick a coherent visual identity from the catalog: ONE palette + ONE type pair + ONE spacing preset.
3. Pick ONE variant for each section kind: hero, services, gallery, reviews, cta. Each variant has 'fits'/'best_for'/'avoid_when' metadata — respect those constraints.
4. Write copy that's SPECIFIC to this business. Reference real facts from the research brief (years, neighborhood, services, reviewer quotes). Avoid generic sales-speak.

Hard rules — these are not suggestions:
- BANNED PHRASES (instant rejection): "industry leader", "best in class", "premier", "trusted partner", "synergy", "transform", "elevate", "world-class", "cutting-edge", "state-of-the-art", "one-stop shop", "exciting opportunity", "act now", "limited time", "attention to detail", "family-owned and operated" (rephrase with specifics).
- NEVER state facts that aren't in the research brief. If owner confidence is low/unknown, don't name the owner. If years_in_business is null, don't say a number.
- Don't write 5 demos that look the same. Use distinct combinations of palette + type + sections for variety across neighbor businesses.
- Photos: refer to research_brief.photos by INDEX in your output (images.hero_index = 0, images.gallery_indexes = [1,2,3,4,5]). Don't make up URLs.
- Copy length budgets: headline_top ≤ 30 chars, headline_em ≤ 25 chars, hero_sub 60–180 chars, each services tile body 40–120 chars.
- Service tiles: 3–6 of them. Title is a short noun phrase (e.g. "Leak Detection"). Body is one specific sentence about what that service looks like for THIS business.

CATALOG (the only valid choices — never invent names not in this list):

PALETTES:
{palettes}

TYPE PAIRS:
{type_pairs}

SPACING:
{spacing}

SHELLS:
{shells}

SECTION VARIANTS by kind:
{sections}

OUTPUT: a single JSON object matching:
{{
  "shell": "<shell_name>",
  "palette": "<palette_name>",
  "type_pair": "<type_pair_name>",
  "spacing": "<spacing_name>",
  "sections": {{
    "hero": "<variant_name>",
    "services": "<variant_name>",
    "gallery": "<variant_name>",
    "reviews": "<variant_name>",
    "cta": "<variant_name>"
  }},
  "copy": {{
    "eyebrow": "...",
    "headline_top": "...",
    "headline_em": "...",
    "hero_sub": "...",
    "hero_cta_text": "...",
    "services_h": "... <em>emphasis</em> ...",
    "services_lead": "...",
    "services": [{{"title": "...", "body": "..."}}, ...],
    "gallery_h": "... <em>emphasis</em> ...",
    "reviews_h": "... <em>emphasis</em> ...",
    "reviews_list": [{{"author": "...", "text": "...", "stars": 5, "date": "...", "source": "..."}}],
    "cta_h": "... <em>emphasis</em> ...",
    "cta_sub": "...",
    "footer_blurb": "...",
    "title_tagline": "..."
  }},
  "images": {{
    "hero_index": 0,
    "gallery_indexes": [1, 2, 3, 4, 5]
  }},
  "fingerprint_inputs": {{"_": "leave blank; assembler computes"}}
}}

Output ONLY the JSON, no commentary, no markdown fences."""


def _format_catalog_section(d: dict, name_only_keys: list[str] = ()) -> str:
    """Render a catalog dict as a terse human-readable bullet list."""
    lines = []
    for name, meta in sorted(d.items()):
        if name_only_keys and any(k in name_only_keys for k in []):
            lines.append(f'- {name}')
            continue
        fits = meta.get('fits') or meta.get('best_for') or ''
        vibe = ', '.join(meta.get('vibe_tags', [])) if meta.get('vibe_tags') else ''
        bullet = f'- {name}'
        if vibe:
            bullet += f' (vibe: {vibe})'
        if fits:
            bullet += f': {fits[:140]}'
        lines.append(bullet)
    return '\n'.join(lines)


def _build_system(full_catalog: dict) -> str:
    sections_block = []
    for kind, variants in full_catalog['sections'].items():
        sections_block.append(f'  {kind}:')
        for name, v in sorted(variants.items()):
            meta = v.get('metadata', {})
            vibe = ', '.join(meta.get('vibe_tags', []))
            best = meta.get('best_for', '')
            avoid = meta.get('avoid_when', '')
            req = meta.get('requires', {})
            line = f'    - {name}'
            if vibe:
                line += f' (vibe: {vibe})'
            if req:
                line += f' [requires: {req}]'
            if best:
                line += f' — best: {best[:120]}'
            if avoid:
                line += f' — avoid: {avoid[:80]}'
            sections_block.append(line)

    return SYSTEM_TPL.format(
        palettes=_format_catalog_section(full_catalog['palettes']),
        type_pairs=_format_catalog_section(full_catalog['type_pairs']),
        spacing=_format_catalog_section(full_catalog['spacing']),
        shells='\n'.join(f'- {s}' for s in sorted(full_catalog['shells'].keys())),
        sections='\n'.join(sections_block),
    )


def _parse_composed(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    if s.startswith('```'):
        s = s.split('\n', 1)[1] if '\n' in s else s
        if s.endswith('```'):
            s = s[:-3]
        s = s.strip()
    first = s.find('{')
    last = s.rfind('}')
    if first == -1 or last == -1:
        return None
    try:
        return json.loads(s[first:last + 1])
    except json.JSONDecodeError:
        return None


def _resolve_photo_indexes(composed: dict, research_brief: dict) -> dict:
    """Replace composed.images.{hero_index, gallery_indexes} with actual URLs
    from research_brief.photos. If indexes invalid, drop them — assembler will
    fall back to raw_outscraper photos / industry stock."""
    photos = (research_brief or {}).get('photos') or []
    if not photos:
        return composed
    images = composed.get('images') or {}
    hi = images.get('hero_index')
    gi = images.get('gallery_indexes') or []
    urls = [p['url'] for p in photos if isinstance(p, dict) and p.get('url')]
    new_images = dict(images)
    if isinstance(hi, int) and 0 <= hi < len(urls):
        new_images['hero'] = urls[hi]
    if isinstance(gi, list):
        new_images['gallery'] = [
            {'url': urls[i], 'caption': photos[i].get('caption', '') if i < len(photos) else ''}
            for i in gi if isinstance(i, int) and 0 <= i < len(urls)
        ]
    composed['images'] = new_images
    return composed


def compose_page(
    lead: dict,
    research_brief: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-sonnet-4-6',
    max_output_tokens: int = 3500,
    client: 'Optional[anthropic.Anthropic]' = None,
    full_catalog: Optional[dict] = None,
) -> dict:
    """One-shot composition. Returns composed_page dict.

    On budget exceeded or parse failure, returns {} — assembler treats empty
    composed_page as 'use industry defaults' and still publishes.
    """
    import anthropic  # lazy
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    if full_catalog is None:
        full_catalog = catalog.load_all()

    system = _build_system(full_catalog)

    user = (
        f"BUSINESS:\n"
        f"  name: {lead.get('business_name')}\n"
        f"  category: {lead.get('category')}\n"
        f"  city: {lead.get('city')}, {lead.get('state')}\n"
        f"  phone: {lead.get('phone')}\n"
        f"  rating: {lead.get('rating')}★ across {lead.get('reviews')} Google reviews\n"
        f"\nRESEARCH BRIEF:\n{json.dumps(research_brief, indent=2)[:6000]}\n"
        f"\nCompose the JSON now. Be specific to this business, never generic."
    )

    est_input = (len(system) + len(user)) // 4 + 500
    try:
        tracker.check_can_afford(model, est_input, max_output_tokens)
    except cost.BudgetExceeded:
        return {}

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_output_tokens,
            # Catalog is identical for every lead — cache it. ~90% input cost
            # reduction on every lead after the first in any 5-minute window.
            system=[
                {'type': 'text', 'text': system,
                 'cache_control': {'type': 'ephemeral'}}
            ],
            thinking={'type': 'adaptive'},
            output_config={'effort': 'medium'},
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception:
        return {}

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='compose')

    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    composed = _parse_composed(text)
    if not composed:
        return {'_parse_error': True, '_last_text': text[:500]}

    # Resolve photo indexes into URLs using the research_brief
    composed = _resolve_photo_indexes(composed, research_brief)

    # Scrub banned phrases from copy before returning (belt + suspenders;
    # assembler will do it again as a safety net)
    if isinstance(composed.get('copy'), dict):
        cleaned, removed = banned.clean_copy_dict(composed['copy'])
        composed['copy'] = cleaned
        if removed:
            composed['_banned_removed'] = removed

    # Validate against catalog (warnings only — assembler enforces)
    avail = full_catalog['available']
    errs = []
    if composed.get('palette') and composed['palette'] not in avail['palettes']:
        errs.append(f"palette '{composed['palette']}' not in catalog")
    if composed.get('type_pair') and composed['type_pair'] not in avail['type_pairs']:
        errs.append(f"type_pair '{composed['type_pair']}' not in catalog")
    sections = composed.get('sections') or {}
    for kind, name in sections.items():
        if kind in avail['sections'] and name not in avail['sections'][kind]:
            errs.append(f"{kind} variant '{name}' not in catalog")
    if errs:
        composed['_catalog_warnings'] = errs

    return composed
