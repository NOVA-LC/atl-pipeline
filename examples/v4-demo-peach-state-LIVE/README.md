# v4 pipeline — LIVE end-to-end run (real LLM calls, photo-honest)

Real output from running the new pipeline end-to-end against a synthetic
"Peach State Plumbing & Drain" lead on 2026-05-11, with **zero stock-photo
padding**. PTC now refuses to pick photo-heavy hero/gallery variants when
the lead has no real GBP photos, and the assembler refuses to fill
photo-required sections with Unsplash.

This is the WORST-CASE rendering: a real business with zero photos to
work from. Production runs against real Atlanta leads will have actual
Google Business Profile photos flowing through `photo_grade.py` (palette
color-grading) into the hero + gallery slots.

## Open the rendered demo

- **raw.githack**: https://raw.githack.com/NOVA-LC/atl-pipeline/claude/atl-pipeline-setup-hdYeK/examples/v4-demo-peach-state-LIVE/index.html

## What you're looking at

- Hero: `rugged-trade` with solid `rugged-shop-orange` palette background
  (no photo, because there are no real photos to use)
- Services: bold-list, 5 service tiles, owner-voice copy
- "What we don't do" band + guarantee pull-quote
- Reviews: 3 real review quotes with dates
- Gallery: **suppressed entirely** (no real photos → no section, no stock)
- CTA + footer with license #, neighborhoods, last-updated

## Awwwards verdict

> **TIER: MID (score 68/100)**
> "Copy is punching agency; design is still playing it safe — add motion,
> break one layout rect, and put prices on the cards to close the gap."

The score went MID → AGENCY (83) → MID (68) across three runs as we
fixed real bugs. Run 2 was 83 because it was lying (stock-photo padding
the page felt richer). Run 3 is 68 because the page is now honest about
having no photos. **Both numbers are useful — they bracket the gap that
real GBP photos (production) or AI-generated brand imagery (next phase)
have to fill.**

## Cost: $0.04/lead

- Voice fingerprint: ~$0 (archetype path)
- Design PTC (4 candidates, Haiku): ~$0.001
- Compose (Sonnet): $0.031
- Assemble: $0
- Awwwards (Sonnet): $0.010
- **Total: $0.042** — way under $0.15 per-lead cap

## Bugs fixed this run

1. **PTC was photo-blind.** `_score_candidate` had zero awareness of the
   `requires.min_real_photos` field on hero/gallery variants. It happily
   picked `full-bleed-photo` for a lead with zero real photos, then the
   assembler padded with Unsplash. **Fix:** PTC now takes a
   `real_photo_count` arg, penalizes -35 for hero variants that need
   more photos than available, scales gallery penalty by the gap, and
   rewards type-forward heroes (+6) when imagery is thin. PTC also
   tells the LLM designer the photo count in the user message so it
   routes intentionally.

2. **Assembler force-padded with Unsplash.** `_resolve_images` always
   filled `images.hero` (industry stock) and padded `gallery` to 4 with
   stock. **Fix:** zero stock-photo padding. Hero is empty when no real
   photos exist (template renders solid-palette background). Gallery is
   only real photos.

3. **Gallery section rendered regardless of photo count.** Even when
   a gallery variant required 5+ photos and the lead had 0, the section
   rendered with stock fill. **Fix:** assembler now drops the `gallery`
   key from `sections` and `section_names` when the chosen variant's
   `min_photos` exceeds what's actually available. The shell template
   skips the missing section gracefully.

## What's still unsolved (the honest gap)

Awwwards' three persistent must-fixes:
1. Zero motion attributes — GSAP/ScrollTrigger are loaded but no
   sections emit `data-motion`. Templates need to opt in.
2. Zero price anchors on service tiles — compose has the data but
   isn't surfacing it onto tile JSON.
3. No rectangle-break layout — every section is centered stacked rects.
   Need one full-bleed, sticky-caption, or asymmetric grid moment.

And the bigger one:
4. **Photo strategy when no real photos exist.** The GitHub-research
   agent confirmed no OSS has solved this end-to-end. Replicate FLUX
   schnell or similar for brand-tuned hero/gallery generation is the
   open frontier. Worth ~$0.003-0.01 per image; well within budget.

## Files

- `index.html` — rendered demo (27KB, no `<img>` tags, zero stock)
- `agent_log.json` — composed dict + Awwwards verdict + cost breakdown
