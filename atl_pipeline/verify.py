"""Verify whether a 'no website' lead actually has a website (under another name).

Returns: 'yes' | 'no' | 'likely' | 'unsure'
"""
import os, re
import anthropic

SYSTEM = """You're verifying whether a small business has a website. Return ONE word: yes, no, likely, or unsure.

Decision rule:
- yes: found a real owned website with matching phone or address
- likely: probable owned website but couldn't 100% confirm match
- no: searched and found only directory listings (Yelp, BBB, Google, Angi, etc.)
- unsure: insufficient information either way

Then on a second line, include the URL if you found one (or 'none')."""

PROMPT_TPL = """Verify whether this business has a website (not Yelp/BBB/Google/Angi/etc — a real owned domain).

Business: {name}
Category: {category}
City, State: {city}, {state}
Phone: {phone}
Address: {address}
Email (if any): {email}

Use web search. Check 1-2 queries max. Match by phone or address. If you find an owned domain, include the URL."""

def verify_lead(lead, model='claude-haiku-4-5-20251001', client=None):
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    prompt = PROMPT_TPL.format(
        name=lead['business_name'],
        category=lead.get('category') or '',
        city=lead.get('city') or '',
        state=lead.get('state') or '',
        phone=lead.get('phone') or '',
        address=lead.get('address') or '',
        email=lead.get('email') or '',
    )
    resp = client.messages.create(
        model=model, max_tokens=300, system=SYSTEM,
        tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 2}],
        messages=[{'role': 'user', 'content': prompt}]
    )
    text = ''
    for block in resp.content:
        if block.type == 'text':
            text = block.text
    text = text.strip().lower()
    verdict = 'unsure'
    for w in ('yes', 'no', 'likely', 'unsure'):
        if text.startswith(w):
            verdict = w
            break
    url_match = re.search(r'https?://[^\s)]+', text)
    return {'verdict': verdict, 'url': url_match.group(0) if url_match else None, 'raw': text[:500]}

def verify_batch(leads, max_workers=8):
    from concurrent.futures import ThreadPoolExecutor, as_completed
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(verify_lead, dict(l), 'claude-haiku-4-5-20251001', client): l for l in leads}
        for f in as_completed(futs):
            lead = futs[f]
            try:
                out[lead['id']] = f.result()
            except Exception as e:
                out[lead['id']] = {'verdict': 'unsure', 'raw': f'error: {e}'}
    return out
