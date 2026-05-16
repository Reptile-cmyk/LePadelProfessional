#!/usr/bin/env python3
"""
padel_server.py  —  Web frontend for padel availability checker
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Install:   pip install flask flask-cors
Run:       python padel_server.py
Open:      http://localhost:5000  (standalone)
           OR call from your Claude artifact via http://localhost:5000/api/slots

Must sit in the same folder as padel_checker.py
"""

import sys, os, concurrent.futures
sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS
from datetime import date, timedelta

from padel_checker import (
    scrape_padelmates, scrape_matchi, scrape_wannasport,
    PADELMATES_CLUBS, MATCHI_FACILITIES, WANNASPORT_VENUES,
)

app = Flask(__name__)
CORS(app)   # allows claudeusercontent.com (and any origin) to call the API

@app.errorhandler(Exception)
def handle_exception(e):
    """Always return JSON, never an HTML error page."""
    return jsonify({"error": str(e)}), 500


# ── API ───────────────────────────────────────────────────────────────────────

def slots_for_date(d: date) -> list[dict]:
    tasks = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for name, cid in PADELMATES_CLUBS.items():
            tasks.append(('pm', name, ex.submit(scrape_padelmates, name, cid, d)))
        for name, fid in MATCHI_FACILITIES.items():
            tasks.append(('mc', name, ex.submit(scrape_matchi, name, fid, d)))
        for centre in WANNASPORT_VENUES:
            tasks.append(('ws', centre, ex.submit(scrape_wannasport, centre, d)))

    slots = []
    for _, name, future in tasks:
        try:
            slots.extend(future.result(timeout=15))
        except Exception as e:
            print(f"  [WARN] {name}: {e}")   # log but don't crash

    slots.sort(key=lambda s: (s.centre, s.start, -s.duration))
    return [
        {
            "centre":    s.centre,
            "court":     s.court,
            "start":     s.start,
            "end":       s.end,
            "duration":  s.duration,
            "price":     s.price,
            "preferred": s.preferred,
            "book_url":  s.book_url,
        }
        for s in slots
    ]


@app.route("/api/slots")
def api_slots():
    from_str = request.args.get("from") or request.args.get("date")
    to_str   = request.args.get("to") or from_str
    if not from_str:
        return jsonify({"error": "Missing 'from' or 'date' parameter"}), 400
    try:
        d_from = date.fromisoformat(from_str)
        d_to   = date.fromisoformat(to_str)
    except ValueError:
        return jsonify({"error": "Invalid date, use YYYY-MM-DD"}), 400
    if (d_to - d_from).days > 13:
        return jsonify({"error": "Maximum range is 14 days"}), 400

    results   = []
    preferred = 0
    total     = 0
    d = d_from
    while d <= d_to:
        slots    = slots_for_date(d)
        by_centre: dict[str, list] = {}
        for s in slots:
            by_centre.setdefault(s["centre"], []).append(s)
        preferred += sum(1 for s in slots if s["preferred"])
        total     += len(slots)
        results.append({
            "date":    d.isoformat(),
            "weekday": d.strftime("%A"),
            "centres": [{"name": k, "slots": v} for k, v in sorted(by_centre.items())],
            "total":   len(slots),
        })
        d += timedelta(days=1)

    return jsonify({"results": results, "total": total, "preferred": preferred})


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(PAGE)


PAGE = r"""<!DOCTYPE html>
<html lang="da">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Padel Finder</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0d1626;--sf:#1a2540;--sf2:#1e2d4a;
  --br:#2a3a5c;--br2:#3a4f72;
  --cy:#00d4e8;--cy-bg:#082a36;
  --tx:#e2e8f4;--mt:#6b82a0;
  --star:#f59e0b;--rad:12px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font-family:'Outfit',sans-serif;min-height:100vh}

.hdr{background:var(--sf);border-bottom:1px solid var(--br);height:56px;padding:0 28px;
     display:flex;align-items:center;gap:12px}
.hdr-ico{width:32px;height:32px;background:var(--cy-bg);border:1px solid var(--br2);
          border-radius:8px;display:flex;align-items:center;justify-content:center;
          font-size:17px}
.hdr h1{font-size:15px;font-weight:500;letter-spacing:.01em}
.hdr-sub{font-size:12px;color:var(--mt);margin-left:2px}
.hdr-right{margin-left:auto;font-size:12px;color:var(--mt)}

.main{max-width:920px;margin:0 auto;padding:28px 24px}

.ctrl{background:var(--sf);border:1px solid var(--br);border-radius:var(--rad);padding:20px 24px;margin-bottom:24px}
.ctrl-row{display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap}

.toggle{display:flex;background:var(--bg);border:1px solid var(--br);border-radius:8px;overflow:hidden;height:36px;flex-shrink:0}
.tbtn{padding:0 16px;border:none;background:transparent;color:var(--mt);font-family:'Outfit',sans-serif;font-size:13px;cursor:pointer;transition:.15s}
.tbtn.on{background:var(--cy-bg);color:var(--cy)}

.fld{display:flex;flex-direction:column;gap:6px}
.fld label{font-size:11px;color:var(--mt);letter-spacing:.07em;text-transform:uppercase}
.fld input[type=date]{
  background:var(--bg);border:1px solid var(--br);border-radius:8px;
  color:var(--tx);font-family:'JetBrains Mono',monospace;font-size:13px;
  padding:0 12px;height:36px;outline:none;color-scheme:dark;transition:.15s;
  min-width:150px;
}
.fld input[type=date]:focus{border-color:var(--cy)}

.sbtn{height:36px;padding:0 24px;background:var(--cy);color:#0d1626;border:none;
      border-radius:8px;font-family:'Outfit',sans-serif;font-size:14px;font-weight:600;
      cursor:pointer;transition:.15s;flex-shrink:0}
.sbtn:hover{opacity:.88}.sbtn:disabled{opacity:.4;cursor:not-allowed}

.summ{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}
.stat{background:var(--sf);border:1px solid var(--br);border-radius:10px;padding:14px 20px;min-width:130px}
.stat-l{font-size:11px;color:var(--mt);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.stat-v{font-size:26px;font-weight:600;font-family:'JetBrains Mono',monospace}
.cyan{color:var(--cy)}.gold{color:var(--star)}

.day{margin-bottom:22px}
.day-hdr{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.day-wd{font-size:12px;font-weight:500;color:var(--mt);text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}
.day-dt{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--cy)}
.day-ln{flex:1;height:1px;background:var(--br)}
.day-cnt{background:var(--cy-bg);color:var(--cy);font-size:11px;padding:2px 9px;border-radius:20px;white-space:nowrap}

.card{background:var(--sf);border:1px solid var(--br);border-radius:var(--rad);margin-bottom:10px;overflow:hidden}
.card-hdr{padding:11px 16px;border-bottom:1px solid var(--br);display:flex;align-items:center;gap:10px}
.cdot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.cname{font-size:14px;font-weight:500}
.ccnt{margin-left:auto;font-size:12px;color:var(--mt)}

.slot{display:flex;align-items:center;gap:12px;padding:10px 16px;
      border-bottom:1px solid var(--br);transition:.1s;text-decoration:none;color:inherit}
.slot:last-child{border-bottom:none}
.slot:hover{background:var(--sf2)}
.si{font-size:14px;width:18px;flex-shrink:0;text-align:center}
.si.s{color:var(--star)}.si.o{color:var(--br2)}
.st{font-family:'JetBrains Mono',monospace;font-size:13px;font-weight:500;min-width:112px;white-space:nowrap}
.sd{font-size:12px;color:var(--mt);background:var(--bg);padding:2px 8px;border-radius:20px;border:1px solid var(--br);white-space:nowrap}
.sc{font-size:13px;color:var(--mt);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sp{font-family:'JetBrains Mono',monospace;font-size:12px;white-space:nowrap}
.bk{padding:4px 11px;border:1px solid var(--br2);border-radius:6px;color:var(--mt);font-size:11px;
    text-decoration:none;transition:.15s;white-space:nowrap;flex-shrink:0}
.bk:hover{border-color:var(--cy);color:var(--cy)}

.empty{text-align:center;padding:60px 24px;color:var(--mt);font-size:14px}
.empty-ico{font-size:36px;margin-bottom:10px}
.noslot{background:var(--sf);border:1px solid var(--br);border-radius:var(--rad);
        padding:16px;text-align:center;color:var(--mt);font-size:13px;margin-bottom:10px}

.spin-wrap{text-align:center;padding:60px 24px;color:var(--mt);font-size:14px}
.spin{width:28px;height:28px;border:2px solid var(--br);border-top-color:var(--cy);
      border-radius:50%;animation:sp .7s linear infinite;margin:0 auto 12px}
@keyframes sp{to{transform:rotate(360deg)}}
</style>
</head>
<body>

<div class="hdr">
  <div class="hdr-ico">🏓</div>
  <h1>Padel Finder</h1>
  <span class="hdr-sub">Copenhagen &nbsp;·&nbsp; 17:30–18:30 &nbsp;·&nbsp; Double indoor</span>
  <span class="hdr-right">60 &amp; 90 min</span>
</div>

<div class="main">

  <div class="ctrl">
    <div class="ctrl-row">
      <div class="toggle">
        <button class="tbtn on" id="b-single" onclick="setMode('single')">Single date</button>
        <button class="tbtn"    id="b-range"  onclick="setMode('range')">Date range</button>
      </div>
      <div class="fld">
        <label id="lbl-from">Date</label>
        <input type="date" id="d-from">
      </div>
      <div class="fld" id="fld-to" style="display:none">
        <label>To</label>
        <input type="date" id="d-to">
      </div>
      <button class="sbtn" id="sbtn" onclick="search()">Search</button>
    </div>
  </div>

  <div id="summ" class="summ" style="display:none">
    <div class="stat">
      <div class="stat-l">Total slots</div>
      <div class="stat-v cyan" id="s-total">0</div>
    </div>
    <div class="stat">
      <div class="stat-l">★ 90 min preferred</div>
      <div class="stat-v gold" id="s-pref">0</div>
    </div>
    <div class="stat">
      <div class="stat-l">Days checked</div>
      <div class="stat-v" id="s-days">0</div>
    </div>
  </div>

  <div id="results"></div>
</div>

<script>
const COLOURS = ['#00d4e8','#f59e0b','#10b981','#818cf8','#f97316','#e879f9','#34d399','#fb7185'];
let mode = 'single';

function setMode(m) {
  mode = m;
  document.getElementById('b-single').classList.toggle('on', m==='single');
  document.getElementById('b-range').classList.toggle('on', m==='range');
  document.getElementById('fld-to').style.display = m==='range' ? '' : 'none';
  document.getElementById('lbl-from').textContent  = m==='range' ? 'From' : 'Date';
}

const today = new Date().toISOString().slice(0,10);
document.getElementById('d-from').value = today;
document.getElementById('d-to').value   = today;

function colour(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h*31 + name.charCodeAt(i)) % COLOURS.length;
  return COLOURS[h];
}

function cleanCourt(s) { return s.replace(/^\d+\s*[|│]\s*/, '').trim() || s; }

function render(data) {
  document.getElementById('s-total').textContent = data.total;
  document.getElementById('s-pref').textContent  = data.preferred;
  document.getElementById('s-days').textContent  = data.results.length;
  document.getElementById('summ').style.display  = '';

  const box = document.getElementById('results');
  box.innerHTML = '';

  if (!data.results.length) {
    box.innerHTML = '<div class="empty"><div class="empty-ico">🎾</div>No dates returned.</div>';
    return;
  }

  for (const day of data.results) {
    const sec = document.createElement('div');
    sec.className = 'day';

    const cntHtml = day.total > 0
      ? `<span class="day-cnt">${day.total} slot${day.total!==1?'s':''}</span>` : '';

    sec.innerHTML = `
      <div class="day-hdr">
        <span class="day-wd">${day.weekday}</span>
        <span class="day-dt">${day.date}</span>
        <div class="day-ln"></div>
        ${cntHtml}
      </div>`;

    if (!day.total) {
      sec.innerHTML += '<div class="noslot">No available slots on this date</div>';
    } else {
      for (const centre of day.centres) {
        if (!centre.slots.length) continue;
        const col  = colour(centre.name);
        const card = document.createElement('div');
        card.className = 'card';
        card.innerHTML = `
          <div class="card-hdr">
            <div class="cdot" style="background:${col}"></div>
            <span class="cname">${centre.name}</span>
            <span class="ccnt">${centre.slots.length} slot${centre.slots.length!==1?'s':''}</span>
          </div>`;

        for (const s of centre.slots) {
          const court = cleanCourt(s.court);
          const price = s.price ? `<span class="sp">${s.price}</span>` : '';
          const book  = s.book_url
            ? `<a class="bk" href="${s.book_url}" target="_blank">Book →</a>` : '';
          const row = document.createElement('div');
          row.className = 'slot';
          row.innerHTML = `
            <span class="si ${s.preferred?'s':'o'}">${s.preferred?'★':'○'}</span>
            <span class="st">${s.start}–${s.end}</span>
            <span class="sd">${s.duration} min</span>
            <span class="sc">${court}</span>
            ${price}${book}`;
          card.appendChild(row);
        }
        sec.appendChild(card);
      }
    }
    box.appendChild(sec);
  }
}

async function search() {
  const btn  = document.getElementById('sbtn');
  const from = document.getElementById('d-from').value;
  const to   = mode==='range' ? document.getElementById('d-to').value : from;
  if (!from) return;

  btn.disabled = true;
  btn.textContent = 'Searching…';
  document.getElementById('results').innerHTML =
    '<div class="spin-wrap"><div class="spin"></div>Checking all centres…</div>';
  document.getElementById('summ').style.display = 'none';

  try {
    const res  = await fetch(`/api/slots?from=${from}&to=${to}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    render(data);
  } catch(e) {
    document.getElementById('results').innerHTML =
      `<div class="empty"><div class="empty-ico">⚠️</div>${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Search';
  }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    print(f"\n  🏓  Padel Finder running on port {port}\n")
    app.run(debug=False, host="0.0.0.0", port=port)
