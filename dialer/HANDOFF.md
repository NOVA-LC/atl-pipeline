# Dialer Handoff

Tyler Brown's brain-off cold-call dialer. Built across Claude + Codex sessions, May 2026.

## What it is
A single-page Flask app at `localhost:5050` that:
- Pulls live leads from https://tyler-call-dashboard.vercel.app/ (93 ATL local businesses)
- Calls them via Twilio Voice JS SDK from `+1 404 941 3398` (caller ID = local Atlanta)
- Auto-advances on disconnect, with keyboard dispositions (1=no answer, 2=voicemail, 3=callback, 4=not interested, 5=interested, D=DNC, B=start-build, Esc=skip)
- Stores dispositions + notes locally in `dialer/dialer.db` (SQLite)
- Has an IVR Copilot stub (Deepgram transcription + intent parser → auto-DTMF) — wired but not active until deployed to a public URL
- Has a "they answered + interested" button (`B`) that triggers a background website build via the atl-pipeline generator

## Files
- `dialer/server.py` — Flask backend (1067 lines). Endpoints: `/`, `/leads`, `/token`, `/voice`, `/disposition`, `/note`, `/build`, `/build/status/<id>`, `/ivr/events`, `/media` (WebSocket), `/healthz`
- `dialer/index.html` — single-page UI (858 lines). Twilio Voice SDK + DTMF keypad + call events log + notes panel + queue with auto-expand
- `dialer/single.html` — legacy single-number test page (still works at `/single`)
- `dialer/dialer.db` — SQLite (dispositions, notes, build jobs, IVR events)

## How to run
```powershell
cd C:\Users\tyler\OneDrive\Desktop\atl-pipeline\dialer
python server.py
# Open http://localhost:5050
```

Twilio outbound calls also require the cloudflared tunnel running so Twilio can POST to `/voice`:
```powershell
& "C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --url http://localhost:5050
```
The current TwiML App (`AP43a2ef0674e92a733d927cc70fcd80f4`) points at `https://rules-thriller-truth-possibilities.trycloudflare.com/voice`. Cloudflare quick tunnels are ephemeral — if the URL changes, update the TwiML App's voice webhook via Twilio Console or REST.

## Env vars
Lives in `C:\Users\tyler\OneDrive\Desktop\claude code\.env`. Loaded by server.py via `load_dotenv(..., override=True)`.

Required:
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`
- `TWILIO_FROM_NUMBER=+14049413398`
- `TWILIO_TWIML_APP_SID=AP43a2ef0674e92a733d927cc70fcd80f4`

For IVR Copilot (not active until deployed):
- `DEEPGRAM_API_KEY` ✅ saved (needs rotation — leaked in chat)
- `ANTHROPIC_API_KEY` ✅ saved (needs rotation — leaked in chat)
- `IVR_COPILOT_MODE=suggest` (or `auto`)
- `DIALER_PUBLIC_BASE_URL` — must be public `https://` host with `wss://` support. Localhost can't accept Twilio Media Streams.

Optional:
- `DIALER_LEAD_SOURCE=call_dashboard` (default) | `mock` | `railway`
- `DIALER_ROOT_PAGE=queue` (default) | `single`
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — not wired yet; storage is local SQLite

## Architecture
```
Browser (localhost:5050)  ──HTTP──►  Flask server.py  ──SQLite──►  dialer.db
   │                                       │
   │  WebRTC                                │  REST  ──►  Twilio API
   │  (Twilio Voice SDK)                    │             (place outbound call)
   ▼                                        ▼
Twilio  ──POST /voice──►  Cloudflare tunnel ──►  Flask /voice (returns TwiML)
   │
   └──optional WSS /media (Media Streams)──►  Deepgram live STT ──►  IVR Copilot
```

Leads come from a public Vercel dashboard, not Supabase or Railway DB directly (that's the unfinished CRM integration).

## What's done
- ✅ Twilio browser calling — confirmed working end-to-end
- ✅ Lead source from live call dashboard (93 real ATL businesses)
- ✅ Deduplicated phones per lead
- ✅ Keyboard dispositions + auto-advance + pause
- ✅ Phone keypad (DTMF) during live calls
- ✅ Post-call "what happened?" prompt
- ✅ Call events log explaining every auto-decision
- ✅ Notes panel (email + callback time + free text) — persists locally
- ✅ NEPQ-style category-aware talking points
- ✅ "Start building website" button + background job (calls into atl-pipeline generator)
- ✅ IVR Copilot scaffolding (Deepgram WS bridge, intent parser, /ivr/events feed)

## What's NOT done (priority order)
1. **Supabase wiring** — notes/dispositions/callbacks live only in local SQLite. Tyler expected Supabase. Schema needs to be designed. Local DB should remain as offline fallback.
2. **Public deploy** — needed for IVR Copilot's WebSocket. Cloudflare quick tunnel works for `/voice` but isn't suitable for `wss:///media`. Recommend Railway (already has the pipeline service).
3. **Website-build quality** — the `/build` endpoint runs the existing atl-pipeline generator which produces template-level sites (~84/100 Awwwards). The Messiah-quality work in another branch hasn't been merged. The build button works but output isn't yet "agency-quality" without the iterate.py loop being plugged in.
4. **API Key Secret rotation** — Deepgram + Anthropic keys were pasted in chat. Rotate after stabilization.
5. **Ephemeral cloudflared tunnel** — every restart changes the URL, breaks Twilio webhook. Either: (a) deploy to a stable host, (b) use named Cloudflare tunnel, or (c) accept manual TwiML App update each restart.
6. **Tyler's Vercel-deploy ask** — see "Vercel question" below.

## Vercel question (Tyler asked: "deploy with vercel")
**Vercel is the wrong host for this app as-is.** Two blockers:
- Flask is stateful (long-lived process, in-memory IVR event queue, persistent SQLite). Vercel Functions are stateless and time-limited.
- IVR Copilot needs a long-lived `wss://` WebSocket for Twilio Media Streams. Vercel Functions don't support persistent WebSockets.

**Right options:**
- **Railway** (recommended) — already used for atl-pipeline. Long-running process + volume for SQLite. ~$5–10/mo. Codex has the Railway agent tooling installed.
- **Fly.io / Render / DigitalOcean App Platform** — same shape.
- **Hybrid** — keep Flask on Railway, deploy a static dashboard read-only mirror on Vercel. Probably not worth the complexity.

If Tyler insists on Vercel, the path is: rewrite endpoints as Vercel Functions, move SQLite → Supabase/Vercel Postgres, host Media Streams WS on a separate service (Railway/Cloudflare Workers/AWS API Gateway). 1–2 days of work, not 10 minutes.

## Live state right now (verified at handoff time)
```
healthz:
  from:           +14049413398
  twiml_app:      ✅ AP43a2ef0674e92a733d927cc70fcd80f4
  lead_source:    call_dashboard (93 dialable leads)
  root_page:      queue
  ivr_copilot:
    deepgram_key: ❌ (env not picked up — verify ANTHROPIC_API_KEY / DEEPGRAM_API_KEY loaded)
    stream_url:   ❌ (no DIALER_PUBLIC_BASE_URL set)
    websocket:    ✅ supported
    mode:         suggest
```

The Deepgram key was saved to .env but server reports `deepgram_key: false`. Either restart server to pick up new env, or there's a key-name mismatch in server.py — check.

## Quick next-session checklist
1. Restart Flask, confirm `/healthz` shows `deepgram_key: true`
2. Deploy to Railway: copy `dialer/` into a service, set env, mount volume for `dialer.db`. Set `DIALER_PUBLIC_BASE_URL=https://<railway-domain>`. Update TwiML App voice URL to the Railway URL.
3. Wire Supabase: create `leads` (read-only mirror), `dispositions`, `notes`, `callbacks`, `build_jobs` tables. Swap writes in server.py from `_db()` to Supabase REST.
4. Plug Messiah-quality `iterate.py` loop into the `/build` endpoint so "Interested + start building" produces a 90+/100 site.
5. Rotate Deepgram + Anthropic + Vercel tokens leaked in chat.

## Contact / source-of-truth
Lead dashboard: https://tyler-call-dashboard.vercel.app/
Pipeline repo: github.com/NOVA-LC/atl-pipeline (branch where dialer was built: `claude/atl-pipeline-setup-hdYeK`)
Twilio Console: https://console.twilio.com — TwiML App `AP43a2ef…`, From number `+1 404 941 3398`
