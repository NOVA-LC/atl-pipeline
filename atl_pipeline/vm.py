"""Day-3 ringless voicemail drop via Slybroadcast.

Slybroadcast's API works by HTTP POST to https://www.mobile-sphere.com/gateway/vmb.php
with the prerecorded audio file URL + list of phone numbers. The drop hits
voicemail directly without ringing the recipient's phone.

Tyler records ONE generic message in his own voice (uploaded to a public URL):
  "Hi, this is Tyler from Nova. I texted you a few days ago about a custom
   website I built for your shop — wanted to leave a quick note in case you
   missed the text. Take a look at the link, give me a call back if you like
   what you see. Thanks."

Cost: ~$0.10 per drop. 50/day = $5/day = $150/mo. Tyler can drop volume by
only sending VM to leads where SMS got engagement (clicked link via shortener).

Setup (one-time):
  1. Sign up at slybroadcast.com (~$10 one-time + per-drop)
  2. Record audio in app or upload MP3 (Tyler's own voice — high trust signal)
  3. Get audio_url + your account creds
  4. Set env vars: SLYBROADCAST_USER, SLYBROADCAST_PASS, SLYBROADCAST_AUDIO_URL,
     SLYBROADCAST_CALLER_ID (the number that shows up on caller ID)

This module is best-effort: if env is missing, vm sends are skipped silently
so the rest of the pipeline still ships SMS.
"""
import os
import requests


SLYBROADCAST_GATEWAY = 'https://www.mobile-sphere.com/gateway/vmb.php'


def send_voicemail_drop(phone_numbers, env=None):
    """Drop a ringless voicemail to a list of US phone numbers.

    Args:
      phone_numbers: list of phone number strings (any format — we normalize)
      env: dict of env vars (defaults to os.environ)

    Returns: dict with {'status': int, 'response': str, 'session_id': str|None}
        or None if creds missing.

    Slybroadcast bills per number drop. Sending one batch with all numbers is
    one API call but you pay per recipient.
    """
    env = env or os.environ
    user = env.get('SLYBROADCAST_USER')
    password = env.get('SLYBROADCAST_PASS')
    audio_url = env.get('SLYBROADCAST_AUDIO_URL')
    caller_id = env.get('SLYBROADCAST_CALLER_ID') or env.get('TWILIO_FROM_NUMBER')

    if not (user and password and audio_url and caller_id):
        return None   # silently skip — vm not configured yet

    # Normalize numbers to 10-digit US (Slybroadcast wants 1{areacode}{number} or just 10-digit)
    normalized = []
    for p in phone_numbers:
        digits = ''.join(c for c in (p or '') if c.isdigit())
        if len(digits) == 10:
            normalized.append(digits)
        elif len(digits) == 11 and digits.startswith('1'):
            normalized.append(digits[1:])
    if not normalized:
        return {'status': 0, 'response': 'no valid numbers', 'session_id': None}

    # Caller ID also needs to be 10-digit
    cid_digits = ''.join(c for c in caller_id if c.isdigit())
    if len(cid_digits) == 11 and cid_digits.startswith('1'):
        cid_digits = cid_digits[1:]

    payload = {
        'c_uid': user,
        'c_password': password,
        'c_url': audio_url,
        'c_callerID': cid_digits,
        'c_phone': ','.join(normalized),   # Slybroadcast accepts comma-separated batch
        'mobile_only': '0',                # 0 = drop to landlines too
    }
    try:
        r = requests.post(SLYBROADCAST_GATEWAY, data=payload, timeout=30)
        body = r.text
        # Slybroadcast returns plain text like "OK\nsession_id=12345" on success
        session_id = None
        for line in body.splitlines():
            if line.startswith('session_id='):
                session_id = line.split('=', 1)[1].strip()
        return {'status': r.status_code, 'response': body[:500], 'session_id': session_id}
    except Exception as e:
        return {'status': 0, 'response': f'error: {e}', 'session_id': None}
