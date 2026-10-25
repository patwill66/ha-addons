"""Publishes what the collector has logged to Home Assistant as sensor.luba_* / binary_sensor.luba_* states.

Every `interval` seconds it reads the database read-only (WAL lets it read while the collector
writes) and POSTs the states through the Supervisor's Home Assistant API (homeassistant_api: true).
The values come from the database, not the Mammotion API, so the sensors show what was actually
logged; sensor.luba_condition and binary_sensor.luba_needs_attention use the collector's
current_condition(). States set this way have no unique_id: they can't be edited in the UI, and after
a Home Assistant restart they reappear with the next publish. The device ID is never published.

With an `Analysis`, it also publishes jobs, reliability, battery rates, zones and recent history from
mammotion-luba's collector.report (jobs, stuck incidents, daily rollups; zones from the zones file and
the eero Collector's database). The analysis reads the whole history, so it runs at most every
ANALYSIS_SECONDS; the history sensors only change when their content does.
"""
import datetime
import json
import os
import pathlib
import sqlite3
import threading
import time
import urllib.request
from zoneinfo import ZoneInfo

from collector import report
from collector.derive import CHARGING_VALUES, STATUS_CLASSES, current_condition

API = "http://supervisor/core/api/states/"
APP = pathlib.Path(__file__).resolve().parent  # VERSION, SOURCE and CHANGELOG.md are baked into the image
P = "LUBA "
ANALYSIS_SECONDS = 300


class Analysis:
    """report.analyze() for the mower, recomputed at most every ANALYSIS_SECONDS and shared by the
    publisher and the statistics import. Zones come from `zones_path` (written by run.py from the
    add-on options); eero samples from `eero_db` (read-only) when the zones name the mower's eero device."""

    def __init__(self, db_path, timezone, zones_path=None, eero_db=None, clock=time.time):
        self.db_path, self.zones_path, self.eero_db, self.clock = db_path, zones_path, eero_db, clock
        self.tz_name, self.tz = timezone, ZoneInfo(timezone)
        self.lock, self.at, self.value = threading.Lock(), None, None

    def get(self):
        """(result, meta), meta: analyzed_at, eero_samples (None without eero), zones file details. A
        failure gives (None, {"error": ...}) and is retried after ANALYSIS_SECONDS too."""
        with self.lock:
            if self.at is None or self.clock() - self.at >= ANALYSIS_SECONDS:
                try:
                    self.value = self._compute()
                except Exception as e:  # never let the analysis stop the core sensors
                    self.value = (None, {"error": f"{type(e).__name__}: {e}"})
                self.at = self.clock()
            return self.value

    def _compute(self):
        zones = report.load_zones(self.zones_path) if self.zones_path else {}
        eero = None
        if self.eero_db and zones.get("eero_device") and os.path.exists(self.eero_db):
            eero = report.load_eero_samples(self.eero_db, zones["eero_device"])
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            device = db.execute("SELECT id FROM devices ORDER BY id LIMIT 1").fetchone()
            data = report.load(db, device["id"]) if device else None
        finally:
            db.close()
        result = report.analyze(data, eero, zones, self.tz) if data else None
        meta = {"analyzed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "eero_samples": None if eero is None else len(eero), "timezone": self.tz_name,
                "zone_fingerprints": len(zones.get("fingerprints") or []), "zone_labels": len(zones.get("labels") or {})}
        return result, meta


def rows(db, sql, *args):
    return db.execute(sql, args).fetchall()


def one(db, sql, *args):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r is not None else {}


def collect(db_path, analysis=None):
    """Returns {entity_id: (state, attributes)} from the database (plus analytics() with an Analysis)."""
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
        # The condition comes from the collector's own rules (derive.current_condition), so HA shows
        # exactly what docs/DATA_INTERPRETATION.md in mammotion-luba defines.
        condition = current_condition(
            db.execute("SELECT * FROM telemetry_samples ORDER BY observed_at DESC, id DESC LIMIT 1").fetchone(),
            db.execute("SELECT * FROM telemetry_samples WHERE raw_status IS NOT NULL "
                       "ORDER BY observed_at DESC, id DESC LIMIT 1").fetchone())
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
    attention = condition["attention"]
    out["sensor.luba_condition"] = (condition["state"] or "unknown", {
        "friendly_name": P + "Condition", "icon": "mdi:alert" if attention else "mdi:robot-mower",
        **{k: condition[k] for k in ("label", "attention", "last_known_state", "last_known_at", "last_known_battery")}})
    out["binary_sensor.luba_needs_attention"] = ("on" if attention else "off", {
        "friendly_name": P + "Needs attention", "device_class": "problem",
        "label": condition["label"], "last_known_battery": condition["last_known_battery"]})
    out["sensor.luba_addon_version"] = addon_version()
    if analysis is not None:
        result, meta = analysis.get()
        if result is not None:
            out.update(analytics(result, meta, analysis.tz))
        elif meta.get("error"):
            out["sensor.luba_analysis"] = ("unknown", {"friendly_name": P + "Analysis", "device_class": "timestamp",
                                                       "error": meta["error"]})
    return out


def analytics(result, meta, tz):
    """Sensors from report.summary(): what the mower got done and how reliably."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    s = report.summary(result, now)
    w7, w30 = s["last_7_days"], s["last_30_days"]
    cur, last, inc = s["current_job"], s["last_job"], s["last_incident"]
    today = datetime.datetime.now(tz).date().isoformat()

    def known(v):
        return "unknown" if v is None else v

    hours = {"unit_of_measurement": "h", "device_class": "duration", "state_class": "measurement"}
    rate = {"unit_of_measurement": "%/min", "state_class": "measurement"}
    return {
        "sensor.luba_current_job": ((cur["zone"] or "unknown zone") if cur else "none", {
            "friendly_name": P + "Current job", "icon": "mdi:robot-mower-outline", **(cur or {})}),
        "sensor.luba_last_job": (last["outcome"] if last else "none", {
            "friendly_name": P + "Last job", "icon": "mdi:flag-checkered", **(last or {})}),
        "sensor.luba_completion_rate_30d": (known(w30["completion_pct"]), {
            "friendly_name": P + "Jobs completed (30 days)", "icon": "mdi:check-circle-outline",
            "unit_of_measurement": "%", "state_class": "measurement",
            **{k: v for k, v in w30.items() if k != "errors"}, "last_7_days": {k: v for k, v in w7.items() if k != "errors"}}),
        "sensor.luba_stuck_incidents_7d": (w7["incidents"], {
            "friendly_name": P + "Stuck incidents (7 days)", "icon": "mdi:alert-octagon-outline",
            "state_class": "measurement", "last_30_days": w30["incidents"]}),
        "sensor.luba_last_stuck": (known(inc and inc["started_at"]), {
            "friendly_name": P + "Last stuck", "device_class": "timestamp", **(inc or {})}),
        "sensor.luba_mowing_hours_7d": (w7["mowing_hours"], {
            "friendly_name": P + "Mowing time (7 days)", "icon": "mdi:grass", **hours, "last_30_days": w30["mowing_hours"]}),
        "sensor.luba_off_in_yard_hours_30d": (w30["off_in_yard_hours"], {
            "friendly_name": P + "Powered off in the yard (30 days)", "icon": "mdi:power-plug-off-outline", **hours,
            "last_7_days": w7["off_in_yard_hours"]}),
        "sensor.luba_faults_7d": (w7["faults"], {
            "friendly_name": P + "Faults (7 days)", "icon": "mdi:alert-circle-outline", "state_class": "measurement",
            "faults_30d": w30["faults"], "errors_7d": w7["errors"], "errors_30d": w30["errors"]}),
        "sensor.luba_drain_rate": (known(s["drain_pct_per_min"]), {
            "friendly_name": P + "Battery drain while mowing (30 days)", "icon": "mdi:battery-arrow-down", **rate}),
        "sensor.luba_charge_rate": (known(s["charge_pct_per_min"]), {
            "friendly_name": P + "Charging rate (30 days)", "icon": "mdi:battery-arrow-up", **rate}),
        "sensor.luba_mowing_minutes_per_charge": (known(s["mowing_minutes_per_charge"]), {
            "friendly_name": P + "Mowing time per charge", "icon": "mdi:timer-outline", "unit_of_measurement": "min",
            "device_class": "duration", "state_class": "measurement"}),
        "sensor.luba_zones": (len(s["zones"]), {
            "friendly_name": P + "Zones", "icon": "mdi:texture-box", "zones": s["zones"]}),
        "sensor.luba_job_history": (len(result["jobs"]), {
            "friendly_name": P + "Job history", "icon": "mdi:format-list-numbered", "jobs": s["jobs"]}),
        "sensor.luba_incident_history": (len(result["incidents"]), {
            "friendly_name": P + "Stuck incident history", "icon": "mdi:alert-octagon-outline", "incidents": s["incidents"]}),
        # Completed days only, so this changes once a day rather than with every analysis.
        "sensor.luba_daily": (next((d["day"] for d in s["days"] if d["day"] < today), "none"), {
            "friendly_name": P + "Daily history", "icon": "mdi:calendar-month-outline",
            "days": [d for d in s["days"] if d["day"] < today]}),
        "sensor.luba_analysis": (meta["analyzed_at"], {
            "friendly_name": P + "Analysis", "device_class": "timestamp", "icon": "mdi:chart-box-outline",
            **{k: v for k, v in meta.items() if k != "analyzed_at"}}),
    }


def addon_version():
    def read(name):
        try:
            return (APP / name).read_text().strip()
        except OSError:
            return None
    return (read("VERSION") or "unknown", {
        "friendly_name": P + "Add-on version", "icon": "mdi:tag",
        "collector_source": read("SOURCE"), "changelog": read("CHANGELOG.md")})


def push(states, token):
    for entity_id, (state, attrs) in states.items():
        body = json.dumps({"state": state, "attributes": attrs}).encode()
        req = urllib.request.Request(API + entity_id, data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()


def supervisor_token():
    # The base image's s6-overlay starts CMD with a cleared environment; the token stays in this file.
    token = os.environ.get("SUPERVISOR_TOKEN")
    try:
        token = token or open("/run/s6/container_environment/SUPERVISOR_TOKEN").read().strip()
    except OSError:
        pass
    return token


def run(db_path, interval, stop, log, analysis=None):
    token = supervisor_token()
    if not token:
        log("no SUPERVISOR_TOKEN: not publishing sensors to Home Assistant")
        return
    log(f"publishing sensor.luba_* to Home Assistant every {interval} s")
    failing = None
    while not stop.wait(5 if failing is None else interval):
        try:
            if os.path.exists(db_path):
                push(collect(db_path, analysis), token)
                if failing:
                    log("publishing sensors to Home Assistant again")
                failing = False
        except Exception as e:  # never let publishing affect collection
            if not failing:
                log(f"publishing sensors to Home Assistant failed: {e}")
            failing = True
