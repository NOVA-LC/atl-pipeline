"""Hand-composed end-to-end v4 demo.

Since Anthropic API calls 401 in this sandbox (proxy doesn't auth third-
party scripts), this script SIMULATES what a successful Tier 4 pipeline
output would look like for our synthetic plumber lead by:

  1. Hand-crafting the `composed` dict to match what compose() would
     have produced AFTER design-PTC pinned the heritage-navy-gold +
     lora-inter design, with full Tier 2 hard-to-fake signals.
  2. Running the real assemble.py to render final HTML — this exercises
     ALL the deterministic structural work (motion lib, signals, layout
     variants, photo grading, sticky CTA, trust strip, footer license #,
     last-updated, etc.).

The output is what the new pipeline produces visually when its inputs
all land cleanly. The LLM-driven copy quality (headline factory voice
critic etc.) is NOT exercised here — that requires a working API key.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from atl_pipeline.agent import assemble


LEAD = {
    'id': 9999,
    'business_name': "Peach State Plumbing & Drain",
    'category': 'Plumber',
    'city': 'Marietta',
    'state': 'GA',
    'phone': '(770) 555-0184',
    'rating': 4.9,
    'reviews': 187,
    'website_url': 'https://peachstateplumbing.example.com',
    'address': '1840 Roswell St, Marietta, GA 30062',
}

RESEARCH_BRIEF = {
    'owner': {'name': 'Joey Calloway', 'role': 'owner-operator'},
    'years_in_business': {'value': 32, 'confidence': 0.9},
    'license_number': 'MP207455',
    'service_area_neighborhoods': [
        'East Cobb', 'Smyrna', 'Vinings', 'Sandy Springs', 'Roswell', 'Powers Ferry'
    ],
    'photos': [
        'https://images.unsplash.com/photo-1581578731548-c64695cc6952?w=1600&q=80',
        'https://images.unsplash.com/photo-1607472586893-edb57bdc0e39?w=1600&q=80',
        'https://images.unsplash.com/photo-1558618666-fcd25c85cd64?w=1600&q=80',
        'https://images.unsplash.com/photo-1581094794329-c8112a89af12?w=1600&q=80',
        'https://images.unsplash.com/photo-1607400201515-c2c41c07d307?w=1600&q=80',
        'https://images.unsplash.com/photo-1582719471384-894fbb16e074?w=1600&q=80',
    ],
}

# === This is what compose() would have returned with PTC pinning ===
# heritage-navy-gold palette + lora-inter type pair (editorial serif
# fits the "32 years in business" heritage angle). Sections chosen to
# differ from neighbors on at least 2 axes.
COMPOSED = {
    'palette': 'heritage-navy-gold',
    'type_pair': 'lora-inter',
    'sections': {
        'hero': 'minimal-type',          # not split-photo (neighbors used that)
        'services': 'numbered-grid',     # not icon-cards (template tell)
        'gallery': 'masonry',
        'reviews': 'full-width-quote',   # not card-grid (more committed)
        'cta': 'phone-prominent',
    },
    'shell': 'standard',
    'copy': {
        'eyebrow': 'East Cobb · since 1993',
        'headline_top': "Joey's been fixing pipes",
        'headline_em': 'in East Cobb since 1993.',
        'hero_sub': "Owner-operated. License # on every invoice. We quote on the phone — the price you hear is the price you pay. Camera-jet diagnostics with the recording emailed to you.",
        'hero_cta_text': "Call Joey: (770) 555-0184",

        'services_h': "What we <em>actually</em> do.",
        'services_lead': "We don't pretend to do everything. Three things, done by Joey or his son Caleb — no apprentices on your job.",
        'services': [
            {'title': 'Drain & sewer cleaning',
             'body': "Camera-jet with the recording emailed to you. Most clogs done in under 90 minutes.",
             'price_signal': 'flat $189'},
            {'title': 'Water heater service',
             'body': "Flush, repair, or replace. We'll tell you if yours has another 2-3 years in it. Honest answer, even when the new one would pay us more.",
             'price_signal': 'from $349'},
            {'title': 'Slab leak detection & repair',
             'body': "Electronic locator + thermal imaging. Most leaks pinpointed in under 30 min.",
             'price_signal': 'from $1,890 flat'},
        ],

        'gallery_h': "32 years of <em>actual</em> work.",

        'reviews_h': "What East Cobb says.",
        'reviews_list': [
            {'author': 'Margaret W.', 'rating': 5,
             'text': "Joey himself came out Saturday at 11pm when my main line backed up. Quoted me before he started, no surprise charges. Camera-jetted the line and emailed me the recording. Done in 90 minutes.",
             'date': '3 weeks ago', 'source': 'Google'},
            {'author': 'David T.', 'rating': 5,
             'text': "Third plumber I called after two no-shows. Peach State picked up on the first ring, was at my house in 45 min, and the price he quoted on the phone is the price I paid. License # right on the truck.",
             'date': '2 months ago', 'source': 'Google'},
            {'author': 'Sarah K.', 'rating': 5,
             'text': "32 years of doing this shows. Joey diagnosed a slab leak in 10 minutes that another company quoted me $4,500 to find. His repair: $1,890 flat.",
             'date': '4 months ago', 'source': 'Google'},
        ],

        'cta_eyebrow': "Phone rings to Joey or Caleb.",
        'cta_h': "Got a problem? <em>Call us.</em>",
        'cta_sub': "(770) 555-0184 · East Cobb, Marietta, Smyrna, Vinings",

        # === Tier 2 hard-to-fake signals ===
        'license_number': 'MP207455',
        'neighborhoods': ['East Cobb', 'Smyrna', 'Vinings', 'Sandy Springs', 'Roswell', 'Powers Ferry'],
        'what_we_dont_do': [
            "We don't quote on the phone for slab leaks — we have to find it first.",
            "We don't use apprentices. Joey does the work, or Caleb does.",
            "We don't do new construction. Repairs and renovations only.",
        ],
        'guarantee': "If we can't quote the job in one phone call or one site visit, your service call is free.",

        'footer_blurb': "Joey Calloway, MP207455. Family-owned in East Cobb since 1993. We answer the phone and put the license number on every invoice.",
        'stats_fourth_label': '32 yrs',
        'stats_fourth_sub': "since '93",

        'title_tagline': 'Owner-operated plumber, East Cobb · License MP207455',
        'meta_description': "Joey Calloway has been fixing plumbing in East Cobb since 1993. Owner-operated. We quote on the phone, license # on every invoice, camera-jet recordings emailed.",
    },
}


def main():
    final = assemble.assemble(LEAD, COMPOSED, RESEARCH_BRIEF)
    html = final.get('html', '')
    slug = final.get('slug', 'demo')
    fp = final.get('fingerprint_inputs', {})

    out = Path(f'/tmp/v4_demo_{slug}')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'index.html').write_text(html)
    (out / 'composed.json').write_text(json.dumps(COMPOSED, indent=2))

    print(f"HTML written: {out / 'index.html'}")
    print(f"Size: {len(html):,} bytes")
    print(f"Slug: {slug}")
    print(f"Fingerprint: palette={fp.get('palette')!r} type_pair={fp.get('type_pair')!r}")
    print(f"Sections: {fp.get('sections')}")
    if final.get('warnings'):
        print(f"\nAssembler warnings:")
        for w in final['warnings']:
            print(f"  - {w}")


if __name__ == '__main__':
    main()
