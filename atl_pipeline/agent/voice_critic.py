"""Voice-fidelity critic — hostile audit of composed copy vs. voice_card.

Per the voice-preservation research, generation-and-critique-in-the-same-call
is too lenient. The model grades itself charitably. This critic runs as a
separate Claude call with a different system prompt and a hostile posture
("assume the writer is a generic AI; catch it").

Scores each section against 5 axes:
  1. contraction_ratio match — KS-test-style: within 30% of voice_card target
  2. sentence_word_stddev — must be > 4 (uniformity is the LLM tell)
  3. signature_phrase usage — at least one must_use_at_least_once string used
  4. banned-phrase / latent-tell hits — programmatic, hostile
  5. verbatim_quote presence — at least one section uses a quotable verbatim

Verdict: ship | regenerate | fallback_to_verbatim_quotes
  - regenerate: any banned phrase OR fidelity_score < 0.75 OR no verbatim quote in hero/reviews
  - fallback: after 2 regenerations still failing
  - ship: clean enough to publish
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import banned as banned_mod, cost, voice as voice_mod


_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"\b[\w']+\b")
_CONTRACTION = re.compile(r"\b\w+'\w+\b")


def _stats_for_text(text: str) -> dict:
    """Compute the numeric features the critic compares against voice_card."""
    if not text:
        return {'word_count': 0, 'sentence_count': 0, 'avg_len': 0.0,
                'stddev': 0.0, 'contraction_ratio': 0.0, 'em_dashes': 0}
    sentences = [s.strip() for s in _SENTENCE.split(text) if s.strip()]
    sentence_lens = [len(_WORD.findall(s)) for s in sentences]
    words = _WORD.findall(text)
    avg = sum(sentence_lens) / max(len(sentence_lens), 1) if sentence_lens else 0.0
    var = sum((x - avg) ** 2 for x in sentence_lens) / max(len(sentence_lens), 1) if sentence_lens else 0.0
    return {
        'word_count': len(words),
        'sentence_count': len(sentences),
        'avg_len': round(avg, 2),
        'stddev': round(var ** 0.5, 2),
        'contraction_ratio': round(len(_CONTRACTION.findall(text)) / max(len(words), 1), 4),
        'em_dashes': text.count('—') + text.count('–'),
    }


def _flatten_copy(composed: dict) -> dict:
    """Pull text from the composed_page.copy dict into per-section strings."""
    copy = composed.get('copy') or {}
    services_text = ' '.join(
        f"{s.get('title', '')}. {s.get('body', '')}"
        for s in copy.get('services') or [] if isinstance(s, dict)
    )
    reviews_text = ' '.join(
        f"{r.get('text', '')}"
        for r in copy.get('reviews_list') or [] if isinstance(r, dict)
    )
    return {
        'hero': ' '.join(filter(None, [
            copy.get('eyebrow', ''), copy.get('headline_top', ''),
            copy.get('headline_em', ''), copy.get('hero_sub', ''),
            copy.get('hero_cta_text', ''),
        ])),
        'services': ' '.join(filter(None, [copy.get('services_h', ''),
                                            copy.get('services_lead', ''),
                                            services_text])),
        'gallery': copy.get('gallery_h', ''),
        'reviews': ' '.join(filter(None, [copy.get('reviews_h', ''), reviews_text])),
        'cta': ' '.join(filter(None, [copy.get('cta_h', ''), copy.get('cta_sub', '')])),
        'footer': ' '.join(filter(None, [copy.get('footer_blurb', ''),
                                          copy.get('title_tagline', ''),
                                          copy.get('meta_description', '')])),
    }


def algorithmic_audit(composed: dict, voice_card: dict) -> dict:
    """Pure-Python audit — no LLM call. Returns per-section issue list."""
    if not composed or not isinstance(composed.get('copy'), dict):
        return {'verdict': 'ship', 'sections': {}, 'fidelity_score': 0.0,
                'issues': ['empty composed_page'], '_source': 'algorithmic'}

    sections_text = _flatten_copy(composed)
    em_dash_ok = bool(voice_card and voice_card.get('em_dash_rate', 0) > 0.05)

    must_use = list(voice_card.get('must_use_at_least_once') or [])
    forbidden = [f.lower() for f in (voice_card.get('forbidden_for_this_owner') or [])]
    target_contraction = voice_card.get('contraction_ratio', 0.35)
    target_register = voice_card.get('register', 'unknown')

    # Aggregate
    all_text = ' '.join(sections_text.values())
    all_text_lower = all_text.lower()

    issues: list[dict] = []

    # Per-section: banned + latent tells
    for section_name, text in sections_text.items():
        if not text:
            continue
        # Headlines/H2s: stricter — no em-dashes, ever (unless voice allows)
        is_headline_block = section_name in ('hero',)  # eyebrow + h1 + h2-ish
        bad = banned_mod.find_banned(text)
        if bad:
            issues.append({
                'section': section_name, 'kind': 'banned_phrase',
                'severity': 'critical', 'evidence': bad,
            })
        tells = banned_mod.find_latent_tells(text, allow_em_dash=em_dash_ok)
        for t in tells:
            issues.append({
                'section': section_name, 'kind': 'latent_tell',
                'severity': 'critical' if t['pattern'] in ('not-just-X-its-Y', 'whether-youre-X-or-Y', 'look-no-further', 'bold-colon-list') else 'warn',
                'pattern': t['pattern'], 'matched': t['matched'],
            })

    # forbidden_for_this_owner words
    for word in forbidden:
        if word and word in all_text_lower:
            issues.append({'kind': 'forbidden_for_owner', 'severity': 'critical',
                          'evidence': word})

    # must_use_at_least_once — at least one of the phrases should appear once total
    if must_use:
        used = [p for p in must_use if p.lower() in all_text_lower]
        if not used:
            issues.append({'kind': 'missing_must_use', 'severity': 'warn',
                          'expected_any_of': must_use[:5]})

    # Sentence-rhythm uniformity check (only for sufficient text)
    aggregate_stats = _stats_for_text(all_text)
    if aggregate_stats['sentence_count'] >= 8 and aggregate_stats['stddev'] < 4.0:
        issues.append({'kind': 'cadence_uniformity', 'severity': 'warn',
                      'evidence': f"stddev {aggregate_stats['stddev']} < 4.0 (LLM tell)"})

    # Contraction-ratio match (warn at >50% relative delta)
    if target_contraction > 0 and aggregate_stats['word_count'] >= 50:
        delta = abs(aggregate_stats['contraction_ratio'] - target_contraction) / max(target_contraction, 0.01)
        if delta > 0.6:
            issues.append({'kind': 'contraction_mismatch', 'severity': 'warn',
                          'evidence': f"got {aggregate_stats['contraction_ratio']} vs target {target_contraction} (delta {delta:.2f})"})

    # Verbatim-quote presence — at least one quotable should appear in reviews_list
    quotables = voice_card.get('quotable_sentences') or []
    if quotables:
        reviews_text = sections_text.get('reviews', '')
        used_quote = any((q.get('text', '')[:30].lower() in reviews_text.lower())
                         for q in quotables[:5])
        if not used_quote:
            issues.append({'kind': 'no_verbatim_quote', 'severity': 'critical',
                          'evidence': 'reviews_list does not contain any voice_card.quotable_sentences verbatim'})

    # Compute fidelity score: start at 1.0, subtract per-issue weight
    score = 1.0
    for i in issues:
        if i['severity'] == 'critical':
            score -= 0.18
        else:
            score -= 0.06
    score = max(0.0, score)

    # Verdict
    critical_count = sum(1 for i in issues if i.get('severity') == 'critical')
    if critical_count >= 2 or score < 0.4:
        verdict = 'fallback_to_verbatim_quotes'
    elif critical_count >= 1 or score < 0.75:
        verdict = 'regenerate'
    else:
        verdict = 'ship'

    return {
        'verdict': verdict,
        'fidelity_score': round(score, 3),
        'issues': issues,
        'sections_stats': {k: _stats_for_text(v) for k, v in sections_text.items()},
        'critical_count': critical_count,
        '_source': 'algorithmic',
    }


# -----------------------------------------------------------------------------
# Optional LLM-based hostile review — fires only if algorithmic gave verdict
# != 'ship' AND we have budget. Catches subtler register/tone issues the
# algorithmic audit misses.
# -----------------------------------------------------------------------------

HOSTILE_SYSTEM = """You are a hostile copy auditor catching AI-generated marketing copy. Your hypothesis is that EVERY page in front of you was written by an LLM and is trying to hide it. Your job is to catch that.

You'll see a composed marketing page's copy + the owner's voice card. Score each of 6 sections (hero, services, about/gallery, reviews, cta, footer/meta) on whether they would pass as written by THIS owner.

Be hostile. Bias toward 'regenerate'. A page that 'sounds fine' is suspicious — real human SMB copy is bumpier than that.

Specifically scan for:
- Cadence uniformity (all sentences ~18-24 words = LLM tell)
- 'Bold term: explanation' list format
- 'It's not just X — it's Y' / 'Not just X, but Y'
- 'Whether you're X or Y'
- Rule-of-three triplets ('fast, reliable, and affordable')
- Title-case feature names invented from thin air ('Premium Drain Solutions')
- Sales-buzzword adjectives (trusted, leading, premier, dedicated, passionate)
- 'In today's fast-paced/digital/evolving world' openers
- Em-dash in marketing headlines (unless voice_card permits)
- Soft modal stacking ('can help you to be able to')
- 'Here's the thing,' / 'Let's dive in' transitions
- Sanitized profanity (owner curses in their corpus; copy doesn't)
- Missing voice_card.must_use_at_least_once phrases
- Missing verbatim review quote

Output ONLY this JSON:
{
  "section_scores": {
    "hero": {"score_0_10": <int>, "tells": ["..."], "verdict": "ship|regenerate"},
    "services": {...}, "about": {...}, "reviews": {...},
    "cta": {...}, "footer": {...}
  },
  "overall_fidelity": <float 0.0-1.0>,
  "verdict": "ship | regenerate | fallback_to_verbatim_quotes",
  "rewrite_hint": "<one-line specific instruction for the regen pass>",
  "biggest_tell": "<one sentence describing the most obvious AI tell you caught>"
}"""


def hostile_review(
    composed: dict,
    voice_card: dict,
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Optional second-pass hostile review. Cheap (~$0.002/lead). Returns
    a verdict dict; on any failure returns {} so caller falls back to the
    algorithmic audit's verdict alone.
    """
    if client is None:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (ImportError, KeyError):
            return {}

    copy = composed.get('copy') or {}
    voice_summary = voice_mod.card_summary_for_prompt(voice_card) if voice_card else ''
    user = (
        f"VOICE CARD\n{voice_summary or '(no card)'}\n\n"
        f"COMPOSED COPY (the page's full copy block)\n"
        f"{json.dumps(copy, indent=2)[:5000]}\n\n"
        f"Audit it. Be hostile. Output the JSON verdict."
    )

    est_input = (len(HOSTILE_SYSTEM) + len(user)) // 4 + 100
    try:
        tracker.check_can_afford(model, est_input, 800)
    except cost.BudgetExceeded:
        return {}

    try:
        resp = client.messages.create(
            model=model, max_tokens=800,
            system=[{'type': 'text', 'text': HOSTILE_SYSTEM,
                     'cache_control': {'type': 'ephemeral'}}],
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception:
        return {}

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='voice-critic')

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


def audit(
    composed: dict,
    voice_card: dict,
    tracker: cost.CostTracker,
    hostile_pass: bool = True,
    model: str = 'claude-haiku-4-5',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Run the full audit pipeline.

    1. Always run algorithmic audit (free, deterministic, catches the
       obvious tells).
    2. If algo verdict != 'ship' AND hostile_pass requested AND budget
       available, run the hostile LLM review for nuance.
    3. Combine — LLM verdict wins on ambiguity, algo wins on hard rule
       violations (banned phrases, forbidden words).

    Returns the merged verdict dict.
    """
    algo = algorithmic_audit(composed, voice_card)
    algo_critical = algo.get('critical_count', 0)

    if not hostile_pass or algo.get('verdict') == 'ship':
        return algo

    llm = hostile_review(composed, voice_card, tracker, model=model, client=client)
    if not llm:
        return algo

    # Combine: if algorithmic flagged critical (banned phrase, forbidden
    # word, no verbatim quote), respect that even if LLM says ship.
    merged_verdict = llm.get('verdict', algo['verdict'])
    if algo_critical >= 1 and merged_verdict == 'ship':
        merged_verdict = 'regenerate'

    return {
        'verdict': merged_verdict,
        'fidelity_score': round((algo['fidelity_score'] + float(llm.get('overall_fidelity', 0.5))) / 2, 3),
        'algorithmic': algo,
        'llm': llm,
        'rewrite_hint': llm.get('rewrite_hint', ''),
        'biggest_tell': llm.get('biggest_tell', ''),
        '_source': 'algorithmic+llm',
    }
