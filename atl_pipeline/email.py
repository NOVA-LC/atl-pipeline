"""Resend send + Day-3 + Day-7 sequencing.

Email content is generated per-prospect by Claude using the research payload.
"""
import os, json, requests, anthropic

RESEND_API = 'https://api.resend.com'

EMAIL_SYSTEM = """You write short cold emails that explain — in plain English — why a small home-services business is leaving real money on the table without a website.

Voice: friendly, direct, lowercase-leaning, never salesy. Sound like a person who's done the work, not a marketer. No "I hope this finds you well."

The email must do three things in order:
1. Open with one CONCRETE money-relevant reason a website prints leads for THIS kind of business — quote a stat, a customer behavior, a competitor's setup, or a missed-call scenario. Specific beats clever.
2. Show the live demo URL inline (raw URL, never a button or 'click here').
3. Ask one short question — usually their best number to call.

Banned words: synergy, leverage, transform, elevate, unlock, "in today's digital landscape", "boost", "amazing", "exciting opportunity"."""

EMAIL_PROMPT_DAY1 = """Write a Day-1 cold email to {owner_first} who runs {business} ({category}) in {city}.

You already built them a working demo website at {demo_url}. They didn't ask. You did it on spec.

The email must:
1. Open with a punchy, money-relevant reason they need a real site. Pick the angle that fits a {category}. Spirit examples (do not copy verbatim — invent your own using these as the spirit):
   - "9 out of 10 people google a {category} before calling — if you don't show up clean, your competitor does."
   - "Right now when somebody searches '{category} {city}', three other shops have a 'request a quote' button before yours."
   - "your google business profile is doing its job. but the click goes to a facebook page from 2019, and that's where leads die."
   - "after-hours calls are 30% of the work for {category}s. without a contact form, those leads call the next guy on the list."
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

def send_via_resend(api_key, from_email, from_name, to_email, subject, body_text, reply_to=None, headers=None):
    payload = {
        'from': f'{from_name} <{from_email}>',
        'to': [to_email],
        'subject': subject,
        'text': body_text,
    }
    if reply_to:
        payload['reply_to'] = reply_to
    if headers:
        payload['headers'] = headers
    r = requests.post(f'{RESEND_API}/emails',
                      headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                      json=payload)
    return r.status_code, r.json()

def send_day1(lead, demo_url, research, env):
    if not lead.get('email'):
        return None
    msg = write_email(EMAIL_PROMPT_DAY1, lead, demo_url, research, model=env.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6'))
    status, resp = send_via_resend(
        env['RESEND_API_KEY'],
        env.get('RESEND_FROM_EMAIL', 'tyler@gonenova.com'),
        env.get('RESEND_FROM_NAME', 'Tyler · Nova'),
        lead['email'],
        msg['subject'], msg['body'],
        reply_to=env.get('RESEND_REPLY_TO'),
    )
    return {'subject': msg['subject'], 'body': msg['body'], 'status': status, 'resend_id': resp.get('id'), 'response': resp}
