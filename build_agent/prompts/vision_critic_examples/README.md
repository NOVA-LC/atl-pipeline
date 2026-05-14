# Vision Critic Anchor Examples

Per SPEC §4: the vision critic's system prompt **must include 5 worked anchor
examples** before first real build. Without anchors, the critic drifts.

## What goes here

Five files, named `01-...md` through `05-...md`, each one a worked critic example:

```
01-rugged-plumber-great.md     ← score 9.2 — exemplar
02-modern-tech-mid.md          ← score 6.8 — "fine but generic"
03-family-legacy-bad.md        ← score 4.5 — template-feeling
04-aggressive-bold-mixed.md    ← score 7.4 — strong photography, weak type
05-quiet-confidence-edge.md    ← score 8.6 — exactly on the ship line
```

## Each file's structure

```markdown
# Anchor: <name> (final weighted: X.X)

## What the critic saw
- Mobile screenshot: <path or description>
- Tablet screenshot: ...
- Desktop screenshot: ...
- Brief excerpt: <business, vertical, vibe>
- Inspiration refs used by builder: [<id>, <id>, ...]

## Rubric
- Composition: X — <one-sentence reason>
- Type:        X — <one-sentence reason>
- Color:       X — <one-sentence reason>
- Photography: X — <one-sentence reason>
- Originality: X — <one-sentence reason>
- Craft:       X — <one-sentence reason>

## must_fixes
- ...

## Why this score (1-3 sentences for the critic's calibration)
- ...
```

## Who fills this in

Tyler. One sitting, ~1 hour. Pick 5 sites that span the score range so the critic
learns the full distribution.

## Then what

The vision critic loader concatenates all five `.md` files into its system prompt
at runtime. New anchors override old via filename ordering.

## Monthly recalibration

When the calibration loop (SPEC §2) flags drift between `feels_like_score` and
the critic's score, swap 1-2 anchors here for fresh ones from recent builds. Keep
total at 5 — the prompt context grows otherwise.
