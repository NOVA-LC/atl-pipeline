"""Deep prospect research using Claude + targeted web queries.

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
  "brand_colors": ["#0A1F3F", "#C9A961"],          # picked from logo / truck / website
  "vibe": "stately heritage",                       # one-line vibe description
  "tagline_options": ["...", "..."],
  "pain_points_solved": ["..."],
  "real_reviews": [{"author":"...", "text":"...", "stars":5, "date":"...", "source":"yelp|google"}],
  "wow_facts": ["35 years family-owned", "5 metro locations"],
  "warning": null,                                  # e.g. "may be closed" or "duplicate listing"
  "sources": ["url1", "url2"]
}
"""
import os, json
import anthropic

RESEARCH_PROMPT = """You're researching a small business so a website-sales pitch can be hyper-personalized.

Business:
- Name: {name}
- Category: {category}
- City, State: {city}, {state}
- Phone: {phone}
- Address: {address}
- Google Maps URL: {google_maps_url}
{extra}

Goal: produce a structured JSON brief for the website demo.

Use web search to find:
1. **Owner's full name** — check LinkedIn, Facebook, GMB owner reply, BBB, Yelp owner replies, news mentions
2. **Owner's social profiles** — LinkedIn, personal Facebook, Instagram (only if linked to business)
3. **Founding year** + years in business (claim)
4. **Specific services** they actually do (vs the generic category)
5. **Service area / neighborhoods** they cover
6. **Brand colors** — from logo on Google, truck wraps, Facebook page header, any website screenshots
7. **Brand vibe** — formal/family/scrappy/premium/etc., one line
8. **2-3 real customer reviews** verbatim, with name + date + source (Yelp, Google, BBB)
9. **3 "wow" facts** the salesperson can drop in conversation
10. **Warnings** — flag if business looks closed, has a 2-star rating problem, or is a duplicate listing

Be honest. If you can't find something, say "unknown". Don't fabricate.

Return STRICT JSON matching the schema in the system prompt — no commentary, no markdown fences."""

SYSTEM = """You are a thorough, honest prospect researcher. You only state facts you have evidence for.

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

def research_lead(lead, model='claude-haiku-4-5-20251001', client=None):
    """Run deep research on one lead. Returns dict matching schema, or None on failure."""
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    extra = ''
    if lead.get('email'):
        extra += f"\n- Email on file: {lead['email']} (use the email-domain to find their website if any)"
    if lead.get('rating') and lead.get('reviews'):
        extra += f"\n- Google rating: {lead['rating']}★ across {lead['reviews']} reviews"

    prompt = RESEARCH_PROMPT.format(
        name=lead['business_name'],
        category=lead.get('category') or 'small business',
        city=lead.get('city') or '',
        state=lead.get('state') or '',
        phone=lead.get('phone') or '',
        address=lead.get('address') or '',
        google_maps_url=lead.get('google_maps_url') or '',
        extra=extra
    )
    resp = client.messages.create(
        model=model,
        max_tokens=4000,
        system=SYSTEM,
        tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 6}],
        messages=[{'role': 'user', 'content': prompt}]
    )
    # Find the final text block
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

def research_batch(leads, max_workers=6, model='claude-haiku-4-5-20251001'):
    """Parallel research. Returns dict[lead_id] -> research_payload."""
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
