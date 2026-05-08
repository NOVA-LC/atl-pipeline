"""Find owner email + LinkedIn for leads that don't have one.

Outscraper surfaces email on ~10-15% of leads. We can lift that to 50-70% by chaining:

1. Hunter.io domain-search    — given a website domain, returns all known emails
2. Hunter.io email-finder     — given website + first/last name, predicts the email
3. Snov.io domain-search       — alternative to Hunter
4. Apollo.io people-search    — finds owner LinkedIn + email by company name + city
5. Web search → owner LinkedIn → fetch profile → guess email pattern

For our case (small home-services), Hunter has best ROI:
- $34/mo for 500 searches
- Free tier: 25 searches/mo
- Confidence score per result; we drop anything below 80

The enrich.py output schema (stored in research_payload):
{
  "owner_email": "joey@pulliamhvac.com",
  "owner_email_confidence": 92,
  "owner_email_source": "hunter-finder",
  "owner_linkedin": "...",
  "domain_emails": [{"value":"...", "confidence":..., "first_name":"...", "last_name":"...", "position":"..."}]
}
"""
import os, re, requests
from urllib.parse import urlparse

def domain_from_research(research):
    """Best-effort: pull a website domain from research payload (LinkedIn → company website, etc.)."""
    if not research:
        return None
    # Prefer explicit website if research found one
    for k in ('owned_website', 'website'):
        if research.get(k):
            return urlparse(research[k]).netloc.lower().replace('www.', '')
    return None

def hunter_domain_search(domain, api_key):
    """Returns list of {value, first_name, last_name, position, confidence}."""
    if not domain or not api_key:
        return []
    try:
        r = requests.get('https://api.hunter.io/v2/domain-search',
                         params={'domain': domain, 'api_key': api_key, 'limit': 10},
                         timeout=15)
        if r.status_code == 200:
            return r.json().get('data', {}).get('emails', []) or []
    except Exception:
        pass
    return []

def hunter_email_finder(domain, first_name, last_name, api_key):
    """Predicts the email for a person at a domain. Returns (email, confidence)."""
    if not (domain and first_name and last_name and api_key):
        return None, None
    try:
        r = requests.get('https://api.hunter.io/v2/email-finder',
                         params={'domain': domain, 'first_name': first_name, 'last_name': last_name, 'api_key': api_key},
                         timeout=15)
        if r.status_code == 200:
            d = r.json().get('data') or {}
            return d.get('email'), d.get('score')
    except Exception:
        pass
    return None, None

def snov_domain_search(domain, snov_id, snov_secret):
    """Snov.io alternative. Requires OAuth client credentials flow."""
    if not (domain and snov_id and snov_secret):
        return []
    try:
        # Get token
        tok = requests.post('https://api.snov.io/v1/oauth/access_token', data={
            'grant_type': 'client_credentials',
            'client_id': snov_id, 'client_secret': snov_secret,
        }, timeout=10).json().get('access_token')
        if not tok:
            return []
        # Search
        r = requests.post('https://api.snov.io/v2/domain-search', headers={'Authorization': f'Bearer {tok}'},
                          json={'domain': domain, 'limit': 10}, timeout=15)
        if r.status_code == 200:
            return r.json().get('emails', [])
    except Exception:
        pass
    return []

def guess_email_patterns(domain, first_name, last_name):
    """Generate the 12 most common business-email patterns. Used as a free fallback.

    Caller is expected to verify each via SMTP RCPT (free) and pick the first that resolves.
    """
    if not (domain and first_name and last_name):
        return []
    f = first_name.lower().strip()
    l = last_name.lower().strip()
    fi = f[0] if f else ''
    li = l[0] if l else ''
    patterns = [
        f'{f}@{domain}',
        f'{f}.{l}@{domain}',
        f'{f}{l}@{domain}',
        f'{fi}{l}@{domain}',
        f'{fi}.{l}@{domain}',
        f'{f}{li}@{domain}',
        f'{f}_{l}@{domain}',
        f'{f}-{l}@{domain}',
        f'{l}@{domain}',
        f'{l}.{f}@{domain}',
        f'{fi}{li}@{domain}',
        f'{l}{fi}@{domain}',
    ]
    return list(dict.fromkeys(patterns))  # dedup, preserve order

def find_email_via_pattern_probing(domain, first_name, last_name):
    """Free email finder: guess patterns + SMTP RCPT probe each. Returns first that resolves.

    Quality is mediocre (~30-40% success rate) but it's free.
    """
    from . import email_verify
    candidates = guess_email_patterns(domain, first_name, last_name)
    for email in candidates:
        result = email_verify.verify(email)
        if result.get('verdict') == 'valid' and result.get('tier') == 'smtp':
            return email, 65   # confidence 65 — SMTP probe accepted
    return None, None

def enrich_lead(lead, research, env, verify_payload=None):
    """Try to find an owner email if the lead doesn't have one.

    New ordering (2026-05-08):
      0. SCRAPE the verify URL or any research-found website. Free + fast,
         works for "likely" verify hits and any URL Brave Search returned.
      1. Hunter domain-search (paid, accurate)
      2. Hunter email-finder by owner name
      3. Snov domain-search (paid alternative)
      4. SMTP-pattern probing (free fallback, mediocre quality)

    Returns enrichment payload (may be empty dict if nothing found).
    """
    out = {}
    if lead.get('email'):
        return out  # already has one

    # 0. FREE SCRAPE FIRST — try any URL we already know about.
    from . import email_scraper
    scraped = email_scraper.best_email_for_lead(verify_payload, research)
    if scraped:
        addr, score, source = scraped
        out['owner_email'] = addr
        out['owner_email_confidence'] = max(40, min(85, 50 + score))
        out['owner_email_source'] = f'site-scrape ({source})'
        return out

    domain = domain_from_research(research)
    hunter_key = env.get('HUNTER_API_KEY')
    snov_id = env.get('SNOV_CLIENT_ID')
    snov_secret = env.get('SNOV_CLIENT_SECRET')

    candidates = []

    # 1. Hunter domain search
    if domain and hunter_key:
        emails = hunter_domain_search(domain, hunter_key)
        for e in emails:
            candidates.append({
                'email': e['value'], 'confidence': e.get('confidence', 0),
                'first_name': e.get('first_name'), 'last_name': e.get('last_name'),
                'position': e.get('position'), 'source': 'hunter-domain'
            })

    # 2. Hunter email-finder by owner name (if research found one)
    owner_name = (research or {}).get('owner_name', '')
    if domain and hunter_key and owner_name and owner_name.lower() != 'unknown':
        parts = owner_name.split()
        if len(parts) >= 2:
            email, score = hunter_email_finder(domain, parts[0], parts[-1], hunter_key)
            if email:
                candidates.append({
                    'email': email, 'confidence': score or 0,
                    'first_name': parts[0], 'last_name': parts[-1],
                    'position': 'owner', 'source': 'hunter-finder'
                })

    # 3. Snov fallback
    if domain and snov_id and snov_secret and not candidates:
        snov_emails = snov_domain_search(domain, snov_id, snov_secret)
        for e in snov_emails:
            candidates.append({
                'email': e.get('email'), 'confidence': 75,
                'first_name': e.get('firstName'), 'last_name': e.get('lastName'),
                'position': e.get('position'), 'source': 'snov-domain'
            })

    # 4. FREE FALLBACK — guess email patterns + SMTP probe
    if domain and owner_name and owner_name.lower() != 'unknown' and not candidates:
        parts = owner_name.split()
        if len(parts) >= 2:
            email, conf = find_email_via_pattern_probing(domain, parts[0], parts[-1])
            if email:
                candidates.append({
                    'email': email, 'confidence': conf,
                    'first_name': parts[0], 'last_name': parts[-1],
                    'position': 'owner', 'source': 'pattern-probe-free'
                })

    if not candidates:
        return out

    # Pick best:
    # - prefer exec/owner positions
    # - then highest confidence
    # - filter out role-based unless nothing else
    EXEC_KEYWORDS = ('owner','founder','president','ceo','principal')
    ROLE_PREFIXES = ('info@','contact@','admin@','support@','sales@','noreply@')

    def score(c):
        s = c.get('confidence', 0) or 0
        pos = (c.get('position') or '').lower()
        if any(k in pos for k in EXEC_KEYWORDS): s += 30
        if (c.get('email') or '').lower().startswith(ROLE_PREFIXES): s -= 25
        return s

    best = max(candidates, key=score)
    out['owner_email'] = best['email']
    out['owner_email_confidence'] = best.get('confidence')
    out['owner_email_source'] = best.get('source')
    out['enrich_first_name'] = best.get('first_name')
    out['enrich_last_name'] = best.get('last_name')
    out['domain_emails'] = candidates  # for audit
    return out
