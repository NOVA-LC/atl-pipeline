"""Compose a personalized 1200x630 'preview' PNG for each prospect.

The image is the visual hook in MMS/email outreach — it shows the prospect's
ACTUAL business photo (pulled from raw_outscraper) with a 'Preview your new
site' card overlaid on top. Lands in MMS via Twilio's MediaUrl param.

Design contract:
  - Use a real Google photo from raw_outscraper.photos as the background
  - Dark gradient overlay to make text readable
  - Card with business name (display font) + city + city + CTA
  - Output written to <repo>/static/preview/{slug}.png so Vercel serves it at
    https://<demos-host>/static/preview/{slug}.png

Fail-soft: if the prospect has no real photo OR PIL can't fetch it, fall back
to a typography-only card with the business name. We never publish a broken
image, but we always publish ONE.
"""
from __future__ import annotations
import io
import json
import logging
from pathlib import Path
from typing import Optional

import requests

from . import outscraper_fields as osf
from . import photo_library as pl


log = logging.getLogger(__name__)

# Standard social/MMS aspect ratio. Most carriers accept up to 5MB MMS images.
CANVAS_W, CANVAS_H = 1200, 630

# Palette per industry — picks something that'll read on dark/light photos
INDUSTRY_ACCENT = {
    'plumber': '#2563EB',
    'hvac': '#C9A961',
    'radiator': '#D4663E',
    'landscape': '#C85A3A',
    'septic': '#DC2626',
}


def _try_font(size: int, italic: bool = False):
    """Try a series of TrueType paths; fall back to PIL default bitmap font."""
    from PIL import ImageFont
    candidates = [
        # Common Linux locations
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
        # macOS
        '/Library/Fonts/Arial Bold.ttf',
        '/System/Library/Fonts/Helvetica.ttc',
    ]
    if italic:
        candidates = [c.replace('Bold', 'BoldItalic').replace('Sans-', 'Sans-Bold') for c in candidates] + candidates
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _fetch_photo(url: str, timeout: int = 6):
    """Pull a JPEG/PNG from a URL. Returns PIL Image or None on any failure."""
    from PIL import Image
    try:
        r = requests.get(url, timeout=timeout, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; NovaPipelineBot/1.0)',
        })
        if r.status_code != 200:
            return None
        img = Image.open(io.BytesIO(r.content))
        # Drop alpha if present (we'll composite on RGBA canvas anyway)
        return img.convert('RGB')
    except Exception as e:
        log.warning(f'preview fetch failed for {url}: {e}')
        return None


def _cover_resize(img, target_w: int, target_h: int):
    """object-fit: cover equivalent — fill the target, crop overflow."""
    from PIL import Image
    src_w, src_h = img.size
    src_aspect = src_w / src_h
    tgt_aspect = target_w / target_h
    if src_aspect > tgt_aspect:
        # Source is wider — match height, crop width
        new_h = target_h
        new_w = int(src_w * (target_h / src_h))
    else:
        new_w = target_w
        new_h = int(src_h * (target_w / src_w))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip('#')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def _wrap_text(draw, text: str, font, max_width: int) -> list[str]:
    """Manual word-wrap so the business name doesn't overflow the card."""
    words = text.split()
    if not words:
        return []
    lines = []
    current = words[0]
    for word in words[1:]:
        trial = current + ' ' + word
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def generate_preview(lead: dict, output_path: Path | str) -> dict:
    """Compose a preview PNG for one lead and save to output_path.

    Returns {'ok': bool, 'path': str, 'used_real_photo': bool, 'reason'?}.
    Never raises — caller can decide whether to attach to outreach or skip.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return {'ok': False, 'path': '', 'reason': 'Pillow not installed'}

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    industry = pl.industry_for(lead.get('category'))
    accent = INDUSTRY_ACCENT.get(industry, '#2563EB')
    business_name = (lead.get('business_name') or 'Your Business').strip()
    city = (lead.get('city') or '').strip()

    # Pull real Google photo if available
    osf_data = osf.parse_all(lead.get('raw_outscraper'))
    real_photos = osf_data.get('photos') or []
    used_real = False
    bg_img = None
    if real_photos:
        bg_img = _fetch_photo(real_photos[0])
        used_real = bg_img is not None

    # Build the canvas
    canvas = Image.new('RGB', (CANVAS_W, CANVAS_H), color=(15, 20, 30))
    if bg_img is not None:
        bg = _cover_resize(bg_img, CANVAS_W, CANVAS_H)
        # Slight blur so the foreground text stays the focal point
        bg = bg.filter(ImageFilter.GaussianBlur(radius=2))
        canvas.paste(bg, (0, 0))
        # Dark gradient overlay for readability — top transparent → bottom dark
        overlay = Image.new('RGBA', (CANVAS_W, CANVAS_H), (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)
        for y in range(CANVAS_H):
            # Stronger dark at the bottom where the card sits
            alpha = int(60 + (y / CANVAS_H) * 165)
            odraw.line([(0, y), (CANVAS_W, y)], fill=(0, 0, 0, alpha))
        canvas = Image.alpha_composite(canvas.convert('RGBA'), overlay).convert('RGB')
    else:
        # No real photo — paint a dark tonal background with industry accent gradient
        for y in range(CANVAS_H):
            ratio = y / CANVAS_H
            r = int(15 + (40 - 15) * ratio)
            g = int(20 + (50 - 20) * ratio)
            b = int(30 + (70 - 30) * ratio)
            ImageDraw.Draw(canvas).line([(0, y), (CANVAS_W, y)], fill=(r, g, b))

    draw = ImageDraw.Draw(canvas)

    # ===== Foreground card =====
    eyebrow_font = _try_font(20)
    headline_font = _try_font(76)
    sub_font = _try_font(28)
    cta_font = _try_font(22)

    # Wrap business name to fit
    pad = 80
    card_w = CANVAS_W - pad * 2
    headline_lines = _wrap_text(draw, business_name, headline_font, card_w)
    # If 3+ lines, shrink font once
    if len(headline_lines) >= 3:
        headline_font = _try_font(56)
        headline_lines = _wrap_text(draw, business_name, headline_font, card_w)

    # Eyebrow row — small accent square + "PREVIEW BY NOVA"
    eyebrow_y = pad + 30
    draw.rectangle(
        [(pad, eyebrow_y + 4), (pad + 16, eyebrow_y + 20)],
        fill=_hex_rgb(accent),
    )
    draw.text(
        (pad + 28, eyebrow_y),
        'PREVIEW BY NOVA',
        font=eyebrow_font,
        fill=(255, 255, 255),
    )

    # Headline — business name, wrapped, large
    line_h = headline_font.size + 8
    block_h = line_h * len(headline_lines)
    headline_y = (CANVAS_H - block_h) // 2 + 20
    for i, line in enumerate(headline_lines):
        draw.text(
            (pad, headline_y + i * line_h),
            line,
            font=headline_font,
            fill=(255, 255, 255),
        )

    # Subhead — city + category
    if city:
        sub_y = headline_y + block_h + 16
        sub_text = f'{city} · {industry.title()}'
        draw.text((pad, sub_y), sub_text, font=sub_font, fill=(220, 220, 230))

    # CTA pill — bottom right
    cta_text = 'Open your demo  →'
    cta_bbox = draw.textbbox((0, 0), cta_text, font=cta_font)
    cta_w = cta_bbox[2] - cta_bbox[0] + 56
    cta_h = cta_bbox[3] - cta_bbox[1] + 30
    cta_x = CANVAS_W - pad - cta_w
    cta_y = CANVAS_H - pad - cta_h
    # Pill background
    draw.rounded_rectangle(
        [(cta_x, cta_y), (cta_x + cta_w, cta_y + cta_h)],
        radius=cta_h // 2,
        fill=_hex_rgb(accent),
    )
    draw.text(
        (cta_x + 28, cta_y + 13),
        cta_text,
        font=cta_font,
        fill=(255, 255, 255),
    )

    # Save
    canvas.save(output_path, 'PNG', optimize=True)
    return {
        'ok': True,
        'path': str(output_path),
        'used_real_photo': used_real,
    }


def preview_url(slug: str, base: Optional[str] = None) -> str:
    """Public URL where the preview PNG is served from the demos repo via Vercel."""
    import os
    host = base or os.environ.get('DEMOS_BASE_URL') or 'atlanta-demos.vercel.app'
    host = host.rstrip('/').removeprefix('https://').removeprefix('http://')
    return f'https://{host}/static/preview/{slug}.png'


def write_preview_for_lead(lead: dict, repo_path: str | Path) -> dict:
    """Build the preview PNG and write it into the demos repo at the canonical
    path. Returns {'ok', 'url', 'path', ...}.
    """
    slug = lead.get('slug') or f'lead-{lead.get("id")}'
    out_path = Path(repo_path) / 'static' / 'preview' / f'{slug}.png'
    result = generate_preview(lead, out_path)
    result['url'] = preview_url(slug)
    result['slug'] = slug
    return result
