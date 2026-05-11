#!/usr/bin/env bash
# Railway cron entrypoint.
#
# Each invocation:
#   1. Ensures the demos repo is cloned (or up-to-date) on the persistent volume
#   2. Configures git identity for the commit-and-push step
#   3. Runs the daily pipeline (scrape → verify → research → generate → deploy → email → blog)
#
# Logs print to stdout; Railway captures them in the service logs.

set -euo pipefail

DEMOS_REPO_LOCAL="${DEMOS_REPO_LOCAL:-./demos_repo}"
GITHUB_OWNER="${GITHUB_OWNER:-NOVA-LC}"
DEMOS_REPO_NAME="${DEMOS_REPO_NAME:-atlanta-website-demos}"

echo ">> [entrypoint] starting daily run at $(date -u)"
echo ">> [entrypoint] demos repo target: $DEMOS_REPO_LOCAL"

# Ensure parent dir exists (volume mount may need it)
mkdir -p "$(dirname "$DEMOS_REPO_LOCAL")"

# One-time cleanup: pre-fix vercel_urls in DB all 404 (alias never assigned).
# Clear them so the deploy stage re-runs against the fixed code path. Marker file
# on the persistent volume gates this to fire exactly once. Python one-liner so
# we don't depend on the sqlite3 CLI being in the Nixpacks image.
DB_PATH="${PIPELINE_DB_PATH:-/data/pipeline.db}"
MARKER="$(dirname "$DB_PATH")/.cleanup_stale_urls_v1.done"
if [ ! -f "$MARKER" ] && [ -f "$DB_PATH" ]; then
    echo ">> [entrypoint] one-time cleanup: clearing pre-fix vercel_urls"
    python -c "
import sqlite3, os
db = os.environ.get('PIPELINE_DB_PATH', '/data/pipeline.db')
con = sqlite3.connect(db)
before = con.execute('SELECT COUNT(*) FROM leads WHERE vercel_url IS NOT NULL').fetchone()[0]
con.execute(\"UPDATE leads SET vercel_url = NULL WHERE vercel_url IS NOT NULL AND updated_at < '2026-05-08 22:00'\")
con.commit()
after = con.execute('SELECT COUNT(*) FROM leads WHERE vercel_url IS NOT NULL').fetchone()[0]
print(f'>> [entrypoint] cleared {before - after} stale URLs ({before} -> {after})')
con.close()
" && touch "$MARKER"
fi

# One-time migration v2: rewrite all existing vercel_urls to the umbrella-project
# path scheme. After this, every demo lives at https://{DEMOS_BASE_URL}/{slug}/
# served by a single Vercel project. Eliminates the 50-project-per-repo limit.
MARKER_V2="$(dirname "$DB_PATH")/.migrate_to_umbrella_v2.done"
if [ ! -f "$MARKER_V2" ] && [ -f "$DB_PATH" ]; then
    echo ">> [entrypoint] one-time migration v2: rewriting URLs to umbrella scheme"
    python -c "
import sqlite3, os
db = os.environ.get('PIPELINE_DB_PATH', '/data/pipeline.db')
base = (os.environ.get('DEMOS_BASE_URL') or 'atlanta-demos.vercel.app').rstrip('/')
base = base.replace('https://', '').replace('http://', '')
con = sqlite3.connect(db)
rows = con.execute(\"SELECT id, slug FROM leads WHERE demo_html IS NOT NULL AND slug IS NOT NULL AND slug != ''\").fetchall()
for lead_id, slug in rows:
    url = f'https://{base}/{slug}/'
    con.execute('UPDATE leads SET vercel_url = ? WHERE id = ?', (url, lead_id))
con.commit()
print(f'>> [entrypoint] rewrote {len(rows)} vercel_urls to {base}/{{slug}}/')
con.close()
" && touch "$MARKER_V2"
fi

# Clone or pull the demos repo
if [ ! -d "$DEMOS_REPO_LOCAL/.git" ]; then
    echo ">> [entrypoint] cloning demos repo (first run)"
    git clone "https://${GITHUB_TOKEN}@github.com/${GITHUB_OWNER}/${DEMOS_REPO_NAME}.git" "$DEMOS_REPO_LOCAL"
else
    echo ">> [entrypoint] pulling latest demos repo"
    git -C "$DEMOS_REPO_LOCAL" remote set-url origin "https://${GITHUB_TOKEN}@github.com/${GITHUB_OWNER}/${DEMOS_REPO_NAME}.git"
    git -C "$DEMOS_REPO_LOCAL" pull --rebase --autostash
fi

# Configure git identity for the commit step
git config --global user.email "${RESEND_FROM_EMAIL:-tyler@gonenova.com}"
git config --global user.name "${RESEND_FROM_NAME:-Tyler · Nova}"
git config --global init.defaultBranch main

echo ">> [entrypoint] running daily pipeline"
exec python -m atl_pipeline.cli daily
