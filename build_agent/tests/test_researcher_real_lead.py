"""Smoke test the researcher against real ATL leads.

This is an integration test — it calls the real Outscraper API and scrapes
real websites. Skips automatically if API keys are missing.

Run: python build_agent/tests/test_researcher_real_lead.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Load .env from the global claude code workspace (where keys live).
try:
    from dotenv import load_dotenv
    env_path = REPO_ROOT.parent / "claude code" / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
except ImportError:
    pass

from build_agent.agents.researcher import research


SAMPLE_LEADS = [
    # GBP-only lead — relies on Outscraper. Currently fails because account is past-due.
    {
        "lead_id": "test-1-gbp-only",
        "business_name": "Mp Services Llc",
        "city": "Atlanta",
        "phone": "",
        "state": "GA",
    },
    # Existing-site path — bypasses Outscraper to prove scraper + synthesis work.
    # gonenova.com is Tyler's own site — safe to scrape, validates the parser.
    {
        "lead_id": "test-2-existing-site",
        "business_name": "Gone Nova",
        "city": "Atlanta",
        "phone": "",
        "state": "GA",
        "existing_url": "https://gonenova.com",
    },
    # Existing-site on the demos vercel project (our deployed plumber example).
    {
        "lead_id": "test-3-demo-site",
        "business_name": "Peach State Plumbing",
        "city": "Marietta",
        "phone": "",
        "state": "GA",
        "existing_url": "https://atlanta-demos.vercel.app",
    },
]


def _validate_sources(brief: dict, lead_id: str) -> list[str]:
    """Walk the brief — flag any non-null field that lacks a source where the schema requires one."""
    failures: list[str] = []
    biz = brief.get("business", {})

    # Reviews must each have source URL
    for i, rev in enumerate(biz.get("real_reviews") or []):
        if not rev.get("source"):
            failures.append(f"{lead_id}: business.real_reviews[{i}] missing source")

    # Photos must each have source URL
    for i, ph in enumerate(biz.get("real_photos") or []):
        if not ph.get("source"):
            failures.append(f"{lead_id}: business.real_photos[{i}] missing source")

    # Services + service_area need source if non-empty
    if biz.get("services") and not biz.get("source_services"):
        failures.append(f"{lead_id}: business.services populated but source_services is null")
    if biz.get("service_area") and not biz.get("source_service_area"):
        failures.append(f"{lead_id}: business.service_area populated but source_service_area is null")

    # Brand palette needs palette_source if any color set
    brand = brief.get("brand") or {}
    if any(brand.get(k) for k in ("primary_color", "secondary_color", "accent_color")):
        if not brand.get("palette_source"):
            failures.append(f"{lead_id}: brand colors set but palette_source is null")

    return failures


def main():
    if not os.environ.get("OUTSCRAPER_API_KEY"):
        print("SKIP: OUTSCRAPER_API_KEY not in env — load from .env first")
        return

    out_dir = REPO_ROOT / "build_agent" / "tests" / "_smoke_output"
    out_dir.mkdir(exist_ok=True)

    total_cost = 0.0
    all_failures: list[str] = []

    for lead in SAMPLE_LEADS:
        print(f"\n── {lead['lead_id']}: {lead['business_name']} ──")
        try:
            brief = research(lead)
        except Exception as e:
            print(f"  ! research() raised: {e}")
            all_failures.append(f"{lead['lead_id']}: exception {e}")
            continue

        # Save full output for human inspection
        out_path = out_dir / f"{lead['lead_id']}.json"
        out_path.write_text(json.dumps(brief, indent=2, default=str))
        print(f"  wrote {out_path.name}")

        cost = (brief.get("_meta") or {}).get("research_cost_usd", 0)
        total_cost += cost
        print(f"  cost: ${cost:.4f}")
        print(f"  build_unfit: {brief.get('build_unfit')}")
        if not brief.get("build_unfit"):
            biz = brief["business"]
            print(f"  rating: {biz.get('rating')}  reviews: {biz.get('review_count')}")
            print(f"  photos: {len(biz.get('real_photos') or [])}  real_reviews: {len(biz.get('real_reviews') or [])}")
            print(f"  vertical: {brief['industry_context']['vertical']}")
            print(f"  palette: {[brief['brand'].get(k) for k in ('primary_color', 'secondary_color', 'accent_color')]}")

        failures = _validate_sources(brief, lead["lead_id"])
        if failures:
            print(f"  ! source-validation failures:")
            for f in failures:
                print(f"    - {f}")
            all_failures.extend(failures)
        else:
            print(f"  ✓ all sources valid")

    print(f"\n── SUMMARY ──")
    print(f"  leads tested:  {len(SAMPLE_LEADS)}")
    print(f"  total cost:    ${total_cost:.4f}")
    print(f"  avg cost/lead: ${total_cost / len(SAMPLE_LEADS):.4f}")
    print(f"  failures:      {len(all_failures)}")
    if all_failures:
        print("  ✗ FAIL")
        sys.exit(1)
    if total_cost / len(SAMPLE_LEADS) > 1.00:
        print(f"  ✗ FAIL: avg cost per lead > $1.00 ceiling")
        sys.exit(1)
    print("  ✓ PASS")


if __name__ == "__main__":
    main()
