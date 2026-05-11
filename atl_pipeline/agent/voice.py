"""Voice fingerprinting pipeline for the compose agent.

Per the copywriting research: Claude-default voice sanitizes everything,
especially when the owner has personality (old-school plumber who curses,
gruff auto-shop owner, folksy landscaper). Sites read "AI-generated"
because the model washes voice out of the source material.

This module reverses that. Given a lead's research_brief + raw Outscraper
reviews, it produces a `voice_card.json` that:

  - extracts numeric stylometric features (sentence-length distribution,
    contraction ratio, profanity rate, exclamation density, em-dash habit)
  - extracts qualitative features via one Claude call (signature_phrases,
    must_use, forbidden_for_this_owner, quotable_sentences, register,
    profanity_policy)
  - selects ~5 verbatim review snippets the page can pull-quote, picked
    by a Harry-Dry-style heuristic (specific noun + falsifiable claim
    + voice marker + 6-18 words + no PII beyond first name)
  - falls back to trade-region archetypes when the corpus is too thin

The voice_card flows into both the compose system prompt (as exemplars)
and the voice-fidelity critic (as ground truth to score against).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import cost
from .. import outscraper_fields as osf


# Heuristic profanity list — Atlanta blue-collar register tolerates mild
# damn/hell/crap; harder profanity flagged separately. Used for register
# detection, not censorship.
_MILD_PROFANITY = re.compile(r"\b(damn|hell|crap|sucks?|jackass|piss(ed)?|bs)\b", re.IGNORECASE)
_HARD_PROFANITY = re.compile(r"\b(fuck\w*|shit\w*|bitch\w*|asshole|bastard)\b", re.IGNORECASE)

_CONTRACTION_PATTERN = re.compile(r"\b\w+'\w+\b")
_WORD_PATTERN = re.compile(r"\b[\w']+\b")
_SENTENCE_PATTERN = re.compile(r"(?<=[.!?])\s+")
_REGIONAL_MARKERS = re.compile(r"\b(y'all|fixin' to|yonder|reckon|fittin' to|holla|hollering|hollerin'|ain't|gonna|gotta|wanna|cuz|y'all's)\b", re.IGNORECASE)


# Trade-region archetypes for the no-corpus fallback. Atlanta SMB
# trade verticals only — extend as we move into new markets.
ARCHETYPES: dict[str, dict[str, Any]] = {
    'plumber': {
        'register': 'blue_collar',
        'profanity_policy': 'never',
        'contraction_ratio': 0.45,
        'must_use_at_least_once': ['fix it right the first time', 'flat-rate', 'show up when we say we will'],
        'signature_phrases': ['flat-rate', 'no surprise charges', 'same-day'],
        'forbidden_for_this_owner': ['elevate', 'seamless', 'transform', 'unlock'],
        'sentence_rhythm': 'short_punchy',
        'exclamation_habit': 'sparing',
    },
    'hvac': {
        'register': 'professional',
        'profanity_policy': 'never',
        'contraction_ratio': 0.35,
        'must_use_at_least_once': ['flat-rate diagnostic', 'NATE-certified', 'comfort'],
        'signature_phrases': ['comfort you can count on', 'transparent pricing'],
        'forbidden_for_this_owner': ['elevate', 'seamless', 'transform', 'cutting-edge'],
        'sentence_rhythm': 'mixed',
        'exclamation_habit': 'sparing',
    },
    'radiator': {
        'register': 'gruff',
        'profanity_policy': 'mild_damn_hell',
        'contraction_ratio': 0.55,
        'must_use_at_least_once': ['been doing this for', 'won\'t bullshit you', 'we fix what others won\'t'],
        'signature_phrases': ['been at it', 'old-school'],
        'forbidden_for_this_owner': ['elevate', 'seamless', 'transform', 'modern', 'premium'],
        'sentence_rhythm': 'short_punchy',
        'exclamation_habit': 'none',
    },
    'landscape': {
        'register': 'warm',
        'profanity_policy': 'never',
        'contraction_ratio': 0.40,
        'must_use_at_least_once': ['real plants', 'crew shows up', 'priced upfront'],
        'signature_phrases': ['real materials', 'we don\'t cut corners on soil'],
        'forbidden_for_this_owner': ['elevate', 'transform', 'curate'],
        'sentence_rhythm': 'mixed',
        'exclamation_habit': 'sparing',
    },
    'septic': {
        'register': 'folksy',
        'profanity_policy': 'never',
        'contraction_ratio': 0.50,
        'must_use_at_least_once': ['24/7', 'no upcharge for nights', 'we\'ve seen it all'],
        'signature_phrases': ['septic emergency', 'we\'ll get there'],
        'forbidden_for_this_owner': ['elevate', 'transform', 'premium'],
        'sentence_rhythm': 'mixed',
        'exclamation_habit': 'sparing',
    },
}


def _empty_card() -> dict:
    return {
        'register': 'unknown',
        'profanity_policy': 'never',
        'contraction_ratio': 0.0,
        'signature_phrases': [],
        'regionalisms': [],
        'must_use_at_least_once': [],
        'forbidden_for_this_owner': [],
        'quotable_sentences': [],
        'sentence_rhythm': 'mixed',
        'capitalization_quirks': 'standard',
        'exclamation_habit': 'sparing',
        'em_dash_rate': 0.0,
        '_source': 'empty',
        '_corpus_word_count': 0,
    }


# -----------------------------------------------------------------------------
# Corpus collection
# -----------------------------------------------------------------------------

def collect_corpus(lead: dict, research_brief: dict | None = None) -> dict:
    """Pull reviews + owner replies + brief claims into structured corpus.

    Returns a dict with:
      reviews_customer: [{author, text, stars, date}]  — written by customers
      owner_voice:      [{text, source}]               — written by the owner
      total_word_count: int
    """
    out = {'reviews_customer': [], 'owner_voice': [], 'total_word_count': 0}

    # Real Google reviews from raw_outscraper
    osf_data = osf.parse_all(lead.get('raw_outscraper'))
    for r in osf_data.get('reviews') or []:
        if isinstance(r, dict) and r.get('text'):
            out['reviews_customer'].append({
                'author': r.get('author', '—'),
                'text': r['text'],
                'stars': r.get('stars', 5),
                'date': r.get('date', ''),
            })

    # GBP description = owner voice
    desc = osf_data.get('description')
    if desc and len(desc) > 40:
        out['owner_voice'].append({'text': desc, 'source': 'gbp_description'})

    # Research brief — claims may include owner-direct snippets
    if isinstance(research_brief, dict):
        for c in research_brief.get('claims') or []:
            if isinstance(c, dict) and c.get('text'):
                out['owner_voice'].append({'text': c['text'], 'source': 'research_claim'})
        # real_reviews from brief (mostly redundant with osf, but be defensive)
        for r in research_brief.get('real_reviews') or []:
            if isinstance(r, dict) and r.get('text'):
                if not any(rc['text'] == r['text'] for rc in out['reviews_customer']):
                    out['reviews_customer'].append({
                        'author': r.get('author', '—'), 'text': r['text'],
                        'stars': r.get('stars', 5), 'date': r.get('date', ''),
                    })

    total = sum(len(_WORD_PATTERN.findall(r['text'])) for r in out['reviews_customer'])
    total += sum(len(_WORD_PATTERN.findall(o['text'])) for o in out['owner_voice'])
    out['total_word_count'] = total
    return out


# -----------------------------------------------------------------------------
# Numeric stylometry — cheap, deterministic, catches what LLM eyeballing misses
# -----------------------------------------------------------------------------

def numeric_features(corpus: dict) -> dict:
    """Compute stylometric numeric features from corpus dict.

    Owner_voice weighted 3× over customer reviews when present (customers
    don't write in the owner's voice, but their compliments often *quote*
    the owner — useful weak signal regardless).
    """
    all_text: list[tuple[str, float]] = []
    for o in corpus.get('owner_voice') or []:
        all_text.append((o['text'], 3.0))
    for r in corpus.get('reviews_customer') or []:
        all_text.append((r['text'], 1.0))

    if not all_text:
        return {
            'sentence_count': 0, 'word_count': 0, 'avg_sentence_words': 0.0,
            'sentence_word_stddev': 0.0, 'contraction_ratio': 0.0,
            'mild_profanity_per_100w': 0.0, 'hard_profanity_per_100w': 0.0,
            'exclamation_per_100w': 0.0, 'em_dash_per_100w': 0.0,
            'allcaps_token_ratio': 0.0, 'regional_marker_count': 0,
        }

    total_sentences = 0
    sentence_lengths: list[float] = []
    total_words = 0
    weighted_words = 0.0
    contractions = 0
    mild_prof = 0
    hard_prof = 0
    exclamations = 0
    em_dashes = 0
    allcaps_tokens = 0
    regional_hits = 0

    for text, weight in all_text:
        if not text:
            continue
        sentences = [s.strip() for s in _SENTENCE_PATTERN.split(text) if s.strip()]
        total_sentences += len(sentences)
        for s in sentences:
            words_in_s = _WORD_PATTERN.findall(s)
            sentence_lengths.append(len(words_in_s))
        words = _WORD_PATTERN.findall(text)
        total_words += len(words)
        weighted_words += len(words) * weight
        contractions += len(_CONTRACTION_PATTERN.findall(text))
        mild_prof += len(_MILD_PROFANITY.findall(text))
        hard_prof += len(_HARD_PROFANITY.findall(text))
        exclamations += text.count('!')
        em_dashes += text.count('—') + text.count('–')
        for w in words:
            if len(w) >= 3 and w.isupper() and w.isalpha():
                allcaps_tokens += 1
        regional_hits += len(_REGIONAL_MARKERS.findall(text))

    avg_len = sum(sentence_lengths) / max(len(sentence_lengths), 1)
    variance = sum((x - avg_len) ** 2 for x in sentence_lengths) / max(len(sentence_lengths), 1)
    stddev = variance ** 0.5
    word_count_safe = max(total_words, 1)

    return {
        'sentence_count': total_sentences,
        'word_count': total_words,
        'avg_sentence_words': round(avg_len, 2),
        'sentence_word_stddev': round(stddev, 2),
        'contraction_ratio': round(contractions / word_count_safe, 4),
        'mild_profanity_per_100w': round((mild_prof / word_count_safe) * 100, 2),
        'hard_profanity_per_100w': round((hard_prof / word_count_safe) * 100, 2),
        'exclamation_per_100w': round((exclamations / word_count_safe) * 100, 2),
        'em_dash_per_100w': round((em_dashes / word_count_safe) * 100, 2),
        'em_dash_rate': round(em_dashes / max(total_sentences, 1), 3),
        'allcaps_token_ratio': round(allcaps_tokens / word_count_safe, 4),
        'regional_marker_count': regional_hits,
    }


# -----------------------------------------------------------------------------
# Verbatim quote selection — Harry-Dry-style filter
# -----------------------------------------------------------------------------

_CONCRETE_NOUN_HINTS = re.compile(
    r'\b('
    # places + neighborhoods
    r'atlanta|marietta|smyrna|decatur|alpharetta|roswell|sandy springs|kennesaw|woodstock|johns creek|brookhaven|dunwoody|buckhead|midtown|east cobb|east point|college park|stone mountain'
    # trade-specific gear/job nouns
    r'|drain|pipe|leak|clog|backup|water heater|sewer|septic tank|drainfield|main line|toilet|sink|disposal|sump'
    r'|furnace|condenser|compressor|air handler|coil|ductwork|refrigerant|filter|thermostat|hvac|ac|a\.c\.'
    r'|radiator|hose|valve|gasket|transmission|alternator|brake|exhaust'
    r'|mower|sod|mulch|hedge|driveway|patio|lawn|tree|stump'
    # money/time
    r'|\$\d+|\d+\s*(am|pm|min(?:ute)?s?|hr|hour|hours|day|days|week|weeks|month|months|year|years)'
    r')\b',
    re.IGNORECASE,
)


def _is_quotable(text: str) -> bool:
    """Filter: 6–18 words, concrete noun OR falsifiable claim, has voice marker."""
    if not text:
        return False
    words = _WORD_PATTERN.findall(text)
    if not (6 <= len(words) <= 28):  # slightly wider than research's 6-18, real review data is messy
        return False
    has_concrete = bool(_CONCRETE_NOUN_HINTS.search(text))
    has_voice = bool(_CONTRACTION_PATTERN.search(text)
                     or _REGIONAL_MARKERS.search(text)
                     or _MILD_PROFANITY.search(text)
                     or '!' in text)
    has_specific_number = bool(re.search(r'\d', text))
    # Quotable if (concrete OR has a specific number) AND has at least one voice marker
    return (has_concrete or has_specific_number) and has_voice


def select_quotable(reviews: list[dict], limit: int = 5) -> list[dict]:
    """Pick verbatim review snippets the demo page can pull-quote."""
    out = []
    seen_signatures = set()
    for r in reviews or []:
        text = (r.get('text') or '').strip()
        if not text:
            continue
        # Try the whole review first; if too long, try splitting into sentences
        candidates = [text] if len(_WORD_PATTERN.findall(text)) <= 28 else [
            s.strip() for s in _SENTENCE_PATTERN.split(text)
        ]
        for snippet in candidates:
            if not _is_quotable(snippet):
                continue
            # Dedupe by first 30 chars
            sig = snippet.lower()[:30]
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            author = r.get('author', '—')
            # Reduce author to first name + last initial
            parts = str(author).strip().split()
            if len(parts) >= 2 and len(parts[-1]) >= 1:
                author = f'{parts[0]} {parts[-1][0]}.'
            elif parts:
                author = parts[0]
            out.append({
                'text': snippet.strip().rstrip('.,;:'),
                'author': author,
                'date': r.get('date', ''),
                'stars': r.get('stars', 5),
                'source': 'google',
            })
            break  # one snippet per review
        if len(out) >= limit:
            break
    return out


# -----------------------------------------------------------------------------
# Qualitative card via Claude — the half stylometry can't compute
# -----------------------------------------------------------------------------

VOICE_CARD_SYSTEM = """You are a forensic linguist profiling a single small-business owner from their own writing and from customer reviews about them. You will see TWO corpora:

1. REVIEWS — written ABOUT the business by customers. Useful for vocabulary the owner's customers throw back at the owner, NOT for the owner's own voice.
2. OWNER_VOICE — written BY the owner (their Google Business description, their direct posts, replies they wrote to reviews). Weight this 10× over customer reviews.

Your job is forensic, not polite. If the owner curses, record it. If they spell 'ain't' without an apostrophe, record THAT spelling. Do not sanitize.

Return ONE JSON object matching this schema. No prose, no preamble:

{
  "register": "blue_collar | folksy | professional | gruff | warm | refined",
  "profanity_policy": "never | mild_damn_hell | whatever_they_use",
  "signature_phrases": [exact strings the owner uses 2+ times — verbatim],
  "regionalisms": [e.g. "y'all", "fixin' to", "yonder"],
  "must_use_at_least_once": [3-5 phrases that ARE this owner — must appear once across the generated page],
  "forbidden_for_this_owner": [words/phrases that would sound fake in their mouth — at minimum: any banned phrase you'd predict the LLM trying to write],
  "sentence_rhythm": "short_punchy | medium | long_rambling | mixed",
  "capitalization_quirks": "string describing observed pattern or 'standard'",
  "exclamation_habit": "none | sparing | frequent",
  "voice_summary": "one sentence describing how this owner sounds when they write"
}

If the OWNER_VOICE corpus is < 50 words, mark register='unknown' and return empty arrays — there isn't enough signal."""


def qualitative_card(
    corpus: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Single Claude call to extract the qualitative half of the voice card.

    On budget exceeded or parse failure returns an empty card — caller
    falls back to numeric + archetype.
    """
    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (KeyError, ImportError):
            return {}

    owner_text = '\n\n'.join(o['text'] for o in corpus.get('owner_voice') or [])[:2500]
    customer_text = '\n\n'.join(
        f'[{r.get("stars", "?")}★] {r["text"]}' for r in (corpus.get('reviews_customer') or [])[:15]
    )[:3500]

    user = (
        f'<owner_voice>\n{owner_text or "(none provided)"}\n</owner_voice>\n\n'
        f'<reviews>\n{customer_text or "(none provided)"}\n</reviews>\n\n'
        'Output the voice card JSON now.'
    )

    est_input = (len(VOICE_CARD_SYSTEM) + len(user)) // 4 + 200
    try:
        tracker.check_can_afford(model, est_input, 800)
    except cost.BudgetExceeded:
        return {}

    try:
        resp = client.messages.create(
            model=model, max_tokens=800,
            system=[{'type': 'text', 'text': VOICE_CARD_SYSTEM,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception:
        return {}

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='voice-card')

    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text
        if text.endswith('```'):
            text = text[:-3]
    first = text.find('{')
    last = text.rfind('}')
    if first == -1 or last == -1:
        return {}
    try:
        return json.loads(text[first:last + 1])
    except json.JSONDecodeError:
        return {}


# -----------------------------------------------------------------------------
# Archetype fallback for thin-corpus prospects
# -----------------------------------------------------------------------------

def archetype_card(industry: str) -> dict:
    """Return a hand-curated voice card for the given trade vertical.

    Used when corpus.total_word_count < threshold OR qualitative_card returned
    empty. Conservative — assumes no profanity, generic blue-collar register.
    """
    base = ARCHETYPES.get(industry, ARCHETYPES['hvac']).copy()
    base['regionalisms'] = []
    base['voice_summary'] = f'Trade-region archetype for {industry}; no real corpus available.'
    base['_source'] = 'archetype'
    return base


# -----------------------------------------------------------------------------
# Main entry
# -----------------------------------------------------------------------------

def extract_voice_card(
    lead: dict,
    research_brief: dict | None,
    tracker: cost.CostTracker,
    industry: str | None = None,
    client: 'Optional[anthropic.Anthropic]' = None,
    model: str = 'claude-haiku-4-5',
    thin_threshold_words: int = 120,
) -> dict:
    """Build a voice_card.json for one lead. Always returns a dict.

    Sequence:
      1. Collect corpus from raw_outscraper + research_brief
      2. Compute numeric features (deterministic, free)
      3. If owner_voice has >= thin_threshold_words: Claude qualitative call
         Else: use trade-region archetype
      4. Select 5 verbatim quotable sentences from real customer reviews
      5. Merge: numeric + qualitative/archetype + quotables → voice_card
    """
    from .. import photo_library as pl
    if industry is None:
        industry = pl.industry_for(lead.get('category'))

    corpus = collect_corpus(lead, research_brief)
    numeric = numeric_features(corpus)
    owner_words = sum(len(_WORD_PATTERN.findall(o['text'])) for o in corpus.get('owner_voice') or [])

    # Qualitative pass only when we have enough owner voice; otherwise archetype
    if owner_words >= thin_threshold_words:
        qual = qualitative_card(corpus, tracker, model=model, client=client)
        if not qual:
            qual = archetype_card(industry)
            qual['_source'] = 'archetype_after_claude_fail'
        else:
            qual['_source'] = 'claude'
    else:
        qual = archetype_card(industry)
        qual['_source'] = 'archetype_thin_corpus'

    # Verbatim quotables from real customer reviews
    quotables = select_quotable(corpus.get('reviews_customer') or [], limit=5)

    # Merge
    card = _empty_card()
    card.update(numeric)
    for k, v in qual.items():
        if v is not None and v != []:
            card[k] = v
    card['quotable_sentences'] = quotables
    card['_corpus_word_count'] = corpus['total_word_count']
    card['_owner_voice_word_count'] = owner_words
    card['_industry'] = industry

    # Auto-derive em_dash_rate from numeric
    card['em_dash_rate'] = numeric.get('em_dash_rate', 0.0)

    return card


def card_summary_for_prompt(card: dict) -> str:
    """Compact human-readable summary of the voice card for embedding in
    the composer's system prompt as ground truth.
    """
    lines = []
    lines.append(f"register: {card.get('register', 'unknown')}")
    lines.append(f"profanity_policy: {card.get('profanity_policy', 'never')}")
    lines.append(f"sentence_rhythm: {card.get('sentence_rhythm', 'mixed')} (avg {card.get('avg_sentence_words', 0)} words, stddev {card.get('sentence_word_stddev', 0)})")
    lines.append(f"contraction_ratio: {card.get('contraction_ratio', 0)}")
    lines.append(f"em_dash_rate: {card.get('em_dash_rate', 0)}")
    lines.append(f"exclamation_habit: {card.get('exclamation_habit', 'sparing')}")
    sigs = card.get('signature_phrases') or []
    if sigs:
        lines.append(f"signature_phrases: {sigs}")
    regs = card.get('regionalisms') or []
    if regs:
        lines.append(f"regionalisms: {regs}")
    must = card.get('must_use_at_least_once') or []
    if must:
        lines.append(f"MUST_USE (at least once across the page): {must}")
    forb = card.get('forbidden_for_this_owner') or []
    if forb:
        lines.append(f"FORBIDDEN (never use): {forb}")
    quotables = card.get('quotable_sentences') or []
    if quotables:
        lines.append('verbatim_quotables (use at least one as a pull-quote):')
        for q in quotables[:5]:
            lines.append(f"  - \"{q['text']}\" — {q.get('author', '—')}")
    return '\n'.join(lines)
