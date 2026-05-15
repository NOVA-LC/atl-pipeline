"""Smoke test the builder by generating a real site for one lead.

COSTS REAL MONEY — calls Sonnet via Anthropic API.
Set RUN_LIVE_BUILDER=1 to actually run.
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
    env_path = REPO_ROOT.parent / "claude code" / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=True)
except ImportError:
    pass

from build_agent.agents.inspiration_picker import pick
from build_agent.agents.builder import build_html


def main():
    if not os.environ.get("RUN_LIVE_BUILDER"):
        print("SKIP: set RUN_LIVE_BUILDER=1 to spend ~$0.07 on a live builder test")
        return

    brief_path = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "test-3-demo-site.json"
    manifest_path = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "assets_test" / "assets_manifest.json"
    if not brief_path.exists():
        print(f"SKIP: run test_researcher_real_lead.py first")
        return
    if not manifest_path.exists():
        print(f"SKIP: run test_asset_gatherer.py first")
        return

    brief = json.loads(brief_path.read_text())
    manifest = json.loads(manifest_path.read_text())

    print("== Picking inspiration refs ==")
    refs = pick(brief)
    print(f"  picked: {[r['id'] for r in refs]}")

    print("\n== Calling builder (Sonnet) ==")
    result = build_html(brief, manifest, refs)
    print(f"  model: {result['model']}")
    print(f"  in_tokens:  {result['input_tokens']}")
    print(f"  out_tokens: {result['output_tokens']}")
    print(f"  cost: ${result['cost_usd']}")
    print(f"  html length: {len(result['html'])} chars")

    # Write to disk for inspection
    out = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "test-build-output.html"
    out.write_text(result["html"], encoding="utf-8")
    print(f"  wrote {out}")

    # Validation
    failures = []
    html = result["html"].lower()
    if not html.startswith("<!doctype") and not html.startswith("<html"):
        failures.append("HTML doesn't start with <!DOCTYPE or <html>")
    if len(result["html"]) > 200 * 1024:
        failures.append(f"HTML > 200KB ({len(result['html'])} bytes)")
    if "lorem ipsum" in html or "placeholder" in html and "todo" not in html:
        failures.append("HTML contains lorem ipsum or placeholder text")
    # img tags should only reference ./assets/
    import re
    img_srcs = re.findall(r'<img[^>]+src="([^"]+)"', result["html"])
    for src in img_srcs:
        if src.startswith("http") and "fonts.gstatic.com" not in src:
            failures.append(f"<img> src points outside ./assets/: {src}")
    if result["cost_usd"] > 2.00:
        failures.append(f"single build call exceeded $2 cost: {result['cost_usd']}")

    if failures:
        print(f"\n✗ FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\n✓ PASS")


if __name__ == "__main__":
    main()
