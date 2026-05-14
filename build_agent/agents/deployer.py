"""Deployer — push final HTML to Vercel and return the preview URL.

Target: https://preview.gonenova.com/<slug>?expires=YYYYMMDD per SPEC §8.

Step 1 STATUS: stub. Implemented in Step 7.
"""
from __future__ import annotations

from typing import Any


def deploy(slug: str, html: str, expires_days: int = 7) -> dict[str, Any]:
    """Returns {url, deployment_id, expires_at}."""
    raise NotImplementedError("Step 7 deliverable")
