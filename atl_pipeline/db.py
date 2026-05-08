"""SQLite store for pipeline state. Idempotent + resumable.

DB path is configurable via PIPELINE_DB_PATH env var. On Railway we mount a
1GB volume at /data and set PIPELINE_DB_PATH=/data/pipeline.db so state
persists across deploys. Locally it defaults to ./atl_pipeline.db.
"""
import os
import sqlite3
import json
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get('PIPELINE_DB_PATH', 'atl_pipeline.db'))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id        TEXT UNIQUE NOT NULL,
    business_name   TEXT NOT NULL,
    category        TEXT,
    city            TEXT,
    state           TEXT,
    phone           TEXT,
    email           TEXT,
    address         TEXT,
    rating          REAL,
    reviews         INTEGER,
    google_maps_url TEXT,
    raw_outscraper  TEXT,                       -- json blob
    -- pipeline state
    verify_status   TEXT,                       -- pending | yes | no | unsure | likely | duplicate
    verify_payload  TEXT,                       -- json
    verify_email_payload TEXT,                  -- json: result of email_verify.verify()
    research_status TEXT,                       -- pending | done | failed
    research_payload TEXT,                      -- json: owner, brand_colors, hooks, etc.
    slug            TEXT UNIQUE,                -- subfolder name in demos repo
    demo_html       TEXT,                       -- generated html cache
    vercel_project  TEXT,
    vercel_url      TEXT,
    email1_sent_at  TIMESTAMP,
    email1_resend_id TEXT,
    email2_sent_at  TIMESTAMP,
    email2_resend_id TEXT,
    email3_sent_at  TIMESTAMP,
    email3_resend_id TEXT,
    replied         INTEGER DEFAULT 0,
    do_not_contact  INTEGER DEFAULT 0,           -- set when prospect unsubscribes; never email again
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_verify ON leads(verify_status);
CREATE INDEX IF NOT EXISTS idx_research ON leads(research_status);
CREATE INDEX IF NOT EXISTS idx_email1 ON leads(email1_sent_at);
CREATE INDEX IF NOT EXISTS idx_dnc ON leads(do_not_contact);

CREATE TABLE IF NOT EXISTS blog_posts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT UNIQUE,
    lead_id       INTEGER,
    title         TEXT,
    body_md       TEXT,
    published_at  TIMESTAMP,
    gonenova_path TEXT,
    FOREIGN KEY(lead_id) REFERENCES leads(id)
);
"""

def _migrate(c):
    """Idempotent column adds for existing DBs that predate new fields."""
    cols = {row[1] for row in c.execute("PRAGMA table_info(leads)").fetchall()}
    if 'do_not_contact' not in cols:
        c.execute('ALTER TABLE leads ADD COLUMN do_not_contact INTEGER DEFAULT 0')


@contextmanager
def conn(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.executescript(SCHEMA)
    _migrate(c)
    try:
        yield c
        c.commit()
    finally:
        c.close()

def upsert_lead(c, place_id, **fields):
    """Insert if new, update if exists. Returns lead_id."""
    cols = ['place_id'] + list(fields.keys())
    vals = [place_id] + list(fields.values())
    placeholders = ','.join('?' * len(cols))
    setters = ','.join(f'{k}=excluded.{k}' for k in fields.keys())
    sql = f"""
    INSERT INTO leads ({','.join(cols)}) VALUES ({placeholders})
    ON CONFLICT(place_id) DO UPDATE SET {setters}, updated_at=CURRENT_TIMESTAMP
    """
    c.execute(sql, vals)
    row = c.execute('SELECT id FROM leads WHERE place_id = ?', (place_id,)).fetchone()
    return row['id']

def update_lead(c, lead_id, **fields):
    if not fields:
        return
    sets = ','.join(f'{k}=?' for k in fields.keys())
    vals = list(fields.values()) + [lead_id]
    c.execute(f'UPDATE leads SET {sets}, updated_at=CURRENT_TIMESTAMP WHERE id=?', vals)

def get_lead(c, lead_id):
    return c.execute('SELECT * FROM leads WHERE id=?', (lead_id,)).fetchone()

def leads_pending(c, stage):
    """stage in {'verify','research','demo','deploy','email1','email2','email3'}"""
    if stage == 'verify':
        return c.execute("SELECT * FROM leads WHERE verify_status IS NULL OR verify_status = 'pending'").fetchall()
    if stage == 'research':
        return c.execute("SELECT * FROM leads WHERE verify_status IN ('no','unsure') AND (research_status IS NULL OR research_status = 'pending')").fetchall()
    if stage == 'demo':
        return c.execute("SELECT * FROM leads WHERE research_status='done' AND demo_html IS NULL").fetchall()
    if stage == 'deploy':
        return c.execute("SELECT * FROM leads WHERE demo_html IS NOT NULL AND vercel_url IS NULL").fetchall()
    if stage == 'email1':
        return c.execute("""SELECT * FROM leads WHERE vercel_url IS NOT NULL AND email IS NOT NULL AND email != ''
                            AND email1_sent_at IS NULL AND do_not_contact = 0""").fetchall()
    if stage == 'email2':
        return c.execute("""SELECT * FROM leads WHERE email1_sent_at < datetime('now','-3 days')
                            AND email2_sent_at IS NULL AND replied = 0 AND do_not_contact = 0""").fetchall()
    if stage == 'email3':
        return c.execute("""SELECT * FROM leads WHERE email2_sent_at < datetime('now','-4 days')
                            AND email3_sent_at IS NULL AND replied = 0 AND do_not_contact = 0""").fetchall()
    raise ValueError(f'unknown stage: {stage}')
