"""Brave Search API — free 2,000 queries/mo, $5/mo for 20k.

Replaces Anthropic's built-in web_search ($0.01/search) for the bulk of work.
Use this for: owner LinkedIn lookup, "is the business closed" checks, brand color
research, real review fetching.

Sign up: https://api.search.brave.com/app/keys
"""
import os, time, requests
from urllib.parse import quote

BRAVE_API = 'https://api.search.brave.com/res/v1'

# Brave free plan rate-limit: 1 query/sec. Track last call to throttle.
_last_call = [0.0]

def _throttle(min_interval=1.05):
    elapsed = time.time() - _last_call[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call[0] = time.time()

def web(query, count=10, country='US', api_key=None):
    """Web search. Returns list of {title, url, description}.

    Brave docs: https://api.search.brave.com/app/documentation/web-search
    """
    api_key = api_key or os.environ.get('BRAVE_API_KEY')
    if not api_key:
        return []
    _throttle()
    try:
        r = requests.get(f'{BRAVE_API}/web/search',
                         headers={'X-Subscription-Token': api_key, 'Accept': 'application/json'},
                         params={'q': query, 'count': count, 'country': country, 'safesearch': 'moderate'},
                         timeout=15)
        if r.status_code != 200:
            return []
        web_results = r.json().get('web', {}).get('results', []) or []
        return [{'title': x.get('title'), 'url': x.get('url'), 'description': x.get('description')} for x in web_results]
    except Exception:
        return []

def find_owner_linkedin(business_name, city, api_key=None):
    """Returns first LinkedIn URL for the owner of <business_name> in <city>, or None."""
    q = f'site:linkedin.com/in "{business_name}" {city or ""}'
    results = web(q, count=5, api_key=api_key)
    for r in results:
        if r['url'] and 'linkedin.com/in/' in r['url']:
            return r['url']
    return None

def find_owned_website(business_name, phone, city, api_key=None):
    """Find a real owned website for the business (not Yelp/BBB). Returns URL or None.

    Returns owned domain when result snippet contains the phone number — strong match signal.
    """
    DIRECTORY_DOMAINS = ('yelp.com','bbb.org','google.com','facebook.com','instagram.com',
                        'angi.com','homeadvisor.com','manta.com','yellowpages.com',
                        'thumbtack.com','nextdoor.com','mapquest.com','foursquare.com')
    q = f'"{business_name}" {city or ""} {phone or ""}'
    results = web(q, count=10, api_key=api_key)
    phone_digits = ''.join(c for c in (phone or '') if c.isdigit())[-10:]
    for r in results:
        url = r.get('url') or ''
        if not url or any(d in url for d in DIRECTORY_DOMAINS):
            continue
        desc = (r.get('description') or '') + ' ' + (r.get('title') or '')
        desc_digits = ''.join(c for c in desc if c.isdigit())
        if phone_digits and phone_digits in desc_digits:
            return url   # strong match: phone in snippet
    return None

def find_real_reviews(business_name, city, api_key=None, max_reviews=5):
    """Try to surface review snippets. Reviewer name + verbatim text usually appears in Brave's description."""
    out = []
    for site in ('site:yelp.com', 'site:bbb.org', 'site:angi.com'):
        q = f'{site} "{business_name}" {city or ""} review'
        for r in web(q, count=5, api_key=api_key):
            d = (r.get('description') or '').strip()
            if d and len(d) > 60:
                out.append({'snippet': d, 'source_url': r.get('url'), 'title': r.get('title')})
            if len(out) >= max_reviews:
                return out
    return out
