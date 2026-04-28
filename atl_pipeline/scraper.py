"""Autonomous daily lead scraping via Outscraper API.

Tyler already uses Outscraper. Their API matches the manual UI 1:1, so we just
script the same job he runs (e.g., "HVAC contractors in Marietta GA") on a daily
cron, save the xlsx, and feed it into ingest.py.

API docs: https://app.outscraper.com/api-docs

The bulk endpoint is async: you submit a request, get a request_id, poll for results.
We wrap that here.

Cost: ~$3 per 1,000 records. 50/day = $5/month at full ramp.
"""
import os, time, json, requests
from pathlib import Path

OUTSCRAPER_API = 'https://api.outscraper.cloud'

# A rotating set of queries that Tyler can extend in atl_pipeline/queries.json
# Each entry maps to a single Outscraper Google Maps query.
DEFAULT_QUERIES = [
    'HVAC contractor, Marietta, GA, USA',
    'HVAC contractor, Smyrna, GA, USA',
    'HVAC contractor, Decatur, GA, USA',
    'HVAC contractor, Sandy Springs, GA, USA',
    'Plumber, Marietta, GA, USA',
    'Plumber, Smyrna, GA, USA',
    'Plumber, Decatur, GA, USA',
    'Plumber, Brookhaven, GA, USA',
    'Roofing contractor, Marietta, GA, USA',
    'Roofing contractor, Smyrna, GA, USA',
    'Tree service, Marietta, GA, USA',
    'Tree service, Atlanta, GA, USA',
    'Landscaping supply, Atlanta, GA, USA',
    'Septic service, Cobb County, GA, USA',
    'Electrician, Marietta, GA, USA',
    'Electrician, Decatur, GA, USA',
    'Painting contractor, Atlanta, GA, USA',
    'Pressure washing, Atlanta, GA, USA',
    'Garage door, Atlanta, GA, USA',
    'Locksmith, Atlanta, GA, USA',
]

QUERY_STATE = Path('.scraper_state.json')

def load_query_rotation():
    """Round-robin through DEFAULT_QUERIES so we don't hit the same areas every day."""
    if QUERY_STATE.exists():
        return json.loads(QUERY_STATE.read_text())
    return {'last_idx': -1}

def save_query_rotation(state):
    QUERY_STATE.write_text(json.dumps(state))

def pick_queries(n=5, custom_queries=None):
    """Pick n queries, rotating through the list. n=5 typically yields 100-300 leads."""
    qs = custom_queries or DEFAULT_QUERIES
    state = load_query_rotation()
    start = (state.get('last_idx', -1) + 1) % len(qs)
    picked = []
    for i in range(n):
        picked.append(qs[(start + i) % len(qs)])
    state['last_idx'] = (start + n - 1) % len(qs)
    save_query_rotation(state)
    return picked

def submit_google_maps_search(queries, api_key, limit_per_query=20, language='en', region='US'):
    """Submit an async Google Maps search. Returns a request_id."""
    r = requests.get(f'{OUTSCRAPER_API}/maps/search-v3', params={
        'query': queries,                    # list — multiple queries supported
        'limit': limit_per_query,
        'language': language,
        'region': region,
        'async': 'true',
        'enrichment': 'emails_validator_service,domains_service,company_insights_service',
    }, headers={'X-API-KEY': api_key}, timeout=30)
    if r.status_code not in (200, 202):
        raise RuntimeError(f'Outscraper submit failed: {r.status_code} {r.text[:300]}')
    body = r.json()
    return body.get('id') or body.get('request_id')

def poll_results(request_id, api_key, timeout_minutes=15):
    """Poll until the async job finishes. Returns the parsed results array."""
    url = f'{OUTSCRAPER_API}/requests/{request_id}'
    deadline = time.time() + timeout_minutes * 60
    while time.time() < deadline:
        r = requests.get(url, headers={'X-API-KEY': api_key}, timeout=20)
        body = r.json()
        status = body.get('status', '').lower()
        if status in ('success','succeeded'):
            return body.get('data', [])
        if status in ('failed','error'):
            raise RuntimeError(f'Outscraper job failed: {body}')
        time.sleep(15)
    raise TimeoutError(f'Outscraper job {request_id} did not finish in {timeout_minutes} min')

def to_xlsx(results, out_path):
    """Convert Outscraper result arrays into a single flattened xlsx that ingest.py can eat."""
    import pandas as pd
    rows = []
    # results is List[List[place_dict]] — one inner list per query
    for query_results in results:
        if not isinstance(query_results, list):
            continue
        for place in query_results:
            rows.append(place)
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df.to_excel(out_path, index=False)
    return out_path

def daily_scrape(api_key, n_queries=5, limit_per_query=20, out_dir='./scrapes'):
    """One-shot daily scrape. Returns path to the resulting xlsx (or None if empty)."""
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    queries = pick_queries(n_queries)
    print(f'  queries: {queries}')
    request_id = submit_google_maps_search(queries, api_key, limit_per_query)
    print(f'  request_id: {request_id}')
    results = poll_results(request_id, api_key)
    import datetime
    out = Path(out_dir) / f'outscraper-{datetime.date.today().isoformat()}.xlsx'
    return to_xlsx(results, out)
