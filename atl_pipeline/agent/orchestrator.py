"""Orchestrator — wires research → compose → critic → assemble → publish.

CORE CONTRACT: Every lead that enters build_for_lead() emerges with HTML
written to disk. The variable is QUALITY (agent_status), not WHETHER.

agent_status values, every one = a live published site:
  agent_built              — full agent path succeeded
  degraded_thin_research   — research thin, assembler used Phase 1 defaults
  degraded_similar         — agent composed but too similar after retry
  degraded_budget          — budget cap hit; published partial best-effort
  degraded_compose_failed  — compose returned empty; assembler used defaults
  degraded_render          — shell render failed; legacy fallback published
"""
from __future__ import annotations
import datetime
import json
import os
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import anthropic

from . import (
    assemble, banned, catalog, compose, cost, critic, research, schemas,
    voice, headline, voice_critic,
)
from .. import db, deploy
from .. import outscraper_fields as osf
from .. import photo_library as pl


def _now_iso() -> str:
    return datetime.datetime.utcnow().isoformat(timespec='seconds')


def build_for_lead(
    lead_id: int,
    per_lead_cap_cents: int = 15,
    daily_cap_cents: int = 1000,
    research_model: str = 'claude-haiku-4-5',
    compose_model: str = 'claude-sonnet-4-6',
    repo_path: Optional[str] = None,
    client: 'Optional[anthropic.Anthropic]' = None,
    full_catalog: Optional[dict] = None,
) -> dict:
    """Build, render, and persist one agent-composed demo.

    Returns a result dict:
      {
        'lead_id', 'slug', 'agent_status', 'html_path',
        'cost_usd', 'fingerprint', 'warnings': [...],
        'effective_choices': {...},
      }

    NEVER raises. If something blows up, falls back to Phase 1 generate() and
    still writes HTML to disk, then returns with status='degraded_render' or
    similar.
    """
    repo_path = repo_path or os.environ.get('DEMOS_REPO_LOCAL', './demos_repo')
    if client is None:
        try:
            import anthropic  # lazy
            client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
        except (KeyError, ImportError) as e:
            return _publish_phase1_fallback(lead_id, repo_path, reason=f'no anthropic: {e!r}')

    if full_catalog is None:
        full_catalog = catalog.load_all()

    tracker = cost.CostTracker(
        per_lead_cap_cents=per_lead_cap_cents,
        daily_cap_cents=daily_cap_cents,
    )
    tracker.reset_per_lead()

    log: list[dict] = []
    warnings: list[str] = []
    agent_status = 'agent_built'

    # 1. Load lead from DB
    with db.conn() as c:
        row = db.get_lead(c, lead_id)
    if not row:
        return {'lead_id': lead_id, 'agent_status': 'error_no_lead'}
    lead = dict(row)

    # 2. Research sub-agent (always returns SOMETHING; may be empty brief)
    log.append({'ts': _now_iso(), 'stage': 'research', 'event': 'start'})
    research_brief: dict = {}
    try:
        research_brief = research.research_lead(
            lead, tracker, model=research_model, client=client,
        )
    except cost.BudgetExceeded as e:
        warnings.append(f'research budget exceeded: {e}')
        agent_status = 'degraded_budget'
    except Exception as e:
        warnings.append(f'research crashed: {e!r}')

    log.append({'ts': _now_iso(), 'stage': 'research', 'event': 'done',
                'cost_so_far_cents': round(tracker.per_lead_spent_cents, 2)})

    if schemas.is_brief_thin(research_brief):
        agent_status = 'degraded_thin_research' if agent_status == 'agent_built' else agent_status
        warnings.append('research brief thin — falling back to Phase 1 defaults for unspecified copy')

    # 2b. Voice fingerprinting — extract per-lead voice card from reviews +
    # owner-written content. Always returns a dict; uses trade-region archetype
    # when the corpus is too thin. ~$0.003/lead when Claude qualitative call
    # fires; $0 for the archetype path.
    voice_card: dict = {}
    log.append({'ts': _now_iso(), 'stage': 'voice', 'event': 'start'})
    try:
        industry = pl.industry_for(lead.get('category'))
        voice_card = voice.extract_voice_card(
            lead, research_brief, tracker,
            industry=industry, client=client,
        )
    except cost.BudgetExceeded:
        warnings.append('voice budget exceeded; falling back to archetype only')
        try:
            voice_card = voice.archetype_card(pl.industry_for(lead.get('category')))
        except Exception:
            voice_card = {}
    except Exception as e:
        warnings.append(f'voice extraction crashed: {e!r}')
    log.append({'ts': _now_iso(), 'stage': 'voice', 'event': 'done',
                'source': voice_card.get('_source', 'unknown'),
                'owner_voice_words': voice_card.get('_owner_voice_word_count', 0)})

    # 2b. PTC-lite design candidate generation (Tier 4) — runs Haiku 4.5
    # N times with temperature spread to enumerate (palette, type_pair,
    # sections) candidates, scores them for anti-clone + vertical fit +
    # doctrine compliance, picks the winner. Compose then runs once with
    # the design pinned, focusing its budget on copy.
    # Non-blocking: failures fall through to compose's own design picks.
    if tracker.per_lead_spent_cents < tracker.per_lead_cap_cents:
        try:
            from . import design_ptc
            ptc_result = design_ptc.pick_design(
                lead, voice_card, neighbor_fps, tracker,
                full_catalog=full_catalog, client=client,
            )
            if ptc_result.get('design_hint'):
                research_brief = dict(research_brief or {})
                research_brief['_design_hint'] = ptc_result['design_hint']
                log.append({
                    'ts': _now_iso(), 'stage': 'design-ptc',
                    'winner_palette': ptc_result['design_hint'].get('palette'),
                    'winner_type_pair': ptc_result['design_hint'].get('type_pair'),
                    'winner_score': ptc_result['winner_score'],
                    'n_rejected': len(ptc_result['rejected_candidates']),
                    'cost_cents': ptc_result['cost_cents'],
                })
            elif ptc_result.get('errors'):
                warnings.extend([f'design-ptc: {e}' for e in ptc_result['errors']])
        except Exception as e:
            warnings.append(f'design-ptc crashed: {e!r}')

    # 3. Composition sub-agent — may run twice (revise once)
    composed: dict = {}
    if tracker.per_lead_spent_cents < tracker.per_lead_cap_cents:
        log.append({'ts': _now_iso(), 'stage': 'compose', 'event': 'start'})
        try:
            composed = compose.compose_page(
                lead, research_brief, tracker,
                model=compose_model, client=client, full_catalog=full_catalog,
                voice_card=voice_card,
            )
            # If PTC pinned the design, compose may have legitimately omitted
            # the design fields from its top-level output (we told it "design
            # is decided"). Backfill from the hint so assemble doesn't fall
            # through to defaults.
            dh = (research_brief or {}).get('_design_hint') or {}
            if dh:
                composed.setdefault('palette', dh.get('palette'))
                composed.setdefault('type_pair', dh.get('type_pair'))
                if dh.get('sections') and not composed.get('sections'):
                    composed['sections'] = dh['sections']
        except cost.BudgetExceeded as e:
            warnings.append(f'compose budget exceeded: {e}')
            agent_status = 'degraded_budget'
        except Exception as e:
            warnings.append(f'compose crashed: {e!r}')

        if not composed or composed.get('_parse_error'):
            warnings.append('compose returned empty or unparseable — assembler will use industry defaults')
            if agent_status == 'agent_built':
                agent_status = 'degraded_compose_failed'

        log.append({'ts': _now_iso(), 'stage': 'compose', 'event': 'done',
                    'cost_so_far_cents': round(tracker.per_lead_spent_cents, 2)})

    # 3b. Headline factory — 25-gen + rubric-grade selection, Haiku.
    # Replaces composed.copy.headline_top/headline_em only when the factory's
    # winner scores >= 10/14; otherwise compose's headline is kept.
    if composed and not composed.get('_parse_error') and tracker.per_lead_spent_cents < tracker.per_lead_cap_cents:
        try:
            composed_headline = (composed.get('copy') or {}).get('headline_top', '')
            hf = headline.run_factory(
                lead, research_brief, voice_card, tracker,
                composed_headline=composed_headline, client=client,
            )
            log.append({'ts': _now_iso(), 'stage': 'headline-factory',
                        'kept_compose': hf.get('kept_compose_headline', True),
                        'candidates': hf.get('candidates_count', 0),
                        'winner_score': (hf.get('winner') or {}).get('score'),
                        'winner': (hf.get('winner') or {}).get('headline', '')[:80]})
            if not hf.get('kept_compose_headline') and hf.get('winner', {}).get('headline'):
                if 'copy' not in composed:
                    composed['copy'] = {}
                composed['copy']['headline_top'] = hf['winner']['headline']
                # Headline factory winners are single-line; clear the italicized
                # split so the hero renders as one strong line.
                composed['copy']['headline_em'] = ''
        except cost.BudgetExceeded:
            warnings.append('headline factory: budget exceeded, kept compose headline')
        except Exception as e:
            warnings.append(f'headline factory crashed (kept compose headline): {e!r}')

    # 3c. Voice-fidelity critic — hostile audit of composed copy vs voice_card.
    # Algorithmic pass is free; optional LLM hostile pass fires only if algo
    # flagged issues. Verdict can trigger a single compose-revise (4 below).
    voice_verdict: dict = {}
    if composed and not composed.get('_parse_error'):
        try:
            voice_verdict = voice_critic.audit(
                composed, voice_card, tracker,
                hostile_pass=True, client=client,
            )
            log.append({'ts': _now_iso(), 'stage': 'voice-critic',
                        'verdict': voice_verdict.get('verdict'),
                        'fidelity_score': voice_verdict.get('fidelity_score'),
                        'issue_count': len(voice_verdict.get('issues', [])) if voice_verdict.get('issues') else len(voice_verdict.get('algorithmic', {}).get('issues', [])),
                        'biggest_tell': voice_verdict.get('biggest_tell', '')[:120]})
        except Exception as e:
            warnings.append(f'voice critic crashed: {e!r}')

    # 4. Critic (quality grader against top-tier-website rubric) + optional revise pass
    if composed and not composed.get('_parse_error'):
        # Build the prospective fingerprint by running assemble once
        first_render = assemble.assemble(lead, composed, research_brief)
        prospective_fp = first_render['fingerprint_inputs']
        # Pull recent neighbor fingerprints for clone-risk check
        with db.conn() as c:
            neighbor_fps = critic.neighbor_fingerprints_from_db(
                c, limit=10, exclude_lead_id=lead_id,
            )
        verdict = critic.critique(
            composed, prospective_fp, research_brief, neighbor_fps,
            tracker, client=client,
        )
        log.append({
            'ts': _now_iso(), 'stage': 'critic',
            'verdict': verdict['verdict'],
            'quality_score': verdict.get('quality_score'),
            'similarity': verdict.get('similarity_to_neighbors'),
            'weaknesses': verdict.get('weaknesses', [])[:5],
        })

        if verdict['verdict'] == 'revise' and tracker.per_lead_spent_cents < tracker.per_lead_cap_cents:
            # One retry: re-compose with concrete revision hints from the critic
            try:
                composed2 = compose.compose_page(
                    lead,
                    research_brief | {'_critic_hints': verdict.get('revision_hints', {}),
                                       '_critic_weaknesses': verdict.get('weaknesses', [])},
                    tracker, model=compose_model, client=client, full_catalog=full_catalog,
                    voice_card=voice_card,
                )
                if composed2 and not composed2.get('_parse_error'):
                    composed = composed2
                    rerender = assemble.assemble(lead, composed, research_brief)
                    revised_verdict = critic.critique(
                        composed, rerender['fingerprint_inputs'], research_brief,
                        neighbor_fps, tracker, client=client,
                    )
                    log.append({
                        'ts': _now_iso(), 'stage': 'critic-revise',
                        'verdict': revised_verdict['verdict'],
                        'quality_score': revised_verdict.get('quality_score'),
                        'similarity': revised_verdict.get('similarity_to_neighbors'),
                    })
                    if revised_verdict['verdict'] == 'revise':
                        # Still flagged after retry — ship anyway, mark degraded
                        sim = revised_verdict.get('similarity_to_neighbors', 0)
                        if sim >= critic.SIMILARITY_CEILING:
                            agent_status = 'degraded_similar'
                            warnings.append(f"still too similar after revise (sim={sim})")
                        else:
                            agent_status = 'degraded_low_quality'
                            warnings.append(f"quality_score={revised_verdict.get('quality_score')} after revise — shipping anyway")
            except cost.BudgetExceeded:
                warnings.append('budget exceeded during revise — keeping first composition')
                if agent_status == 'agent_built':
                    agent_status = 'degraded_budget'

    # 4b. Photo color-grading — pull every real photo through the palette
    # tint pipeline before final render. Always-publish: failed gradings
    # fall back to the original URL transparently.
    slug = lead.get('slug') or f'lead-{lead_id}'
    try:
        from . import photo_grade
        # Pull palette dict via the catalog using the composed_page's palette
        # name (after fallback resolution). Falls back to industry default.
        from . import catalog as _cat
        _full = full_catalog or _cat.load_all()
        palette_name = composed.get('palette') if composed else None
        if not palette_name or palette_name not in _full['available']['palettes']:
            palette_name = assemble.DEFAULT_PALETTE_BY_INDUSTRY.get(
                pl.industry_for(lead.get('category')), 'clean-trade-blue')
        palette_dict = _full['palettes'].get(palette_name) or {}
        # Source URLs: prefer composed.images, else raw_outscraper photos
        composed_images = (composed or {}).get('images') or {}
        photo_urls = []
        if composed_images.get('hero'):
            photo_urls.append(composed_images['hero'])
        if composed_images.get('gallery'):
            photo_urls.extend([
                p.get('url') if isinstance(p, dict) else p
                for p in (composed_images['gallery'] or [])
            ])
        # Dedup preserving order
        seen = set(); photo_urls_dedup = []
        for u in photo_urls:
            if u and u.startswith(('http://', 'https://')) and 'lh3.googleusercontent.com' in u and u not in seen:
                seen.add(u)
                photo_urls_dedup.append(u)
        if photo_urls_dedup:
            grade_results = photo_grade.grade_all_for_lead(
                photo_urls_dedup, palette_dict, slug, repo_path,
            )
            # Build url→graded_url map for successful gradings
            url_to_graded = {
                r['original_url']: r['graded_url']
                for r in grade_results
                if r.get('ok') and r.get('graded_url')
            }
            # Rewrite composed.images to point at graded URLs
            if url_to_graded and composed and isinstance(composed.get('images'), dict):
                if composed['images'].get('hero') in url_to_graded:
                    composed['images']['hero'] = url_to_graded[composed['images']['hero']]
                new_gallery = []
                for p in composed['images'].get('gallery') or []:
                    if isinstance(p, dict) and p.get('url') in url_to_graded:
                        p = dict(p)
                        p['url'] = url_to_graded[p['url']]
                    elif isinstance(p, str) and p in url_to_graded:
                        p = url_to_graded[p]
                    new_gallery.append(p)
                composed['images']['gallery'] = new_gallery
            n_ok = sum(1 for r in grade_results if r.get('ok'))
            n_cached = sum(1 for r in grade_results if r.get('from_cache'))
            log.append({'ts': _now_iso(), 'stage': 'photo-grade',
                        'urls_in': len(photo_urls_dedup),
                        'graded_ok': n_ok, 'cached': n_cached,
                        'palette': palette_name})
        else:
            log.append({'ts': _now_iso(), 'stage': 'photo-grade', 'urls_in': 0})
    except Exception as e:
        warnings.append(f'photo grading crashed (using original urls): {e!r}')

    # 5. Final render — assembler never fails
    final = assemble.assemble(lead, composed, research_brief)
    html = final['html']
    if final['warnings']:
        warnings.extend(final['warnings'])
        # If the legacy fallback was used, mark degraded_render
        if any('shell render failed' in w for w in final['warnings']):
            agent_status = 'degraded_render'

    # 6. Persist to demos repo
    # slug was assigned in 4b (photo grading); reuse here
    try:
        deploy.write_demo(repo_path, slug, html)
        html_path = str(Path(repo_path) / slug / 'index.html')
    except Exception as e:
        warnings.append(f'write_demo failed: {e!r}')
        html_path = ''

    # 6b. Awwwards-vs-template classifier — runs after publish, never gates.
    # Captures a tier verdict in the agent_log for visibility. Tyler can use
    # this to decide whether to actually send the demo to the prospect, or
    # whether to re-roll. Non-blocking by design — quality signal, not
    # publish gate (yet).
    try:
        from . import awwwards
        if tracker.per_lead_spent_cents < tracker.per_lead_cap_cents:
            verdict = awwwards.classify(composed, final['fingerprint_inputs'], html, tracker, client=client)
            log.append({
                'ts': _now_iso(), 'stage': 'awwwards',
                'tier': verdict.get('tier'),
                'score': verdict.get('score'),
                'one_liner': verdict.get('one_line_verdict'),
                'must_fixes': verdict.get('must_fixes', [])[:3],
                'cost_cents': verdict.get('cost_cents', 0),
            })
            if verdict.get('errors'):
                warnings.extend([f'awwwards: {e}' for e in verdict['errors']])
        else:
            log.append({'ts': _now_iso(), 'stage': 'awwwards', 'tier': 'skipped',
                        'reason': 'per-lead cost cap reached before classifier'})
    except Exception as e:
        warnings.append(f'awwwards classifier crashed: {e!r}')

    # 7. Save DB row updates
    fp = assemble.fingerprint(final['fingerprint_inputs'])
    cost_cents = int(tracker.per_lead_spent_cents)
    # Stash voice_card alongside research_brief so it persists without a
    # schema migration. Removed from research_brief if it accidentally got
    # injected upstream — only this orchestrator owns the field.
    enriched_brief = dict(research_brief or {})
    enriched_brief['_voice_card'] = voice_card
    with db.conn() as c:
        db.update_lead(
            c, lead_id,
            research_brief=json.dumps(enriched_brief)[:200_000],
            composed_page=json.dumps(composed)[:200_000],
            fingerprint=json.dumps(final['fingerprint_inputs']),
            agent_cost_cents=cost_cents,
            agent_status=agent_status,
            agent_log=json.dumps(log)[:50_000],
            demo_html=html,
        )

    return {
        'lead_id': lead_id,
        'slug': slug,
        'agent_status': agent_status,
        'html_path': html_path,
        'cost_usd': round(tracker.per_lead_spent_cents / 100, 4),
        'cost_summary': tracker.summary(),
        'fingerprint': fp,
        'fingerprint_inputs': final['fingerprint_inputs'],
        'effective_choices': final['effective_choices'],
        'warnings': warnings,
    }


def _publish_phase1_fallback(lead_id: int, repo_path: str, reason: str) -> dict:
    """Used when we can't even start the agent (e.g. no API key). Renders with
    the Phase 1 generator and publishes — never leaves a lead unpublished."""
    with db.conn() as c:
        row = db.get_lead(c, lead_id)
    if not row:
        return {'lead_id': lead_id, 'agent_status': 'error_no_lead'}
    lead = dict(row)
    from .. import generate as legacy
    try:
        html = legacy.render_demo(lead, json.loads(lead.get('research_payload') or '{}'))
    except Exception as e:
        # Last-resort minimal HTML
        html = assemble._minimal_html(lead)
    slug = lead.get('slug') or f'lead-{lead_id}'
    try:
        deploy.write_demo(repo_path, slug, html)
    except Exception:
        pass
    with db.conn() as c:
        db.update_lead(c, lead_id,
                       agent_status='degraded_compose_failed',
                       agent_log=json.dumps([{'reason': reason}]),
                       demo_html=html)
    return {
        'lead_id': lead_id, 'slug': slug,
        'agent_status': 'degraded_compose_failed',
        'cost_usd': 0.0,
        'warnings': [reason],
    }


def build_all(
    limit: int = 50,
    per_lead_cap_cents: int = 15,
    daily_cap_cents: int = 1000,
    repo_path: Optional[str] = None,
    push: bool = True,
    where_filter: str = "research_status='done' AND slug IS NOT NULL AND slug != ''",
) -> dict:
    """Build agent-composed demos for up to `limit` leads. Pushes once at end.

    `where_filter` is appended to the SELECT — defaults to processing leads
    that have completed Phase 1 research. Override for replays.
    """
    repo_path = repo_path or os.environ.get('DEMOS_REPO_LOCAL', './demos_repo')
    full_catalog = catalog.load_all()
    try:
        client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    except KeyError:
        client = None

    with db.conn() as c:
        rows = c.execute(
            f"""SELECT id FROM leads WHERE {where_filter}
                ORDER BY COALESCE(rating, 0) DESC, COALESCE(reviews, 0) DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()

    results = []
    slugs = []
    daily_budget_hit = False
    for r in rows:
        out = build_for_lead(
            r['id'],
            per_lead_cap_cents=per_lead_cap_cents,
            daily_cap_cents=daily_cap_cents,
            repo_path=repo_path,
            client=client,
            full_catalog=full_catalog,
        )
        results.append(out)
        if out.get('slug'):
            slugs.append(out['slug'])
        # Check daily budget — bail out if hit, but everything processed so far IS published
        # (daily tracker persists, so next call would BudgetExceeded immediately)
        tracker_summary = out.get('cost_summary') or {}
        daily_spent = tracker_summary.get('daily_spent_cents', 0)
        if daily_spent >= daily_cap_cents:
            daily_budget_hit = True
            break

    pushed = False
    if push and slugs:
        try:
            pushed = deploy.git_commit_and_push(
                repo_path,
                f'agent: build {len(slugs)} demos {datetime.date.today().isoformat()}',
                slugs,
            )
        except Exception as e:
            pushed = False
            results.append({'_push_error': str(e)})

    status_counts: dict[str, int] = {}
    for r in results:
        s = r.get('agent_status', 'unknown')
        status_counts[s] = status_counts.get(s, 0) + 1

    total_cost = sum(r.get('cost_usd', 0) for r in results)
    return {
        'leads_processed': len(results),
        'pushed': pushed,
        'total_cost_usd': round(total_cost, 4),
        'daily_budget_hit': daily_budget_hit,
        'status_counts': status_counts,
        'results': results,
    }
