"""Push demo HTML to GitHub repo + create Vercel project + trigger deploy."""
import os, subprocess, json, time
from pathlib import Path
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

GITHUB_API = 'https://api.github.com'
VERCEL_API = 'https://api.vercel.com'

# Retry transient API errors (5xx, network blips). Don't retry 4xx — those are bugs.
class TransientVercelError(Exception):
    pass

_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=10),
    retry=retry_if_exception_type((TransientVercelError, requests.RequestException)),
    reraise=True,
)

def write_demo(repo_path, slug, html):
    folder = Path(repo_path) / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'index.html').write_text(html)

def git_commit_and_push(repo_path, message, slugs):
    # Stage just the changed slugs (so commits are clean)
    subprocess.check_call(['git', '-C', repo_path, 'add'] + [f'{s}/index.html' for s in slugs])
    # noop if nothing staged
    r = subprocess.run(['git', '-C', repo_path, 'diff', '--cached', '--quiet'])
    if r.returncode == 0:
        return False
    subprocess.check_call(['git', '-C', repo_path, 'commit', '-m', message])
    subprocess.check_call(['git', '-C', repo_path, 'push', 'origin', 'main'])
    return True

@_retry
def vercel_create_project(token, name, repo_full, root_dir, team_id=None):
    """Create a Vercel project linked to a GitHub repo subfolder.

    Retries on 5xx / network errors. 4xx (incl. 409 already-exists) returns normally.
    """
    url = f'{VERCEL_API}/v11/projects'
    if team_id:
        url += f'?teamId={team_id}'
    body = {
        'name': name,
        'gitRepository': {'type': 'github', 'repo': repo_full},
        'rootDirectory': root_dir,
        'framework': None,
    }
    r = requests.post(url, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                      json=body, timeout=30)
    if r.status_code >= 500:
        raise TransientVercelError(f'create_project {r.status_code}: {r.text[:200]}')
    return r.status_code, r.json()

@_retry
def vercel_trigger_deploy(token, project_name, repo_id, ref='main', team_id=None):
    url = f'{VERCEL_API}/v13/deployments?forceNew=1'
    if team_id:
        url += f'&teamId={team_id}'
    body = {
        'name': project_name, 'project': project_name, 'target': 'production',
        'gitSource': {'type': 'github', 'repoId': repo_id, 'ref': ref},
    }
    r = requests.post(url, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
                      json=body, timeout=30)
    if r.status_code >= 500:
        raise TransientVercelError(f'trigger_deploy {r.status_code}: {r.text[:200]}')
    return r.status_code, r.json()

def vercel_get_project(token, project_name, team_id=None):
    """Fetch existing project (used after 409 'already exists' to recover project_id)."""
    url = f'{VERCEL_API}/v9/projects/{project_name}'
    if team_id:
        url += f'?teamId={team_id}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=30)
    return r.status_code, r.json() if r.headers.get('content-type','').startswith('application/json') else {}

def vercel_get_deployment(token, deployment_id, team_id=None):
    """Fetch deployment status. Returns (status_code, body)."""
    url = f'{VERCEL_API}/v13/deployments/{deployment_id}'
    if team_id:
        url += f'?teamId={team_id}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=30)
    return r.status_code, r.json() if r.headers.get('content-type','').startswith('application/json') else {}

def vercel_wait_for_ready(token, deployment_id, team_id=None, timeout_s=180, poll_s=4):
    """Poll deployment until readyState is terminal. Returns final body dict.

    Terminal states: READY, ERROR, CANCELED. Raises TimeoutError if still building
    after timeout_s. Raises RuntimeError on ERROR/CANCELED.
    """
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        sc, body = vercel_get_deployment(token, deployment_id, team_id)
        last = body
        state = body.get('readyState') or body.get('status')
        if state == 'READY':
            return body
        if state in ('ERROR', 'CANCELED'):
            raise RuntimeError(f'vercel deploy {deployment_id} ended in {state}: {body.get("errorMessage") or body.get("error")}')
        time.sleep(poll_s)
    raise TimeoutError(f'vercel deploy {deployment_id} not READY after {timeout_s}s (last state {last.get("readyState")})')

def vercel_production_alias(token, project_name, team_id=None):
    """Returns the production alias domain (e.g. project.vercel.app) for a project, if assigned."""
    url = f'{VERCEL_API}/v9/projects/{project_name}/domains?production=true'
    if team_id:
        url += f'&teamId={team_id}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=30)
    if r.status_code != 200:
        return None
    domains = r.json().get('domains', [])
    # Prefer a non-git-branch domain attached to production (verified, no redirect)
    prod = next((d for d in domains
                 if d.get('name') and not d.get('gitBranch') and not d.get('redirect')), None)
    return prod['name'] if prod else None

def _resolve_url(token, slug, deployment_body, team_id=None):
    """Given a READY deployment, pick the best public URL.

    Order of preference:
      1. {slug}.vercel.app, if Vercel assigned it as a production alias (read from project domains)
      2. any production alias listed in the deployment body
      3. the unique deployment URL (always works while the deployment exists)
    """
    # 1. Project-level production alias (the pretty URL — only ours if the global name wasn't taken)
    alias = vercel_production_alias(token, slug, team_id)
    if alias:
        return f'https://{alias}'
    # 2. Deployment-level alias array
    for a in (deployment_body.get('alias') or []):
        if isinstance(a, str) and a.endswith('.vercel.app'):
            return f'https://{a}'
    # 3. Unique deployment URL (e.g. slug-hash-team.vercel.app)
    durl = deployment_body.get('url')
    if durl:
        return f'https://{durl}'
    return None

def deploy_lead(lead, html, repo_path, repo_full, repo_id, vercel_token, team_id=None,
                wait_timeout_s=180):
    slug = lead['slug']
    write_demo(repo_path, slug, html)
    # Caller is responsible for batching pushes; here we just write.

    # Vercel: ensure project exists, then trigger deploy
    status, body = vercel_create_project(vercel_token, slug, repo_full, slug, team_id)
    project_id = body.get('id')
    if status == 409 or (status == 400 and 'already exists' in str(body).lower()):
        # Project already exists — recover project_id via GET
        gsc, gbody = vercel_get_project(vercel_token, slug, team_id)
        if gsc == 200:
            project_id = gbody.get('id') or project_id
    elif status not in (200, 201):
        # Unknown failure — surface it so caller marks the lead as failed
        raise RuntimeError(f'vercel_create_project {slug}: status={status} body={str(body)[:300]}')

    # Trigger production deploy
    status, body = vercel_trigger_deploy(vercel_token, slug, repo_id, 'main', team_id)
    if status not in (200, 201, 202):
        raise RuntimeError(f'vercel_trigger_deploy {slug}: status={status} body={str(body)[:300]}')
    deploy_id = body.get('id')
    if not deploy_id:
        raise RuntimeError(f'vercel_trigger_deploy {slug}: no deployment id in response')

    # Wait for build to finish so the alias is actually live before we hand the URL to Tyler
    final = vercel_wait_for_ready(vercel_token, deploy_id, team_id, timeout_s=wait_timeout_s)

    url = _resolve_url(vercel_token, slug, final, team_id)
    if not url:
        raise RuntimeError(f'deploy_lead {slug}: build READY but no usable URL resolved')

    return {'slug': slug, 'project_id': project_id, 'deploy_id': deploy_id, 'url': url}
