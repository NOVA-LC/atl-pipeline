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
import os, json, datetime, subprocess, click, requests
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

from . import (db, ingest as _ingest, dedup, verify, research, generate,
               deploy, email as _email, blog, enrich, email_verify, scraper, warmup, inbox,
               sms as _sms, vm as _vm, call_sheet, dashboard)


def _url_is_live(url, timeout=8, max_attempts=5, wait_seconds=20):
    """HEAD the URL with retry-and-wait. Vercel deploys often take 30-90s after
    trigger to actually serve, so a single check returns false-positive 404.

    Returns True if any attempt 2xx/3xx within max_attempts * wait_seconds.
    """
    if not url:
        return False
    import time
    for attempt in range(max_attempts):
        try:
            r = requests.head(url, allow_redirects=True, timeout=timeout)
            if 200 <= r.status_code < 400:
                return True
        except Exception:
            try:
                r = requests.get(url, timeout=timeout, stream=True)
                r.close()
                if 200 <= r.status_code < 400:
                    return True
            except Exception:
                pass
        if attempt < max_attempts - 1:
            time.sleep(wait_seconds)
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
@click.option('--queries', default=8, help='Number of queries to rotate through today')
@click.option('--per-query', default=20, help='Results per query (8x20=160 raw → ~50-70 valid after dedup+verify)')
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

    # 5. (SKIPPED in phone-outreach mode) — used to enrich emails. Kept here as
    #    a noop comment so future readers see the intentional skip. Phones cover
    #    100% of leads from Outscraper; emails covered <15%, so we pivoted.

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

    # 9a. CALL SHEET (moved BEFORE SMS so Tyler gets it even if SMS stage stalls)
    try:
        # Look back 7 days so the sheet always has SOMETHING (today's batch + recent)
        with db.conn() as c:
            todays = c.execute("""SELECT * FROM leads
                                  WHERE updated_at > datetime('now', '-7 days')
                                    AND vercel_url IS NOT NULL
                                    AND phone IS NOT NULL AND phone != ''
                                  ORDER BY rating DESC, reviews DESC
                                  LIMIT 100""").fetchall()
        if todays:
            md = call_sheet.render_call_sheet([dict(r) for r in todays])
            # Always write to volume so Tyler can pull from /data even if email fails
            try:
                sheet_path = Path(os.environ.get('PIPELINE_DB_PATH', 'atl_pipeline.db')).parent / f'call_sheet_{datetime.date.today().isoformat()}.md'
                sheet_path.parent.mkdir(parents=True, exist_ok=True)
                sheet_path.write_text(md)
                click.echo(f'  📁 call sheet written to {sheet_path}')
            except Exception as e:
                click.echo(f'  ! file write failed (non-fatal): {e}')
            # Always print full content to stdout so it's in Railway logs
            click.echo('=== CALL SHEET START ===')
            click.echo(md)
            click.echo('=== CALL SHEET END ===')
            # Try email send (may fail with bad Resend key — non-fatal)
            if not dry_run:
                try:
                    cs_status, cs_resp = call_sheet.email_call_sheet(md, env, subject_suffix=f'{len(todays)} leads')
                    click.echo(f"  📋 call sheet emailed (status={cs_status}, {len(todays)} leads)")
                    if cs_status != 200:
                        click.echo(f'    ! Resend response: {cs_resp}')
                except Exception as e:
                    click.echo(f'  ! email send failed (non-fatal — content above): {e}')

        # 9b. DASHBOARD — interactive HTML version with one-click call/text
        try:
            base = (env.get('DEMOS_BASE_URL') or 'atlanta-demos.vercel.app').rstrip('/')
            base = base.replace('https://', '').replace('http://', '')
            html = dashboard.render_dashboard(
                [dict(r) for r in todays],
                base_url=f'https://{base}',
            )
            # Write to umbrella demos repo so Vercel serves it
            dash_dir = Path(repo_path) / 'dashboard'
            dash_dir.mkdir(parents=True, exist_ok=True)
            (dash_dir / 'index.html').write_text(html, encoding='utf-8')
            # Option B: also emit pure JSON so the dialer doesn't have to scrape HTML.
            # This is the lossless feed — full raw_outscraper subset per lead.
            try:
                leads_json_payload = [
                    {
                        **{k: dict(r).get(k) for k in (
                            'id', 'business_name', 'phone', 'rating', 'reviews',
                            'category', 'city', 'state', 'vercel_url',
                            'google_maps_url', 'email',
                        )},
                        'research_payload': json.loads(dict(r).get('research_payload') or '{}') if dict(r).get('research_payload') else {},
                        'gbp': json.loads(dict(r).get('raw_outscraper') or '{}') if dict(r).get('raw_outscraper') else {},
                    }
                    for r in todays
                ]
                (dash_dir / 'leads.json').write_text(
                    json.dumps({
                        'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
                        'count': len(leads_json_payload),
                        'leads': leads_json_payload,
                    }, indent=2, default=str),
                    encoding='utf-8',
                )
            except Exception as e:
                click.echo(f'  ! leads.json emit failed (non-fatal): {e}')
            # Commit + push as its own commit so it's clear in git history
            try:
                subprocess.check_call(['git', '-C', repo_path, 'add', 'dashboard/index.html', 'dashboard/leads.json'])
                # noop guard
                r = subprocess.run(['git', '-C', repo_path, 'diff', '--cached', '--quiet'])
                if r.returncode != 0:
                    subprocess.check_call(['git', '-C', repo_path, 'commit', '-m',
                                           f'dashboard: refresh {datetime.date.today().isoformat()} ({len(todays)} leads)'])
                    subprocess.check_call(['git', '-C', repo_path, 'push', 'origin', 'main'])
                    click.echo(f'  🎛  dashboard pushed: https://{base}/dashboard/')
                else:
                    click.echo(f'  🎛  dashboard unchanged')
            except Exception as e:
                click.echo(f'  ! dashboard push failed (non-fatal): {e}')

            # Email a tiny notification with just the dashboard link (alongside the call sheet email)
            if not dry_run and env.get('RESEND_API_KEY') and env.get('RESEND_FROM_EMAIL'):
                try:
                    import requests as _rq
                    dash_url = f'https://{base}/dashboard/'
                    to_email = env.get('RESEND_FROM_EMAIL')
                    subject = f'🎛  Dashboard — {datetime.date.today().isoformat()} · {len(todays)} leads'
                    big_btn = (
                        f'<a href="{dash_url}" style="display:inline-block;background:#3a82f7;'
                        'color:#fff;padding:14px 28px;border-radius:10px;font-weight:600;'
                        f'text-decoration:none;font-family:-apple-system,sans-serif;font-size:16px">'
                        f'Open dashboard ({len(todays)} leads) →</a>'
                    )
                    body_html = (
                        '<div style="font-family:-apple-system,sans-serif;line-height:1.5;'
                        'padding:20px;max-width:520px">'
                        f'<h2 style="margin:0 0 6px">📞 Today\'s leads are ready</h2>'
                        f'<p style="color:#555;margin:0 0 18px">'
                        f'Live demos, one-click call/text. Tracks who you\'ve dialed via localStorage.</p>'
                        f'{big_btn}'
                        f'<p style="color:#888;font-size:13px;margin-top:24px">'
                        f'Direct link: <a href="{dash_url}">{dash_url}</a></p>'
                        '</div>'
                    )
                    payload = {
                        'from': f'Nova Pipeline <{to_email}>',
                        'to': [to_email],
                        'subject': subject,
                        'text': f'Dashboard ready: {dash_url}',
                        'html': body_html,
                    }
                    rr = _rq.post('https://api.resend.com/emails',
                                  headers={'Authorization': f'Bearer {env["RESEND_API_KEY"]}',
                                           'Content-Type': 'application/json'},
                                  json=payload, timeout=15)
                    click.echo(f'  📬 dashboard link emailed (status={rr.status_code})')
                except Exception as e:
                    click.echo(f'  ! dashboard email failed (non-fatal): {e}')
        except Exception as e:
            click.echo(f'  ! dashboard generation failed (non-fatal): {e}')
    except Exception as e:
        click.echo(f'  ! call-sheet failed: {e}')

    # 9. SMS Day-1 — skip entirely if Twilio not fully configured
    if not (env.get('TWILIO_ACCOUNT_SID') and env.get('TWILIO_AUTH_TOKEN') and env.get('TWILIO_FROM_NUMBER')):
        click.echo('SMS Day-1 SKIPPED: Twilio not fully configured (need ACCOUNT_SID + AUTH_TOKEN + FROM_NUMBER).')
        click.echo('Done.')
        return
    click.echo(f'Sending Day-1 SMS (cap: {cap}){"  [DRY-RUN]" if dry_run else ""}...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'sms1')[:cap * 2]
    sent = 0
    skipped_url = skipped_quality = 0
    for lead_row in pending:
        if sent >= cap:
            break
        lead = dict(lead_row)
        r = json.loads(lead.get('research_payload') or '{}')

        # GATE 1: research must have produced something personal (so the SMS isn't generic)
        if not _research_is_personalized(r):
            skipped_quality += 1
            with db.conn() as c:
                db.update_lead(c, lead['id'], notes=f"skipped: research too generic")
            continue

        # GATE 2: demo URL must actually load (Vercel deploy may still be in flight)
        if not _url_is_live(lead.get('vercel_url')):
            skipped_url += 1
            with db.conn() as c:
                db.update_lead(c, lead['id'], notes=f"skipped: vercel_url not live")
            click.echo(f"  ✗ skip (demo 404): {lead['business_name']} → {lead.get('vercel_url')}")
            continue

        try:
            if dry_run:
                msg = _sms.write_sms(lead, lead['vercel_url'], r,
                                     model=env.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'))
                with open(_dryrun_log_path(), 'a') as f:
                    f.write(f"\n{'='*70}\nTO: {lead['phone']}  ({lead['business_name']})\nDEMO: {lead['vercel_url']}\n\n{msg['body']}\n")
                sent += 1
                click.echo(f"  📝 dry-run logged: {lead['phone']}")
            else:
                result = _sms.send_day1_sms(lead, lead['vercel_url'], r, env)
                if result and result.get('sid'):
                    with db.conn() as c:
                        db.update_lead(c, lead['id'],
                            sms1_sent_at=datetime.datetime.utcnow().isoformat(),
                            sms1_sid=result['sid'],
                            sms1_body=result.get('body'))
                    sent += 1
                    click.echo(f"  ✉ {lead['phone']}  ({lead['business_name']})")
                elif result:
                    click.echo(f"  ! SMS failed for {lead.get('phone')}: status={result.get('status')} resp={result.get('response')}")
        except Exception as e:
            click.echo(f"  ! send failed for {lead.get('phone')}: {e}")
    click.echo(f'  sent {sent}/{cap} Day-1 SMS (skipped {skipped_quality} thin research, {skipped_url} dead demo URL)')

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
def _alert_tyler(stage, exc):
    """Send a Resend email to Tyler when the cron crashes mid-pipeline.

    Falls through silently if Resend is the thing that's broken — last thing we
    want is an alerting loop. Best-effort only.
    """
    import traceback
    api_key = os.environ.get('RESEND_API_KEY')
    to_email = os.environ.get('RESEND_FROM_EMAIL')
    if not (api_key and to_email):
        return
    tb = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-3000:]
    body = (f"Daily cron crashed at stage: {stage}\n"
            f"Time: {datetime.datetime.utcnow().isoformat()} UTC\n"
            f"Exception: {type(exc).__name__}: {exc}\n\n"
            f"Last 3KB of traceback:\n{tb}\n")
    try:
        requests.post('https://api.resend.com/emails',
                      headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                      json={'from': f'atl-pipeline alerts <{to_email}>',
                            'to': [to_email],
                            'subject': f'[atl-pipeline] CRASH at stage: {stage}',
                            'text': body},
                      timeout=10)
    except Exception:
        pass


@cli.command()
@click.option('--dry-run', is_flag=True, help='Write emails to dryrun.log instead of sending')
@click.pass_context
def daily(ctx, dry_run):
    """Scrape fresh leads, mark replied threads, run the full pipeline.

    Wraps each stage in error-handling so one stage's failure doesn't kill the
    rest. On any unhandled exception, emails Tyler with the traceback.
    """
    stage = 'init'
    try:
        # 0. Inbox + Resend scan: mark unsubscribes / replies before anything else.
        stage = 'check-replies'
        try:
            ctx.invoke(check_replies)
        except Exception as e:
            click.echo(f'  ! check-replies failed (non-fatal): {e}')

        # 1. Scrape new leads
        stage = 'scrape'
        ctx.invoke(scrape)

        # 2. Run the full pipeline on today's xlsx
        stage = 'run'
        today = datetime.date.today().isoformat()
        xlsx = Path('./scrapes') / f'outscraper-{today}.xlsx'
        if not xlsx.exists():
            click.echo(f'  ! no xlsx at {xlsx} — scrape may have produced no results')
            return
        ctx.invoke(run, xlsx=str(xlsx), dry_run=dry_run)

        # 3. Send Day-3 / Day-7 follow-ups
        stage = 'send-followups'
        if not dry_run:
            ctx.invoke(send_followups)
    except Exception as e:
        click.echo(f'  ✗ CRASH at stage={stage}: {e}')
        _alert_tyler(stage, e)
        raise

# ---------------------------------------------------------------------------
# send-followups — Day-3 ringless voicemail to non-responders
# ---------------------------------------------------------------------------
@cli.command()
@click.option('--limit', default=50)
def send_followups(limit):
    """Day-3 ringless voicemail drop via Slybroadcast for any prospect who got
    SMS Day-1 but hasn't replied. Old email-followup logic kept below as a
    fallback if anyone's still in the email pipeline (won't fire normally)."""
    env = dict(os.environ)

    # ---- Day-3 ringless voicemail drop ----
    with db.conn() as c:
        vm_pending = db.leads_pending(c, 'vm1')[:limit]
    if vm_pending:
        click.echo(f'Day-3 voicemail drop: {len(vm_pending)} non-responders...')
        phones = [r['phone'] for r in vm_pending if r['phone']]
        result = _vm.send_voicemail_drop(phones, env)
        if result is None:
            click.echo('  ! Slybroadcast not configured (set SLYBROADCAST_USER/PASS/AUDIO_URL/CALLER_ID env)')
        else:
            session = result.get('session_id')
            click.echo(f"  ✓ dropped to {len(phones)} numbers · status={result['status']} session={session}")
            with db.conn() as c:
                for r in vm_pending:
                    db.update_lead(c, r['id'],
                        vm1_sent_at=datetime.datetime.utcnow().isoformat(),
                        vm1_id=session)
    else:
        click.echo('Day-3 voicemail drop: 0 pending')

    # ---- Legacy email follow-ups (only fires for any leads still on email-mode) ----
    with db.conn() as c:
        d3 = db.leads_pending(c, 'email2')[:limit]
        d7 = db.leads_pending(c, 'email3')[:limit]
    if d3 or d7:
        click.echo(f'(legacy) Day-3 email: {len(d3)}, Day-7 email: {len(d7)}')
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
                    lead['email'], msg['subject'], msg['body'], reply_to=env.get('RESEND_REPLY_TO'),
                    lead_id=lead['id'],
                )
                if status == 200 and resp.get('id'):
                    with db.conn() as c:
                        db.update_lead(c, lead['id'], **{field_at: datetime.datetime.utcnow().isoformat(), field_id: resp['id']})
                    click.echo(f"  ✉ {lead['email']} {field_at}")
            except Exception as e:
                click.echo(f"  ! followup failed: {e}")

@cli.command('mark-unsubscribed')
@click.argument('email')
def mark_unsubscribed(email):
    """Permanently mark a lead as do-not-contact. Use when someone replies STOP
    or hits the one-click unsubscribe link. Required for CAN-SPAM compliance —
    the law requires honoring opt-outs within 10 business days."""
    with db.conn() as c:
        rows = c.execute('SELECT id, business_name FROM leads WHERE lower(email) = lower(?)',
                         (email,)).fetchall()
        if not rows:
            click.echo(f'  ! no lead with email = {email}')
            return
        for row in rows:
            db.update_lead(c, row['id'], do_not_contact=1, replied=1,
                           notes='unsubscribed — do-not-contact')
            click.echo(f'  ✓ unsubscribed: {row["business_name"]} ({email})')


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
    """Scan two sources for engagement and suppress follow-ups:

    1. Gmail inbox (IMAP) — actual replies AND one-click unsubscribes that
       landed at tyler+unsub-{lead_id}@gonenova.com. Requires GMAIL_APP_PASSWORD.
    2. Resend events — clicks on the demo link (strongest cheap engagement signal).
    """
    # 1. Gmail inbox scan (email replies + email unsubscribes — for any leads
    #    still on the email pipeline; harmless no-op for phone-only mode)
    click.echo('Scanning Gmail inbox for replies + unsubscribes...')
    unsub, reply = inbox.scan_all()
    if unsub is None:
        click.echo('  ! GMAIL_APP_PASSWORD not set or IMAP failed — skipping inbox scan')
    else:
        click.echo(f'  ✓ inbox: {unsub} unsubscribes, {reply} replies')

    # 2. Twilio inbound SMS scan (Day-1 SMS replies + STOP opt-outs)
    click.echo('Scanning Twilio for SMS replies...')
    sms_reply, sms_optout = inbox.scan_twilio_replies()
    if sms_reply is None:
        click.echo('  ! Twilio creds missing — skipping SMS inbox scan')
    else:
        click.echo(f'  ✓ SMS: {sms_reply} replies, {sms_optout} opt-outs')

    # 2. Resend click-events (anyone who clicked the demo link is engaged)
    api_key = os.environ.get('RESEND_API_KEY')
    if not api_key:
        click.echo('  ! RESEND_API_KEY missing — skipping click check'); return

    with db.conn() as c:
        rows = c.execute("""SELECT id, business_name, email, email1_resend_id
                            FROM leads
                            WHERE email1_resend_id IS NOT NULL
                              AND replied = 0
                              AND do_not_contact = 0
                              AND email1_sent_at > datetime('now','-14 days')""").fetchall()
    if not rows:
        click.echo('  no recent sends to check for clicks'); return

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
            if data.get('last_event') in ('clicked',):
                with db.conn() as c:
                    db.update_lead(c, row['id'], replied=1, notes='auto-replied: demo-click')
                marked += 1
                click.echo(f'  ✓ engaged (clicked demo): {row["business_name"]}')
        except Exception:
            continue
    click.echo(f'  marked {marked} as engaged via demo click')

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

@cli.command('rebuild-dashboard')
@click.option('--lookback-days', default=14, help='Pull leads updated in the last N days')
@click.option('--limit', default=200)
@click.option('--push/--no-push', default=True, help='Push to demos repo (set --no-push for dry-run)')
def rebuild_dashboard(lookback_days, limit, push):
    """Regenerate dashboard/index.html + dashboard/leads.json from pipeline.db
    WITHOUT running the full daily pipeline. Use this to push fresh data to the
    dialer/build_agent when Outscraper is down or the cron has been failing."""
    env = _env()
    repo_path = _ensure_demos_repo(env)
    if not repo_path:
        click.echo('  ! demos repo not available — set DEMOS_REPO_LOCAL or run inside the cron service')
        return
    base = (env.get('DEMOS_BASE_URL') or 'atlanta-demos.vercel.app').rstrip('/').replace('https://', '').replace('http://', '')

    with db.conn() as c:
        rows = c.execute(f"""SELECT * FROM leads
                            WHERE updated_at > datetime('now', '-{int(lookback_days)} days')
                              AND phone IS NOT NULL AND phone != ''
                            ORDER BY rating DESC, reviews DESC
                            LIMIT ?""", (limit,)).fetchall()
    if not rows:
        click.echo(f'  ! no leads in last {lookback_days} days — nothing to rebuild')
        return
    click.echo(f'  → rebuilding dashboard for {len(rows)} leads')

    html = dashboard.render_dashboard([dict(r) for r in rows], base_url=f'https://{base}')
    dash_dir = Path(repo_path) / 'dashboard'
    dash_dir.mkdir(parents=True, exist_ok=True)
    (dash_dir / 'index.html').write_text(html, encoding='utf-8')
    click.echo(f'  ✓ wrote {dash_dir / "index.html"}')

    # leads.json (Option B feed)
    try:
        leads_json_payload = [
            {
                **{k: dict(r).get(k) for k in (
                    'id', 'business_name', 'phone', 'rating', 'reviews',
                    'category', 'city', 'state', 'vercel_url',
                    'google_maps_url', 'email',
                )},
                'research_payload': json.loads(dict(r).get('research_payload') or '{}') if dict(r).get('research_payload') else {},
                'gbp': json.loads(dict(r).get('raw_outscraper') or '{}') if dict(r).get('raw_outscraper') else {},
            }
            for r in rows
        ]
        (dash_dir / 'leads.json').write_text(
            json.dumps({
                'generated_at': datetime.datetime.utcnow().isoformat() + 'Z',
                'count': len(leads_json_payload),
                'leads': leads_json_payload,
            }, indent=2, default=str),
            encoding='utf-8',
        )
        click.echo(f'  ✓ wrote {dash_dir / "leads.json"} ({len(leads_json_payload)} leads)')
    except Exception as e:
        click.echo(f'  ! leads.json failed: {e}')
        return

    if not push:
        click.echo(f'  (skipping push — dry run)')
        return
    try:
        subprocess.check_call(['git', '-C', repo_path, 'add', 'dashboard/index.html', 'dashboard/leads.json'])
        r = subprocess.run(['git', '-C', repo_path, 'diff', '--cached', '--quiet'])
        if r.returncode != 0:
            subprocess.check_call(['git', '-C', repo_path, 'commit', '-m',
                                   f'dashboard: rebuild from pipeline.db ({len(rows)} leads)'])
            subprocess.check_call(['git', '-C', repo_path, 'push', 'origin', 'main'])
            click.echo(f'  🎛  pushed: https://{base}/dashboard/  and  https://{base}/dashboard/leads.json')
        else:
            click.echo(f'  🎛  dashboard unchanged')
    except Exception as e:
        click.echo(f'  ! dashboard push failed: {e}')


def _ensure_demos_repo(env):
    """Clone the demos repo to DEMOS_REPO_LOCAL if needed; return its path or None."""
    repo_local = env.get('DEMOS_REPO_LOCAL') or env.get('DEMOS_REPO_LOCAL_PATH') or '/data/demos_repo'
    owner = env.get('GITHUB_OWNER') or 'NOVA-LC'
    name = env.get('DEMOS_REPO_NAME') or 'atlanta-website-demos'
    token = env.get('GITHUB_TOKEN')
    if not Path(repo_local).exists() and token:
        try:
            url = f'https://{token}@github.com/{owner}/{name}.git'
            Path(repo_local).parent.mkdir(parents=True, exist_ok=True)
            subprocess.check_call(['git', 'clone', '--depth', '1', url, repo_local])
        except Exception as e:
            click.echo(f'  ! clone failed: {e}')
            return None
    return repo_local if Path(repo_local).exists() else None


def _env():
    return {k: os.environ.get(k) for k in os.environ}


if __name__ == '__main__':
    cli()
