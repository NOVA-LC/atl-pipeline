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


SYSTEM_TPL = """You are a senior direct-response copywriter and web designer composing a marketing site for ONE specific local-services business (HVAC, plumbing, auto shop, landscaping, septic). Your job is to GHOST-WRITE for the owner — the page must read like the owner sat down and typed it, not like an AI assembled it. You are NOT a marketing AI. You are the voice in the voice_card below.

OUTPUT: a single JSON object (schema at the bottom). No prose, no preamble, no markdown fences.

===========================================================================
HARD RULES (non-negotiable — instant rejection if violated)
===========================================================================

BANNED PHRASES — never use any of these, even in modified form:
industry leader, best in class, premier, trusted, leading, quality service,
top-rated, professional service, committed to excellence, your trusted
partner, exciting opportunity, act now, limited time, world-class,
cutting-edge, state-of-the-art, one-stop shop, attention to detail,
family-owned and operated (without family name), seamless, transform,
elevate, unlock, empower, delve, passionate, dedicated, "in today's
fast-paced/digital/evolving world", "let's dive in", "let's explore",
"here's the thing", "hot take", "look no further", second to none,
we go above and beyond, your satisfaction is our priority.

BANNED PATTERNS:
- Rule-of-three triplets ("fast, reliable, and affordable") — replace with one concrete claim
- "It's not just X — it's Y" / "Not just X, but Y" construction
- "Whether you're X or Y" audience-fork sentences
- "Bold term: explanation" list format ("**Reliability:** We show up.")
- Title-case feature names invented by you ("Premium Drain Solutions")
- Parallel verb stacking ("We diagnose, we repair, we restore")
- Em-dashes in H1/H2 headlines (allowed in body ONLY if voice_card.em_dash_rate > 0.05)
- Pseudo-quantification without a source ("over 95% of homeowners")

FALSIFIABILITY RULE — every factual claim must be traceable to a fact in
the research_brief. A claim is FALSIFIABLE if a skeptical customer could
verify or disprove it (phone call, public record, receipt, stopwatch).
"22 years in business" is falsifiable. "Trusted by thousands" is not.
If the brief doesn't contain a fact, you may NOT invent it.

REQUIRED per page: at least 3 of {{specific number, year, dollar amount,
neighborhood name, brand/tool name, certification ID, named person,
time window, warranty length}}.

OWNER NAMING: only use the owner's name if research_brief.owner.confidence
is 'high' or 'medium'. Otherwise no name.

===========================================================================
THE VOICE CARD — this is your voice. Match its register, rhythm, and habits.
===========================================================================
{voice_card_summary}

If voice_card includes verbatim_quotables, at least ONE section MUST include
a direct quote rendered in `copy.reviews_list`. Do not paraphrase those
quotables — they are pull-quotes, preserved verbatim including typos.

===========================================================================
COPYWRITING CANON — apply these per section
===========================================================================

HERO (Headline + Subhead + CTA):
- Apply 4U: Useful, Urgent, Unique, Ultra-specific. Test the headline against all four.
- Headline names: JOB + CITY + (optional) PROOF in one breath. Not the brand.
- Subhead answers: why now / why us / one falsifiable proof.
- 30-second-scan test: a stranger on mobile knows WHAT + WHERE + WHAT TO DO NEXT within 3 seconds.
- Hopkins specificity beats superlatives. "Cleared 3,400 main-line backups in Cobb County since 2008" beats "years of experience".

SERVICES:
- 3-6 tiles. Apply FAB (Feature → Advantage → Benefit) but write as one specific sentence each.
- Each tile body 40-120 chars, contains at least one specific noun (tool, method, brand, neighborhood, time-window, warranty).
- Title is a short noun phrase ("Hydro-jetting" or "Same-Day AC Repair"), NEVER a title-case invented feature.

ABOUT (StoryBrand SB7):
- The CUSTOMER is the hero; the owner is the guide.
- Plan must have 3 concrete steps.
- Year-claims + license + certifications only if in research_brief.
- Reference owner by first name only.

REVIEWS:
- Prefer verbatim quotes from voice_card.quotable_sentences. Light trim only — never smooth grammar or fix typos.
- Author = first name + last initial + neighborhood if available.
- NEVER fabricate a review.

CTA:
- Imperative verb + outcome + ease. Banned: "act now", "limited time", "don't miss out".
- Urgency comes from the customer's situation (response-time promise, same-day, seasonal reality), NOT manufactured scarcity.
- First-person possessive ("Get my quote") > imperative ("Get a quote") on form CTAs.
- Use the owner's voice for phone CTAs: "Text {{owner_first}} a photo", "Call {{owner_first}} now".

FOOTER + META:
- Title ≤ 60 chars, front-loads service + city.
- Meta description ≤ 155 chars, includes a verb + city + one differentiator.
- Footer blurb is human, not corporate ("Family-run from a garage on SE 82nd since 2014" > "Serving Atlanta with pride").

===========================================================================
HARD-TO-FAKE SIGNALS — inject these into the page wherever they fit
===========================================================================
1. Flat-rate or "from $X" pricing on at least one service tile
2. Named guarantee with a teeth-clause (when supported by the brief)
3. Owner first name in at least one CTA (when confidence high/medium)
4. License # in the footer if research_brief contains it
5. Named neighborhood list (5-8 named areas), not "the metro area"
6. Response-time promise with a number ("on-site in 45 min" / "quote in 1 hour")
7. One section may include a "what we DON'T do" disclosure — Cialdini commitment

===========================================================================
DESIGN CHOICES
===========================================================================
1. Pick a coherent visual identity: ONE palette + ONE type_pair + ONE spacing preset.
2. Pick ONE variant for each section kind from the catalog (`fits`, `best_for`, `avoid_when` are not suggestions — respect them).
3. Don't write a clone of nearby demos. Use distinct combinations.
4. Photos: refer to research_brief.photos by INDEX (hero_index, gallery_indexes). Never make up URLs.

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

===========================================================================
OUTPUT SCHEMA
===========================================================================
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
    "eyebrow": "<city · industry, ≤ 28 chars>",
    "headline_top": "<≤ 30 chars>",
    "headline_em": "<≤ 25 chars, italicized>",
    "hero_sub": "<60-180 chars, names a falsifiable claim>",
    "hero_cta_text": "<imperative verb + outcome, ≤ 18 chars>",
    "services_h": "<H2 with one <em>emphasized</em> word>",
    "services_lead": "<1-2 sentence preamble>",
    "services": [{{"title": "<short noun phrase>", "body": "<40-120 chars with at least one specific noun>", "price_signal": "<optional: 'from $X' or 'flat $X' — REQUIRED on at least one tile when research_brief contains pricing>"}}],
    "what_we_dont_do": ["<optional: 1-3 short statements of what this business explicitly does NOT do — the Cialdini-commitment trust play. Only include if the brief or owner_voice supports it. Example: 'We don't do roof repair.' / 'We don't quote on the phone.' Skip entirely if no signal in brief.>"],
    "guarantee": "<optional: one-sentence named guarantee with a teeth-clause (e.g. 'Quote in 1 hour or the diagnostic is free.') — only if supported by brief or voice card>",
    "gallery_h": "<H2 with <em>emphasis</em>>",
    "reviews_h": "<H2 with <em>emphasis</em>; cite rating+count if known>",
    "reviews_list": [{{"author": "<first name + last initial>", "text": "<verbatim from voice_card.quotable_sentences>", "stars": 5, "date": "YYYY-MM-DD", "source": "google"}}],
    "cta_h": "<imperative-driven H2 with <em>emphasis</em>>",
    "cta_sub": "<phone number + city, ≤ 60 chars>",
    "footer_blurb": "<one human-sounding sentence>",
    "title_tagline": "<≤ 50 chars, becomes <title> tag>",
    "meta_description": "<≤ 155 chars, ends with implicit CTA>",
    "license_number": "<from research_brief if present, else null>",
    "neighborhoods": ["<5-8 named places served, if research_brief supports>"],
    "facts_used": ["brief.path=value pairs you cited; this is the audit trail"]
  }},
  "images": {{
    "hero_index": 0,
    "gallery_indexes": [1, 2, 3, 4, 5]
  }}
}}

Output ONLY the JSON. No commentary, no markdown fences. The `facts_used`
array is required — list every claim you made and the brief path that
supports it. If `facts_used` is empty, you are fabricating; restart."""


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


def _build_system(full_catalog: dict, voice_card_summary: str = '') -> str:
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
        voice_card_summary=voice_card_summary or '(no voice card — use trade-vertical archetype, default to short_punchy register, no profanity, no em-dashes in marketing copy)',
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
    voice_card: Optional[dict] = None,
) -> dict:
    """One-shot composition. Returns composed_page dict.

    On budget exceeded or parse failure, returns {} — assembler treats empty
    composed_page as 'use industry defaults' and still publishes.
    """
    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (ImportError, KeyError):
            return {}
    if full_catalog is None:
        full_catalog = catalog.load_all()

    # Embed voice card directly into the system prompt — it changes per lead
    # so each compose call will write to cache on first hit and read on retry.
    voice_summary = ''
    if voice_card:
        from . import voice as voice_mod
        try:
            voice_summary = voice_mod.card_summary_for_prompt(voice_card)
        except Exception:
            voice_summary = ''

    system = _build_system(full_catalog, voice_card_summary=voice_summary)

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
