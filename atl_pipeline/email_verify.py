"""Verify every email before sending.

Three layers:
1. Syntax (RFC 5322-ish + DNS-resolvable domain)
2. MX lookup — domain must accept email
3. (Optional) Third-party verifier API for bounce/role/disposable detection.
   Recommend: Reoon ($0.0007/check, no card) or ZeroBounce ($0.008/check, more features).

Returns: {'verdict': 'valid' | 'invalid' | 'risky' | 'unknown', 'reason': str}

Reject before send when verdict in ('invalid', 'risky') — bouncing kills domain reputation fast.
"""
import os, re, socket, dns.resolver, requests

EMAIL_RE = re.compile(r'^[a-zA-Z0-9._%+-]+@([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}$')
DISPOSABLE_DOMAINS = {
    'mailinator.com','tempmail.com','10minutemail.com','guerrillamail.com',
    'throwaway.email','yopmail.com','maildrop.cc','sharklasers.com',
}
ROLE_PREFIXES = {'info','contact','admin','support','sales','noreply','no-reply','postmaster','webmaster','help','team'}

def syntax_check(email):
    if not email or not isinstance(email, str):
        return False, 'empty'
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return False, 'bad-syntax'
    if len(email) > 254:
        return False, 'too-long'
    return True, 'ok'

def mx_check(domain, timeout=5):
    """DNS-resolve MX records. Returns (ok, reason)."""
    try:
        resolver = dns.resolver.Resolver()
        resolver.timeout = timeout
        resolver.lifetime = timeout
        answers = resolver.resolve(domain, 'MX')
        if len(answers) == 0:
            return False, 'no-mx'
        return True, 'mx-ok'
    except dns.resolver.NXDOMAIN:
        return False, 'domain-nxdomain'
    except dns.resolver.NoAnswer:
        # No MX — try A record (some domains accept on A)
        try:
            resolver.resolve(domain, 'A')
            return True, 'a-record-fallback'
        except Exception:
            return False, 'no-mx-no-a'
    except dns.exception.Timeout:
        return False, 'dns-timeout'
    except Exception as e:
        return False, f'dns-err: {e.__class__.__name__}'

def reoon_check(email, api_key):
    """Reoon Email Verifier — instant API. Returns dict or None on error.

    Docs: https://emailverifier.reoon.com/api/
    """
    try:
        r = requests.get('https://emailverifier.reoon.com/api/v1/verify',
                         params={'email': email, 'key': api_key, 'mode': 'quick'},
                         timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def verify(email, reoon_key=None):
    """Full verification. Returns {'verdict', 'reason', 'tier_used'}.

    Verdicts:
      valid    — safe to send
      invalid  — DO NOT send (bounce risk)
      risky    — role-based or disposable; sender's call
      unknown  — couldn't determine; retry later or skip
    """
    ok, reason = syntax_check(email)
    if not ok:
        return {'verdict': 'invalid', 'reason': reason, 'tier': 'syntax'}

    email = email.strip().lower()
    local, domain = email.split('@', 1)

    if domain in DISPOSABLE_DOMAINS:
        return {'verdict': 'invalid', 'reason': 'disposable', 'tier': 'syntax'}

    mx_ok, mx_reason = mx_check(domain)
    if not mx_ok:
        return {'verdict': 'invalid', 'reason': mx_reason, 'tier': 'mx'}

    # Role-based addresses are technically valid but dangerous for cold outreach
    is_role = local.split('+')[0] in ROLE_PREFIXES
    if is_role:
        return {'verdict': 'risky', 'reason': 'role-based', 'tier': 'mx'}

    # Optional 3rd-party deeper check
    if reoon_key:
        result = reoon_check(email, reoon_key)
        if result:
            status = (result.get('status') or '').lower()
            if status == 'valid':
                return {'verdict': 'valid', 'reason': 'reoon-valid', 'tier': 'reoon', 'raw': result}
            if status in ('invalid', 'unknown'):
                return {'verdict': status, 'reason': f'reoon-{status}', 'tier': 'reoon', 'raw': result}
            if status in ('risky','catch-all','catch_all'):
                return {'verdict': 'risky', 'reason': f'reoon-{status}', 'tier': 'reoon', 'raw': result}

    # If we got here without Reoon, MX passed and not role-based → probably good
    return {'verdict': 'valid', 'reason': 'mx-passed', 'tier': 'mx'}

def verify_batch(emails, reoon_key=None, max_workers=10):
    """Parallel verify. Returns dict[email] -> result."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    out = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(verify, e, reoon_key): e for e in emails}
        for f in as_completed(futs):
            email = futs[f]
            try:
                out[email] = f.result()
            except Exception as e:
                out[email] = {'verdict': 'unknown', 'reason': str(e), 'tier': 'error'}
    return out
