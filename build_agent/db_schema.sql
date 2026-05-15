-- build_agent SQLite schema
-- Co-located with dialer.db on the Railway volume at /data.
-- Two tables: build_jobs (operational) + build_calibration (feedback engine).

-- ─── build_jobs ──────────────────────────────────────────────────────────────
-- One row per build attempt. Tracks cost + timing + outcome.
CREATE TABLE IF NOT EXISTS build_jobs (
    id              TEXT PRIMARY KEY,         -- UUID
    lead_id         TEXT NOT NULL,
    business_name   TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,     -- URL slug for preview.gonenova.com/<slug>
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP,
    status          TEXT NOT NULL DEFAULT 'queued',  -- queued|researching|building|critiquing|shipped|failed|build_unfit
    -- budget tracking
    budget_cap_usd       REAL NOT NULL DEFAULT 7.00,
    spend_actual_usd     REAL NOT NULL DEFAULT 0.0,
    spend_research_usd   REAL DEFAULT 0.0,
    spend_assets_usd     REAL DEFAULT 0.0,
    spend_inspiration_usd REAL DEFAULT 0.0,
    spend_builder_usd    REAL DEFAULT 0.0,
    spend_critic_code_usd REAL DEFAULT 0.0,
    spend_critic_vision_usd REAL DEFAULT 0.0,
    spend_other_usd      REAL DEFAULT 0.0,
    -- iteration tracking
    iterations           INTEGER NOT NULL DEFAULT 0,
    fallbacks_used       TEXT,                 -- JSON list of tool fallbacks triggered
    -- quality gates final scores
    code_score           REAL,
    vision_score         REAL,
    lighthouse_perf      INTEGER,
    lighthouse_a11y      INTEGER,
    html_valid           INTEGER,              -- 1 valid, 0 invalid
    responsive_ok        INTEGER,
    real_asset_ratio     REAL,
    -- inspiration refs used + fingerprint (for diversity)
    inspiration_ref_ids  TEXT,                 -- JSON list
    fingerprint          TEXT,                 -- JSON {palette_hash, hero_composition, section_sequence}
    -- shipped artifact
    preview_url          TEXT,
    expires_at           TIMESTAMP,
    -- failure flags
    ship_reason          TEXT,                 -- ok|budget_exhausted|time_exhausted|gates_failed|tool_failure
    error_summary        TEXT,
    -- rep gate
    rep_approved_at      TIMESTAMP,
    rep_approved_by      TEXT,
    lead_sms_sent_at     TIMESTAMP,
    lead_opened_url      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_build_jobs_lead    ON build_jobs(lead_id);
CREATE INDEX IF NOT EXISTS idx_build_jobs_status  ON build_jobs(status);
CREATE INDEX IF NOT EXISTS idx_build_jobs_started ON build_jobs(started_at);


-- ─── build_calibration ──────────────────────────────────────────────────────
-- The calibration loop's ground truth. One row per "feels like theirs" rating.
-- See SPEC §2 — this drives monthly critic recalibration.
CREATE TABLE IF NOT EXISTS build_calibration (
    build_id         TEXT PRIMARY KEY REFERENCES build_jobs(id),
    lead_id          TEXT NOT NULL,
    feels_like_score INTEGER NOT NULL CHECK(feels_like_score BETWEEN 1 AND 5),
    feels_like_note  TEXT,
    code_critic_score REAL,
    vision_critic_score REAL,
    inspiration_ref_ids TEXT,    -- JSON, copied from build_jobs at rating time
    rated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    rated_by         TEXT,
    -- lead outcome (filled in days/weeks later as we learn)
    outcome          TEXT,        -- booked|interested|ghosted|dnc
    outcome_at       TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calibration_score ON build_calibration(feels_like_score);
CREATE INDEX IF NOT EXISTS idx_calibration_lead  ON build_calibration(lead_id);


-- ─── daily_spend (lightweight cache for daily fleet cap) ────────────────────
-- Optional: compute from build_jobs on the fly. Materialized only if perf matters.
-- CREATE VIEW IF NOT EXISTS daily_spend AS
--   SELECT date(started_at) AS day, SUM(spend_actual_usd) AS total_usd
--   FROM build_jobs GROUP BY date(started_at);
