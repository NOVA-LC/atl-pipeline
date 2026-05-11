"""AI brand-photo generation — FLUX schnell on Replicate.

The frontier problem per the GitHub-research agent: no OSS site builder for
local SMBs handles the "looks AI" tell when the business has weak or zero
Google photos. Stock-photo padding contradicts the "real work" claim above
it; suppressing the gallery leaves the page visually sparse. AI-generated
imagery — done right — fills the gap without lying.

Strategy:
  - Only fire when trusted real_photo_count < TRIGGER_THRESHOLD (default 4)
    AND REPLICATE_API_TOKEN is set in env. Otherwise this module is a no-op.
  - Use FLUX schnell ($0.003/image) — 6 images = $0.018, well under the
    $0.15 per-lead cap. Higher-quality FLUX dev/pro reserved for premium
    leads.
  - Per-lead prompts encode palette as a color-grade specification, trade
    as a documentary-photojournalism subject, location as concrete city
    geography. The goal is "documentary photograph, working light, slight
    grain" — not "studio marketing photo."
  - Negative prompts strip the obvious AI tells (plastic skin, melty hands,
    over-saturation, watermarks).
  - Output: local file paths (PNG/JPEG) for hero + gallery. Assembler
    serves them as static assets alongside the rendered HTML.

Cost-tracked through the same cost.CostTracker the rest of the pipeline
uses, so budget caps apply.

Disabled by default — opts in via REPLICATE_API_TOKEN env var. When
disabled, returns empty dict and the assembler falls through to the
by-the-numbers gallery + empty-hero behavior.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Replicate FLUX models — schnell is the sweet spot for SMB demo budgets.
# Pricing (May 2026): schnell $0.003/img, dev $0.030/img, 1.1-pro $0.040/img.
DEFAULT_MODEL = 'black-forest-labs/flux-schnell'
DEFAULT_TIMEOUT_S = 60
DEFAULT_LONG_EDGE = 1280  # 16:9 → 1280x720 for hero, square for gallery

# Fire only when business has fewer than this many trusted real photos.
TRIGGER_THRESHOLD = 4

# Per-image cost in CENTS for the cost tracker — schnell is 0.3¢.
COST_CENTS_PER_IMAGE = {
    'black-forest-labs/flux-schnell': 0.3,
    'black-forest-labs/flux-dev': 3.0,
    'black-forest-labs/flux-1.1-pro': 4.0,
}


# Palette name → color-grade spec the model can interpret as a film stock.
# Each maps to (mood, key lighting, tint) the FLUX vision encoder reads well.
PALETTE_GRADES: dict[str, str] = {
    'warm-earth': (
        'warm sepia-amber color grade, late-afternoon golden-hour natural light, '
        'soft warm shadows, slight film grain, Kodak Portra 400 film stock aesthetic'
    ),
    'clean-trade-blue': (
        'crisp neutral color grade, clean overcast daylight, slightly cool shadows, '
        'commercial photography clarity, Fujifilm Pro 400H aesthetic'
    ),
    'modern-charcoal': (
        'high-contrast cool monochrome-leaning grade, low-key industrial lighting, '
        'deep shadow falloff, fine film grain, Ilford HP5 black-and-white aesthetic '
        '(but in color, with desaturated accents)'
    ),
    'rugged-shop-orange': (
        'high-contrast warm grade with burnt-orange accents, dramatic shop-light '
        'tungsten + cool window-light mix, deep shadows, gritty texture, '
        'documentary photojournalism style'
    ),
    'heritage-navy-gold': (
        'refined warm-neutral grade with gold-toned highlights, soft north-window '
        'natural light, editorial restraint, slight grain, Kodak Ektar 100 aesthetic'
    ),
    'emergency-red': (
        'punchy high-contrast grade with controlled red accents, hard direct '
        'sunlight or single dramatic light source, action-photography clarity'
    ),
}

# Trade vertical → documentary subject sets the model handles well. Subjects
# are intentionally specific and trade-realistic — "hands on PEX fitting" not
# "smiling plumber in clean uniform."
TRADE_SUBJECTS: dict[str, dict] = {
    'plumber': {
        'hero': (
            'documentary photograph of a working plumbers gloved hands on a copper '
            'pipe joint under a residential sink, soldering torch flame in soft '
            'focus, water droplet on the pipe, captured candidly mid-job, no faces '
            'visible, three-quarter angle, shallow depth of field'
        ),
        'gallery': [
            'work van with ladder rack and tool boxes parked on a suburban driveway, '
            'side-three-quarter view, no logos visible, late afternoon light, '
            'documentary realism, no people',

            'close-up of pipe wrenches and PEX fittings laid out on a clean cloth '
            'on a tile floor, top-down composition, shallow depth of field, '
            'professional but lived-in, no text or watermarks',

            'water heater installed in a basement utility area, fresh copper '
            'supply lines, photographed straight-on with a slight up angle, '
            'cool fluorescent light, no people in frame',

            'a hand inspecting a drain camera monitor showing a pipe interior, '
            'over-the-shoulder framing, screen glow lighting the hand, '
            'photojournalism realism, no faces visible',

            'a clean main-line cleanout cover newly replaced in a backyard, '
            'morning shadows, slight dew, ground-level perspective, '
            'no people in frame',
        ],
    },
    'hvac': {
        'hero': (
            'documentary photograph of a technicians gloved hand holding a digital '
            'manifold gauge near an outdoor AC condenser unit, summer evening light, '
            'three-quarter framing, no face visible, shallow depth of field'
        ),
        'gallery': [
            'roof-top HVAC unit on a residential home with a small ladder leaning '
            'against the side, golden-hour light, no people, no logos',

            'close-up of a hand cleaning condenser coil fins with a soft brush, '
            'side angle, evening light, shallow depth of field, no faces',

            'thermostat being installed on a beige interior wall, screwdriver in a '
            'hand, professional but realistic, slight motion blur on the hand',

            'a clean ductwork installation in an unfinished basement ceiling, '
            'measured camera framing, even neutral light, no people',

            'an outdoor condenser pad with fresh copper line set running up the '
            'side of a brick house, morning light, no people, no logos',
        ],
    },
    'radiator': {
        'hero': (
            'documentary photograph of weathered hands inspecting an automotive '
            'radiator core on a shop workbench, single hanging shop-light overhead, '
            'industrial setting, three-quarter framing, no face visible'
        ),
        'gallery': [
            'classic muscle car nose-up on a shop floor with the radiator removed, '
            'low ambient shop light, no people, gritty realistic texture',

            'top-down view of tools on a workbench: torque wrench, fluid cans, '
            'shop rag with smudges, brass radiator cap, shallow depth of field',

            'engine bay close-up with fresh coolant lines and a rebuilt radiator '
            'just installed, side angle, single key light, no people',

            'rolling tool chest in a working garage with drawers slightly ajar, '
            'side angle, ambient fluorescent + tungsten mix, no logos',

            'mechanics weathered glove resting on a chrome radiator hose clamp, '
            'macro close-up, shallow depth, documentary realism',
        ],
    },
    'landscape': {
        'hero': (
            'documentary photograph of a hand pulling pine straw from a wheelbarrow '
            'over a freshly mulched bed, suburban yard, late afternoon light, '
            'three-quarter framing, no face visible'
        ),
        'gallery': [
            'fresh sod laid in a backyard with a roller and shovel leaning against '
            'a wooden fence, golden-hour light, no people',

            'commercial mower trailer parked on a residential street with mowers '
            'visible, no logos, side angle, soft morning light',

            'close-up of a hand placing pine straw bales near a freshly edged '
            'flower bed, shallow depth, no face visible',

            'finished landscape with neat mulched beds, ornamental grasses, and a '
            'brick walkway, photographed straight-on at eye level, evening light',

            'pruning shears resting on a workbench with cut branches and a '
            'leather glove, top-down composition, soft natural light',
        ],
    },
    'septic': {
        'hero': (
            'documentary photograph of a heavy-duty service truck with a vacuum tank '
            'parked on a rural property, side three-quarter angle, working light, '
            'no logos visible, no people in frame'
        ),
        'gallery': [
            'septic tank lid removed for inspection on a green lawn, technician '
            'gloved hand reaching toward it, no face visible, midday light',

            'roll of perforated drainfield pipe staged near a freshly dug trench, '
            'shovel in the soil, golden-hour light, no people',

            'gauge cluster on the side of a vacuum truck, macro detail, slightly '
            'weathered metal, slight dust, soft side light',

            'a clean drainfield cleanout cap newly installed in a backyard, '
            'ground-level perspective, morning light, no people',

            'service truck driving down a rural Georgia road with red clay '
            'shoulders and pine trees, three-quarter rear view, no logos',
        ],
    },
}


# Negative-prompt clauses applied to every generation. These strip the most
# obvious "AI photo" tells without crippling FLUX schnell's photoreal mode.
NEGATIVE_PROMPT = (
    'cartoon, illustration, 3d render, plastic skin, glossy, over-saturated, '
    'fashion model, smiling at camera, melty fingers, distorted hands, '
    'extra fingers, text overlay, watermark, logo, signage, deformed, '
    'low-resolution, jpeg artifacts, painting'
)


def _slug_for_cache(prompt: str, palette_name: str, kind: str, idx: int) -> str:
    """Stable hash so re-renders of the same lead don't re-pay Replicate."""
    key = f'{kind}|{palette_name}|{idx}|{prompt}'
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _replicate_run(model: str, payload: dict, token: str, timeout: int) -> Optional[str]:
    """Synchronously kick off a Replicate prediction and poll until the
    output URL is available. Returns the first output URL or None on error.
    """
    create_url = f'https://api.replicate.com/v1/models/{model}/predictions'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
        'Prefer': 'wait',  # block up to ~60s for completion before returning
    }
    try:
        resp = requests.post(create_url, headers=headers, json={'input': payload},
                             timeout=timeout)
    except requests.RequestException as e:
        log.warning('replicate create failed: %r', e)
        return None
    if resp.status_code not in (200, 201):
        log.warning('replicate create %s: %s', resp.status_code, resp.text[:300])
        return None

    body = resp.json()
    status = body.get('status')
    out = body.get('output')
    # If the Prefer:wait header succeeded, we're done.
    if status == 'succeeded' and out:
        return out[0] if isinstance(out, list) else out

    # Otherwise poll the get URL.
    get_url = (body.get('urls') or {}).get('get')
    if not get_url:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1.5)
        try:
            r2 = requests.get(get_url, headers=headers, timeout=10)
        except requests.RequestException:
            continue
        if r2.status_code != 200:
            continue
        b2 = r2.json()
        if b2.get('status') == 'succeeded':
            o2 = b2.get('output')
            return o2[0] if isinstance(o2, list) else o2
        if b2.get('status') in ('failed', 'canceled'):
            log.warning('replicate prediction %s: %s',
                        b2.get('status'), b2.get('error'))
            return None
    return None


def _download(url: str, dest: Path, timeout: int = 30) -> bool:
    try:
        r = requests.get(url, stream=True, timeout=timeout)
    except requests.RequestException as e:
        log.warning('download failed: %r', e)
        return False
    if r.status_code != 200:
        log.warning('download %s: %s', r.status_code, url[:120])
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    return True


def generate_brand_photos(
    industry: str,
    palette_name: str,
    palette_dict: dict,
    business: dict,
    out_dir: Path,
    tracker,
    *,
    model: str = DEFAULT_MODEL,
    want_hero: bool = True,
    want_gallery: int = 5,
) -> dict:
    """Generate hero + gallery images for one lead, palette-graded.

    Returns:
      {
        'hero': str | None,        # local relative path or None on failure
        'gallery': list[str],      # local relative paths, may be shorter than requested
        'cost_cents': float,
        'errors': list[str],
      }

    No-ops cleanly when REPLICATE_API_TOKEN is unset.
    """
    out = {'hero': None, 'gallery': [], 'cost_cents': 0.0, 'errors': []}
    token = os.environ.get('REPLICATE_API_TOKEN', '').strip()
    if not token:
        out['errors'].append('REPLICATE_API_TOKEN not set — skipping image gen')
        return out

    trade = TRADE_SUBJECTS.get(industry)
    if not trade:
        out['errors'].append(f'no trade-subject map for industry {industry!r}')
        return out

    grade = PALETTE_GRADES.get(palette_name) or PALETTE_GRADES['clean-trade-blue']
    city = (business.get('city') or 'an Atlanta suburb').strip()
    cost_per = COST_CENTS_PER_IMAGE.get(model, 0.5)

    def _enrich(prompt_base: str) -> str:
        # Common envelope around every generation: location grounding, grade,
        # documentary intent. Keeps the model anchored on realism.
        return (
            f'{prompt_base}, on location in {city}, '
            f'{grade}, candid documentary photojournalism, '
            f'shot on Leica M11 with 35mm lens, slight film grain, '
            f'no AI-rendered look, photoreal'
        )

    # === Hero ===
    if want_hero:
        prompt = _enrich(trade['hero'])
        payload = {
            'prompt': prompt,
            'aspect_ratio': '16:9',
            'output_format': 'jpg',
            'output_quality': 88,
            'num_outputs': 1,
            'go_fast': True,
            'megapixels': '1',
            # FLUX schnell ignores negative prompts; keeping the field for
            # future flux-dev/pro upgrades that honor it.
            'disable_safety_checker': False,
        }
        url = _replicate_run(model, payload, token, DEFAULT_TIMEOUT_S)
        if url:
            slug = _slug_for_cache(prompt, palette_name, 'hero', 0)
            dest = out_dir / f'gen-hero-{slug}.jpg'
            if _download(url, dest):
                out['hero'] = dest.name  # relative to out_dir for static serving
                out['cost_cents'] += cost_per
            else:
                out['errors'].append(f'hero download failed: {url[:120]}')
        else:
            out['errors'].append('hero generation returned no URL')

    # === Gallery ===
    if want_gallery > 0:
        for i, subj in enumerate(trade['gallery'][:want_gallery]):
            prompt = _enrich(subj)
            payload = {
                'prompt': prompt,
                'aspect_ratio': '4:5',  # portrait-ish; reads as real-photo
                'output_format': 'jpg',
                'output_quality': 86,
                'num_outputs': 1,
                'go_fast': True,
                'megapixels': '1',
            }
            url = _replicate_run(model, payload, token, DEFAULT_TIMEOUT_S)
            if not url:
                out['errors'].append(f'gallery[{i}] no URL')
                continue
            slug = _slug_for_cache(prompt, palette_name, 'gallery', i)
            dest = out_dir / f'gen-gallery-{slug}.jpg'
            if _download(url, dest):
                out['gallery'].append(dest.name)
                out['cost_cents'] += cost_per
            else:
                out['errors'].append(f'gallery[{i}] download failed')

    # === Cost tracking ===
    if hasattr(tracker, 'add_cents'):
        tracker.add_cents(out['cost_cents'])
    elif hasattr(tracker, 'per_lead_spent_cents'):
        tracker.per_lead_spent_cents += out['cost_cents']

    return out
