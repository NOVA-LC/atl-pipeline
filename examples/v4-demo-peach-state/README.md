# v4 pipeline end-to-end demo — Peach State Plumbing & Drain

What the new v4 pipeline produces visually for a representative Atlanta plumber lead, with every Tier 1-4 rule active.

## Open the rendered HTML

GitHub serves HTML as plain text. To view it rendered:

- **htmlpreview.github.io** (easiest):
  https://htmlpreview.github.io/?https://github.com/NOVA-LC/atl-pipeline/blob/claude/atl-pipeline-setup-hdYeK/examples/v4-demo-peach-state/index.html
- **raw.githack.com** (faster CDN):
  https://raw.githack.com/NOVA-LC/atl-pipeline/claude/atl-pipeline-setup-hdYeK/examples/v4-demo-peach-state/index.html
- **Locally**: `python -m http.server -d examples/v4-demo-peach-state 8000` then open http://localhost:8000

## What this demonstrates

**Tier 1 — CRO + UX polish**
- Sticky mobile call CTA (always-visible button on phones)
- Trust strip above the fold (rating · reviews · license #)
- Branded styling, no template-y fallbacks

**Tier 2 — Hard-to-fake signals**
- License # `MP207455` rendered on every invoice mention (3× in document, including footer)
- Owner first name (`Joey`) in CTAs
- Neighborhoods strip (`East Cobb · Smyrna · Vinings · Sandy Springs · Roswell · Powers Ferry`)
- "What we don't do" section — 3 specific exclusions
- Owner guarantee with concrete promise
- Price signals on services (`flat $189`, `from $349`, `from $1,890 flat`)
- Owner-voice copy throughout ("Joey himself", "Joey or Caleb")

**Tier 3 — Design system + motion**
- Palette: `heritage-navy-gold` (committed editorial palette, not Inter/purple-gradient template)
- Type pair: `lora-inter` (Lora display + Inter body — editorial serif fits "32 years in business" angle)
- Sections variant-picked: `hero=minimal-type`, `services=numbered-grid`, `gallery=masonry`, `reviews=full-width-quote`, `cta=phone-prominent`
- GSAP + Lenis motion lib wired (`data-motion=...` × 4)
- Last-updated stamp in footer

**Tier 4 — Quality enforcement (would run if API were reachable)**
- `design_ptc` would enumerate 4 design candidates and pick this one for anti-clone + vertical fit
- `awwwards` would classify the final HTML as `agency` / `mid` / `template` with must-fix list

## What's NOT exercised in this snapshot

The LLM-driven copy (headline factory, voice critic, compose) was simulated by hand because Anthropic API calls 401 in the local sandbox. Production runs against the real lead DB on Railway hit the full LLM stack — copy quality there is what Tier 2 headline-factory + voice-critic produces, not the hand-written copy here.

## Files

- `index.html` — rendered demo (28KB)
- `composed.json` — the dict that would have come from `compose()` after PTC pinned the design
- `render.py` — reproducible render script (run with `PYTHONPATH=<repo-root> python render.py`)
