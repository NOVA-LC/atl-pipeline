"""Resend send + Day-3 + Day-7 sequencing.

Email content is generated per-prospect by Claude using the research payload.
"""
import os, json, requests, anthropic

RESEND_API = 'https://api.resend.com'

EMAIL_SYSTEM = """You write short cold emails that explain — in plain English — why a small home-services business is leaving real money on the table without a website.

Voice: friendly, direct, lowercase-leaning, never salesy. Sound like a person who's done the work, not a marketer. No "I hope this finds you well."

The email must do three things in order:
1. Open with one CONCRETE money-relevant reason a website prints leads for THIS kind of business — a customer behavior, a missed-call scenario, or a generic stat. Specific beats clever.
2. Show the live demo URL inline (raw URL, never a button or 'click here').
3. Ask one short question — usually their best number to call.

CRITICAL — never invent specifics:
- Do NOT name competitors by name (you don't know which competitors actually rank).
- Do NOT cite specific stats with sources you didn't see ("according to Google data" etc.) unless the stat is general industry knowledge a 50-year-old contractor would already accept.
- Do NOT claim you saw their reviews / their Facebook / their listing unless the research_summary explicitly mentions it.

Banned words: synergy, leverage, transform, elevate, unlock, "in today's digital landscape", "boost", "amazing", "exciting opportunity"."""

EMAIL_PROMPT_DAY1 = """Write a Day-1 cold email to {owner_first} who runs {business} ({category}) in {city}.

You already built them a working demo website at {demo_url}. They didn't ask. You did it on spec.

The email must:
1. Open with a punchy, money-relevant reason they need a real site. Pick the angle that fits a {category}. Spirit examples (DO NOT copy verbatim — invent your own line using the spirit):
   - "most folks google before they call. if your business doesn't show up with a real site, the call goes to whoever does."
   - "without a real site, after-hours leads go to voicemail and rarely call back. a quote form catches them while you sleep."
   - "directory listings convert at 1-2%. a clean home-services site converts at 8-15%. that's 4-7x more calls from the same eyeballs."
   - "people decide who to call in about 5 seconds of clicking. a facebook page or a yelp tile rarely wins that 5 seconds."

   IMPORTANT: do not name specific competitors. Do not say "three other shops" or list company names. Use generic phrasing like "competitors" or "the next guy on the list".

2. Then: "i built this for you on spec — {demo_url} — no charge to look."
3. End with a direct ask: "what's a good number to call you on to flesh it out into your real site?"

Personalize using research:
- {research_summary}

Constraints:
- 90-130 words
- subject under 50 chars, specific to {business}
- 2-3 short paragraphs (no wall of text)
- include {demo_url} inline as a raw URL
- sign off with "— tyler @ nova" (lowercase)

Return JSON: {{"subject": "...", "body": "..."}}"""

EMAIL_PROMPT_DAY3 = """Day-3 follow-up to {owner_first} at {business} ({category}, {city}).

They didn't reply to the Day-1 email about the demo at {demo_url}.

This email should:
1. Be self-aware about cold-emails ("i know cold emails go to die" or similar — vary it)
2. Reinforce ONE money angle by pointing at a specific feature of the demo. Spirit examples:
   - "the demo has click-to-call at the top of every page — phones convert 5x better than forms for {category}s"
   - "i added a quote-request form that texts you. most missed jobs are after-hours leads nobody calls back in time"
   - "your real reviews are pulled in up top — half of {city} googlers decide on stars before reading"
3. Ask: "what's a good number for you?"

Constraints: under 70 words. Subject: "Re: <matches Day-1 subject>".
Return JSON: {{"subject": "Re: ...", "body": "..."}}"""

EMAIL_PROMPT_DAY7 = """Day-7 break-up email to {owner_first} at {business} ({category}).

Last email. One sentence on why a site matters for {category}s, then: the demo at {demo_url} stays up free for a year, come back when ready. No guilt, no fake urgency.

Constraints: under 60 words. Kind, low-key. Sign off "— tyler".
Return JSON: {{"subject": "...", "body": "..."}}"""

def write_email(prompt_tpl, lead, demo_url, research, model='claude-haiku-4-5-20251001'):
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    research_summary = ''
    if research:
        bits = []
        if research.get('owner_name') and research['owner_name'] != 'unknown':
            bits.append(f"owner is {research['owner_name']}")
        if research.get('years_in_business_claim'):
            bits.append(f"{research['years_in_business_claim']} years in business")
        if research.get('vibe'):
            bits.append(f"vibe: {research['vibe']}")
        if research.get('wow_facts'):
            bits.append(f"facts: {', '.join(research['wow_facts'][:2])}")
        research_summary = '; '.join(bits)

    owner_first = (research or {}).get('owner_name', '').split()[0] if research and (research or {}).get('owner_name') not in (None, 'unknown') else 'there'

    prompt = prompt_tpl.format(
        owner_first=owner_first,
        business=lead['business_name'],
        city=lead.get('city') or 'town',
        category=lead.get('category') or 'local business',
        demo_url=demo_url,
        research_summary=research_summary or 'no extra context',
    )
    resp = client.messages.create(
        model=model, max_tokens=600, system=EMAIL_SYSTEM,
        messages=[{'role': 'user', 'content': prompt}]
    )
    text = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(text)

def _can_spam_footer():
    """Build the CAN-SPAM-compliant footer.

    Required by US law for commercial email:
    1. Physical mailing address (NOVA_MAILING_ADDRESS env var)
    2. Clear opt-out mechanism

    Tyler must set NOVA_MAILING_ADDRESS in env or this raises — sending without
    a real address is illegal and the FTC fine is up to $51K per email.
    """
    address = os.environ.get('NOVA_MAILING_ADDRESS', '').strip()
    if not address:
        # Fall back to a placeholder so the pipeline doesn't crash, but log loudly.
        # Tyler MUST set this in production.
        address = '(set NOVA_MAILING_ADDRESS env var — required for CAN-SPAM)'
    from_email = os.environ.get('RESEND_FROM_EMAIL', 'tyler@gonenova.com')
    return (
        f"\n\n—\n"
        f"{address}\n"
        f"reply STOP to opt out, or email {from_email} with 'unsubscribe' in the subject."
    )


def send_via_resend(api_key, from_email, from_name, to_email, subject, body_text,
                    reply_to=None, headers=None, lead_id=None):
    """Send via Resend with CAN-SPAM footer + List-Unsubscribe headers.

    List-Unsubscribe header: lets Gmail/Outlook show a one-click 'Unsubscribe'
    button. The mailto sends to tyler+unsub-{lead_id}@gonenova.com which lands
    in Tyler's inbox — `process-unsubscribes` CLI command can scan and mark.
    """
    body_with_footer = body_text + _can_spam_footer()

    # One-click unsubscribe via mailto
    unsub_email = (os.environ.get('RESEND_FROM_EMAIL', 'tyler@gonenova.com')
                   .replace('@', f'+unsub-{lead_id or "x"}@'))
    unsub_headers = {
        'List-Unsubscribe': f'<mailto:{unsub_email}?subject=unsubscribe>',
        'List-Unsubscribe-Post': 'List-Unsubscribe=One-Click',
    }
    if headers:
        unsub_headers.update(headers)

    payload = {
        'from': f'{from_name} <{from_email}>',
        'to': [to_email],
        'subject': subject,
        'text': body_with_footer,
        'headers': unsub_headers,
    }
    if reply_to:
        payload['reply_to'] = reply_to
    r = requests.post(f'{RESEND_API}/emails',
                      headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                      json=payload)
    return r.status_code, r.json()

def send_day1(lead, demo_url, research, env):
    if not lead.get('email'):
        return None
    msg = write_email(EMAIL_PROMPT_DAY1, lead, demo_url, research,
                      model=env.get('ANTHROPIC_MODEL', 'claude-haiku-4-5-20251001'))
    status, resp = send_via_resend(
        env['RESEND_API_KEY'],
        env.get('RESEND_FROM_EMAIL', 'tyler@gonenova.com'),
        env.get('RESEND_FROM_NAME', 'Tyler · Nova'),
        lead['email'],
        msg['subject'], msg['body'],
        reply_to=env.get('RESEND_REPLY_TO'),
        lead_id=lead.get('id'),
    )
    return {'subject': msg['subject'], 'body': msg['body'], 'status': status, 'resend_id': resp.get('id'), 'response': resp}
