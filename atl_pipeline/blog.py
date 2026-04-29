"""Generate a daily design-essay blog post and commit to gonenova repo.

gonenova is Vite + React + Tailwind + shadcn + Supabase (Lovable). To make this
zero-touch we write MDX/markdown to `src/content/blog/<slug>.md`. Lovable will
pick it up if a `<BlogPost>` route is wired; if not, this still ships the
content into the repo for later integration.
"""
import os, json, base64, datetime, requests, anthropic

GITHUB_API = 'https://api.github.com'

ESSAY_SYSTEM = """You write short, opinionated essays for a website-builder's blog.
Voice: tyler @ gonenova — confident, plain-spoken, thoughtful about design choices, never glossy.
Aim for 350-500 words. No corporate filler. Specific over general."""

ESSAY_PROMPT = """Write today's blog post about a website I built (no charge) for a real local business in Atlanta.

Subject:
- Business: {business} ({category}) in {city}
- Vibe target: {vibe}
- Owner: {owner}
- Why they got picked: {pick_reason}
- Demo URL: {demo_url}

Cover:
1. Why this business deserved a unique site (not a Squarespace template)
2. The ONE design choice you made for them and why (typography? color? layout? voice?)
3. A throwaway-but-true sentence about what most local-services sites get wrong
4. End with a soft CTA — link to the live demo

Format: front-matter (yaml: title, date, slug, business, demo_url) + markdown body. No "in this post" intros. Just start.

Return raw markdown with the front-matter at top."""

def generate_post(lead, research, demo_url, model='claude-haiku-4-5-20251001'):
    client = anthropic.Anthropic(api_key=os.environ['ANTHROPIC_API_KEY'])
    pick_reason = (research or {}).get('vibe') or 'they have great Google reviews and no website yet'
    prompt = ESSAY_PROMPT.format(
        business=lead['business_name'],
        category=lead.get('category') or 'local business',
        city=lead.get('city') or 'Atlanta',
        vibe=(research or {}).get('vibe') or 'warm, local, real',
        owner=(research or {}).get('owner_name') or 'the owner',
        pick_reason=pick_reason,
        demo_url=demo_url,
    )
    resp = client.messages.create(
        model=model, max_tokens=2000, system=ESSAY_SYSTEM,
        messages=[{'role': 'user', 'content': prompt}]
    )
    md = ''.join(b.text for b in resp.content if b.type == 'text').strip()
    return md

def commit_to_gonenova(token, owner, repo, path, content_md, message):
    """PUT /repos/{owner}/{repo}/contents/{path} — creates or updates the file."""
    url = f'{GITHUB_API}/repos/{owner}/{repo}/contents/{path}'
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    # Check if file exists to grab sha
    sha = None
    r = requests.get(url, headers=headers)
    if r.status_code == 200:
        sha = r.json().get('sha')
    body = {
        'message': message,
        'content': base64.b64encode(content_md.encode()).decode(),
        'branch': 'main',
    }
    if sha:
        body['sha'] = sha
    r = requests.put(url, headers=headers, json=body)
    return r.status_code, r.json()

def post_essay(lead, research, demo_url, env):
    today = datetime.date.today().isoformat()
    slug = f"{today}-{lead['slug']}"
    md = generate_post(lead, research, demo_url, model=env.get('ANTHROPIC_MODEL', 'claude-sonnet-4-6'))
    blog_path = env.get('GONENOVA_BLOG_PATH', 'src/content/blog')
    full_path = f'{blog_path}/{slug}.md'
    status, resp = commit_to_gonenova(
        env['GITHUB_TOKEN'],
        env.get('GITHUB_OWNER', 'NOVA-LC'),
        env.get('GONENOVA_REPO_NAME', 'gonenova'),
        full_path, md,
        f'blog: {lead["business_name"]} ({today})'
    )
    return {'slug': slug, 'path': full_path, 'status': status, 'commit': resp.get('commit', {}).get('sha')}
