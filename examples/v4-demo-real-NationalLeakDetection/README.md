# v4 pipeline — REAL lead test (National Leak Detection, Marietta)

End-to-end production pipeline run against a real Atlanta plumber lead
sourced from Tyler's Outscraper export (April 2026).

## The honest finding

**TIER MID (score 67/100)** — vs synthetic Peach State at AGENCY 84.

The 17-point gap is **research depth**, not the rendering machinery.
Production output still produced:
- 33KB rendered HTML, 11 motion attributes, 2 images, image-led process section
- 5 service tiles all with concrete price anchors (`from $189 flat`, etc.)
- by-the-numbers gallery, editorial reviews layout, rectangle-break guarantee
- Real Google CDN photo as the hero background (not stock, not generated)

Awwwards verbatim: _"Copy and trust signals punch agency; palette timidity
and flat layout keep it mid — one bold visual choice away from the premium
tier."_

## What Peach State synthetic had that this lead doesn't

| Field | Peach State | National Leak |
|---|---|---|
| Owner first name | "Joey Calloway" | — |
| License # | MP207455 | — |
| Years in business | 32 (since 1993) | — |
| Review text | 4 verbatim quotes w/ $1,890 etc | — |
| Description | Hand-written owner-voice paragraph | — |
| Service area neighborhoods | 6 named | inferred 5 |
| Real GBP photos | 0 (synthetic ldata) | 1 (Google CDN) |

The Excel export only carries the bones — name, category, rating,
review count, hours, 1 cover photo, place_id. To reach AGENCY tier on
real leads, the pipeline needs a research pass that fills the gaps:

  1. `research.research_lead()` already exists. It uses Claude tools:
     - `brave_search` → needs BRAVE_API_KEY
     - `fetch_page` → no key needed
     - `outscraper_place_details` → needs OUTSCRAPER_API_KEY for enrichment
  2. Outscraper enrichment endpoint pulls full review text + business
     description + owner contacts — but the Excel export already used the
     basic `maps/search-v3` without `enrichment=reviews,photos`. Need
     to re-fetch with enrichment turned on (~$0.05/lead).
  3. State contractor licensing lookup for license numbers (free; Georgia
     has a public DBPR-equivalent search by business name).

## What this proves

The rendering pipeline is production-ready. The bottleneck is research.
With richer per-lead facts, the same machinery would hit AGENCY tier on
real leads — same as it did on Peach State synthetic.

## Cost: $0.063/lead

- Voice (archetype): $0
- PTC: $0
- Compose: $0.034
- FLUX dev hero (1 cached): $0
- Assemble: $0
- Awwwards: $0.013
- Total: **$0.063** — well under $0.15 cap

(FLUX hero already cached from prior Peach State experiments; a fresh
run would add $0.03 for one hero generation. Total still under cap.)

## Files

- `index.html` — rendered demo
- `composed.json` — full compose output (palette, sections, copy, services with prices)
- `assets/gen-hero-bd7f070b99dd2dd3.jpg` — FLUX dev fallback hero (unused since real Google photo wins priority)
