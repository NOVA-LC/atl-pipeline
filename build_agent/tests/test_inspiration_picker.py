"""Smoke test for inspiration_picker against the real corpus."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from build_agent.agents.inspiration_picker import pick


SAMPLE_BRIEFS = [
    {
        "lead_id": "plumbing-test",
        "business": {"name": "Acme Plumbing", "rating": 4.8},
        "industry_context": {"vertical": "plumbing"},
        "owner": {},
    },
    {
        "lead_id": "landscaping-test",
        "business": {"name": "Atlanta Lawn Pros", "rating": 4.9},
        "industry_context": {"vertical": "landscaping"},
        "owner": {},
    },
    {
        "lead_id": "auto-test",
        "business": {"name": "Druid Hills Auto", "rating": 4.7},
        "industry_context": {"vertical": "auto"},
        "owner": {},
    },
]


def main():
    print("== Corpus size check ==")
    corpus_dir = REPO_ROOT / "inspiration"
    n = len(list(corpus_dir.glob("*.meta.json")))
    print(f"  files: {n}")
    if n < 20:
        print(f"  WARN: corpus thin ({n} refs)")

    print("\n== Pick test on 3 verticals ==")
    failures = []
    for brief in SAMPLE_BRIEFS:
        refs = pick(brief, recent_build_fingerprints=[])
        if not refs:
            failures.append(f"{brief['lead_id']}: returned 0 refs")
            continue
        if len(refs) < 3:
            failures.append(f"{brief['lead_id']}: only {len(refs)} refs (need ≥3)")
        # Check at least one ref's industry_fits contains the vertical
        vertical = brief["industry_context"]["vertical"]
        if not any(vertical in (r.get("industry_fits") or []) for r in refs):
            failures.append(f"{brief['lead_id']}: no ref matched vertical {vertical}")
        print(f"  {brief['lead_id']:20s}  picked: {[r['id'] for r in refs]}")

    print("\n== Diversity test (same vertical, second build) ==")
    # First build sets a recent fingerprint; second should AVOID same palette
    brief = SAMPLE_BRIEFS[0]
    first = pick(brief, recent_build_fingerprints=[])
    first_palette = (first[0] if first else {}).get("palette_dominant", [])
    second = pick(brief, recent_build_fingerprints=[{"palette": first_palette}])
    if first and second:
        if first[0]["id"] == second[0]["id"]:
            print(f"  ⚠ second pick top-result == first ({first[0]['id']}) — diversity penalty may be too weak")
        else:
            print(f"  ✓ diversity penalty rotated top pick: {first[0]['id']} → {second[0]['id']}")

    if failures:
        print(f"\n✗ FAIL ({len(failures)} issue(s))")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\n✓ PASS")


if __name__ == "__main__":
    main()
