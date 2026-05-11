"""Critic — grades a composed page against a quality bar encoded from real
top-tier modern web design (Linear, Stripe, Vercel, Anthropic, agency-tier
landing pages: real photography, type-driven hierarchy, depth/motion cues,
specificity over generic, cohesive palette + typography).

Uses one Haiku call. Cheap (~$0.003/lead). Returns:
  {
    'verdict': 'pass' | 'revise',
    'quality_score': 1..10,
    'weaknesses': [str, ...],
    'revision_hints': {...},
    'similarity_to_neighbors': float,
  }

The orchestrator may use the verdict to trigger ONE revise pass. Always
publishes either way — verdict='revise' twice still ships, just tags
agent_status='degraded_similar' or 'degraded_low_quality'.
"""
from __future__ import annotations
import json
import os
from typing import Iterable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import assemble, cost


# Pass at score >= this. Below: trigger one revise pass.
PASS_THRESHOLD = 7
# Even after revision, if max similarity to recent neighbors exceeds this,
# orchestrator tags degraded_similar (still publishes).
SIMILARITY_CEILING = 0.75


SYSTEM = """You are a senior web design critic. Your reference standard is the visual quality bar of top-tier modern websites — Linear, Stripe, Vercel, Anthropic, Awwwards-featured agency sites, premium design studio portfolios. Hallmarks of that bar:

- Real photography or strong original imagery — never generic stock when business-owned photos exist
- Specific, business-particular copy — never sales-speak ("industry leader", "premier", "trusted partner")
- Type-driven visual hierarchy — display typeface chosen to match brand vibe, not the default for every site
- Cohesive palette — accent color appears purposefully, not sprinkled
- Sectional rhythm — each section has a distinct visual role; no two consecutive sections feel the same
- Depth and dimension cues — shadows, layered photography, real motion/parallax cues (where appropriate)
- Confident negative space — generous whitespace, not crammed
- Anti-template feel — looks composed for THIS specific business, not stamped from a template
- 3D/depth/motion language where industry permits (premium services, modern trades) — flatness is fine for industrial/heritage trades, but the COMPOSITION must still feel intentional

You will be given:
1. A composed_page JSON describing the page choices and copy
2. A research_brief snippet showing what facts were available
3. Fingerprints of recent neighbor demos (to flag clone risk)

Your job: grade it 1-10 against the quality bar. Be honest. A 6 is "average template, would not make a buyer say wow." A 7 is "starts to feel intentional." An 8 is "this would convince me they hired a designer." 9-10 is rare.

If score < 7: return verdict='revise' and give SPECIFIC actionable hints (e.g. 'palette feels generic for this rugged auto-shop vibe — try rugged-shop-orange or modern-charcoal', 'services_lead is sales-speak, rewrite to mention the actual neighborhoods', 'hero photo is industry stock — the brief had 3 real Google photos, use real_photos[0]').

If score >= 7: return verdict='pass'.

Also check: does this composition look TOO SIMILAR to any of the neighbor_fingerprints? If yes and quality is borderline, lean toward 'revise'.

Output ONE JSON object:
{
  "quality_score": <int 1-10>,
  "verdict": "pass" | "revise",
  "weaknesses": ["specific issue 1", "specific issue 2", ...],
  "strengths": ["specific strength 1", ...],
  "revision_hints": {
    "change_palette_to": "<name from catalog>" | null,
    "change_type_pair_to": "<name>" | null,
    "change_sections": {"<kind>": "<new variant>"} | {},
    "rewrite_copy_fields": ["hero_sub", "services_lead", ...],
    "specific_instructions": "string"
  }
}

Output ONLY the JSON."""


def _algorithmic_similarity(composed_fp: dict, neighbor_fps: Iterable[dict]) -> tuple[float, Optional[dict]]:
    """Quick pre-check before the LLM. Returns (max_sim, closest_fp)."""
    max_sim = 0.0
    closest = None
    for n in neighbor_fps:
        if not isinstance(n, dict):
            continue
        s = assemble.similarity(composed_fp, n)
        if s > max_sim:
            max_sim = s
            closest = n
    return max_sim, closest


def critique(
    composed: dict,
    composed_fp: dict,
    research_brief: dict,
    neighbor_fps: Iterable[dict],
    tracker: cost.CostTracker,
    model: str = 'claude-haiku-4-5-20251001',
    client: 'Optional[anthropic.Anthropic]' = None,
) -> dict:
    """Grade the composed page. Always returns a verdict dict, never raises."""
    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (KeyError, ImportError):
            # No API — fall back to algorithmic-only verdict
            max_sim, closest = _algorithmic_similarity(composed_fp, neighbor_fps)
            return {
                'verdict': 'revise' if max_sim >= SIMILARITY_CEILING else 'pass',
                'quality_score': 5,
                'weaknesses': ['no LLM critic available; algorithmic check only'],
                'revision_hints': {},
                'similarity_to_neighbors': round(max_sim, 4),
                'closest_neighbor_fp': closest,
            }

    neighbors_list = list(neighbor_fps)[:10]
    max_sim, closest = _algorithmic_similarity(composed_fp, neighbors_list)

    user = (
        f"COMPOSED PAGE:\n{json.dumps(composed, indent=2)[:4000]}\n\n"
        f"RESEARCH BRIEF (what facts were available):\n"
        f"  owner: {research_brief.get('owner', {})}\n"
        f"  years: {research_brief.get('years_in_business', {})}\n"
        f"  real photos: {len(research_brief.get('photos', []))} available\n"
        f"  real reviews: {len(research_brief.get('real_reviews', []))} available\n"
        f"  vibe_tags: {research_brief.get('vibe_tags', [])}\n"
        f"  customer_segment: {research_brief.get('customer_segment')}\n"
        f"  buyer_psychology: {research_brief.get('buyer_psychology')}\n\n"
        f"NEIGHBOR FINGERPRINTS (recent demos in our pipeline — flag clone risk):\n"
        f"{json.dumps(neighbors_list, indent=2)[:1500]}\n\n"
        f"ALGORITHMIC SIMILARITY: max Jaccard vs neighbors = {round(max_sim, 3)}\n\n"
        f"Grade against the top-tier-modern-website bar. Return the JSON verdict."
    )

    est_input = (len(SYSTEM) + len(user)) // 4 + 200
    try:
        tracker.check_can_afford(model, est_input, 1200)
    except cost.BudgetExceeded:
        # Skip the LLM critic, return algorithmic-only verdict
        return {
            'verdict': 'pass' if max_sim < SIMILARITY_CEILING else 'revise',
            'quality_score': 6,
            'weaknesses': ['budget exceeded before critic; algorithmic check only'],
            'revision_hints': {},
            'similarity_to_neighbors': round(max_sim, 4),
            'closest_neighbor_fp': closest,
        }

    try:
        resp = client.messages.create(
            model=model, max_tokens=1200, system=SYSTEM,
            messages=[{'role': 'user', 'content': user}],
        )
    except Exception as e:
        return {
            'verdict': 'pass', 'quality_score': 5,
            'weaknesses': [f'critic call failed: {e!r}'],
            'revision_hints': {},
            'similarity_to_neighbors': round(max_sim, 4),
            'closest_neighbor_fp': closest,
        }

    usage = getattr(resp, 'usage', None)
    if usage:
        tracker.record_call(model, usage.input_tokens, usage.output_tokens, label='critic')

    text = '\n'.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1] if '\n' in text else text
        if text.endswith('```'):
            text = text[:-3]
    first = text.find('{')
    last = text.rfind('}')
    verdict_data: Optional[dict] = None
    if first != -1 and last != -1:
        try:
            verdict_data = json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            verdict_data = None

    if not isinstance(verdict_data, dict):
        return {
            'verdict': 'pass', 'quality_score': 5,
            'weaknesses': ['critic JSON parse failed'],
            '_raw_critic_text': text[:500],
            'revision_hints': {},
            'similarity_to_neighbors': round(max_sim, 4),
        }

    # Coerce + cap fields
    score = verdict_data.get('quality_score', 5)
    try:
        score = int(score)
    except (TypeError, ValueError):
        score = 5
    score = max(1, min(10, score))

    verdict = verdict_data.get('verdict', 'pass' if score >= PASS_THRESHOLD else 'revise')
    if verdict not in ('pass', 'revise'):
        verdict = 'pass' if score >= PASS_THRESHOLD else 'revise'

    # Force revise if similarity is too high regardless of LLM verdict
    if max_sim >= SIMILARITY_CEILING and verdict == 'pass':
        verdict = 'revise'
        verdict_data.setdefault('weaknesses', []).append(
            f'similarity to neighbor demo = {round(max_sim, 3)} — too close, pick different palette/sections'
        )

    return {
        'verdict': verdict,
        'quality_score': score,
        'weaknesses': verdict_data.get('weaknesses', []),
        'strengths': verdict_data.get('strengths', []),
        'revision_hints': verdict_data.get('revision_hints', {}),
        'similarity_to_neighbors': round(max_sim, 4),
        'closest_neighbor_fp': closest,
    }


def neighbor_fingerprints_from_db(conn, limit: int = 10, exclude_lead_id: int | None = None) -> list[dict]:
    """Pull fingerprint_inputs JSON from recent agent-built demos."""
    sql = """SELECT fingerprint FROM leads
             WHERE fingerprint IS NOT NULL AND fingerprint != ''
               AND agent_status LIKE 'agent_built%' OR agent_status LIKE 'degraded_%'"""
    args: list = []
    if exclude_lead_id is not None:
        sql += ' AND id != ?'
        args.append(exclude_lead_id)
    sql += ' ORDER BY updated_at DESC LIMIT ?'
    args.append(max(1, min(limit * 2, 50)))
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r['fingerprint'])
            if isinstance(d, dict):
                out.append(d)
        except Exception:
            continue
        if len(out) >= limit:
            break
    return out
