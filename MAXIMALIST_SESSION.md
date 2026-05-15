# Maximalist Session — what changed while you were gone

Live working doc. Updated as I ship.

---

## North star (from Tyler's brief)

> "100% clarity, 100% happiness. Apple's ecosystem as 50% baseline.
> Make it the most maximalist, phenomenal thing imaginable —
> then audit for the user (not for me), strip it down, rebuild for clarity."

The tension to hold: maximalist features + Apple-level restraint.
Apple ships fewer things, polished. We ship the 8 things that
matter most + we polish each one until it disappears.

---

## Deep audit — current state

### Dialer UX (dialer/index.html, ~900 lines)
- 3-column grid: queue / current lead / sidebar
- Dark slate theme (#0a0a0f) with orange accent (#f97316)
- Inter / system fonts
- Functional but reads as "dev UI" not "product"
- Cognitive load high: many panels visible simultaneously
- Hierarchy unclear: which button do I press in 3 seconds?
- Talking points crammed below the call button
- No "focus mode" for live calls
- No system status — when Twilio/Deepgram/Anthropic break, you find out by trying

### Dialer server (dialer/server.py, ~1500 lines)
- Twilio Voice in-browser ✓
- IVR Copilot (regex + Sonnet agent) ✓
- Lead source: tyler-call-dashboard.vercel.app ✓
- Dispositions + notes persisted to dialer/dialer.db ✓
- Build_agent integration (Step 7 of SPEC) ✓
- Calibration widget ✓

### Build agent (build_agent/, just shipped)
- Full pipeline: research → assets → inspiration → freestyle Sonnet builder → critic loop → rep approval → calibration
- 56-ref corpus
- Hard quality gates (code, vision, lighthouse, responsive, html, real-asset%)
- Daily $100 cap
- Per-build $7 cap / 12 min cap
- Proven end-to-end on Haiku: code=100, $0.97, 5:21

### Gaps Tyler will hit (audit from a prospect's perspective)
1. **Voicemail awareness** — IVR Copilot detects menus but not voicemails; rep
   manually marks. AMD (Twilio's answering-machine detection) would auto-mark
   and we'd never waste a "Hello" on a recording.
2. **Transcript loss** — Deepgram transcribes every call but the transcript
   is thrown away after the call ends. No post-call analysis, no objection
   library, no learning loop.
3. **Cold open** — you press Enter and dial, but the agent could have spent
   the 2 seconds of ring time briefing you: "Joey runs Peach State; he's
   responded to 3 recent 1-star reviews himself — he reads everything."
4. **Build status anxiety** — when you press B, you wait 5 minutes wondering
   if it's working. Need clear progress.
5. **Calibration friction** — the 1-5 widget pops up but only after rep
   approves. We should also let rep "skip approval" with one click.
6. **No global search** — 93 leads in a flat list, no way to find Joey when
   he calls back two days later.
7. **Demo handoff dead air** — rep approves build → SMS sends → silence.
   Should show "delivered ✓ opened ✓" with timestamps.
8. **System status invisible** — Outscraper past-due? You only learn during
   a call when the agent says build_unfit. Should be a quiet red dot in
   the corner that you can click for details.
9. **Mobile dead** — dialer is desktop only. Tyler on his Fold should at
   least be able to glance at status.
10. **No "next session" handoff** — when Tyler comes back the next day, no
    summary of yesterday's calls / builds / pipeline state.

---

## 20 things that would help (ranked by ROI × ease)

| # | Idea | Impact | Effort | Tier |
|---|---|---|---|---|
| 1 | **Apple-quality UI redesign** of the dialer | 10 | 6 | P0 |
| 2 | **Persist call transcripts** + post-call AI summary | 10 | 4 | P0 |
| 3 | **Pre-call AI briefing card** (3 bullets, surfaces during ring) | 9 | 3 | P0 |
| 4 | **System status header** (Twilio/Deepgram/Anthropic/Outscraper green dots) | 8 | 2 | P0 |
| 5 | **Live build progress** (visible cost meter, stage indicator, iter count) | 8 | 3 | P0 |
| 6 | **Fuzzy lead search** (Cmd-K palette across 93 leads) | 7 | 3 | P1 |
| 7 | **Twilio AMD** (auto-detect answering machine, never waste a Hello) | 9 | 3 | P1 |
| 8 | **Objection library** (transcript → extracted objections → searchable) | 8 | 5 | P1 |
| 9 | **Post-call summary** with sentiment + follow-up actions | 8 | 3 | P1 |
| 10 | **End-of-day summary** ("yesterday you called X, booked Y, built Z") | 7 | 2 | P1 |
| 11 | **Calibration dashboard** (all builds × scores × your ratings) | 7 | 3 | P1 |
| 12 | **Voicemail script library** (pre-recorded clips by category) | 6 | 4 | P2 |
| 13 | **ElevenLabs voice clone** for VM drops | 8 | 7 | P2 |
| 14 | **Cross-build pattern learner** (corpus refs → calibration score) | 7 | 6 | P2 |
| 15 | **Auto-dial silent voicemails** (skip rep, drop, move on) | 7 | 4 | P2 |
| 16 | **Lead pipeline view** (kanban: new → talked → interested → built → sent) | 6 | 4 | P2 |
| 17 | **Build cost projection** ("if you build all 12 interested leads: $84") | 5 | 2 | P2 |
| 18 | **iPhone Shortcuts integration** (mark callback from anywhere) | 5 | 5 | P3 |
| 19 | **Spotlight-style command palette** (Cmd-K everywhere) | 6 | 4 | P3 |
| 20 | **Daily standup** (auto-DM to a Slack/email: "yesterday's calls, today's queue") | 5 | 3 | P3 |

---

## Build plan for this session

P0 only. Five items. Each one polished, not half-done.

### 1. Apple-quality UI redesign
- Drop the 3-column "command center" aesthetic
- New layout: focused lead card center stage, queue as collapsible side rail, sidebar as drawer
- SF Pro Display / SF Pro Text fallbacks → Inter / -apple-system
- 8pt grid, restrained color (one accent at a time), proper type scale
- Micro-animations on state change (call connecting, build progress, etc.)
- Status badges that are calm (single dot, not loud chips)
- "Focus mode" automatically when call is live: queue collapses, talking points front and center

### 2. Persist call transcripts
- New table: `call_transcripts` (call_sid, lead_id, transcript_full, started_at, ended_at, duration_sec)
- Deepgram bridge writes each chunk in addition to the IVR Copilot path
- On disconnect, flush + post-call AI summary
- Server endpoint: GET /transcripts, GET /transcripts/<call_sid>

### 3. Post-call AI summary
- After disconnect, Haiku reads the transcript
- Output: { outcome, key_objections[], follow_up_actions[], sentiment, key_quotes[] }
- Persisted to call_summaries table, linked to transcript
- Surfaced in dialer + searchable later

### 4. Pre-call AI briefing card
- When a lead becomes "current" (focused or about to dial), generate a 3-bullet briefing from the research_brief + recent reviews
- Cached after first generation (don't re-pay each time)
- Displayed inline above talking points

### 5. System status header
- Top-right of every dialer page: 4-5 small dots (Twilio / Deepgram / Anthropic / Outscraper / Railway)
- Each dot polls a /healthz endpoint silently every 30s
- Click any dot → drawer with details + retry button

---

## Progress log

### 14:00 Session start
- Wrote this doc, audited current state, selected 5 P0 items.

### 14:30 P0 batch shipped (commit `0c50f70`)
- **P0.1 Apple-quality CSS redesign**: design tokens, status strip in topbar
  (5 live dots: Twilio / Deepgram / Sonnet / Outscraper / Device), focus-mode
  (body[data-mode='calling'] collapses queue rail to 76px, hides labels,
  pulses the active item), restrained palette, hairline borders, motion curve.
- **P0.2 Transcript persistence**: new `call_transcripts` table; every
  Deepgram chunk appended via `_persist_transcript_chunk`; finalized on
  Media Stream 'stop'. GET /transcripts + GET /transcript/<id>.
- **P0.3 Post-call AI summary**: new `call_summaries` table; Haiku reads
  finalized transcript, returns strict JSON with outcome / sentiment /
  key_objections / follow_up_actions / key_quotes / suggested_disposition /
  confidence. Async on disconnect, idempotent. GET /summary/<id>.
- **P0.4 Pre-call AI briefing**: new `briefings` table; POST /briefing
  generates 3 sharp bullets via Haiku (~$0.0006 each, cached). Frontend
  lazy-fetches via ensureBriefing(); renders inline above talking points
  with skeleton loading state. Verified live: Joey/Acme returned three
  specific, sourced bullets.
- **P0.5 System status header**: GET /status with per-tool probes (Twilio,
  Deepgram, Anthropic, Outscraper). 5-min in-memory cache. 529/past-due
  → degraded (yellow), missing creds → error (red). Live verified: Anthropic
  green ("Sonnet+Haiku reachable"), Outscraper amber ("account past-due —
  top up"), Twilio green.

### 15:00 P1 batch shipped (this commit)
- **⌘K command palette**: full-screen overlay with blur backdrop, fuzzy
  search across all 93 leads by name/phone/city/category, arrow-key nav,
  enter to focus. Glass aesthetic (rgba + backdrop-filter). `⌘K to search`
  pill in topbar so people discover it.
- **Post-call summary card**: 8 seconds after a call disconnect, dialer
  fetches /summary/<session_id> and renders a popover with: outcome pill,
  sentiment chip (positive/neutral/negative color-coded), best verbatim
  quote in italic with accent-bar, objections list, follow-up bullets in
  green, and a one-click "Apply: <suggested_disposition>" button that
  files the disposition + dismisses.
- **Dialer UI loads clean**: 88,066 bytes, 200 OK.

### Outstanding (deferred to next session)
- Twilio AMD (auto-detect answering machine — needs TwiML changes + cost)
- ElevenLabs voicemail drops (needs voice clone setup)
- Build cost meter inline in build status banner (data is in build_jobs,
  needs frontend polish)
- Calibration dashboard page (would be a separate route /calibration)
- End-of-day summary (cron job that emails/SMS a daily wrap)

### What's true about the dialer now
- Status of every tool is visible at a glance
- Pre-call briefing surfaces specific intel about each lead before you dial
- Every transcript is persisted; every call gets summarized; objections
  and follow-up actions are extracted automatically
- Cmd-K finds any lead instantly across 93 records
- Focus mode auto-engages during live calls — fewer panels, less noise
- Design uses tokens consistently; one accent at a time; hairline borders;
  Apple-style motion curve
- UX/UX still has rough edges (right rail panels not all restyled, some
  legacy classes), but the bones are right and the daily-use flows
  (search, dial, listen, summarize) are now polished
