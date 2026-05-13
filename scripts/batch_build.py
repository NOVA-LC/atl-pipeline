"""Batch-build websites for every lead in the Outscraper export.

Reads the xlsx, runs each lead through the v4 pipeline (voice → PTC →
compose → assemble → awwwards) with FLUX dev imagery, writes
index.html + assets/* per lead under examples/batch-100/<slug>/.

Run with:
  ANTHROPIC_API_KEY=sk-ant-...
  REPLICATE_API_TOKEN=r8_...
  PYTHONPATH=/path/to/atl-pipeline
  python scripts/batch_build.py [--limit N] [--start N] [--slug-only foo]

Each lead costs ~$0.135 (compose + 3 FLUX dev images + classifier).
100 leads ≈ $13.50.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import openpyxl
import anthropic

from atl_pipeline.agent import (
    catalog, cost, voice, design_ptc, compose, assemble, awwwards, image_gen,
)


def slugify(name: str) -> str:
    s = (name or '').lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s[:60] or 'lead'


def parse_xlsx_row(row, col):
    """Build the raw_outscraper dict + canonical lead dict from one xlsx row."""
    raw = {h: row[col[h]] for h in col}
    for k in ('reviews_per_score', 'about', 'working_hours'):
        v = raw.get(k)
        if isinstance(v, str) and v.startswith('{'):
            try: raw[k] = json.loads(v)
            except: pass
    if raw.get('photo'):
        raw['photos'] = [raw['photo']]
    lead = {
        'id': hash((raw.get('name'), raw.get('phone'))) & 0xfffff,
        'slug': slugify(raw.get('name')),
        'business_name': raw.get('name'),
        'category': raw.get('category') or '',
        'city': raw.get('city') or '',
        'state': raw.get('state_code') or 'GA',
        'phone': raw.get('phone') or '',
        'rating': raw.get('rating'),
        'reviews': raw.get('reviews'),
        'website_url': raw.get('website') or '',
        'address': raw.get('address') or '',
        'raw_outscraper': raw,
    }
    return lead, raw


def build_one(lead: dict, raw: dict, client, full_catalog, out_root: Path,
              tracker, model_compose: str = 'claude-sonnet-4-6') -> dict:
    """Run the full pipeline against one lead. Returns the result manifest."""
    t0 = time.time()
    out_dir = out_root / lead['slug']
    out_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = out_dir / 'assets'
    assets_dir.mkdir(exist_ok=True)

    # Industry normalization for PTC + image_gen
    cat = (lead.get('category') or '').lower()
    industry = 'plumber'
    for v in ('plumber', 'hvac', 'electric', 'roofer', 'roofing',
              'landscape', 'septic', 'auto', 'mechanic', 'radiator',
              'tree', 'general contractor'):
        if v in cat:
            industry = ('roofer' if v in ('roofer','roofing') else
                        'electrician' if 'electric' in v else
                        'landscape' if v in ('landscape','tree') else
                        'auto' if v in ('mechanic','radiator') else
                        v)
            break

    research_brief = {
        'photos': [{'url': p, 'source': 'gbp'}
                   for p in (raw.get('photos') or [])
                   if p and ('googleusercontent' in p or 'maps.googleapis' in p)],
        'real_reviews': [],
        'service_area_neighborhoods': [lead['city']],
        'license_number': None,
        'hours': raw.get('working_hours'),
    }
    real_photo_count = len(research_brief['photos'])

    # 1. Voice (archetype path — no LLM call)
    try:
        voice_card = voice.archetype_card(industry)
    except Exception:
        voice_card = {'register': 'blue_collar'}

    # 2. PTC — picks shell + palette + type + sections + hero variant
    ptc = design_ptc.pick_design(
        lead, voice_card, [], tracker,
        full_catalog=full_catalog, client=client,
        real_photo_count=real_photo_count,
    )
    dh = ptc.get('design_hint') or {}
    research_brief['_design_hint'] = dh

    # 2b. Image gen — FLUX dev for hero + process + environmental
    palette_name = dh.get('palette') or 'clean-trade-blue'
    try:
        gen = image_gen.generate_brand_photos(
            industry=industry, palette_name=palette_name, palette_dict={},
            business=lead, out_dir=assets_dir, tracker=tracker,
            model='black-forest-labs/flux-dev',
            want_hero=True, want_gallery=2,
        )
    except Exception as e:
        gen = {'hero': None, 'gallery': [], 'errors': [repr(e)], 'cost_cents': 0}
    research_brief['_generated_photos'] = {
        'hero': f'assets/{gen["hero"]}' if gen.get('hero') else None,
        'process_image': f'assets/{gen["gallery"][0]}'
            if gen.get('gallery') else None,
        'environmental_image': f'assets/{gen["gallery"][1]}'
            if len(gen.get('gallery') or []) > 1 else None,
    }

    # 3. Compose
    try:
        composed = compose.compose_page(
            lead, research_brief, tracker,
            model=model_compose, client=client,
            full_catalog=full_catalog, voice_card=voice_card,
        )
    except Exception as e:
        composed = {}
    # Backfill design hint into composed
    if dh:
        composed.setdefault('palette', dh.get('palette'))
        composed.setdefault('type_pair', dh.get('type_pair'))
        if dh.get('shell'):
            composed.setdefault('shell', dh['shell'])
        if dh.get('sections') and not composed.get('sections'):
            composed['sections'] = dh['sections']
    composed.setdefault('images', {})
    composed['images']['hero'] = research_brief['_generated_photos']['hero']

    # 4. Assemble
    result = assemble.assemble(lead, composed, research_brief)
    html = result['html']
    fp = result['fingerprint_inputs']

    (out_dir / 'index.html').write_text(html)
    (out_dir / 'composed.json').write_text(
        json.dumps(composed, indent=2, default=str))

    # 5. Awwwards (optional; can disable for cost)
    score = None
    try:
        verdict = awwwards.classify(composed, fp, html, tracker, client=client)
        score = verdict.get('score')
    except Exception:
        verdict = {}

    elapsed = time.time() - t0
    return {
        'slug': lead['slug'],
        'name': lead['business_name'],
        'industry': industry,
        'category': lead['category'],
        'city': lead['city'],
        'shell': result['effective_choices'].get('shell'),
        'palette': result['effective_choices']['palette'],
        'type_pair': result['effective_choices']['type_pair'],
        'html_bytes': len(html),
        'images_count': fp.get('image_count', 0),
        'awwwards_tier': verdict.get('tier'),
        'awwwards_score': score,
        'cost_cents': round(tracker.per_lead_spent_cents, 2),
        'duration_s': round(elapsed, 1),
        'errors': result.get('warnings', []) + (gen.get('errors') or []),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--xlsx', required=True)
    p.add_argument('--out', default='examples/batch-100')
    p.add_argument('--start', type=int, default=0)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--slug-only', default=None,
                   help='build only the lead whose slugified name contains this')
    p.add_argument('--top-n', type=int, default=None,
                   help='only build the top N leads by composite score')
    args = p.parse_args()

    wb = openpyxl.load_workbook(args.xlsx, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    col = {h: i for i, h in enumerate(header) if h}
    leads_raw = rows[1:]

    if args.top_n:
        import math
        def score(r):
            rating = r[col['rating']] or 0
            reviews = r[col['reviews']] or 0
            photos = r[col['photos_count']] or 0
            try:
                rating = float(rating); reviews = int(reviews); photos = int(photos)
            except: return 0
            return rating * math.log(max(reviews, 1) + 1) * (2 if photos >= 5 else 1)
        leads_raw = sorted(leads_raw, key=score, reverse=True)[:args.top_n]

    leads_raw = leads_raw[args.start:]
    if args.limit:
        leads_raw = leads_raw[:args.limit]
    if args.slug_only:
        leads_raw = [r for r in leads_raw
                     if r[col['name']] and args.slug_only.lower() in r[col['name']].lower()]

    print(f'Building {len(leads_raw)} leads → {args.out}')
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    client = anthropic.Anthropic()
    full_catalog = catalog.load_all()

    manifest = []
    grand_total = 0.0
    t_start = time.time()
    for i, row in enumerate(leads_raw, 1):
        if not row[col['name']]:
            continue
        lead, raw = parse_xlsx_row(row, col)
        # Fresh tracker per lead so per-lead cap applies
        tracker = cost.CostTracker(per_lead_cap_cents=40, daily_cap_cents=2000)
        tracker.reset_per_lead()
        try:
            entry = build_one(lead, raw, client, full_catalog, out_root, tracker)
        except Exception as e:
            entry = {'slug': lead['slug'], 'name': lead['business_name'],
                     'error': repr(e)}
        manifest.append(entry)
        grand_total += entry.get('cost_cents', 0)
        elapsed = time.time() - t_start
        print(f'  [{i:3d}/{len(leads_raw)}] {entry.get("shell","?"):42s} '
              f'{entry.get("name","")[:40]:40s} '
              f'{entry.get("html_bytes",0):6d}b '
              f'{entry.get("cost_cents",0):.1f}¢ '
              f'{entry.get("duration_s",0):.0f}s '
              f'(total: ${grand_total/100:.2f}, {elapsed/60:.1f}min)')

    (out_root / 'manifest.json').write_text(
        json.dumps(manifest, indent=2, default=str))
    print(f'\n=== DONE ===')
    print(f'Built: {sum(1 for e in manifest if not e.get("error"))}/{len(manifest)}')
    print(f'Total cost: ${grand_total/100:.2f}')
    print(f'Wall time: {(time.time()-t_start)/60:.1f} min')
    print(f'Manifest: {out_root}/manifest.json')


if __name__ == '__main__':
    main()
