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
"""

import datetime
import os

from . import models

WEAK_SIGNAL_DBM = -70
POOR_BARS = 2
STALE_SECONDS = 900  # a device sample older than this isn't "current"


def _one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else {}


def _all(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def local_midnight(now, tz):
    d = datetime.datetime.fromtimestamp(now, tz)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def device_name(d):
    return d.get("nickname") or d.get("display_name") or d.get("hostname") or d.get("mac")


def network(db, now, tz, db_path=None):
    s = _one(db, "SELECT * FROM network_samples ORDER BY ts DESC LIMIT 1")
    last_hour = _one(db, "SELECT * FROM usage_network_hourly WHERE complete=1 ORDER BY hour DESC LIMIT 1")
    midnight = local_midnight(now, tz)
    today = _one(db, "SELECT sum(down) AS down, sum(up) AS up FROM usage_network_hourly WHERE hour >= ?", midnight)
    speed = _one(db, "SELECT * FROM speedtests ORDER BY ts DESC LIMIT 1")
    latest_ts = _one(db, "SELECT max(ts) AS ts FROM device_samples").get("ts")
    current = _all(db, """SELECT d.*, s.signal, s.score_bars FROM device_samples s JOIN devices d ON d.id = s.device_id
        WHERE s.ts = ?""", latest_ts) if latest_ts else []
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


def nodes(db):
    out = []
    for n in _all(db, "SELECT * FROM nodes ORDER BY is_gateway DESC, location"):
        s = _one(db, "SELECT * FROM node_samples WHERE node_id=? ORDER BY ts DESC LIMIT 1", n["id"])
        ts = s.get("ts")
        radios = _all(db, "SELECT * FROM radio_samples WHERE node_id=? AND ts=? ORDER BY band", n["id"], ts) if ts else []
        online = s.get("status") == "green" and s.get("heartbeat_ok") == 1 if s else None
        out.append({**n, **{k: v for k, v in s.items() if k not in ("node_id", "os_version")},
                    "online": online, "radios": radios})
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
    return {**d, "name": device_name(d), "node": node.get("location"), "sample": s,
            "today_mb": _mb((today.get("down") or 0) + (today.get("up") or 0)) if today.get("down") is not None else 0.0,
            "last_hour_mb": _mb((last.get("down") or 0) + (last.get("up") or 0)) if last else 0.0}


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


def find_devices(db, keys):
    """Device ids for user-given names or MACs (case-insensitive); unknown keys are returned separately."""
    found, missing = [], []
    for key in keys:
        k = key.strip().lower()
        row = db.execute("""SELECT id FROM devices WHERE lower(mac)=? OR lower(nickname)=? OR lower(display_name)=?
            OR lower(hostname)=? ORDER BY connected DESC, last_connected_at DESC LIMIT 1""", (k, k, k, k)).fetchone()
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
