"""Read-only summaries of the database: what is true now, and series for charts.

Used by the Home Assistant add-on (MQTT sensors and the ingress pages), so the add-on presents what
this module decides. All functions take a connection (preferably read-only) and an explicit `now`.

Definitions:
- "Last hour" usage is eero's most recent *complete* hourly bucket; its average rate is
  bytes x 8 / 3600 s. eero reports no live per-device rate on the free tier, so this is the
  freshest accurate bandwidth figure.
- "Today" is the network's local calendar day, summed from hourly buckets (the current hour is partial).
- A "new device" is one whose eero first_seen is within the window, so a fresh collector database
  doesn't report every device as new.
- Weak signal: below WEAK_SIGNAL_DBM. Poor link: eero's score_bars <= POOR_BARS.
- Channel utilization is busy from UTIL_BUSY % and congested from UTIL_CONGESTED %.
- A node's upstream has two parts, kept apart: the eero it connects through (`upstream_eero`; an upstream
  change means this changed) and the band of that link (`backhaul_radio`, eero's primary_mesh_radio).
  `direct_to_gateway_24h` is the share of valid time in the last 24 h spent directly on the gateway
  (see meshtest for "valid").
"""

import datetime
import os

from . import meshtest, models

WEAK_SIGNAL_DBM = -70
POOR_BARS = 2
STALE_SECONDS = 900  # a device sample older than this isn't "current"
RECENT_TESTS = 10
RECENT_EVENTS = 15
CHOICE_DAYS = 30  # the device picker lists devices seen within this many days
UTIL_BUSY = 50  # channel utilization (% of airtime in use) from which a channel reads as busy
UTIL_CONGESTED = 75  # ... and congested: clients on it will notice


def _one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else {}


def _all(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def local_midnight(now, tz):
    d = datetime.datetime.fromtimestamp(now, tz)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def device_name(d):
    """eero's nickname, else its display name or hostname (without the DNS trailing dot), else the MAC."""
    name = d.get("nickname") or d.get("display_name") or d.get("hostname")
    return name.rstrip(".") if name else d.get("mac")


def network(db, now, tz, db_path=None):
    s = _one(db, "SELECT * FROM network_samples ORDER BY ts DESC LIMIT 1")
    last_hour = _one(db, "SELECT * FROM usage_network_hourly WHERE complete=1 ORDER BY hour DESC LIMIT 1")
    midnight = local_midnight(now, tz)
    today = _one(db, "SELECT sum(down) AS down, sum(up) AS up FROM usage_network_hourly WHERE hour >= ?", midnight)
    speed = _one(db, "SELECT * FROM speedtests ORDER BY ts DESC LIMIT 1")
    latest_ts = _one(db, "SELECT max(ts) AS ts FROM device_samples").get("ts")
    current = _all(db, """SELECT d.*, s.signal, s.score_bars FROM device_samples s JOIN devices d ON d.id = s.device_id
        WHERE s.ts = ?""", latest_ts) if latest_ts else []
    bands = {r["band"]: r["n"] for r in _all(db, """SELECT band, count(*) AS n FROM device_samples
        WHERE ts = ? AND band IS NOT NULL GROUP BY band""", latest_ts)} if latest_ts else {}
    weak = sorted((d for d in current if d["signal"] is not None and d["signal"] < WEAK_SIGNAL_DBM), key=lambda d: d["signal"])
    poor = [d for d in current if d["score_bars"] is not None and d["score_bars"] <= POOR_BARS]
    new = [d for d in _all(db, "SELECT * FROM devices WHERE eero_first_seen IS NOT NULL")
           if (models.ts(d["eero_first_seen"]) or 0) >= now - 86400]
    calls = _one(db, """SELECT sum(calls) AS calls, sum(CASE WHEN status NOT IN (200, 201) THEN calls ELSE 0 END) AS errors
        FROM api_calls_hourly WHERE hour >= ?""", now - 3600)
    state = db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
    backfill = {r["name"]: bool(r["done"]) for r in _all(db, "SELECT name, done FROM backfill")}
    caps = _all(db, "SELECT * FROM capabilities ORDER BY requires_premium, name")
    size = None
    if db_path:
        size = round(sum(os.path.getsize(f"{db_path}{x}") for x in ("", "-wal") if os.path.exists(f"{db_path}{x}")) / 1e6, 1)
    hour_s = 3600
    return {
        "sample_at": s.get("ts"),
        "status": s.get("status"), "internet_status": s.get("internet_status"), "isp_up": s.get("isp_up"),
        "mesh_status": s.get("mesh_status"), "online_eeros": s.get("online_eeros"),
        "offline_eeros": s.get("offline_eeros"), "clients_connected": s.get("clients_connected"),
        "clients_wireless": s.get("clients_wireless"), "clients_wired": s.get("clients_wired"),
        "clients_guest": s.get("clients_guest"), "premium_status": s.get("premium_status"),
        "last_hour_start": last_hour.get("hour"),
        "last_hour_down_mb": _mb(last_hour.get("down")), "last_hour_up_mb": _mb(last_hour.get("up")),
        "last_hour_down_mbps": _rate(last_hour.get("down"), hour_s), "last_hour_up_mbps": _rate(last_hour.get("up"), hour_s),
        "today_down_gb": _gb(today.get("down")), "today_up_gb": _gb(today.get("up")),
        "speedtest_at": speed.get("ts"), "speedtest_down_mbps": speed.get("down_mbps"), "speedtest_up_mbps": speed.get("up_mbps"),
        "speedtests": _all(db, "SELECT ts, down_mbps, up_mbps FROM speedtests ORDER BY ts DESC LIMIT ?", RECENT_TESTS),
        "clients_2_4": bands.get("2.4", 0) if current else None, "clients_5": bands.get("5", 0) if current else None,
        "clients_6": bands.get("6", 0) if current else None,
        "network_events": _all(db, """SELECT ts_ms / 1000 AS ts, description AS text FROM events WHERE category = 'Network'
            ORDER BY ts_ms DESC LIMIT ?""", RECENT_EVENTS),
        "weak_signal_count": len(weak) if current else None,
        "weak_signal": [f"{device_name(d)} ({d['signal']} dBm)" for d in weak[:20]],
        "poor_link_count": len(poor) if current else None,
        "poor_link": [device_name(d) for d in poor[:20]],
        "new_devices_24h": len(new), "new_devices": [device_name(d) for d in new[:20]],
        "collector_state": state[0].strip('"') if state else None,
        "api_calls_last_hour": calls.get("calls") or 0, "api_errors_last_hour": calls.get("errors") or 0,
        "database_mb": size, "backfill": backfill,
        "capabilities": {c["name"]: c["status"] for c in caps},
        "premium_locked": [c["name"] for c in caps if c["requires_premium"] and c["status"] == "not_entitled"],
    }


def upstream_changes(db, node_id, since):
    """How often a node's upstream (the eero it connects through, or wired) changed since `since`."""
    return db.execute("""SELECT count(*) FROM (SELECT upstream, lag(upstream) OVER (ORDER BY ts) AS prev, ts,
        lag(ts) OVER (ORDER BY ts) AS prev_ts FROM node_samples WHERE node_id = ?)
        WHERE prev_ts IS NOT NULL AND ts >= ? AND upstream IS NOT prev""", (node_id, since)).fetchone()[0]


def nodes(db, now=None):
    out = []
    for n in _all(db, "SELECT * FROM nodes ORDER BY is_gateway DESC, location"):
        s = _one(db, "SELECT * FROM node_samples WHERE node_id=? ORDER BY ts DESC LIMIT 1", n["id"])
        ts = s.get("ts")
        radios = _all(db, "SELECT * FROM radio_samples WHERE node_id=? AND ts=? ORDER BY band", n["id"], ts) if ts else []
        online = s.get("status") == "green" and s.get("heartbeat_ok") == 1 if s else None
        extra = {}
        if now is not None:
            extra = {"upstream_changes_24h": upstream_changes(db, n["id"], now - 86400),
                     "upstream_changes_7d": upstream_changes(db, n["id"], now - 7 * 86400),
                     "direct_to_gateway_24h": None if n["is_gateway"] else
                     meshtest.direct_share(db, n["location"], now - 86400, now)}
        parts = {"upstream_eero": meshtest.upstream_of(s) if s else None,
                 "backhaul_radio": meshtest.band_of(s) if s else None}
        out.append({**n, **{k: v for k, v in s.items() if k not in ("node_id", "os_version")},
                    "online": online, "radios": radios, **parts, **extra})
    return out


def utilization_level(pct):
    """'ok', 'busy' or 'congested' for a channel utilization percentage (None when unknown)."""
    if pct is None:
        return None
    return "congested" if pct >= UTIL_CONGESTED else "busy" if pct >= UTIL_BUSY else "ok"


def topology(nodes):
    """The mesh as a tree, from nodes(): each eero with the eero it connects through.

    Returns the nodes parents-first (depth-first, siblings by location) with `parent_id`, `depth` and `link`:
    'wired' (wired backhaul; eero doesn't say through which switch, so it hangs off the gateway), 'wireless'
    (to the eero named in `upstream`, over `link_band`) or 'unknown' (upstream not among the eeros, or a loop;
    drawn under the gateway). The gateway is a root with link None. Each radio gets a utilization `level`.
    """
    by_id = {n["id"]: n for n in nodes}
    by_location = {}
    for n in nodes:
        by_location.setdefault(n.get("location"), n["id"])
    gateway = next((n["id"] for n in nodes if n.get("is_gateway")), None)
    parent, link = {}, {}
    for n in nodes:
        nid = n["id"]
        if nid == gateway:
            parent[nid], link[nid] = None, None
        elif n.get("connection_type") == "WIRED":
            parent[nid], link[nid] = gateway, "wired"
        elif by_location.get(n.get("upstream")) not in (None, nid):
            parent[nid], link[nid] = by_location[n["upstream"]], "wireless"
        else:
            parent[nid], link[nid] = gateway, "unknown"
    for nid in parent:  # a loop in eero's upstream names would hide nodes from the tree: cut it at the gateway
        seen, p = {nid}, parent[nid]
        while p is not None and p not in seen:
            seen.add(p)
            p = parent[p]
        if p is not None:
            parent[nid], link[nid] = gateway, "unknown"
    children = {}
    for nid, p in parent.items():
        children.setdefault(p, []).append(nid)
    out = []

    def walk(nid, depth):
        n = by_id[nid]
        band = n.get("upstream_radio") if link[nid] == "wireless" else None
        out.append({**n, "parent_id": parent[nid], "depth": depth, "link": link[nid],
                    "link_band": band.replace("GHz", "").strip() if band else None,
                    "radios": [{**r, "level": utilization_level(r.get("utilization"))} for r in n.get("radios", [])]})
        for c in sorted(children.get(nid, []), key=lambda c: by_id[c].get("location") or ""):
            walk(c, depth + 1)

    for root in sorted(children.get(None, []), key=lambda r: (r != gateway, by_id[r].get("location") or "")):
        walk(root, 0)
    return out


def device_now(db, device_id, now, tz):
    d = _one(db, "SELECT * FROM devices WHERE id=?", device_id)
    if not d:
        return None
    s = _one(db, "SELECT * FROM device_samples WHERE device_id=? ORDER BY ts DESC LIMIT 1", device_id)
    if s and now - s["ts"] > STALE_SECONDS:
        s = {}
    node = _one(db, "SELECT location FROM nodes WHERE id=?", d.get("node_id")) if d.get("node_id") else {}
    midnight = local_midnight(now, tz)
    today = _one(db, "SELECT sum(down) AS down, sum(up) AS up FROM usage_device_hourly WHERE device_id=? AND hour >= ?",
                 device_id, midnight)
    last = _one(db, """SELECT u.* FROM usage_device_hourly u JOIN usage_network_hourly n ON n.hour = u.hour
        WHERE u.device_id=? AND n.complete=1 ORDER BY u.hour DESC LIMIT 1""", device_id)
    week = _one(db, "SELECT sum(down + up) AS b FROM usage_device_hourly WHERE device_id=? AND hour >= ?",
                device_id, now - 7 * 86400)
    counts = {f"{k}_{w}": db.execute("SELECT count(*) FROM device_changes WHERE device_id=? AND kind=? AND ts >= ?",
                                     (device_id, kind, now - secs)).fetchone()[0]
              for k, kind in (("drops", "disconnected"), ("roams", "node"))
              for w, secs in (("24h", 86400), ("7d", 7 * 86400))}
    return {**d, "name": device_name(d), "node": node.get("location"), "sample": s,
            "today_mb": _mb((today.get("down") or 0) + (today.get("up") or 0)) if today.get("down") is not None else 0.0,
            "last_hour_mb": _mb((last.get("down") or 0) + (last.get("up") or 0)) if last else 0.0,
            "week_mb": _mb(week.get("b") or 0), **counts}


def device_choices(db, now):
    """[(device id, label)] for a device picker: devices seen within CHOICE_DAYS, connected first, then by
    name. Labels are unique: a shared name gets the manufacturer (or type) and the end of the MAC."""
    rows = _all(db, """SELECT * FROM devices WHERE connected = 1 OR last_connected_at >= ? OR first_seen_at >= ?
        ORDER BY connected DESC, lower(coalesce(nullif(nickname, ''), nullif(display_name, ''), hostname, mac))""",
                now - CHOICE_DAYS * 86400, now - CHOICE_DAYS * 86400)
    names = [device_name(r) for r in rows]
    seen = {}
    for n in names:
        seen[n] = seen.get(n, 0) + 1
    out = []
    for r, n in zip(rows, names):
        label = n
        if seen[n] > 1:
            who = (r.get("manufacturer") or r.get("device_type") or "").strip()
            label = f"{n} ({who + ', ' if who else ''}…{(r.get('mac') or '')[-5:]})"
        out.append((r["id"], label))
    return out


def device_detail(db, device_id, now, tz):
    """device_now() plus what a one-device panel shows: the last 24 h of link samples summarized, daily
    usage for the last 14 days and the latest changes (node ids translated to eero locations)."""
    d = device_now(db, device_id, now, tz)
    if not d:
        return None
    loc = {str(r["id"]): r["location"] for r in _all(db, "SELECT id, location FROM nodes")}
    link = _one(db, """SELECT count(*) AS samples, round(avg(signal)) AS signal_avg, min(signal) AS signal_min,
        max(signal) AS signal_max, round(avg(tx_retry_pct), 1) AS retry_avg, round(avg(rx_rate_mbps)) AS rate_avg,
        group_concat(DISTINCT band) AS bands FROM device_samples WHERE device_id=? AND ts >= ?""", device_id, now - 86400)
    nodes_used = [loc.get(str(r["node_id"]), str(r["node_id"])) for r in
                  _all(db, "SELECT DISTINCT node_id FROM device_samples WHERE device_id=? AND ts >= ? AND node_id IS NOT NULL",
                       device_id, now - 86400)]
    daily = [{"day": r["day"], "down_mb": _mb(r["down"]), "up_mb": _mb(r["up"])} for r in
             _all(db, "SELECT day, down, up FROM usage_device_daily WHERE device_id=? ORDER BY day DESC LIMIT 14", device_id)]
    changes = []
    for c in _all(db, "SELECT ts, kind, old, new FROM device_changes WHERE device_id=? ORDER BY ts DESC LIMIT 20", device_id):
        if c["kind"] == "node":
            c["old"], c["new"] = loc.get(c["old"], c["old"]), loc.get(c["new"], c["new"])
        changes.append(c)
    return {**d, "link_24h": {**link, "nodes": nodes_used}, "daily": daily, "changes": changes}


def device_timeline(db, device_id, now, days=7, limit=40):
    """A device's recent connection events, newest first: [{ts, text, source}].

    Sources: eero's event feed ("eero": exact times; connects with the eero, band and channel, and disconnects),
    the 5-minute polls ("poll": roams between eeros, band and IP changes, and connects, disconnects or roams the
    feed didn't already report within 10 minutes) and channel changes on the same band seen between consecutive samples
    ("sample"; client samples are kept sample_retention_days)."""
    since = now - days * 86400
    loc = {str(r["id"]): r["location"] for r in _all(db, "SELECT id, location FROM nodes")}
    out, feed, joined = [], {"connected": [], "disconnected": []}, []
    for e in _all(db, """SELECT ts_ms / 1000 AS ts, action, node_location, band, channel FROM events
            WHERE ts_ms >= ? AND device_id = ? AND action IN ('connected', 'disconnected') ORDER BY ts_ms""",
                  since * 1000, device_id):
        feed[e["action"]].append(e["ts"])
        if e["action"] == "connected":
            joined.append((e["ts"], e["node_location"]))
            text = f"Connected to {e['node_location'] or 'an eero'}"
            if e["band"]:
                text += f" · {e['band']} GHz" + (f" ch {e['channel']}" if e["channel"] else "")
        else:
            text = f"Disconnected from {e['node_location'] or 'its eero'}"
        out.append({"ts": e["ts"], "text": text, "source": "eero"})
    for c in _all(db, """SELECT ts, kind, old, new FROM device_changes WHERE device_id = ? AND ts >= ? AND source = 'poll'
            AND kind IN ('node', 'band', 'ip', 'connected', 'disconnected') ORDER BY ts""", device_id, since):
        if c["kind"] == "node":
            new = loc.get(c["new"], c["new"])
            if any(abs(t - c["ts"]) <= 600 and where == new for t, where in joined):
                continue  # the feed's "Connected to <new eero>" already shows this roam
            text = f"Roamed {loc.get(c['old'], c['old'])} → {new}"
        elif c["kind"] == "band":
            text = f"Band {c['old']} → {c['new']} GHz"
        elif c["kind"] == "ip":
            text = f"IP address {c['old']} → {c['new']}"
        elif any(abs(t - c["ts"]) <= 600 for t in feed[c["kind"]]):
            continue  # eero's feed already has it, with the exact time
        else:
            text = "Connected" if c["kind"] == "connected" else "Disconnected"
        out.append({"ts": c["ts"], "text": text, "source": "poll"})
    prev = None
    for s in _all(db, "SELECT ts, band, channel FROM device_samples WHERE device_id = ? AND ts >= ? ORDER BY ts",
                  device_id, since):
        if (prev and s["ts"] - prev["ts"] <= 450 and s["band"] == prev["band"] and s["channel"] is not None
                and prev["channel"] is not None and s["channel"] != prev["channel"]):
            out.append({"ts": s["ts"], "text": f"Channel {prev['channel']} → {s['channel']} ({s['band']} GHz)",
                        "source": "sample"})
        prev = s
    return sorted(out, key=lambda e: e["ts"], reverse=True)[:limit]


def device_table(db, now, tz):
    """Every known device with its current state and usage, for the inventory page."""
    midnight = local_midnight(now, tz)
    latest = _one(db, "SELECT max(ts) AS ts FROM device_samples").get("ts") or 0
    rows = _all(db, """
        SELECT d.*, n.location AS node, s.signal, s.score_bars, s.tx_retry_pct, s.rx_rate_mbps,
          (SELECT sum(down + up) FROM usage_device_hourly u WHERE u.device_id = d.id AND u.hour >= ?) AS today_bytes,
          (SELECT sum(down + up) FROM usage_device_daily u WHERE u.device_id = d.id AND u.day >= ?) AS week_bytes,
          (SELECT count(*) FROM device_changes c WHERE c.device_id = d.id AND c.kind = 'disconnected' AND c.ts >= ?) AS drops_7d,
          (SELECT count(*) FROM device_changes c WHERE c.device_id = d.id AND c.kind = 'node' AND c.ts >= ?) AS roams_7d
        FROM devices d
        LEFT JOIN nodes n ON n.id = d.node_id
        LEFT JOIN device_samples s ON s.device_id = d.id AND s.ts = ?
        ORDER BY d.connected DESC, today_bytes DESC""",
                midnight, _day(now - 7 * 86400, tz), now - 7 * 86400, now - 7 * 86400, latest)
    for r in rows:
        r["name"] = device_name(r)
    return rows


def device_series(db, device_id, now, days=7):
    """Signal and link samples (raw where kept, hourly rollup before), hourly and daily usage, changes."""
    since = now - days * 86400
    return {
        "samples": _all(db, "SELECT * FROM device_samples WHERE device_id=? AND ts >= ? ORDER BY ts", device_id, since),
        "hourly_link": _all(db, "SELECT * FROM device_hourly WHERE device_id=? AND hour >= ? ORDER BY hour", device_id, since),
        "usage_hourly": _all(db, "SELECT * FROM usage_device_hourly WHERE device_id=? AND hour >= ? ORDER BY hour",
                             device_id, since),
        "usage_daily": _all(db, "SELECT * FROM usage_device_daily WHERE device_id=? ORDER BY day DESC LIMIT 120", device_id),
        "changes": _all(db, "SELECT * FROM device_changes WHERE device_id=? AND ts >= ? ORDER BY ts DESC LIMIT 200",
                        device_id, now - 30 * 86400),
        "events": _all(db, "SELECT * FROM events WHERE device_id=? ORDER BY ts_ms DESC LIMIT 100", device_id),
    }


STATISTICS = {
    # key: (name, unit, unit class, kind) - kind "sum" is a running total, "mean" an hourly mean/min/max.
    "network_download": ("eero network download", "GB", "information", "sum"),
    "network_upload": ("eero network upload", "GB", "information", "sum"),
    "speedtest_download": ("eero speed test download", "Mbit/s", "data_rate", "mean"),
    "speedtest_upload": ("eero speed test upload", "Mbit/s", "data_rate", "mean"),
}


def statistics(db, since=0):
    """Hourly series for Home Assistant's long-term statistics: {key: [row, ...]} with rows whose
    `start` (UTC hour epoch) is >= since.

    - network_download/upload: eero's complete hourly counters in GB, as a running total (`sum`) from
      the first hour with any traffic (eero answers zeros for days before its history starts, which
      aren't real). Sums always count from that first hour, so re-sending recent rows is safe.
    - speedtest_download/upload: eero's speed tests (gateway), mean/min/max of the tests in each hour.
    """
    out = {k: [] for k in STATISTICS}
    first = db.execute("SELECT min(hour) FROM usage_network_hourly WHERE down > 0 OR up > 0").fetchone()[0]
    if first is not None:
        total = {"down": 0, "up": 0}
        for r in db.execute("""SELECT hour, down, up FROM usage_network_hourly WHERE complete=1 AND hour >= ?
                ORDER BY hour""", (first,)):
            for d in total:
                total[d] += r[d]
            if r["hour"] >= since:
                out["network_download"].append({"start": r["hour"], "sum": round(total["down"] / 1e9, 4)})
                out["network_upload"].append({"start": r["hour"], "sum": round(total["up"] / 1e9, 4)})
    for d, key in (("down", "speedtest_download"), ("up", "speedtest_upload")):
        for r in db.execute(f"""SELECT ts / 3600 * 3600 AS hour, avg({d}_mbps) AS mean, min({d}_mbps) AS min,
                max({d}_mbps) AS max FROM speedtests WHERE {d}_mbps IS NOT NULL AND ts >= ? / 3600 * 3600
                GROUP BY 1 ORDER BY 1""", (since,)):
            out[key].append({"start": r["hour"], "mean": round(r["mean"], 2), "min": r["min"], "max": r["max"]})
    return out


def find_devices(db, keys):
    """Device ids for user-given names or MACs (case-insensitive; hostnames with or without the trailing dot).
    Unknown keys are returned separately."""
    found, missing = [], []
    for key in keys:
        k = key.strip().lower().rstrip(".")
        row = db.execute("""SELECT id FROM devices WHERE lower(mac)=? OR lower(rtrim(nickname, '.'))=?
            OR lower(rtrim(display_name, '.'))=? OR lower(rtrim(hostname, '.'))=?
            ORDER BY connected DESC, last_connected_at DESC LIMIT 1""", (k, k, k, k)).fetchone()
        (found if row else missing).append(row[0] if row else key)
    return found, missing


def _mb(b):
    return round(b / 1e6, 1) if b is not None else None


def _gb(b):
    return round(b / 1e9, 2) if b is not None else None


def _rate(b, seconds):
    return round(b * 8 / seconds / 1e6, 2) if b is not None else None


def _day(epoch, tz):
    return datetime.datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d")
