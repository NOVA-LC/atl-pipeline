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
