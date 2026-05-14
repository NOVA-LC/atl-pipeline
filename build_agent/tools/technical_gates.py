"""Technical gates per SPEC §5:
- Puppeteer screenshots at 320 / 375 / 414 / 768 / 1024 / 1440
- lighthouse-cli for mobile perf (≥85) + a11y (≥90)
- htmlhint for HTML validation (zero errors)
- responsive check (no horizontal scroll at any width)

All deterministic. No API spend.

Step 1 STATUS: stub. Implemented in Step 5.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def screenshot_at(url_or_html_path: str, viewport_width: int, out_path: Path) -> Path:
    """Headless Chrome screenshot at a specific viewport width."""
    raise NotImplementedError("Step 5 deliverable")


def lighthouse_mobile(url: str) -> dict[str, Any]:
    """Returns {performance, accessibility, best_practices, seo} 0-100 each."""
    raise NotImplementedError("Step 5 deliverable")


def html_validate(html: str) -> dict[str, Any]:
    """Returns {valid: bool, errors: [...]}."""
    raise NotImplementedError("Step 5 deliverable")


def responsive_check(html_path: Path) -> dict[str, Any]:
    """Returns {ok: bool, failures: [{width, scrollWidth}]} for widths 320-1440."""
    raise NotImplementedError("Step 5 deliverable")
