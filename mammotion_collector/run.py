"""Mammotion Collector add-on entry point: runs ~/Projects/mammotion-luba's scripts/run_collector.py.

Options from /data/options.json become the collector's environment variables. The database and the
single-instance lock live in /share/mammotion_collector/, not in the add-on's /data, so that any
install of this add-on (local or from the GitHub repository; HA treats those as different add-ons
with separate /data) and later analytics can reach them. /share is part of HA's full backups. The
lock is shared too, so two installs can never collect at the same time.

When there's no database there yet (missing or empty), one is restored with SQLite's backup API from,
in order: /data/mammotion.db (where versions before 0.2.0 kept it; renamed to .migrated afterwards),
or /share/mammotion/mammotion.db (a copy of the Mac collector's database). Stop the Mac collector
(scripts/service.sh stop) before copying it: only one collector may hold the Mammotion credentials.

Unless `publish_sensors` is off, publish.py runs alongside it in a thread and mirrors what has been
logged into sensor.luba_* states in Home Assistant, for the "LUBA Mower" dashboard.

The collector is restarted after a crash (60 s later, like launchd's ThrottleInterval), but not after
a clean stop, missing or refused credentials (exit 2) or "already running" (exit 75).
"""
import json
import os
import pathlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time

import publish

OPTIONS = pathlib.Path("/data/options.json")
DB_DIR = pathlib.Path("/share/mammotion_collector")
DB = DB_DIR / "mammotion.db"
SOURCES = (pathlib.Path("/data/mammotion.db"), pathlib.Path("/share/mammotion/mammotion.db"))
DONT_RESTART = {0, 2, 75}


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} INFO    [add-on] {msg}", flush=True)


def restore_db():
    """Copies the first existing source (and any -wal) into a staging folder, then restores it into
    place with the backup API, so an uncheckpointed -wal is included and DB only appears complete."""
    DB_DIR.mkdir(exist_ok=True)
    if DB.exists() and DB.stat().st_size > 0:
        return
    source = next((s for s in SOURCES if s.exists() and s.stat().st_size > 0), None)
    if source is None:
        return
    staging = DB_DIR / "restore"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    for suffix in ("", "-wal"):
        if pathlib.Path(f"{source}{suffix}").exists():
            shutil.copyfile(f"{source}{suffix}", staging / f"{source.name}{suffix}")
    tmp = DB.with_name(DB.name + ".restoring")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(staging / source.name)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    tmp.replace(DB)
    shutil.rmtree(staging)
    if source == SOURCES[0]:
        for suffix in ("-wal", "-shm"):
            pathlib.Path(f"{source}{suffix}").unlink(missing_ok=True)
        source.rename(source.with_name(source.name + ".migrated"))
    log(f"restored {DB} ({DB.stat().st_size // 1024} KB) from {source}")


def main():
    options = json.loads(OPTIONS.read_text())
    env = dict(os.environ,
               MAMMOTION_CLIENT_ID=options.get("client_id", ""),
               MAMMOTION_CLIENT_SECRET=options.get("client_secret", ""),
               MAMMOTION_POLL_SECONDS=str(options.get("poll_seconds", 30)),
               MAMMOTION_DB_PATH=str(DB),
               MAMMOTION_LOCK_PATH=str(DB_DIR / "collector.lock"))
    restore_db()
    cmd = [sys.executable, "-u", "/app/scripts/run_collector.py"] + (["--verbose"] if options.get("verbose") else [])
    log(f"collector source: {pathlib.Path('/app/SOURCE').read_text().strip()}")

    child = None
    stopping = False
    stop_publishing = threading.Event()
    if options.get("publish_sensors", True):
        threading.Thread(target=publish.run, daemon=True,
                         args=(str(DB), int(options.get("poll_seconds", 30)), stop_publishing, log)).start()

    def forward(signum, _frame):
        nonlocal stopping
        stopping = True
        stop_publishing.set()
        if child and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        child = subprocess.Popen(cmd, env=env)
        code = child.wait()
        if stopping or code in DONT_RESTART:
            return code
        log(f"collector exited with code {code}; restarting in 60 s")
        for _ in range(60):
            if stopping:
                return code
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
