# Vision Critic Anchor Scoring — Tyler's Calibration

Per SPEC §4: the vision critic uses 5 anchor examples to know what good/mid/bad
looks like for **this specific business** (cold-call website builds for ATL
contractors). Until you fill these in, the critic is hill-climbing on its own
prompt.

This is the only step in the whole system I can't automate — your taste is
the thing being calibrated. Plan on ~1 hour. Open each URL on your laptop
(not phone — the critic grades desktop screenshots, and you should see what
it sees).

## How to use this file

For each of the 5 anchor sites below:

1. Open the URL.
2. Score each of the 6 axes from 1–10 (10 = best in class for a contractor demo).
3. Write one specific sentence per axis explaining WHY you scored it that way.
   Concrete > abstract. *"The hero photo crops out the truck's license number"*
   beats *"hero photo is weak"*.
4. Write the FINAL weighted (I'll compute it from your axis scores).
5. List 2-3 must_fixes you'd give if you had to rebuild it.

When done, save the file and tell me — I auto-convert your fills into the
5 anchor `.md` files the vision critic loads at runtime.

---

## The 5 candidates (intentional spread)

I picked these to span the score range. **Do not adjust scores to match my
ordering** — if you think #1 should be a 4 and #5 should be a 9, that IS
the calibration data we need.

### 1. Premium-craftsman tier
**URL:** https://www.marvelltileandstone.com/
**Why I'm showing it:** This is one of the inspiration corpus refs
(awwwards-marvell-tile-stone). Full-bleed material photography, restrained type,
quiet luxury aesthetic. Closest thing to "what we're aiming for at the top end."

```yaml
composition:  { score: ?, reason: "" }
type:         { score: ?, reason: "" }
color:        { score: ?, reason: "" }
photography:  { score: ?, reason: "" }
originality:  { score: ?, reason: "" }
craft:        { score: ?, reason: "" }
final_weighted: ?     # 0.25*orig + 0.20*comp + 0.15*type + 0.15*color + 0.15*photo + 0.10*craft
must_fixes:
  - ""
strengths:
  - ""
```

### 2. Rugged-trade tier
**URL:** https://www.thiswasmajor.com/
**Why:** Trade-focused, documentary photography, asymmetric hero — closer to
the "Peach State Plumbing" type build we'd actually produce. The realistic
"great" we're aiming for in a normal day.

```yaml
composition:  { score: ?, reason: "" }
type:         { score: ?, reason: "" }
color:        { score: ?, reason: "" }
photography:  { score: ?, reason: "" }
originality:  { score: ?, reason: "" }
craft:        { score: ?, reason: "" }
final_weighted: ?
must_fixes:
  - ""
strengths:
  - ""
```

### 3. Mid-tier real plumber (typical-good)
**URL:** https://www.dahmplumbing.com/
**Why:** Real Atlanta-area plumber, decent template, photos of trucks + team,
clear CTAs, but no real composition or typographic personality. The "fine,
won't lose us a deal" middle.

```yaml
composition:  { score: ?, reason: "" }
type:         { score: ?, reason: "" }
color:        { score: ?, reason: "" }
photography:  { score: ?, reason: "" }
originality:  { score: ?, reason: "" }
craft:        { score: ?, reason: "" }
final_weighted: ?
must_fixes:
  - ""
strengths:
  - ""
```

### 4. Template/generic tier
**URL:** https://www.serviceexpertsplumbing.com/
**Why:** Big national chain. Looks "professional" but template-y, stock
photography vibes, blue gradient backgrounds, no personality. The trap our
agent might fall into.

```yaml
composition:  { score: ?, reason: "" }
type:         { score: ?, reason: "" }
color:        { score: ?, reason: "" }
photography:  { score: ?, reason: "" }
originality:  { score: ?, reason: "" }
craft:        { score: ?, reason: "" }
final_weighted: ?
must_fixes:
  - ""
strengths:
  - ""
```

### 5. Genuinely bad
**URL:** https://www.bullsupplyinc.com/
**Why:** Late-1990s aesthetic, cluttered, low contrast, no real hierarchy,
Comic Sans-adjacent. Defines the floor of "what we will never ship."

```yaml
composition:  { score: ?, reason: "" }
type:         { score: ?, reason: "" }
color:        { score: ?, reason: "" }
photography:  { score: ?, reason: "" }
originality:  { score: ?, reason: "" }
craft:        { score: ?, reason: "" }
final_weighted: ?
must_fixes:
  - ""
strengths:
  - ""
```

---

## What happens after you fill this in

When you save this file with all scores filled, I run a small script that:

1. Captures Playwright screenshots of each URL at 375/768/1440 widths
   (so the critic prompt has the same visual data you scored).
2. Builds 5 anchor markdown files in this directory (one per candidate).
3. The vision critic system prompt includes those anchors verbatim from now on.
4. Future builds get scored against YOUR taste, not the cold-start guess.

If you want to swap a URL (any of the 5 above doesn't fit), do it — just
update the URL line and score the replacement. I'll handle the rest.

If you can't open one (404, dead site), tell me which one and I'll swap it.

## When to recalibrate

Per SPEC §2 (Calibration Loop): monthly, pull the 5 worst-rated + 5 best-rated
real builds. If the vision critic's scores correlate poorly with your 1–5
"feels like theirs" ratings, swap 1–2 anchors here for fresh real builds.
Keep the total at 5.
