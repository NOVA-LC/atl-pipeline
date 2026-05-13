# Lead enrichment scripts

Helpers that run **outside** the Claude Code sandbox (because their target
sites bot-block) but plug into the same lead-list manifest format.

## `scrape_ga_ecorp.py` — Owner-name lookup via Georgia Corporations Division

Free, ~85% owner-name yield on legitimately-registered Georgia LLCs.
Per-lead cost: $0. ~5 min runtime for 100 leads.

The Georgia Corporations Division (ecorp.sos.ga.gov) is the highest-yield
source for owner first+last name on Atlanta-area trade businesses. The
site is bot-blocked from server-side fetches (403) and not Google-indexed,
so you need a real headless browser to scrape it. Playwright is the
cleanest option.

### Setup (one-time)

```bash
pip install playwright openpyxl
playwright install chromium
```

### Run

```bash
python scripts/scrape_ga_ecorp.py \
    --xlsx /path/to/outscraper-export.xlsx \
    --out  /tmp/ga-ecorp-results.json
```

Optional flags:
- `--limit N` — process only the first N leads (smoke test)
- `--start N` — skip first N (resume from middle)
- `--slug-only foo` — only businesses whose name contains "foo"
- `--headed` — show the browser window (default headless)
- `--throttle-ms 1500` — delay between lookups (default 1.5 sec; don't go
  lower than 1000, the portal will start rate-limiting)
- `--resume` — skip leads already in the output file

### Output

JSON manifest, one entry per lead:

```json
{
  "lead_name": "Joe's Plumbing & Drain",
  "lead_city": "Marietta",
  "lead_phone": "+1 770-555-0184",
  "match_found": true,
  "match_name": "JOES PLUMBING AND DRAIN LLC",
  "match_id": "21043567",
  "match_status": "Active/Compliance",
  "principal_address": "1840 Roswell St, Marietta, GA 30062",
  "officers": [
    {"name": "Joseph Calloway", "title": "CEO"},
    {"name": "Joseph Calloway", "title": "Registered Agent"}
  ],
  "owner_first": "Joseph",
  "owner_last": "Calloway",
  "confidence": "high",
  "error": null
}
```

### Confidence buckets

- `high`     — single officer across all roles OR officer with a clear
               ownership title (CEO/President/Sole Member). ~70% of matches.
- `medium`   — multiple officers, picked by title priority. ~15% of matches.
- `low`      — only Registered Agent surfaced (may or may not be the
               owner — often a CPA/lawyer instead). Flag for manual check.
- `none`     — no person-shaped name found / no eCorp match at all.

### Known gotchas

- Many GA LLCs register under "[Owner Name] LLC" or "[Initials] Holdings
  LLC" different from the GBP display name. Fuzzy token-match catches most.
  If the script returns `match_found: false` for a business you KNOW is
  registered, re-run with `--headed` and search by partial name manually.
- The registered agent is the OWNER ~70-85% of the time for sub-$1M shops
  but is sometimes a corporate registered-agent service (Incfile, LegalZoom,
  Cogency, CSC). The script's blacklist filters those out — they appear in
  `officers[]` but won't be picked as `owner_first/last`.
- eCorp doesn't surface emails. For email enrichment, use Outscraper's
  Emails & Contacts Scraper as a separate pass (and accept that for
  businesses without web/social presence, yield is near zero — cold-call
  ask remains the highest-yield long-tail).

### Merge with the build pipeline

The output JSON keys (`lead_name`, `owner_first`, `owner_last`) match the
`research_brief.owner.{name|first}` fields the v4 pipeline's `compose` and
shell templates expect. Once you have results, point `batch_build.py` at
both files and merge before building so the rendered sites carry the
real owner name in every CTA and footer.
