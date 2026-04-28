"""Read an Outscraper xlsx and upsert leads into the DB."""
import json
import pandas as pd
from . import db

REQ_COLS = ['name','phone','address','city','state_code','postal_code','rating','reviews','place_id','location_link','email','category','type']

def slugify(name):
    import re
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return s[:50]

def ingest(xlsx_path):
    df = pd.read_excel(xlsx_path)
    # Skip rows that already have a website (those aren't prospects)
    if 'website' in df.columns:
        df = df[df['website'].isna()]
    inserted = 0
    skipped = 0
    with db.conn() as c:
        for _, r in df.iterrows():
            place_id = r.get('place_id')
            if not place_id or pd.isna(place_id):
                skipped += 1
                continue
            name = str(r.get('name','')).strip()
            if not name:
                skipped += 1
                continue
            slug = slugify(name)
            db.upsert_lead(c, place_id,
                business_name=name,
                category=str(r.get('category') or r.get('type') or '').strip() or None,
                city=str(r.get('city') or '').strip() or None,
                state=str(r.get('state_code') or '').strip() or None,
                phone=str(r.get('phone') or '').strip() or None,
                email=str(r.get('email') or '').strip() or None,
                address=str(r.get('address') or '').strip() or None,
                rating=float(r['rating']) if pd.notna(r.get('rating')) else None,
                reviews=int(r['reviews']) if pd.notna(r.get('reviews')) else None,
                google_maps_url=str(r.get('location_link') or '').strip() or None,
                slug=slug,
                raw_outscraper=json.dumps({k: (str(v) if pd.notna(v) else None) for k,v in r.items()}, default=str),
            )
            inserted += 1
    return inserted, skipped
