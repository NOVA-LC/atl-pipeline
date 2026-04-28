# atl-pipeline

Daily 50-website outreach engine for **gonenova.com**. Takes an Outscraper export, runs deep research + custom demo generation, deploys 50 unique demos to Vercel, sends Resend cold emails with each prospect's personalized URL, and publishes a same-day design essay to gonenova.com.

## Daily flow

```
Outscraper export (xlsx)
    ↓ verify.py    — confirm prospect actually has no website (parallel agents)
    ↓ research.py  — owner name, LinkedIn, Facebook, brand colors, truck colors, BBB,
                    real reviews, social posts, neighborhood reputation
    ↓ generate.py  — render personalized HTML from research → 50 subfolders
    ↓ deploy.py    — git push + Vercel API: create 50 projects, trigger deploys
    ↓ email.py     — Resend: send Day-1 + schedule Day-3 + Day-7 follow-ups
    ↓ blog.py      — pick the most interesting demo, generate design essay,
                    commit to gonenova repo (auto-publishes via Lovable)
```

## Setup

```bash
cp .env.example .env
# Fill in:
#   GITHUB_TOKEN             — fine-grained PAT, contents:read+write on demos repo
#   VERCEL_TOKEN             — vercel.com/account/tokens
#   RESEND_API_KEY           — resend.com/api-keys
#   RESEND_FROM_EMAIL        — must match verified domain in Resend
#   GONENOVA_REPO_TOKEN      — same GitHub PAT, contents:write on gonenova
#   ANTHROPIC_API_KEY        — for the research + email-writing model
#   DEMOS_REPO_PATH          — local path to the demos repo (we'll git-push from here)
#   DEMOS_REPO_REMOTE        — e.g. https://github.com/NOVA-LC/atlanta-website-demos.git

pip install -r requirements.txt

# Daily run
python -m atl_pipeline.cli run --xlsx path/to/outscraper.xlsx --batch-size 50
```

## Architecture

- **Orchestrator** (`cli.py`): one command runs the whole daily flow with checkpoints; resumable.
- **Per-stage modules**: each stage reads from + writes to a SQLite DB so partial runs resume cleanly.
- **Parallelism**: verify + research stages fan out 5-10 parallel HTTP/agent calls.
- **Idempotency**: re-running with the same xlsx skips already-deployed leads.

## What's NOT in the box (intentional)

- Lead scraping itself — Outscraper is your scraper. Drop the xlsx in.
- Inbox monitoring (replies/clicks) — that lives in `yc-campaign-dashboard`-style tracker; the email-send side just hands off to Resend's analytics.
- CRM sync — out of scope for v1.

## Files

- `atl_pipeline/cli.py` — entrypoint
- `atl_pipeline/db.py` — SQLite schema + helpers
- `atl_pipeline/verify.py` — website-existence verification
- `atl_pipeline/research.py` — deep prospect research (Claude + web)
- `atl_pipeline/generate.py` — demo HTML rendering
- `atl_pipeline/deploy.py` — git + Vercel API
- `atl_pipeline/email.py` — Resend send + sequencing
- `atl_pipeline/blog.py` — gonenova design-essay drop
- `atl_pipeline/templates/` — Jinja2 demo templates per industry
- `atl_pipeline/photo_library.py` — verified-loading Unsplash IDs by industry
