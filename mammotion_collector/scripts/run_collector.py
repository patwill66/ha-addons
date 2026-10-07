#!/usr/bin/env python3
"""Run the Mammotion REST historical collector until Ctrl-C / SIGTERM.

  python3 scripts/run_collector.py                 # poll every MAMMOTION_POLL_SECONDS (default 30)
  python3 scripts/run_collector.py --verbose       # also log every poll
  python3 scripts/run_collector.py --duration 600  # stop after 10 minutes (testing)
  python3 scripts/run_collector.py --backfill-errors   # page through the full error history once
  python3 scripts/run_collector.py --service       # launchd mode: rotating log file, quiet exits

Only one collector may run at a time (Mammotion allows 2 live tokens per client and a 3rd login
revokes the oldest). A lock file (data/collector.lock) is taken BEFORE logging in; a second
instance exits with code 75 without authenticating. Stop the launchd service before running
the collector manually:  scripts/service.sh stop
"""

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collector.api import MammotionClient  # noqa: E402
from collector.auth import TokenManager  # noqa: E402
from collector.collector import Collector  # noqa: E402
from collector.config import Settings  # noqa: E402
from collector.db import Store  # noqa: E402
from collector.lock import AlreadyRunning, InstanceLock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOCK_PATH = Path(os.environ.get("MAMMOTION_LOCK_PATH", str(ROOT / "data" / "collector.lock")))
EX_ALREADY_RUNNING = 75  # EX_TEMPFAIL: launchd may retry later; no login happens


def setup_logging(verbose: bool, service: bool) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    if service:
        LOG_DIR.mkdir(exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(LOG_DIR / "collector.log", maxBytes=2_000_000, backupCount=5)
        for name in ("collector.stderr.log", "collector.stdout.log"):  # launchd never rotates these
            trim_file(LOG_DIR / name)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(fmt)
    root.addHandler(handler)


def trim_file(path: Path, max_bytes: int = 1_000_000, keep_bytes: int = 256_000) -> None:
    """Bound a file launchd appends to (crash tracebacks): keep only its tail."""
    try:
        if path.stat().st_size > max_bytes:
            with open(path, "rb") as fh:
                fh.seek(-keep_bytes, os.SEEK_END)
                tail = fh.read()
            path.write_bytes(b"[earlier output trimmed]\n" + tail)
    except FileNotFoundError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true", help="log every poll")
    ap.add_argument("--duration", type=int, help="stop after this many seconds")
    ap.add_argument("--poll-seconds", type=int, help="override MAMMOTION_POLL_SECONDS (min 10)")
    ap.add_argument("--backfill-errors", action="store_true", help="import the whole error history")
    ap.add_argument("--service", action="store_true",
                    help="launchd mode: log to logs/collector.log (rotating); configuration errors exit 0 "
                         "so launchd does not retry them forever")
    args = ap.parse_args()

    setup_logging(args.verbose, args.service)
    log = logging.getLogger("collector")
    fatal_exit = 0 if args.service else 2

    try:
        lock = InstanceLock(str(LOCK_PATH)).acquire()  # before any login
    except AlreadyRunning as e:
        msg = f"Not starting: {e}. Stop it first (scripts/service.sh stop)."
        log.error(msg)  # terminal in foreground mode, logs/collector.log in service mode
        return EX_ALREADY_RUNNING

    settings = Settings.from_env()
    if args.poll_seconds:
        settings.poll_seconds = max(10, args.poll_seconds)
        settings.gap_threshold_seconds = max(180, 6 * settings.poll_seconds)
    if not settings.client_id or not settings.client_secret:
        log.error("MAMMOTION_CLIENT_ID / MAMMOTION_CLIENT_SECRET are not set (see .env.example)")
        lock.release()
        return fatal_exit

    tokens = TokenManager(settings.client_id, settings.client_secret,
                          on_auth=lambda reason, exp: log.info("Logged in (%s); token valid ~%.1f days",
                                                                reason, exp / 86400))
    store = Store(settings.db_path)
    collector = Collector(settings, MammotionClient(tokens), store, tokens)

    def _stop(signum, _frame):
        log.info("Received %s, shutting down…", signal.Signals(signum).name)
        collector.stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    try:
        reason = collector.run(max_seconds=args.duration, backfill_errors=args.backfill_errors)
    finally:
        store.close()
        lock.release()
    return fatal_exit if reason.startswith("authentication failed") else 0


if __name__ == "__main__":
    sys.exit(main())
