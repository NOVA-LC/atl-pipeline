"""Verify whether a 'no website' lead actually has a website (under another name).

Uses Brave Search directly — no LLM needed for the binary "has site?" question.
Strongest signal: a non-directory URL whose snippet contains the business phone.

Returns: 'yes' | 'no' | 'likely' | 'unsure'
"""
import os
from . import brave_search


def verify_lead(lead, **kwargs):
    """Returns {'verdict': str, 'url': str|None, 'raw': str}.

    Verdict rules:
      yes     — owned site found AND phone matched in snippet
      likely  — non-directory site found in top results, phone didn't match
      no      — only directory listings (Yelp/BBB/Google/Angi) in top 10 results
      unsure  — Brave returned nothing or API key missing
    """
    api_key = os.environ.get('BRAVE_API_KEY')
    if not api_key:
        return {'verdict': 'unsure', 'url': None, 'raw': 'no BRAVE_API_KEY'}

    name = lead['business_name']
    phone = lead.get('phone') or ''
    city = lead.get('city') or ''

    # Strong-match path: search with phone, look for non-directory URL whose snippet has the phone
    strong = brave_search.find_owned_website(name, phone, city, api_key=api_key)
    if strong:
        return {'verdict': 'yes', 'url': strong, 'raw': f'owned site (phone matched): {strong}'}

    # Weak-match path: any non-directory result in top 10 = "likely"
    DIRECTORY_DOMAINS = ('yelp.com','bbb.org','google.com','facebook.com','instagram.com',
                         'angi.com','homeadvisor.com','manta.com','yellowpages.com',
                         'thumbtack.com','nextdoor.com','mapquest.com','foursquare.com',
                         'apple.com','bing.com')
    q = f'"{name}" {city}'
    results = brave_search.web(q, count=10, api_key=api_key)
    if not results:
        return {'verdict': 'unsure', 'url': None, 'raw': 'no Brave results'}

    non_directory = [r for r in results if r.get('url') and not any(d in r['url'] for d in DIRECTORY_DOMAINS)]
    if non_directory:
        return {'verdict': 'likely', 'url': non_directory[0]['url'],
                'raw': f"non-directory result without phone match: {non_directory[0]['url']}"}

    return {'verdict': 'no', 'url': None, 'raw': f'{len(results)} results, all directory listings'}


def verify_batch(leads, max_workers=4):
    """Parallel verify. Returns dict[lead_id] -> result.

    max_workers=4 because Brave free tier is 1 qps and we make 1-2 queries per lead.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(verify_lead, dict(l)): l for l in leads}
        for f in as_completed(futs):
            lead = futs[f]
            try:
                out[lead['id']] = f.result()
            except Exception as e:
                out[lead['id']] = {'verdict': 'unsure', 'url': None, 'raw': f'error: {e}'}
    return out
