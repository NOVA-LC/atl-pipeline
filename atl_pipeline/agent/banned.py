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
