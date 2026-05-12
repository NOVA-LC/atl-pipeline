"""Parse the rich fields stashed in `leads.raw_outscraper`.

Outscraper's `maps/search-v3` returns ~40 fields per place (photos, full reviews,
hours, description, subcategories, claimed/verified status, etc). `ingest.py`
JSON-dumps the whole xlsx row into `raw_outscraper`, but the demo renderer only
consumed 13 columns. This module reaches into the JSON blob and normalizes the
photos / reviews / description / hours / subtypes / years fields so
`generate.py` can render real business-specific content.

The xlsx export stringifies complex fields. Sometimes that's valid JSON,
sometimes Python repr (`[{'foo': 1}]`), sometimes pandas' NaN-as-'nan'. We try
each strategy and fall back to the raw string.
"""
import ast
import json
import re


def _parse(val):
    """JSON-loads → ast.literal_eval → str. Returns None for empty/nan."""
    if val is None:
        return None
    if not isinstance(val, str):
        return val
    s = val.strip()
    if not s or s.lower() in ('nan', 'none', 'null', '[]', '{}'):
        return None
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        pass
    try:
        return ast.literal_eval(s)
    except (ValueError, SyntaxError):
        pass
    return s


def _url_ok(u):
    return isinstance(u, str) and u.startswith(('http://', 'https://'))


def photos(raw, limit=8):
    """Real Google Maps photos for this business. List of HTTPS URLs in priority order."""
    urls = []
    # Outscraper exposes the photo list under several keys depending on tier/enrichment
    for key in ('photos_sample', 'photos', 'photo_links', 'photos_data'):
        v = _parse(raw.get(key))
        if isinstance(v, list):
            for p in v:
                if isinstance(p, str) and _url_ok(p):
                    urls.append(p)
                elif isinstance(p, dict):
                    for k in ('photo_url_big', 'photo_url', 'original_photo_url', 'url', 'src'):
                        u = p.get(k)
                        if _url_ok(u):
                            urls.append(u)
                            break
        if urls:
            break
    # Primary photo fallback (single field)
    if not urls:
        for key in ('photo', 'photo_url', 'cover_image', 'main_photo'):
            v = _parse(raw.get(key))
            if _url_ok(v):
                urls.append(v)
                break
    # De-dup preserving order
    seen = set()
    deduped = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
        if len(deduped) >= limit:
            break
    return deduped


def reviews(raw, limit=6, min_len=20, max_len=320):
    """Real Google reviews verbatim. Returns [{author, text, stars, date, source}]."""
    out = []
    for key in ('reviews_data', 'reviews_per_score', 'reviews_list', 'reviews_array'):
        v = _parse(raw.get(key))
        if not isinstance(v, list):
            continue
        for r in v:
            if not isinstance(r, dict):
                continue
            text = (r.get('review_text') or r.get('text') or r.get('review') or '').strip()
            if len(text) < min_len:
                continue
            if len(text) > max_len:
                text = text[: max_len - 1].rstrip() + '…'
            author = (r.get('author_title') or r.get('author_name')
                      or r.get('reviewer_name') or r.get('author') or '—')
            try:
                stars = int(r.get('review_rating') or r.get('rating') or 5)
            except (TypeError, ValueError):
                stars = 5
            stars = max(1, min(5, stars))
            date_str = (r.get('review_datetime_utc') or r.get('review_date')
                        or r.get('date') or '')
            if date_str:
                date_str = str(date_str)[:10]
            out.append({
                'author': str(author).strip() or '—',
                'text': text,
                'stars': stars,
                'date': date_str,
                'source': 'google',
            })
            if len(out) >= limit:
                return out
        if out:
            return out
    return out


def description(raw, max_len=480):
    """Owner's own Google Business Profile description (the 'About' blurb)."""
    for key in ('description', 'business_description', 'about', 'snippet'):
        v = _parse(raw.get(key))
        if isinstance(v, str) and len(v.strip()) >= 30:
            s = v.strip()
            if len(s) > max_len:
                s = s[: max_len - 1].rstrip() + '…'
            return s
    return None


def working_hours(raw):
    """Hours as a normalized dict {day: 'HH:MM-HH:MM'} or a plain string for display."""
    for key in ('working_hours', 'hours', 'working_hours_old_format'):
        v = _parse(raw.get(key))
        if isinstance(v, dict) and v:
            return v
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def hours_summary(raw):
    """Compact one-liner for hero/footer display, e.g. 'Mon-Fri 8AM-5PM'."""
    h = working_hours(raw)
    if not h:
        return None
    if isinstance(h, str):
        return h[:60]
    if isinstance(h, dict):
        # Outscraper sometimes returns hours as {"Monday": "8AM-5PM"} and
        # sometimes as {"Monday": ["8AM-5PM"]} — coerce list-values to their
        # joined string so the dedup set() doesn't blow up on unhashable.
        def _norm(v):
            if isinstance(v, list):
                return ', '.join(str(x) for x in v) if v else None
            return v
        # If all weekdays match, collapse
        weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
        wd_vals = [_norm(h.get(d)) for d in weekdays if h.get(d)]
        if wd_vals and len(set(wd_vals)) == 1:
            return f'Mon–Fri {wd_vals[0]}'
        # Otherwise show today's hours
        import datetime
        today = datetime.datetime.now().strftime('%A')
        if h.get(today):
            return f'{today[:3]} {_norm(h[today])}'
        # Fallback: first available
        for d in weekdays + ['Saturday', 'Sunday']:
            if h.get(d):
                return f'{d[:3]} {h[d]}'
    return None


def subtypes(raw):
    """Google subcategory list — these are the most specific service descriptors."""
    for key in ('subtypes', 'subtype', 'category_ids'):
        v = _parse(raw.get(key))
        if isinstance(v, list):
            out = [str(s).strip() for s in v if isinstance(s, str) and s.strip()]
            if out:
                return out
        if isinstance(v, str) and v.strip():
            out = [s.strip() for s in v.split(',') if s.strip()]
            if out:
                return out
    return []


def years_in_business(raw):
    """Years-in-business as reported by Google, if present."""
    for key in ('years_in_business', 'business_years', 'years_active'):
        v = _parse(raw.get(key))
        if isinstance(v, int) and 0 < v < 200:
            return v
        if isinstance(v, float) and 0 < v < 200:
            return int(v)
        if isinstance(v, str):
            m = re.search(r'\d{1,3}', v)
            if m:
                n = int(m.group())
                if 0 < n < 200:
                    return n
    return None


def claimed(raw):
    """Owner has claimed their GBP. Strong signal they read emails / texts."""
    v = _parse(raw.get('claimed'))
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('true', 'yes', '1')
    return None


def verified(raw):
    v = _parse(raw.get('verified'))
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ('true', 'yes', '1')
    return None


def parse_all(raw_json):
    """Take the raw_outscraper TEXT (or dict) and return normalized fields."""
    if isinstance(raw_json, str):
        try:
            raw = json.loads(raw_json)
        except (ValueError, TypeError):
            raw = {}
    elif isinstance(raw_json, dict):
        raw = raw_json
    else:
        raw = {}
    return {
        'photos': photos(raw),
        'reviews': reviews(raw),
        'description': description(raw),
        'working_hours': working_hours(raw),
        'hours_summary': hours_summary(raw),
        'subtypes': subtypes(raw),
        'years_in_business': years_in_business(raw),
        'claimed': claimed(raw),
        'verified': verified(raw),
    }
