#!/usr/bin/env python3
"""Look inside the collector database (opened read-only).

  python3 scripts/inspect_db.py status                  # row counts, backfill, tasks, data sources
  python3 scripts/inspect_db.py nodes                   # eero nodes now
  python3 scripts/inspect_db.py devices [--all]         # connected (or all) devices with usage today
  python3 scripts/inspect_db.py usage [--days 7]        # network usage per day
  python3 scripts/inspect_db.py changes [--hours 24]    # device changes (new, connect, roam, band, ip)
  python3 scripts/inspect_db.py events [--hours 24]     # eero's event feed

--db PATH (default data/eero.db, or EERO_DB_PATH). Output contains device names and MACs: don't paste it
anywhere public.
"""

import argparse
import datetime
import os
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector import db as dbmod, report  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("network_samples", "node_samples", "radio_samples", "devices", "device_samples", "device_hourly",
          "device_changes", "events", "usage_network_hourly", "usage_network_daily", "usage_device_hourly",
          "usage_device_daily", "speedtests", "notifications", "premium_raw")


def when(epoch, tz):
    return datetime.datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M") if epoch else "-"


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=["status", "nodes", "devices", "usage", "changes", "events"])
    p.add_argument("--db", default=os.environ.get("EERO_DB_PATH", str(ROOT / "data" / "eero.db")))
    p.add_argument("--all", action="store_true")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--hours", type=int, default=24)
    p.add_argument("--tz", default="America/Chicago")
    a = p.parse_args()
    tz, now = ZoneInfo(a.tz), int(time.time())
    db = dbmod.connect(a.db, readonly=True)

    if a.cmd == "status":
        for t in TABLES:
            print(f"{t:24} {db.execute(f'SELECT count(*) FROM {t}').fetchone()[0]:>9}")
        print("\nbackfill:", ", ".join(f"{r['name']}={'done' if r['done'] else r['cursor']}"
                                      for r in db.execute("SELECT * FROM backfill")))
        for r in db.execute("SELECT * FROM task_state ORDER BY name"):
            print(f"task {r['name']:14} runs {r['runs']:>5} failures {r['failures']:>3} last ok {when(r['last_ok_at'], tz)}"
                  f"{'  last error: ' + r['last_error'] if r['last_error'] else ''}")
        for r in db.execute("SELECT * FROM capabilities ORDER BY requires_premium, name"):
            print(f"source {r['name']:28} {'plus' if r['requires_premium'] else 'free':5} {r['status']}")
    elif a.cmd == "nodes":
        for n in report.nodes(db):
            radios = "  ".join(f"{r['band']}GHz ch{r['channel']} {r['utilization']}% {r['clients']}c" for r in n["radios"])
            print(f"{n['location']:14} {'online' if n['online'] else 'OFFLINE':8} mesh {n.get('mesh_bars')}/5 "
                  f"clients {n.get('clients'):>3}  up {n.get('upstream') or 'wired'}  {radios}")
    elif a.cmd == "devices":
        for r in report.device_table(db, now, tz):
            if not a.all and not r["connected"]:
                continue
            print(f"{(r['name'] or '')[:28]:28} {'on ' if r['connected'] else 'off'} {(r.get('node') or '')[:12]:12} "
                  f"{r.get('band') or '-':>3} {r['signal'] if r['signal'] is not None else '':>4} "
                  f"{(r['today_bytes'] or 0) / 1e6:>9.1f} MB today")
    elif a.cmd == "usage":
        for r in db.execute("SELECT * FROM usage_network_daily ORDER BY day DESC LIMIT ?", (a.days,)):
            print(f"{r['day']}  down {r['down'] / 1e9:7.1f} GB  up {r['up'] / 1e9:6.1f} GB{'' if r['complete'] else '  (so far)'}")
    elif a.cmd == "changes":
        for r in db.execute("""SELECT c.*, d.nickname, d.display_name, d.mac FROM device_changes c JOIN devices d ON d.id=c.device_id
                WHERE c.ts >= ? AND c.kind != 'new' ORDER BY c.ts""", (now - a.hours * 3600,)):
            print(f"{when(r['ts'], tz)}  {(r['nickname'] or r['display_name'] or r['mac'])[:28]:28} {r['kind']:12} "
                  f"{r['old'] or ''} -> {r['new'] or ''}  ({r['source']})")
    elif a.cmd == "events":
        for r in db.execute("SELECT * FROM events WHERE ts_ms >= ? ORDER BY ts_ms", ((now - a.hours * 3600) * 1000,)):
            print(f"{when(r['ts_ms'] // 1000, tz)}  {r['description']}")


if __name__ == "__main__":
    main()
