"""SQLite storage. The schema evolves through numbered migrations tracked in PRAGMA user_version.

Conventions: `ts`/`*_at` columns are UTC epoch seconds (INTEGER); `hour` is the epoch of the start of
a UTC hour; `day` is a local calendar date 'YYYY-MM-DD' in the network's time zone (eero's own day
boundaries). Byte counts are eero's counters, never estimates. High-volume tables are WITHOUT ROWID
with the natural key first, which keeps them compact on the Green.
"""

import json
import sqlite3
import time
from pathlib import Path

MIGRATIONS = [
    # 1 - initial schema
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, ended_at INTEGER,
        version TEXT, stop_reason TEXT
    );
    CREATE TABLE task_state (
        name TEXT PRIMARY KEY, last_run_at INTEGER, last_ok_at INTEGER, last_error TEXT,
        last_error_at INTEGER, runs INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE api_calls_hourly (
        hour INTEGER NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, status INTEGER NOT NULL,
        calls INTEGER NOT NULL, ms_total INTEGER NOT NULL, bytes INTEGER NOT NULL,
        PRIMARY KEY (hour, method, path, status)
    ) WITHOUT ROWID;
    -- One row per data source; requires_premium = Eero Plus. status: ok | not_entitled | error | unknown
    CREATE TABLE capabilities (
        name TEXT PRIMARY KEY, requires_premium INTEGER NOT NULL, status TEXT NOT NULL,
        checked_at INTEGER, detail TEXT
    );
    CREATE TABLE backfill (name TEXT PRIMARY KEY, cursor TEXT, done INTEGER NOT NULL DEFAULT 0, updated_at INTEGER);

    CREATE TABLE network_samples (
        ts INTEGER PRIMARY KEY, status TEXT, internet_status TEXT, isp_up INTEGER, mesh_status TEXT,
        online_eeros INTEGER, offline_eeros INTEGER, clients_connected INTEGER, clients_wireless INTEGER,
        clients_wired INTEGER, clients_guest INTEGER, wan_ip TEXT, premium_status TEXT
    );

    CREATE TABLE nodes (
        id INTEGER PRIMARY KEY,               -- eero's node id
        serial TEXT, mac TEXT, model TEXT, location TEXT, is_gateway INTEGER, ip TEXT,
        os_version TEXT, first_seen_at INTEGER NOT NULL, last_seen_at INTEGER NOT NULL
    );
    CREATE TABLE node_samples (
        node_id INTEGER NOT NULL, ts INTEGER NOT NULL,
        status TEXT, state TEXT, heartbeat_ok INTEGER, last_heartbeat_at INTEGER,
        uptime_s INTEGER, cloud_uptime_s INTEGER, last_reboot_at INTEGER,
        mesh_bars INTEGER, connection_type TEXT, upstream TEXT, upstream_radio TEXT,
        clients INTEGER, clients_wired INTEGER, clients_wireless INTEGER,
        os_version TEXT, update_available INTEGER,
        PRIMARY KEY (node_id, ts)
    ) WITHOUT ROWID;
    CREATE TABLE radio_samples (
        node_id INTEGER NOT NULL, band TEXT NOT NULL, ts INTEGER NOT NULL,
        channel INTEGER, width_mhz INTEGER, tx_power INTEGER, utilization INTEGER, clients INTEGER,
        PRIMARY KEY (node_id, band, ts)
    ) WITHOUT ROWID;

    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        mac TEXT NOT NULL UNIQUE,
        url TEXT, nickname TEXT, display_name TEXT, hostname TEXT, manufacturer TEXT,
        device_type TEXT, model_name TEXT, inferred_make TEXT, inferred_model TEXT,
        is_private INTEGER, is_guest INTEGER, connection_type TEXT, profile TEXT,
        eero_first_seen TEXT, eero_last_active TEXT,
        first_seen_at INTEGER NOT NULL,       -- when this collector first saw it
        last_connected_at INTEGER,            -- last poll that found it connected
        connected INTEGER, node_id INTEGER, band TEXT, channel INTEGER, ip TEXT,
        updated_at INTEGER NOT NULL
    );
    -- Connected devices only, one row per poll.
    CREATE TABLE device_samples (
        device_id INTEGER NOT NULL, ts INTEGER NOT NULL,
        node_id INTEGER, band TEXT, channel INTEGER, signal INTEGER, snr INTEGER,
        score_bars INTEGER, score_milli INTEGER, tx_retry_pct INTEGER,
        rx_rate_mbps REAL, tx_rate_mbps REAL, rx_drop_ppm INTEGER, tx_fail_ppm INTEGER,
        PRIMARY KEY (device_id, ts)
    ) WITHOUT ROWID;
    -- Hourly rollup of device_samples, kept after raw samples are pruned.
    CREATE TABLE device_hourly (
        device_id INTEGER NOT NULL, hour INTEGER NOT NULL,
        samples INTEGER NOT NULL, signal_avg REAL, signal_min INTEGER, score_bars_min INTEGER,
        tx_retry_avg REAL, nodes TEXT, bands TEXT,
        PRIMARY KEY (device_id, hour)
    ) WITHOUT ROWID;
    -- Changes seen between polls (source 'poll') or reported by eero's event feed (source 'event').
    CREATE TABLE device_changes (
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, device_id INTEGER NOT NULL,
        kind TEXT NOT NULL, old TEXT, new TEXT, source TEXT NOT NULL
    );
    CREATE INDEX ix_changes_device_ts ON device_changes(device_id, ts);
    CREATE INDEX ix_changes_ts ON device_changes(ts);

    -- eero's own event feed (app_events), de-duplicated by its hash.
    CREATE TABLE events (
        hash TEXT PRIMARY KEY, ts_ms INTEGER NOT NULL, category TEXT, description TEXT,
        action TEXT, device_name TEXT, node_location TEXT, band TEXT, channel INTEGER, device_id INTEGER
    );
    CREATE INDEX ix_events_ts ON events(ts_ms);

    CREATE TABLE usage_network_hourly (
        hour INTEGER PRIMARY KEY, down INTEGER NOT NULL, up INTEGER NOT NULL,
        complete INTEGER NOT NULL, fetched_at INTEGER NOT NULL
    );
    CREATE TABLE usage_network_daily (
        day TEXT PRIMARY KEY, down INTEGER NOT NULL, up INTEGER NOT NULL,
        complete INTEGER NOT NULL, fetched_at INTEGER NOT NULL
    );
    CREATE TABLE usage_device_hourly (
        device_id INTEGER NOT NULL, hour INTEGER NOT NULL, down INTEGER NOT NULL, up INTEGER NOT NULL,
        PRIMARY KEY (device_id, hour)
    ) WITHOUT ROWID;
    CREATE TABLE usage_device_daily (
        device_id INTEGER NOT NULL, day TEXT NOT NULL, down INTEGER NOT NULL, up INTEGER NOT NULL,
        complete INTEGER NOT NULL,
        PRIMARY KEY (device_id, day)
    ) WITHOUT ROWID;
    -- Which device-usage windows have been fetched (a device with no traffic has no row).
    CREATE TABLE usage_device_windows (kind TEXT NOT NULL, start TEXT NOT NULL, fetched_at INTEGER NOT NULL,
        devices INTEGER, PRIMARY KEY (kind, start)) WITHOUT ROWID;

    CREATE TABLE speedtests (ts INTEGER PRIMARY KEY, down_mbps REAL, up_mbps REAL);
    CREATE TABLE notifications (
        uuid TEXT PRIMARY KEY, ts INTEGER NOT NULL, category TEXT, title TEXT, body TEXT, device_url TEXT
    );
    -- Eero Plus endpoints whose response shape isn't known yet: stored raw, modeled later.
    CREATE TABLE premium_raw (
        id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, endpoint TEXT NOT NULL,
        window_start TEXT, window_end TEXT, json TEXT NOT NULL
    );
    """,
]


def connect(path, readonly=False):
    if readonly:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def migrate(db):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with db:
            db.executescript(sql)
            db.execute(f"PRAGMA user_version={i}")
    return db.execute("PRAGMA user_version").fetchone()[0]


class Store:
    """Write access for the collector. Every method commits its own transaction."""

    def __init__(self, path):
        self.path = str(path)
        self.db = connect(self.path)
        migrate(self.db)

    def close(self):
        self.db.close()

    # --- bookkeeping ---------------------------------------------------------------------------
    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (key, json.dumps(value)))

    def start_run(self, version):
        with self.db:
            return self.db.execute("INSERT INTO runs(started_at, version) VALUES(?,?)",
                                   (int(time.time()), version)).lastrowid

    def end_run(self, run_id, reason):
        with self.db:
            self.db.execute("UPDATE runs SET ended_at=?, stop_reason=? WHERE id=?", (int(time.time()), reason, run_id))

    def record_call(self, method, path, status, ms, size, now=None):
        hour = int(now or time.time()) // 3600 * 3600
        with self.db:
            self.db.execute("""INSERT INTO api_calls_hourly VALUES(?,?,?,?,1,?,?)
                ON CONFLICT DO UPDATE SET calls=calls+1, ms_total=ms_total+excluded.ms_total, bytes=bytes+excluded.bytes""",
                            (hour, method, path, status, ms, size))

    def task_result(self, name, error=None, now=None):
        now = int(now or time.time())
        with self.db:
            self.db.execute("INSERT INTO task_state(name) VALUES(?) ON CONFLICT DO NOTHING", (name,))
            if error is None:
                self.db.execute("UPDATE task_state SET last_run_at=?, last_ok_at=?, runs=runs+1 WHERE name=?",
                                (now, now, name))
            else:
                self.db.execute("""UPDATE task_state SET last_run_at=?, last_error=?, last_error_at=?, runs=runs+1,
                    failures=failures+1 WHERE name=?""", (now, str(error)[:300], now, name))

    def task_last_ok(self, name):
        row = self.db.execute("SELECT last_ok_at FROM task_state WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def set_capability(self, name, requires_premium, status, detail=None, now=None):
        with self.db:
            self.db.execute("""INSERT INTO capabilities VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET
                requires_premium=excluded.requires_premium, status=excluded.status, checked_at=excluded.checked_at,
                detail=excluded.detail""", (name, int(requires_premium), status, int(now or time.time()), detail))

    def capability(self, name):
        row = self.db.execute("SELECT * FROM capabilities WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def backfill_state(self, name):
        row = self.db.execute("SELECT cursor, done FROM backfill WHERE name=?", (name,)).fetchone()
        return (row[0], bool(row[1])) if row else (None, False)

    def set_backfill(self, name, cursor, done=False):
        with self.db:
            self.db.execute("""INSERT INTO backfill VALUES(?,?,?,?) ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,
                done=excluded.done, updated_at=excluded.updated_at""", (name, cursor, int(done), int(time.time())))
