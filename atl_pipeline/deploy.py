"""Push demo HTML to GitHub repo + create Vercel project + trigger deploy."""
import os, subprocess, json, time
from pathlib import Path
import requests

GITHUB_API = 'https://api.github.com'
VERCEL_API = 'https://api.vercel.com'

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

def vercel_create_project(token, name, repo_full, root_dir, team_id=None):
    """Create a Vercel project linked to a GitHub repo subfolder."""
    url = f'{VERCEL_API}/v11/projects'
    if team_id:
        url += f'?teamId={team_id}'
    body = {
        'name': name,
        'gitRepository': {'type': 'github', 'repo': repo_full},
        'rootDirectory': root_dir,
        'framework': None,
    }
    r = requests.post(url, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, json=body)
    return r.status_code, r.json()

def vercel_trigger_deploy(token, project_name, repo_id, ref='main', team_id=None):
    url = f'{VERCEL_API}/v13/deployments?forceNew=1'
    if team_id:
        url += f'&teamId={team_id}'
    body = {
        'name': project_name, 'project': project_name, 'target': 'production',
        'gitSource': {'type': 'github', 'repoId': repo_id, 'ref': ref},
    }
    r = requests.post(url, headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}, json=body)
    return r.status_code, r.json()

def vercel_production_alias(token, project_name, team_id=None):
    """Returns the {project}.vercel.app domain after deploy."""
    url = f'{VERCEL_API}/v9/projects/{project_name}/domains'
    if team_id:
        url += f'?teamId={team_id}'
    r = requests.get(url, headers={'Authorization': f'Bearer {token}'})
    if r.status_code != 200:
        return None
    domains = r.json().get('domains', [])
    prod = next((d for d in domains if d.get('name') and not d.get('gitBranch')), None)
    return f"https://{prod['name']}" if prod else None

def deploy_lead(lead, html, repo_path, repo_full, repo_id, vercel_token, team_id=None):
    slug = lead['slug']
    write_demo(repo_path, slug, html)
    # Caller is responsible for batching pushes; here we just write.
    # Vercel: ensure project, then trigger deploy
    status, body = vercel_create_project(vercel_token, slug, repo_full, slug, team_id)
    project_id = body.get('id')
    if status not in (200, 201, 409):
        if status == 400 and 'already exists' in str(body):
            pass  # ok
    # Always trigger a deploy (re-deploy is fine)
    status, body = vercel_trigger_deploy(vercel_token, slug, repo_id, 'main', team_id)
    return {'slug': slug, 'project_id': project_id, 'deploy_id': body.get('id'), 'url': f'https://{slug}.vercel.app'}
