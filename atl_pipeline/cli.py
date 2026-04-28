"""Daily orchestrator. One command runs the whole pipeline.

Usage:
  python -m atl_pipeline.cli run --xlsx outscraper.xlsx --batch-size 50
  python -m atl_pipeline.cli verify          # only run verify stage on pending leads
  python -m atl_pipeline.cli research        # only run research
  python -m atl_pipeline.cli generate-deploy # generate + deploy demos
  python -m atl_pipeline.cli send-day1       # send Day-1 cold emails
  python -m atl_pipeline.cli send-followups  # Day-3 + Day-7 sequencing
  python -m atl_pipeline.cli blog --lead-id N # post one blog essay
"""
import os, json, click
from dotenv import load_dotenv
load_dotenv()

from . import db, ingest as _ingest, verify, research, generate, deploy, email as _email, blog

@click.group()
def cli(): pass

@cli.command()
@click.option('--xlsx', required=True, type=click.Path(exists=True))
@click.option('--batch-size', default=50)
@click.option('--skip-blog', is_flag=True)
def run(xlsx, batch_size, skip_blog):
    """Full daily run: ingest → verify → research → generate → deploy → email Day-1 → blog."""
    click.echo(f'Ingesting {xlsx}...')
    inserted, skipped = _ingest.ingest(xlsx)
    click.echo(f'  upserted {inserted}, skipped {skipped}')

    click.echo('Verifying website status (parallel)...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'verify')[:batch_size]
    click.echo(f'  {len(pending)} to verify')
    if pending:
        results = verify.verify_batch([dict(l) for l in pending])
        with db.conn() as c:
            for lid, r in results.items():
                db.update_lead(c, lid, verify_status=r['verdict'], verify_payload=json.dumps(r))

    click.echo('Researching (parallel, slow)...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'research')[:batch_size]
    click.echo(f'  {len(pending)} to research')
    if pending:
        results = research.research_batch([dict(l) for l in pending])
        with db.conn() as c:
            for lid, r in results.items():
                status = 'failed' if '_error' in r or '_parse_error' in r else 'done'
                db.update_lead(c, lid, research_status=status, research_payload=json.dumps(r))

    click.echo('Generating demo HTML...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'demo')
    repo_path = os.environ['DEMOS_REPO_LOCAL']
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
        deploy.git_commit_and_push(repo_path, f'demos: batch of {len(slugs)}', slugs)

    click.echo('Deploying to Vercel (creating projects + triggering deploys)...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'deploy')
    repo_full = f"{os.environ.get('GITHUB_OWNER','NOVA-LC')}/{os.environ['DEMOS_REPO_NAME']}"
    repo_id = int(os.environ['DEMOS_REPO_VERCEL_REPO_ID'])
    vc_token = os.environ['VERCEL_TOKEN']
    team_id = os.environ.get('VERCEL_TEAM_ID') or None
    for lead_row in pending:
        lead = dict(lead_row)
        out = deploy.deploy_lead(lead, lead['demo_html'], repo_path, repo_full, repo_id, vc_token, team_id)
        with db.conn() as c:
            db.update_lead(c, lead['id'], vercel_project=out['slug'], vercel_url=out['url'])
        click.echo(f"  ✓ {out['slug']} → {out['url']}")

    click.echo('Sending Day-1 emails...')
    with db.conn() as c:
        pending = db.leads_pending(c, 'email1')
    env = dict(os.environ)
    for lead_row in pending:
        lead = dict(lead_row)
        r = json.loads(lead.get('research_payload') or '{}')
        result = _email.send_day1(lead, lead['vercel_url'], r, env)
        if result and result.get('resend_id'):
            with db.conn() as c:
                db.update_lead(c, lead['id'],
                    email1_sent_at='datetime("now")',  # let SQL fill it
                    email1_resend_id=result['resend_id'])
            click.echo(f"  ✉ {lead['email']} subject={result['subject']}")

    if not skip_blog:
        click.echo('Blog drop to gonenova...')
        # Pick the most interesting deployed lead today
        with db.conn() as c:
            row = c.execute("""SELECT * FROM leads
                WHERE vercel_url IS NOT NULL
                  AND research_status='done'
                  AND id NOT IN (SELECT lead_id FROM blog_posts WHERE lead_id IS NOT NULL)
                ORDER BY rating DESC, reviews DESC LIMIT 1""").fetchone()
        if row:
            lead = dict(row)
            r = json.loads(lead.get('research_payload') or '{}')
            out = blog.post_essay(lead, r, lead['vercel_url'], env)
            click.echo(f"  ✓ blog: {out['path']} (commit={out['commit']})")
            with db.conn() as c:
                c.execute('INSERT INTO blog_posts (slug, lead_id, title, gonenova_path, published_at) VALUES (?,?,?,?,datetime("now"))',
                          (out['slug'], lead['id'], lead['business_name'], out['path']))

    click.echo('Done.')

@cli.command()
@click.option('--limit', default=50)
def status(limit):
    with db.conn() as c:
        rows = c.execute("""SELECT business_name, verify_status, research_status, vercel_url, email1_sent_at
                            FROM leads ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
    for r in rows:
        click.echo(f"  {r['business_name']:35s} verify={r['verify_status'] or '-':6s} research={r['research_status'] or '-':6s} url={r['vercel_url'] or '-':40s} d1={r['email1_sent_at'] or '-'}")

@cli.command()
@click.option('--limit', default=50)
def send_followups(limit):
    """Send Day-3 + Day-7 follow-ups. Run daily as a cron."""
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
            msg = _email.write_email(prompt_tpl, lead, lead['vercel_url'], r)
            status, resp = _email.send_via_resend(
                env['RESEND_API_KEY'], env.get('RESEND_FROM_EMAIL'), env.get('RESEND_FROM_NAME','Tyler · Nova'),
                lead['email'], msg['subject'], msg['body'], reply_to=env.get('RESEND_REPLY_TO')
            )
            if status == 200 and resp.get('id'):
                with db.conn() as c:
                    db.update_lead(c, lead['id'], **{field_at: 'datetime("now")', field_id: resp['id']})
                click.echo(f"  ✉ {lead['email']} {field_at}")

if __name__ == '__main__':
    cli()
