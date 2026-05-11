"""Banned phrases and citation enforcement for agent-generated copy.

We do NOT want cookie-cutter sales-speak ("industry leader", "best in class")
or fabricated factual claims. Every claim about the business must be backed by
a source URL the research agent gathered, OR the claim is stripped before
render. Render fails outright on banned phrases.
"""
from __future__ import annotations
import re
from typing import Iterable


# Lowercased substrings. Match is case-insensitive on the rendered copy.
BANNED_PHRASES = [
    'industry leader',
    'best in class',
    'best-in-class',
    'synergy',
    'leverage',  # tolerated in some technical contexts; risk acceptable to ban broadly
    'transform',
    'elevate',
    'premier',
    'trusted partner',
    'exciting opportunity',
    'act now',
    'limited time',
    'world-class',
    'world class',
    'cutting-edge',
    'cutting edge',
    'state-of-the-art',
    'state of the art',
    'one-stop shop',
    'one stop shop',
    'attention to detail',  # cliche, banned to force specificity
    'family-owned and operated',  # cliche; allowed if rephrased with specifics
    # Tier-2 additions (per copywriting research):
    'trusted',
    'leading',
    'quality service',
    'top-rated',
    'professional service',
    'committed to excellence',
    'your satisfaction is our priority',
    'we go above and beyond',
    'second to none',
    'passionate',
    'dedicated',
    'unlock',
    'empower',
    'delve',
    'seamless',
    'in today',  # catches "in today's fast-paced world" etc.
    "let's dive in",
    "let's explore",
    "here's the thing",
    'hot take',
]


# Latent AI tells — patterns that betray a model wrote the copy even when no
# banned phrases appear. Each is a compiled regex; matched at critic-time.
LATENT_AI_TELLS = [
    # "It's not just X — it's Y" / "Not just X, but Y" construction
    (re.compile(r"\bit's not just\b|\bnot just\s+\w+[,—-]\s*it'?s\b", re.IGNORECASE), 'not-just-X-its-Y'),
    # Em-dash in marketing copy (allowed in body if voice card permits; flagged
    # by default — banned if voice_card.em_dash_rate < threshold)
    (re.compile(r'—|–'), 'em-dash-in-marketing'),
    # Bold-term-colon-explanation list ("**Reliability:** We show up.")
    (re.compile(r'\*\*[A-Z][A-Za-z\s]{2,30}\*\*:\s', ), 'bold-colon-list'),
    # Rule-of-three triplets: "fast, reliable, and affordable"
    (re.compile(r'\b\w+,\s+\w+,\s+and\s+\w+\b', re.IGNORECASE), 'rule-of-three'),
    # "Whether you're X or Y" audience-fork
    (re.compile(r"\bwhether you'?re\b", re.IGNORECASE), 'whether-youre-X-or-Y'),
    # "Whether it's X or Y"
    (re.compile(r"\bwhether it'?s\b", re.IGNORECASE), 'whether-its-X-or-Y'),
    # Title-case feature names invented by the model
    (re.compile(r'\b(?:Premium|Comprehensive|Advanced|Total|Complete)\s+[A-Z][a-z]+\s+(?:Solutions|Services|Care|Experience)\b'), 'titlecase-feature'),
    # Soft modal stacking: "can help you to be able to"
    (re.compile(r'\bcan help you to\b|\bbe able to be\b', re.IGNORECASE), 'soft-modal-stack'),
    # Generic pseudo-quantification with no source
    (re.compile(r'\bover\s+\d+%\s+of\s+(homeowners|customers|clients|americans)\b', re.IGNORECASE), 'pseudo-quant'),
    # Parallel verb stacking — "We diagnose, we repair, we restore" (3+ first-person verbs)
    (re.compile(r'\b(?:we|i)\s+\w+,\s+(?:we|i)\s+\w+,\s+(?:we|i)\s+\w+\b', re.IGNORECASE), 'parallel-verb-stack'),
    # "Look no further" — local-services AI default
    (re.compile(r'\blook no further\b', re.IGNORECASE), 'look-no-further'),
]


# Claims that need a citation: any "since YYYY", "X years", "licensed and insured",
# specific certifications, awards, named competitors, owner-name claims.
CITED_CLAIM_PATTERNS = [
    re.compile(r'\bsince\s+(19|20)\d{2}\b', re.IGNORECASE),
    re.compile(r'\b\d{1,3}\s+years\s+(in business|of experience|serving)\b', re.IGNORECASE),
    re.compile(r'\blicensed\s+and\s+insured\b', re.IGNORECASE),
    re.compile(r'\bnate[-\s]?certified\b', re.IGNORECASE),
    re.compile(r'\bbbb\s+accredited\b', re.IGNORECASE),
    re.compile(r'\baward[-\s]?winning\b', re.IGNORECASE),
    re.compile(r'\b#1\s+(\w+\s+){0,3}in\b', re.IGNORECASE),
]


def find_banned(text: str) -> list[str]:
    """Return list of banned phrases found in text (case-insensitive)."""
    if not text:
        return []
    low = text.lower()
    return [p for p in BANNED_PHRASES if p in low]


def find_uncited_claims(text: str, sources: Iterable[str]) -> list[str]:
    """Return list of claim-phrases in text that lack supporting sources.

    Heuristic: if a claim pattern matches AND there are zero source URLs,
    we flag it. Refined matching (does THIS source back THIS claim) requires
    the agent to attach claim→source mapping in research_brief; this function
    is the coarse render-time gate.
    """
    if not text:
        return []
    sources = list(sources or [])
    found = []
    for pat in CITED_CLAIM_PATTERNS:
        for m in pat.finditer(text):
            phrase = m.group(0)
            # If no sources at all, every claim is uncited
            if not sources:
                found.append(phrase)
            # Otherwise we trust the agent's claim→source mapping (enforced
            # separately in compose stage); this is the fallback last gate.
    return found


def scrub_banned(text: str) -> tuple[str, list[str]]:
    """Remove banned-phrase sentences from text. Returns (clean_text, removed_phrases).

    Used as a soft-fail recovery when the orchestrator decides to publish
    despite minor violations (we still strip them from the actual output).
    """
    if not text:
        return text, []
    removed = find_banned(text)
    if not removed:
        return text, []
    clean = text
    # Remove the whole sentence containing each banned phrase
    sentences = re.split(r'(?<=[.!?])\s+', clean)
    kept = []
    for s in sentences:
        low = s.lower()
        if any(b in low for b in removed):
            continue
        kept.append(s)
    return ' '.join(kept).strip(), removed


def assert_clean(text: str, sources: Iterable[str] = ()) -> None:
    """Raise ValueError if text contains banned phrases. Render-time gate.

    Use sparingly — the orchestrator prefers scrub_banned() since we always
    want to publish *something*. Reserved for catastrophic violations.
    """
    bad = find_banned(text)
    if bad:
        raise ValueError(f'banned phrases in copy: {bad!r}')


def find_latent_tells(text: str, allow_em_dash: bool = False) -> list[dict]:
    """Scan text for the latent AI-tell regex patterns. Returns a list of
    {pattern, matched_text, span} dicts. Allow em-dash if voice_card permits
    them in the owner's actual writing.
    """
    if not text:
        return []
    hits = []
    for pat, name in LATENT_AI_TELLS:
        if allow_em_dash and name == 'em-dash-in-marketing':
            continue
        for m in pat.finditer(text):
            hits.append({'pattern': name, 'matched': m.group(0)[:80], 'span': [m.start(), m.end()]})
    return hits


def clean_copy_dict(copy: dict) -> tuple[dict, list[str]]:
    """Walk every string value in a copy dict, scrub banned phrases. Returns
    (cleaned_copy, all_phrases_removed). Default path for the agent.
    """
    if not isinstance(copy, dict):
        return copy, []
    out = {}
    all_removed = []
    for k, v in copy.items():
        if isinstance(v, str):
            cleaned, removed = scrub_banned(v)
            # If scrubbing left an empty string, keep original (better than blank)
            out[k] = cleaned if cleaned else v
            all_removed.extend(removed)
        elif isinstance(v, list):
            new_list = []
            for item in v:
                if isinstance(item, str):
                    cleaned, removed = scrub_banned(item)
                    new_list.append(cleaned if cleaned else item)
                    all_removed.extend(removed)
                else:
                    new_list.append(item)
            out[k] = new_list
        else:
            out[k] = v
    return out, all_removed
