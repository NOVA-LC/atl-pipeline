"""Headline factory — 25-generate + rubric-score-select pattern.

Per Ogilvy: "When you have written your headline, you have spent eighty
cents out of your dollar." The compose call writes a competent headline
inline, but headlines are too high-leverage to leave to the same call
that's juggling layout + copy + voice. This module runs two extra calls
specifically on the headline: 25 candidates across 5 axes, then a
fresh-context selection pass with a 14-point rubric.

Uses Haiku for both calls — total ~$0.009/lead. Falls back gracefully:
on budget overrun or parse failure, the compose-call headline is kept.

Selection rubric (max score 14, pass at >= 10):
  Specificity 0/1/2          — has a number, place, or named thing
  30-sec scan 0/1/2          — WHAT + WHERE + WHO clear in 3 sec
  4U hit-count 0/1/2         — Useful, Urgent, Unique, Ultra-specific
  Voice fidelity 0/1/2       — matches voice_card cadence
  Falsifiability 0/1/2       — no banned adjectives, claims traceable
  Differentiation 0/1/2      — would 5 competitors also write this?
  Mobile readability 0/1/2   — <=10 words, scannable
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import cost, banned, voice


GEN_SYSTEM = """You are a direct-response copy chief writing hero headlines for a local-services business website. Output ONLY a JSON object — no prose, no fences.

You will generate exactly 25 headline candidates varying across 5 axes (5 each):
  - question form ("Burst pipe in Marietta at 11pm?")
  - statement form ("Same-day plumbing in East Cobb.")
  - number-led ("3,400 backups cleared since 2008.")
  - neighborhood-led ("The plumber East Cobb actually keeps.")
  - pain-led ("Water spreading? Truck in 47 minutes.")

Hard constraints on EVERY candidate:
- ≤ 10 words. Mobile-readable.
- Each must contain at least ONE of: specific number, dollar amount, year,
  neighborhood name, brand/tool name, certification, time-window, named
  person. No abstractions ("trusted", "professional", "premier" forbidden).
- Voice card matters — match register, contractions, em-dash habit, etc.
- BANNED PHRASES (instant rejection of that candidate): industry leader,
  best in class, premier, trusted, leading, quality service, top-rated,
  professional service, committed to excellence, your trusted partner,
  exciting opportunity, act now, limited time, world-class, cutting-edge,
  state-of-the-art, one-stop shop, attention to detail, seamless,
  transform, elevate, unlock, empower, delve, "in today's...", look no further.
- BANNED PATTERNS: rule-of-three triplets, "it's not just X, it's Y",
  "whether you're X or Y", em-dash in headline (UNLESS voice_card permits).

Output schema (return EXACTLY this shape):
{
  "candidates": [
    {"axis": "question|statement|number_led|neighborhood_led|pain_led",
     "headline": "<≤ 10 words>",
     "uses_fact": "<brief.path that backs this candidate>"}
  ]
}

The array MUST contain exactly 25 entries, 5 per axis. If you can't make a
candidate that satisfies the constraints for an axis, output your best
attempt anyway and a fresh-context selection pass will reject it."""


SELECT_SYSTEM = """You are a direct-response copy chief grading 25 hero
headline candidates against a 7-criterion rubric. You did NOT write these.
Be ruthless. Output ONLY JSON.

For each candidate score 0/1/2 on each of these 7 criteria (max 14):
  1. Specificity      — contains a number, place, or named thing? 0=none, 1=one, 2=multiple
  2. 30-sec scan      — WHAT + WHERE + WHO clear in 3 seconds? 0=unclear, 2=instant
  3. 4U hit-count     — Useful + Urgent + Unique + Ultra-specific. 0=none, 2=all four
  4. Voice fidelity   — matches the supplied voice_card cadence/register? 0=clashes, 2=natural
  5. Falsifiability   — no banned adjectives, every claim traceable? 0=fluff, 2=verifiable
  6. Differentiation  — would 5 competitors also write this? 0=yes-they-would, 2=distinctive
  7. Mobile readable  — ≤10 words, no clause-stacking, no buried subject? 0=no, 2=yes

PASS threshold: total ≥ 10. Output the WINNER (highest total; ties broken
by Specificity then Differentiation) plus 2 runners-up, and a list of
rejected_reasons keyed by candidate index for the rest.

Output schema:
{
  "winner": {
    "index": <int>,
    "headline": "<verbatim>",
    "score": <int 0-14>,
    "breakdown": {"specificity": <int>, "scan_30s": <int>, "4u_hits": <int>,
                  "voice_fidelity": <int>, "falsifiability": <int>,
                  "differentiation": <int>, "mobile_readable": <int>},
    "why": "<one sentence>"
  },
  "runners_up": [
    {"index": <int>, "headline": "<verbatim>", "score": <int>, "why": "<one line>"}
  ],
  "rejected_reasons": {"<index>": "<one line>"}
}"""


def _user_for_gen(lead: dict, research_brief: dict, voice_card: dict) -> str:
    voice_summary = voice.card_summary_for_prompt(voice_card) if voice_card else ''
    return (
        f"BUSINESS\n"
        f"  name: {lead.get('business_name')}\n"
        f"  category: {lead.get('category')}\n"
        f"  city: {lead.get('city')}, {lead.get('state')}\n"
        f"  rating: {lead.get('rating')}★ across {lead.get('reviews')} Google reviews\n"
        f"\nVOICE CARD\n{voice_summary or '(use trade-vertical archetype, short-punchy register, no profanity)'}\n"
        f"\nRESEARCH BRIEF\n{json.dumps(research_brief or {}, indent=2)[:3000]}\n\n"
        f"Generate 25 candidates now. Output JSON only."
    )


def _user_for_select(candidates: list, voice_card: dict, lead: dict) -> str:
    voice_summary = voice.card_summary_for_prompt(voice_card) if voice_card else ''
    rows = [f'  [{i}] (axis={c.get("axis", "?")}) {c.get("headline", "")}' for i, c in enumerate(candidates)]
    return (
        f"BUSINESS: {lead.get('business_name')} · {lead.get('category')} · {lead.get('city')}\n\n"
        f"VOICE CARD\n{voice_summary or '(trade archetype)'}\n\n"
        f"CANDIDATES\n" + '\n'.join(rows) +
        f"\n\nScore each, pick the winner + 2 runners-up. Output JSON only."
    )


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    s = text.strip()
    if s.startswith('```'):
        s = s.split('\n', 1)[1] if '\n' in s else s
        if s.endswith('```'):
            s = s[:-3]
    first = s.find('{')
    last = s.rfind('}')
    if first == -1 or last == -1:
        return None
    try:
        return json.loads(s[first:last + 1])
    except json.JSONDecodeError:
        return None


def generate_candidates(
    lead: dict,
    research_brief: dict,
    voice_card: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> list[dict]:
    """Return the 25-candidate list (may have fewer on parse fail). Each
    element: {axis, headline, uses_fact}.
    """
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (ImportError, KeyError):
            return []

    user = _user_for_gen(lead, research_brief, voice_card)
    est_input = (len(GEN_SYSTEM) + len(user)) // 4 + 150
    try:
        tracker.check_can_afford(model, est_input, 1500)
    except cost.BudgetExceeded:
        return []

    try:
        resp = client.messages.create(
            model=model, max_tokens=1500,
            system=[{'type': 'text', 'text': GEN_SYSTEM,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception:
        return []

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='headline-gen')

    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    parsed = _parse_json(text)
    if not parsed or not isinstance(parsed.get('candidates'), list):
        return []

    # Filter out any candidate with a banned phrase / latent tell
    em_dash_ok = bool(voice_card and voice_card.get('em_dash_rate', 0) > 0.05)
    clean = []
    for c in parsed['candidates']:
        if not isinstance(c, dict) or not c.get('headline'):
            continue
        h = c['headline']
        if banned.find_banned(h):
            continue
        if banned.find_latent_tells(h, allow_em_dash=em_dash_ok):
            continue
        if len(h.split()) > 12:  # tolerance over the 10-word target
            continue
        clean.append(c)
    return clean


def select_best(
    candidates: list[dict],
    lead: dict,
    voice_card: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Rubric-grade the candidate list and pick the winner. Returns
    {'winner': {headline, score, ...}, 'runners_up': [...]}. Empty dict on
    failure — caller keeps the compose-generated headline.
    """
    if not candidates:
        return {}
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (ImportError, KeyError):
            return {}

    user = _user_for_select(candidates, voice_card, lead)
    est_input = (len(SELECT_SYSTEM) + len(user)) // 4 + 100
    try:
        tracker.check_can_afford(model, est_input, 600)
    except cost.BudgetExceeded:
        return {}

    try:
        resp = client.messages.create(
            model=model, max_tokens=600,
            system=[{'type': 'text', 'text': SELECT_SYSTEM,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception:
        return {}

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='headline-select')

    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    parsed = _parse_json(text) or {}

    # Defensive: confirm winner.headline matches a candidate verbatim — model
    # sometimes paraphrases. If it doesn't match, look up by index.
    winner = parsed.get('winner') or {}
    if winner.get('headline'):
        if not any(c.get('headline') == winner['headline'] for c in candidates):
            idx = winner.get('index')
            if isinstance(idx, int) and 0 <= idx < len(candidates):
                winner['headline'] = candidates[idx]['headline']
                winner['_corrected_from_paraphrase'] = True
            else:
                winner = {}
    parsed['winner'] = winner
    return parsed


def run_factory(
    lead: dict,
    research_brief: dict,
    voice_card: dict,
    tracker: cost.CostTracker,
    composed_headline: str | None = None,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Generate + select. Returns {'winner', 'runners_up', 'candidates_count',
    'kept_compose_headline': bool}. Falls back to compose-generated headline
    on any failure.
    """
    candidates = generate_candidates(lead, research_brief, voice_card, tracker, model=model, client=client)
    if not candidates:
        return {'kept_compose_headline': True, 'winner': {'headline': composed_headline or ''},
                'candidates_count': 0}

    selection = select_best(candidates, lead, voice_card, tracker, model=model, client=client)
    winner = (selection or {}).get('winner') or {}
    if not winner.get('headline'):
        return {'kept_compose_headline': True, 'winner': {'headline': composed_headline or ''},
                'candidates_count': len(candidates)}

    # If the winning score is below pass threshold (10/14), prefer the
    # compose-generated headline — it was written under the same constraints
    # and has the benefit of full-page context.
    if isinstance(winner.get('score'), int) and winner['score'] < 10 and composed_headline:
        return {'kept_compose_headline': True,
                'winner': {'headline': composed_headline},
                'candidates_count': len(candidates),
                'selection_winner': winner,
                'reason': f"factory winner scored {winner['score']}/14, below pass threshold; kept compose headline"}

    return {
        'kept_compose_headline': False,
        'winner': winner,
        'runners_up': (selection or {}).get('runners_up', []),
        'candidates_count': len(candidates),
    }
