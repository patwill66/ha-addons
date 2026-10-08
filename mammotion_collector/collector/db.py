"""SQLite storage. Schema evolves through numbered migrations tracked in PRAGMA user_version."""

import json
import sqlite3
from pathlib import Path

MIGRATIONS = [
    # 1 — initial schema
    """
    CREATE TABLE devices (
        id INTEGER PRIMARY KEY,
        mammotion_id TEXT NOT NULL UNIQUE,      -- sensitive: never print in full
        name TEXT, nickname TEXT, model TEXT, firmware_version TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    );

    CREATE TABLE collector_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        poll_seconds INTEGER,
        auth_count INTEGER NOT NULL DEFAULT 0,
        polls_ok INTEGER NOT NULL DEFAULT 0,
        polls_failed INTEGER NOT NULL DEFAULT 0,
        stop_reason TEXT
    );

    CREATE TABLE telemetry_samples (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        run_id INTEGER REFERENCES collector_runs(id),
        observed_at TEXT NOT NULL,              -- UTC, when the response was received
        online INTEGER,
        raw_status TEXT,                        -- exactly as returned by the API
        battery_level INTEGER,
        charge_status INTEGER,
        used_network TEXT,
        wifi_available INTEGER,
        wifi_rssi INTEGER,
        cellular_available INTEGER,
        cellular_rssi INTEGER,
        firmware_version TEXT,
        request_id TEXT,
        raw_json TEXT NOT NULL                  -- the API `data` object minus wifiIp
    );
    CREATE INDEX ix_samples_device_time ON telemetry_samples(device_id, observed_at);

    CREATE TABLE state_events (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        observed_at TEXT NOT NULL,
        event_type TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        sample_id INTEGER NOT NULL REFERENCES telemetry_samples(id),
        prev_sample_id INTEGER REFERENCES telemetry_samples(id),
        gap_seconds INTEGER                     -- time since the previous observation
    );
    CREATE INDEX ix_events_device_time ON state_events(device_id, observed_at);

    CREATE TABLE error_events (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        dedupe_key TEXT NOT NULL,
        code INTEGER,
        implication TEXT,
        solution TEXT,
        gmt_create_ms INTEGER,
        create_time_ms INTEGER,
        occurred_at TEXT,                       -- gmtCreate as UTC ISO
        fault_level INTEGER,
        priority INTEGER,
        raw_json TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        UNIQUE (device_id, dedupe_key)
    );
    CREATE INDEX ix_errors_device_time ON error_events(device_id, gmt_create_ms);

    CREATE TABLE saved_tasks (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        task_id TEXT,
        task_name TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        is_present INTEGER NOT NULL DEFAULT 1,  -- in the most recent /plan snapshot
        UNIQUE (device_id, task_id, task_name)
    );

    CREATE TABLE work_parameter_snapshots (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        observed_at TEXT NOT NULL,
        reason TEXT,
        knife_height INTEGER, speed INTEGER, channel_width INTEGER, channel_mode INTEGER,
        job_content INTEGER, edge_mode INTEGER, toward INTEGER, toward_mode INTEGER,
        toward_included_angle INTEGER, ultra_wave INTEGER, boundary_zigzag_order INTEGER,
        forbidden_area_circle_times INTEGER, dump_period_sqm INTEGER, ride_boundary_distance REAL,
        raw_json TEXT NOT NULL
    );

    CREATE TABLE mowing_sessions (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        started_at TEXT NOT NULL,
        ended_at TEXT,                          -- NULL while the session is open
        start_sample_id INTEGER, end_sample_id INTEGER, last_sample_id INTEGER,
        last_class TEXT,
        start_battery INTEGER, end_battery INTEGER, lowest_battery INTEGER, battery_used INTEGER,
        elapsed_seconds INTEGER,
        pause_count INTEGER NOT NULL DEFAULT 0,
        end_status TEXT, end_reason TEXT,
        uncertain INTEGER NOT NULL DEFAULT 0,
        uncertain_reasons TEXT                  -- JSON list
    );
    CREATE INDEX ix_mowing_device_time ON mowing_sessions(device_id, started_at);

    CREATE TABLE charging_sessions (
        id INTEGER PRIMARY KEY,
        device_id INTEGER NOT NULL REFERENCES devices(id),
        started_at TEXT NOT NULL,
        ended_at TEXT,
        start_sample_id INTEGER, end_sample_id INTEGER, last_sample_id INTEGER,
        start_battery INTEGER, end_battery INTEGER, highest_battery INTEGER, battery_gained INTEGER,
        elapsed_seconds INTEGER,
        uncertain INTEGER NOT NULL DEFAULT 0,
        uncertain_reasons TEXT
    );
    CREATE INDEX ix_charging_device_time ON charging_sessions(device_id, started_at);
    """,
    # 2 — key/value metadata (e.g. derivation_version)
    """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """,
]

SAMPLE_FIELDS = ("observed_at", "online", "raw_status", "battery_level", "charge_status", "used_network",
                 "wifi_available", "wifi_rssi", "cellular_available", "cellular_rssi", "firmware_version")


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.migrate()

    def close(self):
        self.conn.close()

    # --- schema -------------------------------------------------------------------------------
    def schema_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def migrate(self) -> None:
        version = self.schema_version()
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            self.conn.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")

    # --- generic helpers ----------------------------------------------------------------------
    def _insert(self, table: str, row: dict) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        return self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())).lastrowid

    def _update(self, table: str, row_id: int, fields: dict) -> None:
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*fields.values(), row_id))

    # --- runs ---------------------------------------------------------------------------------
    def start_run(self, started_at: str, poll_seconds: int) -> int:
        with self.conn:
            return self._insert("collector_runs", {"started_at": started_at, "poll_seconds": poll_seconds})

    def close_orphaned_runs(self, ended_at: str) -> int:
        """Runs with no end time ended without a clean shutdown (crash, kill -9, power loss)."""
        with self.conn:
            return self.conn.execute(
                "UPDATE collector_runs SET ended_at = ?, stop_reason = 'ended without clean shutdown"
                " (detected at next start)' WHERE ended_at IS NULL", (ended_at,)).rowcount

    def get_meta(self, key: str):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)"
                              " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def update_run(self, run_id: int, **fields) -> None:
        with self.conn:
            self._update("collector_runs", run_id, fields)

    # --- devices / samples / events -----------------------------------------------------------
    def upsert_device(self, mammotion_id: str, meta: dict, seen_at: str) -> int:
        row = self.conn.execute("SELECT id FROM devices WHERE mammotion_id = ?", (mammotion_id,)).fetchone()
        fields = {k: v for k, v in meta.items() if v is not None}
        if row:
            self._update("devices", row["id"], {**fields, "last_seen_at": seen_at})
            return row["id"]
        return self._insert("devices", {"mammotion_id": mammotion_id, **fields,
                                        "first_seen_at": seen_at, "last_seen_at": seen_at})

    def latest_sample(self, device_id: int):
        return self.conn.execute(
            "SELECT * FROM telemetry_samples WHERE device_id = ? ORDER BY observed_at DESC, id DESC LIMIT 1",
            (device_id,)).fetchone()

    def insert_sample(self, device_id: int, run_id, sample: dict, request_id, raw_json: str) -> int:
        row = {"device_id": device_id, "run_id": run_id, **{k: sample.get(k) for k in SAMPLE_FIELDS},
               "request_id": request_id, "raw_json": raw_json}
        return self._insert("telemetry_samples", row)

    def latest_real_sample(self, device_id: int):
        """Most recent sample that carried device state (raw_status present)."""
        return self.conn.execute(
            "SELECT * FROM telemetry_samples WHERE device_id = ? AND raw_status IS NOT NULL"
            " ORDER BY observed_at DESC, id DESC LIMIT 1", (device_id,)).fetchone()

    def last_known_values(self, device_id: int, columns) -> dict:
        """Latest non-null value of each column (columns come from a fixed internal list)."""
        known = {}
        for col in columns:
            row = self.conn.execute(
                f"SELECT {col} FROM telemetry_samples WHERE device_id = ? AND {col} IS NOT NULL"
                " ORDER BY observed_at DESC, id DESC LIMIT 1", (device_id,)).fetchone()
            if row is not None:
                known[col] = row[0]
        return known

    def get_sample(self, sample_id: int):
        return self.conn.execute("SELECT * FROM telemetry_samples WHERE id = ?", (sample_id,)).fetchone()

    def iter_samples(self, device_id: int):
        return self.conn.execute(
            "SELECT * FROM telemetry_samples WHERE device_id = ? ORDER BY observed_at, id", (device_id,))

    def insert_event(self, device_id: int, sample, prev, event_type: str, old, new, gap_seconds) -> int:
        return self._insert("state_events", {
            "device_id": device_id, "observed_at": sample["observed_at"], "event_type": event_type,
            "old_value": None if old is None else str(old), "new_value": None if new is None else str(new),
            "sample_id": sample["id"], "prev_sample_id": prev["id"] if prev else None, "gap_seconds": gap_seconds})

    # --- sessions -----------------------------------------------------------------------------
    def open_session(self, table: str, device_id: int):
        return self.conn.execute(
            f"SELECT * FROM {table} WHERE device_id = ? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
            (device_id,)).fetchone()

    def insert_session(self, table: str, row: dict) -> int:
        return self._insert(table, row)

    def update_session(self, table: str, session_id: int, fields: dict) -> None:
        self._update(table, session_id, fields)

    def clear_derived(self, device_id: int) -> None:
        with self.conn:
            for table in ("state_events", "mowing_sessions", "charging_sessions"):
                self.conn.execute(f"DELETE FROM {table} WHERE device_id = ?", (device_id,))

    # --- errors / tasks / work params ---------------------------------------------------------
    def insert_errors(self, device_id: int, records: list, imported_at: str, to_iso) -> int:
        """Idempotent: a record already stored (same code + timestamps) is skipped. Returns new count."""
        inserted = 0
        with self.conn:
            for r in records:
                key = f"{r.get('code')}|{r.get('gmtCreate')}|{r.get('createTime')}"
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO error_events (device_id, dedupe_key, code, implication, solution,"
                    " gmt_create_ms, create_time_ms, occurred_at, fault_level, priority, raw_json, imported_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, key, r.get("code"), r.get("implication"), r.get("solution"), r.get("gmtCreate"),
                     r.get("createTime"), to_iso(r.get("gmtCreate")), r.get("faultLevel"), r.get("priority"),
                     json.dumps(r, separators=(",", ":"), ensure_ascii=False, sort_keys=True), imported_at))
                inserted += cur.rowcount
        return inserted

    def snapshot_tasks(self, device_id: int, tasks: list, seen_at: str) -> tuple[int, int]:
        """Upsert the current /plan list. Returns (new_tasks, tasks_no_longer_present)."""
        new = 0
        with self.conn:
            before = {(r["task_id"], r["task_name"]) for r in self.conn.execute(
                "SELECT task_id, task_name FROM saved_tasks WHERE device_id = ? AND is_present = 1", (device_id,))}
            self.conn.execute("UPDATE saved_tasks SET is_present = 0 WHERE device_id = ?", (device_id,))
            current = set()
            for t in tasks:
                key = (str(t.get("taskId")), t.get("taskName"))
                current.add(key)
                cur = self.conn.execute(
                    "UPDATE saved_tasks SET last_seen_at = ?, is_present = 1"
                    " WHERE device_id = ? AND task_id = ? AND task_name IS ?", (seen_at, device_id, *key))
                if cur.rowcount == 0:
                    self._insert("saved_tasks", {"device_id": device_id, "task_id": key[0], "task_name": key[1],
                                                 "first_seen_at": seen_at, "last_seen_at": seen_at})
                    new += 1
        return new, len(before - current)

    def latest_work_params_at(self, device_id: int):
        row = self.conn.execute("SELECT MAX(observed_at) t FROM work_parameter_snapshots WHERE device_id = ?",
                                (device_id,)).fetchone()
        return row["t"]

    def insert_work_params(self, device_id: int, observed_at: str, reason: str, normalized: dict) -> int:
        with self.conn:
            return self._insert("work_parameter_snapshots",
                                {"device_id": device_id, "observed_at": observed_at, "reason": reason, **normalized})
