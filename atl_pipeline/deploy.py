"""Push demo HTML to GitHub repo. Vercel auto-deploys via a single umbrella project.

Previously this module created one Vercel project per slug. That hit Vercel's
hard 50-projects-per-repo limit after ~50 leads (every subsequent deploy
returned 400 'repo_links_exceeded_limit'). Now one umbrella project
(DEMOS_BASE_URL, default atlanta-demos.vercel.app) is wired to the demos repo
and serves /{slug}/index.html at /{slug}/. We just write the HTML and git
push; Vercel auto-deploys on the push event.

URL modes:
  1. Path-based (default):       https://atlanta-demos.vercel.app/<slug>/
  2. Branded subdomain (opt-in): https://<slug>.demos.gonenova.com/

To enable branded subdomains, set the env var DEMOS_DOMAIN=demos.gonenova.com
(or whatever apex you own) AND complete the one-time setup:
  a. Add wildcard domain *.demos.gonenova.com to the Vercel project
  b. DNS: CNAME *.demos.gonenova.com → cname.vercel-dns.com
  c. Ensure vercel.json (see DEMOS_VERCEL_CONFIG below) is in the demos repo root —
     ensure_vercel_config() writes it for you on the next git push.
"""
import json
import os
import subprocess
from pathlib import Path
import requests

# The umbrella Vercel project's public hostname. Override via env if you set up
# a custom domain (e.g. demos.gonenova.com).
DEFAULT_DEMOS_BASE = 'atlanta-demos.vercel.app'


# vercel.json that ships in the demos repo. Two rewrite rules:
#  1. Wildcard subdomain `<slug>.demos.gonenova.com/*` → `/<slug>/*`
#  2. (Implicit) path-based access `/<slug>/*` keeps working
# The `has` host pattern captures the slug from the subdomain. If DEMOS_DOMAIN
# is unset on Vercel, the rewrite no-ops harmlessly and path-based access
# continues to work — backwards compatible.
def _vercel_config(demos_domain: str | None) -> dict:
    """Build vercel.json. When DEMOS_DOMAIN is set, branded-subdomain rewrites
    are added; otherwise just a minimal clean-urls config.

    The rewrites order matters: more specific rules go first. /static/* and
    /api/* pass through unchanged on subdomains (assets and the tracker
    function share the apex), then the catch-all rewrites everything else
    under the slug.
    """
    if not demos_domain:
        return {
            'cleanUrls': True,
            'trailingSlash': False,
        }
    apex = demos_domain.rstrip('/').removeprefix('https://').removeprefix('http://')
    # Escape dots for the regex
    apex_re = apex.replace('.', r'\.')
    host_match = {'type': 'host', 'value': f'(?<slug>[^.]+)\\.{apex_re}'}
    return {
        'cleanUrls': True,
        'trailingSlash': False,
        'rewrites': [
            # Pass-through for shared assets + the tracker function
            {'source': '/static/:path*', 'has': [host_match], 'destination': '/static/:path*'},
            {'source': '/api/:path*', 'has': [host_match], 'destination': '/api/:path*'},
            # Catch-all: route the subdomain into the slug folder
            {'source': '/:path*', 'has': [host_match], 'destination': '/:slug/:path*'},
        ],
    }


def ensure_vercel_config(repo_path: str | Path) -> bool:
    """Write/overwrite vercel.json at the demos repo root if its content differs.
    Returns True if the file was written, False if it was already up to date.
    Idempotent — safe to call on every push.
    """
    target = _vercel_config(os.environ.get('DEMOS_DOMAIN'))
    path = Path(repo_path) / 'vercel.json'
    desired = json.dumps(target, indent=2) + '\n'
    if path.exists():
        try:
            if path.read_text() == desired:
                return False
        except Exception:
            pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(desired)
    return True


def ensure_tracker_function(repo_path: str | Path) -> bool:
    """Write the /api/track.js Vercel serverless function into the demos repo.
    Returns True if the file changed, False if it was already current.

    The function is generated from atl_pipeline.tracker_function.TRACK_JS so
    it's version-controlled with the Python package, not hand-edited in the
    demos repo.
    """
    from . import tracker_function as tf
    target = Path(repo_path) / 'api' / 'track.js'
    target.parent.mkdir(parents=True, exist_ok=True)
    desired = tf.TRACK_JS
    if target.exists():
        try:
            if target.read_text() == desired:
                return False
        except Exception:
            pass
    target.write_text(desired)
    return True


def ensure_infra(repo_path: str | Path) -> list[str]:
    """Write all infra files into the demos repo (vercel.json, tracker fn,
    etc.). Returns the list of repo-relative paths that were changed; pass
    this to git_commit_and_push as extra_paths.
    """
    changed = []
    if ensure_vercel_config(repo_path):
        changed.append('vercel.json')
    if ensure_tracker_function(repo_path):
        changed.append('api/track.js')
    return changed


def write_demo(repo_path, slug, html):
    folder = Path(repo_path) / slug
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'index.html').write_text(html)


def git_commit_and_push(repo_path, message, slugs, extra_paths=None):
    """Stage + commit + push a batch of demo slugs.

    `extra_paths` is an optional list of repo-relative paths (e.g. preview
    images, vercel.json, api/track.js) that should be included in the same
    commit. We always run `git add -A` for extras because they may be new,
    modified, or deleted depending on the run.
    """
    paths_to_add = [f'{s}/index.html' for s in slugs] + list(extra_paths or [])
    if paths_to_add:
        subprocess.check_call(['git', '-C', repo_path, 'add'] + paths_to_add)
    # noop if nothing staged
    r = subprocess.run(['git', '-C', repo_path, 'diff', '--cached', '--quiet'])
    if r.returncode == 0:
        return False
    subprocess.check_call(['git', '-C', repo_path, 'commit', '-m', message])
    subprocess.check_call(['git', '-C', repo_path, 'push', 'origin', 'main'])
    return True


def demo_url(slug, base=None):
    """Build the public URL for a slug.

    If DEMOS_DOMAIN is set (e.g. 'demos.gonenova.com'), returns a branded
    subdomain URL: 'https://<slug>.demos.gonenova.com/'.
    Otherwise falls back to the path-based umbrella URL.
    """
    domain = os.environ.get('DEMOS_DOMAIN')
    if domain:
        d = domain.rstrip('/').removeprefix('https://').removeprefix('http://')
        return f'https://{slug}.{d}/'
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
