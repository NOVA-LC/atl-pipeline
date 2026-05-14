"""Image compression — target ≤ 200 KB per asset.

Pure deterministic via Pillow.

Step 1 STATUS: stub. Implemented in Step 3.
"""
from __future__ import annotations

from pathlib import Path


def compress(image_path: Path, target_kb: int = 200) -> Path:
    """Compresses in-place. Returns the path."""
    raise NotImplementedError("Step 3 deliverable")


def auto_crop(image_path: Path, aspect_ratio: tuple[int, int]) -> Path:
    """Crops to the closest match of the target aspect ratio, centered."""
    raise NotImplementedError("Step 3 deliverable")
