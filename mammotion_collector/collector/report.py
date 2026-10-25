"""Read-side analysis of the collector database: stuck incidents, jobs with zones and details,
daily rollups and battery metrics.

What each state means is decided in derive.py; this module only aggregates those states over
time. Everything here is a pure function of rows (sqlite3.Row or dicts), except the `load_*`
helpers, which read them. Interpretations are recorded in docs/DATA_INTERPRETATION.md.

Zones: the API can't tell zones apart, so a job's zone comes from the user's labels or from the
eero node and band the mower used while mowing (read from the eero collector's database). Both
live in a zones file (MAMMOTION_ZONES_PATH, default data/zones.json, git-ignored), never in code:
    {"eero_device": "<eero device name>",
     "fingerprints": [{"node": "<eero location>", "band": "2.4", "zone": "Front Yard"}, ...],
     "labels": {"<job start, UTC ISO>": "Front Yard", ...}}
"""

import bisect
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

from .config import ROOT, seconds_between
from .derive import NOTICE_CATEGORIES, current_condition, error_category, has_telemetry, mowing_jobs

CAP_SECONDS = 120          # longest gap credited to the previous sample's state
FAULT_WINDOW_SECONDS = 600  # faults this close to a stuck incident belong to it
LABEL_MATCH_SECONDS = 300   # a label applies to the job that started this close to it
MIN_ZONE_SHARE = 0.6        # an eero zone is used only when this share of the votes agree
MIN_RUN_MINUTES = 10        # shortest mowing or charging run used for a battery rate
MIN_CHARGE_GAIN = 20        # charging runs gaining less (top-ups) get no charge rate
TOP_UP_FROM = 99            # a charging session starting at or above this is a top-up


# --- timeline -----------------------------------------------------------------------------------
def timeline(samples, cap=CAP_SECONDS) -> list[tuple]:
    """[(sample, state, seconds)] in time order. `state` is derive.current_condition's state, so
    offline after paused_off_dock is stuck_powered_off and no_data keeps the last known state.
    `seconds` is the time until the next sample, capped at `cap` (longer holes stay unobserved)."""
    samples = list(samples)
    out, last_real = [], None
    for i, s in enumerate(samples):
        if has_telemetry(s):
            last_real = s
        state = current_condition(s, last_real)["state"]
        secs = min(seconds_between(s["observed_at"], samples[i + 1]["observed_at"]), cap) if i + 1 < len(samples) else 0
        out.append((s, state, secs))
    return out


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _plus(ts: str, seconds: int) -> str:
    return (_at(ts) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# --- stuck incidents ----------------------------------------------------------------------------
def stuck_incidents(tl, errors=(), eero=None) -> list[dict]:
    """Periods paused off the dock (derive.ATTENTION_STATES), oldest first. Each has the state
    before and after it; if it powered itself off, how long it stayed off and the state it came
    back in (`back_as`: returning or paused_off_dock means powered on in the yard, a docked state
    means it was carried to the dock). `faults` are the error codes logged within
    FAULT_WINDOW_SECONDS. With eero samples, `eero_node`/`eero_band`/`eero_signal` say roughly
    where it was."""
    incidents, cur, prev_state = [], None, None
    for s, state, _ in tl:
        if s["online"] != 0 and not has_telemetry(s):
            continue  # an incomplete response says nothing about the incident
        if cur is not None and cur.get("_off"):
            if state == "stuck_powered_off":
                continue
            del cur["_off"]
            cur.update(back_as=state, back_at=s["observed_at"],
                       offline_minutes=round(seconds_between(cur["last_seen"], s["observed_at"]) / 60, 1))
            incidents.append(cur)
            cur = None
        if state == "paused_off_dock":
            if cur is None:
                cur = {"started_at": s["observed_at"], "start_battery": s["battery_level"], "before": prev_state}
            cur.update(last_seen=s["observed_at"], end_battery=s["battery_level"])
        elif cur is not None:
            cur["then"] = state
            cur["powered_off"] = state == "stuck_powered_off"
            if cur["powered_off"]:
                cur["_off"] = True
            else:
                incidents.append(cur)
                cur = None
        prev_state = state
    if cur is not None:
        cur.pop("_off", None)
        cur.setdefault("then", None)
        cur["ongoing"] = True
        incidents.append(cur)
    errors = sorted(errors, key=lambda e: e["occurred_at"] or "")
    for i in incidents:
        for key, default in (("powered_off", False), ("ongoing", False), ("back_as", None), ("back_at", None),
                             ("offline_minutes", None)):
            i.setdefault(key, default)
        i["minutes"] = round(seconds_between(i["started_at"], i["last_seen"]) / 60, 1)
        lo, hi = _plus(i["started_at"], -FAULT_WINDOW_SECONDS), _plus(i["last_seen"], FAULT_WINDOW_SECONDS)
        i["faults"] = [e["code"] for e in errors if e["occurred_at"] and lo <= e["occurred_at"] <= hi]
        if eero is not None:
            i.update(_eero_place(eero, [(i["started_at"], _plus(i["last_seen"], CAP_SECONDS))]))
    return incidents


# --- eero ---------------------------------------------------------------------------------------
class EeroSamples:
    """The mower's eero samples (observed_at UTC ISO, node, band, signal), searchable by time."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r: r[0])
        self.times = [r[0] for r in self.rows]

    def within(self, intervals):
        """Samples in each [start, end) interval."""
        for start, end in intervals:
            lo, hi = bisect.bisect_left(self.times, start), bisect.bisect_left(self.times, end)
            yield from self.rows[lo:hi]


def _eero_place(eero, intervals) -> dict:
    rows = list(eero.within(intervals))
    if not rows:
        return {"eero_node": None, "eero_band": None, "eero_signal": None}
    counts = {}
    for _, node, band, _ in rows:
        counts[(node, band)] = counts.get((node, band), 0) + 1
    node, band = max(counts, key=counts.get)
    signals = [sig for _, n, b, sig in rows if (n, b) == (node, band) and sig is not None]
    return {"eero_node": node, "eero_band": band, "eero_signal": round(median(signals)) if signals else None}


def eero_zone(eero, intervals, fingerprints) -> tuple:
    """(zone, share of votes, samples) from the eero samples within `intervals` (the job's mowing
    time only, so a long stuck period at one spot can't outvote the rest of the zone). Each sample
    whose (node, band) has a fingerprint votes for that zone."""
    zone_of = {(f["node"], str(f["band"])): f["zone"] for f in fingerprints}
    votes, total = {}, 0
    for _, node, band, _ in eero.within(intervals):
        total += 1
        z = zone_of.get((node, str(band)))
        if z is not None:
            votes[z] = votes.get(z, 0) + 1
    if not votes:
        return None, None, total
    zone = max(votes, key=votes.get)
    return zone, round(votes[zone] / sum(votes.values()), 2), total


# --- jobs ---------------------------------------------------------------------------------------
def job_details(jobs, sessions, tl, incidents=(), errors=(), eero=None, zones=None) -> list[dict]:
    """derive.mowing_jobs() rows plus: time mowing / returning / stuck in the field, mowing drain
    rate, incidents (with powered-off time), errors by category, and the zone with its source
    (`user` label, `eero` fingerprint, or None). The eero vote is always reported (`eero_zone`,
    `eero_zone_share`), but becomes the zone only with at least MIN_ZONE_SHARE of the votes."""
    zones = zones or {}
    by_id = {s["id"]: s for s in sessions}
    spans = sorted((by_id[sid]["started_at"], by_id[sid]["ended_at"] or "9999", j["job"])
                   for j in jobs for sid in j["session_ids"] if sid in by_id)
    starts = [sp[0] for sp in spans]
    acc = {j["job"]: {"mowing": 0, "returning": 0, "paused_off_dock": 0, "drop": 0, "mowing_spans": []} for j in jobs}
    prev = None
    for s, state, secs in tl:
        k = bisect.bisect_right(starts, s["observed_at"]) - 1
        job = spans[k][2] if k >= 0 and s["observed_at"] <= spans[k][1] else None
        if job is not None:
            a = acc[job]
            if state in ("mowing", "returning", "paused_off_dock"):
                a[state] += secs
            if state == "mowing":
                if prev and prev[1] == "mowing" and prev[2] == job and prev[0]["battery_level"] is not None \
                        and s["battery_level"] is not None:
                    a["drop"] += max(0, prev[0]["battery_level"] - s["battery_level"])
                    a["mowing_spans"][-1][1] = _plus(s["observed_at"], secs)
                else:
                    a["mowing_spans"].append([s["observed_at"], _plus(s["observed_at"], secs)])
        prev = (s, state, job)
    labels = zones.get("labels") or {}
    out = []
    for j in jobs:
        a = acc[j["job"]]
        end = j["ended_at"] or "9999"
        mine = [i for i in incidents if j["started_at"] <= i["started_at"] <= end]
        errs = [e for e in errors if e["occurred_at"] and j["started_at"] <= e["occurred_at"] <= end]
        cats = {}
        for e in errs:
            c = error_category(e["code"])
            cats[c] = cats.get(c, 0) + 1
        zone, source, ez, share, n_eero = None, None, None, None, None
        label = next((z for ts, z in labels.items() if abs(seconds_between(ts, j["started_at"])) <= LABEL_MATCH_SECONDS), None)
        if label:
            zone, source = label, "user"
        if eero is not None:
            ez, share, n_eero = eero_zone(eero, a["mowing_spans"], zones.get("fingerprints") or [])
            if zone is None and ez is not None and share >= MIN_ZONE_SHARE:
                zone, source = ez, "eero"
        mow_min = a["mowing"] / 60
        out.append(j | {
            "zone": zone, "zone_source": source, "eero_zone": ez, "eero_zone_share": share, "eero_samples": n_eero,
            "mowing_minutes": round(mow_min, 1), "returning_minutes": round(a["returning"] / 60, 1),
            "stuck_minutes": round(a["paused_off_dock"] / 60, 1),
            "drain_pct_per_min": round(a["drop"] / mow_min, 2) if mow_min >= MIN_RUN_MINUTES else None,
            "incidents": len(mine),
            "powered_off_minutes": round(sum(i.get("offline_minutes") or 0 for i in mine if i["powered_off"]), 1),
            "error_categories": cats,
        })
    return out


# --- battery ------------------------------------------------------------------------------------
def battery_runs(tl, min_minutes=MIN_RUN_MINUTES) -> dict:
    """Contiguous mowing runs (drain) and charging runs (gain) of at least `min_minutes`, each with
    start, minutes, battery from/to and the rate in % per minute. Charging runs gaining less than
    MIN_CHARGE_GAIN points (top-ups) have no rate."""
    out = {"drain": [], "charge": []}
    for kind, state in (("drain", "mowing"), ("charge", "docked_charging")):
        run = []
        for s, st, _ in tl + [(None, None, 0)]:
            if st == state and s["battery_level"] is not None:
                run.append(s)
                continue
            if len(run) > 1:
                minutes = seconds_between(run[0]["observed_at"], run[-1]["observed_at"]) / 60
                change = run[0]["battery_level"] - run[-1]["battery_level"] if kind == "drain" \
                    else run[-1]["battery_level"] - run[0]["battery_level"]
                if minutes >= min_minutes:
                    rate = round(change / minutes, 2) if kind == "drain" or change >= MIN_CHARGE_GAIN else None
                    out[kind].append({"started_at": run[0]["observed_at"], "minutes": round(minutes, 1),
                                      "from": run[0]["battery_level"], "to": run[-1]["battery_level"],
                                      "pct_per_min": rate})
            run = []
    return out


# --- daily rollups ------------------------------------------------------------------------------
def _day(ts: str, tz) -> str:
    return _at(ts).astimezone(tz).date().isoformat()


def daily(tl, jobs=(), incidents=(), errors=(), charging_sessions=(), battery=None, tz=timezone.utc) -> list[dict]:
    """One row per local day (time zone `tz`): minutes in each state, jobs started and completed,
    stuck incidents, battery used while mowing, median drain and charge rates, top-ups, offline
    blips (short of being stuck), minutes on cellular, median Wi-Fi signal while mowing, and error
    records by category. Only days with telemetry get a row; older error history isn't counted."""
    days = {}

    def row(d):
        return days.setdefault(d, {"day": d, "minutes": {}, "jobs_started": 0, "jobs_completed": 0, "incidents": 0,
                                   "mowing_battery_used": 0, "top_ups": 0, "offline_blips": 0, "cellular_minutes": 0.0,
                                   "errors": {}, "_wifi": [], "_drain": [], "_charge": []})

    prev = None
    for s, state, secs in tl:
        r = row(_day(s["observed_at"], tz))
        r["minutes"][state] = r["minutes"].get(state, 0) + secs / 60
        if s["used_network"] == "2":
            r["cellular_minutes"] += secs / 60
        if state == "offline" and (prev is None or prev[1] != "offline"):
            r["offline_blips"] += 1
        if state == "mowing":
            if s["wifi_rssi"] is not None:
                r["_wifi"].append(s["wifi_rssi"])
            if prev and prev[1] == "mowing" and prev[0]["battery_level"] is not None and s["battery_level"] is not None:
                r["mowing_battery_used"] += max(0, prev[0]["battery_level"] - s["battery_level"])
        prev = (s, state)
    seen = lambda ts: days.get(_day(ts, tz)) if ts else None  # noqa: E731  (no new rows from here on)
    for j in jobs:
        if (r := seen(j["started_at"])) is not None:
            r["jobs_started"] += 1
        if j["outcome"] == "completed" and (r := seen(j["ended_at"])) is not None:
            r["jobs_completed"] += 1
    for i in incidents:
        if (r := seen(i["started_at"])) is not None:
            r["incidents"] += 1
    for e in errors:
        if (r := seen(e["occurred_at"])) is not None:
            c = error_category(e["code"])
            r["errors"][c] = r["errors"].get(c, 0) + 1
    for c in charging_sessions:
        if c["start_battery"] is not None and c["start_battery"] >= TOP_UP_FROM and (r := seen(c["started_at"])) is not None:
            r["top_ups"] += 1
    for kind, key in (("drain", "_drain"), ("charge", "_charge")):
        for run in (battery or {}).get(kind, []):
            if run["pct_per_min"] is not None and (r := seen(run["started_at"])) is not None:
                r[key].append(run["pct_per_min"])
    out = []
    for d in sorted(days):
        r = days[d]
        r["minutes"] = {k: round(v, 1) for k, v in sorted(r["minutes"].items())}
        r["cellular_minutes"] = round(r["cellular_minutes"], 1)
        wifi = r.pop("_wifi")
        r["wifi_median_mowing"] = median(wifi) if wifi else None
        r["drain_pct_per_min"] = round(median(r["_drain"]), 2) if r["_drain"] else None
        r["charge_pct_per_min"] = round(median(r["_charge"]), 2) if r["_charge"] else None
        del r["_drain"], r["_charge"]
        out.append(r)
    return out


# --- everything at once -------------------------------------------------------------------------
def analyze(data: dict, eero_rows=None, zones=None, tz=timezone.utc) -> dict:
    """All of the above from load()'s rows for one device."""
    tl = timeline(data["samples"])
    eero = EeroSamples(eero_rows) if eero_rows is not None else None
    jobs = mowing_jobs(data["mowing_sessions"], data["work_params"], data["latest_real"])
    incidents = stuck_incidents(tl, data["errors"], eero)
    battery = battery_runs(tl)
    return {
        "jobs": job_details(jobs, data["mowing_sessions"], tl, incidents, data["errors"], eero, zones),
        "incidents": incidents,
        "battery": battery,
        "daily": daily(tl, jobs, incidents, data["errors"], data["charging_sessions"], battery, tz),
    }


# --- pages: a window of history, and findings for a stuck incident ------------------------------
SAMPLE_COLUMNS = "id, observed_at, online, raw_status, charge_status, battery_level, used_network, wifi_rssi"
SAME_SPOT_DB = 6  # incidents at the same eero node and band within this many dB count as the same spot

BEFORE_WORDS = {"mowing": "mowing", "returning": "heading home to the dock"}
THEN_WORDS = {"returning": "it headed home", "mowing": "it carried on mowing", "paused_off_dock": "it stayed stuck"}
BACK_WORDS = {"returning": "it came back on in the yard and drove home (powered on there)",
              "paused_off_dock": "it came back on in the yard, still stuck",
              "mowing": "it came back on and carried on mowing (restarted in place)",
              "docked_charging": "it came back on already on the dock (carried there)",
              "docked_charged": "it came back on already on the dock (carried there)",
              "docked_waiting": "it came back on already on the dock (carried there)"}


def load_window(conn, device_id: int, start: str, end: str) -> dict:
    """Samples from start to end, plus the last sample with state before start (so the window's first
    state is known), and the state events and error records in the window."""
    q = lambda sql, *a: conn.execute(sql, (device_id, *a)).fetchall()  # noqa: E731
    before = q(f"SELECT {SAMPLE_COLUMNS} FROM telemetry_samples WHERE device_id=? AND observed_at < ?"
               " AND raw_status IS NOT NULL ORDER BY observed_at DESC, id DESC LIMIT 1", start)
    return {
        "start": start, "end": end,
        "samples": list(before) + list(q(f"SELECT {SAMPLE_COLUMNS} FROM telemetry_samples WHERE device_id=?"
                                         " AND observed_at BETWEEN ? AND ? ORDER BY observed_at, id", start, end)),
        "events": q("SELECT observed_at, event_type, old_value, new_value FROM state_events WHERE device_id=?"
                    " AND observed_at BETWEEN ? AND ? ORDER BY observed_at, id", start, end),
        "errors": q("SELECT code, implication, occurred_at FROM error_events WHERE device_id=?"
                    " AND occurred_at BETWEEN ? AND ? ORDER BY gmt_create_ms", start, end),
    }


def window_view(win: dict, eero=None) -> dict:
    """load_window() as chart-ready data: `points` (time, state, battery, Wi-Fi), `segments` (runs of
    one state, clipped to the window), errors with their category, and the eero samples."""
    start, end = win["start"], win["end"]
    points = [{"at": max(s["observed_at"], start), "state": st, "battery": s["battery_level"], "wifi": s["wifi_rssi"]}
              for s, st, _ in timeline(win["samples"])]
    segments = []
    for p in points:
        if segments and segments[-1]["state"] == p["state"]:
            continue
        if segments:
            segments[-1]["end"] = p["at"]
        segments.append({"state": p["state"], "start": p["at"], "end": end})
    return {"start": start, "end": end, "points": points, "segments": segments,
            "events": [dict(e) for e in win["events"]],
            "errors": [dict(e) | {"category": error_category(e["code"])} for e in win["errors"]],
            "eero": list(eero.within([(start, end)])) if eero is not None else None}


def _when(minutes):
    return f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60:.1f} h"


def incident_findings(incidents, index: int, jobs=()) -> list[dict]:
    """What happened in incidents[index], as findings {level: bad|warn|ok|info, at, text}: what it was
    doing, how fast it drained, how it ended, whether it was at the same spot (eero node, band and
    signal within SAME_SPOT_DB) as other incidents, and its place in the zone's history."""
    inc = incidents[index]
    job = next((j for j in jobs if j["started_at"] <= inc["started_at"] <= (j["ended_at"] or "9999")), None)
    zone = job["zone"] if job else None
    out = [{"level": "bad", "at": inc["started_at"],
            "text": f"Got stuck while {BEFORE_WORDS.get(inc['before'], inc['before'] or 'in an unknown state')}"
                    f" at {inc['start_battery']} %" + (f", in the {zone}" if zone else "")
                    + (f" (job {job['job']})" if job else "")}]
    drop = (inc["start_battery"] or 0) - (inc["end_battery"] or 0)
    if inc["minutes"] >= 10 and drop > 0:
        out.append({"level": "info", "at": None,
                    "text": f"Drained {drop} % in {_when(inc['minutes'])} while stuck (1 % every {inc['minutes'] / drop:.0f} min)"})
    if inc["ongoing"]:
        out.append({"level": "bad", "at": inc["last_seen"],
                    "text": "Powered off and not back yet" if inc["powered_off"] else "Still stuck at the last sample"})
    elif not inc["powered_off"]:
        out.append({"level": "ok", "at": inc["last_seen"],
                    "text": f"Freed after {_when(inc['minutes'])}: {THEN_WORDS.get(inc['then'], inc['then'])}"})
    else:
        out.append({"level": "bad", "at": inc["last_seen"],
                    "text": f"Powered itself off after {_when(inc['minutes'])} stuck, at {inc['end_battery']} %"})
        if inc["back_as"]:
            out.append({"level": "warn", "at": inc["back_at"],
                        "text": f"{_when(inc['offline_minutes'])} later, {BACK_WORDS.get(inc['back_as'], inc['back_as'])}"})
    if inc.get("eero_node"):
        def same(o):
            return (o.get("eero_node"), o.get("eero_band")) == (inc["eero_node"], inc["eero_band"]) and \
                o.get("eero_signal") is not None and inc["eero_signal"] is not None and \
                abs(o["eero_signal"] - inc["eero_signal"]) <= SAME_SPOT_DB
        earlier = [o for o in incidents[:index] if same(o)]
        later = [o for o in incidents[index + 1:] if same(o)]
        where = f"{inc['eero_node']} eero, {inc['eero_band']} GHz, {inc['eero_signal']} dBm"
        if earlier or later:
            others = " and ".join(f"{len(x)} {word} incident{'s' if len(x) > 1 else ''}"
                                  for x, word in ((earlier, "earlier"), (later, "later")) if x)
            out.append({"level": "warn", "at": None, "text": f"Same spot ({where}) as {others}",
                        "same_spot": [o["started_at"] for o in earlier + later]})
        else:
            out.append({"level": "info", "at": None, "text": f"The only incident at this spot ({where})"})
    else:
        out.append({"level": "info", "at": None, "text": "No eero data for this incident, so no location"})
    if zone:
        in_zone = [o for o in incidents if (j := next((j for j in jobs if j["started_at"] <= o["started_at"]
                                                       <= (j["ended_at"] or "9999")), None)) and j["zone"] == zone]
        out.append({"level": "info", "at": None,
                    "text": f"Incident {in_zone.index(inc) + 1} of {len(in_zone)} in the {zone}"})
    return out


# --- Home Assistant: long-term statistics and a summary -----------------------------------------
STATISTICS = {
    # key: (name, unit, unit class, kind, daily row field) - kind "sum" is a running total of the
    # daily values, "mean" the day's value as mean/min/max.
    "mowing_hours": ("LUBA mowing time", "h", "duration", "sum", lambda d: d["minutes"].get("mowing", 0) / 60),
    "stuck_hours": ("LUBA time stuck in the yard", "h", "duration", "sum",
                    lambda d: d["minutes"].get("paused_off_dock", 0) / 60),
    "off_in_yard_hours": ("LUBA time powered off in the yard", "h", "duration", "sum",
                          lambda d: d["minutes"].get("stuck_powered_off", 0) / 60),
    "stuck_incidents": ("LUBA stuck incidents", None, None, "sum", lambda d: d["incidents"]),
    "jobs_started": ("LUBA jobs started", None, None, "sum", lambda d: d["jobs_started"]),
    "jobs_completed": ("LUBA jobs completed", None, None, "sum", lambda d: d["jobs_completed"]),
    "mowing_battery_used": ("LUBA battery used mowing", "%", None, "sum", lambda d: d["mowing_battery_used"]),
    "offline_blips": ("LUBA offline blips", None, None, "sum", lambda d: d["offline_blips"]),
    "cellular_hours": ("LUBA time on cellular", "h", "duration", "sum", lambda d: d["cellular_minutes"] / 60),
    "drain_rate": ("LUBA battery drain while mowing", "%/min", None, "mean", lambda d: d["drain_pct_per_min"]),
    "charge_rate": ("LUBA charging rate", "%/min", None, "mean", lambda d: d["charge_pct_per_min"]),
}


def slug(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in name.lower()).strip("_")


def statistics(result: dict, tz=timezone.utc) -> dict:
    """Series for Home Assistant's long-term statistics from analyze(): {key: {"name", "unit",
    "unit_class", "kind", "rows"}}, rows {"start": UTC epoch of the local day's first hour, and "sum"
    or "mean"/"min"/"max"}. One row per local day; sums run from the first day, so re-sending all
    rows is safe. Per-zone mowing time (`mowing_hours_<zone>`) credits each job's mowing time to the
    day it started."""
    def start(day):
        return int(datetime.fromisoformat(day).replace(tzinfo=tz).timestamp()) // 3600 * 3600  # HA wants whole hours

    out = {}
    for key, (name, unit, unit_class, kind, value) in STATISTICS.items():
        rows, total = [], 0.0
        for d in result["daily"]:
            v = value(d)
            if kind == "sum":
                total += v or 0
                rows.append({"start": start(d["day"]), "sum": round(total, 4)})
            elif v is not None:
                rows.append({"start": start(d["day"]), "mean": v, "min": v, "max": v})
        out[key] = {"name": name, "unit": unit, "unit_class": unit_class, "kind": kind, "rows": rows}
    zones = sorted({j["zone"] for j in result["jobs"] if j["zone"]})
    for zone in zones:
        per_day = {}
        for j in result["jobs"]:
            if j["zone"] == zone:
                day = _day(j["started_at"], tz)
                per_day[day] = per_day.get(day, 0) + j["mowing_minutes"] / 60
        rows, total = [], 0.0
        for d in result["daily"]:
            total += per_day.get(d["day"], 0)
            rows.append({"start": start(d["day"]), "sum": round(total, 4)})
        out[f"mowing_hours_{slug(zone)}"] = {"name": f"LUBA mowing time, {zone}", "unit": "h", "unit_class": "duration",
                                             "kind": "sum", "rows": rows}
    return out


JOB_FIELDS = ("job", "zone", "zone_source", "started_at", "ended_at", "outcome", "mowing_minutes", "stuck_minutes",
              "recharges", "battery_used", "drain_pct_per_min", "incidents", "powered_off_minutes", "error_categories")
INCIDENT_FIELDS = ("started_at", "minutes", "before", "then", "powered_off", "offline_minutes", "back_as",
                   "start_battery", "end_battery", "faults", "eero_node", "eero_band", "eero_signal", "zone")


def summary(result: dict, now: str, history: int = 15) -> dict:
    """What a dashboard shows from analyze(): the current and last job, 7- and 30-day reliability,
    battery rates, per-zone figures and recent jobs, incidents and days."""
    jobs = result["jobs"]
    incidents = [i | {"zone": next((j["zone"] for j in jobs  # the zone of the job it happened in
                                    if j["started_at"] <= i["started_at"] <= (j["ended_at"] or "9999")), None)}
                 for i in result["incidents"]]

    def since(days):
        return _plus(now, -days * 86400)

    def window(days):
        cut = since(days)
        done = [j for j in jobs if (j["ended_at"] or "") >= cut and j["outcome"] not in ("in_progress", "pending")]
        ok = sum(j["outcome"] == "completed" for j in done)
        errors = {}
        for d in result["daily"]:
            if d["day"] >= cut[:10]:
                for c, v in d["errors"].items():
                    errors[c] = errors.get(c, 0) + v
        return {"jobs_finished": len(done), "jobs_completed": ok, "errors": errors,
                "faults": sum(v for c, v in errors.items() if c not in NOTICE_CATEGORIES),
                "completion_pct": round(100 * ok / len(done)) if done else None,
                "incidents": sum(i["started_at"] >= cut for i in incidents),
                "mowing_hours": round(sum(d["minutes"].get("mowing", 0) for d in result["daily"]
                                          if d["day"] >= cut[:10]) / 60, 1),
                "off_in_yard_hours": round(sum(d["minutes"].get("stuck_powered_off", 0) for d in result["daily"]
                                               if d["day"] >= cut[:10]) / 60, 1)}

    def median_rate(kind, days=30):
        rates = [r["pct_per_min"] for r in result["battery"][kind] if r["pct_per_min"] is not None
                 and r["started_at"] >= since(days)]
        return round(median(rates), 2) if rates else None

    current = next((j for j in reversed(jobs) if j["outcome"] in ("in_progress", "pending")), None)
    last = next((j for j in reversed(jobs) if j["outcome"] not in ("in_progress", "pending")), None)
    zones = {}
    for j in jobs:
        if not j["zone"]:
            continue
        z = zones.setdefault(j["zone"], {"jobs": 0, "completed": 0, "incidents": 0, "last_mowed": None,
                                         "mowing_minutes_median": [], "battery_used_median": []})
        z["jobs"] += 1
        z["completed"] += j["outcome"] == "completed"
        z["incidents"] += j["incidents"]
        z["last_mowed"] = j["started_at"]
        if j["outcome"] == "completed":
            z["mowing_minutes_median"].append(j["mowing_minutes"])
            z["battery_used_median"].append(j["battery_used"])
    for z in zones.values():
        for k in ("mowing_minutes_median", "battery_used_median"):
            z[k] = round(median(z[k])) if z[k] else None
    drain = median_rate("drain")
    last_incident = incidents[-1] if incidents else None
    return {
        "current_job": {k: current[k] for k in JOB_FIELDS} if current else None,
        "last_job": {k: last[k] for k in JOB_FIELDS} if last else None,
        "last_7_days": window(7),
        "last_30_days": window(30),
        "last_incident": {k: last_incident.get(k) for k in INCIDENT_FIELDS} if last_incident else None,
        "drain_pct_per_min": drain,
        "charge_pct_per_min": median_rate("charge"),
        "mowing_minutes_per_charge": round(100 / drain) if drain else None,
        "zones": zones,
        "jobs": [{k: j[k] for k in JOB_FIELDS} for j in jobs[-history:]][::-1],
        "incidents": [{k: i.get(k) for k in INCIDENT_FIELDS} for i in incidents[-history:]][::-1],
        "days": result["daily"][-14:][::-1],
    }


def load(conn, device_id: int) -> dict:
    """The rows analyze() needs, from a collector database connection (row_factory sqlite3.Row)."""
    q = lambda sql: conn.execute(sql, (device_id,)).fetchall()  # noqa: E731
    latest = q("SELECT * FROM telemetry_samples WHERE device_id=? AND raw_status IS NOT NULL"
               " ORDER BY observed_at DESC, id DESC LIMIT 1")
    return {
        "samples": q("SELECT id, observed_at, online, raw_status, charge_status, battery_level, used_network, wifi_rssi"
                     " FROM telemetry_samples WHERE device_id=? ORDER BY observed_at, id"),
        "mowing_sessions": q("SELECT * FROM mowing_sessions WHERE device_id=? ORDER BY started_at"),
        "charging_sessions": q("SELECT * FROM charging_sessions WHERE device_id=? ORDER BY started_at"),
        "work_params": q("SELECT observed_at, raw_json FROM work_parameter_snapshots WHERE device_id=?"),
        "errors": q("SELECT code, occurred_at FROM error_events WHERE device_id=? ORDER BY gmt_create_ms"),
        "latest_real": latest[0] if latest else None,
    }


def zones_path() -> Path:
    return Path(os.environ.get("MAMMOTION_ZONES_PATH", str(ROOT / "data" / "zones.json")))


def load_zones(path=None) -> dict:
    """The zones file (see the module docstring), or {} if there is none."""
    path = Path(path) if path else zones_path()
    return json.loads(path.read_text()) if path.exists() else {}


def load_eero_samples(eero_db, device_name: str, since: str | None = None, until: str | None = None) -> list[tuple]:
    """The mower's samples from the eero collector's database (read-only):
    [(observed_at UTC ISO, node location, band, signal dBm)]. [] if the database or device is missing."""
    try:
        conn = sqlite3.connect(f"file:{eero_db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        since_epoch = int(_at(since).timestamp()) if since else 0
        until_epoch = int(_at(until).timestamp()) if until else 2 ** 62
        rows = conn.execute(
            "SELECT ds.ts, n.location, ds.band, ds.signal FROM device_samples ds"
            " JOIN devices d ON d.id = ds.device_id LEFT JOIN nodes n ON n.id = ds.node_id"
            " WHERE (d.nickname = ? OR d.display_name = ? OR d.hostname = ?) AND ds.ts >= ? AND ds.ts <= ? ORDER BY ds.ts",
            (device_name, device_name, device_name, since_epoch, until_epoch)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [(datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"), node, band, signal)
            for ts, node, band, signal in rows]
