"""Section library + design token catalog.

Tokens (palettes, type pairs, spacing) and section variants are described in
JSON metadata files alongside each Jinja partial. This module loads all of them
at import-time into in-memory dicts. The composition agent gets these dicts as
context so it can pick from real, available options instead of inventing names.

Layout on disk:
  templates/sections/<kind>/<name>.html.j2        — Jinja partial
  templates/sections/<kind>/<name>.json           — metadata: when it fits
  templates/tokens/palettes/<name>.json
  templates/tokens/type/<name>.json
  templates/tokens/spacing/<name>.json
  templates/shells/<name>.html.j2                 — outer page shell
"""
from __future__ import annotations
import json
from pathlib import Path

TPL_DIR = Path(__file__).parent.parent / 'templates'
SECTIONS_DIR = TPL_DIR / 'sections'
TOKENS_DIR = TPL_DIR / 'tokens'
SHELLS_DIR = TPL_DIR / 'shells'

SECTION_KINDS = ['hero', 'services', 'gallery', 'reviews', 'cta']


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def load_sections() -> dict:
    """Return {kind: {name: {metadata, partial_path}}} for every section variant."""
    out = {}
    for kind in SECTION_KINDS:
        kind_dir = SECTIONS_DIR / kind
        out[kind] = {}
        if not kind_dir.is_dir():
            continue
        for tpl in kind_dir.glob('*.html.j2'):
            name = tpl.stem.replace('.html', '')
            meta_path = kind_dir / f'{name}.json'
            out[kind][name] = {
                'name': name,
                'partial': f'sections/{kind}/{tpl.name}',
                'metadata': _read_json(meta_path),
            }
    return out


def load_tokens(category: str) -> dict:
    """Return {name: token_dict} for one of: palettes, type, spacing."""
    out = {}
    d = TOKENS_DIR / category
    if not d.is_dir():
        return out
    for jf in d.glob('*.json'):
        out[jf.stem] = _read_json(jf)
    return out


def load_shells() -> dict:
    """Return {name: 'shells/name.html.j2'} for every shell."""
    out = {}
    if not SHELLS_DIR.is_dir():
        return out
    for tpl in SHELLS_DIR.glob('*.html.j2'):
        name = tpl.stem.replace('.html', '')
        out[name] = f'shells/{tpl.name}'
    return out


def load_all() -> dict:
    """Bundle for validators + agent prompt context."""
    sections = load_sections()
    palettes = load_tokens('palettes')
    type_pairs = load_tokens('type')
    spacing = load_tokens('spacing')
    shells = load_shells()
    return {
        'sections': sections,
        'palettes': palettes,
        'type_pairs': type_pairs,
        'spacing': spacing,
        'shells': shells,
        # Sets for fast membership checks in validators
        'available': {
            'sections': {k: set(v.keys()) for k, v in sections.items()},
            'palettes': set(palettes.keys()),
            'type_pairs': set(type_pairs.keys()),
            'spacing': set(spacing.keys()),
            'shells': set(shells.keys()),
        },
    }


def compact_for_agent(catalog: dict) -> dict:
    """Return a slim dict for passing to the composition agent (no Jinja paths)."""
    return {
        'sections': {
            kind: {name: v['metadata'] for name, v in variants.items()}
            for kind, variants in catalog['sections'].items()
        },
        'palettes': {name: v for name, v in catalog['palettes'].items()},
        'type_pairs': {name: v for name, v in catalog['type_pairs'].items()},
        'spacing': {name: v for name, v in catalog['spacing'].items()},
        'shells': list(catalog['shells'].keys()),
    }
