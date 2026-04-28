"""Resend send + Day-3 + Day-7 sequencing.

Email content is generated per-prospect by Claude using the research payload.
"""
import os, json, requests, anthropic

RESEND_API = 'https://api.resend.com'

EMAIL_SYSTEM = """You write short, personal-feeling cold emails for a website-design business.
Voice: friendly, direct, lowercase-leaning, never salesy. No buzzwords. No "I hope this finds you well."
Goal: get the prospect to reply with a phone number or click the demo link.
Always include the demo link inline in the body."""

EMAIL_PROMPT_DAY1 = """Write a Day-1 cold email to {owner_first} who runs {business} in {city}.

You already built them a custom demo at {demo_url}. The email tells them: "I built this for you to see. Feel free to look. What's the best number to flesh it out into your real site?"

Personalize using research:
- {research_summary}

Constraints:
- 80-110 words MAX
- subject line under 50 chars
- one paragraph in the body, max two sentences
- include {demo_url} inline (raw URL, not button)
- end with a casual sign-off (e.g. "— Tyler @ Nova")

Return JSON: {{"subject": "...", "body": "..."}}"""

EMAIL_PROMPT_DAY3 = """Day-3 follow-up to {owner_first} at {business} ({city}).

They didn't reply to the Day-1 email about the custom demo at {demo_url}. Be brief, slightly self-aware, friendly. One thing they should look at on the demo. Ask what their best number is.

Constraints: under 60 words, subject reuses Re: from Day-1 thread.
Return JSON: {{"subject": "Re: <day-1-subject>", "body": "..."}}"""

EMAIL_PROMPT_DAY7 = """Day-7 break-up email to {owner_first} at {business}.

Tell them this is the last email. The demo at {demo_url} stays up. If a website ever becomes a priority, they know where to find me. No guilt.

Constraints: under 50 words, kind tone.
Return JSON: {{"subject": "...", "body": "..."}}"""

def write_email(prompt_tpl, lead, demo_url, research, model='claude-sonnet-4-6'):
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
