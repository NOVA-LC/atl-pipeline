"""Scan tyler@gonenova.com inbox via IMAP for unsubscribes + replies, mark DB.

Two signals to catch:
  1. UNSUBSCRIBES — Gmail/Outlook one-click sends a mailto to
     tyler+unsub-{lead_id}@gonenova.com. We grep for "+unsub-" in the To
     header, parse the lead_id, and set do_not_contact=1.

  2. REPLIES — when a prospect replies to a Day-1 / Day-3 email, the From
     address matches a lead.email we sent to. We mark replied=1 so the
     follow-up sequence stops.

Setup:
  1. Tyler generates a Gmail App Password at:
     https://myaccount.google.com/apppasswords  (requires 2FA enabled)
  2. Add to Railway env: GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
  3. The IMAP user is whatever RESEND_FROM_EMAIL is.

Why IMAP not Gmail API: zero OAuth setup, no Google Cloud Console project,
imaplib is in stdlib. App password is the only credential.
"""
import os
import re
import imaplib
import email as _emailmod
from email.header import decode_header
from datetime import datetime, timedelta

from . import db


IMAP_HOST = 'imap.gmail.com'
SCAN_DAYS = 14   # only look at recent mail


def _decode(s):
    """Decode RFC 2047 encoded headers (e.g. =?UTF-8?B?...?=)."""
    if not s:
        return ''
    parts = decode_header(s)
    out = ''
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out += txt.decode(enc or 'utf-8', errors='replace')
            except LookupError:
                out += txt.decode('utf-8', errors='replace')
        else:
            out += txt
    return out


def _imap_connect():
    user = os.environ.get('RESEND_FROM_EMAIL')
    password = os.environ.get('GMAIL_APP_PASSWORD')
    if not (user and password):
        return None
    try:
        M = imaplib.IMAP4_SSL(IMAP_HOST)
        M.login(user, password.replace(' ', ''))   # Google formats with spaces; IMAP wants no spaces
        M.select('INBOX', readonly=True)
        return M
    except Exception as e:
        print(f'  ! IMAP connect failed: {e}')
        return None


def _since_str(days=SCAN_DAYS):
    """IMAP SINCE wants 01-Jan-2026 format."""
    return (datetime.utcnow() - timedelta(days=days)).strftime('%d-%b-%Y')


def scan_unsubscribes(M):
    """Find emails sent to tyler+unsub-{lead_id}@... and mark do_not_contact=1.

    Returns count of newly-unsubscribed leads.
    """
    user = os.environ.get('RESEND_FROM_EMAIL', '')
    local = user.split('@')[0] if '@' in user else 'tyler'
    domain = user.split('@')[1] if '@' in user else 'gonenova.com'

    # IMAP SEARCH on the To header. Gmail+aliasing means tyler+unsub-N@domain hits this inbox.
    typ, data = M.search(None, 'TO', f'"{local}+unsub-"', 'SINCE', _since_str())
    if typ != 'OK':
        return 0
    msg_ids = data[0].split() if data and data[0] else []
    if not msg_ids:
        return 0

    marked = 0
    for mid in msg_ids:
        typ, raw = M.fetch(mid, '(BODY.PEEK[HEADER.FIELDS (TO SUBJECT FROM)])')
        if typ != 'OK' or not raw or not raw[0]:
            continue
        headers = raw[0][1].decode('utf-8', errors='replace') if isinstance(raw[0][1], bytes) else str(raw[0][1])
        m = re.search(rf'{re.escape(local)}\+unsub-(\d+)@', headers)
        if not m:
            continue
        lead_id = int(m.group(1))
        with db.conn() as c:
            row = c.execute('SELECT business_name, do_not_contact FROM leads WHERE id=?', (lead_id,)).fetchone()
            if row and not row['do_not_contact']:
                db.update_lead(c, lead_id, do_not_contact=1, replied=1,
                               notes='auto-unsubscribed via List-Unsubscribe header')
                marked += 1
                print(f'  ✓ unsubscribed lead {lead_id} ({row["business_name"]})')
    return marked


def scan_replies(M):
    """Find inbound mail whose From matches a lead.email we sent to in the last
    SCAN_DAYS, and mark replied=1. Skips leads that already have replied=1 or
    do_not_contact=1.

    Returns count of newly-replied leads.
    """
    with db.conn() as c:
        rows = c.execute("""SELECT id, email, business_name FROM leads
                            WHERE email IS NOT NULL AND email != ''
                              AND email1_sent_at IS NOT NULL
                              AND replied = 0
                              AND do_not_contact = 0
                              AND email1_sent_at > datetime('now', '-21 days')""").fetchall()
    candidates = [dict(r) for r in rows]
    if not candidates:
        return 0

    marked = 0
    for lead in candidates:
        # IMAP FROM filter (matches local-part + domain)
        typ, data = M.search(None, 'FROM', f'"{lead["email"]}"', 'SINCE', _since_str(21))
        if typ != 'OK':
            continue
        if data and data[0]:
            with db.conn() as c:
                db.update_lead(c, lead['id'], replied=1, notes='auto-replied: inbox match')
                marked += 1
                print(f'  ✓ reply detected: {lead["business_name"]} <{lead["email"]}>')
    return marked


def scan_all():
    """One-shot: scan unsubscribes + replies. Returns (unsub_count, reply_count)."""
    M = _imap_connect()
    if not M:
        return (None, None)
    try:
        u = scan_unsubscribes(M)
        r = scan_replies(M)
        return (u, r)
    finally:
        try:
            M.close()
            M.logout()
        except Exception:
            pass
