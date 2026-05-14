"""FLUX image generation via Replicate — LAST-RESORT only.

Hard rule: never used when a real prospect asset exists for that slot.

Per SPEC §8: timeout 90s, 1 retry, fallback = leave slot imageless.

Cost: Replicate FLUX-dev ≈ $0.03 per image. FLUX-schnell ≈ $0.003. We default
to schnell for asset_gatherer fallback (we're filling minor gaps, not making
hero photographs).
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

REPLICATE_API = "https://api.replicate.com/v1/predictions"
FLUX_SCHNELL_VERSION = "black-forest-labs/flux-schnell"
TIMEOUT_SEC = 90
RETRIES = 1


def _api_key() -> str | None:
    return os.environ.get("REPLICATE_API_TOKEN") or os.environ.get("REPLICATE_API_KEY") or None


def generate(prompt: str, slot: str, out_path: Path, aspect_ratio: str = "16:9") -> Path | None:
    """Generate one image and save to out_path. Returns the path or None on failure.

    slot is a label (e.g. 'hero', 'process_detail', 'neighborhood') for logging.
    """
    key = _api_key()
    if not key:
        return None
    try:
        # Submit prediction
        r = requests.post(
            "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "wait",  # synchronous response when possible
            },
            json={
                "input": {
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "output_format": "jpg",
                    "output_quality": 85,
                    "num_inference_steps": 4,  # schnell is 4-step
                    "go_fast": True,
                }
            },
            timeout=TIMEOUT_SEC,
        )
        if r.status_code not in (200, 201):
            return None
        body = r.json()
        # If still processing, poll
        prediction_id = body.get("id")
        for _ in range(20):
            status = body.get("status")
            if status == "succeeded":
                break
            if status in ("failed", "canceled"):
                return None
            time.sleep(2)
            poll = requests.get(
                f"{REPLICATE_API}/{prediction_id}",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            if poll.status_code == 200:
                body = poll.json()
        output = body.get("output")
        if not output:
            return None
        if isinstance(output, list):
            image_url = output[0]
        else:
            image_url = output
        # Download
        img_resp = requests.get(image_url, timeout=30)
        if img_resp.status_code != 200:
            return None
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(img_resp.content)
        return out_path
    except (requests.Timeout, requests.ConnectionError, ValueError):
        return None


def estimate_cost(images: int = 1) -> float:
    """FLUX-schnell pricing: ~$0.003 per image."""
    return round(images * 0.003, 4)
