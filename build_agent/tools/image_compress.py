"""Image compression + crop — target ≤ 200 KB per asset.

Pure deterministic via Pillow. No API spend.
"""
from __future__ import annotations

from pathlib import Path


def compress(image_path: Path, target_kb: int = 200) -> Path:
    """Compress in-place. Returns the path. Best-effort; if PIL missing, no-op."""
    try:
        from PIL import Image
    except ImportError:
        return image_path
    try:
        img = Image.open(image_path)
        # Normalize to RGB (drops alpha; we don't deploy transparent images)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        # Downsample large dimensions
        max_dim = 1600
        if max(img.size) > max_dim:
            img.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS)
        # Iterate quality until under target
        out_path = image_path.with_suffix(".jpg")
        for q in (85, 75, 65, 55, 45):
            img.save(out_path, format="JPEG", quality=q, optimize=True, progressive=True)
            if out_path.stat().st_size <= target_kb * 1024:
                break
        # If we changed the extension, remove the original
        if out_path != image_path and image_path.exists():
            image_path.unlink()
        return out_path
    except Exception:
        return image_path


def auto_crop(image_path: Path, aspect_ratio: tuple[int, int]) -> Path:
    """Crop to the closest match of the target aspect ratio, centered.
    Returns same path (in-place). No-op on error."""
    try:
        from PIL import Image
    except ImportError:
        return image_path
    try:
        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        target_w, target_h = aspect_ratio
        target_aspect = target_w / target_h
        actual_aspect = img.width / img.height
        if abs(actual_aspect - target_aspect) < 0.05:
            return image_path  # close enough
        if actual_aspect > target_aspect:
            # Too wide — crop sides
            new_w = int(img.height * target_aspect)
            left = (img.width - new_w) // 2
            img = img.crop((left, 0, left + new_w, img.height))
        else:
            # Too tall — crop top/bottom
            new_h = int(img.width / target_aspect)
            top = (img.height - new_h) // 2
            img = img.crop((0, top, img.width, top + new_h))
        img.save(image_path, format="JPEG", quality=85, optimize=True)
        return image_path
    except Exception:
        return image_path


def get_dimensions(image_path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size  # (w, h)
    except Exception:
        return None
