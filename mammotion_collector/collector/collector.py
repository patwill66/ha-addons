"""Ingestion (provider-independent) and the REST polling collector.

`Ingestor` turns one observation of a device into stored history: device row, telemetry sample,
state events and session updates, all in one transaction and all reconstructed from SQLite on
restart. It knows nothing about HTTP — a future SSE or LAN provider can feed it the same way.

`Collector` is the REST provider: it schedules polls, error/plan syncs and sparse work-params
snapshots, and backs off on failures.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .api import ApiError, AuthError, TransientError
from .config import mask, seconds_between, utcnow
from .derive import TRACKED_FIELDS, SessionDeriver, classify, detect_changes, has_telemetry
from .models import device_meta, normalize_detail, normalize_work_params, raw_detail_json

log = logging.getLogger("collector")


def ms_to_iso(ms):
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


@dataclass
class IngestResult:
    device_id: int
    sample_id: int
    sample: object
    prev: object             # last earlier sample WITH telemetry
    changes: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    prev_any: object = None  # immediately preceding sample (may lack telemetry)


class Ingestor:
    def __init__(self, store, gap_threshold_seconds: int = 180):
        self.store = store
        self.gap_threshold = gap_threshold_seconds

    def ingest_detail(self, mammotion_id: str, data: dict, observed_at: str, request_id=None, run_id=None) -> IngestResult:
        sample = normalize_detail(data, observed_at)
        with self.store.conn:
            device_id = self.store.upsert_device(mammotion_id, device_meta(data), observed_at)
            # SQLite, not memory, is the source of truth for everything "previous".
            prev = self.store.latest_sample(device_id)
            prev_real = self.store.latest_real_sample(device_id)
            known = self.store.last_known_values(device_id, [col for _, col in TRACKED_FIELDS])
            sample_id = self.store.insert_sample(device_id, run_id, sample, request_id, raw_detail_json(data))
            row = self.store.get_sample(sample_id)
            gap = seconds_between(prev["observed_at"], observed_at) if prev else None
            changes = detect_changes(known, row)
            for event_type, old, new in changes:
                self.store.insert_event(device_id, row, prev, event_type, old, new, gap)
            notes = SessionDeriver(self.store, device_id, self.gap_threshold).apply(row, prev_real)
        return IngestResult(device_id, sample_id, row, prev_real, changes, notes, prev)


class Collector:
    def __init__(self, settings, client, store, tokens, clock_now=utcnow):
        self.s = settings
        self.client = client
        self.store = store
        self.tokens = tokens
        self.now = clock_now
        self.ingestor = Ingestor(store, settings.gap_threshold_seconds)
        self.stop = threading.Event()
        self.run_id = None
        self.mowers = []   # mammotion device ids found at startup
        self.devices = {}  # mammotion_id -> db device id
        self.failures = 0
        self.polls_ok = self.polls_failed = 0
        self._last_error_sync = self._last_plan_sync = 0.0

    # --- lifecycle ----------------------------------------------------------------------------
    def run(self, max_seconds=None, backfill_errors=False) -> str:
        started = time.monotonic()
        orphaned = self.store.close_orphaned_runs(self.now())
        if orphaned:
            log.warning("Previous run(s) ended without a clean shutdown: %d", orphaned)
        self.run_id = self.store.start_run(self.now(), self.s.poll_seconds)
        log.info("Collector started (run #%s, poll every %ss, db %s)", self.run_id, self.s.poll_seconds, self.s.db_path.name)
        reason = "stopped"
        try:
            self.mowers = self._discover() or []
            if not self.mowers:
                reason = "stopped before mower discovery"
                return reason
            self._sync_errors(full=backfill_errors)
            self._sync_plan()
            for mid in self.mowers:
                self._snapshot_work_params(mid, "collector startup")
            next_poll = time.monotonic()
            while not self.stop.is_set():
                if max_seconds is not None and time.monotonic() - started >= max_seconds:
                    reason = f"duration limit ({max_seconds}s) reached"
                    break
                if time.monotonic() >= next_poll:
                    ok = self._poll_all(self.mowers)
                    self._periodic()
                    delay = self.s.poll_seconds if ok else self._backoff()
                    next_poll = time.monotonic() + delay
                self.stop.wait(max(0.0, min(1.0, next_poll - time.monotonic())))
            else:
                reason = "signal received"
        except AuthError as e:
            reason = f"authentication failed: {e}"
            log.error("%s", reason)
        finally:
            self.store.update_run(self.run_id, ended_at=self.now(), stop_reason=reason,
                                  auth_count=self.tokens.auth_count, polls_ok=self.polls_ok,
                                  polls_failed=self.polls_failed)
            log.info("Collector stopped: %s (polls ok %d, failed %d, logins %d)",
                     reason, self.polls_ok, self.polls_failed, self.tokens.auth_count)
        return reason

    def _backoff(self) -> int:
        delay = min(self.s.poll_seconds * 2 ** (self.failures - 1), self.s.max_backoff_seconds)
        log.warning("Backing off: next attempt in %ss (%d consecutive failures)", delay, self.failures)
        return delay

    def _discover(self):
        while not self.stop.is_set():
            try:
                mowers = [m for m in self.client.list_mowers() if isinstance(m, dict) and m.get("id")]
                for m in mowers:
                    log.info("Mower found: %s (%s), online=%s", m.get("model"), mask(m.get("id")), m.get("online"))
                if not mowers:
                    log.warning("No mowers on this account; retrying later")
                else:
                    self.failures = 0
                    return [m["id"] for m in mowers]
            except (TransientError, ApiError) as e:
                if isinstance(e, AuthError):
                    raise
                log.warning("Mower discovery failed: %s", e)
            self.failures += 1
            self.stop.wait(self._backoff())
        return None

    # --- polling ------------------------------------------------------------------------------
    def _poll_all(self, mowers) -> bool:
        ok = True
        for mid in mowers:
            try:
                data, request_id = self.client.device_detail(mid)
                if not isinstance(data, dict):
                    raise ApiError(None, "device detail had no data object", request_id)
                result = self.ingestor.ingest_detail(mid, data, self.now(), request_id, self.run_id)
                self.devices[mid] = result.device_id
                self._report(mid, result)
                self.polls_ok += 1
            except AuthError as e:
                ok = False
                self.polls_failed += 1
                log.error("Poll rejected (auth): %s", e)
            except (TransientError, ApiError) as e:
                ok = False
                self.polls_failed += 1
                log.warning("Poll failed for %s: %s", mask(mid), e)
        self.failures = 0 if ok else self.failures + 1
        if self.polls_ok and self.polls_ok % 20 == 0:
            self.store.update_run(self.run_id, polls_ok=self.polls_ok, polls_failed=self.polls_failed,
                                  auth_count=self.tokens.auth_count)
        return ok

    def _report(self, mid: str, r: IngestResult) -> None:
        s = r.sample
        log.debug("Poll %s: %s, battery %s%%, charging %s, net %s, wifi %s / cell %s dBm", mask(mid),
                  s["raw_status"], s["battery_level"], s["charge_status"], s["used_network"],
                  s["wifi_rssi"], s["cellular_rssi"])
        if not has_telemetry(s):
            if r.prev_any is None or has_telemetry(r.prev_any):  # log once per streak
                log.info("Poll returned no device state (online=%s); keeping previous state", s["online"])
            for event_type, old, new in r.changes:  # e.g. online 1 → 0 still matters
                log.info("Event %s: %s → %s", event_type, old, new)
            return
        if r.prev is None:
            log.info("First observation: %s, battery %s%%, charging %s", s["raw_status"], s["battery_level"], s["charge_status"])
        for event_type, old, new in r.changes:
            log.info("Event %s: %s → %s (battery %s%%)", event_type, old, new, s["battery_level"])
            if event_type == "status_changed" and classify(s) == "mowing":
                self._snapshot_work_params(mid, f"entered {new}")
        if r.prev is not None and r.prev["battery_level"] is not None and s["battery_level"] is not None:
            if r.prev["battery_level"] // 10 != s["battery_level"] // 10:
                log.info("Battery %s%% → %s%%", r.prev["battery_level"], s["battery_level"])
        for note in r.notes:
            log.info("Session: %s", note)

    # --- low-frequency jobs -------------------------------------------------------------------
    def _periodic(self) -> None:
        now = time.monotonic()
        if now - self._last_error_sync >= self.s.error_sync_seconds:
            self._sync_errors()
        if now - self._last_plan_sync >= self.s.plan_sync_seconds:
            self._sync_plan()

    def _device_id(self, mid: str) -> int:
        if mid not in self.devices:
            with self.store.conn:
                self.devices[mid] = self.store.upsert_device(mid, {}, self.now())
        return self.devices[mid]

    def _sync_errors(self, full: bool = False) -> None:
        """Page newest-first until a page contains already-known records (or history ends)."""
        self._last_error_sync = time.monotonic()
        for mid in self.mowers:
            dev = self._device_id(mid)
            total_new, page = 0, 1
            try:
                while not self.stop.is_set():
                    data = self.client.error_codes(mid, page)
                    records = data.get("records") or []
                    new = self.store.insert_errors(dev, records, self.now(), ms_to_iso)
                    total_new += new
                    if not records or not data.get("hasMore") or (new < len(records) and not full):
                        break
                    page += 1
                    self.stop.wait(1)  # gentle pacing between pages
                if total_new:
                    log.info("Error history: imported %d new record(s) for %s", total_new, mask(mid))
                else:
                    log.debug("Error history: up to date for %s", mask(mid))
            except (TransientError, ApiError) as e:
                if isinstance(e, AuthError):
                    raise
                log.warning("Error-history sync failed: %s", e)

    def _sync_plan(self) -> None:
        self._last_plan_sync = time.monotonic()
        for mid in self.mowers:
            try:
                tasks = self.client.plan(mid)
                new, gone = self.store.snapshot_tasks(self._device_id(mid), tasks, self.now())
                log.info("Saved tasks: %d listed (%d new, %d no longer listed)", len(tasks), new, gone)
            except (TransientError, ApiError) as e:
                if isinstance(e, AuthError):
                    raise
                log.warning("Plan sync failed: %s", e)

    def _snapshot_work_params(self, mid: str, reason: str) -> None:
        """/work-params sends a command to the mower, so at most one snapshot per interval — checked
        against SQLite so that restarts don't trigger extra commands."""
        last = self.store.latest_work_params_at(self._device_id(mid))
        if last is not None and seconds_between(last, self.now()) < self.s.work_params_min_seconds:
            log.info("Work params snapshot skipped (%s): last one at %s is under %ss old",
                     reason, last, self.s.work_params_min_seconds)
            return
        try:
            data = self.client.work_params(mid)
            self.store.insert_work_params(self._device_id(mid), self.now(), reason, normalize_work_params(data))
            log.info("Work params snapshot (%s): blade height %s, spacing %s, speed %s",
                     reason, data.get("knifeHeight"), data.get("channelWidth"), data.get("speed"))
        except (TransientError, ApiError) as e:
            if isinstance(e, AuthError):
                raise
            log.warning("Work params snapshot failed: %s", e)
