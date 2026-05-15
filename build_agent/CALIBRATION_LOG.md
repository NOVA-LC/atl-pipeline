# Calibration Log — first 10 real builds

Per SPEC §2 / §8 (Step 8). Each row is a real build with Tyler dialing,
pressing B, reviewing, optionally approving, and rating 1–5.

This file is the source of truth for the **monthly critic recalibration loop**.
Without these 10 ratings, every prompt change is hill-climbing on a proxy
(Awwwards-style code score + the cold-start vision rubric).

---

## How to log a build

After each real build, fill one row below. Pull the data from:
- `build_agent/_data/build_agent.db` → `build_jobs` and `build_calibration` tables
- `build_agent/_data/builds/<slug>/index.html` for the artifact
- `build_agent/_data/builds/<slug>/verdict.json` for the critic verdict

---

## Builds

| # | Date | Business | Vertical | Code | Vision | Real-asset% | feels_like_theirs (1-5) | Note |
|---|------|----------|----------|------|--------|-------------|------------------------|------|
| 1 | TBD  |          |          |      |        |             |                        |      |
| 2 | TBD  |          |          |      |        |             |                        |      |
| 3 | TBD  |          |          |      |        |             |                        |      |
| 4 | TBD  |          |          |      |        |             |                        |      |
| 5 | TBD  |          |          |      |        |             |                        |      |
| 6 | TBD  |          |          |      |        |             |                        |      |
| 7 | TBD  |          |          |      |        |             |                        |      |
| 8 | TBD  |          |          |      |        |             |                        |      |
| 9 | TBD  |          |          |      |        |             |                        |      |
| 10| TBD  |          |          |      |        |             |                        |      |

---

## After 10 builds — required analysis

### Inspiration refs that performed well (feels_like_score ≥ 4)
- (list ref IDs + count)

### Inspiration refs that underperformed (feels_like_score ≤ 2)
- (list ref IDs + count → these are corpus quality issues, candidates for removal)

### Verticals that work
- (which industries averaged ≥ 4?)

### Verticals that fail
- (which industries averaged ≤ 2.5? — corpus + prompt work needed)

### Top 3 prompt edits suggested by the data
1. (e.g. "Builder over-uses serif fonts on aggressive verticals — add a hard rule")
2. (e.g. "Code critic doesn't catch X — add a check")
3. (e.g. "Vision critic rates Originality too generously — recalibrate anchors")

### SPEC §11 (Known Unknowns) updates
- Mark which assumptions held vs broke after 10 real builds.
- Update the SPEC with revised confidence levels.

---

## Hard rules during calibration

1. **No prompt tuning between builds to chase scores.** Bug fixes are fine
   (the empty `href="tel:"` bug, the missing `<!DOCTYPE>` bug). Improvements
   based on patterns in the data wait for the log review.
2. **Rate every build, even the bad ones.** Especially the bad ones.
3. **The 1–5 widget pops up automatically** after rep approval. Don't dismiss
   — it's the feedback loop's only data source.
4. **One rater for v1 (Tyler).** Adding a second rater later requires a
   shared rubric to keep scores comparable.
