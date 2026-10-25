"""The add-on's ingress pages (Settings → Apps → Mammotion Collector → Open Web UI, or the sidebar).

  GET /            overview: condition, the last 7 and 30 days, recent jobs and stuck incidents
  GET /jobs        every job: sortable, searchable
  GET /job?n=N     one job: timeline (state, battery, Wi-Fi, eero), sessions, stuck incidents, errors
  GET /incident    "What happened?": a stuck incident (?i=N) or any moment (?at=YYYY-MM-DDTHH:MM&win=min):
                   findings, timeline, state changes, errors and eero readings
  GET /zones       per-zone figures, the zone settings, and each zone's jobs and incidents
  GET /snapshot    a consistent copy of the database (SQLite online backup from a read-only connection);
                   ?gzip=1 sends it gzip-compressed (about 1/15 the size), which gets through Nabu Casa
                   before its cutoff of about a minute

What the data means comes from mammotion-luba's collector.report (jobs, incidents, findings); this
module only lays it out. Only the Supervisor's ingress proxy may connect: Home Assistant authenticates
the user, so the server has no credentials. It never writes to the database or calls Mammotion.
"""
import datetime
import gzip
import hashlib
import shutil
import html
import http.server
import pathlib
import sqlite3
import tempfile
import threading
import urllib.parse

from collector import report
from collector.derive import STATE_LABELS

PORT = 8099  # ingress_port in config.yaml
INGRESS_PROXY = "172.30.32.2"
CHUNK = 1 << 16
WINDOWS = (30, 60, 120, 240)
e = html.escape

CSS = """
:root{--bg:#fff;--fg:#1c1c1c;--muted:#666;--line:#e3e3e3;--card:#f7f7f8;--accent:#0a7cff;--bad:#c62828;--ok:#2e7d32;--warn:#b26a00}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#9a9a9a;--line:#2b2b2b;--card:#1b1b1d;--accent:#4aa3ff;--bad:#ef5350;--ok:#66bb6a;--warn:#ffb74d}}
body{font:14px/1.45 system-ui,sans-serif;margin:0;background:var(--bg);color:var(--fg)}
main{max-width:1200px;margin:0 auto;padding:16px}
nav a{margin-right:16px;color:var(--accent);text-decoration:none;font-weight:600}
a{color:var(--accent)}
h1{font-size:20px;margin:8px 0 16px} h2{font-size:16px;margin:24px 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.card b{display:block;font-size:20px} .card span{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%} th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{cursor:pointer;user-select:none;position:sticky;top:0;background:var(--bg)} td.n,th.n{text-align:right}
.wrap{overflow-x:auto} .muted{color:var(--muted)} .ok{color:var(--ok)} .bad{color:var(--bad)} .warn{color:var(--warn)}
input[type=search],input[type=datetime-local],select{padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg);min-width:240px}
button{padding:8px 14px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;cursor:pointer}
ul.find{list-style:none;padding:0} ul.find li{padding:4px 0;border-bottom:1px solid var(--line)}
form.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
svg text{fill:var(--muted);font-size:10px} .chart{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px;margin:8px 0}
.legend span{display:inline-block;margin-right:12px;font-size:12px} .legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
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

# State colours for the timeline band (states from collector.derive / report.timeline).
STATE_COLORS = {"mowing": "#2e7d32", "returning": "#0a7cff", "paused_off_dock": "#c62828", "stuck_powered_off": "#7f0000",
                "docked_charging": "#8e99a8", "docked_charged": "#c5ccd6", "docked_waiting": "#a7b0bd", "mapping": "#7b1fa2",
                "idle_off_dock": "#b26a00", "offline": "#e0e0e0", "no_data": "#eeeeee", "other": "#ff9800"}
OUTCOMES = {"completed": ("✅ Completed", "ok"), "stuck_powered_off": ("⛔ Stuck, powered off", "bad"),
            "not_completed": ("✖ Not completed", "bad"), "pending": ("⏸ Waiting on the dock", "muted"),
            "in_progress": ("▶ Mowing", "ok"), "offline": ("❔ Went offline", "warn"), "other": ("❔ Other", "warn")}
LEVELS = {"bad": "bad", "warn": "warn", "ok": "ok", "info": "muted"}


def page(title, body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>{CSS}</style></head><body><main>"
            f"<nav><a href='./'>Overview</a><a href='jobs'>Jobs</a><a href='incident'>What happened?</a>"
            f"<a href='zones'>Zones</a></nav><h1>{e(title)}</h1>{body}</main><script>{SORT_JS}</script></body></html>").encode()


def epoch(ts):
    return datetime.datetime.fromisoformat(ts).timestamp()


def hm(minutes):
    if minutes is None:
        return "—"
    return f"{minutes:.0f} min" if minutes < 60 else f"{int(minutes // 60)} h {int(minutes % 60):02d} min"


def timeline_svg(view, tz, width=900):
    """The window as stacked strips on one time axis: state band, battery (0–100 %), Wi-Fi (−90…−30 dBm)
    and, with eero data, the eero node and band per reading."""
    t0, t1 = epoch(view["start"]), max(epoch(view["end"]), epoch(view["start"]) + 1)
    x = lambda ts: (epoch(ts) - t0) / (t1 - t0) * width  # noqa: E731
    lab = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%a %b %-d %H:%M")  # noqa: E731
    out, y = [], 0
    band = "".join(f"<rect x='{x(s['start']):.1f}' y='0' width='{max(x(s['end']) - x(s['start']), 0.5):.1f}' height='18' "
                   f"fill='{STATE_COLORS.get(s['state'], '#999')}'><title>{e(STATE_LABELS.get(s['state'], s['state']))}: "
                   f"{lab(epoch(s['start']))} → {lab(epoch(s['end']))}</title></rect>" for s in view["segments"])
    out.append(f"<g>{band}</g>")
    y = 26

    def line(key, lo, hi, unit, color, height=80):
        nonlocal y
        pts = [(x(p["at"]), p[key]) for p in view["points"] if p[key] is not None]
        if not pts:
            return
        path = " ".join(f"{'M' if i == 0 else 'L'}{px:.1f},{y + height - (min(max(v, lo), hi) - lo) / (hi - lo) * height:.1f}"
                        for i, (px, v) in enumerate(pts))
        out.append(f"<path d='{path}' fill='none' stroke='{color}' stroke-width='1.5'/>"
                   f"<text x='2' y='{y + 10}'>{hi} {unit}</text><text x='2' y='{y + height - 2}'>{lo} {unit}</text>")
        y += height + 8

    line("battery", 0, 100, "%", "var(--accent)")
    line("wifi", -90, -30, "dBm", "#8e99a8", 50)
    legend = ""
    if view["eero"]:
        keys = sorted({(n, b) for _, n, b, _ in view["eero"]}, key=str)
        palette = ["#0a7cff", "#2e7d32", "#b26a00", "#7b1fa2", "#c62828", "#00838f", "#5d4037"]
        color = {k: palette[i % len(palette)] for i, k in enumerate(keys)}
        out.append("".join(f"<rect x='{x(ts):.1f}' y='{y}' width='3' height='12' fill='{color[(n, b)]}'>"
                           f"<title>{lab(epoch(ts))} {e(str(n))} {e(str(b))} GHz {sig} dBm</title></rect>"
                           for ts, n, b, sig in view["eero"]))
        legend = "".join(f"<span><i style='background:{color[k]}'></i>{e(str(k[0]))} {e(str(k[1]))} GHz</span>" for k in keys)
        y += 20
    out.append(f"<text x='2' y='{y + 10}'>{lab(t0)}</text><text x='{width - 2}' y='{y + 10}' text-anchor='end'>{lab(t1)}</text>")
    states = []
    for s in view["segments"]:
        if s["state"] not in states:
            states.append(s["state"])
    legend = "".join(f"<span><i style='background:{STATE_COLORS.get(s, '#999')}'></i>{e(STATE_LABELS.get(s, s))}</span>"
                     for s in states) + (f"<br>eero: {legend}" if legend else "")
    return (f"<div class='chart'><svg viewBox='0 0 {width} {y + 14}' width='100%' preserveAspectRatio='none'>{''.join(out)}</svg>"
            f"<div class='legend'>{legend}</div></div>")


class App:
    def __init__(self, db_path, analysis, log):
        self.db_path, self.analysis, self.log = db_path, analysis, log
        self.tz = analysis.tz
        self.snapshot_lock = threading.Lock()  # one snapshot at a time

    def when(self, ts, fmt="%a %b %-d, %H:%M"):
        return e(datetime.datetime.fromisoformat(ts).astimezone(self.tz).strftime(fmt)) if ts else "—"

    def result(self):
        result, meta = self.analysis.get()
        return result, meta

    def ro(self):
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def view(self, start, end):
        """report.window_view() for start..end, with eero readings when the zones name the mower's eero device."""
        db = self.ro()
        try:
            device = db.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()
            win = report.load_window(db, device["id"], start, end)
        finally:
            db.close()
        zones = report.load_zones(self.analysis.zones_path) if self.analysis.zones_path else {}
        eero = None
        if self.analysis.eero_db and zones.get("eero_device") and pathlib.Path(self.analysis.eero_db).exists():
            eero = report.EeroSamples(report.load_eero_samples(self.analysis.eero_db, zones["eero_device"], start, end))
        return report.window_view(win, eero)

    def unavailable(self, title, meta):
        return page(title, f"<p class='warn'>The analysis isn't available: {e(str(meta.get('error', 'no data yet')))}</p>")

    # --- pages ------------------------------------------------------------------------------------
    def overview(self):
        result, meta = self.result()
        if result is None:
            return self.unavailable("LUBA mower", meta)
        s = report.summary(result, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        w7, w30, last = s["last_7_days"], s["last_30_days"], s["last_incident"]
        cards = [("Mowing, 7 days", f"{w7['mowing_hours']} h"),
                 ("Jobs completed, 30 days", f"{w30['completion_pct']} %" if w30["completion_pct"] is not None else "—"),
                 ("Stuck incidents, 7 / 30 days", f"{w7['incidents']} / {w30['incidents']}"),
                 ("Powered off in the yard, 30 days", f"{w30['off_in_yard_hours']} h"),
                 ("Last stuck", self.when(last["started_at"], "%a %b %-d") if last else "never"),
                 ("Mowing per charge", f"{s['mowing_minutes_per_charge']} min" if s["mowing_minutes_per_charge"] else "—")]
        body = "<div class='cards'>" + "".join(f"<div class='card'><b>{v}</b><span>{e(k)}</span></div>" for k, v in cards) + "</div>"
        body += "<h2>Recent jobs</h2>" + self.job_table(list(reversed(result["jobs"]))[:5])
        body += "<h2>Recent stuck incidents</h2>" + self.incident_table(result["incidents"], last_n=5)
        body += (f"<h2>Data</h2><p class='muted'>Analysis {self.when(meta['analyzed_at'])} · "
                 f"{meta.get('eero_samples') or 'no'} eero readings · {meta.get('zone_fingerprints')} zone fingerprint(s), "
                 f"{meta.get('zone_labels')} label(s) · times in {e(meta.get('timezone', ''))}</p>"
                 "<p><a href='snapshot?gzip=1'>Download a database snapshot</a> (a consistent copy, made while the collector "
                 "keeps running; gzip-compressed, <code>gunzip</code> it to get the .db file).</p>")
        return page("LUBA mower", body)

    def job_table(self, jobs, sortable=False):
        if not jobs:
            return "<p class='muted'>None yet.</p>"
        rows = ""
        for j in jobs:
            label, cls = OUTCOMES.get(j["outcome"], (j["outcome"], ""))
            zone = e(j["zone"]) + (" ✎" if j["zone_source"] == "user" else "") if j["zone"] else "<span class=muted>—</span>"
            rows += (f"<tr><td class=n data-v='{j['job']}'><a href='job?n={j['job']}'>{j['job']}</a></td>"
                     f"<td data-v='{e(j['started_at'])}'>{self.when(j['started_at'])}</td><td>{zone}</td>"
                     f"<td class=n data-v='{j['mowing_minutes']}'>{hm(j['mowing_minutes'])}</td>"
                     f"<td class=n data-v='{j['stuck_minutes']}'>{hm(j['stuck_minutes']) if j['stuck_minutes'] else '—'}</td>"
                     f"<td class=n>{len(j['session_ids']) - 1}</td><td class=n>{j['battery_used']} %</td>"
                     f"<td class=n>{j['drain_pct_per_min'] if j['drain_pct_per_min'] is not None else '—'}</td>"
                     f"<td class=n>{j['incidents'] or ''}</td><td class='{cls}'>{e(label)}</td></tr>")
        return (f"<div class='wrap'><table class='{'sort' if sortable else ''}'><thead><tr><th class=n>#</th><th>Started</th>"
                "<th>Zone</th><th class=n>Mowing</th><th class=n>Stuck</th><th class=n>Recharges</th><th class=n>Battery</th>"
                "<th class=n>Drain %/min</th><th class=n>Stuck incidents</th><th>Outcome</th></tr></thead>"
                f"<tbody>{rows}</tbody></table></div>")

    def incident_table(self, incidents, last_n=None, only=None):
        items = [(i, x) for i, x in enumerate(incidents) if only is None or i in only]
        items = items[-last_n:] if last_n else items
        if not items:
            return "<p class='muted'>None.</p>"
        rows = "".join(
            f"<tr><td><a href='incident?i={i}'>{self.when(x['started_at'])}</a></td><td class=n>{hm(x['minutes'])}</td>"
            f"<td>{e(report.BEFORE_WORDS.get(x['before'], x['before'] or ''))}</td>"
            f"<td class='{'bad' if x['powered_off'] else 'ok'}'>{'powered itself off' if x['powered_off'] else 'freed'}</td>"
            f"<td>{e(str(x.get('eero_node') or '—'))}{(' ' + e(str(x['eero_band'])) + ' GHz, ' + str(x['eero_signal']) + ' dBm') if x.get('eero_node') else ''}</td>"
            f"<td>{e(', '.join(map(str, x['faults'])) or '—')}</td></tr>" for i, x in reversed(items))
        return ("<div class='wrap'><table><tr><th>Started</th><th class=n>Stuck for</th><th>While</th><th>Then</th>"
                f"<th>Where (eero)</th><th>Faults</th></tr>{rows}</table></div>")

    def jobs(self):
        result, meta = self.result()
        if result is None:
            return self.unavailable("Jobs", meta)
        body = ("<p class='muted'>A job is one run of a mowing task, from its start until it finishes, including breaks to "
                "recharge. ✎ marks a zone you labeled; other zones come from the eero node and band the mower used while "
                "mowing. Click a column to sort.</p><input type='search' id='q' placeholder='Filter…'>")
        return page("Jobs", body + self.job_table(list(reversed(result["jobs"])), sortable=True))

    def job(self, n):
        result, meta = self.result()
        if result is None:
            return self.unavailable("Job", meta)
        job = next((j for j in result["jobs"] if j["job"] == n), None)
        if job is None:
            return None
        end = job["ended_at"] or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        view = self.view(report._plus(job["started_at"], -900), report._plus(end, 900))
        label, cls = OUTCOMES.get(job["outcome"], (job["outcome"], ""))
        zone = (f"{e(job['zone'])} ({'your label' if job['zone_source'] == 'user' else 'from eero'})" if job["zone"]
                else "zone unknown")
        if job["eero_zone"]:
            zone += f" · eero vote: {e(job['eero_zone'])} {round(100 * job['eero_zone_share'])} % of {job['eero_samples']} readings"
        nav = " · ".join(f"<a href='job?n={k}'>{'← job ' if k < n else 'job '}{k}{'' if k < n else ' →'}</a>"
                         for k in (n - 1, n + 1) if any(j["job"] == k for j in result["jobs"]))
        cards = [("Outcome", f"<span class='{cls}'>{e(label)}</span>"), ("Mowing", hm(job["mowing_minutes"])),
                 ("Stuck", hm(job["stuck_minutes"]) if job["stuck_minutes"] else "—"),
                 ("Recharges", len(job["session_ids"]) - 1), ("Battery used", f"{job['battery_used']} %"),
                 ("Drain while mowing", f"{job['drain_pct_per_min']} %/min" if job["drain_pct_per_min"] is not None else "—")]
        body = (f"<p>{self.when(job['started_at'])} → {self.when(job['ended_at']) if job['ended_at'] else 'now'} · {zone}"
                f"{' · ' + nav if nav else ''}</p><div class='cards'>"
                + "".join(f"<div class='card'><b>{v}</b><span>{e(k)}</span></div>" for k, v in cards) + "</div>")
        body += "<h2>Timeline</h2>" + timeline_svg(view, self.tz)
        db = self.ro()
        try:
            marks = ",".join("?" * len(job["session_ids"]))
            sessions = db.execute(f"SELECT * FROM mowing_sessions WHERE id IN ({marks}) ORDER BY started_at",
                                  job["session_ids"]).fetchall()
        finally:
            db.close()
        body += ("<h2>Sessions</h2><p class='muted'>The job's mowing stretches, between breaks on the dock.</p>"
                 "<div class='wrap'><table><tr><th>Started</th><th>Ended</th><th class=n>Minutes</th><th class=n>Battery</th>"
                 "<th>Ended because</th></tr>" + "".join(
                     f"<tr><td>{self.when(s['started_at'])}</td><td>{self.when(s['ended_at'])}</td>"
                     f"<td class=n>{round((s['elapsed_seconds'] or 0) / 60)}</td>"
                     f"<td class=n>{s['start_battery']} → {s['end_battery']} %</td><td>{e(str(s['end_reason'] or 'still open'))} "
                     f"<span class=muted>({e(str(s['end_status'] or ''))})</span></td></tr>" for s in sessions) + "</table></div>")
        mine = [i for i, x in enumerate(result["incidents"])
                if job["started_at"] <= x["started_at"] <= (job["ended_at"] or "9999")]
        body += "<h2>Stuck incidents</h2>" + self.incident_table(result["incidents"], only=set(mine))
        body += "<h2>Errors</h2>" + self.error_list(view["errors"])
        return page(f"Job {n}" + (f": {job['zone']}" if job["zone"] else ""), body)

    def error_list(self, errors):
        if not errors:
            return "<p class='muted'>None.</p>"
        return "<ul class='find'>" + "".join(
            f"<li><b>{self.when(x['occurred_at'], '%H:%M')}</b> {x['code']} {e(x['implication'] or '')} "
            f"<span class='{'muted' if x['category'] in ('routine', 'schedule', 'weather') else 'bad'}'>· {e(x['category'])}</span></li>"
            for x in errors) + "</ul>"

    def incident(self, q):
        result, meta = self.result()
        if result is None:
            return self.unavailable("What happened?", meta)
        incidents = result["incidents"]
        raw_i, raw_at = q.get("i", [""])[0], q.get("at", [""])[0]
        win = int(q["win"][0]) if q.get("win", [""])[0] in {str(w) for w in WINDOWS} else 60
        opts = "".join(f"<option value='{i}'{' selected' if str(i) == raw_i else ''}>{self.when(x['started_at'])} · "
                       f"{e(hm(x['minutes']))}{' · powered off' if x['powered_off'] else ''}</option>"
                       for i, x in reversed(list(enumerate(incidents))))
        wins = "".join(f"<option value='{w}'{' selected' if w == win else ''}>± {w} min</option>" for w in WINDOWS)
        body = ("<p class='muted'>Pick a stuck incident, or any moment (when it stopped somewhere, went quiet or did "
                "something odd). This lines up its states, battery, Wi-Fi, eero readings, state changes and error records "
                "around it.</p>"
                f"<form class='row' method='get' action='incident'><select name='i'><option value=''>Stuck incident…</option>"
                f"{opts}</select><button>Show</button></form><p></p>"
                f"<form class='row' method='get' action='incident'><input type='datetime-local' name='at' value='{e(raw_at[:16])}' "
                f"required><select name='win'>{wins}</select><button>Show this moment</button></form>")
        index, start, end, title = None, None, None, "What happened?"
        if raw_i.isdigit() and int(raw_i) < len(incidents):
            index = int(raw_i)
            x = incidents[index]
            start, end = report._plus(x["started_at"], -1800), report._plus(x["last_seen"], 1800)
            title = f"What happened? Stuck {self.when(x['started_at'])}"
        elif raw_at:
            try:
                at = datetime.datetime.strptime(raw_at[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=self.tz)
            except ValueError:
                return page(title, body + "<p class='warn'>That time wasn't understood.</p>")
            utc = at.astimezone(datetime.timezone.utc).isoformat(timespec="seconds")
            start, end = report._plus(utc, -win * 60), report._plus(utc, win * 60)
            title = f"What happened? {self.when(utc)}"
        if start is None:
            return page(title, body)
        view = self.view(start, end)
        found = [index] if index is not None else [
            i for i, x in enumerate(incidents) if x["started_at"] <= end and (x["back_at"] or x["last_seen"]) >= start]
        body += "<h2>Findings</h2><ul class='find'>"
        for i in found:
            body += "".join(f"<li><b>{self.when(f['at'], '%a %H:%M') if f['at'] else ''}</b> "
                            f"<span class='{LEVELS[f['level']]}'>{e(f['text'])}</span></li>"
                            for f in report.incident_findings(incidents, i, result["jobs"]))
        if not found:
            spent = {}
            for s in view["segments"]:
                spent[s["state"]] = spent.get(s["state"], 0) + (epoch(s["end"]) - epoch(s["start"])) / 60
            body += "<li class='muted'>No stuck incident in this window. Time in each state: " + e(", ".join(
                f"{STATE_LABELS.get(k, k).lower()} {hm(v)}" for k, v in sorted(spent.items(), key=lambda kv: -kv[1]))) + "</li>"
        job = next((j for j in result["jobs"] if j["started_at"] <= end and (j["ended_at"] or "9999") >= start), None)
        body += "</ul>" + (f"<p>During <a href='job?n={job['job']}'>job {job['job']}</a>"
                           f"{' (' + e(job['zone']) + ')' if job['zone'] else ''}.</p>" if job else "")
        body += "<h2>Timeline</h2>" + timeline_svg(view, self.tz)
        body += "<h2>State changes</h2>" + ("<ul class='find'>" + "".join(
            f"<li><b>{self.when(x['observed_at'], '%H:%M:%S')}</b> {e(x['event_type'].replace('_', ' '))}: "
            f"<code>{e(str(x['old_value']))}</code> → <code>{e(str(x['new_value']))}</code></li>"
            for x in view["events"]) + "</ul>" if view["events"] else "<p class='muted'>None.</p>")
        body += "<h2>Errors</h2>" + self.error_list(view["errors"])
        if view["eero"]:
            body += ("<h2>eero readings</h2><div class='wrap'><table><tr><th>Time</th><th>eero</th><th>Band</th>"
                     "<th class=n>Signal</th></tr>" + "".join(
                         f"<tr><td>{self.when(ts, '%H:%M')}</td><td>{e(str(n))}</td><td>{e(str(b))} GHz</td>"
                         f"<td class=n>{sig if sig is not None else '—'} dBm</td></tr>" for ts, n, b, sig in view["eero"])
                     + "</table></div><p class='muted'>eero reports every 5 minutes and sometimes repeats a stale value. "
                       "A flat signal while stuck is the mower sitting still.</p>")
        return page(title, body)

    def zones(self):
        result, meta = self.result()
        if result is None:
            return self.unavailable("Zones", meta)
        s = report.summary(result, datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"))
        body = ("<div class='wrap'><table><tr><th>Zone</th><th class=n>Jobs</th><th class=n>Completed</th>"
                "<th class=n>Stuck incidents</th><th class=n>Typical run</th><th class=n>Typical battery</th><th>Last mowed</th></tr>"
                + "".join(f"<tr><td>{e(name)}</td><td class=n>{z['jobs']}</td><td class=n>{z['completed']}</td>"
                          f"<td class=n>{z['incidents']}</td><td class=n>{hm(z['mowing_minutes_median'])}</td>"
                          f"<td class=n>{z['battery_used_median'] if z['battery_used_median'] is not None else '—'} %</td>"
                          f"<td>{self.when(z['last_mowed'], '%a %b %-d')}</td></tr>" for name, z in s["zones"].items())
                + "</table></div><p class='muted'>Typical run and battery are medians over completed jobs.</p>")
        unknown = [j for j in result["jobs"] if not j["zone"]]
        for name in list(s["zones"]) + (["Zone unknown"] if unknown else []):
            jobs = unknown if name == "Zone unknown" else [j for j in result["jobs"] if j["zone"] == name]
            ids = {i for i, x in enumerate(result["incidents"])
                   for j in jobs if j["started_at"] <= x["started_at"] <= (j["ended_at"] or "9999")}
            body += (f"<h2>{e(name)}</h2>" + self.job_table(list(reversed(jobs)))
                     + ("<p><b>Stuck incidents</b></p>" + self.incident_table(result["incidents"], only=ids) if ids else ""))
        zones = report.load_zones(self.analysis.zones_path) if self.analysis.zones_path else {}
        fps = zones.get("fingerprints") or []
        labels = zones.get("labels") or {}
        body += ("<h2>Zone settings</h2><p class='muted'>From the add-on's options (Settings → Apps → Mammotion Collector → "
                 "Configuration). A label wins over eero; an eero zone needs at least "
                 f"{round(100 * report.MIN_ZONE_SHARE)} % of a job's mowing-time readings to agree.</p>"
                 f"<p>eero device: <b>{e(str(zones.get('eero_device') or '—'))}</b></p>"
                 "<table><tr><th>eero node</th><th>Band</th><th>Zone</th></tr>"
                 + "".join(f"<tr><td>{e(str(f['node']))}</td><td>{e(str(f['band']))} GHz</td><td>{e(f['zone'])}</td></tr>"
                           for f in fps) + "</table><p></p><table><tr><th>Labeled job start</th><th>Zone</th></tr>"
                 + "".join(f"<tr><td>{self.when(ts)}</td><td>{e(z)}</td></tr>" for ts, z in sorted(labels.items()))
                 + "</table>")
        return page("Zones", body)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def make_handler(app, dev=False):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "MammotionCollector"

        def log_message(self, *args):
            pass  # snapshots and failures are logged below; page loads aren't worth a line

        def do_GET(self):
            if self.client_address[0] != INGRESS_PROXY and not dev:
                self.send_error(403)
                return
            path, _, query = self.path.partition("?")
            q = urllib.parse.parse_qs(query)
            try:
                if path == "/":
                    self.reply(app.overview())
                elif path == "/jobs":
                    self.reply(app.jobs())
                elif path == "/job" and q.get("n", [""])[0].isdigit():
                    out = app.job(int(q["n"][0]))
                    self.reply(out) if out else self.send_error(404)
                elif path == "/incident":
                    self.reply(app.incident(q))
                elif path == "/zones":
                    self.reply(app.zones())
                elif path == "/snapshot":
                    self.snapshot(compress=q.get("gzip", [""])[0] == "1")
                else:
                    self.send_error(404)
            except Exception as err:  # a page error must never take the add-on down
                app.log(f"page {path} failed: {type(err).__name__}: {err}")
                self.send_error(500)

        def reply(self, body, content_type="text/html; charset=utf-8"):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def snapshot(self, compress=False):
            """X-Snapshot-Sha256 is always the database's own SHA-256; a compressed download adds
            X-Snapshot-Gzip-Sha256 for the file as sent."""
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            with app.snapshot_lock, tempfile.TemporaryDirectory(prefix="snapshot-") as tmp:
                copy = pathlib.Path(tmp) / "mammotion.db"
                try:
                    src = sqlite3.connect(f"file:{app.db_path}?mode=ro", uri=True, timeout=10)
                    dst = sqlite3.connect(copy)
                    try:
                        src.backup(dst)
                    finally:
                        src.close()
                        dst.close()
                except sqlite3.Error as err:
                    app.log(f"snapshot failed: {err}")
                    self.send_error(500, "snapshot failed")
                    return
                digest = sha256(copy)
                sent, name, kind = copy, f"mammotion-{stamp}.db", "application/vnd.sqlite3"
                if compress:
                    sent, name, kind = copy.with_suffix(".db.gz"), name + ".gz", "application/gzip"
                    with copy.open("rb") as f, gzip.open(sent, "wb", compresslevel=6) as g:
                        shutil.copyfileobj(f, g, 1 << 20)
                size = sent.stat().st_size
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(size))
                self.send_header("Content-Disposition", f'attachment; filename="{name}"')
                self.send_header("X-Snapshot-Sha256", digest)
                if compress:
                    self.send_header("X-Snapshot-Gzip-Sha256", sha256(sent))
                self.end_headers()
                with sent.open("rb") as f:
                    for block in iter(lambda: f.read(CHUNK), b""):
                        self.wfile.write(block)
            app.log(f"served snapshot ({size // 1024} KB{', gzip' if compress else ''})")

    return Handler


def run(db_path, analysis, log, port=PORT, dev=False):
    """dev: listen on localhost only and accept it (testing on the Mac); never set in the add-on."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1" if dev else "0.0.0.0", port),
                                             make_handler(App(db_path, analysis, log), dev))
    server.daemon_threads = True
    log(f"ingress pages and snapshots on port {port}" + (" (dev: localhost)" if dev else " (ingress only)"))
    server.serve_forever()
