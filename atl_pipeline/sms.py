"""SMS Day-1 send via Twilio.

Tyler picked SMS-first (not email-first) because:
  1. Outscraper rarely returns email for small home-services — phones are 100%
  2. SMS open rate on B2B is ~98% vs cold email's ~20%
  3. Older trades read texts, often don't open email

The send uses Haiku to generate a short, lowercase, demo-link SMS per lead.
Replies come back to Tyler's Twilio number — `inbox.scan_twilio_replies()`
(in inbox.py) polls Twilio's inbound SMS log and marks replied=1.

Cost model: ~$0.008/msg outbound (carrier-dependent), $1/mo per number.
50 sends/day = $0.40/day = $12/mo.

10DLC: high-volume B2B sending requires Brand + Campaign registration in
Twilio Console (1-3 day approval). Without it, sends >5/day will be filtered.
"""
import os, json
import anthropic


SMS_SYSTEM = """You write very short B2B cold-outreach SMS messages for a website-design business.

Voice: friendly, lowercase-leaning, never salesy. Sound like a real person who built something on spec and wants the owner to take a look.

Constraints:
- 240-300 characters MAX (one SMS segment is 160; we want 1-2 segments, never more)
- include the demo URL inline (raw, no shortener)
- end with "— tyler @ nova" (lowercase)
- never say "exclusive offer", "amazing", "limited time", "act now"
- don't start with "Hi" or "Hello" — be more direct"""


SMS_PROMPT_DAY1 = """Write a Day-1 SMS to {owner_first} who runs {business} ({category}) in {city}.

You already built a working demo website at {demo_url}. They didn't ask. You did it on spec.

Required structure:
1. ONE punchy sentence on why a website prints money for {category}s (generic, no fabricated competitors)
2. "built one for you on spec — {demo_url}"
3. Direct ask: "text back if you want to talk"
4. Sign off: "— tyler @ nova"

Total: under 300 chars. Banned: synergy, leverage, transform, elevate, "exciting opportunity", "boost".

Return JSON: {{"body": "..."}}"""


def write_sms(lead, demo_url, research, model='claude-haiku-4-5-20251001'):
    """Generate the Day-1 SMS body via Haiku."""
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])

    owner_first = 'there'
    if research and research.get('owner_name') and research['owner_name'].lower() != 'unknown':
        owner_first = research['owner_name'].split()[0]

    prompt = SMS_PROMPT_DAY1.format(
        owner_first=owner_first,
        business=lead['business_name'],
        city=lead.get('city') or 'town',
        category=lead.get('category') or 'local business',
        demo_url=demo_url,
    )

    resp = client.messages.create(
        model=model, max_tokens=400, system=SMS_SYSTEM,
        messages=[{'role': 'user', 'content': prompt}]
    )
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(text)


def send_sms_via_twilio(account_sid, auth_token, from_number, to_number, body):
    """Send via Twilio REST API. Returns (status_code, response_json).

    Doesn't import twilio SDK to keep deps minimal — just uses the REST API directly.
    """
    import requests
    if not (account_sid and auth_token and from_number and to_number):
        return None, {'error': 'missing twilio creds or numbers'}
    # Normalize to_number to E.164 (US default)
    to = to_number.strip()
    digits = ''.join(c for c in to if c.isdigit())
    if len(digits) == 10:
        to = '+1' + digits
    elif len(digits) == 11 and digits.startswith('1'):
        to = '+' + digits
    # else assume already formatted

    url = f'https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json'
    r = requests.post(
        url,
        auth=(account_sid, auth_token),
        data={'From': from_number, 'To': to, 'Body': body},
        timeout=15,
    )
    return r.status_code, r.json()


def send_day1_sms(lead, demo_url, research, env):
    """Generate body via Haiku and send via Twilio. Returns dict or None on failure."""
    if not lead.get('phone'):
        return None

    msg = write_sms(lead, demo_url, research,
                    model=env.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'))
    body = msg['body'].strip()
    # Hard cap to keep cost low (3+ segments cost more)
    if len(body) > 320:
        body = body[:317] + '...'

    status, resp = send_sms_via_twilio(
        env.get('TWILIO_ACCOUNT_SID'),
        env.get('TWILIO_AUTH_TOKEN'),
        env.get('TWILIO_FROM_NUMBER'),
        lead['phone'],
        body,
    )
    return {
        'body': body,
        'status': status,
        'sid': resp.get('sid') if isinstance(resp, dict) else None,
        'response': resp,
    }
