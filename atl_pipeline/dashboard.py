"""Daily call dashboard — interactive HTML version of the call sheet.

Generated alongside the markdown call sheet on every cron run, pushed to
demos_repo/dashboard/index.html, and served via the umbrella Vercel project
at https://atlanta-demos.vercel.app/dashboard/.

One click on mobile = call (tel:) or text (sms: with prefilled body).
Status (called / contacted / dismissed) is tracked in localStorage so it
persists per device without a backend.
"""
import json
import re
from datetime import date
from html import escape


def _format_phone(p):
    digits = re.sub(r'\D', '', p or '')
    if len(digits) >= 10:
        d = digits[-10:]
        return f'({d[:3]}) {d[3:6]}-{d[6:]}'
    return p or ''


def _e164(p):
    """Return phone as +1XXXXXXXXXX for tel:/sms: links, or empty if invalid."""
    digits = re.sub(r'\D', '', p or '')
    if len(digits) == 10:
        return f'+1{digits}'
    if len(digits) == 11 and digits.startswith('1'):
        return f'+{digits}'
    return ''


def _is_valid_phone(p):
    """Reject garbage area codes (nan, 943, etc.) — North American area codes
    start 2-9 and second digit is 0-9 (no 1)."""
    digits = re.sub(r'\D', '', p or '')
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return False
    if digits[0] in '01':
        return False
    # Known invalid/unassigned area codes Outscraper sometimes returns
    bad_areas = {'000', '111', '555', '943'}
    return digits[:3] not in bad_areas


def _talking_points(research):
    points = []
    if not research:
        return []
    if research.get('years_in_business_claim'):
        points.append(f"{research['years_in_business_claim']} years in business")
    elif research.get('founded_year'):
        points.append(f"founded {research['founded_year']}")
    if research.get('owner_name') and research['owner_name'].lower() != 'unknown':
        points.append(f"owner: {research['owner_name']}")
    if research.get('vibe'):
        points.append(research['vibe'])
    for f in (research.get('wow_facts') or [])[:2]:
        if len(points) >= 4:
            break
        points.append(f)
    return points[:5]


def _sms_body(business, owner_first, demo_url):
    """Prefilled SMS body. Lowercase, friendly, under one segment."""
    who = owner_first or 'there'
    return (
        f"hey {who}, tyler with nova — built a custom website demo for "
        f"{business} on spec. take a look: {demo_url} — happy to chat if "
        f"you want to talk shop. — tyler @ nova"
    )


def render_dashboard(leads, today=None, base_url=None):
    """Return a self-contained HTML string for the dashboard."""
    today = today or date.today().isoformat()
    base_url = (base_url or 'https://atlanta-demos.vercel.app').rstrip('/')

    cards_data = []
    for lead in leads:
        research = {}
        if lead.get('research_payload'):
            try:
                research = json.loads(lead['research_payload'])
            except Exception:
                pass
        biz = lead.get('business_name') or '(no name)'
        phone_raw = lead.get('phone') or ''
        e164 = _e164(phone_raw)
        phone_valid = _is_valid_phone(phone_raw)
        owner_first = ''
        if research.get('owner_name') and research['owner_name'].lower() != 'unknown':
            owner_first = research['owner_name'].split()[0]
        rating = lead.get('rating')
        try:
            rating = float(rating) if rating is not None else None
        except (ValueError, TypeError):
            rating = None
        reviews = lead.get('reviews') or 0
        category = lead.get('category') or ''
        city = lead.get('city') or ''
        demo = lead.get('vercel_url') or ''
        gmaps = lead.get('google_maps_url') or ''
        cards_data.append({
            'id': str(lead.get('id') or biz),
            'business': biz,
            'phone_display': _format_phone(phone_raw),
            'phone_e164': e164,
            'phone_valid': phone_valid,
            'sms_body': _sms_body(biz, owner_first, demo),
            'owner_first': owner_first,
            'rating': rating,
            'reviews': reviews,
            'category': category,
            'city': city,
            'demo': demo,
            'gmaps': gmaps,
            'talking_points': _talking_points(research),
        })

    # Sort: phone-valid leads first, then by rating desc, then by reviews desc
    cards_data.sort(key=lambda c: (not c['phone_valid'], -(c['rating'] or 0), -(c['reviews'] or 0)))

    n_total = len(cards_data)
    n_dialable = sum(1 for c in cards_data if c['phone_valid'])
    n_five_star = sum(1 for c in cards_data if c['rating'] and c['rating'] >= 4.8)

    # Unique categories + cities for filter pills
    categories = sorted({c['category'] for c in cards_data if c['category']})
    cities = sorted({c['city'] for c in cards_data if c['city']})

    cards_json = json.dumps(cards_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#0b0f17">
<title>Nova Pipeline · {today} · {n_total} leads</title>
<style>
  * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
  html, body {{ margin: 0; padding: 0; background: #0b0f17; color: #e7ecf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 16px; line-height: 1.45; }}
  a {{ color: inherit; text-decoration: none; }}
  .wrap {{ max-width: 920px; margin: 0 auto; padding: 16px; }}
  header {{ padding: 8px 0 16px; border-bottom: 1px solid #1e2738; margin-bottom: 16px; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .sub {{ color: #8a94a6; font-size: 14px; }}
  .stats {{ display: flex; gap: 12px; margin-top: 12px; flex-wrap: wrap; }}
  .stat {{ background: #131a26; border-radius: 10px; padding: 8px 12px; font-size: 13px; }}
  .stat b {{ color: #fff; font-size: 16px; }}
  .toolbar {{ position: sticky; top: 0; background: #0b0f17; padding: 12px 0;
    z-index: 10; border-bottom: 1px solid #1e2738; margin-bottom: 8px; }}
  .search {{ width: 100%; padding: 12px 14px; font-size: 16px; border-radius: 10px;
    background: #131a26; border: 1px solid #1e2738; color: #e7ecf3; }}
  .filters {{ display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }}
  .pill {{ background: #131a26; border: 1px solid #1e2738; padding: 6px 11px;
    border-radius: 999px; font-size: 13px; cursor: pointer; user-select: none;
    transition: background 0.1s; }}
  .pill:hover {{ background: #1c2638; }}
  .pill.active {{ background: #3a82f7; border-color: #3a82f7; color: #fff; }}
  .pill.muted {{ opacity: 0.6; }}
  .card {{ background: #131a26; border: 1px solid #1e2738; border-radius: 14px;
    padding: 16px; margin: 12px 0; transition: opacity 0.15s; }}
  .card.dialed {{ opacity: 0.45; border-color: #2a3140; }}
  .card.contacted {{ border-color: #1f6f3e; background: #0e1c14; }}
  .card.dismissed {{ display: none; }}
  .biz {{ font-size: 18px; font-weight: 600; color: #fff; margin: 0 0 4px; }}
  .meta {{ font-size: 13px; color: #8a94a6; margin-bottom: 4px; }}
  .star {{ color: #ffc857; font-weight: 600; }}
  .askfor {{ font-size: 14px; color: #ffcf69; margin: 4px 0; }}
  .actions {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 12px 0 8px; }}
  .btn {{ display: flex; align-items: center; justify-content: center; gap: 6px;
    padding: 12px; border-radius: 10px; font-weight: 600; font-size: 15px;
    border: none; cursor: pointer; color: #fff; transition: transform 0.05s, opacity 0.1s; }}
  .btn:active {{ transform: scale(0.97); }}
  .btn-call {{ background: #1f6f3e; }}
  .btn-text {{ background: #2657c1; }}
  .btn-demo {{ background: #3a3f4d; color: #e7ecf3; padding: 9px 12px;
    width: 100%; margin-bottom: 8px; font-size: 14px; }}
  .btn-disabled {{ background: #2a3140; color: #8a94a6; cursor: not-allowed; }}
  .btn-gmaps {{ background: transparent; color: #8a94a6; font-size: 13px; padding: 4px 0;
    text-decoration: underline; cursor: pointer; }}
  .row-secondary {{ display: flex; gap: 12px; align-items: center; margin-top: 6px;
    flex-wrap: wrap; }}
  .status-bar {{ display: flex; gap: 4px; margin-top: 10px; font-size: 12px; }}
  .status-btn {{ background: transparent; border: 1px solid #2a3140; color: #8a94a6;
    padding: 4px 10px; border-radius: 6px; cursor: pointer; font-size: 12px; }}
  .status-btn.active {{ background: #1f6f3e; border-color: #1f6f3e; color: #fff; }}
  .status-btn.active.contacted {{ background: #1f6f3e; }}
  .status-btn.active.dialed {{ background: #555; border-color: #555; }}
  .tps {{ font-size: 13px; color: #c8d0dd; margin-top: 8px; }}
  .tps ul {{ margin: 4px 0 0; padding-left: 20px; }}
  .tps li {{ margin: 2px 0; }}
  details summary {{ cursor: pointer; color: #8a94a6; font-size: 13px; margin-top: 6px;
    list-style: none; }}
  details summary::-webkit-details-marker {{ display: none; }}
  details summary::before {{ content: "▸ "; }}
  details[open] summary::before {{ content: "▾ "; }}
  .nophone {{ background: #5a2a2a; color: #fcd; font-size: 12px; padding: 3px 8px;
    border-radius: 6px; display: inline-block; margin-top: 4px; }}
  footer {{ text-align: center; color: #5a6478; font-size: 12px; padding: 32px 0 16px; }}
  @media (min-width: 600px) {{
    .actions {{ grid-template-columns: 1fr 1fr 1fr; }}
    .actions .btn-demo {{ grid-column: 1; margin: 0; }}
  }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>📞 Nova Pipeline</h1>
  <div class="sub">{today} · live demos, one-click outreach</div>
  <div class="stats">
    <div class="stat"><b>{n_total}</b> total</div>
    <div class="stat"><b>{n_dialable}</b> dialable</div>
    <div class="stat"><b>{n_five_star}</b> 4.8★+</div>
    <div class="stat" id="stat-called"><b id="called-count">0</b> called</div>
    <div class="stat" id="stat-contacted"><b id="contacted-count">0</b> contacted</div>
  </div>
</header>

<div class="toolbar">
  <input id="search" class="search" placeholder="Search business, city, owner…" type="search" autocomplete="off">
  <div class="filters" id="filters">
    <span class="pill active" data-filter="all">All</span>
    <span class="pill" data-filter="dialable">Dialable</span>
    <span class="pill" data-filter="five">4.8★+</span>
    <span class="pill" data-filter="hide-dialed">Hide called</span>
  </div>
  <div class="filters" id="cat-filters" style="margin-top:6px"></div>
</div>

<div id="cards"></div>

<footer>End of {today} · {n_total} leads · atl-pipeline</footer>
</div>

<script>
const LEADS = {cards_json};
const STORAGE_KEY = 'nova_pipeline_status_v1';
const status = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{{}}');
let search = '';
let primaryFilter = 'all';
let catFilter = '';

function save() {{ localStorage.setItem(STORAGE_KEY, JSON.stringify(status)); }}

function setStatus(id, st) {{
  if (status[id] === st) {{ delete status[id]; }} else {{ status[id] = st; }}
  save();
  render();
}}

function callNow(id, phone) {{
  if (!status[id]) status[id] = 'dialed';
  save();
  location.href = 'tel:' + phone;
  setTimeout(render, 100);
}}

function textNow(id, phone, body) {{
  if (!status[id]) status[id] = 'dialed';
  save();
  const sep = /iPhone|iPad|Mac/.test(navigator.userAgent) ? '&' : '?';
  location.href = 'sms:' + phone + sep + 'body=' + encodeURIComponent(body);
  setTimeout(render, 100);
}}

function render() {{
  const container = document.getElementById('cards');
  const q = search.toLowerCase().trim();
  let nCalled = 0, nContacted = 0;
  const html = LEADS.map(lead => {{
    const st = status[lead.id] || '';
    if (st === 'dialed') nCalled++;
    if (st === 'contacted') nContacted++;
    // Filter
    if (primaryFilter === 'dialable' && !lead.phone_valid) return '';
    if (primaryFilter === 'five' && !(lead.rating >= 4.8)) return '';
    if (primaryFilter === 'hide-dialed' && (st === 'dialed' || st === 'contacted' || st === 'dismissed')) return '';
    if (catFilter && lead.category !== catFilter) return '';
    if (q) {{
      const hay = (lead.business + ' ' + lead.city + ' ' + lead.owner_first + ' ' + lead.category).toLowerCase();
      if (!hay.includes(q)) return '';
    }}
    const ratingStr = lead.rating ? `<span class="star">${{lead.rating.toFixed(1)}}★</span> · ${{lead.reviews}} reviews` : '';
    const bits = [lead.category, lead.city, ratingStr].filter(Boolean).join(' · ');
    const askFor = lead.owner_first ? `<div class="askfor">Ask for: ${{lead.owner_first}}</div>` : '';
    const phoneDisp = lead.phone_valid
      ? `<div class="meta">${{lead.phone_display}}</div>`
      : `<div class="nophone">no valid phone — skip</div>`;
    const callBtn = lead.phone_valid
      ? `<button class="btn btn-call" onclick="callNow('${{lead.id}}','${{lead.phone_e164}}')">📞 Call</button>`
      : `<button class="btn btn-disabled" disabled>📞 No phone</button>`;
    const sms = lead.sms_body.replace(/'/g, "\\\\'");
    const textBtn = lead.phone_valid
      ? `<button class="btn btn-text" onclick="textNow('${{lead.id}}','${{lead.phone_e164}}','${{sms}}')">💬 Text</button>`
      : `<button class="btn btn-disabled" disabled>💬 No phone</button>`;
    const demoBtn = lead.demo
      ? `<a class="btn btn-demo" href="${{lead.demo}}" target="_blank" rel="noopener">🔗 Preview demo</a>`
      : '';
    const tps = lead.talking_points.length
      ? `<details class="tps"><summary>${{lead.talking_points.length}} talking points</summary><ul>${{lead.talking_points.map(t => `<li>${{escapeHtml(t)}}</li>`).join('')}}</ul></details>`
      : '';
    const gmaps = lead.gmaps ? `<a class="btn-gmaps" href="${{lead.gmaps}}" target="_blank" rel="noopener">📍 maps</a>` : '';
    return `
      <div class="card ${{st}}" data-id="${{lead.id}}">
        <div class="biz">${{escapeHtml(lead.business)}}</div>
        <div class="meta">${{bits}}</div>
        ${{askFor}}
        ${{phoneDisp}}
        <div class="actions">
          ${{demoBtn}}
          ${{callBtn}}
          ${{textBtn}}
        </div>
        <div class="row-secondary">${{gmaps}}</div>
        ${{tps}}
        <div class="status-bar">
          <button class="status-btn ${{st === 'dialed' ? 'active dialed' : ''}}" onclick="setStatus('${{lead.id}}','dialed')">Called</button>
          <button class="status-btn ${{st === 'contacted' ? 'active contacted' : ''}}" onclick="setStatus('${{lead.id}}','contacted')">Contacted</button>
          <button class="status-btn" onclick="setStatus('${{lead.id}}','dismissed')">Hide</button>
        </div>
      </div>`;
  }}).join('');
  container.innerHTML = html || '<p style="color:#8a94a6;text-align:center;padding:32px">No leads match the filter.</p>';
  document.getElementById('called-count').textContent = nCalled;
  document.getElementById('contacted-count').textContent = nContacted;
}}

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
}}

// Wire toolbar
document.getElementById('search').addEventListener('input', e => {{ search = e.target.value; render(); }});
document.getElementById('filters').addEventListener('click', e => {{
  const t = e.target.closest('.pill');
  if (!t) return;
  document.querySelectorAll('#filters .pill').forEach(p => p.classList.remove('active'));
  t.classList.add('active');
  primaryFilter = t.dataset.filter;
  render();
}});

// Build category filter pills
const categories = [...new Set(LEADS.map(l => l.category).filter(Boolean))].sort();
const catEl = document.getElementById('cat-filters');
catEl.innerHTML = ['<span class="pill active" data-cat="">All categories</span>',
  ...categories.map(c => `<span class="pill" data-cat="${{c}}">${{escapeHtml(c)}}</span>`)].join('');
catEl.addEventListener('click', e => {{
  const t = e.target.closest('.pill');
  if (!t) return;
  document.querySelectorAll('#cat-filters .pill').forEach(p => p.classList.remove('active'));
  t.classList.add('active');
  catFilter = t.dataset.cat;
  render();
}});

render();
</script>
</body>
</html>
"""
