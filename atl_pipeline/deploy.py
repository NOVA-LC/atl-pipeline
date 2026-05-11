"""Push demo HTML to GitHub repo. Vercel auto-deploys via a single umbrella project.

Previously this module created one Vercel project per slug. That hit Vercel's
hard 50-projects-per-repo limit after ~50 leads (every subsequent deploy
returned 400 'repo_links_exceeded_limit'). Now one umbrella project
(DEMOS_BASE_URL, default atlanta-demos.vercel.app) is wired to the demos repo
and serves /{slug}/index.html at /{slug}/. We just write the HTML and git
push; Vercel auto-deploys on the push event.
"""
import os, subprocess
from pathlib import Path
import requests

# The umbrella Vercel project's public hostname. Override via env if you set up
# a custom domain (e.g. demos.gonenova.com).
DEFAULT_DEMOS_BASE = 'atlanta-demos.vercel.app'


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


def demo_url(slug, base=None):
    """Build the public URL for a slug under the umbrella project."""
    host = base or os.environ.get('DEMOS_BASE_URL') or DEFAULT_DEMOS_BASE
    host = host.rstrip('/').removeprefix('https://').removeprefix('http://')
    return f'https://{host}/{slug}/'


def deploy_lead(lead, html, repo_path, *_args, base=None, **_kwargs):
    """Write the HTML to the repo and return its public URL.

    Caller is responsible for batching the git push (see cli.py — it pushes
    once per cron run, not once per lead). The umbrella Vercel project picks
    up the push and rebuilds automatically.

    Extra positional/keyword args are accepted but ignored, for backwards
    compatibility with the old per-slug-project signature.
    """
    slug = lead['slug']
    write_demo(repo_path, slug, html)
    return {'slug': slug, 'url': demo_url(slug, base=base)}


def head_check(url, timeout=10):
    """Return True if URL responds 2xx/3xx, False otherwise. Used for spot-check."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=timeout)
        return 200 <= r.status_code < 400
    except requests.RequestException:
        return False
