"""Deep prospect research using Brave Search + Haiku.

Strategy: gather raw signals via Brave Search, then ask Haiku to synthesize them
into a structured JSON brief. We control the search so we know it works (vs
relying on Anthropic's web_search tool which has unclear Haiku 4.5 support).

Output schema (research_payload):
{
  "owner_name": "Joey Pulliam",
  "owner_confidence": "high|medium|low|unknown",
  "owner_linkedin": "https://...",
  "owner_facebook": "https://...",
  "owner_instagram": "https://...",
  "founded_year": 1991,
  "years_in_business_claim": 35,
  "specialties": ["HVAC", "Plumbing"],
  "service_area": ["Atlanta", "Tucker", ...],
  "brand_colors": ["#0A1F3F", "#C9A961"],
  "vibe": "stately heritage",
  "tagline_options": ["...", "..."],
  "pain_points_solved": ["..."],
  "real_reviews": [{"author":"...", "text":"...", "stars":5, "date":"...", "source":"yelp|google"}],
  "wow_facts": ["35 years family-owned", "5 metro locations"],
  "warning": null,
  "sources": ["url1", "url2"]
}
"""
import os, json
import anthropic
from . import brave_search


SYSTEM = """You are a thorough, honest prospect researcher. You ONLY state facts that appear verbatim or are clearly implied in the SEARCH_CONTEXT provided. If a fact isn't in the context, say "unknown" — do not invent owner names, competitors, dates, or quotes.

Return JSON in this exact shape:
{
  "owner_name": "string or 'unknown'",
  "owner_confidence": "high|medium|low|unknown",
  "owner_linkedin": "url or null",
  "owner_facebook": "url or null",
  "owner_instagram": "url or null",
  "founded_year": null or number,
  "years_in_business_claim": null or number,
  "specialties": [string],
  "service_area": [string],
  "brand_colors": [hex strings],
  "vibe": "one-line vibe",
  "tagline_options": [string, string, string],
  "pain_points_solved": [string],
  "real_reviews": [{"author": "...", "text": "...", "stars": 5, "date": "...", "source": "..."}],
  "wow_facts": [string],
  "warning": null or "string",
  "sources": [url]
}"""


PROMPT_TPL = """Synthesize a research brief on this small business for a website-sales pitch.

BUSINESS:
- Name: {name}
- Category: {category}
- City, State: {city}, {state}
- Phone: {phone}
- Address: {address}
- Google rating: {rating_str}

SEARCH_CONTEXT (from web search — only state facts that appear here):
{search_context}

Pull from the context: owner full name (if a person is named on LinkedIn/BBB/Yelp owner reply), social links (LinkedIn, Facebook, Instagram), founding year if mentioned, specific services they offer, neighborhoods they serve, customer review snippets verbatim with author + source, and 1-3 "wow" facts. Suggest a one-line vibe and 3 tagline options that fit.

If a fact isn't in the context, output "unknown" or null. NEVER invent names, dates, competitors, or quotes.

Return ONLY the JSON — no commentary, no markdown fences."""


def gather_search_context(lead, api_key, max_chars=6000):
    """Hit Brave Search across 4-5 angles, concatenate snippets, return as text block."""
    name = lead['business_name']
    city = lead.get('city') or ''
    phone = lead.get('phone') or ''

    queries = [
        f'"{name}" {city} owner',
        f'"{name}" {city} {phone}',
        f'site:linkedin.com/in "{name}"',
        f'site:yelp.com "{name}" {city} review',
        f'site:bbb.org "{name}" {city}',
    ]

    chunks = []
    seen_urls = set()
    for q in queries:
        for r in brave_search.web(q, count=5, api_key=api_key):
            url = r.get('url') or ''
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = (r.get('title') or '').strip()
            desc = (r.get('description') or '').strip()
            if title or desc:
                chunks.append(f"[{url}]\n  {title}\n  {desc}")
            if sum(len(c) for c in chunks) > max_chars:
                break
        if sum(len(c) for c in chunks) > max_chars:
            break
    return '\n\n'.join(chunks) if chunks else '(no results from web search)'


def research_lead(lead, model='claude-haiku-4-5-20251001', client=None):
    """Run Brave-Search + Haiku research on one lead. Returns dict matching schema."""
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    brave_key = os.environ.get('BRAVE_API_KEY')
    if not brave_key:
        return {'_error': 'BRAVE_API_KEY missing — cannot gather search context'}

    search_context = gather_search_context(lead, brave_key)

    rating_str = ''
    if lead.get('rating') and lead.get('reviews'):
        rating_str = f"{lead['rating']}★ across {lead['reviews']} reviews"

    prompt = PROMPT_TPL.format(
        name=lead['business_name'],
        category=lead.get('category') or 'small business',
        city=lead.get('city') or '',
        state=lead.get('state') or '',
        phone=lead.get('phone') or '',
        address=lead.get('address') or '',
        rating_str=rating_str or '(no rating data)',
        search_context=search_context,
    )

    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{'role': 'user', 'content': prompt}]
    )
    text = ''
    for block in resp.content:
        if block.type == 'text':
            text = block.text
    text = text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {'_raw': text, '_parse_error': True}


def research_batch(leads, max_workers=4, model='claude-haiku-4-5-20251001'):
    """Parallel research. max_workers=4 to respect Brave's 1qps free tier."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(research_lead, dict(l), model, client): l for l in leads}
        for f in as_completed(futs):
            lead = futs[f]
            try:
                out[lead['id']] = f.result()
            except Exception as e:
                out[lead['id']] = {'_error': str(e)}
    return out
