# v4 pipeline — LIVE end-to-end run (real LLM calls)

Real output from running the new pipeline end-to-end against a synthetic
"Peach State Plumbing & Drain" lead on 2026-05-11, with all 4 Tier 4
stages firing against the real Anthropic API.

## Open the rendered HTML

- **htmlpreview**: https://htmlpreview.github.io/?https://github.com/NOVA-LC/atl-pipeline/blob/claude/atl-pipeline-setup-hdYeK/examples/v4-demo-peach-state-LIVE/index.html
- **raw.githack** (faster): https://raw.githack.com/NOVA-LC/atl-pipeline/claude/atl-pipeline-setup-hdYeK/examples/v4-demo-peach-state-LIVE/index.html

## Awwwards verdict

> **TIER: MID (score 68/100)**
> "Copy and trust signals punch agency; design execution stalls at mid — motion-dead, photo-grading unconfirmed, and zero price anchors waste the flat-rate brand promise."

**Strengths:**
- Owner-voice signals are **exceptional**: first name in CTA, "what we don't do" callout, dated last-updated, license # repeated, named crew members throughout
- Lora/Inter pairing with warm-earth palette is a deliberate, trade-appropriate choice that avoids the generic blue-teal gradient trap
- Service tiles carry real specifics: camera footage emailed, 4000 PSI hydro-jet, Joey-or-Caleb crew policy — well above bullet-list tier

**Must-fixes (per Awwwards):**
1. Add at least one entry-point price anchor per service tile (currently only slab leak has one — `from $1,890 flat`)
2. Introduce selective motion: scroll-triggered fade on reviews, sticky-CTA slide-in
3. Grade all gallery photos with a warm amber/sepia tint to lock them into the palette

## Pipeline stages — actual data

| Stage | Outcome | Cost |
|-------|---------|------|
| Voice fingerprint | `register=blue_collar` | ~$0 (archetype path) |
| Design PTC (4 candidates) | Winner: `warm-earth + lora-inter + full-bleed-photo` score 80 | ~$0.001 |
| Compose (Sonnet) | Full copy w/ license#, neighborhoods, what-we-dont-do, guarantee, 5 services | $0.032 |
| Assemble | 30,327-byte HTML, Lora display (17×), warm-earth palette, sticky-cta (7×), trust-strip (8×) | $0 |
| Awwwards classifier | Tier MID, score 68 | $0.010 |
| **Total** | **TIER: MID 68/100** | **$0.042** |

Under the $0.15 per-lead budget cap, well under the $0.25 ceiling we'd want for production.

## Signals that landed (live grep on the rendered HTML)

- License # rendered 2× (incl. footer)
- `MP207455` 3×
- `East Cobb` 5× · `Joey` 9× · `Caleb` 2×
- `Main Line Backed Up` / `in East Cobb?` (hero headline)
- `Last updated`, `What we don't`, `guarantee` — all rendered
- `data-motion` 2×, `sticky-cta` 7×, `trust-strip` 8×
- GSAP 23× · Lenis 7× (motion lib wired)
- `Lora` 17× (Tier 3 type pair applied)
- Price anchors: `$1,890 flat`, `$4,500` (one tile + one review citation)

## Bugs found + fixed during this run

1. **Compose returned empty** because `thinking={'type':'adaptive'}` + `output_config={'effort':'medium'}` had Sonnet burning 100% of max_tokens budget on thinking with zero text output. Fixed by removing those experimental params and bumping max_tokens to 8000.

2. **PTC design hint wasn't reaching assemble** because compose's new design-pin prompt told Sonnet "design is decided, just write copy" — so compose legitimately omitted `palette`/`type_pair`/`sections` from its JSON. Fixed by backfilling those fields in the orchestrator from the PTC hint AFTER compose returns.

3. **Compose had no diagnostic on parse failures** — when it returned `{'_parse_error': True, '_last_text': ''}`, there was no way to tell apart "model returned prose" from "no text block produced". Added `_blocks` (block-type breakdown) + `_stop_reason` so future failures can be triaged in one log line.

## Files

- `index.html` — rendered demo (30KB)
- `agent_log.json` — composed dict + fingerprint + Awwwards verdict + cost breakdown
