"""Full orchestrator smoke test — runs the entire loop on a real lead.

Gated on RUN_LIVE_ORCHESTRATOR=1 because it spends real money (~$0.10-0.50
depending on how many critic iterations fire).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT.parent / "claude code" / ".env", override=True)
except ImportError:
    pass

from build_agent import orchestrator


def main():
    if not os.environ.get("RUN_LIVE_ORCHESTRATOR"):
        print("SKIP: set RUN_LIVE_ORCHESTRATOR=1 to spend ~$0.10-0.50 on a full build")
        return

    lead = {
        "lead_id":       "orch-test-1",
        "business_name": "Peach State Plumbing",
        "city":          "Marietta",
        "state":         "GA",
        "phone":         "+17705550101",
        "existing_url":  "https://atlanta-demos.vercel.app",
    }

    def progress(event, payload):
        print(f"  [{event}] {payload}")

    print(f"Calling orchestrator.build({lead['business_name']})...")
    result = orchestrator.build(lead, progress=progress)
    print("\n── RESULT ──")
    print(json.dumps(result, indent=2, default=str))

    failures = []
    if result.get("error"):
        if result.get("error") not in ("build_unfit",):
            failures.append(f"unexpected error: {result['error']}")
    else:
        if result.get("budget_used", 0) > 7.0:
            failures.append(f"over budget: ${result['budget_used']}")
        if result.get("duration_sec", 0) > 720:
            failures.append(f"over deadline: {result['duration_sec']}s")
        if not Path(result["html_path"]).exists():
            failures.append(f"HTML not written to {result['html_path']}")

    if failures:
        print("\n✗ FAIL")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\n✓ PASS")


if __name__ == "__main__":
    main()
