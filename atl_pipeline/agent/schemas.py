"""Validators for the two structured outputs the agent produces.

We don't pull in pydantic — these are small focused validators that raise
ValueError with actionable messages.
"""
from __future__ import annotations


# -----------------------------------------------------------------------------
# research_brief.json
# -----------------------------------------------------------------------------

RESEARCH_BRIEF_REQUIRED = {
    'owner', 'years_in_business', 'claims', 'real_reviews', 'photos',
    'service_specifics', 'customer_segment', 'buyer_psychology', 'vibe_tags',
    'sources_visited',
}

CUSTOMER_SEGMENTS = {
    'homeowner-residential', 'property-manager', 'commercial', 'mixed', 'unknown',
}
BUYER_PSYCHOLOGY = {
    'price-sensitive', 'quality-first', 'speed-first', 'trust-first',
    'tradition-first', 'unknown',
}
CONFIDENCE = {'high', 'medium', 'low', 'unknown'}


def validate_research_brief(brief: dict) -> list[str]:
    """Return list of error strings. Empty list = valid."""
    errs = []
    if not isinstance(brief, dict):
        return ['brief must be a dict']
    for k in RESEARCH_BRIEF_REQUIRED:
        if k not in brief:
            errs.append(f'missing key: {k}')

    owner = brief.get('owner') or {}
    if not isinstance(owner, dict):
        errs.append('owner must be a dict')
    else:
        if owner.get('confidence') not in CONFIDENCE:
            errs.append(f'owner.confidence must be one of {CONFIDENCE}')
        if not isinstance(owner.get('sources', []), list):
            errs.append('owner.sources must be a list')

    yib = brief.get('years_in_business') or {}
    if not isinstance(yib, dict):
        errs.append('years_in_business must be a dict')

    claims = brief.get('claims') or []
    if not isinstance(claims, list):
        errs.append('claims must be a list')
    else:
        for i, c in enumerate(claims):
            if not isinstance(c, dict):
                errs.append(f'claims[{i}] must be a dict')
                continue
            if 'text' not in c:
                errs.append(f'claims[{i}].text missing')
            if 'sources' not in c or not isinstance(c['sources'], list):
                errs.append(f'claims[{i}].sources missing or not a list')
            if not c.get('sources'):
                errs.append(f'claims[{i}] has no sources — uncited claim')

    reviews = brief.get('real_reviews') or []
    if not isinstance(reviews, list):
        errs.append('real_reviews must be a list')

    photos = brief.get('photos') or []
    if not isinstance(photos, list):
        errs.append('photos must be a list')
    else:
        for i, p in enumerate(photos):
            if not isinstance(p, dict) or not p.get('url'):
                errs.append(f'photos[{i}] missing url')

    if brief.get('customer_segment') not in CUSTOMER_SEGMENTS:
        errs.append(f'customer_segment must be one of {CUSTOMER_SEGMENTS}')
    if brief.get('buyer_psychology') not in BUYER_PSYCHOLOGY:
        errs.append(f'buyer_psychology must be one of {BUYER_PSYCHOLOGY}')

    vibe = brief.get('vibe_tags') or []
    if not isinstance(vibe, list):
        errs.append('vibe_tags must be a list')

    srcs = brief.get('sources_visited') or []
    if not isinstance(srcs, list):
        errs.append('sources_visited must be a list')

    return errs


def is_brief_thin(brief: dict) -> bool:
    """Return True if the brief has too little signal to compose a good page.

    Threshold: must have at least 2 of {real_reviews≥1, photos≥1, claims≥2,
    service_specifics≥2}.
    """
    if not isinstance(brief, dict):
        return True
    score = 0
    if len(brief.get('real_reviews') or []) >= 1:
        score += 1
    if len(brief.get('photos') or []) >= 1:
        score += 1
    if len(brief.get('claims') or []) >= 2:
        score += 1
    if len(brief.get('service_specifics') or []) >= 2:
        score += 1
    return score < 2


# -----------------------------------------------------------------------------
# composed_page.json — the composition agent's output
# -----------------------------------------------------------------------------

COMPOSED_REQUIRED = {
    'shell', 'palette', 'type_pair', 'spacing', 'sections', 'copy',
    'images', 'fingerprint_inputs',
}


def validate_composed_page(page: dict, available: dict) -> list[str]:
    """Validate the composed_page structure AND check section/palette/type
    choices exist in the catalog.

    `available` is the catalog dict produced by `catalog.load_all()`:
      {'shells': set, 'palettes': set, 'type_pairs': set, 'spacing': set,
       'sections': {kind: set(names)}}
    """
    errs = []
    if not isinstance(page, dict):
        return ['composed_page must be a dict']
    for k in COMPOSED_REQUIRED:
        if k not in page:
            errs.append(f'missing key: {k}')

    if page.get('shell') and page['shell'] not in available.get('shells', set()):
        errs.append(f"unknown shell: {page['shell']}")
    if page.get('palette') and page['palette'] not in available.get('palettes', set()):
        errs.append(f"unknown palette: {page['palette']}")
    if page.get('type_pair') and page['type_pair'] not in available.get('type_pairs', set()):
        errs.append(f"unknown type_pair: {page['type_pair']}")
    if page.get('spacing') and page['spacing'] not in available.get('spacing', set()):
        errs.append(f"unknown spacing: {page['spacing']}")

    sections = page.get('sections') or {}
    if not isinstance(sections, dict):
        errs.append('sections must be a dict {kind: variant_name}')
    else:
        avail_sections = available.get('sections', {})
        for kind, name in sections.items():
            if kind not in avail_sections:
                errs.append(f'unknown section kind: {kind}')
            elif name not in avail_sections[kind]:
                errs.append(f'unknown {kind} variant: {name}')

    copy = page.get('copy') or {}
    if not isinstance(copy, dict):
        errs.append('copy must be a dict')

    images = page.get('images') or {}
    if not isinstance(images, dict):
        errs.append('images must be a dict')

    return errs
