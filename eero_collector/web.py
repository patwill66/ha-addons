"""The add-on's ingress pages (Home Assistant sidebar: "eero Network").

  GET  /                status, eero login, data sources (free / Eero Plus), backfill, API use
  POST login, verify    eero login: identifier -> eero sends a code -> code (stored in /data/session.json)
  GET  devices          every device eero knows: sortable, searchable table
  GET  device?id=N      one device: link quality, signal per eero/band/channel, usage and change history
  GET  incident         "What happened?": one device and the mesh around a moment (?id=N&at=YYYY-MM-DDTHH:MM&win=min)
  GET  snapshot         a consistent copy of the database (SQLite online backup, read-only)

Only the Supervisor's ingress proxy may connect; Home Assistant authenticates the user. The database
is opened read-only. The only eero calls made here are the two login POSTs, on the user's request.
"""

import datetime
import hashlib
import html
import http.server
import json
import pathlib
import sqlite3
import tempfile
import threading
import time
import urllib.parse
from zoneinfo import ZoneInfo

from collector import report
from collector.api import EeroClient, EeroError

PORT = 8099
INGRESS_PROXY = "172.30.32.2"
CHUNK = 1 << 16
e = html.escape

CSS = """
:root{--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#e3e3e3;--card:#f7f7f8;--accent:#0a7cff;--bad:#c62828;--ok:#2e7d32;--warn:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#9a9a9a;--line:#2b2b2b;--card:#1b1b1d;--accent:#4aa3ff;--bad:#ef5350;--ok:#66bb6a;--warn:#ffb74d}}
body{font:14px/1.45 system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:16px}
nav a{margin-right:16px;color:var(--accent);text-decoration:none;font-weight:600}
h1{font-size:20px;margin:8px 0 16px} h2{font-size:16px;margin:24px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:20px} .card span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{cursor:pointer;user-select:none;position:sticky;top:0;background:var(--bg)} td.n,th.n{text-align:right}
.wrap{overflow-x:auto} .muted{color:var(--muted)} .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
input[type=text],input[type=search],input[type=datetime-local],select{padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);min-width:240px}
button{padding:8px 14px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
ul.find{list-style:none;padding:0} ul.find li{padding:4px 0;border-bottom:1px solid var(--line)}
form.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
svg text{fill:var(--muted);font-size:10px} .chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;margin:8px 0}
"""

SORT_JS = """
document.querySelectorAll('table.sort th').forEach((th,i)=>th.onclick=()=>{
 const t=th.closest('table'),b=t.tBodies[0],rows=[...b.rows],asc=th.dataset.asc!=='1';
 t.querySelectorAll('th').forEach(x=>delete x.dataset.asc); th.dataset.asc=asc?'1':'0';
 const key=r=>{const c=r.cells[i],v=c.dataset.v??c.textContent.trim();const n=parseFloat(v);return isNaN(n)?v.toLowerCase():n};
 rows.sort((a,b)=>{const x=key(a),y=key(b);return (x>y?1:x<y?-1:0)*(asc?1:-1)}); rows.forEach(r=>b.appendChild(r))});
const q=document.getElementById('q'); if(q) q.oninput=()=>{const s=q.value.toLowerCase();
 document.querySelectorAll('table.sort tbody tr').forEach(r=>r.style.display=r.textContent.toLowerCase().includes(s)?'':'none')};
"""


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>"
            f"<nav><a href='./'>Status</a><a href='devices'>Devices</a><a href='incident'>What happened?</a></nav><h1>{e(title)}</h1>{body}"
            f"</main><script>{SORT_JS}</script></body></html>").encode()


def fmt_bytes(b):
    if b is None:
        return "—"
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= size:
            return f"{b / size:.1f} {unit}"
    return f"{int(b)} B"


def ago(epoch, now):
    if not epoch:
        return "—"
    s = now - epoch
    return "now" if s < 90 else f"{s // 60} min ago" if s < 5400 else f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} d ago"


def svg_bars(values, labels, height=120, color="var(--accent)", unit=""):
    """values: list of numbers (None = no data). Simple bar chart with min/max labels."""
    w, n = 900, max(len(values), 1)
    top = max([v for v in values if v] or [1])
    bw = w / n
    bars = "".join(
        f"<rect x='{i * bw:.1f}' y='{height - (v or 0) / top * (height - 14):.1f}' width='{max(bw - 1, 1):.1f}' "
        f"height='{(v or 0) / top * (height - 14):.1f}' fill='{color}'><title>{e(labels[i])}: {v if v is not None else 'no data'} {unit}</title></rect>"
        for i, v in enumerate(values))
    return (f"<svg viewBox='0 0 {w} {height + 14}' width='100%' preserveAspectRatio='none'>{bars}"
            f"<text x='2' y='10'>{top:.1f} {e(unit)}</text><text x='2' y='{height + 12}'>{e(labels[0] if labels else '')}</text>"
            f"<text x='{w - 2}' y='{height + 12}' text-anchor='end'>{e(labels[-1] if labels else '')}</text></svg>")


def svg_line(points, lo, hi, height=120, unit="", color="var(--accent)", tz=None):
    """points: [(epoch, value)]; fixed y range lo..hi; time labels in tz."""
    if not points:
        return "<p class='muted'>No samples in this period.</p>"
    w = 900
    t0, t1 = points[0][0], max(points[-1][0], points[0][0] + 1)
    xy = [((t - t0) / (t1 - t0) * w, height - (min(max(v, lo), hi) - lo) / (hi - lo) * height) for t, v in points if v is not None]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(xy))
    lab = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%b %d %H:%M")  # noqa: E731
    return (f"<svg viewBox='0 0 {w} {height + 14}' width='100%' preserveAspectRatio='none'>"
            f"<path d='{path}' fill='none' stroke='{color}' stroke-width='1.5'/>"
            f"<text x='2' y='10'>{hi} {e(unit)}</text><text x='2' y='{height - 2}'>{lo} {e(unit)}</text>"
            f"<text x='2' y='{height + 12}'>{lab(t0)}</text><text x='{w - 2}' y='{height + 12}' text-anchor='end'>{lab(t1)}</text></svg>")


class App:
    def __init__(self, db_path, session_store, timezone, log):
        self.db_path, self.session, self.tz, self.log = db_path, session_store, ZoneInfo(timezone), log
        self.pending = None  # (token, started) during a login
        self.snapshot_lock = threading.Lock()

    def ro(self):
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    # --- pages ------------------------------------------------------------------------------------
    def status(self, message=""):
        now = int(time.time())
        body = f"<p class='warn'>{e(message)}</p>" if message else ""
        logged_in = bool(self.session.load())
        if not pathlib.Path(self.db_path).exists():
            return page("eero Network", body + self.login_form(logged_in) + "<p class='muted'>No data yet.</p>")
        db = self.ro()
        try:
            n = report.network(db, now, self.tz, self.db_path)
            nodes = report.nodes(db)
            tasks = [dict(r) for r in db.execute("SELECT * FROM task_state ORDER BY name")]
            calls = [dict(r) for r in db.execute("""SELECT path, status, sum(calls) AS calls, sum(ms_total)/sum(calls) AS ms
                FROM api_calls_hourly WHERE hour >= ? GROUP BY path, status ORDER BY calls DESC""", (now - 86400,))]
            caps = [dict(r) for r in db.execute("SELECT * FROM capabilities ORDER BY requires_premium, name")]
            bf = {r["name"]: dict(r) for r in db.execute("SELECT * FROM backfill")}
            hourly = [dict(r) for r in db.execute("SELECT * FROM usage_network_hourly WHERE hour >= ? ORDER BY hour",
                                                  (now - 48 * 3600,))]
            changes = report.mesh_changes(db, now - 7 * 86400, now)[::-1]
        finally:
            db.close()
        state = n["collector_state"] or "unknown"
        cls = "ok" if state in ("collecting", "backfilling") else "bad"
        cards = [
            ("Internet", "up" if n["isp_up"] else "down" if n["isp_up"] == 0 else "—"),
            ("eeros online", f"{n['online_eeros']} / {(n['online_eeros'] or 0) + (n['offline_eeros'] or 0)}"),
            ("Clients connected", n["clients_connected"]),
            ("Download, last hour", f"{n['last_hour_down_mbps']} Mbit/s avg" if n["last_hour_down_mbps"] is not None else "—"),
            ("Downloaded today", f"{n['today_down_gb']} GB" if n["today_down_gb"] is not None else "—"),
            ("Last speed test", f"{n['speedtest_down_mbps']} / {n['speedtest_up_mbps']} Mbit/s"),
            ("Weak signal", n["weak_signal_count"]), ("Poor link", n["poor_link_count"]),
            ("Collector", f"<span class='{cls}'>{e(state)}</span>"), ("Database", f"{n['database_mb']} MB"),
        ]
        body += "<div class='cards'>" + "".join(f"<div class='card'><b>{v if str(v).startswith('<') else e(str(v))}</b><span>{e(k)}</span></div>"
                                                for k, v in cards) + "</div>"
        body += self.login_form(logged_in, state)
        if hourly:
            labels = [datetime.datetime.fromtimestamp(h["hour"], self.tz).strftime("%a %H:00") for h in hourly]
            body += "<h2>Network traffic, last 48 hours (eero hourly counters)</h2><div class='chart'>"
            body += svg_bars([round(h["down"] / 1e9, 2) for h in hourly], labels, unit="GB down")
            body += svg_bars([round(h["up"] / 1e9, 2) for h in hourly], labels, height=60, color="var(--warn)", unit="GB up")
            body += "</div>"
        body += "<h2>eero nodes</h2><div class='wrap'><table><tr><th>Node</th><th>Status</th><th class=n>Mesh</th><th>Upstream</th><th class=n>Clients</th><th>Radios (channel · utilization · clients)</th><th>Last reboot</th><th>Firmware</th></tr>"
        for x in nodes:
            radios = " · ".join(f"{r['band']} GHz ch {r['channel']} {r['utilization']}% {r['clients']}" for r in x["radios"])
            up = "wired (gateway)" if x.get("is_gateway") else f"{x.get('upstream') or '—'} {x.get('upstream_radio') or ''}"
            body += (f"<tr><td>{e(x['location'] or '')}</td><td class='{'ok' if x['online'] else 'bad'}'>{'online' if x['online'] else 'offline'}</td>"
                     f"<td class=n>{x.get('mesh_bars') or '—'}/5</td><td>{e(up)}</td><td class=n>{x.get('clients')}</td>"
                     f"<td>{e(radios)}</td><td>{ago(x.get('last_reboot_at'), now)}</td><td>{e(x.get('os_version') or '')}</td></tr>")
        body += "</table></div><h2>Mesh changes, last 7 days</h2>" + self.change_list(changes)
        body += "<h2>Data sources</h2><table><tr><th>Source</th><th>Needs</th><th>Status</th><th>Checked</th></tr>"
        for c in caps:
            st = {"ok": "<span class=ok>collecting</span>", "not_entitled": "<span class=muted>locked (Eero Plus)</span>"}.get(
                c["status"], f"<span class=bad>{e(c['status'])}</span>")
            body += f"<tr><td>{e(c['name'])}</td><td>{'Eero Plus' if c['requires_premium'] else 'free'}</td><td>{st}</td><td>{ago(c['checked_at'], now)}</td></tr>"
        body += "</table><h2>History backfill</h2><table><tr><th>Stage</th><th>Done</th><th>Position</th></tr>"
        for stage in ("network_daily", "network_hourly", "device_daily", "device_hourly"):
            b = bf.get(stage, {})
            pos = b.get("cursor") or ""
            if pos.isdigit():
                pos = datetime.datetime.fromtimestamp(int(pos), self.tz).strftime("%Y-%m-%d %H:00")
            body += f"<tr><td>{stage}</td><td>{'yes' if b.get('done') else 'no'}</td><td>{e(pos)}</td></tr>"
        body += "</table><h2>Tasks</h2><table><tr><th>Task</th><th class=n>Runs</th><th class=n>Failures</th><th>Last OK</th><th>Last error</th></tr>"
        for t in tasks:
            body += (f"<tr><td>{e(t['name'])}</td><td class=n>{t['runs']}</td><td class=n>{t['failures']}</td>"
                     f"<td>{ago(t['last_ok_at'], now)}</td><td class=muted>{e(t['last_error'] or '')}</td></tr>")
        body += "</table><h2>eero API calls, last 24 hours</h2><table><tr><th>Endpoint</th><th class=n>Status</th><th class=n>Calls</th><th class=n>Avg ms</th></tr>"
        body += "".join(f"<tr><td>{e(c['path'])}</td><td class=n>{c['status']}</td><td class=n>{c['calls']}</td><td class=n>{c['ms']}</td></tr>" for c in calls)
        body += "</table><p><a href='snapshot'>Download a database snapshot</a></p>"
        return page("eero Network", body)

    def login_form(self, logged_in, state=None):
        if self.pending:
            return ("<h2>eero login</h2><form method='post' action='verify'><p>eero sent a verification code by email or text. "
                    "Enter it here:</p><input type='text' name='code' autocomplete='one-time-code' inputmode='numeric' required> "
                    "<button>Verify</button></form>")
        if logged_in and state != "login_required":
            return ""
        why = "The eero session expired. " if logged_in else ""
        return ("<h2>eero login</h2><form method='post' action='login'>"
                f"<p>{why}Log in with the eero account dedicated to Home Assistant (email or phone; Amazon logins aren't supported).</p>"
                "<input type='text' name='login' autocomplete='username' required> <button>Send code</button></form>")

    def devices(self):
        now = int(time.time())
        db = self.ro()
        try:
            rows = report.device_table(db, now, self.tz)
        finally:
            db.close()
        body = "<p><input type='search' id='q' placeholder='Search name, node, type, maker…'></p><div class='wrap'><table class='sort'><thead><tr>"
        cols = ["Device", "Status", "eero", "Band", "Ch", "Signal", "Link", "Retry %", "Today", "7 days", "Drops 7d",
                "Roams 7d", "Last connected", "First seen", "Type", "Maker", "IP", "MAC"]
        body += "".join(f"<th{' class=n' if c in ('Signal', 'Link', 'Retry %', 'Today', '7 days', 'Drops 7d', 'Roams 7d') else ''}>{c}</th>" for c in cols)
        body += "</tr></thead><tbody>"
        for r in rows:
            sig = r["signal"]
            sig_cls = "bad" if sig is not None and sig < report.WEAK_SIGNAL_DBM else ""
            first = (r.get("eero_first_seen") or "")[:10]
            body += (f"<tr><td><a href='device?id={r['id']}'>{e(r['name'] or '')}</a>{' 🔒' if r.get('is_private') else ''}</td>"
                     f"<td class='{'ok' if r['connected'] else 'muted'}'>{'online' if r['connected'] else 'offline'}</td>"
                     f"<td>{e(r.get('node') or '')}</td><td>{e(r.get('band') or ('wired' if r.get('connection_type') == 'wired' else ''))}</td>"
                     f"<td>{r.get('channel') or ''}</td><td class='n {sig_cls}' data-v='{sig if sig is not None else -999}'>{sig if sig is not None else ''}</td>"
                     f"<td class=n>{r.get('score_bars') or ''}</td><td class=n>{r.get('tx_retry_pct') if r.get('tx_retry_pct') is not None else ''}</td>"
                     f"<td class=n data-v='{r['today_bytes'] or 0}'>{fmt_bytes(r['today_bytes'])}</td>"
                     f"<td class=n data-v='{r['week_bytes'] or 0}'>{fmt_bytes(r['week_bytes'])}</td>"
                     f"<td class=n>{r['drops_7d'] or ''}</td><td class=n>{r['roams_7d'] or ''}</td>"
                     f"<td data-v='{r.get('last_connected_at') or 0}'>{ago(r.get('last_connected_at'), now) if not r['connected'] else 'now'}</td>"
                     f"<td>{e(first)}</td><td>{e(r.get('device_type') or '')}</td><td>{e(r.get('manufacturer') or '')}</td>"
                     f"<td>{e(r.get('ip') or '')}</td><td class=muted>{e(r.get('mac') or '')}</td></tr>")
        body += ("</tbody></table></div><p class='muted'>Click a column to sort. Today = eero's hourly counters; 7 days = complete days. "
                 "Drops and roams are counted between 5-minute polls plus eero's event feed. 🔒 = private (rotating) MAC.</p>")
        return page(f"Devices ({sum(1 for r in rows if r['connected'])} online of {len(rows)})", body)

    def device(self, device_id):
        now = int(time.time())
        db = self.ro()
        try:
            d = report.device_now(db, device_id, now, self.tz)
            if not d:
                return None
            s = report.device_series(db, device_id, now, days=7)
            nodes = {r[0]: r[1] for r in db.execute("SELECT id, location FROM nodes")}
            q = report.roam_summary(db, device_id, now)
            by = report.signal_by(db, device_id, now)
        finally:
            db.close()
        cur = d["sample"] or {}
        cards = [("Status", "online" if d["connected"] else "offline"), ("eero", d.get("node") or "—"),
                 ("Band / channel", f"{d.get('band') or '—'} GHz · ch {d.get('channel') or '—'}" if d.get("band") else "wired" if d.get("connection_type") == "wired" else "—"),
                 ("Signal", f"{cur.get('signal')} dBm" if cur.get("signal") is not None else "—"),
                 ("Link quality", f"{cur.get('score_bars')}/5" if cur.get("score_bars") else "—"),
                 ("Link rate", f"{cur.get('rx_rate_mbps')} Mbit/s" if cur.get("rx_rate_mbps") else "—"),
                 ("Today", f"{d['today_mb']} MB"), ("Last hour", f"{d['last_hour_mb']} MB"),
                 ("First seen by eero", (d.get("eero_first_seen") or "—")[:10]), ("Type", d.get("device_type") or "—")]
        body = "<div class='cards'>" + "".join(f"<div class='card'><b>{e(str(v))}</b><span>{e(k)}</span></div>" for k, v in cards) + "</div>"
        body += f"<p class='muted'>{e(d.get('manufacturer') or '')} · {e(d.get('mac') or '')} · {e(d.get('ip') or '')}{' · private MAC' if d.get('is_private') else ''}</p>"
        body += (f"<p><a href='incident?id={device_id}'>What happened? Look at this device and the mesh around a moment →</a></p>"
                 "<h2>Roaming, last 24 hours</h2><div class='cards'>")
        for k, v in (("Roams", q["roams"]), ("Roams to a weaker eero", q["weaker_roams"]),
                     ("Minutes on a weaker eero", q["weaker_minutes"]),
                     ("Best eero", f"{q['best_eero']} {q['best_band']} GHz · {q['best_dbm']} dBm" if q["best_eero"] else "—")):
            warn = k in ("Roams to a weaker eero", "Minutes on a weaker eero") and v
            body += f"<div class='card'><b class='{'warn' if warn else ''}'>{e(str(v))}</b><span>{e(k)}</span></div>"
        body += (f"</div><p class='muted'>A weaker eero is one where this device's usual signal is {report.ROAM_WEAKER_DB} dB "
                 "or more below the eero it left, or below its best eero on the same band (medians over 7 days). "
                 "That means most for devices that stay in one place.</p>")
        if by:
            body += ("<h2>Signal by eero, band and channel, last 30 days</h2><div class='wrap'><table class='sort'><thead><tr>"
                     "<th>eero</th><th>Band</th><th class=n>Channel</th><th class=n>Hours</th><th class=n>Signal (median)</th>"
                     "<th class=n>rx Mbit/s</th><th class=n>tx Mbit/s</th><th class=n>Retry %</th><th>Last</th></tr></thead><tbody>")
            for r in by:
                cls = "bad" if r["signal"] is not None and r["signal"] < report.WEAK_SIGNAL_DBM else ""
                body += (f"<tr><td>{e(str(r['node']))}</td><td>{e(r['band'] or '')}</td><td class=n>{r['channel'] or ''}</td>"
                         f"<td class=n>{r['hours']}</td><td class='n {cls}'>{r['signal'] if r['signal'] is not None else ''}</td>"
                         f"<td class=n>{r['rx_mbps'] or ''}</td><td class=n>{r['tx_mbps'] or ''}</td>"
                         f"<td class=n>{r['retry_pct'] if r['retry_pct'] is not None else ''}</td><td>{ago(r['last'], now)}</td></tr>")
            body += ("</tbody></table></div><p class='muted'>eero moves every eero's 5 and 6 GHz channel together, so rows "
                     "for the same eero and band on different channels show what a channel change did to this device.</p>")
        sig =[(x["ts"], x["signal"]) for x in s["samples"] if x["signal"] is not None]
        if sig:
            body += "<h2>Signal, last 7 days</h2><div class='chart'>" + svg_line(sig, -90, -20, unit="dBm", tz=self.tz) + "</div>"
        uh = s["usage_hourly"]
        if uh:
            labels = [datetime.datetime.fromtimestamp(x["hour"], self.tz).strftime("%a %H:00") for x in uh]
            body += "<h2>Data per hour, last 7 days (eero counters)</h2><div class='chart'>"
            body += svg_bars([round((x["down"] + x["up"]) / 1e6, 1) for x in uh], labels, unit="MB") + "</div>"
        ud = list(reversed(s["usage_daily"]))
        if ud:
            body += "<h2>Data per day</h2><div class='chart'>"
            body += svg_bars([round((x["down"] + x["up"]) / 1e9, 2) for x in ud], [x["day"] for x in ud], unit="GB") + "</div>"
        body += "<h2>Changes, last 30 days</h2><table><tr><th>When</th><th>What</th><th>From</th><th>To</th><th>Source</th></tr>"
        for c in s["changes"][:100]:
            old, new = c["old"], c["new"]
            if c["kind"] == "node":
                old, new = nodes.get(int(old), old) if old and old.isdigit() else old, nodes.get(int(new), new) if new and new.isdigit() else new
            when = datetime.datetime.fromtimestamp(c["ts"], self.tz).strftime("%b %d %H:%M")
            body += f"<tr><td>{when}</td><td>{e(c['kind'])}</td><td>{e(str(old or ''))}</td><td>{e(str(new or ''))}</td><td class=muted>{e(c['source'])}</td></tr>"
        body += "</table>"
        return page(d["name"] or "Device", body)

    def when(self, t, fmt="%a %b %-d %H:%M"):
        return datetime.datetime.fromtimestamp(t, self.tz).strftime(fmt) if t else ""

    def change_list(self, changes):
        if not changes:
            return "<p class='muted'>None.</p>"
        return "<ul class='find'>" + "".join(
            f"<li><b>{self.when(c['ts'])}</b> · <span class='{'bad' if c['kind'] in ('reboot', 'internet') else ''}'>"
            f"{e(c['text'])}</span></li>" for c in changes) + "</ul>"

    WINDOWS = (15, 30, 60, 120)

    def incident(self, q):
        """The "What happened?" page: a form, and when it names a device and a time, the report around it."""
        now = int(time.time())
        raw_id, raw_at = q.get("id", [""])[0], q.get("at", [""])[0]
        win = int(q["win"][0]) if q.get("win", [""])[0] in {str(w) for w in self.WINDOWS} else 30
        at, message = None, ""
        if raw_at:
            try:
                at = int(datetime.datetime.strptime(raw_at[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=self.tz).timestamp())
            except ValueError:
                message = "That time wasn't understood."
        db = self.ro()
        try:
            choices = report.device_choices(db, now)
            inc = report.incident(db, int(raw_id), at, win * 60, win * 60, now) if raw_id.isdigit() and at else None
        finally:
            db.close()
        default_at = raw_at[:16] if at else datetime.datetime.fromtimestamp(now - 600, self.tz).strftime("%Y-%m-%dT%H:%M")
        opts = "".join(f"<option value='{did}'{' selected' if str(did) == raw_id else ''}>{e(label)}</option>"
                       for did, label in choices)
        wins = "".join(f"<option value='{w}'{' selected' if w == win else ''}>± {w} min</option>" for w in self.WINDOWS)
        body = ("<p class='muted'>Pick a device and the moment something went wrong (a call dropped, a page stalled). "
                "This lines up the device's own polls and events with what the mesh and other devices did around it.</p>"
                f"<form class='row' method='get' action='incident'><select name='id' required><option value=''>Device…</option>{opts}"
                f"</select><input type='datetime-local' name='at' value='{e(default_at)}' required>"
                f"<select name='win'>{wins}</select><button>Show</button></form>")
        if message:
            body += f"<p class='warn'>{e(message)}</p>"
        if raw_id.isdigit() and at and inc is None:
            body += "<p class='warn'>That device isn't known.</p>"
        if not inc:
            return page("What happened?", body)
        return page(f"What happened? {inc['device']['name']}", body + self.incident_report(inc, raw_id, win))

    def incident_report(self, inc, raw_id, win):
        hm = lambda t: self.when(t, "%H:%M:%S")  # noqa: E731
        step = lambda t: datetime.datetime.fromtimestamp(t, self.tz).strftime("%Y-%m-%dT%H:%M")  # noqa: E731
        span = win * 60
        body = (f"<p><b>{e(inc['device']['name'])}</b> · {self.when(inc['from'])} → {self.when(inc['to'], '%H:%M')} "
                f"(around {self.when(inc['at'], '%H:%M')}) · "
                f"<a href='incident?id={raw_id}&at={step(inc['at'] - span)}&win={win}'>← earlier</a> · "
                f"<a href='incident?id={raw_id}&at={step(inc['at'] + span)}&win={win}'>later →</a> · "
                f"<a href='device?id={raw_id}'>device page</a></p>")
        cls = {"bad": "bad", "warn": "warn", "info": "muted"}
        body += "<h2>Findings</h2><ul class='find'>" + "".join(
            f"<li><b>{hm(f['ts']) if f['ts'] else ''}</b> <span class='{cls[f['level']]}'>{e(f['text'])}</span></li>"
            for f in inc["findings"]) + "</ul>"
        sig = [(s["ts"], s["signal"]) for s in inc["samples"] if s["signal"] is not None]
        if len(sig) > 1:
            body += "<h2>Signal</h2><div class='chart'>" + svg_line(sig, -90, -30, unit="dBm", tz=self.tz) + "</div>"
        body += ("<h2>The device, every poll</h2><div class='wrap'><table><tr><th>Time</th><th>eero</th><th>Band</th>"
                 "<th class=n>Ch</th><th class=n>Signal</th><th class=n>SNR</th><th class=n>Bars</th><th class=n>Retry %</th>"
                 "<th class=n>rx Mbit/s</th><th class=n>tx Mbit/s</th></tr>")
        prev = None
        for s in inc["samples"]:
            moved = prev is not None and s["node_id"] != prev
            weak = s["signal"] is not None and s["signal"] < report.WEAK_SIGNAL_DBM
            body += (f"<tr><td>{hm(s['ts'])}</td><td class='{'warn' if moved else ''}'>{e(str(s['node']))}</td>"
                     f"<td>{e(s['band'] or '')}</td><td class=n>{s['channel'] or ''}</td>"
                     f"<td class='n {'bad' if weak else ''}'>{s['signal'] if s['signal'] is not None else ''}</td>"
                     f"<td class=n>{s['snr'] if s['snr'] is not None else ''}</td><td class=n>{s['score_bars'] or ''}</td>"
                     f"<td class=n>{s['tx_retry_pct'] if s['tx_retry_pct'] is not None else ''}</td>"
                     f"<td class=n>{s['rx_rate_mbps'] or ''}</td><td class=n>{s['tx_rate_mbps'] or ''}</td></tr>")
            prev = s["node_id"]
        body += "</table></div><p class='muted'>SNR 0 right after a connect usually means eero hasn't measured the link yet.</p>"
        body += "<h2>The device's events</h2>"
        if inc["events"]:
            body += "<ul class='find'>" + "".join(
                f"<li><b>{hm(x['ts'])}</b> {e(x['text'])} <span class='muted'>· {e(x['source'])}</span></li>"
                for x in inc["events"]) + "</ul>"
        else:
            body += "<p class='muted'>None.</p>"
        body += ("<h2>Its usual signal per eero (7 days)</h2><table><tr><th>eero</th><th>Band</th><th class=n>Usual signal</th></tr>"
                 + "".join(f"<tr><td>{e(str(u['node']))}</td><td>{e(u['band'] or '')}</td><td class=n>{u['dbm']} dBm</td></tr>"
                           for u in inc["usual"]) + "</table>")
        body += ("<h2>The mesh</h2><div class='wrap'><table><tr><th>eero</th><th>Online</th><th>Upstream</th>"
                 "<th>Clients</th><th>Radios (channel · busiest)</th></tr>")
        for n in inc["nodes"]:
            radios = " · ".join(f"{r['band']} GHz ch {'/'.join(map(str, r['channels']))} "
                                f"{r['max_util'] if r['max_util'] is not None else '—'} %" for r in n["radios"])
            online = "—" if n["online"] is None else "yes" if n["online"] else "<span class=bad>no</span>"
            clients = f"{n['clients'][0]}–{n['clients'][1]}" if n["clients"] else "—"
            body += (f"<tr><td>{e(n['node'])}</td><td>{online}</td><td>{e(' → '.join(n['upstreams']))}</td>"
                     f"<td>{clients}</td><td>{e(radios)}</td></tr>")
        body += "</table></div><h2>Mesh changes</h2>" + self.change_list(inc["mesh_changes"])
        near = {(o["ts"], o["device"]) for o in inc["nearby"]}
        body += f"<h2>Other devices ({len(inc['others'])})</h2>"
        if inc["others"]:
            body += "<div class='wrap'><table><tr><th>Time</th><th>Device</th><th>What</th><th>Source</th></tr>" + "".join(
                f"<tr><td>{hm(o['ts'])}</td><td class='{'warn' if (o['ts'], o['device']) in near else ''}'>{e(o['device'])}</td>"
                f"<td>{e(o['text'])}</td><td class=muted>{e(o['source'])}</td></tr>" for o in inc["others"]) + "</table></div>"
            body += (f"<p class='muted'>Highlighted: within {report.NEARBY_S // 60} min of this device's own changes. "
                     "Many devices changing together points at the mesh; this device alone points at its own link.</p>")
        else:
            body += "<p class='muted'>None.</p>"
        return body

    # --- login --------------------------------------------------------------------------------------
    def login(self, identifier):
        try:
            token = EeroClient(self.session).login(identifier.strip())
        except EeroError as err:
            self.log(f"eero login request failed: {err.error or err}")
            return self.status("eero refused that login. Check the email or phone number.")
        self.pending = (token, time.time())
        self.log("eero login started; waiting for the verification code")
        return self.status()

    def verify(self, code):
        if not self.pending or time.time() - self.pending[1] > 900:
            self.pending = None
            return self.status("The login timed out. Start again.")
        try:
            name = EeroClient(self.session).verify(self.pending[0], code.strip())
        except EeroError as err:
            self.log(f"eero verification failed: {err.error or err}")
            return self.status("eero didn't accept that code. Try again or start over.")
        self.pending = None
        self.log(f"eero login complete ({'account ' + name if name else 'ok'}); the collector resumes within 10 s")
        return self.status("Logged in. The collector resumes within a few seconds.")


def make_handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "EeroCollector"

        def log_message(self, *args):
            pass

        def guard(self):
            if self.client_address[0] != INGRESS_PROXY and not app_dev():
                self.send_error(403)
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            path, _, query = self.path.partition("?")
            q = urllib.parse.parse_qs(query)
            try:
                if path == "/":
                    self.reply(app.status())
                elif path == "/devices":
                    self.reply(app.devices())
                elif path == "/device" and q.get("id", [""])[0].isdigit():
                    out = app.device(int(q["id"][0]))
                    self.reply(out) if out else self.send_error(404)
                elif path == "/incident":
                    self.reply(app.incident(q))
                elif path == "/snapshot":
                    self.snapshot()
                else:
                    self.send_error(404)
            except Exception as err:  # a page error must never take the add-on down
                app.log(f"page {path} failed: {err}")
                self.send_error(500)

        def do_POST(self):
            if not self.guard():
                return
            length = min(int(self.headers.get("Content-Length") or 0), 4096)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            path = self.path.split("?", 1)[0]
            if path == "/login" and form.get("login"):
                self.reply(app.login(form["login"][0]))
            elif path == "/verify" and form.get("code"):
                self.reply(app.verify(form["code"][0]))
            else:
                self.send_error(400)

        def reply(self, body, content_type="text/html; charset=utf-8"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def snapshot(self):
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with app.snapshot_lock, tempfile.TemporaryDirectory(prefix="snapshot-") as tmp:
                copy = pathlib.Path(tmp) / "eero.db"
                src = sqlite3.connect(f"file:{app.db_path}?mode=ro", uri=True, timeout=10)
                dst = sqlite3.connect(copy)
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
                finally:
                    src.close()
                    dst.close()
                digest = hashlib.sha256(copy.read_bytes()).hexdigest()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.sqlite3")
                self.send_header("Content-Length", str(copy.stat().st_size))
                self.send_header("Content-Disposition", f'attachment; filename="eero-{stamp}.db"')
                self.send_header("X-Snapshot-Sha256", digest)
                self.end_headers()
                with copy.open("rb") as f:
                    for block in iter(lambda: f.read(CHUNK), b""):
                        self.wfile.write(block)
    return Handler


_DEV = {"on": False}


def app_dev():
    return _DEV["on"]


def run(db_path, session_store, timezone, log, port=PORT, dev=False):
    _DEV["on"] = dev  # dev: accept localhost (testing on the Mac); never set in the add-on
    app = App(db_path, session_store, timezone, log)
    server = http.server.ThreadingHTTPServer(("127.0.0.1" if dev else "0.0.0.0", port), make_handler(app))
    log(f"ingress page on port {port}")
    server.serve_forever()
