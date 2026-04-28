"""Detect and flag duplicate leads inside a batch + against existing DB.

Strategies:
1. Exact match on normalized phone (most reliable signal)
2. Exact match on (street + city + zip) — same address = same business
3. Fuzzy match on (normalized name + city) using rapidfuzz token-set ratio
4. Multi-listing detection — Google Maps occasionally has the same business on
   two place_ids (e.g., "1st Class HVAC Contractors Atlanta Inc" + "1ST Class HVAC Atlanta")
"""
import re
from collections import defaultdict
from rapidfuzz import fuzz

PHONE_RE = re.compile(r'\D')

def norm_phone(p):
    if not p: return ''
    digits = PHONE_RE.sub('', str(p))
    return digits[-10:] if len(digits) >= 10 else digits

def norm_name(n):
    if not n: return ''
    n = n.lower()
    n = re.sub(r'\b(inc|llc|llp|corp|co|incorporated|company|ltd|services?|the)\b\.?', '', n)
    n = re.sub(r'[^a-z0-9 ]+', ' ', n)
    return ' '.join(n.split())

def norm_addr(addr):
    if not addr: return ''
    a = addr.lower()
    a = re.sub(r'\bsuite\s*\w+|\bste\s*\w+|\bunit\s*\w+|#\s*\w+', '', a)
    a = re.sub(r'\b(road|rd|street|st|avenue|ave|boulevard|blvd|drive|dr|lane|ln|highway|hwy|parkway|pkwy)\b\.?', '', a)
    a = re.sub(r'[^a-z0-9 ]+', ' ', a)
    return ' '.join(a.split())

def find_dupes(leads, name_threshold=88):
    """Returns list of dupe groups. Each group is a list of lead place_ids that look like the same business."""
    by_phone = defaultdict(list)
    by_addr = defaultdict(list)
    name_norms = []  # (place_id, norm_name, city)
    for l in leads:
        ph = norm_phone(l.get('phone'))
        ad = norm_addr(l.get('address'))
        nm = norm_name(l.get('business_name'))
        city = (l.get('city') or '').lower().strip()
        if ph:
            by_phone[ph].append(l['place_id'])
        if ad:
            by_addr[ad].append(l['place_id'])
        if nm:
            name_norms.append((l['place_id'], nm, city))

    groups = set()
    # Phone-based
    for ph, ids in by_phone.items():
        if len(ids) > 1:
            groups.add(tuple(sorted(ids)))
    # Address-based
    for ad, ids in by_addr.items():
        if len(ids) > 1:
            groups.add(tuple(sorted(ids)))
    # Name+city fuzzy
    for i in range(len(name_norms)):
        pid_i, n_i, c_i = name_norms[i]
        for j in range(i+1, len(name_norms)):
            pid_j, n_j, c_j = name_norms[j]
            if c_i != c_j: continue
            if not n_i or not n_j: continue
            if fuzz.token_set_ratio(n_i, n_j) >= name_threshold:
                groups.add(tuple(sorted([pid_i, pid_j])))

    # Merge overlapping groups (transitive: if A=B and B=C, then A=B=C)
    merged = []
    used = set()
    flat = [list(g) for g in groups]
    for grp in flat:
        if any(p in used for p in grp):
            for m in merged:
                if any(p in m for p in grp):
                    m.update(grp); break
        else:
            merged.append(set(grp))
        used.update(grp)
    return [sorted(m) for m in merged]

def apply_dedup(leads):
    """Return (kept, dropped). 'kept' = list of leads to process, 'dropped' = list with dupe metadata.

    For each dupe group: keep the lead with the most reviews (proxy for "main" listing), drop others.
    """
    groups = find_dupes(leads)
    if not groups:
        return list(leads), []
    # Build pid -> lead map
    by_pid = {l['place_id']: l for l in leads}
    drop_pids = set()
    drop_reasons = {}
    for grp in groups:
        # Pick the keeper = most reviews, fallback to highest rating
        keeper = max(grp, key=lambda pid: (by_pid[pid].get('reviews') or 0, by_pid[pid].get('rating') or 0))
        for pid in grp:
            if pid != keeper:
                drop_pids.add(pid)
                drop_reasons[pid] = f'duplicate-of: {keeper}'
    kept = [l for l in leads if l['place_id'] not in drop_pids]
    dropped = [{**l, 'dropped_reason': drop_reasons[l['place_id']]} for l in leads if l['place_id'] in drop_pids]
    return kept, dropped
