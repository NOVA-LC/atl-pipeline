# Build Agent — v1 Spec (Phase 0)

**Status:** Pre-implementation. Scope locked. Ready to build.
**Owner:** Tyler Brown (gonenova) — sales call live; he presses "B = interested + start building" in the dialer; this agent has ~10 minutes and ~$7 to land a site the prospect believes is theirs before he hangs up.
**Last updated:** 2026-05-14
**Repo location:** `atl-pipeline/build_agent/` (new directory)

---

## Quick start for whoever picks this up

1. Read **North Star** + **Calibration Loop** before anything else. They define what "good" means and how the system knows.
2. The system is **inspiration corpus + design system primitives + freestyle builder + code critic + vision critic + technical gates + rep approval**. No templates. No social scraping. Owner age estimation cut.
3. Build order is in **Build Sequence (Phase 0)** at the bottom — strict, do not skip.
4. Anything not explicitly in scope is **Out of Scope** and belongs to a later phase. Do not creep.
5. The single most load-bearing assumption is corpus quality. If the first 10 builds look generic, the corpus is wrong, not the agent.

---

## 1. North Star

When the agent finishes, the lead opens the link and feels two things:

1. **"That's *my* business."** Real photos, real reviews, real colors, real owner name, real services, real address — nothing invented.
2. **"That doesn't look like a template."** Structurally novel from every other site we've built — even other plumbers.

Hit both or don't ship. Score floor (Awwwards 97, Vision 7.5/10) is a proxy. The ground truth is Tyler's 1–5 rating on each build. The system is calibrated to that, not to its own critic.

---

## 2. Calibration Loop (FIRST-CLASS SYSTEM COMPONENT)

> **This is the feedback engine. Every other piece in the system is hill-climbing without it. Treat as a P0 deliverable, not a "nice to have."**

### The widget

After every build completes and the rep approves it, the dialer pops a non-dismissible 1–5 widget on Tyler's screen:

```
Did this feel like {{owner_first}}'s business?
[ 1 ] [ 2 ] [ 3 ] [ 4 ] [ 5 ]
Optional: one line on what worked / didn't worked → __________
```

### Storage

```sql
CREATE TABLE build_calibration (
  build_id        TEXT PRIMARY KEY,
  lead_id         TEXT NOT NULL,
  feels_like_score INTEGER NOT NULL CHECK(feels_like_score BETWEEN 1 AND 5),
  feels_like_note TEXT,
  code_critic_score REAL,
  vision_critic_score REAL,
  builder_inspiration_ids JSON,
  built_at        TIMESTAMP,
  rep_approved    INTEGER DEFAULT 0,
  lead_opened_url INTEGER DEFAULT 0,        -- did they click the SMS?
  outcome         TEXT                       -- booked|interested|ghost|dnc
);
```

### Use of the data

- **Monthly recalibration:** pull the 5 worst-rated + 5 best-rated builds. Re-screenshot them. Manually annotate "why this scored what it scored." Update the vision critic's system prompt with those 10 examples as concrete few-shot anchors. Repeat monthly.
- **Inspiration corpus refinement:** when a build with `feels_like_score >= 4` ships, the inspiration_ids that fed it get a +1 "performed well" counter. When a build with `feels_like_score <= 2` ships, -1. Refs that consistently underperform get pulled or re-tagged.
- **Per-vertical learning:** correlate `feels_like_score` with industry. If "plumber" builds average 2.8 but "landscaper" averages 4.2, the plumber corpus + system prompt needs work.

### Until 50 builds have ratings, every Sonnet critic prompt change is theater. Do not waste cycles tuning the critic prompts before ground truth exists.

---

## 3. Architecture

```
                            ┌────────────────────────────────┐
                            │   ORCHESTRATOR (Sonnet 4.6)    │
                            │   budget cap, deadline, daily  │
                            │   cap, tool dispatch, fallback │
                            └──────┬─────────────────────────┘
                                   │
        ┌──────────┬──────────┬────┴─────┬──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼          ▼          ▼
   RESEARCHER  ASSETS    INSPIRATION  BUILDER   CODE         VISION    DEPLOYER →
   (Sonnet)    (Haiku)   PICKER       (Sonnet)  CRITIC       CRITIC    NOTIFIER (rep approval)
                         (Haiku)                (deterministic, (Sonnet
                                                Awwwards-style) vision)
                                                              +
                                                           TECHNICAL
                                                           GATES
                                                       (Lighthouse,
                                                        HTML validate,
                                                        responsive)
```

### Data flow

1. Orchestrator receives `{lead_id, business_name, phone, owner_name?}` from dialer's "B" press.
2. Pre-filter: if lead has no GBP AND no existing website → reject, tell rep "skip this lead."
3. Researcher → `research_brief.json` with sources required for every fact.
4. Asset gatherer → `/builds/<slug>/assets/` with real images, palette JSON.
5. Inspiration picker → 3–5 corpus refs that fit this prospect.
6. Builder → writes HTML/CSS from scratch using brief + refs + design system primitives.
7. Loop:
   - Code critic → score
   - Technical gates → pass/fail (Lighthouse mobile, HTML valid, responsive screenshots)
   - Vision critic → rubric score (see §4)
   - If gates pass AND code ≥ 90 AND vision ≥ 7.5 → break
   - Else: orchestrator dispatches a fix to the right sub-agent. Cap at 6 iterations.
8. Deployer → `preview.gonenova.com/<slug>?expires=YYYYMMDD`
9. Notifier → ping the REP (not the lead) with "Site ready, review and approve to send."
10. After rep clicks "Send," SMS goes to lead.
11. Calibration widget pops up for rep's 1–5 rating.

---

## 4. Vision Critic Rubric (EXPLICIT, NOT "rate as a designer would")

The vision critic receives three screenshots (mobile 375, tablet 768, desktop 1440) + the research brief. Returns a strict JSON with six weighted axes:

```json
{
  "composition":            { "score": 1-10, "reason": "..." },
  "type":                   { "score": 1-10, "reason": "..." },
  "color":                  { "score": 1-10, "reason": "..." },
  "photography":            { "score": 1-10, "reason": "..." },
  "originality":            { "score": 1-10, "reason": "..." },
  "craft":                  { "score": 1-10, "reason": "..." },
  "final_weighted":         <number 1-10>,
  "must_fixes":             ["concrete actionable fix", "..."],
  "should_fixes":           ["..."]
}
```

### Weighting

| Axis | Weight | What it measures |
|---|---|---|
| **Originality** | 0.25 | Does this look like a designer made it for this business, or like a template? Compare against the inspiration refs the builder used — too literal = downgrade. |
| **Composition** | 0.20 | Hierarchy, balance, focal point. Is the eye led? |
| **Type** | 0.15 | Pairing, scale rhythm, line height. Are weights and sizes intentional? |
| **Color** | 0.15 | Harmony, restraint, contrast (a11y included). Does color do work or just decorate? |
| **Photography** | 0.15 | Treatment, crop, integration. Are photos compositional elements or just blocks? |
| **Craft** | 0.10 | Alignment, spacing consistency, small details. The stuff only senior designers notice. |

Final = weighted sum. Floor: **7.5/10**.

### Calibration anchor (mandatory at prompt time)

The vision critic's system prompt MUST include **at least 5 worked examples** of scored builds with reasoning, drawn from the calibration table. Cold-start: hand-curate 5 anchor sites (2 great, 2 mid, 1 bad) before first build.

### Anti-drift rules

- Critic must reference the **inspiration refs** the builder used in its reasoning. If it can't see what the builder was reaching for, it's grading in the dark.
- Critic must give at least one **must_fix** when score is < 9.0 — no vague "polish overall."
- Critic outputs are logged. Monthly review reads 20 random outputs and checks for prompt drift.

---

## 5. Hard Quality Gates (ship-or-don't)

All four must pass before the site can be sent to the rep:

| Gate | Threshold | How |
|---|---|---|
| **Code critic** | ≥ 90 | Existing `iterate.py` Awwwards grader, extended with realness + cross-section consistency checks |
| **Vision critic** | ≥ 7.5/10 | §4 rubric, mobile + tablet + desktop screenshots |
| **Technical: Lighthouse mobile** | perf ≥ 85, a11y ≥ 90 | `lighthouse-cli` via headless Chrome, deterministic |
| **Technical: HTML validates** | zero errors | `htmlhint` or `html-validate`, deterministic |
| **Technical: Responsive** | no horizontal scroll at 320/375/414/768/1024/1440 | Puppeteer screenshot at each width + check `document.body.scrollWidth <= window.innerWidth` |
| **Realness** | ≥ 60% of `<img>` tags sourced from prospect (not FLUX) | Asset registry tracks origin per file |
| **Hallucination check** | zero unsourced factual claims | `research_brief` enforced via `source` field; builder is forbidden from rendering anything without a source |

Any gate fails → orchestrator dispatches the appropriate fix and retries. After 6 iterations or budget exhaustion → **ship best-so-far + dashboard banner: "Site shipped at code=92, vision=6.8 — needs Tyler review."**

### Rep approval gate (mandatory before any lead SMS)

Site lands in dialer. Rep sees:
- Live URL preview iframe
- All four gate scores
- A **green "Send to lead" button** + a **red "Reject and don't send" button**
- Auto-SMS to lead is impossible without the green click.

---

## 6. Inspiration Corpus

### v1 target: 60 references (not 200)

Curated by Tyler in 4-6 hours, one-time. Expand to 100-150 only after first 10 real builds expose corpus gaps.

### Structure

```
inspiration/
  awwwards-001.jpg
  awwwards-001.meta.json     {source_url, vibe_tags, industry_fits, what_works, palette_dominant}
  mindsparkle-014.jpg
  mindsparkle-014.meta.json
  httpster-203.jpg
  ...
```

### Tagging schema (per ref)

```json
{
  "id": "awwwards-001",
  "source_url": "https://www.awwwards.com/sites/...",
  "captured_at": "2026-05-14",
  "vibe_tags": ["rugged-editorial", "trade-focused", "warm-trust"],
  "industry_fits": ["plumbing", "hvac", "auto", "roofing"],
  "what_works": "Asymmetric hero with a large van photo bleeding off the right edge. The headline lives in the dead space top-left. Reads like a documentary, not an ad.",
  "what_does_not_translate": "The flash-style cursor effect won't carry to a marketing page. Skip that.",
  "palette_dominant": ["#1C1C1C", "#C73B1E", "#E8B931", "#F5F0E8"],
  "type_observations": "Bebas Neue 96px / Inter 16px body. Tight tracking on display.",
  "performed_well_count": 0,
  "performed_poorly_count": 0
}
```

### Selection (Inspiration Picker agent)

Input: `research_brief`.
Output: 3-5 ref IDs the builder should reference.
Logic: industry_fit match + vibe_tag distance from the brand's signals + diversity rule (never pick refs whose dominant palette overlaps with the last 5 builds by > 50%).

### Diversity enforcement (NOT template-based)

Track per-build **fingerprint**: dominant-palette hash + hero composition class (split / centered / asymmetric / full-bleed) + section sequence pattern. New build must differ from last 5 builds on ≥ 2 of those dimensions. Soft penalty in critic, not hard reject. (Pushback #5 from the design review: three plumbers can all use the rugged-trade aesthetic if the assets, copy, and palette differ enough.)

### Legal notes

- Corpus is **internal reference only**. Screenshots never embedded in deployed sites.
- Every ref has `source_url`. Tracked for attribution if ever published.
- Awwwards / Mindsparkle / Httpster are preferred sources — they exist explicitly to be inspirational. Avoid random Pinterest scrapes.
- If a referenced design system, font, or component is licensed (e.g. paid theme), the BUILDER is system-prompted to never copy verbatim — only learn composition/treatment principles.

---

## 7. Design System Primitives

A small, opinionated tokens-and-rules layer that makes "ugly" hard while leaving composition free.

### Tokens

```css
/* design_system/primitives.css */
:root {
  /* spacing scale — never deviate */
  --s-1: 4px; --s-2: 8px; --s-3: 16px; --s-4: 24px; --s-5: 32px;
  --s-6: 48px; --s-7: 64px; --s-8: 96px; --s-9: 128px; --s-10: 192px;

  /* type scale — clamp-based */
  --t-display: clamp(56px, 10vw, 160px);
  --t-h1:      clamp(40px, 6vw, 88px);
  --t-h2:      clamp(28px, 4vw, 56px);
  --t-h3:      clamp(22px, 2.5vw, 36px);
  --t-body:    clamp(15px, 1.1vw, 18px);
  --t-eyebrow: clamp(11px, 0.8vw, 13px);

  /* colors are per-build, but always 3 max: --bg, --fg, --accent */
}
```

### Rules (enforced in builder system prompt)

1. Never more than 3 primary colors. Accent is < 8% of viewport area.
2. Type is two families max — one for display, one for body. Never three.
3. Spacing must come from the scale. No arbitrary 17px paddings.
4. Body text contrast ≥ 4.5:1, headlines ≥ 3:1 against background.
5. Mobile hero headline must fit on one line at 375px viewport.
6. Every CTA has a target outside `tel:` or `mailto:` (real button, real URL).
7. No emoji icons on $1K+ tier builds. SVG inline or no icons.
8. No stock photography. Real prospect assets or no image in that slot.

### Component primitives (optional, freestyle composes them)

`.button-primary`, `.card`, `.eyebrow`, `.pull-quote`, `.split-50`, `.full-bleed`. Builder may use or ignore. Not templates — just safe defaults.

---

## 8. Failure Modes + Constraints

### Per-build constraints

| Limit | Value | Behavior at limit |
|---|---|---|
| Wall-clock budget | 12 min | Ship best-so-far, flag in dialer |
| Spend budget | $7.00 | Ship best-so-far, flag in dialer |
| Critic iterations | 6 | Ship best-so-far |
| Tool retries | 2 | Then fall back |

### Daily fleet constraints

```yaml
DAILY_FLEET_CAP_USD: 100.00
ON_CAP_REACHED:
  - reject new build requests
  - dialer banner: "Build agent paused: daily cap $100 reached. Manual unlock at /build/unlock"
  - notify Tyler via SMS
  - log all queued requests for next-day retry
```

`POST /build/unlock` requires a confirmation token Tyler types. No silent restart.

### Tool failure handling

| Tool | Timeout | Retries | Fallback |
|---|---|---|---|
| Outscraper GBP | 30s | 2 (1s, 4s backoff) | Skip, lean on existing-site scrape |
| Existing-site scrape | 30s | 1 | Skip, lean on GBP |
| Brave search | 15s | 2 | Skip, use only sourced facts |
| Anthropic API | 60s | 2 (exp backoff) | Pause iteration, return best-so-far |
| FLUX image gen | 90s | 1 | Use real asset if available; otherwise skip image slot |
| Vercel deploy | 90s | 1 | Fall back to GitHub Pages static URL |
| Lighthouse CLI | 60s | 1 | Skip technical gate, raise code+vision thresholds (code 92, vision 8.0) |
| Twilio SMS | 10s | 2 | Log to dashboard, notify rep manually |

### "No web presence" path

Pre-filter in dialer: when a lead is queued, check `lead.vercel_url` AND `lead.has_gbp_photos`. If both empty/false → mark as `build_unfit` and surface in dialer as **"⚠ Skip for build — no online presence to mirror."** Rep can override but the warning is loud.

### Site lifecycle

- URL: `https://preview.gonenova.com/{slug}` (subdomain — reads real in SMS).
- TTL: 7 days from build time.
- After expiry: page auto-redirects to `https://preview.gonenova.com/expired` which says *"This preview expired. Reply to Tyler's text or call (404) 941-3398 to bring it back."* Urgency baked in.
- DB tracks `expires_at`; cron sweeps expired and replaces with redirect stub.
- If lead converts → manual promote to permanent subdomain or hand off to actual production deploy. Out of scope for v1.

---

## 9. Build Sequence (Phase 0, strict order)

| # | Task | Owner | Est | Gate |
|---|---|---|---|---|
| 1 | Curate inspiration corpus (60 refs) | Tyler | 4-6 hr | 60 refs in `inspiration/` with full metadata |
| 2 | Design system primitives + rules doc | Engineer | 0.5 day | `design_system/primitives.css` + `rules.md`; lints on a known-good build |
| 3 | Cold-start vision critic anchors | Tyler | 1 hr | 5 hand-scored anchor sites in `prompts/vision_critic_examples/` |
| 4 | Researcher agent end-to-end on 1 real lead | Engineer | 0.5 day | Validated `research_brief.json` with all sources present |
| 5 | Asset gatherer (GBP + palette extraction + FLUX fallback) | Engineer | 0.5 day | Pulls ≥ 4 real assets for a test plumber, falls back gracefully when none exist |
| 6 | Inspiration picker | Engineer | 0.25 day | Returns 3-5 ref IDs given a brief, enforces diversity vs last 5 builds |
| 7 | Builder writing freestyle HTML/CSS | Engineer | 1 day | Renders a complete site using only refs + design system + brief; no unsourced claims |
| 8 | Code critic (extend existing `iterate.py`) | Engineer | 0.5 day | Adds realness + consistency + originality checks |
| 9 | Vision critic with rubric | Engineer | 0.5 day | Returns rubric JSON; calibrated against 5 anchors |
| 10 | Technical gates (Lighthouse + HTML validate + responsive) | Engineer | 0.5 day | All deterministic; pass/fail logged |
| 11 | Orchestrator (budget, daily cap, dispatch, fallbacks) | Engineer | 1 day | One full build under $7 / 12 min on a real lead |
| 12 | Rep approval UI + calibration widget in dialer | Engineer | 0.5 day | Tyler can review, approve, rate 1-5 |
| 13 | Deployer + notifier | Engineer | 0.25 day | `preview.gonenova.com/<slug>` live with 7-day expiry; rep gets ping not lead |
| 14 | **First 10 real builds + feedback capture** | Tyler + Engineer | 1 day | 10 builds in DB with `feels_like_score` populated; corpus + critic adjusted from feedback |

**Total: ~7-8 focused days end-to-end. Do not skip step 14. The first 10 builds tell us whether the system works.**

---

## 10. Cost Model

Target: **$5-7 per successful build.**

| Phase | Provider | Typical cost |
|---|---|---|
| Researcher (Sonnet 4.6 + Brave + Outscraper) | Anthropic + Brave + Outscraper | $1.00 |
| Asset gatherer (Haiku + 1-2 FLUX if needed) | Anthropic + Replicate | $0.50 |
| Inspiration picker (Haiku) | Anthropic | $0.05 |
| Builder first draft (Sonnet) | Anthropic | $0.75 |
| Code critic (existing logic + Sonnet for must-fixes parsing) | Anthropic | $0.30 |
| Vision critic (Sonnet vision, 3 screenshots, 1-3 iterations) | Anthropic | $1.20 |
| Builder regenerations (Sonnet, 2-4 iter) | Anthropic | $1.00 |
| Technical gates (Lighthouse / htmlhint, local) | $0 | $0.00 |
| Deployer (Vercel API) | $0 (under quota) | $0.00 |
| Notifier (Twilio SMS to rep + lead) | Twilio | $0.02 |
| Misc API overhead / retries | various | $0.50 |
| **Subtotal** | | **$5.32** |
| **Buffer** | | $1.68 |
| **Cap** | | **$7.00** |

**Daily fleet cap: $100/day** (~14 builds at $7 each).

---

## 11. Known Unknowns

These are the assumptions the system rides on. If any prove wrong, the system breaks. Re-check after first 10 builds.

1. **Vision critic rubric calibration.** The 6 weighted axes + their weights are our best guess. They're untested. The monthly recalibration loop (§2) is the safety net — but until 50 builds with `feels_like_score` exist, the weights are unverified.

2. **Corpus quality assumption.** 60 hand-picked refs supposedly cover 80% of contractor verticals + vibes. We don't know this. First 10 builds will reveal gaps — likely whole verticals (e.g. roofing has nothing aspirational in the corpus) or vibe holes (e.g. no "modern woman-owned" reference).

3. **Freestyle builder consistency risk.** A Sonnet writing HTML/CSS from scratch every time CAN produce broken output. The technical gates (Lighthouse, responsive, HTML validate) are the floor. They are deterministic and will catch most failures — but not all. Watch for: weird focus states, JS that breaks on slow networks, accessibility regressions the deterministic tools miss.

4. **Lead-side rendering.** Sent via SMS, opened on a contractor's 4-year-old Android in a truck. Vision critic grades a Chrome-headless screenshot. Real-device rendering may differ. After build 10, sample 3 actual lead-device opens and compare to the critic's screenshots.

5. **"Feels like theirs" inter-rater consistency.** Only Tyler rates v1. If a second rep joins later, their 1-5 may diverge from Tyler's. Plan for shared rubric before adding a second rep.

6. **The 7-day TTL UX.** Untested. Some leads may take 8 days to circle back and find the site dead. Could backfire. Worth A/B testing after enough volume.

7. **GBP scrape reliability.** Outscraper is paid + ToS-clean but rate-limited and occasionally returns garbage for sparse listings. Failure mode is "research brief is thin" → builder defaults toward generic-feeling output. Mitigation: critic flags low-data builds.

8. **Brand color extraction quality.** k-means on a logo / van photo works for sharp logos. Falls apart on muddy photos or businesses with no logo. Fallback: industry-default palette neutral enough to feel correct (e.g. plumber → blue / steel / orange neutral). Track which builds used real vs fallback palette and check correlation with `feels_like_score`.

9. **The "fingerprint diversity" check is hand-wavy.** Comparing palette hashes + composition class + section sequence is a heuristic. May over-fire (rejecting genuinely different builds that happen to share a color) or under-fire (passing two near-clones with one color shift). Refine after data exists.

10. **Daily cap of $100 is a guess.** Could be too tight (we stop at 14 builds when Tyler wants 30) or too loose (a stuck loop burns $80 in one bad hour). Watch the spend pattern and tighten after week 1.

---

## 12. Out of Scope (Phase 1+, not v1)

These are tempting and should be cut from v1 even if "they'd be easy."

- **Owner age estimation from photos.** Cut. Biased + low-signal + risky.
- **Social scraping (FB / IG / LinkedIn).** Cut. ToS landmines, especially LinkedIn.
- **Easter-egg personality nods** (Star Wars, etc.). Cut. Real palette + real photos cover this implicitly.
- **24 templates.** Cut. We're freestyle now.
- **Auto-SMS to lead without rep approval.** Cut. Rep gate is the safety net.
- **A/B testing different templates per vertical.** Cut. Phase 3 after we have data.
- **Vision critic over the entire page scroll** (not just folds). Cut for v1. 3 width screenshots above-fold + below-fold is enough.
- **Self-improving prompts.** Cut. Calibration loop is manual monthly, not auto.
- **Multi-language sites.** Cut. English only v1.
- **Lead conversion → permanent production deploy.** Cut. Manual handoff in v1.
- **Live build progress streaming to the lead's phone.** Cut. Rep narrates progress during the call.
- **Vector logo regeneration when prospect logo is low-res.** Cut. Use as-is, note in build log.
- **Voice / TTS playback of pitch on the preview page.** Cut. Maybe never.

---

## Appendix A: System prompt scaffolds (cold-start versions, to be replaced after calibration)

### Researcher
> You are the Research agent for Tyler Brown's website-build pipeline. You have ≤ 2 minutes and a budget cap. Your job: produce a `research_brief.json` for {{business_name}} in {{city}}, GA. Every fact you record MUST have a `source` URL or it doesn't ship. No invented data. If a field can't be sourced, set it to null. Tools: brave_search, scrape_gbp, scrape_existing_site, extract_brand_palette. Return strict JSON matching the schema in build_agent/schemas/research_brief.schema.json.

### Builder
> You are the Builder for Tyler Brown's website-build pipeline. You have a research brief, 3-5 inspiration references, and a design system. Write a complete single-page HTML+CSS site for this prospect from scratch. Hard rules:
> - Only render facts present in the research brief.
> - Reference the inspiration refs' COMPOSITIONS and TREATMENTS — never copy code verbatim.
> - Follow the design system primitives. No arbitrary spacing. Max 3 colors. Max 2 type families.
> - Real prospect images only. If a slot has no real image, leave it imageless.
> - Mobile-first. Hero headline fits one line at 375px.
> - One CTA above the fold. Phone-number tap-to-call.
> Output: full HTML in one file, inline CSS or `<style>` block, no external dependencies except Google Fonts.

### Vision critic
> You are a senior design critic. You see three screenshots of a freshly-built website (mobile 375px, tablet 768px, desktop 1440px) plus the research brief and the inspiration refs the builder used. Score on six weighted axes per build_agent/SPEC.md §4. Output strict JSON. Be specific and actionable in must_fixes — never vague. Anchor examples: see build_agent/prompts/vision_critic_examples/*.md.

---

## Appendix B: Open questions Tyler must answer before step 1

1. **Domain configuration:** `preview.gonenova.com` — needs DNS pointed at Vercel. Tyler owns gonenova.com. Action: add wildcard subdomain to Vercel project.
2. **Vercel project for previews:** new project or reuse `atlanta-website-demos`? Recommendation: new project named `gonenova-previews` so demos repo stays for the cron worker.
3. **Outscraper monthly budget:** v1 will hit it harder than the cron worker did (every "B" press, not just daily batches). Confirm budget headroom or set a per-day Outscraper cap.
4. **FLUX vendor:** Replicate API was used by the demo pipeline. Confirm same provider + budget for v1.
5. **Lighthouse runner:** local headless Chrome (free, slow) or Vercel-hosted PageSpeed API (rate-limited, faster). Recommend local for v1.

---

*End of spec. Pick up step 1 in `Build Sequence (Phase 0)` when ready.*
