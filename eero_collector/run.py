"""eero Collector add-on entry point.

Runs ~/Projects/eero-network's scripts/run_collector.py as a child process (restarted 60 s after a crash),
publish.py in a thread (MQTT discovery to Home Assistant) and web.py in a thread (ingress pages,
including the eero login). The database and lock live in /share/eero_collector/ (part of HA's full
backups); the eero session token lives in /data/session.json, private to this add-on.
"""

import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/app")

import publish  # noqa: E402
import web  # noqa: E402
from collector.session import FileSessionStore  # noqa: E402

OPTIONS = pathlib.Path("/data/options.json")
DB_DIR = pathlib.Path("/share/eero_collector")
DB = DB_DIR / "eero.db"
SESSION = pathlib.Path("/data/session.json")
APP = pathlib.Path("/app")
DONT_RESTART = {0, 75}


def log(msg):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} INFO    [add-on] {msg}", flush=True)


def read(name):
    try:
        return (APP / name).read_text().strip()
    except OSError:
        return None


def supervisor_token():
    # The base image's s6-overlay starts CMD with a cleared environment; the token stays in this file.
    token = os.environ.get("SUPERVISOR_TOKEN")
    try:
        token = token or open("/run/s6/container_environment/SUPERVISOR_TOKEN").read().strip()
    except OSError:
        pass
    return token


def main():
    options = json.loads(OPTIONS.read_text())
    version, source = read("VERSION") or "unknown", read("SOURCE")
    timezone = options.get("timezone") or "America/Chicago"
    DB_DIR.mkdir(exist_ok=True)
    env = dict(os.environ,
               EERO_DB_PATH=str(DB), EERO_SESSION_FILE=str(SESSION), EERO_LOCK_PATH=str(DB_DIR / "collector.lock"),
               EERO_POLL_SECONDS=str(options.get("poll_seconds", 300)),
               EERO_BACKFILL="1" if options.get("backfill", True) else "0",
               EERO_BACKFILL_DEVICE_HOURLY="1" if options.get("backfill_device_hourly", True) else "0",
               EERO_RETENTION_DAYS=str(options.get("sample_retention_days", 35)),
               EERO_VERSION=version)
    log(f"eero Collector {version} ({source})")
    if not SESSION.exists():
        log("no eero session yet: open the add-on's Web UI and log in")

    stop = threading.Event()
    session = FileSessionStore(SESSION)
    threading.Thread(target=web.run, daemon=True, args=(str(DB), session, timezone, log)).start()
    if options.get("publish_mqtt", True):
        token = supervisor_token()
        try:
            broker = publish.broker_from_supervisor(token)
            pub = publish.Publisher(broker, str(DB), options.get("tracked_devices", []),
                                    int(options.get("publish_seconds", 60)), timezone, version, source, log)
            threading.Thread(target=pub.run, daemon=True, args=(stop,)).start()
        except Exception as e:
            log(f"MQTT not available ({e}); sensors won't be published. Is the Mosquitto broker add-on running?")

    cmd = [sys.executable, "-u", "/app/scripts/run_collector.py"] + (["--verbose"] if options.get("verbose") else [])
    child = None
    stopping = False

    def forward(signum, _frame):
        nonlocal stopping
        stopping = True
        stop.set()
        if child and child.poll() is None:
            child.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        child = subprocess.Popen(cmd, env=env)
        code = child.wait()
        if stopping or code in DONT_RESTART:
            stop.set()
            time.sleep(2)  # let the publisher mark the entities unavailable
            return code
        log(f"collector exited with code {code}; restarting in 60 s")
        for _ in range(60):
            if stopping:
                return code
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
