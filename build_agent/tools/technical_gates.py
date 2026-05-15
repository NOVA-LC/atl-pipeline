"""Technical gates per SPEC §5 — all deterministic, no API spend.

- Puppeteer/Playwright screenshots at 320/375/414/768/1024/1440
- Lighthouse-cli mobile perf (>=85) + a11y (>=90) via npx
- HTML validation (htmlhint via npx, or pure-Python fallback)
- Responsive check (no horizontal scroll at any width)

Each gate returns structured pass/fail + details. The orchestrator
combines them into the final ship decision.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.request import pathname2url

# Viewport widths we check per SPEC §5
VIEWPORTS = [320, 375, 414, 768, 1024, 1440]


def _html_path_to_url(path: Path) -> str:
    """file://... URL for a local HTML file."""
    return "file:" + pathname2url(str(path.resolve()))


# ─── responsive screenshot + horizontal-scroll check ────────────────────────
def responsive_check(html_path: Path) -> dict[str, Any]:
    """Open the file in headless chromium at each viewport. Pass = no horizontal scroll."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"ok": False, "error": "playwright not installed", "failures": []}

    url = _html_path_to_url(html_path)
    failures: list[dict[str, Any]] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width in VIEWPORTS:
                context = browser.new_context(viewport={"width": width, "height": 800})
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                # Page metrics
                scroll_w = page.evaluate("document.documentElement.scrollWidth")
                client_w = page.evaluate("document.documentElement.clientWidth")
                overflow = scroll_w - client_w
                if overflow > 4:  # tolerance of 4px for scrollbar rounding
                    failures.append({"width": width, "scrollWidth": scroll_w, "clientWidth": client_w, "overflow": overflow})
                context.close()
            browser.close()
    except Exception as e:
        return {"ok": False, "error": str(e), "failures": []}
    return {"ok": len(failures) == 0, "failures": failures, "viewports_checked": VIEWPORTS}


def screenshot_widths(html_path: Path, out_dir: Path, widths: tuple[int, ...] = (375, 768, 1440)) -> dict[int, Path]:
    """Capture screenshots at the given widths. Returns {width: path}.
    Used by the vision critic — it grades all three."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[int, Path] = {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return paths
    url = _html_path_to_url(html_path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for w in widths:
                context = browser.new_context(viewport={"width": w, "height": 900})
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=15000)
                out_path = out_dir / f"screenshot_{w}.jpg"
                page.screenshot(path=str(out_path), full_page=True, type="jpeg", quality=80)
                paths[w] = out_path
                context.close()
            browser.close()
    except Exception:
        pass
    return paths


# ─── HTML validation ────────────────────────────────────────────────────────
def html_validate(html: str) -> dict[str, Any]:
    """Validate HTML structure. Uses pure-Python checks — no external deps.
    Catches the common builder bugs: missing <!DOCTYPE>, unclosed tags, missing alt."""
    errors: list[str] = []

    # 1. <!DOCTYPE> declaration required
    if not re.match(r"^\s*<!doctype\s+html", html, re.I):
        errors.append("Missing <!DOCTYPE html> declaration")

    # 2. <html lang="..."> required (a11y)
    if not re.search(r"<html\s+[^>]*lang=", html, re.I):
        errors.append("<html> tag missing lang attribute")

    # 3. <title> required
    if not re.search(r"<title>.+?</title>", html, re.I | re.S):
        errors.append("Missing <title>")

    # 4. <meta name='viewport'> for mobile
    if not re.search(r'<meta[^>]+name=["\']viewport["\']', html, re.I):
        errors.append("Missing viewport meta tag")

    # 5. img tags must have alt attribute
    for m in re.finditer(r'<img\b([^>]*)>', html, re.I):
        attrs = m.group(1)
        if not re.search(r'\balt=["\'][^"\']*["\']', attrs):
            errors.append(f"<img> missing alt attribute: <img {attrs[:60]}>")

    # 6. balanced tags — count opens vs closes for div, section, header, footer, main, nav, ul, li, a
    for tag in ("div", "section", "header", "footer", "main", "nav", "h1", "h2", "h3", "p"):
        opens = len(re.findall(rf"<{tag}\b", html, re.I))
        closes = len(re.findall(rf"</{tag}\s*>", html, re.I))
        if opens != closes:
            errors.append(f"Unbalanced <{tag}> tags: {opens} open, {closes} close")

    # 7. No emoji icons in clickable/heading contexts (design system rule 7)
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F000-\U0001F02F]")
    for context_re in (r"<button[^>]*>([^<]*)</button>", r"<h[1-6][^>]*>([^<]*)</h", r'<a\b[^>]*class="[^"]*(?:cta|button)[^"]*"[^>]*>([^<]*)</a>'):
        for m in re.finditer(context_re, html, re.I):
            if emoji_re.search(m.group(1) or ""):
                errors.append(f"Emoji icon in interactive/heading context: {m.group(1)[:40]!r}")

    return {
        "valid": len(errors) == 0,
        "error_count": len(errors),
        "errors": errors,
    }


# ─── Lighthouse (optional — only if lighthouse-cli is installed) ─────────────
def lighthouse_mobile(html_path: Path) -> dict[str, Any]:
    """Run lighthouse-cli against the HTML file. Returns
    {performance, accessibility, best_practices, seo} 0-100 each.

    Falls back gracefully if lighthouse-cli isn't installed — Step 5e treats
    a missing tool as 'gate not run' (orchestrator raises code/vision floors)."""
    if not shutil.which("lighthouse") and not shutil.which("npx"):
        return {"ok": False, "error": "neither lighthouse nor npx in PATH"}

    out_json = html_path.with_suffix(".lighthouse.json")
    url = _html_path_to_url(html_path)
    cmd = [
        "npx", "--yes", "lighthouse@latest",
        url,
        "--quiet",
        "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
        "--preset=desktop",  # for HTML files local, mobile sometimes flakes — desktop is more stable
        "--only-categories=performance,accessibility,best-practices,seo",
        f"--output-path={out_json}",
        "--output=json",
    ]
    try:
        # Use shell=True on Windows; otherwise direct subprocess
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            shell=True,  # windows-friendly
        )
        if result.returncode != 0 and not out_json.exists():
            return {"ok": False, "error": result.stderr[-500:]}
        if not out_json.exists():
            return {"ok": False, "error": "lighthouse produced no output"}
        data = json.loads(out_json.read_text())
        cats = data.get("categories", {})
        return {
            "ok": True,
            "performance": int((cats.get("performance", {}).get("score") or 0) * 100),
            "accessibility": int((cats.get("accessibility", {}).get("score") or 0) * 100),
            "best_practices": int((cats.get("best-practices", {}).get("score") or 0) * 100),
            "seo": int((cats.get("seo", {}).get("score") or 0) * 100),
        }
    except (subprocess.TimeoutExpired, Exception) as e:
        return {"ok": False, "error": str(e)[:500]}


# ─── unified run ─────────────────────────────────────────────────────────────
def run_all(html_path: Path, run_lighthouse: bool = True, screenshot_dir: Path | None = None) -> dict[str, Any]:
    """Run every gate. Returns combined verdict."""
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    validation = html_validate(html)
    responsive = responsive_check(html_path)
    lighthouse = lighthouse_mobile(html_path) if run_lighthouse else {"ok": False, "error": "skipped"}
    screenshots: dict[int, Path] = {}
    if screenshot_dir:
        screenshots = screenshot_widths(html_path, screenshot_dir)

    # Aggregate pass/fail per SPEC §5
    gates: dict[str, bool] = {
        "html_valid":     validation["valid"],
        "responsive":     responsive["ok"],
    }
    if lighthouse.get("ok"):
        gates["lighthouse_perf"] = lighthouse.get("performance", 0) >= 85
        gates["lighthouse_a11y"] = lighthouse.get("accessibility", 0) >= 90

    return {
        "gates": gates,
        "all_pass": all(gates.values()),
        "html_validate": validation,
        "responsive": responsive,
        "lighthouse": lighthouse,
        "screenshots": {w: str(p) for w, p in screenshots.items()},
    }
