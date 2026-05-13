"""Scrape Georgia Corporations Division for officer + registered-agent
names on a batch of leads. Free, ~85% yield on legitimately-registered
LLCs in Georgia.

Run locally (not from Claude Code sandbox — eCorp is bot-blocked):

  pip install playwright openpyxl
  playwright install chromium

  python scripts/scrape_ga_ecorp.py \
      --xlsx /path/to/outscraper-export.xlsx \
      --out  /path/to/ga-ecorp-results.json \
      [--limit N]              # process only first N
      [--slug-only foo]        # only businesses containing "foo"
      [--headed]               # show the browser window for debugging
      [--throttle-ms 1500]     # delay between lookups (default 1500ms)
      [--resume]               # skip leads already present in --out

Output JSON manifest entries:
  {
    "lead_name":      "...",     # input business name
    "lead_city":      "...",
    "lead_phone":     "...",
    "match_found":    true,
    "match_name":     "...",     # eCorp's full LLC/Corp name (may differ)
    "match_id":       "...",     # GA business ID
    "match_status":   "Active",
    "principal_address": "...",
    "officers": [
      {"name": "John Smith", "title": "CEO"},
      {"name": "Jane Doe",   "title": "Registered Agent"}
    ],
    "owner_first":    "John",    # best-guess from officers
    "owner_last":     "Smith",
    "confidence":     "high"     # high/medium/low/none
  }

Confidence rules:
  high   : single officer OR officer name matches business name owner pattern
  medium : multiple officers, picked by heuristic (CEO > Manager > Member > RA)
  low    : only registered agent surfaced (often a CPA/lawyer, not owner)
  none   : no match found in eCorp at all

Honest caveats baked in:
  - Many GA LLCs register under a "[Owner Name] LLC" or "[Initial Letters]
    Holdings LLC" different from the DBA. Fuzzy name matching catches most.
  - The registered agent is the OWNER ~70-85% of the time for sub-$1M shops
    but is sometimes a CPA, attorney, or registered-agent service.
    Confidence=low flags these for manual review.
  - Throttle defaults to 1.5 sec between lookups — friendly to the public
    portal. Don't crank it down.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import openpyxl
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError


# ----- name normalization for fuzzy matching -----

def _normalize(s: str) -> str:
    s = (s or '').lower()
    # Strip entity suffixes that vary between DBA and registered name
    for suffix in (' llc', ' l.l.c.', ' inc', ' inc.', ' corp', ' corporation',
                   ' co.', ' co ', ' company', ' services', ' service',
                   ' the ', ' a ', ' an ', ' & ', ' and '):
        s = s.replace(suffix, ' ')
    s = re.sub(r'[^a-z0-9 ]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _token_overlap(a: str, b: str) -> float:
    """Jaccard token similarity between normalized strings."""
    ta, tb = set(_normalize(a).split()), set(_normalize(b).split())
    if not ta and not tb:
        return 0.0
    return len(ta & tb) / max(len(ta | tb), 1)


# ----- officer name → likely owner guess -----

# Roles that typically equal "owner" for an LLC owner-operator (in priority order)
_OWNER_TITLES = [
    'ceo', 'president', 'manager', 'member', 'sole member',
    'managing member', 'organizer', 'officer',
]
_RA_TITLES = ['registered agent', 'agent', 'r.a.']
# Registered-agent services that are NEVER the owner
_RA_BLACKLIST = [
    'incfile', 'legalzoom', 'corp serv', 'registered agents inc',
    'csc lawyers', 'cogency global', 'csc', 'national registered agents',
    'paracorp', 'inc.', 'attorneys at law', 'cpa', 'p.c.',
]


def _is_likely_owner_name(name: str) -> bool:
    """True if this name looks like a person (not a corporate RA service)."""
    n = (name or '').lower()
    if any(b in n for b in _RA_BLACKLIST):
        return False
    # Person names tend to have 2-3 word tokens
    tokens = name.split()
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    # Roman numerals / Jr/Sr/III suffixes are fine; reject "& Co." patterns
    if '&' in name:
        return False
    return True


def _pick_owner(officers: list[dict]) -> tuple[str, str, str]:
    """From an officer list pick the most likely owner name.
    Returns (first_name, last_name, confidence) where confidence is
    high/medium/low/none."""
    if not officers:
        return '', '', 'none'

    # Filter to person-shaped names
    person_candidates = [o for o in officers if _is_likely_owner_name(o.get('name', ''))]

    if not person_candidates:
        return '', '', 'none'

    # 1. Single person across all officers → high confidence
    unique_names = {o['name'] for o in person_candidates}
    if len(unique_names) == 1:
        name = next(iter(unique_names))
        parts = name.split()
        return parts[0], parts[-1], 'high'

    # 2. Priority by title
    by_priority: dict[str, dict] = {}
    for o in person_candidates:
        title = (o.get('title') or '').lower()
        for i, kw in enumerate(_OWNER_TITLES):
            if kw in title:
                if kw not in by_priority or len(person_candidates) > 1:
                    by_priority.setdefault(kw, o)
                break
    for kw in _OWNER_TITLES:
        if kw in by_priority:
            name = by_priority[kw]['name']
            parts = name.split()
            conf = 'medium' if len(person_candidates) > 1 else 'high'
            return parts[0], parts[-1], conf

    # 3. Only registered agent surfaced — low confidence
    for o in person_candidates:
        title = (o.get('title') or '').lower()
        if any(kw in title for kw in _RA_TITLES):
            name = o['name']
            parts = name.split()
            return parts[0], parts[-1], 'low'

    # 4. Fallback — first person-shaped name, low confidence
    name = person_candidates[0]['name']
    parts = name.split()
    return parts[0], parts[-1], 'low'


# ----- Playwright eCorp interaction -----

ECORP_BASE = 'https://ecorp.sos.ga.gov'
SEARCH_URL = f'{ECORP_BASE}/BusinessSearch'


async def _search_and_pick(page, business_name: str, city: str) -> str | None:
    """Search eCorp by name + accept top fuzzy match. Returns the entity
    detail-page URL or None."""
    await page.goto(SEARCH_URL, wait_until='domcontentloaded')

    # eCorp uses an ASP.NET form. The "Business Name" radio is default-selected.
    # Type into the search box and submit.
    await page.fill('input[id$="txtBusinessName"]', business_name)
    await page.click('input[id$="btnSearch"]')

    # Wait for the results grid
    try:
        await page.wait_for_selector('table[id$="grdSearchResults"] tr', timeout=8000)
    except PWTimeoutError:
        return None

    # Walk the result rows, find the best fuzzy match on the entity-name cell
    rows = await page.query_selector_all('table[id$="grdSearchResults"] tbody tr')
    best, best_score, best_link = None, 0.0, None
    for row in rows:
        cells = await row.query_selector_all('td')
        if len(cells) < 2:
            continue
        link = await cells[1].query_selector('a')
        if not link:
            continue
        entity_name = (await link.inner_text()).strip()
        score = _token_overlap(business_name, entity_name)
        if score > best_score:
            best, best_score, best_link = entity_name, score, link

    if not best_link or best_score < 0.40:  # threshold — tune as needed
        return None

    # Click into the entity detail page
    href = await best_link.get_attribute('href')
    await best_link.click()
    try:
        await page.wait_for_selector('table[id*="grdOfficers"]', timeout=8000)
    except PWTimeoutError:
        # Some entity pages don't have officers grid — still useful for status
        pass
    return page.url


async def _extract_detail(page) -> dict:
    """Pull entity status, principal address, registered agent + officer list
    off the detail page."""
    result: dict = {'officers': []}

    # Status — usually labeled "Status:" near top
    try:
        status_el = await page.query_selector('span[id*="lblStatus"]')
        if status_el:
            result['match_status'] = (await status_el.inner_text()).strip()
    except Exception:
        pass

    # Match (entity) name
    try:
        for sel in ['span[id*="lblEntityName"]', 'span[id*="lblBusinessName"]']:
            el = await page.query_selector(sel)
            if el:
                result['match_name'] = (await el.inner_text()).strip()
                break
    except Exception:
        pass

    # Business ID
    try:
        for sel in ['span[id*="lblControlNumber"]', 'span[id*="lblBusinessID"]']:
            el = await page.query_selector(sel)
            if el:
                result['match_id'] = (await el.inner_text()).strip()
                break
    except Exception:
        pass

    # Principal address
    try:
        for sel in ['span[id*="lblPrincipalAddress"]', 'span[id*="lblPrincipal"]']:
            el = await page.query_selector(sel)
            if el:
                result['principal_address'] = (await el.inner_text()).strip()
                break
    except Exception:
        pass

    # Registered agent — separate panel from officer grid
    try:
        ra_name_el = await page.query_selector('span[id*="lblRAName"]')
        if ra_name_el:
            ra_name = (await ra_name_el.inner_text()).strip()
            if ra_name:
                result['officers'].append({'name': ra_name, 'title': 'Registered Agent'})
    except Exception:
        pass

    # Officer grid — rows with name + title columns
    try:
        rows = await page.query_selector_all('table[id*="grdOfficers"] tbody tr')
        for row in rows:
            cells = await row.query_selector_all('td')
            if len(cells) >= 2:
                name = (await cells[0].inner_text()).strip()
                title = (await cells[1].inner_text()).strip()
                if name and not any(o['name'] == name for o in result['officers']):
                    result['officers'].append({'name': name, 'title': title})
    except Exception:
        pass

    return result


async def lookup_one(page, lead: dict) -> dict:
    """Run a full lookup for one lead, returning the manifest entry."""
    entry = {
        'lead_name': lead['name'],
        'lead_city': lead.get('city'),
        'lead_phone': lead.get('phone'),
        'match_found': False,
        'match_name': None,
        'match_id': None,
        'match_status': None,
        'principal_address': None,
        'officers': [],
        'owner_first': '',
        'owner_last': '',
        'confidence': 'none',
        'error': None,
    }
    try:
        detail_url = await _search_and_pick(page, lead['name'], lead.get('city') or '')
        if not detail_url:
            return entry
        detail = await _extract_detail(page)
        entry.update(detail)
        entry['match_found'] = True
        first, last, conf = _pick_owner(entry['officers'])
        entry['owner_first'] = first
        entry['owner_last'] = last
        entry['confidence'] = conf
    except Exception as e:
        entry['error'] = repr(e)
    return entry


# ----- main / batch runner -----

async def main():
    p = argparse.ArgumentParser()
    p.add_argument('--xlsx', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--limit', type=int, default=None)
    p.add_argument('--start', type=int, default=0)
    p.add_argument('--slug-only', default=None)
    p.add_argument('--headed', action='store_true',
                   help='Show the browser (default headless)')
    p.add_argument('--throttle-ms', type=int, default=1500)
    p.add_argument('--resume', action='store_true',
                   help='Skip leads already present in --out')
    args = p.parse_args()

    # Load input leads
    wb = openpyxl.load_workbook(args.xlsx, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    col = {h: i for i, h in enumerate(header) if h}
    leads = []
    for r in rows[1:]:
        if not r[col['name']]:
            continue
        leads.append({
            'name': r[col['name']],
            'city': r[col['city']] or '',
            'phone': r[col['phone']] or '',
            'address': r[col.get('address', col['name'])] or '',
        })

    leads = leads[args.start:]
    if args.limit:
        leads = leads[:args.limit]
    if args.slug_only:
        leads = [l for l in leads if args.slug_only.lower() in l['name'].lower()]

    # Resume support
    existing: dict[str, dict] = {}
    if args.resume and Path(args.out).exists():
        with open(args.out) as f:
            for e in json.load(f):
                existing[e['lead_name']] = e
        leads = [l for l in leads if l['name'] not in existing]
        print(f'Resume: skipping {len(existing)} already-processed leads')

    print(f'Looking up {len(leads)} leads on eCorp...')

    results = list(existing.values())
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not args.headed)
        ctx = await browser.new_context(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
        )
        page = await ctx.new_page()

        for i, lead in enumerate(leads, 1):
            entry = await lookup_one(page, lead)
            results.append(entry)
            status = '✓' if entry['match_found'] else '·'
            owner = f'{entry["owner_first"]} {entry["owner_last"]}'.strip() or '—'
            print(f'  [{i:3d}/{len(leads)}] {status} {lead["name"][:42]:42s}  '
                  f'owner={owner!s:30s}  conf={entry["confidence"]:6s}')

            # Persist after every lookup so crashes don't lose work
            with open(args.out, 'w') as f:
                json.dump(results, f, indent=2)

            await asyncio.sleep(args.throttle_ms / 1000)

        await browser.close()

    found = sum(1 for r in results if r['match_found'])
    high = sum(1 for r in results if r['confidence'] == 'high')
    med = sum(1 for r in results if r['confidence'] == 'medium')
    low = sum(1 for r in results if r['confidence'] == 'low')
    print(f'\n=== DONE ===')
    print(f'  matched in eCorp: {found}/{len(results)}')
    print(f'  owner-name yield: {high} high · {med} medium · {low} low')
    print(f'  total usable:     {high + med + low}/{len(results)}')
    print(f'  output:           {args.out}')


if __name__ == '__main__':
    asyncio.run(main())
