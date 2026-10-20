"""Mesh tests: controlled backhaul experiments (moving an eero, opening a cabinet) and what they measure.

Definitions (they decide what Home Assistant's "Mesh Testing" view shows):
- A session (a row in `experiments`) measures one satellite (`node`) from started_at to ended_at (or now).
  The first `stabilization_min` minutes are left out: the measurement window is [start + stabilization, end].
- Valid time: a node sample that has a reported upstream (or is wired) and a heartbeat covers the time until
  the next sample, or POLL seconds when the next one is more than GAP away (a missed poll). Missed polls, offline samples and samples without an upstream are not
  valid and are left out of every share; coverage = valid time / window length.
- Route shares: share of valid time per upstream eero; "direct" is the gateway. Band shares likewise, from eero's
  primary_mesh_radio (the band of the node's own link to its upstream).
- Upstream changes: consecutive valid samples whose upstream eero differs. Band-only changes are counted on their
  own and never as upstream changes. Per 24 h = changes / valid hours x 24. A change seen across a gap still
  counts, and is flagged.
- Latency comes from an outside prober (Home Assistant's Ping integration), stored in ping_samples: RTT median and
  p95 over the readings in the window; packet loss time-weighted (a reading holds until the next one); outages are
  the periods the prober reported the target unreachable. The internet is out when every internet target is.
- Preliminary: fewer than `preliminary_hours` valid hours. Low coverage: below `coverage_warn_pct`.
- Events that make a result suspect are listed with their times: reboots, firmware changes, 5/6 GHz channel
  changes, collector gaps, internet outages and eero's "offline" LED events.
eero's API has no backhaul RSSI, PHY rate or throughput; throughput is only what was entered by hand.

Everything above is read-only. The functions under "writes" are the only ones that change the database.
"""

import bisect
import datetime
import json
import statistics
from collections import Counter, defaultdict

POLL = 300
GAP = 450  # samples further apart than this mean at least one poll was missed
DEFAULTS = {"target_hours": 48, "stabilization_min": 60, "preliminary_hours": 24, "coverage_warn_pct": 90}
LIMITS = {"target_hours": (1, 720), "stabilization_min": (0, 1440), "preliminary_hours": (1, 720),
          "coverage_warn_pct": (0, 100)}
INTERNET = ("internet_google", "internet_cloudflare")
BASELINE = "BASELINE"


class MeshTestError(Exception):
    pass


def _all(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def _one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else {}


def percentile(values, q):
    v = sorted(values)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _r(x, n=1):
    return None if x is None else round(x, n)


def gateway_name(db):
    return _one(db, "SELECT location FROM nodes WHERE is_gateway = 1 ORDER BY last_seen_at DESC LIMIT 1").get("location")


def satellites(db):
    return [r["location"] for r in _all(db, "SELECT location FROM nodes WHERE NOT is_gateway ORDER BY location")]


# --- node samples -> valid time ------------------------------------------------------------------------------
def upstream_of(s):
    """The eero a node connects through ('wired' for wired backhaul), or None when not reported."""
    if s.get("connection_type") == "WIRED":
        return "wired"
    return s.get("upstream") or None


def band_of(s):
    """'2.4', '5' or '6' from eero's primary_mesh_radio ('6GHz'), None when wired or not reported."""
    r = s.get("upstream_radio")
    return r.replace("GHz", "").strip() if r and s.get("connection_type") != "WIRED" else None


def is_valid(s):
    return s.get("heartbeat_ok") != 0 and upstream_of(s) is not None


def node_samples(db, node, lo, hi):
    return _all(db, """SELECT s.* FROM node_samples s JOIN nodes n ON n.id = s.node_id
        WHERE n.location = ? AND s.ts BETWEEN ? AND ? ORDER BY s.ts""", node, lo, hi)


def segments(samples, lo, hi):
    """(start, end, sample) for the time each sample covers inside [lo, hi): until the next sample, or POLL seconds
    when the next sample is more than GAP away."""
    out = []
    for i, s in enumerate(samples):
        nxt = samples[i + 1]["ts"] if i + 1 < len(samples) else None
        end = nxt if nxt is not None and nxt - s["ts"] <= GAP else s["ts"] + POLL
        a, b = max(s["ts"], lo), min(end, hi)
        if b > a:
            out.append((a, b, s))
    return out


def routes(db, node, lo, hi, gateway=None, tz=datetime.timezone.utc):
    """Route statistics of one node over [lo, hi): valid time, shares, upstream changes, band-only changes."""
    gateway = gateway or gateway_name(db)
    samples = node_samples(db, node, lo - GAP, hi)
    window = max(hi - lo, 0)
    by_up, by_band, days = Counter(), Counter(), defaultdict(lambda: {"changes": 0, "valid_s": 0})
    valid_s = 0
    for a, b, s in segments(samples, lo, hi):
        if is_valid(s):
            valid_s += b - a
            by_up[upstream_of(s)] += b - a
            if band_of(s):
                by_band[band_of(s)] += b - a
            days[datetime.datetime.fromtimestamp(a, tz).strftime("%Y-%m-%d")]["valid_s"] += b - a
    changes, band_only, across_gap, last_change = 0, 0, 0, None
    prev = None
    for s in samples:
        if not is_valid(s):
            continue
        if prev is not None and lo < s["ts"] <= hi:
            gap = s["ts"] - prev["ts"] > GAP
            if upstream_of(s) != upstream_of(prev):
                changes += 1
                across_gap += gap
                last_change = s["ts"]
                days[datetime.datetime.fromtimestamp(s["ts"], tz).strftime("%Y-%m-%d")]["changes"] += 1
            elif band_of(s) != band_of(prev):
                band_only += 1
        prev = s
    valid_samples = sum(1 for s in samples if lo <= s["ts"] < hi and is_valid(s))
    pct = (lambda c: {k: round(100 * v / valid_s, 1) for k, v in c.most_common()}) if valid_s else (lambda c: {})
    return {
        "node": node, "gateway": gateway, "window_s": window, "valid_s": valid_s, "valid_hours": round(valid_s / 3600, 2),
        "coverage_pct": _r(100 * valid_s / window) if window else None, "valid_samples": valid_samples,
        "route_pct": pct(by_up), "direct_pct": _r(100 * by_up.get(gateway, 0) / valid_s) if valid_s else None,
        "band_pct": pct(by_band), "upstream_changes": changes, "band_only_changes": band_only,
        "changes_across_gaps": across_gap,
        "changes_per_24h": _r(changes / valid_s * 86400, 2) if valid_s else None,
        "last_change_in_window": last_change,
        "daily": [{"day": d, "changes": v["changes"], "valid_hours": round(v["valid_s"] / 3600, 2)}
                  for d, v in sorted(days.items())],
    }


def direct_share(db, node, lo, hi, gateway=None):
    """Share of valid time (%) the node connected directly to the gateway over [lo, hi), or None without data."""
    return routes(db, node, lo, hi, gateway)["direct_pct"]


def last_upstream_change(db, node, now, lookback=30 * 86400):
    """When the node last changed its upstream eero (band-only changes don't count), within `lookback`."""
    samples = [s for s in node_samples(db, node, now - lookback, now) if is_valid(s)]
    for prev, cur in zip(reversed(samples[:-1]), reversed(samples)):
        if upstream_of(prev) != upstream_of(cur):
            return cur["ts"]
    return None


# --- ping readings ---------------------------------------------------------------------------------------------
def readings(db, target, metric, lo, hi):
    """[(ts, value)] in [lo, hi] plus the last reading before lo (it still holds at lo). value None = unknown."""
    before = db.execute("""SELECT ts, value FROM ping_samples WHERE target = ? AND metric = ? AND ts < ?
        ORDER BY ts DESC LIMIT 1""", (target, metric, lo)).fetchall()
    rows = db.execute("""SELECT ts, value FROM ping_samples WHERE target = ? AND metric = ? AND ts BETWEEN ? AND ?
        ORDER BY ts""", (target, metric, lo, hi)).fetchall()
    return [tuple(r) for r in before] + [tuple(r) for r in rows]


def steps(rows, lo, hi):
    """(start, end, value) for a step function (each reading holds until the next) clipped to [lo, hi)."""
    out = []
    for i, (t, v) in enumerate(rows):
        end = rows[i + 1][0] if i + 1 < len(rows) else hi
        a, b = max(t, lo), min(end, hi)
        if b > a:
            out.append((a, b, v))
    return out


def outages(rows, lo, hi):
    """Periods within [lo, hi) the prober reported the target unreachable (up = 0), merged."""
    out = []
    for a, b, v in steps(rows, lo, hi):
        if v == 0:
            if out and out[-1][1] >= a:
                out[-1][1] = b
            else:
                out.append([a, b])
    return out


def _intersect(a, b):
    out, i, j = [], 0, 0
    while i < len(a) and j < len(b):
        s, e = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if e > s:
            out.append([s, e])
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def latency(db, lo, hi, node_segments=None, target="gateway"):
    """RTT, loss and outages of the prober's target over [lo, hi), plus RTT grouped by the node's upstream."""
    rtt_rows = [r for r in readings(db, target, "rtt", lo, hi) if r[0] >= lo and r[1] is not None]
    source = "readings"
    if not rtt_rows:  # raw readings already pruned: fall back to the permanent 5-minute medians
        rtt_rows = [tuple(r) for r in db.execute("""SELECT bucket, median FROM ping_5min WHERE target = ? AND metric = 'rtt'
            AND bucket BETWEEN ? AND ? AND median IS NOT NULL ORDER BY bucket""", (target, lo, hi)).fetchall()]
        source = "5-minute medians" if rtt_rows else None
    values = [v for _, v in rtt_rows]
    loss_steps = [(a, b, v) for a, b, v in steps(readings(db, target, "loss", lo, hi), lo, hi) if v is not None]
    loss_time = sum(b - a for a, b, _ in loss_steps)
    down = outages(readings(db, target, "up", lo, hi), lo, hi)
    internet = None
    for t in INTERNET:
        if db.execute("SELECT 1 FROM ping_samples WHERE target = ? AND metric = 'up' LIMIT 1", (t,)).fetchone():
            o = outages(readings(db, t, "up", lo, hi), lo, hi)
            internet = o if internet is None else _intersect(internet, o)
    by_up = defaultdict(list)
    if node_segments:
        starts = [a for a, _, _ in node_segments]
        for t, v in rtt_rows:
            i = bisect.bisect_right(starts, t) - 1
            if i >= 0 and t < node_segments[i][1] and is_valid(node_segments[i][2]):
                by_up[upstream_of(node_segments[i][2])].append(v)
    return {
        "target": target, "available": bool(values), "source": source, "rtt_n": len(values),
        "rtt_median_ms": _r(statistics.median(values), 2) if values else None,
        "rtt_p95_ms": _r(percentile(values, .95), 2) if values else None,
        "rtt_by_upstream": {k: {"n": len(v), "median_ms": _r(statistics.median(v), 2)} for k, v in sorted(by_up.items())},
        "loss_pct": _r(sum((b - a) * v for a, b, v in loss_steps) / loss_time, 2) if loss_time else None,
        "loss_events": sum(1 for _, _, v in loss_steps if v and v > 0),
        "outages": len(down), "outage_minutes": _r(sum(b - a for a, b in down) / 60),
        "outage_list": [[a, b] for a, b in down],
        "internet_outages": None if internet is None else len(internet),
        "internet_outage_minutes": None if internet is None else _r(sum(b - a for a, b in internet) / 60),
        "internet_outage_list": internet or [],
    }


# --- events that make a result suspect -------------------------------------------------------------------------
def events(db, lo, hi):
    out = []
    for r in _all(db, """SELECT DISTINCT n.location, s.last_reboot_at FROM node_samples s JOIN nodes n ON n.id = s.node_id
            WHERE s.last_reboot_at BETWEEN ? AND ?""", lo, hi):
        out.append({"ts": r["last_reboot_at"], "kind": "reboot", "text": f"{r['location']} restarted"})
    for r in _all(db, """SELECT location, ts, os, prev FROM (SELECT n.location, s.ts, s.os_version AS os,
            lag(s.os_version) OVER (PARTITION BY s.node_id ORDER BY s.ts) AS prev FROM node_samples s
            JOIN nodes n ON n.id = s.node_id WHERE s.ts BETWEEN ? AND ?) WHERE prev IS NOT NULL AND os IS NOT prev""",
                      lo - GAP, hi):
        if r["ts"] >= lo:
            out.append({"ts": r["ts"], "kind": "firmware", "text": f"{r['location']} firmware {r['prev']} → {r['os']}"})
    moves = defaultdict(list)  # a mesh-wide channel change shows on every eero: one event naming them all
    for r in _all(db, """SELECT location, band, ts, ch, prev FROM (SELECT n.location, r.band, r.ts, r.channel AS ch,
            lag(r.channel) OVER (PARTITION BY r.node_id, r.band ORDER BY r.ts) AS prev FROM radio_samples r
            JOIN nodes n ON n.id = r.node_id WHERE r.band IN ('5', '5L', '5H', '6') AND r.ts BETWEEN ? AND ?)
            WHERE prev IS NOT NULL AND ch IS NOT prev""", lo - GAP, hi):
        if r["ts"] >= lo:
            moves[(r["ts"], r["band"], r["prev"], r["ch"])].append(r["location"])
    for (t, band, prev, ch), where in sorted(moves.items()):
        out.append({"ts": t, "kind": "channel", "text": f"{band} GHz channel {prev} → {ch} ({', '.join(sorted(where))})"})
    ts = [r[0] for r in db.execute("SELECT ts FROM network_samples WHERE ts BETWEEN ? AND ? ORDER BY ts", (lo - GAP, hi))]
    for a, b in zip(ts, ts[1:]):
        if b - a > GAP and b > lo:
            out.append({"ts": a, "kind": "collector_gap", "text": f"no eero data for {round((b - a) / 60)} min"})
    for r in _all(db, "SELECT ts FROM network_samples WHERE ts BETWEEN ? AND ? AND isp_up = 0", lo, hi):
        out.append({"ts": r["ts"], "kind": "internet", "text": "eero reported the internet down"})
    for r in _all(db, """SELECT ts_ms / 1000 AS ts, description FROM events WHERE category = 'eero'
            AND ts_ms BETWEEN ? AND ?""", lo * 1000, hi * 1000):
        out.append({"ts": r["ts"], "kind": "eero", "text": r["description"]})
    return sorted(out, key=lambda e: e["ts"])


# --- a session's measurements ----------------------------------------------------------------------------------
def settings_of(raw):
    s = dict(DEFAULTS)
    s.update({k: v for k, v in (json.loads(raw) if isinstance(raw, str) else raw or {}).items() if k in DEFAULTS})
    return s


def metrics(db, node, start, end, settings, now, tz=datetime.timezone.utc):
    """Everything measured for one session (see the module docstring for definitions)."""
    settings = settings_of(settings)
    lo = start + int(settings["stabilization_min"] * 60)
    hi = min(end, now) if end else now
    gateway = gateway_name(db)
    base = {"node": node, "gateway": gateway, "start": start, "end": end, "measure_from": lo, "measure_to": hi,
            "settings": settings, "elapsed_hours": round((hi - start) / 3600, 2) if hi > start else 0,
            "last_upstream_change": last_upstream_change(db, node, now)}
    if hi <= lo:
        return {**base, "measuring": False, "flags": ["stabilizing"], "warnings": []}
    r = routes(db, node, lo, hi, gateway, tz)
    seg = segments(node_samples(db, node, lo - GAP, hi), lo, hi)
    lat = latency(db, lo, hi, seg)
    ev = events(db, lo, hi)
    others = {}
    for other in satellites(db):
        if other != node:
            o = routes(db, other, lo, hi, gateway, tz)
            others[other] = {k: o[k] for k in ("direct_pct", "route_pct", "upstream_changes", "changes_per_24h",
                                               "coverage_pct")}
    flags, warnings = [], []
    if r["valid_hours"] < settings["preliminary_hours"]:
        flags.append("preliminary")
        warnings.append(f"Preliminary: {r['valid_hours']:g} valid hours, fewer than {settings['preliminary_hours']:g}.")
    if r["coverage_pct"] is not None and r["coverage_pct"] < settings["coverage_warn_pct"]:
        flags.append("low_coverage")
        warnings.append(f"Coverage {r['coverage_pct']:g} % is below {settings['coverage_warn_pct']:g} %.")
    if r["changes_across_gaps"]:
        warnings.append(f"{r['changes_across_gaps']} upstream change(s) happened across a data gap.")
    if not lat["available"]:
        flags.append("no_latency")
        warnings.append("No latency readings in this window.")
    if lat["outages"] or lat["internet_outages"]:
        warnings.append(f"Ping outages: gateway {lat['outages']} ({lat['outage_minutes']:g} min), internet "
                        f"{lat['internet_outages'] or 0} ({lat['internet_outage_minutes'] or 0:g} min).")
    for kind, word in (("reboot", "restart"), ("firmware", "firmware change"), ("channel", "5/6 GHz channel change"),
                       ("collector_gap", "collector gap"), ("internet", "internet outage (eero)"),
                       ("eero", "eero status event")):
        n = sum(1 for e in ev if e["kind"] == kind)
        if n:
            warnings.append(f"{n} {word}{'s' if n > 1 else ''} during the measurement.")
            flags.append(kind)
    return {**base, "measuring": True, **r, "latency": lat, "events": ev, "others": others,
            "flags": flags, "warnings": warnings}


def series(db, node, lo, hi, target="gateway"):
    """What the session chart draws: node segments, 5-minute RTT medians and the running direct share."""
    gateway = gateway_name(db)
    seg = segments(node_samples(db, node, lo - GAP, hi), lo, hi)
    timeline = [{"start": a, "end": b, "upstream": upstream_of(s) if is_valid(s) else None,
                 "band": band_of(s) if is_valid(s) else None} for a, b, s in seg]
    rtt = [{"t": r[0] + POLL // 2, "ms": r[1]} for r in db.execute("""SELECT bucket, median FROM ping_5min
        WHERE target = ? AND metric = 'rtt' AND bucket BETWEEN ? AND ? AND median IS NOT NULL ORDER BY bucket""",
                                                                  (target, lo - POLL, hi))]
    return {"gateway": gateway, "timeline": timeline, "rtt": rtt}


# --- sessions ---------------------------------------------------------------------------------------------------
def _session(db, row):
    d = dict(row)
    d["settings"] = settings_of(d["settings"])
    d["summary"] = json.loads(d["summary"]) if d.get("summary") else None
    d["throughput"] = _all(db, "SELECT ts, down_mbps, up_mbps, note FROM experiment_throughput WHERE experiment_id = ? "
                               "ORDER BY ts", d["id"])
    return d


def sessions(db, kind=None):
    rows = db.execute("SELECT * FROM experiments" + (" WHERE kind = ?" if kind else "") + " ORDER BY started_at",
                      (kind,) if kind else ()).fetchall()
    return [_session(db, r) for r in rows]


def active(db):
    r = db.execute("SELECT * FROM experiments WHERE kind = 'trial' AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"
                   ).fetchone()
    return _session(db, r) if r else None


def session(db, code):
    r = db.execute("SELECT * FROM experiments WHERE code = ?", (code,)).fetchone()
    return _session(db, r) if r else None


def _meta(db, key, default):
    r = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def settings(db):
    return settings_of(_meta(db, "mesh_test_settings", {}))


def draft(db):
    """Values chosen before Start (scenario, notes, throughput fields)."""
    return _meta(db, "mesh_test_draft", {})


# --- writes -------------------------------------------------------------------------------------------------------
def _set_meta(db, key, value):
    with db:
        db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, json.dumps(value)))


def set_setting(db, key, value):
    if key not in DEFAULTS:
        raise MeshTestError(f"unknown setting {key}")
    lo, hi = LIMITS[key]
    s = settings(db)
    v = min(max(float(value), lo), hi)
    s[key] = int(v) if v == int(v) else v
    _set_meta(db, "mesh_test_settings", s)
    return s


def set_draft(db, **values):
    d = draft(db)
    d.update(values)
    _set_meta(db, "mesh_test_draft", d)
    return d


def start(db, scenario, name, node, now):
    """Starts a trial with the current settings and draft notes. Refuses while another trial is running."""
    if active(db):
        raise MeshTestError("a test is already running; end it first")
    n = db.execute("SELECT count(*) FROM experiments WHERE kind = 'trial'").fetchone()[0] + 1
    code = f"T{n:02d}-{scenario}"
    notes = draft(db).get("notes") or None
    with db:
        db.execute("""INSERT INTO experiments(kind, code, scenario, name, node, started_at, settings, notes, created_at,
            updated_at) VALUES('trial', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                   (code, scenario, name, node, now, json.dumps(settings(db)), notes, now, now))
    set_draft(db, notes="")
    return session(db, code)


def end(db, now, tz=datetime.timezone.utc):
    """Ends the running trial and freezes its measurements."""
    s = active(db)
    if not s:
        raise MeshTestError("no test is running")
    summary = metrics(db, s["node"], s["started_at"], now, s["settings"], now, tz)
    with db:
        db.execute("UPDATE experiments SET ended_at = ?, summary = ?, updated_at = ? WHERE id = ?",
                   (now, json.dumps(summary), now, s["id"]))
    return session(db, s["code"])


def set_notes(db, notes, now):
    """Notes go to the running trial, else they wait in the draft for the next Start."""
    s = active(db)
    if s:
        with db:
            db.execute("UPDATE experiments SET notes = ?, updated_at = ? WHERE id = ?", (notes or None, now, s["id"]))
    else:
        set_draft(db, notes=notes)


def add_throughput(db, down, up, now, note=None):
    """Attaches a hand-measured throughput result to the running trial, else to the last one that ended."""
    s = active(db) or next(iter(reversed(sessions(db, "trial"))), None)
    if not s:
        raise MeshTestError("no test to attach the throughput result to")
    with db:
        db.execute("INSERT INTO experiment_throughput(experiment_id, ts, down_mbps, up_mbps, note) VALUES(?, ?, ?, ?, ?)",
                   (s["id"], now, down, up, note))
    return s["code"]


def ensure_baseline(db, node, start, end, now, tz=datetime.timezone.utc):
    """Stores the historical (uncontrolled) baseline window, measured with the same definitions as a trial but
    without a stabilization period. Recomputed until it has latency data, then left alone."""
    s = session(db, BASELINE)
    window_same = s and (s["node"], s["started_at"], s["ended_at"]) == (node, start, end)
    if window_same and s["summary"] and s["summary"].get("latency", {}).get("available"):
        return s
    cfg = {**settings(db), "stabilization_min": 0}
    summary = metrics(db, node, start, end, cfg, now, tz)
    with db:
        if s:
            db.execute("""UPDATE experiments SET node = ?, started_at = ?, ended_at = ?, settings = ?, summary = ?,
                updated_at = ? WHERE id = ?""", (node, start, end, json.dumps(cfg), json.dumps(summary), now, s["id"]))
        else:
            db.execute("""INSERT INTO experiments(kind, code, scenario, name, node, started_at, ended_at, settings, notes,
                summary, created_at, updated_at) VALUES('baseline', ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                       (BASELINE, "Historical baseline (uncontrolled)", node, start, end, json.dumps(cfg),
                        "Normal operation before any controlled test; not a comparable trial.", json.dumps(summary),
                        now, now))
    return session(db, BASELINE)


def ingest_ping(db, target, metric, rows, now, retention_days=35):
    """Stores prober readings [(ts, value or None)], refreshes the 5-minute aggregates they touch and prunes
    readings older than `retention_days` (the aggregates stay)."""
    rows = sorted((int(t), v) for t, v in rows)
    if rows:
        with db:
            db.executemany("INSERT OR REPLACE INTO ping_samples VALUES(?, ?, ?, ?)",
                           [(target, metric, t, v) for t, v in rows])
        lo, hi = rows[0][0] // POLL * POLL, rows[-1][0] // POLL * POLL + POLL
        rollup(db, target, metric, lo, hi)
    with db:
        db.execute("DELETE FROM ping_samples WHERE target = ? AND metric = ? AND ts < ?",
                   (target, metric, now - retention_days * 86400))
    return len(rows)


def rollup(db, target, metric, lo, hi):
    """(Re)computes ping_5min for buckets in [lo, hi), in one pass over the readings."""
    rows = readings(db, target, metric, lo, hi)
    if metric == "rtt":
        by = defaultdict(list)
        for t, x in rows:
            if lo <= t < hi and x is not None:
                by[t // POLL * POLL].append(x)
        out = [(target, metric, b, len(v), statistics.fmean(v), statistics.median(v), max(v)) for b, v in sorted(by.items())]
    else:
        counts = Counter(t // POLL * POLL for t, _ in rows if lo <= t < hi)
        acc = defaultdict(lambda: [0, 0.0, None])  # seconds known, value x seconds, max
        for a, b, x in steps(rows, lo, hi):
            while x is not None and a < b:
                bucket = a // POLL * POLL
                end = min(b, bucket + POLL)
                cell = acc[bucket]
                cell[0] += end - a
                cell[1] += (end - a) * x
                cell[2] = x if cell[2] is None else max(cell[2], x)
                a = end
        out = [(target, metric, b, counts.get(b, 0), w / span, None, mx) for b, (span, w, mx) in sorted(acc.items()) if span]
    with db:
        db.executemany("INSERT OR REPLACE INTO ping_5min VALUES(?, ?, ?, ?, ?, ?, ?)", out)
