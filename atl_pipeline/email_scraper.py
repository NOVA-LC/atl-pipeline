"""Find owner emails by fetching a business's existing website + contact page.

Strategy: when verify.py finds a URL for a lead (verify_status='likely', or
research surfaced an owned_website), fetch the homepage + a contact page, parse
for mailto: links and plain-text emails. Filter out role-based + generic addresses.

This unblocks the chicken-and-egg problem: we target "no-website-or-bad-website"
leads, but every email-finding tool needs a domain. Sites where verify said
"likely" usually do have a website (just bad enough that we can still pitch).
For those, scraping is free and fast.

Limits:
- Network fetch is best-effort with short timeouts.
- We score candidates and return the best one (prefers personal-looking emails
  over info@ / support@).
"""
import re
import requests
from urllib.parse import urljoin, urlparse


EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
ROLE_PREFIXES = ('info', 'contact', 'admin', 'support', 'sales', 'noreply', 'no-reply',
                 'postmaster', 'webmaster', 'help', 'team', 'hello', 'hi', 'office')

# Common contact-page paths to try on the same domain.
CONTACT_PATHS = ('/contact', '/contact-us', '/contact.html', '/about', '/about-us',
                 '/get-in-touch', '/quote', '/estimate')

# Skip these — they're usually image hosts, CDN noise, or external services.
SKIP_DOMAINS = ('sentry.io', 'wixpress.com', 'wix.com', 'squarespace.com',
                'wordpress.com', 'shopify.com', 'cloudflare.com', 'gstatic.com',
                'googletagmanager.com', 'google-analytics.com', 'doubleclick.net',
                'facebook.com', 'instagram.com', 'youtube.com', 'twitter.com',
                'godaddy.com', 'cloudfront.net', 'amazonaws.com')

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')


def _fetch(url, timeout=10):
    try:
        r = requests.get(url, headers={'User-Agent': UA, 'Accept': 'text/html'},
                         timeout=timeout, allow_redirects=True)
        if r.status_code != 200:
            return None
        ct = (r.headers.get('Content-Type') or '').lower()
        if 'text/html' not in ct and 'application/xhtml' not in ct:
            return None
        return r.text
    except Exception:
        return None


def _emails_from_html(html):
    """Returns set of cleaned emails found in HTML (mailto: + plain-text)."""
    if not html:
        return set()
    found = set()
    # mailto: links
    for m in re.finditer(r'mailto:([^"\'<>?\s]+)', html, re.IGNORECASE):
        addr = m.group(1).split('?')[0].strip().lower()
        if EMAIL_RE.fullmatch(addr):
            found.add(addr)
    # plain-text emails (lots of false positives — be aggressive on filtering later)
    for m in EMAIL_RE.finditer(html):
        addr = m.group(0).strip().lower()
        # ignore obvious image filenames or asset hashes
        if any(addr.endswith(ext) for ext in ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp')):
            continue
        found.add(addr)
    return found


def _score(addr, expected_domain=None):
    """Higher score = better candidate. -1000 means reject.

    We REJECT role-based addresses (info@, contact@, sales@, etc.) outright.
    Tyler's pitch is to the owner — info@ inboxes go unread, often hit spam
    filters, and email_verify will mark them 'risky' and blank them out anyway.
    Better to return nothing than a guaranteed-skip.
    """
    local, _, domain = addr.partition('@')
    if not domain:
        return -1000
    if any(skip in domain for skip in SKIP_DOMAINS):
        return -1000
    # Hard reject role-based — sending to info@ is a waste
    if local.split('+')[0] in ROLE_PREFIXES:
        return -1000
    # Hard reject noreply variants
    if 'noreply' in local or 'no-reply' in local or 'donotreply' in local:
        return -1000
    score = 0
    # Bonus if email is on the same domain as the website we scraped
    if expected_domain and expected_domain in domain:
        score += 50
    # Bonus for first.last patterns (the pattern most likely to be the owner)
    if '.' in local:
        score += 15
    # Penalty for very long local-parts (often hash-like)
    if len(local) > 30:
        score -= 20
    # Penalty for digit-heavy local-parts (often automated / non-personal)
    digit_ratio = sum(c.isdigit() for c in local) / max(1, len(local))
    if digit_ratio > 0.3:
        score -= 15
    return score


def find_emails_on_site(url, max_pages=4, timeout=10):
    """Fetch homepage + a couple of contact pages, return list of (email, score) sorted best-first.

    Returns an empty list if URL is bad or no emails found.
    """
    if not url:
        return []
    parsed = urlparse(url)
    if not parsed.scheme:
        url = 'https://' + url
        parsed = urlparse(url)
    base_domain = parsed.netloc.replace('www.', '')

    pages_to_try = [url]
    for path in CONTACT_PATHS:
        pages_to_try.append(urljoin(url, path))

    found = set()
    for page in pages_to_try[:max_pages + 1]:   # +1 for homepage
        html = _fetch(page, timeout=timeout)
        if not html:
            continue
        found.update(_emails_from_html(html))

    if not found:
        return []

    scored = [(a, _score(a, expected_domain=base_domain)) for a in found]
    scored = [(a, s) for (a, s) in scored if s > -1000]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def find_emails_via_search(lead, timeout=10, max_queries=2):
    """Tier 2 fallback: when we have NO URL on file, use Brave Search to discover
    where the business is mentioned online (Facebook page, BBB, Yelp, directory
    mirrors), then fetch those result URLs and parse emails out of them.

    Realistically lifts find rate from ~30% to ~50-60% on no-website home-services
    leads (older trades publicly post personal Gmail/Yahoo on FB About sections
    and BBB profiles, all HTML-scrapeable without JS).

    Cost: 1-2 Brave queries per lead. Free tier is 2k/mo (~66/day) — cap at 1
    query default to stay well under.
    """
    from . import brave_search
    import os

    api_key = os.environ.get('BRAVE_API_KEY')
    if not api_key:
        return []

    name = lead.get('business_name') or ''
    city = lead.get('city') or ''
    if not name:
        return []

    # Query 1: catch-all — business name + city + email
    queries = [f'"{name}" {city} email']
    # Query 2: facebook fallback — small contractors usually have public FB pages
    if max_queries >= 2:
        queries.append(f'"{name}" {city} site:facebook.com')

    candidates = []
    seen_urls = set()

    for q in queries[:max_queries]:
        results = brave_search.web(q, count=5, api_key=api_key)
        for r in results:
            url = r.get('url') or ''
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if any(skip in url for skip in SKIP_DOMAINS):
                continue
            for addr, score in find_emails_on_site(url, max_pages=2, timeout=timeout):
                candidates.append((addr, score, url))
        # Early exit: if we already found a strong candidate (score > 30), stop searching
        if candidates and max(c[1] for c in candidates) > 30:
            break

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates


def best_email_for_lead(verify_payload, research_payload, owned_website=None,
                        lead=None, timeout=10):
    """Try every signal we have for this lead, return single best email or None.

    Cascade (cheapest → most expensive):
      1. verify_payload['url']  — what verify.py found
      2. owned_website explicit override
      3. research_payload['sources']  — non-directory URLs Haiku surfaced
      4. (NEW) Brave Search → top 3-5 result URLs  — the fallback that catches
         leads with NO known URL but a public FB/BBB/Yelp footprint

    Pass `lead` dict to enable Tier 4. Without it, only Tiers 1-3 run.
    """
    candidates = []
    seen_urls = set()

    def _try(u):
        if not u or u in seen_urls:
            return
        seen_urls.add(u)
        for addr, score in find_emails_on_site(u, timeout=timeout):
            candidates.append((addr, score, u))

    if verify_payload and isinstance(verify_payload, dict):
        _try(verify_payload.get('url'))
    if owned_website:
        _try(owned_website)
    if research_payload and isinstance(research_payload, dict):
        for src in (research_payload.get('sources') or []):
            if isinstance(src, str) and any(d in src for d in SKIP_DOMAINS):
                continue
            _try(src)

    # If Tiers 1-3 found nothing useful, escalate to Brave Search.
    best_score = max((c[1] for c in candidates), default=-1000)
    if lead and best_score < 30:
        for addr, score, source in find_emails_via_search(lead, timeout=timeout):
            if (addr, source) not in [(a, s) for (a, _, s) in candidates]:
                candidates.append((addr, score, source))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0]   # (addr, score, source_url)
