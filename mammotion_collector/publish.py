"""Publishes what the collector has logged to Home Assistant as sensor.luba_* / binary_sensor.luba_* states.

Every `interval` seconds it reads the database read-only (WAL lets it read while the collector
writes) and POSTs the states through the Supervisor's Home Assistant API (homeassistant_api: true).
The values come from the database, not the Mammotion API, so the sensors show what was actually
logged. States set this way have no unique_id: they can't be edited in the UI, and after a Home
Assistant restart they reappear with the next publish. The device ID is never published.
"""
import json
import os
import sqlite3
import urllib.request

from collector.derive import CHARGING_VALUES, STATUS_CLASSES

API = "http://supervisor/core/api/states/"
P = "LUBA "


def rows(db, sql, *args):
    return db.execute(sql, args).fetchall()


def one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r is not None else {}


def collect(db_path):
    """Returns {entity_id: (state, attributes)} from the database."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    try:
        latest = one(db, "SELECT * FROM telemetry_samples ORDER BY id DESC LIMIT 1")
        known = one(db, "SELECT * FROM telemetry_samples WHERE raw_status IS NOT NULL ORDER BY id DESC LIMIT 1")
        counts = one(db, """SELECT count(*) AS total,
            sum(observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-1 hour')) AS last_hour,
            sum(observed_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-24 hours')) AS last_day
            FROM telemetry_samples""")
        run = one(db, "SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1")
        event = one(db, "SELECT * FROM state_events ORDER BY id DESC LIMIT 1")
        events = one(db, "SELECT count(*) AS n FROM state_events")["n"]
        error = one(db, "SELECT code, implication, occurred_at FROM error_events ORDER BY gmt_create_ms DESC LIMIT 1")
        errors = one(db, "SELECT count(*) AS n FROM error_events")["n"]
        mow = one(db, "SELECT * FROM mowing_sessions ORDER BY started_at DESC LIMIT 1")
        mows = one(db, "SELECT count(*) AS n FROM mowing_sessions")["n"]
        chg = one(db, "SELECT * FROM charging_sessions ORDER BY started_at DESC LIMIT 1")
        chgs = one(db, "SELECT count(*) AS n FROM charging_sessions")["n"]
        tasks = [r["task_name"] for r in rows(db, "SELECT task_name FROM saved_tasks WHERE is_present = 1 ORDER BY task_name")]
        wp = one(db, "SELECT observed_at, knife_height, channel_width, speed FROM work_parameter_snapshots ORDER BY id DESC LIMIT 1")
    finally:
        db.close()

    def ts(value):  # stored as UTC ISO with offset; HA's timestamp sensors want exactly that
        return value or "unknown"

    def session(s):
        if not s:
            return {}
        return {"started_at": s["started_at"], "ended_at": s["ended_at"], "open": s["ended_at"] is None,
                "elapsed_minutes": round((s["elapsed_seconds"] or 0) / 60),
                "uncertain": bool(s["uncertain"])}

    raw_status = known.get("raw_status") if known else None
    online = latest.get("online") if latest else None
    charge = known.get("charge_status") if known else None
    size_mb = round(sum(os.path.getsize(f"{db_path}{s}") for s in ("", "-wal") if os.path.exists(f"{db_path}{s}")) / 1e6, 2)
    out = {
        "sensor.luba_status": (raw_status or "unknown", {
            "friendly_name": P + "Status", "icon": "mdi:robot-mower",
            "class": "offline" if online == 0 else STATUS_CLASSES.get(raw_status, "other"),
            "observed_at": known.get("observed_at") if known else None}),
        "binary_sensor.luba_online": ("on" if online else "off", {
            "friendly_name": P + "Online", "device_class": "connectivity"}),
        "sensor.luba_battery": (known.get("battery_level", "unknown") if known else "unknown", {
            "friendly_name": P + "Battery", "device_class": "battery", "unit_of_measurement": "%",
            "state_class": "measurement"}),
        "binary_sensor.luba_charging": ("on" if charge in CHARGING_VALUES else "off", {
            "friendly_name": P + "Charging", "device_class": "battery_charging", "charge_status": charge}),
        "sensor.luba_wifi_rssi": (known.get("wifi_rssi", "unknown") if known else "unknown", {
            "friendly_name": P + "Wi-Fi signal", "device_class": "signal_strength",
            "unit_of_measurement": "dBm", "state_class": "measurement",
            "network": known.get("used_network") if known else None}),
        "sensor.luba_last_sample": (ts(latest.get("observed_at") if latest else None), {
            "friendly_name": P + "Last sample logged", "device_class": "timestamp"}),
        "sensor.luba_samples_logged": (counts.get("total") or 0, {
            "friendly_name": P + "Samples logged", "icon": "mdi:database", "unit_of_measurement": "samples",
            "state_class": "total_increasing", "database_mb": size_mb}),
        "sensor.luba_samples_last_hour": (counts.get("last_hour") or 0, {
            "friendly_name": P + "Samples in the last hour", "icon": "mdi:database-clock",
            "unit_of_measurement": "samples", "state_class": "measurement",
            "expected": 3600 // int(run.get("poll_seconds") or 30), "last_24h": counts.get("last_day") or 0}),
        "sensor.luba_database_size": (size_mb, {
            "friendly_name": P + "Database size", "icon": "mdi:database", "unit_of_measurement": "MB",
            "device_class": "data_size", "state_class": "measurement"}),
        "sensor.luba_collector_polls": (run.get("polls_ok", 0), {
            "friendly_name": P + "Polls this run", "icon": "mdi:sync", "state_class": "measurement",
            "failed": run.get("polls_failed", 0), "run": run.get("id"), "started_at": run.get("started_at"),
            "logins": run.get("auth_count", 0)}),
        "sensor.luba_poll_failures": (run.get("polls_failed", 0), {
            "friendly_name": P + "Failed polls this run", "icon": "mdi:sync-alert", "state_class": "measurement"}),
        "sensor.luba_state_events": (events, {
            "friendly_name": P + "State events logged", "icon": "mdi:swap-horizontal", "state_class": "total_increasing",
            "last_type": event.get("event_type"), "last_from": event.get("old_value"),
            "last_to": event.get("new_value"), "last_at": event.get("observed_at")}),
        "sensor.luba_errors": (errors, {
            "friendly_name": P + "Error records", "icon": "mdi:alert-circle-outline", "state_class": "total_increasing",
            "last_code": error.get("code"), "last_text": error.get("implication"), "last_at": error.get("occurred_at")}),
        "sensor.luba_mowing_sessions": (mows, {
            "friendly_name": P + "Mowing sessions", "icon": "mdi:grass", "state_class": "total_increasing",
            **{f"last_{k}": v for k, v in session(mow).items()},
            "last_battery_used": mow.get("battery_used") if mow else None}),
        "sensor.luba_charging_sessions": (chgs, {
            "friendly_name": P + "Charging sessions", "icon": "mdi:battery-charging", "state_class": "total_increasing",
            **{f"last_{k}": v for k, v in session(chg).items()},
            "last_battery_gained": chg.get("battery_gained") if chg else None}),
        "sensor.luba_work_params": (wp["knife_height"] if wp else "unknown", {
            "friendly_name": P + "Blade height (raw, unit undocumented)", "icon": "mdi:ruler",
            "spacing": wp.get("channel_width") if wp else None, "observed_at": wp.get("observed_at") if wp else None,
            "saved_tasks": tasks}),
    }
    return out


def push(states, token):
    for entity_id, (state, attrs) in states.items():
        body = json.dumps({"state": state, "attributes": attrs}).encode()
        req = urllib.request.Request(API + entity_id, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()


def run(db_path, interval, stop, log):
    # The base image's s6-overlay starts CMD with a cleared environment; the token stays in this file.
    token = os.environ.get("SUPERVISOR_TOKEN")
    try:
        token = token or open("/run/s6/container_environment/SUPERVISOR_TOKEN").read().strip()
    except OSError:
        pass
    if not token:
        log("no SUPERVISOR_TOKEN: not publishing sensors to Home Assistant")
        return
    log(f"publishing sensor.luba_* to Home Assistant every {interval} s")
    failing = None
    while not stop.wait(5 if failing is None else interval):
        try:
            if os.path.exists(db_path):
                push(collect(db_path), token)
                if failing:
                    log("publishing sensors to Home Assistant again")
                failing = False
        except Exception as e:  # never let publishing affect collection
            if not failing:
                log(f"publishing sensors to Home Assistant failed: {e}")
            failing = True
