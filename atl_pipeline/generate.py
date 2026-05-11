"""Render demo HTML from research payload + verified Outscraper data."""
import os, re, json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, select_autoescape
from . import photo_library as pl
from . import outscraper_fields as osf

TPL_DIR = Path(__file__).parent / 'templates'

env = Environment(
    loader=FileSystemLoader(TPL_DIR),
    autoescape=select_autoescape(['html'])
)

INDUSTRY_DEFAULTS = {
    'plumber': {
        'accent': '#0E2238', 'ink': '#0E2238',
        'services': [
            {'title': 'Leak detection', 'body': 'Acoustic + thermal pinpointing — find the leak without breaking walls.'},
            {'title': 'Repipe', 'body': 'Whole-home repipes when galvanized or polybutylene needs to go. Fixed price, no surprises.'},
            {'title': 'Water heaters', 'body': 'Tank, tankless, conversions. Right-sized, not oversold.'},
            {'title': 'Drain cleaning', 'body': 'Snake, hydro-jet, camera scope. We show you the video before quoting.'},
            {'title': 'Emergency service', 'body': 'Real technicians on call. Two-hour response across metro Atlanta.'},
            {'title': 'Remodels', 'body': 'Bathroom rough-ins, fixture upgrades, gas lines. Working with your contractor or directly.'},
        ],
        'services_h_default': 'Plumbing done <em>right</em>.',
        'services_lead_default': 'Repair, repipe, water heaters, drains, emergencies. Same-day across metro Atlanta.',
    },
    'hvac': {
        'accent': '#C9A961', 'ink': '#0A1F3F',
        'services': [
            {'title': 'Installation', 'body': 'Manual J load calcs. Right-sized equipment. Clean, square ductwork.'},
            {'title': 'Repair', 'body': 'Diagnostic-first, parts-second. We tell you the truth about your system.'},
            {'title': 'Maintenance', 'body': 'Twice-yearly tune-ups that catch the $80 problem before the $4,000 emergency.'},
            {'title': 'Smart-home', 'body': 'Nest, ecobee, Sensi, Honeywell — wired and programmed correctly.'},
            {'title': 'Indoor air quality', 'body': 'HEPA, UV, dehumidification. Especially for Atlanta humidity.'},
            {'title': '24/7 emergency', 'body': 'Real technicians dispatched within two hours.'},
        ],
        'services_h_default': 'Comfort done <em>right</em>.',
        'services_lead_default': 'Heating, cooling, IAQ. Diagnostic-first, transparent pricing, no subcontractors.',
    },
    'radiator': {
        'accent': '#D4663E', 'ink': '#1B2530',
        'services': [
            {'title': 'Auto radiators', 'body': 'Domestic, foreign, fleet, diesel. Repair when it can be repaired.'},
            {'title': 'Classic recoring', 'body': 'Copper-brass radiators. We rebuild what chain shops won\'t look at.'},
            {'title': 'Industrial cooling', 'body': 'Heavy equipment, generators, ag, marine.'},
            {'title': 'Heater cores', 'body': 'Save $1,500 on dashboard removals — we rebuild and ship back.'},
            {'title': 'Vintage A/C', 'body': 'R-12 to R-134a conversions for classic cars.'},
            {'title': 'Custom hose fittings', 'body': 'Hose blew, dealer doesn\'t carry it? We fabricate. Same day.'},
        ],
        'services_h_default': 'One trade. <em>Mastered.</em>',
        'services_lead_default': 'We don\'t do oil changes. Every radiator gets the same know-how.',
    },
    'landscape': {
        'accent': '#C85A3A', 'ink': '#2A1B0E',
        'services': [
            {'title': 'Long-leaf pine straw', 'body': 'Premium long-leaf needles. Golden, fragrant, longer-lasting.'},
            {'title': 'Hardwood mulch', 'body': 'Triple-shredded, holds color through the season.'},
            {'title': 'Sod & install', 'body': 'Fresh-cut Bermuda, Zoysia, or Fescue — same-day delivery.'},
            {'title': 'Crew installation', 'body': 'We can lay it for you with 2-3 days notice.'},
            {'title': 'Bulk delivery', 'body': 'Contractor pricing, same-day for orders over 50 bales.'},
            {'title': 'Yard refreshes', 'body': 'Spring + fall packages. Quote in 24 hours.'},
        ],
        'services_h_default': 'Real materials. <em>Real fast.</em>',
        'services_lead_default': 'Pine straw, mulch, sod. Same-day quotes, fast delivery.',
    },
    'septic': {
        'accent': '#FF6B35', 'ink': '#0F1F2E',
        'services': [
            {'title': 'Septic pumping', 'body': 'Routine pumping. Tank locating. Lid replacement.'},
            {'title': 'Backup emergencies', 'body': '3am sewage in the basement? We\'re on the way.'},
            {'title': 'Drain field repair', 'body': 'Failing field, slow drains, soggy yard. We diagnose and repair.'},
            {'title': 'Plumbing repair', 'body': 'Leaks, clogs, water heaters, fixtures, gas lines.'},
            {'title': 'Inspections', 'body': 'Pre-purchase. Written reports. Insurance & lender accepted.'},
            {'title': 'New installs', 'body': 'Permitting, install, inspection — full pipeline.'},
        ],
        'services_h_default': '<em>Plumbing.</em> Septic. Both. Always.',
        'services_lead_default': 'One call, one crew. 24/7 dispatch across the county.',
    },
}

_SUBTYPE_STRIP = re.compile(r'\b(contractor|service|services|company|shop)\b', re.IGNORECASE)


def _service_tile_from_subtype(subtype, city):
    """Turn a raw Google subcategory ('Air conditioning repair service') into a
    {title, body} tile. Title strips the boilerplate words; body stays specific."""
    title = _SUBTYPE_STRIP.sub('', subtype).strip().rstrip('-').title()
    if not title:
        title = subtype.title()
    body = f"{subtype.strip()} — serving {city or 'metro Atlanta'} and surrounding."
    return {'title': title, 'body': body}


def build_context(lead, research):
    """Merge Outscraper lead row + research payload into a render context.

    Priority order for personalized fields:
      1. raw_outscraper (Google Business Profile data we already paid for)
      2. research payload (Brave Search + Haiku synthesis)
      3. industry defaults (boilerplate)
    """
    industry = pl.industry_for(lead.get('category'))
    defaults = INDUSTRY_DEFAULTS[industry]

    # Pull rich GBP fields from raw_outscraper (photos, reviews, hours, etc.)
    osf_data = osf.parse_all(lead.get('raw_outscraper'))

    # Phone display
    phone = (lead.get('phone') or '').strip()
    phone_display = re.sub(r'\D', '', phone)
    if len(phone_display) >= 10:
        phone_display = f'({phone_display[-10:-7]}) {phone_display[-7:-4]}-{phone_display[-4:]}'
    else:
        phone_display = phone

    owner = (research.get('owner_name') or 'unknown') if research else 'unknown'
    owner_first = owner.split()[0] if owner and owner != 'unknown' else None

    accent = (research.get('brand_colors') or [defaults['accent']])[0]

    headline_top = f"Trusted in {lead.get('city') or 'town'}"
    headline_em = "for what comes next"
    if industry == 'plumber':
        headline_top, headline_em = ("When water goes", "where it shouldn't")
    elif industry == 'hvac':
        headline_top, headline_em = ("Comfort,", "without compromise")
    elif industry == 'radiator':
        headline_top, headline_em = ("The shop", "your dad would have used")
    elif industry == 'landscape':
        headline_top, headline_em = (f"Real materials,", "real fast")
    elif industry == 'septic':
        headline_top, headline_em = ("Septic backup at", "3 AM?")

    if research and research.get('tagline_options'):
        tag = research['tagline_options'][0]
        # Best-effort split
        if '.' in tag:
            parts = tag.split('.', 1)
            headline_top, headline_em = parts[0].strip(), parts[1].strip(' .')

    # Hero subhead: prefer the owner's own Google description (first sentence),
    # then research vibe, then a fact line. Track whether we consumed the
    # description here so the About section doesn't echo it.
    hero_sub = ''
    description_used_in_hero = False
    desc = osf_data['description']
    if desc:
        first = re.split(r'(?<=[.!?])\s+', desc, maxsplit=1)[0]
        if 30 <= len(first) <= 240:
            hero_sub = first
        else:
            hero_sub = desc[:240].rstrip() + ('…' if len(desc) > 240 else '')
        description_used_in_hero = True
    if not hero_sub and research and research.get('vibe'):
        hero_sub = research['vibe']
    if not hero_sub:
        rating_bit = f"{lead['rating']}★ on Google" if lead.get('rating') else ''
        review_bit = f"{lead['reviews']} reviews" if lead.get('reviews') else ''
        city_bit = lead.get('city') or ''
        hero_sub = ' · '.join([b for b in [rating_bit, review_bit, city_bit] if b])

    # Years in business: Google data > research guess
    years = osf_data['years_in_business'] or (research or {}).get('years_in_business_claim')

    about_h = f"{lead['business_name']}, <em>{lead.get('city') or ''}</em>."
    about_body = ''
    # If we have a description that's long enough AND we didn't already use it
    # as the hero subhead (or used only the first sentence), use the rest here
    if desc and len(desc) >= 80:
        if description_used_in_hero and len(desc) > len(hero_sub) + 40:
            # Use the remainder past the hero's first sentence
            remainder = desc[len(hero_sub):].lstrip(' .…')
            if len(remainder) >= 40:
                about_body = remainder
        elif not description_used_in_hero:
            about_body = desc
    if not about_body and research and research.get('owner_name') and research['owner_name'] != 'unknown':
        if years:
            about_body = f"{owner_first} has run {lead['business_name']} for {years} years. Same trade, same town."
        else:
            about_body = f"{owner_first} runs {lead['business_name']} hands-on — every call, every customer."
    if not about_body and years:
        about_body = f"{lead['business_name']} has served {lead.get('city') or 'metro Atlanta'} for {years} years."
    if not about_body:
        about_body = f"Local-owned and run from {lead.get('city') or 'metro Atlanta'}. Family work, family standards."

    # Hero image: prefer a real Google photo over Unsplash stock
    real_photos = osf_data['photos']
    if real_photos:
        hero_img = real_photos[0]
    else:
        hero_img = f'https://images.unsplash.com/photo-{pl.HEROES[industry]}?auto=format&fit=crop&w=1600&q=80'

    # Gallery: prefer real photos (skip the one used as hero), fall back to industry stock
    if len(real_photos) >= 4:
        gallery_photos = real_photos[1:7] if len(real_photos) > 6 else real_photos[1:]
        # If we sliced down to fewer than 4 (small photo set), pad with industry stock
        if len(gallery_photos) < 4:
            stock = [pl.img_url(pid) for pid, _ in pl.PHOTOS[industry]]
            gallery_photos = gallery_photos + [u for u in stock if u not in gallery_photos]
        gallery = [{'url': u, 'caption': ''} for u in gallery_photos[:6]]
    else:
        gallery = [
            {'url': pl.img_url(pid), 'caption': cap}
            for pid, cap in pl.PHOTOS[industry]
        ]

    # Service tiles: Google subtypes (most specific) > research specialties > industry defaults
    subtypes = osf_data['subtypes']
    services = research.get('specialties') if research else None
    city_for_body = lead.get('city') or 'the area'
    if subtypes and len(subtypes) >= 3:
        services_tiles = [_service_tile_from_subtype(s, city_for_body) for s in subtypes[:6]]
        # Pad with industry defaults if we got fewer than 6
        if len(services_tiles) < 6:
            services_tiles = services_tiles + defaults['services'][: 6 - len(services_tiles)]
    elif services and len(services) >= 4:
        services_tiles = [
            {'title': s.title(), 'body': f"Trusted {s.lower()} work across {city_for_body}."}
            for s in services[:6]
        ]
    else:
        services_tiles = defaults['services']

    # Reviews: real Google reviews from Outscraper beat Brave-Search-scraped fragments
    real_reviews = osf_data['reviews']
    if real_reviews:
        reviews_list = real_reviews[:3]
    else:
        reviews_list = (research or {}).get('real_reviews') or []
        reviews_list = [r for r in reviews_list if r.get('text')][:3]

    return {
        'business_name': lead['business_name'],
        'tagline': (research.get('tagline_options') or [defaults['services_h_default']])[0] if research else defaults['services_h_default'],
        'phone': phone, 'phone_display': phone_display,
        'city': lead.get('city') or '', 'state': lead.get('state') or '',
        'address': lead.get('address') or '',
        'rating': lead.get('rating'),
        'reviews': lead.get('reviews'),
        'google_maps_url': lead.get('google_maps_url'),
        'hours': osf_data['hours_summary'] or (research.get('hours') if research else None),
        'years_in_business': years,
        'same_day': 'Same-day' if industry in ('plumber','hvac','septic') else None,
        'availability_lbl': 'service across metro',
        'owner_name': owner, 'owner_first_name': owner_first,
        'accent': accent, 'ink': defaults['ink'],
        'bg': '#FAFAF7', 'bg_blur': 'rgba(250,250,247,.85)',
        'hero_img': hero_img,
        'eyebrow': f"{lead.get('city')} · {industry.upper()}",
        'headline_top': headline_top, 'headline_em': headline_em, 'headline_bottom': '',
        'hero_sub': hero_sub,
        'services_h': defaults['services_h_default'],
        'services_lead': defaults['services_lead_default'],
        'services': services_tiles,
        'about_h': about_h, 'about_body': about_body,
        'wow_facts': (research or {}).get('wow_facts') or [],
        'gallery': gallery,
        'reviews_list': reviews_list,
        'cta_h': f"Got a problem? <em>Call us.</em>",
        'cta_sub': f"{phone_display} · {lead.get('city') or ''}",
        'footer_blurb': f"Local {industry} service in {lead.get('city') or 'metro Atlanta'}.",
    }

def render_demo(lead, research):
    ctx = build_context(lead, research)
    tpl = env.get_template('base.html.j2')
    return tpl.render(**ctx)
