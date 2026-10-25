#!/usr/bin/env python3
"""Inspect what the collector has stored. Read-only; identifiers are masked.

  python3 scripts/inspect_db.py status
  python3 scripts/inspect_db.py recent [-n 20]
  python3 scripts/inspect_db.py events [-n 20]
  python3 scripts/inspect_db.py mowing-sessions [-n 20]
  python3 scripts/inspect_db.py jobs [-n 20]               # sessions grouped into jobs: zone, outcome, details
  python3 scripts/inspect_db.py charging-sessions [-n 20]
  python3 scripts/inspect_db.py errors [-n 20]
  python3 scripts/inspect_db.py tasks
  python3 scripts/inspect_db.py work-params [-n 5]
  python3 scripts/inspect_db.py runs [-n 10]
  python3 scripts/inspect_db.py incidents          # periods paused off the dock (stuck), with faults
  python3 scripts/inspect_db.py daily [-n 14]      # one row per local day
  python3 scripts/inspect_db.py battery [-n 20]    # mowing drain and charging rates per run

jobs, incidents, daily and battery use collector/report.py. Options: --tz (default MAMMOTION_TZ or
UTC), --zones (default MAMMOTION_ZONES_PATH or data/zones.json), --eero-db (default
MAMMOTION_EERO_DB; the eero collector's database, for zones and incident locations).
  python3 scripts/inspect_db.py rebuild           # recompute events + sessions from telemetry (writes)
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import report  # noqa: E402
from collector.config import Settings, mask  # noqa: E402
from collector.derive import ATTENTION_STATES, error_category, operational_state  # noqa: E402


def table(rows, cols):
    if not rows:
        print("  (none)")
        return
    data = [[("" if r[c] is None else str(r[c])) for c in cols] for r in rows]
    widths = [max(len(c), *(len(d[i]) for d in data)) for i, c in enumerate(cols)]
    print("  " + "  ".join(c.ljust(w) for c, w in zip(cols, widths)))
    print("  " + "  ".join("-" * w for w in widths))
    for d in data:
        print("  " + "  ".join(v.ljust(w) for v, w in zip(d, widths)))


def mins(seconds):
    return None if seconds is None else f"{seconds / 60:.1f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "recent", "events", "mowing-sessions", "jobs", "charging-sessions",
                                        "errors", "tasks", "work-params", "runs", "incidents", "daily", "battery",
                                        "rebuild", "rebuild-sessions"])
    ap.add_argument("-n", type=int, default=20, help="rows to show")
    ap.add_argument("--db", help="database path (default MAMMOTION_DB_PATH or data/mammotion.db)")
    ap.add_argument("--tz", default=os.environ.get("MAMMOTION_TZ", "UTC"), help="time zone for daily rows and job times")
    ap.add_argument("--zones", help="zones file (default MAMMOTION_ZONES_PATH or data/zones.json)")
    ap.add_argument("--eero-db", default=os.environ.get("MAMMOTION_EERO_DB"), help="eero collector database (read-only)")
    args = ap.parse_args()
    path = Path(args.db) if args.db else Settings.from_env().db_path
    if not path.exists():
        print(f"No database at {path}")
        return 1
    if args.command in ("rebuild", "rebuild-sessions"):
        from collector.db import Store
        from collector.derive import rebuild_derived
        store = Store(path)
        for d in store.conn.execute("SELECT id FROM devices").fetchall():
            rebuild_derived(store, d["id"], Settings.from_env().gap_threshold_seconds)
        store.close()
        print("State events and sessions rebuilt from telemetry.")
        return 0

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    q = lambda sql, *p: conn.execute(sql, p).fetchall()  # noqa: E731
    n = args.n
    tz = timezone.utc if args.tz.upper() == "UTC" else ZoneInfo(args.tz)
    local = lambda ts: ts and report._at(ts).astimezone(tz).strftime("%m-%d %H:%M")  # noqa: E731

    def analysis():
        zones = report.load_zones(args.zones)
        out = []
        for d in q("SELECT id FROM devices"):
            data = report.load(conn, d["id"])
            eero = None
            if args.eero_db and zones.get("eero_device"):
                eero = report.load_eero_samples(args.eero_db, zones["eero_device"])
            out.append(report.analyze(data, eero, zones, tz))
        return out

    if args.command == "status":
        print(f"Database: {path.name} (schema v{conn.execute('PRAGMA user_version').fetchone()[0]})")
        for d in q("SELECT * FROM devices"):
            print(f"\nDevice {mask(d['mammotion_id'])}: {d['model']} \"{d['nickname']}\" firmware {d['firmware_version']}")
            print(f"  first seen {d['first_seen_at']}, last seen {d['last_seen_at']}")
            s = q("SELECT * FROM telemetry_samples WHERE device_id=? ORDER BY observed_at DESC, id DESC LIMIT 1", d["id"])
            if s:
                s = s[0]
                last_real = q("SELECT * FROM telemetry_samples WHERE device_id=? AND raw_status IS NOT NULL"
                              " ORDER BY observed_at DESC, id DESC LIMIT 1", d["id"])
                state = operational_state(s)
                if state in ("offline", "no_data") and last_real:
                    state += f" (last known: {operational_state(last_real[0])} at {last_real[0]['observed_at']}, " \
                             f"battery {last_real[0]['battery_level']}%)"
                print(f"  state   {state}{'  ⚠ needs attention' if operational_state(s) in ATTENTION_STATES else ''}")
                print(f"  latest  {s['observed_at']}: {s['raw_status']}, battery {s['battery_level']}%, "
                      f"charging {s['charge_status']}, online {s['online']}, network {s['used_network']}, "
                      f"wifi {s['wifi_rssi']} dBm, cell {s['cellular_rssi']} dBm")
            for t, label in (("telemetry_samples", "samples"), ("state_events", "state events"),
                             ("error_events", "error records"), ("mowing_sessions", "mowing sessions"),
                             ("charging_sessions", "charging sessions"), ("work_parameter_snapshots", "work-param snapshots")):
                print(f"  {label:22} {q(f'SELECT COUNT(*) c FROM {t} WHERE device_id=?', d['id'])[0]['c']}")
            span = q("SELECT MIN(observed_at) a, MAX(observed_at) b FROM telemetry_samples WHERE device_id=?", d["id"])[0]
            print(f"  telemetry span        {span['a']} → {span['b']}")
            for kind, t in (("mowing", "mowing_sessions"), ("charging", "charging_sessions")):
                o = q(f"SELECT * FROM {t} WHERE device_id=? AND ended_at IS NULL", d["id"])
                if o:
                    print(f"  OPEN {kind} session #{o[0]['id']} since {o[0]['started_at']}")
        return 0

    if args.command == "recent":
        rows = q("SELECT * FROM telemetry_samples ORDER BY observed_at DESC, id DESC LIMIT ?", n)
        table(rows[::-1], ["id", "observed_at", "raw_status", "battery_level", "charge_status", "online",
                           "used_network", "wifi_rssi", "cellular_rssi", "firmware_version"])
    elif args.command == "events":
        rows = q("SELECT * FROM state_events ORDER BY observed_at DESC, id DESC LIMIT ?", n)
        table(rows[::-1], ["id", "observed_at", "event_type", "old_value", "new_value", "sample_id", "gap_seconds"])
    elif args.command == "mowing-sessions":
        rows = [dict(r) | {"minutes": mins(r["elapsed_seconds"]),
                           "errors": q("SELECT COUNT(*) c FROM error_events WHERE device_id=? AND occurred_at >= ?"
                                       " AND occurred_at <= COALESCE(?, '9999')", r["device_id"], r["started_at"],
                                       r["ended_at"])[0]["c"],
                           "why_uncertain": "; ".join(json.loads(r["uncertain_reasons"] or "[]"))}
                for r in q("SELECT * FROM mowing_sessions ORDER BY started_at DESC LIMIT ?", n)]
        table(rows[::-1], ["id", "started_at", "ended_at", "minutes", "start_battery", "end_battery", "battery_used",
                           "lowest_battery", "pause_count", "end_status", "end_reason", "errors", "uncertain",
                           "why_uncertain"])
    elif args.command == "jobs":
        for a in analysis():
            jobs = a["jobs"]
            rows = [j | {"start": local(j["started_at"]), "end": local(j["ended_at"]),
                         "zone": f"{j['zone']} ({j['zone_source']})" if j["zone"] else
                         (f"? {j['eero_zone']} {j['eero_zone_share']}" if j["eero_zone"] else "-"),
                         "sessions": len(j["session_ids"]),
                         "errors": ", ".join(f"{k} {v}" for k, v in sorted(j["error_categories"].items()))}
                    for j in jobs[-n:]]
            table(rows, ["job", "start", "end", "zone", "sessions", "mowing_minutes", "stuck_minutes", "battery_used",
                         "drain_pct_per_min", "incidents", "powered_off_minutes", "outcome", "errors"])
            done = [j for j in jobs if j["outcome"] not in ("in_progress", "pending")]
            if done:
                ok = sum(j["outcome"] == "completed" for j in done)
                print(f"\n  {ok} of {len(done)} finished jobs completed; {sum(j['incidents'] > 0 for j in done)} had stuck incidents")
    elif args.command == "charging-sessions":
        rows = [dict(r) | {"minutes": mins(r["elapsed_seconds"]),
                           "why_uncertain": "; ".join(json.loads(r["uncertain_reasons"] or "[]"))}
                for r in q("SELECT * FROM charging_sessions ORDER BY started_at DESC LIMIT ?", n)]
        table(rows[::-1], ["id", "started_at", "ended_at", "minutes", "start_battery", "end_battery",
                           "battery_gained", "uncertain", "why_uncertain"])
    elif args.command == "errors":
        total = q("SELECT COUNT(*) c, MIN(occurred_at) a, MAX(occurred_at) b FROM error_events")[0]
        print(f"  {total['c']} records, {total['a']} → {total['b']}")
        rows = [dict(r) | {"category": error_category(r["code"])}
                for r in q("SELECT * FROM error_events ORDER BY gmt_create_ms DESC LIMIT ?", n)]
        table(rows, ["occurred_at", "code", "category", "implication", "fault_level", "priority"])
        by_code = [dict(r) | {"category": error_category(r["code"])}
                   for r in q("SELECT code, implication, COUNT(*) n FROM error_events GROUP BY code ORDER BY n DESC")]
        print("\n  By category:")
        totals = {}
        for r in by_code:
            totals[r["category"]] = totals.get(r["category"], 0) + r["n"]
        table([{"category": c, "n": v} for c, v in sorted(totals.items(), key=lambda x: -x[1])], ["category", "n"])
        print("\n  By code:")
        table(by_code, ["code", "category", "n", "implication"])
    elif args.command == "tasks":
        table(q("SELECT task_name, task_id, is_present, first_seen_at, last_seen_at FROM saved_tasks ORDER BY task_name"),
              ["task_name", "task_id", "is_present", "first_seen_at", "last_seen_at"])
    elif args.command == "work-params":
        table(q("SELECT * FROM work_parameter_snapshots ORDER BY observed_at DESC LIMIT ?", n),
              ["observed_at", "reason", "knife_height", "speed", "channel_width", "channel_mode", "job_content",
               "edge_mode", "toward_mode", "toward_included_angle", "ultra_wave", "forbidden_area_circle_times"])
    elif args.command == "incidents":
        for a in analysis():
            rows = [i | {"start": local(i["started_at"]), "faults": ", ".join(map(str, i["faults"])) or "-",
                         "then": "(still in progress)" if i["ongoing"] else i["then"],
                         "where": f"{i['eero_node']} {i['eero_band']} GHz {i['eero_signal']} dBm" if i.get("eero_node") else "-"}
                    for i in a["incidents"][-n:]]
            table(rows, ["start", "minutes", "before", "start_battery", "end_battery", "then", "offline_minutes",
                         "back_as", "faults", "where"])
    elif args.command == "daily":
        for a in analysis():
            rows = [d | {"mowing": round(d["minutes"].get("mowing", 0)),
                         "stuck": round(d["minutes"].get("paused_off_dock", 0)),
                         "off_in_yard": round(d["minutes"].get("stuck_powered_off", 0)),
                         "offline": round(d["minutes"].get("offline", 0)),
                         "errors": ", ".join(f"{k} {v}" for k, v in sorted(d["errors"].items()))}
                    for d in a["daily"][-n:]]
            table(rows, ["day", "mowing", "stuck", "off_in_yard", "offline", "jobs_started", "jobs_completed", "incidents",
                         "mowing_battery_used", "drain_pct_per_min", "charge_pct_per_min", "top_ups", "offline_blips",
                         "cellular_minutes", "wifi_median_mowing", "errors"])
    elif args.command == "battery":
        for a in analysis():
            for kind, label in (("drain", "Mowing runs (drain)"), ("charge", "Charging runs (gain)")):
                print(f"\n  {label}:")
                table([r | {"start": local(r["started_at"])} for r in a["battery"][kind][-n:]],
                      ["start", "minutes", "from", "to", "pct_per_min"])
    elif args.command == "runs":
        table(q("SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?", n)[::-1],
              ["id", "started_at", "ended_at", "poll_seconds", "auth_count", "polls_ok", "polls_failed", "stop_reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
