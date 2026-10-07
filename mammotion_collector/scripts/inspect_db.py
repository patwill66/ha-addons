#!/usr/bin/env python3
"""Inspect what the collector has stored. Read-only; identifiers are masked.

  python3 scripts/inspect_db.py status
  python3 scripts/inspect_db.py recent [-n 20]
  python3 scripts/inspect_db.py events [-n 20]
  python3 scripts/inspect_db.py mowing-sessions [-n 20]
  python3 scripts/inspect_db.py charging-sessions [-n 20]
  python3 scripts/inspect_db.py errors [-n 20]
  python3 scripts/inspect_db.py tasks
  python3 scripts/inspect_db.py work-params [-n 5]
  python3 scripts/inspect_db.py runs [-n 10]
  python3 scripts/inspect_db.py rebuild           # recompute events + sessions from telemetry (writes)
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.config import Settings, mask  # noqa: E402


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
    ap.add_argument("command", choices=["status", "recent", "events", "mowing-sessions", "charging-sessions",
                                        "errors", "tasks", "work-params", "runs", "rebuild", "rebuild-sessions"])
    ap.add_argument("-n", type=int, default=20, help="rows to show")
    ap.add_argument("--db", help="database path (default MAMMOTION_DB_PATH or data/mammotion.db)")
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

    if args.command == "status":
        print(f"Database: {path.name} (schema v{conn.execute('PRAGMA user_version').fetchone()[0]})")
        for d in q("SELECT * FROM devices"):
            print(f"\nDevice {mask(d['mammotion_id'])}: {d['model']} \"{d['nickname']}\" firmware {d['firmware_version']}")
            print(f"  first seen {d['first_seen_at']}, last seen {d['last_seen_at']}")
            s = q("SELECT * FROM telemetry_samples WHERE device_id=? ORDER BY observed_at DESC, id DESC LIMIT 1", d["id"])
            if s:
                s = s[0]
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
    elif args.command == "charging-sessions":
        rows = [dict(r) | {"minutes": mins(r["elapsed_seconds"]),
                           "why_uncertain": "; ".join(json.loads(r["uncertain_reasons"] or "[]"))}
                for r in q("SELECT * FROM charging_sessions ORDER BY started_at DESC LIMIT ?", n)]
        table(rows[::-1], ["id", "started_at", "ended_at", "minutes", "start_battery", "end_battery",
                           "battery_gained", "uncertain", "why_uncertain"])
    elif args.command == "errors":
        total = q("SELECT COUNT(*) c, MIN(occurred_at) a, MAX(occurred_at) b FROM error_events")[0]
        print(f"  {total['c']} records, {total['a']} → {total['b']}")
        rows = q("SELECT * FROM error_events ORDER BY gmt_create_ms DESC LIMIT ?", n)
        table(rows, ["occurred_at", "code", "implication", "fault_level", "priority"])
        print("\n  By code:")
        table(q("SELECT code, implication, COUNT(*) n FROM error_events GROUP BY code ORDER BY n DESC"),
              ["code", "n", "implication"])
    elif args.command == "tasks":
        table(q("SELECT task_name, task_id, is_present, first_seen_at, last_seen_at FROM saved_tasks ORDER BY task_name"),
              ["task_name", "task_id", "is_present", "first_seen_at", "last_seen_at"])
    elif args.command == "work-params":
        table(q("SELECT * FROM work_parameter_snapshots ORDER BY observed_at DESC LIMIT ?", n),
              ["observed_at", "reason", "knife_height", "speed", "channel_width", "channel_mode", "job_content",
               "edge_mode", "toward_mode", "toward_included_angle", "ultra_wave", "forbidden_area_circle_times"])
    elif args.command == "runs":
        table(q("SELECT * FROM collector_runs ORDER BY id DESC LIMIT ?", n)[::-1],
              ["id", "started_at", "ended_at", "poll_seconds", "auth_count", "polls_ok", "polls_failed", "stop_reason"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
