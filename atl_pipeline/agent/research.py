"""Research sub-agent — gathers signal about a lead via Brave Search, page
fetches, and (optionally) richer Outscraper data, then writes a structured
research_brief.

Always returns SOMETHING. If budget runs out mid-loop or tools fail, returns
whatever partial brief was built so far. The orchestrator decides what to do
with thin briefs (it never blocks publish on thin research).
"""
from __future__ import annotations
import json
import os
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import cost, tools, schemas


SYSTEM = """You are a thorough, honest small-business researcher. Your job is to gather verifiable facts about ONE business from web search and page fetches, then output a structured research brief.

Hard rules:
- Only state facts that appear verbatim or are clearly implied in the search results / pages you actually fetched. If you didn't see it, mark confidence 'unknown' and leave the field blank.
- NEVER invent owner names, founding years, certifications, named competitors, or quoted reviews.
- For every factual claim about the business (years in business, certifications, owner identity, awards), attach at least one source URL you actually fetched.
- Customer-segment and buyer-psychology are your inferred reads — those don't need citations, just plausibility.
- Vibe tags should describe the business's actual character (heritage/rugged/modern/premium/honest-trade/blue-collar/refined etc), not generic sales-speak.

Tool usage:
- You have a small budget of tool calls. Use them efficiently:
  1) brave_search to find authoritative pages (BBB, Yelp, LinkedIn, the business's own site)
  2) fetch_page on 1-3 of the most promising URLs
  3) outscraper_place_details ONLY if research feels thin and you have a place_id
- Stop searching when you have enough to write a useful brief. Don't burn calls chasing low-probability leads.

When done, output ONE final assistant message containing a single JSON object matching this schema:
{
  "owner": {"name": "string|''", "confidence": "high|medium|low|unknown", "sources": [url, ...]},
  "years_in_business": {"value": int|null, "confidence": "...", "sources": [...]},
  "claims": [{"text": "string (specific, citable claim)", "sources": [url, ...]}],
  "real_reviews": [{"author": "...", "text": "...", "stars": 5, "date": "YYYY-MM-DD", "source": "google|yelp|bbb"}],
  "photos": [{"url": "...", "caption": "...", "source": "..."}],
  "existing_site": null OR {"url": "...", "vibe": "...", "palette_hint": "..."},
  "service_specifics": [{"name": "...", "evidence": "snippet from a page"}],
  "customer_segment": "homeowner-residential|property-manager|commercial|mixed|unknown",
  "buyer_psychology": "price-sensitive|quality-first|speed-first|trust-first|tradition-first|unknown",
  "vibe_tags": ["heritage", "rugged", ...],
  "sources_visited": [url, ...]
}

Output ONLY the JSON, no commentary."""


TOOL_DEFS = [
    {
        'name': 'brave_search',
        'description': 'Search the web. Returns up to 10 results with title/url/description. Use for finding the business on review sites, social, BBB, their own site.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': 'Search query. Quote the business name to bias toward exact matches.'},
                'count': {'type': 'integer', 'description': 'Number of results (1-10).', 'default': 5},
            },
            'required': ['query'],
        },
    },
    {
        'name': 'fetch_page',
        'description': 'Fetch the plain-text content of one URL. Honors robots.txt. Returns title + first 4000 chars of text.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'url': {'type': 'string', 'description': 'Full URL with https://'},
            },
            'required': ['url'],
        },
    },
    {
        'name': 'outscraper_place_details',
        'description': 'Fetch richer Google Maps data (photos, full reviews, hours, description) for a known place_id. Use sparingly — costs cents per call. Only invoke if Brave Search yields little.',
        'input_schema': {
            'type': 'object',
            'properties': {
                'place_id': {'type': 'string', 'description': 'Google place_id from the lead row.'},
            },
            'required': ['place_id'],
        },
    },
]


def _dispatch_tool(name: str, args: dict) -> dict:
    if name == 'brave_search':
        return tools.tool_brave_search(args.get('query', ''), args.get('count', 5))
    if name == 'fetch_page':
        return tools.tool_fetch_page(args.get('url', ''))
    if name == 'outscraper_place_details':
        return tools.tool_outscraper_place_details(args.get('place_id', ''))
    return {'error': f'unknown tool: {name}'}


def _empty_brief() -> dict:
    return {
        'owner': {'name': '', 'confidence': 'unknown', 'sources': []},
        'years_in_business': {'value': None, 'confidence': 'unknown', 'sources': []},
        'claims': [],
        'real_reviews': [],
        'photos': [],
        'existing_site': None,
        'service_specifics': [],
        'customer_segment': 'unknown',
        'buyer_psychology': 'unknown',
        'vibe_tags': [],
        'sources_visited': [],
    }


def _parse_brief(text: str) -> Optional[dict]:
    """Try to extract a JSON object from the agent's final message."""
    if not text:
        return None
    s = text.strip()
    if s.startswith('```'):
        # Strip code fences
        s = s.split('\n', 1)[1] if '\n' in s else s
        if s.endswith('```'):
            s = s[:-3]
        s = s.strip()
    # Find outermost {...}
    first = s.find('{')
    last = s.rfind('}')
    if first == -1 or last == -1:
        return None
    try:
        return json.loads(s[first:last + 1])
    except json.JSONDecodeError:
        return None


def research_lead(
    lead: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5-20251001',
    max_tool_calls: int = 6,
    max_output_tokens: int = 2500,
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Run the research loop. Returns a research_brief dict, always.

    On budget exceeded or hard failure, returns whatever was built so far
    (possibly the empty brief). The orchestrator never blocks on this.
    """
    import anthropic  # lazy import — module loads without the SDK installed
    if client is None:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    initial = (
        f"Research this business for a website-sales pitch.\n\n"
        f"BUSINESS: {lead.get('business_name')}\n"
        f"Category: {lead.get('category')}\n"
        f"City, State: {lead.get('city')}, {lead.get('state')}\n"
        f"Phone: {lead.get('phone')}\n"
        f"Address: {lead.get('address')}\n"
        f"Place ID: {lead.get('place_id', '(unknown)')}\n"
        f"Google rating: {lead.get('rating')}★ across {lead.get('reviews')} reviews\n"
        f"\nGather facts via brave_search and fetch_page (and outscraper_place_details if needed). "
        f"Then output the final JSON research brief. Be honest about what you don't know."
    )

    messages: list[dict[str, Any]] = [
        {'role': 'user', 'content': initial},
    ]

    last_text = ''
    tool_calls_used = 0
    brief: Optional[dict] = None

    while tool_calls_used <= max_tool_calls:
        # Conservative pre-call cost check.
        est_input = sum(len(json.dumps(m, default=str)) // 4 for m in messages) + 500
        try:
            tracker.check_can_afford(model, est_input, max_output_tokens)
        except cost.BudgetExceeded:
            break

        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_output_tokens,
                system=SYSTEM,
                tools=TOOL_DEFS,
                messages=messages,
            )
        except Exception as e:
            # Network or API error — return whatever we have
            return (brief or _empty_brief()) | {'_error': f'api error: {e!r}'}

        # Record actual spend
        usage = getattr(resp, 'usage', None)
        if usage:
            tracker.record_call(
                model, usage.input_tokens, usage.output_tokens, label='research',
            )

        # Collect any text and any tool calls
        text_chunks = []
        tool_uses = []
        for block in resp.content:
            if block.type == 'text':
                text_chunks.append(block.text)
            elif block.type == 'tool_use':
                tool_uses.append(block)
        last_text = '\n'.join(text_chunks).strip()

        if resp.stop_reason == 'end_turn' and not tool_uses:
            # Agent is done; parse final brief
            brief = _parse_brief(last_text)
            break

        if not tool_uses:
            # No tools requested and stop_reason isn't end_turn — odd, treat as done
            brief = _parse_brief(last_text)
            break

        # Append assistant turn + tool_use blocks, then send tool_result back
        messages.append({'role': 'assistant', 'content': resp.content})
        tool_results = []
        for tu in tool_uses:
            tool_calls_used += 1
            result = _dispatch_tool(tu.name, tu.input or {})
            tool_results.append({
                'type': 'tool_result',
                'tool_use_id': tu.id,
                'content': json.dumps(result)[:4000],  # cap result size
            })
        messages.append({'role': 'user', 'content': tool_results})

        if tool_calls_used >= max_tool_calls:
            # Tool-call budget exhausted — ask for final brief in one more turn
            messages.append({
                'role': 'user',
                'content': 'Tool budget exhausted. Output the final JSON research brief now using what you have. No more tool calls.',
            })
            try:
                tracker.check_can_afford(model, est_input + 500, max_output_tokens)
            except cost.BudgetExceeded:
                break
            try:
                resp2 = client.messages.create(
                    model=model, max_tokens=max_output_tokens, system=SYSTEM,
                    messages=messages,  # NO tools this time — force final output
                )
            except Exception:
                break
            usage2 = getattr(resp2, 'usage', None)
            if usage2:
                tracker.record_call(model, usage2.input_tokens, usage2.output_tokens, label='research-final')
            final = '\n'.join(b.text for b in resp2.content if b.type == 'text').strip()
            brief = _parse_brief(final)
            break

    if not isinstance(brief, dict):
        return _empty_brief() | {'_warning': 'failed to parse brief; using empty', '_last_text': last_text[:500]}

    # Fill in any missing required keys with empty defaults (validators expect them)
    empty = _empty_brief()
    for k, v in empty.items():
        if k not in brief:
            brief[k] = v

    # Surface schema errors as warnings but DO NOT block return
    errs = schemas.validate_research_brief(brief)
    if errs:
        brief['_schema_warnings'] = errs[:10]

    return brief
