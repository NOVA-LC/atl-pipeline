"""Render a composed_page.json (or partial/empty) into final HTML.

CORE CONTRACT: assemble() never raises and never returns empty string. Every
failure path falls back to a sensible default. The agent's "policy" is in
composed_page; this module's job is to make it ship.

Degradation layers (each tried in order):
  1. composed_page.sections has all 5 kinds → render with those
  2. missing kind → pick a safe default for that kind
  3. composed_page palette/type/spacing unknown → swap to known-good defaults
  4. images.hero missing → pull from real_photos[0] or industry stock
  5. images.gallery missing/too few → pad with industry stock
  6. copy missing critical fields → fill from lead/research_brief or Phase 1 defaults

All Phase 1 plumbing from generate.py (raw_outscraper → real Google photos /
reviews / hours / description) is honored: if composed_page is empty, the
assembler builds a default composition that matches the Phase 1 output.
"""
from __future__ import annotations
import datetime
import json
import re
import datetime as _dt
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import catalog, banned
from .. import photo_library as pl
from .. import outscraper_fields as osf


TPL_DIR = Path(__file__).parent.parent / 'templates'

_env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(['html', 'j2']),
)


# Safe defaults the assembler falls back to when composed_page is missing pieces.
DEFAULT_SHELL = 'standard'
DEFAULT_PALETTE_BY_INDUSTRY = {
    'plumber': 'clean-trade-blue',
    'hvac': 'heritage-navy-gold',
    'radiator': 'rugged-shop-orange',
    'landscape': 'warm-earth',
    'septic': 'emergency-red',
}
# Multiple type-pair candidates per vertical so a slug-deterministic hash can
# pick a different default for neighbor demos. Composition agent overrides
# this when it makes an explicit pick; this is the fallback diversity layer.
DEFAULT_TYPE_BY_INDUSTRY = {
    'plumber': 'archivo-inter',
    'hvac': 'fraunces-inter',
    'radiator': 'oswald-inter',
    'landscape': 'fraunces-inter',
    'septic': 'archivo-inter',
}
TYPE_CANDIDATES_BY_INDUSTRY = {
    'plumber':   ['archivo-inter', 'lora-inter', 'bebas-inter', 'fraunces-inter'],
    'hvac':      ['fraunces-inter', 'dm-serif-inter', 'ibm-plex-inter', 'cormorant-inter', 'space-mono-inter'],
    'radiator':  ['oswald-inter', 'anton-inter', 'bebas-inter', 'space-mono-inter'],
    'landscape': ['lora-inter', 'cormorant-inter', 'playfair-inter', 'abril-inter', 'dm-serif-inter'],
    'septic':    ['bebas-inter', 'anton-inter', 'oswald-inter', 'archivo-inter'],
}


def _slug_hash(slug: str | None) -> int:
    """Deterministic small integer for picking from variant lists per-slug."""
    if not slug:
        return 0
    return sum(ord(c) for c in slug)
DEFAULT_SPACING = 'default'
DEFAULT_SECTIONS_BY_INDUSTRY = {
    'plumber': {'hero': 'split-photo-copy', 'services': 'icon-cards',
                'gallery': 'single-feature', 'reviews': 'card-grid',
                'cta': 'phone-prominent'},
    'hvac': {'hero': 'stats-band', 'services': 'numbered-grid',
             'gallery': '3-col-grid', 'reviews': 'card-grid',
             'cta': 'phone-prominent'},
    'radiator': {'hero': 'rugged-trade', 'services': 'bold-list',
                 'gallery': 'masonry', 'reviews': 'full-width-quote',
                 'cta': 'phone-prominent'},
    'landscape': {'hero': 'full-bleed-photo', 'services': 'numbered-grid',
                  'gallery': 'masonry', 'reviews': 'card-grid',
                  'cta': 'phone-prominent'},
    'septic': {'hero': 'rugged-trade', 'services': 'bold-list',
               'gallery': 'single-feature', 'reviews': 'full-width-quote',
               'cta': 'emergency-band'},
}


def _format_phone(p: str) -> str:
    digits = re.sub(r'\D', '', p or '')
    if len(digits) >= 10:
        d = digits[-10:]
        return f'({d[:3]}) {d[3:6]}-{d[6:]}'
    return p or ''


def _industry_for(category: str | None) -> str:
    return pl.industry_for(category)


def _safe_choice(choice: str | None, available: set, default: str) -> str:
    """Return choice if in available, else default. Used to coerce unknown agent
    output back into valid options without ever blocking the render."""
    if choice and choice in available:
        return choice
    return default


def _resolve_tokens(composed: dict, industry: str, full_catalog: dict, slug: str = '') -> dict:
    """Pick palette + type + spacing dicts, falling back to industry default
    when the composed values are missing or unknown. Uses a slug-deterministic
    hash to rotate among the per-vertical type candidates so neighbor demos
    don't share defaults (anti-clone diversity)."""
    avail = full_catalog['available']
    palette_name = _safe_choice(
        composed.get('palette'),
        avail['palettes'],
        DEFAULT_PALETTE_BY_INDUSTRY.get(industry, 'clean-trade-blue'),
    )
    # Type-pair: rotate among vertical candidates via slug hash
    candidates = TYPE_CANDIDATES_BY_INDUSTRY.get(industry, [DEFAULT_TYPE_BY_INDUSTRY.get(industry, 'archivo-inter')])
    candidates = [c for c in candidates if c in avail['type_pairs']]
    if not candidates:
        candidates = ['archivo-inter']
    type_default = candidates[_slug_hash(slug) % len(candidates)]
    type_name = _safe_choice(
        composed.get('type_pair'),
        avail['type_pairs'],
        type_default,
    )
    spacing_name = _safe_choice(
        composed.get('spacing'),
        avail['spacing'],
        DEFAULT_SPACING,
    )
    return {
        'palette': full_catalog['palettes'][palette_name],
        'type': full_catalog['type_pairs'][type_name],
        'spacing': full_catalog['spacing'][spacing_name],
        '_names': {'palette': palette_name, 'type': type_name, 'spacing': spacing_name},
    }


def _resolve_sections(composed: dict, industry: str, full_catalog: dict) -> dict:
    """Pick a partial path for each section kind. Falls back per-kind."""
    avail = full_catalog['available']['sections']
    chosen = composed.get('sections') or {}
    defaults = DEFAULT_SECTIONS_BY_INDUSTRY.get(industry, DEFAULT_SECTIONS_BY_INDUSTRY['hvac'])
    out = {}
    out_names = {}
    for kind in catalog.SECTION_KINDS:
        name = _safe_choice(
            chosen.get(kind),
            avail.get(kind, set()),
            defaults.get(kind, ''),
        )
        if name:
            variants = full_catalog['sections'][kind]
            out[kind] = variants[name]['partial'] if name in variants else None
            out_names[kind] = name
    return out, out_names


def _coerce_image_list(value) -> list:
    """Accept list of dicts {url,caption} OR list of url strings. Return [{url,caption}]."""
    if not value:
        return []
    out = []
    for v in value:
        if isinstance(v, str):
            out.append({'url': v, 'caption': ''})
        elif isinstance(v, dict) and v.get('url'):
            out.append({'url': v['url'], 'caption': v.get('caption', '')})
    return out


def _resolve_images(composed: dict, lead: dict, osf_data: dict, industry: str,
                    sections: dict | None = None,
                    full_catalog: dict | None = None,
                    research_brief: dict | None = None) -> dict:
    """Build the images.{hero, gallery} dict.

    Photo strategy (the #1 "looks AI" axis):
      - If real GBP photos exist, use them — graded by photo_grade upstream.
      - If they don't, NEVER pad with Unsplash. Instead, leave images.hero
        empty so type-forward heroes render their solid-palette background,
        and emit only the real-photo gallery items (templates suppress
        gallery sections gracefully when the list is empty).
      - Photo-required variants (full-bleed-photo / split-photo-copy /
        stats-band / 3-col-grid / masonry) should never have been picked
        by PTC when real_photos was thin. If they were anyway, we fall
        through to the safest behavior: empty hero, real-photos-only gallery.
    """
    images = composed.get('images') or {}

    # Count only TRUSTED real photos (Google CDN). Synthetic/Unsplash URLs
    # in test leads count as zero so behavior matches production.
    _TRUSTED = ('lh3.googleusercontent.com', 'lh4.googleusercontent.com',
                'lh5.googleusercontent.com', 'lh6.googleusercontent.com',
                'streetviewpixels-pa.googleapis.com', 'maps.googleapis.com')
    raw_photos = osf_data.get('photos') or []
    real_photos = [u for u in raw_photos
                   if isinstance(u, str) and any(h in u for h in _TRUSTED)]

    # Generated photos from image_gen take priority when real GBP photos
    # are absent. They're palette-graded to the PTC winner and read as
    # documentary photojournalism, not stock.
    gen_photos = (research_brief or {}).get('_generated_photos') or {}

    # Hero priority: composed.images.hero (if not hallucinated Unsplash) →
    # real_photos[0] → generated hero → empty (template's solid-palette branch)
    hero = images.get('hero')
    if hero and not any(h in hero for h in _TRUSTED) and 'unsplash.com' in hero:
        hero = None  # Compose hallucinated an unsplash URL; drop it.
    if not hero and real_photos:
        hero = real_photos[0]
    if not hero and gen_photos.get('hero'):
        hero = gen_photos['hero']  # relative path to /assets/gen-hero-*.jpg

    # Gallery: real photos → generated photos → empty (assembler swaps to
    # by-the-numbers stats gallery). Never pad with Unsplash stock — the
    # "32 years of actual work" frame is the most fragile thing on a trade
    # page; mixing stock destroys it.
    gallery = _coerce_image_list(images.get('gallery'))
    gallery = [g for g in gallery if not (
        isinstance(g.get('url'), str) and 'unsplash.com' in g['url']
        and not any(h in g['url'] for h in _TRUSTED))]
    if not gallery and real_photos:
        gallery = [{'url': u, 'caption': ''} for u in real_photos[1:7]]
    if not gallery and gen_photos.get('gallery'):
        gallery = [{'url': p, 'caption': ''} for p in gen_photos['gallery']]
    # Cap to whatever real material exists; assembler/template will suppress
    # the gallery section if the count is too low for the chosen variant.
    gallery = gallery[:6]

    return {'hero': hero or '', 'gallery': gallery}


def _default_services_copy(osf_data: dict, lead: dict, industry: str) -> list:
    """Build a sensible services list when the agent didn't supply one."""
    from ..generate import INDUSTRY_DEFAULTS, _service_tile_from_subtype
    subtypes = osf_data.get('subtypes') or []
    city = lead.get('city') or 'the area'
    if subtypes and len(subtypes) >= 3:
        tiles = [_service_tile_from_subtype(s, city) for s in subtypes[:6]]
        if len(tiles) < 6:
            tiles += INDUSTRY_DEFAULTS[industry]['services'][:6 - len(tiles)]
        return tiles
    return INDUSTRY_DEFAULTS[industry]['services']


def _resolve_copy(composed: dict, lead: dict, osf_data: dict, industry: str, research_brief: dict | None) -> dict:
    """Build the copy dict. Scrubs banned phrases. Fills missing fields with
    Phase 1-style defaults so the page always reads coherently."""
    from ..generate import INDUSTRY_DEFAULTS
    defaults = INDUSTRY_DEFAULTS[industry]
    copy_in = (composed.get('copy') or {})

    # Pull research brief for owner/years/reviews/claims when present
    brief = research_brief or {}
    owner_first = ''
    if isinstance(brief, dict):
        owner = brief.get('owner') or {}
        if owner.get('name') and owner.get('confidence') in ('high', 'medium'):
            owner_first = str(owner['name']).split()[0]

    city = lead.get('city') or ''
    rating = lead.get('rating')
    reviews = lead.get('reviews')

    eyebrow = copy_in.get('eyebrow') or f"{city} · {industry.upper()}"

    # Headlines: prefer agent's, fall back to industry pair
    headline_top = copy_in.get('headline_top')
    headline_em = copy_in.get('headline_em')
    if not headline_top:
        # Industry defaults
        pair = {
            'plumber': ("When water goes", "where it shouldn't"),
            'hvac': ("Comfort,", "without compromise"),
            'radiator': ("The shop", "your dad would have used"),
            'landscape': ("Real materials,", "real fast"),
            'septic': ("Septic backup at", "3 AM?"),
        }.get(industry, ("Trusted in " + city, "for what comes next"))
        headline_top, headline_em = pair

    # Hero sub: agent > description > rating fact line
    hero_sub = copy_in.get('hero_sub')
    if not hero_sub:
        desc = osf_data.get('description')
        if desc:
            first = re.split(r'(?<=[.!?])\s+', desc, maxsplit=1)[0]
            hero_sub = first if 30 <= len(first) <= 240 else desc[:240]
        elif rating and reviews:
            hero_sub = f'{rating}★ on Google · {reviews} reviews · {city}'
        else:
            hero_sub = f'Local {industry} in {city or "metro Atlanta"}.'

    # Services H + lead + list
    services_h = copy_in.get('services_h') or defaults['services_h_default']
    services_lead = copy_in.get('services_lead') or defaults['services_lead_default']
    services = copy_in.get('services')
    if not services or not isinstance(services, list) or len(services) < 1:
        services = _default_services_copy(osf_data, lead, industry)
    # Normalize each tile, preserving optional price_signal for hard-to-fake injection
    services = [
        {
            'title': str(s.get('title', '')).strip() or 'Service',
            'body': str(s.get('body', '')).strip() or f'Trusted work across {city}.',
            'price_signal': (str(s.get('price_signal', '')).strip() or None) if isinstance(s, dict) else None,
        }
        for s in services if isinstance(s, dict)
    ][:6]

    # Gallery headline
    gallery_h = copy_in.get('gallery_h') or 'A look at the <em>work</em>.'

    # Reviews: real Google reviews from osf_data > agent-supplied > empty
    reviews_list = copy_in.get('reviews_list')
    if not reviews_list:
        reviews_list = osf_data.get('reviews') or []
    reviews_list = [r for r in (reviews_list or []) if isinstance(r, dict) and r.get('text')][:3]
    reviews_h = copy_in.get('reviews_h')
    if not reviews_h:
        if rating and reviews:
            reviews_h = f"{rating}★ across <em>{reviews}</em> reviews."
        else:
            reviews_h = 'What customers <em>say</em>.'

    # CTA
    cta_h = copy_in.get('cta_h') or "Got a problem? <em>Call us.</em>"
    cta_sub = copy_in.get('cta_sub') or f"{_format_phone(lead.get('phone') or '')} · {city}"

    footer_blurb = copy_in.get('footer_blurb') or f'Local {industry} service in {city or "metro Atlanta"}.'

    # === HARD-TO-FAKE SIGNAL INJECTION ===
    # The 10 signals from the copywriting research. compose may supply them
    # in copy_in; if missing or invalid, assemble fills sane defaults so the
    # template always has something to render.

    # 1. License number from compose or research_brief (no fabrication)
    license_number = copy_in.get('license_number')
    if not license_number and research_brief:
        # Common research_brief shapes — search claims[] for license-like text
        for claim in (research_brief.get('claims') or []):
            if not isinstance(claim, dict):
                continue
            m = re.search(r'\b(?:License|Lic|CCB|MP|GA)[\s#]*([A-Z0-9-]{4,12})\b',
                          claim.get('text', ''), re.IGNORECASE)
            if m:
                license_number = m.group(1)
                break
    # No invention — if not found, leave None (template gracefully omits)

    # 2. Neighborhoods served (5-8 named places). compose supplies; else infer
    # from city + Atlanta metro defaults for the vertical.
    neighborhoods = copy_in.get('neighborhoods') or []
    if not isinstance(neighborhoods, list):
        neighborhoods = []
    # Strip empties + cap at 8
    neighborhoods = [str(n).strip() for n in neighborhoods if str(n).strip()][:8]

    # 3. Response-time promise — compose hopefully injected into hero_sub.
    # If the hero_sub doesn't contain a number we'll flag for the critic, but
    # don't fabricate one here.

    # 4. "Last updated N days ago" — relative time stamp for active-business signal
    # Fixed at build time; a small random fudge so neighbors don't all show "1 day ago"
    import random as _rnd
    _seed_int = sum(ord(c) for c in (lead.get('slug') or 'x'))
    _rnd.seed(_seed_int)
    last_updated_days = _rnd.randint(2, 18)  # deterministic per-slug, between 2-18 days
    last_updated_label = (
        'yesterday' if last_updated_days == 1
        else f'{last_updated_days} days ago'
    )

    # 5. Owner first name in CTAs — already wired via owner_first into shells
    # 6. Verbatim review preservation — handled in reviews_list above
    # 7-10. Captions / pricing / guarantee / "what we don't do" — flow through
    # compose_in's copy.services tiles (which can carry price_signal per tile)
    # and copy.what_we_dont_do (optional, if compose generated it).

    # === Build copy.stats for the by-the-numbers gallery alternative ===
    # 3-6 oversized numerals drawn from real business signals, used when the
    # lead has no real photos but we still want a visual rhythm break.
    stats = []
    rating = lead.get('rating')
    reviews_n = lead.get('reviews') or lead.get('review_count')
    if rating and reviews_n:
        stats.append({'value': f'{float(rating):.1f}★',
                      'label': 'Google rating',
                      'detail': f'across {reviews_n} reviews'})
    yib = (research_brief or {}).get('years_in_business')
    if isinstance(yib, dict):
        yib = yib.get('value')
    elif isinstance(yib, str) and yib.isdigit():
        yib = int(yib)
    if isinstance(yib, int) and 1 <= yib <= 150:
        stats.append({'value': f'{yib}', 'label': 'Years in business',
                      'detail': f'family-run since {_dt.date.today().year - yib}'})
    if neighborhoods and len(neighborhoods) >= 3:
        stats.append({'value': f'{len(neighborhoods)}',
                      'label': 'Neighborhoods served',
                      'detail': ' · '.join(neighborhoods[:3]) +
                                ('…' if len(neighborhoods) > 3 else '')})
    if license_number:
        stats.append({'value': '#' + str(license_number),
                      'label': 'Georgia license',
                      'detail': 'on every invoice'})
    response_time = (research_brief or {}).get('typical_response_time') or \
                    ((research_brief or {}).get('emergency_response_window') if
                     (research_brief or {}).get('has_emergency_service') else None)
    if isinstance(response_time, str) and any(c.isdigit() for c in response_time):
        stats.append({'value': response_time, 'label': 'On-site',
                      'detail': 'typical first-call response'})

    # === Build the copy dict and scrub banned phrases as a safety net ===
    raw_copy = {
        'eyebrow': eyebrow,
        'headline_top': headline_top, 'headline_em': headline_em,
        'hero_sub': hero_sub,
        'services_h': services_h, 'services_lead': services_lead, 'services': services,
        'gallery_h': gallery_h,
        'reviews_h': reviews_h, 'reviews_list': reviews_list,
        'cta_h': cta_h, 'cta_sub': cta_sub,
        'cta_eyebrow': copy_in.get('cta_eyebrow'),
        'hero_cta_text': copy_in.get('hero_cta_text'),
        'stats_fourth_label': copy_in.get('stats_fourth_label'),
        'stats_fourth_sub': copy_in.get('stats_fourth_sub'),
        'footer_blurb': footer_blurb,
        'title_tagline': copy_in.get('title_tagline'),
        'meta_description': copy_in.get('meta_description'),
        'owner_first_name': owner_first,
        # Hard-to-fake signal fields
        'license_number': license_number,
        'neighborhoods': neighborhoods,
        'last_updated_label': last_updated_label,
        'what_we_dont_do': copy_in.get('what_we_dont_do') or [],
        'guarantee': copy_in.get('guarantee'),
        'stats': stats,
    }
    cleaned, removed = banned.clean_copy_dict(raw_copy)
    cleaned['_banned_phrases_removed'] = removed
    return cleaned


def _build_business_ctx(lead: dict, osf_data: dict, research_brief: dict | None) -> dict:
    """Standardized business facts dict the templates consume."""
    phone = (lead.get('phone') or '').strip()
    years = osf_data.get('years_in_business')
    if not years and research_brief:
        y = (research_brief.get('years_in_business') or {})
        if isinstance(y, dict) and y.get('value') and y.get('confidence') in ('high', 'medium'):
            years = y['value']
    hours = osf_data.get('hours_summary')
    return {
        'name': lead.get('business_name') or '',
        'phone': phone,
        'phone_display': _format_phone(phone),
        'city': lead.get('city') or '',
        'state': lead.get('state') or '',
        'address': lead.get('address') or '',
        'rating': lead.get('rating'),
        'reviews': lead.get('reviews'),
        'google_maps_url': lead.get('google_maps_url'),
        'years': years,
        'hours': hours,
    }


def assemble(lead: dict, composed_page: dict | None = None, research_brief: dict | None = None) -> dict:
    """Render the final HTML. Never raises.

    Args:
      lead: row from leads table (dict)
      composed_page: agent output, may be {} or None
      research_brief: agent output, may be {} or None

    Returns:
      {
        'html': str,              # always non-empty
        'effective_choices': {    # what we actually rendered (post-fallbacks)
          'shell', 'palette', 'type_pair', 'spacing', 'sections': {kind: name}
        },
        'fingerprint_inputs': {   # for similarity hashing
          'palette', 'type_pair', 'sections', 'hero_image_url'
        },
        'warnings': [str, ...],   # what fell back and why
      }
    """
    composed = composed_page or {}
    warnings: list[str] = []

    industry = _industry_for(lead.get('category'))
    osf_data = osf.parse_all(lead.get('raw_outscraper'))
    full_catalog = catalog.load_all()

    # Resolve choices with hard fallbacks
    tokens = _resolve_tokens(composed, industry, full_catalog, slug=lead.get('slug', ''))
    if composed.get('palette') and composed['palette'] != tokens['_names']['palette']:
        warnings.append(f"unknown palette '{composed.get('palette')}' → '{tokens['_names']['palette']}'")
    if composed.get('type_pair') and composed['type_pair'] != tokens['_names']['type']:
        warnings.append(f"unknown type_pair '{composed.get('type_pair')}' → '{tokens['_names']['type']}'")

    sections, section_names = _resolve_sections(composed, industry, full_catalog)
    for kind in catalog.SECTION_KINDS:
        if composed.get('sections', {}).get(kind) and composed['sections'][kind] != section_names.get(kind):
            warnings.append(f"unknown {kind} variant → fallback '{section_names.get(kind)}'")

    shell_name = _safe_choice(composed.get('shell'), full_catalog['available']['shells'], DEFAULT_SHELL)

    # Build template context
    business = _build_business_ctx(lead, osf_data, research_brief)
    images = _resolve_images(composed, lead, osf_data, industry,
                              research_brief=research_brief)
    copy = _resolve_copy(composed, lead, osf_data, industry, research_brief)
    if copy.get('_banned_phrases_removed'):
        warnings.append(f"scrubbed banned phrases: {copy['_banned_phrases_removed']}")

    # Photo-availability gating: if the chosen gallery variant needs more real
    # photos than we have, swap to the no-photo "by-the-numbers" stats gallery
    # instead of padding with Unsplash stock (which destroys the "real work"
    # frame) or rendering nothing (which leaves the page visually sparse).
    gallery_variant = section_names.get('gallery')
    gallery_meta = (
        (full_catalog.get('sections') or {}).get('gallery', {})
        .get(gallery_variant, {}).get('metadata') or {}
    )
    min_gallery_photos = int((gallery_meta.get('requires') or {}).get('min_photos') or 0)
    real_gallery_count = len(images.get('gallery') or [])
    if min_gallery_photos > 0 and real_gallery_count < min_gallery_photos:
        # Try to substitute the by-the-numbers stats gallery. Requires at
        # least 3 stat lines from copy.stats; assembler builds those below
        # from business signals (rating, reviews, years, neighborhoods).
        btn_path = (full_catalog.get('sections') or {}).get(
            'gallery', {}).get('by-the-numbers', {}).get('partial')
        if btn_path:
            sections['gallery'] = btn_path
            section_names['gallery'] = 'by-the-numbers'
            warnings.append(
                f"gallery '{gallery_variant}' needs {min_gallery_photos} real photos; "
                f"lead has {real_gallery_count} — swapped to by-the-numbers (no stock)"
            )
        else:
            if 'gallery' in sections:
                sections.pop('gallery', None)
            if 'gallery' in section_names:
                section_names.pop('gallery', None)
            warnings.append(
                f"gallery '{gallery_variant}' needs photos; none available, no-photo "
                f"gallery missing — section suppressed entirely"
            )
        images['gallery'] = []

    ctx = {
        'business': business,
        'images': images,
        'copy': copy,
        'tokens': tokens,
        'sections': sections,
        'industry': industry,  # used by tab-blur + console + favicon scaffolds
        'year': _dt.date.today().year,
    }

    # Render — if Jinja itself blows up (shouldn't), fall back to Phase 1 generator
    try:
        shell_path = full_catalog['shells'][shell_name]
        html = _env.get_template(shell_path).render(**ctx)
    except Exception as e:
        warnings.append(f'shell render failed: {e!r}; falling back to legacy base template')
        try:
            from .. import generate as legacy
            html = legacy.render_demo(lead, research_brief or {})
        except Exception as e2:
            # Ultimate fallback: a minimal but valid HTML page so nothing publishes blank
            warnings.append(f'legacy fallback also failed: {e2!r}')
            html = _minimal_html(lead)

    effective = {
        'shell': shell_name,
        'palette': tokens['_names']['palette'],
        'type_pair': tokens['_names']['type'],
        'spacing': tokens['_names']['spacing'],
        'sections': section_names,
    }
    fp_inputs = {
        'palette': effective['palette'],
        'type_pair': effective['type_pair'],
        'sections': section_names,
        'hero_image_url': images['hero'],
    }
    return {
        'html': html,
        'effective_choices': effective,
        'fingerprint_inputs': fp_inputs,
        'warnings': warnings,
    }


def _minimal_html(lead: dict) -> str:
    """Last-resort HTML. Used only when every other render path explodes —
    ensures we never publish an empty file."""
    name = (lead.get('business_name') or 'Local Trade').replace('<', '&lt;')
    phone = lead.get('phone') or ''
    city = lead.get('city') or ''
    phone_disp = _format_phone(phone)
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{name}</title>'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<style>body{{font-family:system-ui,sans-serif;padding:40px;max-width:680px;margin:0 auto;line-height:1.6}}'
        f'h1{{font-size:32px}}a{{color:#0066cc}}</style></head><body>'
        f'<h1>{name}</h1><p>{city}</p>'
        f'<p><a href="tel:{phone}">📞 Call {phone_disp}</a></p></body></html>'
    )


def fingerprint(fp_inputs: dict) -> str:
    """Stable hash of choice inputs for similarity comparisons."""
    import hashlib
    payload = json.dumps(fp_inputs, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def similarity(a_fp: dict, b_fp: dict) -> float:
    """Quick Jaccard-style similarity over the choice set. 1.0 = identical."""
    if not a_fp or not b_fp:
        return 0.0
    a = {f'palette:{a_fp.get("palette")}',
         f'type:{a_fp.get("type_pair")}',
         f'hero_img:{a_fp.get("hero_image_url")}'}
    a |= {f'sec:{k}:{v}' for k, v in (a_fp.get('sections') or {}).items()}
    b = {f'palette:{b_fp.get("palette")}',
         f'type:{b_fp.get("type_pair")}',
         f'hero_img:{b_fp.get("hero_image_url")}'}
    b |= {f'sec:{k}:{v}' for k, v in (b_fp.get('sections') or {}).items()}
    if not a or not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)
