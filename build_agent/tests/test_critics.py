"""Smoke test the code critic + technical gates on fixtures + a real build.

Vision critic is gated on RUN_VISION_CRITIC=1 because it spends Sonnet vision $.
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

from build_agent.agents.critic_code import grade as grade_code
from build_agent.tools import technical_gates


GOOD = REPO_ROOT / "build_agent" / "tests" / "fixtures" / "known_good.html"
BAD = REPO_ROOT / "build_agent" / "tests" / "fixtures" / "known_bad.html"
BUILT = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "test-build-output.html"
ASSETS_MAN = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "assets_test" / "assets_manifest.json"
BRIEF = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "test-3-demo-site.json"


def _empty_brief():
    return {"business": {}, "industry_context": {"vertical": "general_trade"}, "owner": {}, "brand": {}}


def section(name):
    print(f"\n{'=' * 60}\n  {name}\n{'=' * 60}")


def main():
    failures: list[str] = []

    section("CODE CRITIC — known_good.html (expect: pass-ish)")
    html = GOOD.read_text(encoding="utf-8")
    v = grade_code(html, _empty_brief(), {})
    print(f"  score: {v['score']}")
    print(f"  must_fixes: {len(v['must_fixes'])}")
    for m in v["must_fixes"][:5]:
        print(f"    - [{m['intent']}] {m['text']}")
    print(f"  strengths: {v['strengths']}")
    print(f"  fingerprint hero: {v['fingerprint']['hero_composition']}")
    if v["score"] < 80:
        failures.append(f"known_good code score {v['score']} < 80")

    section("CODE CRITIC — known_bad.html (expect: many must_fixes)")
    html = BAD.read_text(encoding="utf-8")
    v = grade_code(html, _empty_brief(), {})
    print(f"  score: {v['score']}")
    print(f"  must_fixes: {len(v['must_fixes'])}")
    for m in v["must_fixes"][:8]:
        print(f"    - [{m['intent']}] {m['text']}")
    if v["score"] > 50:
        failures.append(f"known_bad code score {v['score']} > 50 — critic too lenient")
    if len(v["must_fixes"]) < 3:
        failures.append(f"known_bad only {len(v['must_fixes'])} must_fixes — critic missing rules")

    section("CODE CRITIC — test-build-output.html (real Sonnet-built site)")
    if BUILT.exists():
        html = BUILT.read_text(encoding="utf-8")
        brief = json.loads(BRIEF.read_text(encoding="utf-8")) if BRIEF.exists() else _empty_brief()
        manifest = json.loads(ASSETS_MAN.read_text(encoding="utf-8")) if ASSETS_MAN.exists() else {}
        v = grade_code(html, brief, manifest)
        print(f"  score: {v['score']}")
        print(f"  must_fixes ({len(v['must_fixes'])}):")
        for m in v["must_fixes"]:
            print(f"    - [{m['intent']}] {m['text']}")
        print(f"  strengths: {v['strengths']}")
        print(f"  fingerprint: {v['fingerprint']}")
        print(f"  asset_stats: {v['asset_stats']}")
    else:
        print("  SKIP: test-build-output.html not found")

    section("HTML VALIDATION — known_good vs known_bad")
    g = technical_gates.html_validate(GOOD.read_text(encoding="utf-8"))
    b = technical_gates.html_validate(BAD.read_text(encoding="utf-8"))
    print(f"  known_good valid: {g['valid']}  errors: {g['error_count']}")
    print(f"  known_bad valid:  {b['valid']}  errors: {b['error_count']}")
    if not g["valid"]:
        print(f"  known_good errors: {g['errors']}")
    for e in b["errors"][:8]:
        print(f"    - {e}")
    if g["valid"] is False:
        failures.append(f"known_good HTML invalid: {g['errors'][:3]}")
    if b["valid"] is True:
        failures.append("known_bad HTML reported as valid — validator too lenient")

    section("RESPONSIVE CHECK — known_good vs known_bad (Playwright)")
    rg = technical_gates.responsive_check(GOOD)
    rb = technical_gates.responsive_check(BAD)
    print(f"  known_good: ok={rg.get('ok')} failures={len(rg.get('failures', []))}")
    if rg.get("error"):
        print(f"    error: {rg['error']}")
    print(f"  known_bad: ok={rb.get('ok')} failures={len(rb.get('failures', []))}")
    if rb.get("failures"):
        for f in rb["failures"][:3]:
            print(f"    - {f}")
    if rb.get("ok"):
        failures.append("known_bad passed responsive check — should fail (fixed 600px width)")

    section("SCREENSHOTS — test-build-output at 3 widths")
    if BUILT.exists():
        ss_dir = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "screenshots"
        ss = technical_gates.screenshot_widths(BUILT, ss_dir)
        print(f"  wrote {len(ss)} screenshots:")
        for w, p in ss.items():
            size_kb = p.stat().st_size // 1024 if p.exists() else 0
            print(f"    {w}px → {p.name} ({size_kb} KB)")

    section("VISION CRITIC — gated on RUN_VISION_CRITIC=1")
    if os.environ.get("RUN_VISION_CRITIC") == "1" and BUILT.exists():
        from build_agent.agents.critic_vision import grade as grade_vision
        from build_agent.agents.inspiration_picker import pick
        ss_dir = REPO_ROOT / "build_agent" / "tests" / "_smoke_output" / "screenshots"
        ss = technical_gates.screenshot_widths(BUILT, ss_dir) if not (ss_dir / "screenshot_375.jpg").exists() else {
            375: ss_dir / "screenshot_375.jpg",
            768: ss_dir / "screenshot_768.jpg",
            1440: ss_dir / "screenshot_1440.jpg",
        }
        brief = json.loads(BRIEF.read_text(encoding="utf-8")) if BRIEF.exists() else _empty_brief()
        refs = pick(brief)
        v = grade_vision(ss, brief, inspiration_refs=refs)
        final = v.get("final_weighted")
        print(f"  final_weighted: {final}")
        for axis in ("composition", "type", "color", "photography", "originality", "craft"):
            ax = v.get(axis) or {}
            print(f"    {axis}: {ax.get('score')} — {ax.get('reason','')[:80]}")
        print(f"  must_fixes: {v.get('must_fixes')}")
        meta = v.get("_meta", {})
        print(f"  cost: ${meta.get('cost_usd', 0)}")
    else:
        print("  SKIP")

    section("RESULT")
    if failures:
        print(f"  ✗ FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("  ✓ PASS")


if __name__ == "__main__":
    main()
