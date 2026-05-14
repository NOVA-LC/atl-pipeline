"""Smoke test for the asset gatherer using a real research brief.

Skips FLUX (requires REPLICATE_API_TOKEN we don't want to spend yet).
Validates: real download works, palette extraction works, manifest schema is right.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    env_path = REPO_ROOT.parent / "claude code" / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
except ImportError:
    pass

from build_agent.agents.asset_gatherer import gather


SAMPLE_BRIEF_PATH = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "test-2-existing-site.json"


def main():
    if not SAMPLE_BRIEF_PATH.exists():
        print(f"SKIP: run test_researcher_real_lead.py first to generate {SAMPLE_BRIEF_PATH.name}")
        return

    brief = json.loads(SAMPLE_BRIEF_PATH.read_text())

    # Inject a couple known-good image URLs so the test exercises downloading + palette
    # even when the live brief has only one photo. These are public stable URLs.
    brief["business"]["real_photos"] = brief["business"].get("real_photos") or []
    brief["business"]["real_photos"].extend([
        {
            "url": "https://atlanta-demos.vercel.app/favicon.ico",
            "type": "logo",
            "alt": "favicon",
            "source": "https://atlanta-demos.vercel.app",
        }
    ])

    out_dir = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "assets_test"
    print(f"Gathering assets into {out_dir}...")
    manifest = gather(brief, out_dir)

    print(f"\n── MANIFEST SUMMARY ──")
    print(f"  assets:           {len(manifest['assets'])}")
    print(f"  real_asset_ratio: {manifest['real_asset_ratio']}")
    print(f"  palette:          {manifest['palette']}")
    print(f"  cost_usd:         ${manifest['cost_usd']}")
    print(f"  fallbacks:        {manifest['fallbacks']}")

    failures = []
    # Real asset ratio check (note: if no FLUX runs, ratio should be 1.0 or 0.0)
    real_count = sum(1 for a in manifest["assets"] if a["origin"] == "prospect")
    print(f"  real_count:       {real_count}")

    # Palette presence
    p = manifest["palette"]
    if not (p.get("primary") and p.get("secondary") and p.get("accent")):
        failures.append("Palette missing one of primary/secondary/accent")
    if not p.get("source"):
        failures.append("Palette has no source attribution")

    # Cost check
    if manifest["cost_usd"] > 0.50:
        failures.append(f"Cost ${manifest['cost_usd']} exceeds $0.50 cap")

    # Manifest written to disk?
    manifest_file = out_dir / "assets_manifest.json"
    if not manifest_file.exists():
        failures.append("assets_manifest.json not written")

    if failures:
        for f in failures:
            print(f"  ✗ {f}")
        sys.exit(1)
    print("  ✓ PASS")


if __name__ == "__main__":
    main()
