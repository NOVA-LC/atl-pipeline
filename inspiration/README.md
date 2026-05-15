# Inspiration Corpus

Hand-curated reference screenshots that fuel the build agent's freestyle composition.

Per SPEC §6 — the corpus is **internal reference only**. Screenshots are never
embedded in deployed sites. They feed the inspiration picker, which selects 3-5
refs per build that the builder then references in its prompt.

---

## Curation responsibility

Tyler hand-picks the 60 v1 references. Do NOT scrape this automatically — taste
enters the system at the top of the funnel, not in the middle.

Sources to draw from (in rough preference order):
1. **Awwwards.com** — site of the day / agency portfolios — most explicitly inspirational
2. **Mindsparkle Mag** — editorial design references
3. **Httpster.net** — modern web aesthetics
4. **One Page Love** — focused single-page references
5. **Land-book** — landing-page focused

Avoid: random Pinterest scrapes (licensing murky), Behance student work (signal-to-noise too low), Dribbble dribbles (rarely full sites).

---

## Per-vertical coverage (minimum, before Step 4)

At least 2 refs per industry:
- plumbing
- hvac
- landscaping
- roofing
- auto / mobile mechanic
- electrical
- painting
- cleaning
- pest control
- tree service

That's 20 industry-anchored refs. The remaining 40 should cover the vibes:
rugged-editorial, family-legacy, premium-craftsman, modern-tech, quiet-confidence,
aggressive-bold, neighborhood-local, woman-owned-warm, technical-minimal,
high-touch-luxury.

---

## File naming + structure

Each ref is two files in this directory:

```
{source}-{slug}.{jpg|png}     ← the screenshot, max 1920×1200, < 500 KB
{source}-{slug}.meta.json     ← metadata (schema below)
```

Examples:
- `awwwards-rugged-trade-001.jpg` + `awwwards-rugged-trade-001.meta.json`
- `mindsparkle-family-hvac.png` + `mindsparkle-family-hvac.meta.json`

The base name (without extension) is the `id` used by the inspiration picker.

---

## meta.json schema

```json
{
  "id": "awwwards-rugged-trade-001",
  "source_url": "https://www.awwwards.com/sites/...",
  "captured_at": "2026-05-14",
  "vibe_tags": ["rugged-editorial", "warm-trust", "trade-focused"],
  "industry_fits": ["plumbing", "hvac", "auto", "roofing"],
  "what_works": "Asymmetric hero with a large van photo bleeding off the right edge. The headline lives in the dead space top-left. Reads like a documentary, not an ad.",
  "what_does_not_translate": "The flash-style cursor effect won't carry to a marketing page. Skip that.",
  "palette_dominant": ["#1C1C1C", "#C73B1E", "#E8B931", "#F5F0E8"],
  "type_observations": "Bebas Neue 96px / Inter 16px body. Tight tracking on display.",
  "performed_well_count": 0,
  "performed_poorly_count": 0
}
```

### Field guidance

- **vibe_tags** — pick 2-4 from the master tag list (see below). Don't invent new tags without updating the picker.
- **industry_fits** — even if the original site is for a tech startup, list which TRADE industries this composition would translate to.
- **what_works** — 1-2 sentences. The compositional / treatment insight that makes this worth referencing. Be concrete.
- **what_does_not_translate** — 1 sentence. The thing that would NOT survive translation to a contractor site (e.g. flash effects, custom fonts that cost $400, multi-page state).
- **palette_dominant** — 3-5 hex values, ordered by visual weight.
- **type_observations** — 1 sentence on the typographic decisions.
- **performed_well_count** / **performed_poorly_count** — start at 0. Updated by the calibration loop based on rep ratings.

---

## Master vibe-tag list (use only these)

`rugged-editorial`, `family-legacy`, `premium-craftsman`, `modern-tech`,
`quiet-confidence`, `aggressive-bold`, `neighborhood-local`, `woman-owned-warm`,
`technical-minimal`, `high-touch-luxury`, `documentary-photographic`,
`typographic-statement`, `process-led`, `testimonial-led`, `warmly-handmade`.

If a candidate ref doesn't fit any of these, push back on whether it should be
in the corpus at all.

---

## After Step 0 sign-off

Run `ls *.jpg *.png 2>/dev/null | wc -l` — must return ≥ 60.
Run `ls *.meta.json | wc -l` — must equal the screenshot count.
Industry coverage check: every industry above has ≥ 2 refs whose `industry_fits` includes it.
