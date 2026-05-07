"""Daily orchestrator. One command runs the whole pipeline.

Usage:
  python -m atl_pipeline.cli scrape                       # autonomous: hit Outscraper for fresh leads
  python -m atl_pipeline.cli run --xlsx outscraper.xlsx   # full daily run
  python -m atl_pipeline.cli daily                        # scrape + run + send (one-command cron)
  python -m atl_pipeline.cli daily --dry-run              # same, but write emails to /data/dryrun.log instead of sending
  python -m atl_pipeline.cli send-followups               # Day-3 + Day-7 sequencing
  python -m atl_pipeline.cli check-replies                # Gmail scan: mark replied=1 on engaged threads
  python -m atl_pipeline.cli mark-replied <email>         # manual: mark a lead as replied
  python -m atl_pipeline.cli status                       # see pipeline state
"""
import os, json, datetime, click, requests
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from . import (db, ingest as _ingest, dedup, verify, research, generate,
               deploy, email as _email, blog, enrich, email_verify, scraper, warmup)


def _url_is_live(url, timeout=8):
    """HEAD the URL. Returns True if 2xx or 3xx, False otherwise."""
    if not url:
        return False
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        return 200 <= r.status_code < 400
    except Exception:
        try:
            # Some hosts 405 on HEAD; fall back to small GET
            r = requests.get(url, timeout=timeout, stream=True)
            r.close()
            return 200 <= r.status_code < 400
        except Exception:
            return False


def _research_is_personalized(research):
    """Quality gate: only send if research found *something* useful.

    Returns True if at least 2 of: owner_name, wow_facts, real_reviews, vibe.
    Otherwise the email would be too generic to bother with.
    """
    if not research:
        return False
    score = 0
    if research.get('owner_name') and research['owner_name'].lower() != 'unknown':
        score += 1
    if research.get('wow_facts') and len(research['wow_facts']) >= 1:
        score += 1
    if research.get('real_reviews') and len(research['real_reviews']) >= 1:
        score += 1
    if research.get('vibe'):
        score += 1
    return score >= 2


def _dryrun_log_path():
    base = Path(os.environ.get('PIPELINE_DB_PATH', 'atl_pipeline.db')).parent
    return base / 'dryrun.log'

@click.group()
def cli(): pass

# ---------------------------------------------------------------------------
# scrape — autonomous Outscraper job
# ---------------------------------------------------------------------------
@cli.command()
@click.option('--queries', default=5, help='Number of queries to rotate through today')
@click.option('--per-query', default=25, help='Results per query')
def scrape(queries, per_query):
    api = os.environ.get('OUTSCRAPER_API_KEY')
    if not api:
        click.echo('  ! OUTSCRAPER_API_KEY missing — paste it in .env'); return
    click.echo('Submitting Outscraper job...')
    out = scraper.daily_scrape(api, n_queries=queries, limit_per_query=per_query)
    click.echo(f'  ✓ scraped → {out}')

# ---------------------------------------------------------------------------
# run — process an xlsx through the full pipeline
# ---------------------------------------------------------------------------
@cli.command()
@click.option('--xlsx', required=True, type=click.Path(exists=True))
@click.option('--max-sends', default=None, type=int, help='Override warmup cap for today')
@click.option('--skip-blog', is_flag=True)
@click.option('--dry-run', is_flag=True, help='Write emails to dryrun.log instead of sending via Resend')
def run(xlsx, max_sends, skip_blog, dry_run):
    env = dict(os.environ)
    cap = max_sends if max_sends is not None else warmup.todays_max_sends()
    click.echo(f'Pipeline day {warmup.day_of_pipeline()} · todays send cap: {cap}{" · DRY-RUN" if dry_run else ""}')

    # 1. INGEST
    click.echo(f'Ingesting {xlsx}...')
    inserted, skipped = _ingest.ingest(xlsx)
    click.echo(f'  upserted {inserted}, skipped {skipped}')

    # 2. DEDUP — within DB (across all leads ever, not just today's batch)
    with db.conn() as c:
        all_leads = [dict(r) for r in c.execute('SELECT * FROM leads WHERE verify_status IS NULL').fetchall()]
    if all_leads:
        kept, dropped = dedup.apply_dedup(all_leads)
        click.echo(f'Dedup: kept {len(kept)}, dropped {len(dropped)}')
        if dropped:
            with db.conn() as c:
                for d in dropped:
                    db.update_lead(c, d['id'], verify_status='duplicate', notes=d.get('dropped_reason'))

    # 3. VERIFY website (parallel)
    click.echo('Verifying website status...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'verify')[:max(cap*3, 50)]   # over-verify so we have headroom for filtering
    click.echo(f'  {len(pending)} to verify')
    if pending:
        results = verify.verify_batch([dict(l) for l in pending])
        with db.conn() as c:
            for lid, r in results.items():
                db.update_lead(c, lid, verify_status=r['verdict'], verify_payload=json.dumps(r))

    # 4. RESEARCH (parallel, costs money)
    click.echo('Researching...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'research')[:cap*2]
    click.echo(f'  {len(pending)} to research')
    if pending:
        results = research.research_batch([dict(l) for l in pending])
        with db.conn() as c:
            for lid, r in results.items():
                status = 'failed' if '_error' in r or '_parse_error' in r else 'done'
                db.update_lead(c, lid, research_status=status, research_payload=json.dumps(r))

    # 5. ENRICH — find owner email for leads without one
    click.echo('Enriching emails (Hunter / Snov)...')
    with db.conn() as c:
        rows = c.execute("""SELECT * FROM leads WHERE research_status='done'
                            AND (email IS NULL OR email = '')""").fetchall()
    enriched = 0
    for row in rows:
        lead = dict(row)
        r = json.loads(lead.get('research_payload') or '{}')
        out = enrich.enrich_lead(lead, r, env)
        if out.get('owner_email'):
            with db.conn() as c:
                db.update_lead(c, lead['id'], email=out['owner_email'],
                               research_payload=json.dumps({**r, **out}))
            enriched += 1
    click.echo(f'  found {enriched} new emails')

    # 6. EMAIL VERIFY — drop invalid + risky before they bounce
    click.echo('Verifying emails (MX + syntax + Reoon)...')
    with db.conn() as c:
        rows = c.execute("""SELECT id, email FROM leads
                            WHERE email IS NOT NULL AND email != ''
                            AND (verify_email_payload IS NULL)""").fetchall()
    if rows:
        try:
            email_results = email_verify.verify_batch([r['email'] for r in rows], reoon_key=env.get('REOON_API_KEY'))
            with db.conn() as c:
                for r in rows:
                    res = email_results.get(r['email'], {'verdict':'unknown'})
                    db.update_lead(c, r['id'], verify_email_payload=json.dumps(res))
                    if res['verdict'] in ('invalid', 'risky'):
                        # blank out the email so we never send to it
                        db.update_lead(c, r['id'], email='', notes=f"email-{res['verdict']}: {res.get('reason')}")
            click.echo(f'  verified {len(rows)} emails')
        except Exception as e:
            click.echo(f'  ! email-verify error: {e}')

    # 7. GENERATE demo HTML
    click.echo('Generating demo HTML...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'demo')[:cap]
    repo_path = os.environ.get('DEMOS_REPO_LOCAL', './demos_repo')
    slugs = []
    for lead_row in pending:
        lead = dict(lead_row)
        r = json.loads(lead.get('research_payload') or '{}')
        html = generate.render_demo(lead, r)
        deploy.write_demo(repo_path, lead['slug'], html)
        with db.conn() as c:
            db.update_lead(c, lead['id'], demo_html=html)
        slugs.append(lead['slug'])
    if slugs:
        click.echo(f'  committing + pushing {len(slugs)} demos...')
        deploy.git_commit_and_push(repo_path, f'demos: {datetime.date.today().isoformat()} batch of {len(slugs)}', slugs)

    # 8. DEPLOY — Vercel project per slug
    click.echo('Deploying to Vercel...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'deploy')
    repo_full = f"{os.environ.get('GITHUB_OWNER','NOVA-LC')}/{os.environ.get('DEMOS_REPO_NAME','atlanta-website-demos')}"
    repo_id = int(os.environ.get('DEMOS_REPO_VERCEL_REPO_ID', 0))
    vc_token = os.environ.get('VERCEL_TOKEN')
    team_id = os.environ.get('VERCEL_TEAM_ID') or None
    deployed = 0
    for lead_row in pending:
        lead = dict(lead_row)
        try:
            out = deploy.deploy_lead(lead, lead['demo_html'], repo_path, repo_full, repo_id, vc_token, team_id)
            with db.conn() as c:
                db.update_lead(c, lead['id'], vercel_project=out['slug'], vercel_url=out['url'])
            deployed += 1
        except Exception as e:
            click.echo(f"  ! deploy failed for {lead['slug']}: {e}")
    click.echo(f'  ✓ deployed {deployed} demos')

    # 9. EMAIL — Day-1 sends, capped by warmup, with quality gates
    click.echo(f'Sending Day-1 emails (cap: {cap}){"  [DRY-RUN]" if dry_run else ""}...')
    with db.conn() as c:
        # Pull a buffer larger than cap so quality-gates have headroom
        pending = db.leads_pending(c, 'email1')[:cap * 3]
    sent = 0
    skipped_url = skipped_quality = 0
    for lead_row in pending:
        if sent >= cap:
            break
        lead = dict(lead_row)
        r = json.loads(lead.get('research_payload') or '{}')

        # GATE 1: research must have produced something personal
        if not _research_is_personalized(r):
            skipped_quality += 1
            with db.conn() as c:
                db.update_lead(c, lead['id'], notes=f"skipped: research too generic")
            continue

        # GATE 2: demo URL must actually load
        if not _url_is_live(lead.get('vercel_url')):
            skipped_url += 1
            with db.conn() as c:
                db.update_lead(c, lead['id'], notes=f"skipped: vercel_url not live")
            click.echo(f"  ✗ skip (demo 404): {lead['business_name']} → {lead.get('vercel_url')}")
            continue

        try:
            if dry_run:
                msg = _email.write_email(_email.EMAIL_PROMPT_DAY1, lead, lead['vercel_url'], r,
                                         model=env.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'))
                with open(_dryrun_log_path(), 'a') as f:
                    f.write(f"\n{'='*70}\nTO: {lead['email']}  ({lead['business_name']})\nDEMO: {lead['vercel_url']}\nSUBJECT: {msg['subject']}\n\n{msg['body']}\n")
                sent += 1
                click.echo(f"  📝 dry-run logged: {lead['email']}")
            else:
                result = _email.send_day1(lead, lead['vercel_url'], r, env)
                if result and result.get('resend_id'):
                    with db.conn() as c:
                        db.update_lead(c, lead['id'],
                            email1_sent_at=datetime.datetime.utcnow().isoformat(),
                            email1_resend_id=result['resend_id'])
                    sent += 1
                    click.echo(f"  ✉ {lead['email']}")
        except Exception as e:
            click.echo(f"  ! send failed for {lead['email']}: {e}")
    click.echo(f'  sent {sent}/{cap} Day-1 emails (skipped {skipped_quality} for thin research, {skipped_url} for dead demo URL)')

    # 10. BLOG drop
    if not skip_blog and sent > 0:
        click.echo('Blog drop to gonenova...')
        with db.conn() as c:
            row = c.execute("""SELECT * FROM leads WHERE vercel_url IS NOT NULL AND research_status='done'
                                AND id NOT IN (SELECT lead_id FROM blog_posts WHERE lead_id IS NOT NULL)
                                ORDER BY rating DESC, reviews DESC LIMIT 1""").fetchone()
        if row:
            lead = dict(row)
            r = json.loads(lead.get('research_payload') or '{}')
            try:
                out = blog.post_essay(lead, r, lead['vercel_url'], env)
                with db.conn() as c:
                    c.execute('INSERT INTO blog_posts (slug, lead_id, title, gonenova_path, published_at) VALUES (?,?,?,?,datetime("now"))',
                              (out['slug'], lead['id'], lead['business_name'], out['path']))
                click.echo(f"  ✓ blog: {out['path']}")
            except Exception as e:
                click.echo(f"  ! blog failed: {e}")
    click.echo('Done.')

# ---------------------------------------------------------------------------
# daily — scrape + run, one command
# ---------------------------------------------------------------------------
@cli.command()
@click.option('--dry-run', is_flag=True, help='Write emails to dryrun.log instead of sending')
@click.pass_context
def daily(ctx, dry_run):
    """Scrape fresh leads, mark replied threads, run the full pipeline."""
    # 0. Mark any prospects who replied since last run, so follow-ups don't go to engaged threads.
    try:
        ctx.invoke(check_replies)
    except Exception as e:
        click.echo(f'  ! check-replies failed (non-fatal): {e}')

    # 1. Scrape new leads
    ctx.invoke(scrape)

    # 2. Run the full pipeline on today's xlsx
    today = datetime.date.today().isoformat()
    xlsx = Path('./scrapes') / f'outscraper-{today}.xlsx'
    if not xlsx.exists():
        click.echo(f'  ! no xlsx at {xlsx} — scrape may have produced no results')
        return
    ctx.invoke(run, xlsx=str(xlsx), dry_run=dry_run)

    # 3. Send Day-3 / Day-7 follow-ups (only to non-replied threads — leads_pending checks replied=0)
    if not dry_run:
        ctx.invoke(send_followups)

# ---------------------------------------------------------------------------
# send-followups — Day-3 + Day-7
# ---------------------------------------------------------------------------
@cli.command()
@click.option('--limit', default=50)
def send_followups(limit):
    env = dict(os.environ)
    with db.conn() as c:
        d3 = db.leads_pending(c, 'email2')[:limit]
        d7 = db.leads_pending(c, 'email3')[:limit]
    click.echo(f'Day-3: {len(d3)}, Day-7: {len(d7)}')
    for batch, prompt_tpl, field_at, field_id in [
        (d3, _email.EMAIL_PROMPT_DAY3, 'email2_sent_at', 'email2_resend_id'),
        (d7, _email.EMAIL_PROMPT_DAY7, 'email3_sent_at', 'email3_resend_id'),
    ]:
        for row in batch:
            lead = dict(row)
            r = json.loads(lead.get('research_payload') or '{}')
            try:
                msg = _email.write_email(prompt_tpl, lead, lead['vercel_url'], r)
                status, resp = _email.send_via_resend(
                    env['RESEND_API_KEY'], env.get('RESEND_FROM_EMAIL'), env.get('RESEND_FROM_NAME','Tyler · Nova'),
                    lead['email'], msg['subject'], msg['body'], reply_to=env.get('RESEND_REPLY_TO')
                )
                if status == 200 and resp.get('id'):
                    with db.conn() as c:
                        db.update_lead(c, lead['id'], **{field_at: datetime.datetime.utcnow().isoformat(), field_id: resp['id']})
                    click.echo(f"  ✉ {lead['email']} {field_at}")
            except Exception as e:
                click.echo(f"  ! followup failed: {e}")

@cli.command('mark-replied')
@click.argument('email')
def mark_replied(email):
    """Manually mark a lead as replied so follow-ups stop. Useful when Tyler
    sees a real reply in Gmail and wants to suppress Day-3 / Day-7."""
    with db.conn() as c:
        rows = c.execute('SELECT id, business_name FROM leads WHERE lower(email) = lower(?)',
                         (email,)).fetchall()
        if not rows:
            click.echo(f'  ! no lead with email = {email}')
            return
        for row in rows:
            db.update_lead(c, row['id'], replied=1)
            click.echo(f'  ✓ marked replied: {row["business_name"]} ({email})')

@cli.command('check-replies')
def check_replies():
    """Scan Resend for click events on sent emails — anyone who clicked the demo
    link is treated as engaged and suppressed from follow-ups. Replies via Gmail
    are detected by Tyler manually using `mark-replied`.

    Resend doesn't have a free 'received-replies' API, but it does expose a per-email
    event log. Click on the demo link is the strongest cheap proxy for interest.
    """
    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        click.echo('  ! RESEND_API_KEY missing'); return

    with db.conn() as c:
        rows = c.execute("""SELECT id, business_name, email, email1_resend_id
                            FROM leads
                            WHERE email1_resend_id IS NOT NULL
                              AND replied = 0
                              AND email1_sent_at > datetime('now','-14 days')""").fetchall()
    if not rows:
        click.echo('  no recent sends to check'); return

    click.echo(f'Checking Resend events for {len(rows)} recent sends...')
    marked = 0
    for row in rows:
        try:
            r = requests.get(f'https://api.resend.com/emails/{row["email1_resend_id"]}',
                             headers={'Authorization': f'Bearer {api_key}'},
                             timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            # Resend returns last_event in {sent,delivered,opened,clicked,bounced,complained}
            if data.get('last_event') in ('clicked',):
                with db.conn() as c:
                    db.update_lead(c, row['id'], replied=1, notes='auto-replied: demo-click')
                marked += 1
                click.echo(f'  ✓ engaged (clicked demo): {row["business_name"]}')
        except Exception:
            continue
    click.echo(f'  marked {marked} as engaged')

@cli.command()
@click.option('--limit', default=50)
def status(limit):
    """Pipeline state snapshot."""
    w = warmup.status()
    click.echo(f"Warmup: day {w['day']}, today's cap: {w['cap_today']}, tomorrow: {w['cap_tomorrow']}")
    with db.conn() as c:
        rows = c.execute("""SELECT business_name, verify_status, research_status, vercel_url, email1_sent_at
                            FROM leads ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
    for r in rows:
        click.echo(f"  {r['business_name'][:30]:30s} verify={r['verify_status'] or '-':9s} research={r['research_status'] or '-':6s} url={r['vercel_url'] or '-':40s} d1={r['email1_sent_at'] or '-'}")

if __name__ == '__main__':
    cli()
