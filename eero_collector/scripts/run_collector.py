#!/usr/bin/env python3
"""Run the eero collector until Ctrl-C / SIGTERM.

  python3 scripts/run_collector.py --session-file /data/session.json --db /share/eero_collector/eero.db
  python3 scripts/run_collector.py --keychain eero-probe --duration 900 --verbose   # on the Mac

Environment (used by the Home Assistant add-on): EERO_DB_PATH, EERO_SESSION_FILE, EERO_LOCK_PATH,
EERO_POLL_SECONDS, EERO_BACKFILL (0/1), EERO_BACKFILL_DEVICE_HOURLY (0/1), EERO_RETENTION_DAYS, EERO_VERSION.

When the session is missing or eero rejects it, the collector stops calling eero and waits until the
session changes (a new login through the add-on page, or scripts/eero_login.py on the Mac).
Exit codes: 0 stopped, 75 another collector holds the lock.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.api import AuthRequired, EeroClient  # noqa: E402
from collector.collector import Collector, Settings  # noqa: E402
from collector.db import Store  # noqa: E402
from collector.lock import AlreadyRunning, InstanceLock  # noqa: E402
from collector.session import FileSessionStore, KeychainSessionStore  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
EX_ALREADY_RUNNING = 75
log = logging.getLogger("eero")


def env_bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.lower() in ("1", "true", "yes", "on")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=os.environ.get("EERO_DB_PATH", str(ROOT / "data" / "eero.db")))
    p.add_argument("--session-file", default=os.environ.get("EERO_SESSION_FILE"))
    p.add_argument("--keychain", metavar="SERVICE", help="read the session from this macOS Keychain service")
    p.add_argument("--lock", default=os.environ.get("EERO_LOCK_PATH"))
    p.add_argument("--duration", type=int, help="stop after this many seconds (testing)")
    p.add_argument("--no-backfill", action="store_true")
    p.add_argument("--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
    if a.keychain:
        session = KeychainSessionStore(a.keychain)
    else:
        session = FileSessionStore(a.session_file or ROOT / "data" / "session.json")
    settings = Settings(
        poll_seconds=int(os.environ.get("EERO_POLL_SECONDS", 300)),
        backfill=not a.no_backfill and env_bool("EERO_BACKFILL", True),
        backfill_device_hourly=env_bool("EERO_BACKFILL_DEVICE_HOURLY", True),
        sample_retention_days=int(os.environ.get("EERO_RETENTION_DAYS", 35)),
    )
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    deadline = time.time() + a.duration if a.duration else None

    try:
        lock = InstanceLock(a.lock or str(Path(a.db).with_name("collector.lock"))).acquire()
    except AlreadyRunning as e:
        log.error("%s", e)
        return EX_ALREADY_RUNNING
    store = Store(a.db)
    run_id = store.start_run(os.environ.get("EERO_VERSION", "dev"))
    client = EeroClient(session, on_call=lambda **c: store.record_call(c["method"], c["path"], c["status"],
                                                                        c["ms"], c["size"]))
    collector = Collector(client, store, settings)
    log.info("collector started (db %s, poll %d s, backfill %s)", a.db, settings.poll_seconds, settings.backfill)
    reason = "stopped"
    try:
        while not stop.is_set() and not (deadline and time.time() >= deadline):
            try:
                wait = collector.step()
            except AuthRequired:
                token = session.load()
                while not stop.is_set() and session.load() == token:
                    stop.wait(10)
                if session.load():
                    log.info("new eero session found; resuming")
                    collector.set_state("collecting")
                continue
            stop.wait(wait)
    except Exception as e:
        reason = f"crashed: {e}"
        raise
    finally:
        store.end_run(run_id, reason)
        store.close()
        lock.release()
        log.info("collector %s", reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
