"""The collector loop: decides what to fetch when, calls the read-only client, hands results to Ingestor.

One request at a time. Steady state is about 1 request a minute (snapshot = 3 GETs every 5 min, events
every 2 min, usage hourly, a handful daily); backfill adds one request every `backfill_pause` seconds
while it runs and stops when it reaches eero's history limit.

Failure policy:
- AuthRequired: stop calling eero; state 'login_required' until the session store changes (the user
  logged in again). Never retries a login on its own.
- RateLimited: pause everything for max(Retry-After, 5 min), doubling on repeats (cap 1 h).
- Other errors: the task fails and is retried with its own back-off (interval x 2^n, cap 30 min).
"""

import datetime
import logging
import time
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from . import models
from .api import AuthRequired, EeroError, PremiumRequired, RateLimited
from .ingest import Ingestor

log = logging.getLogger("eero")

# Data sources and whether they need Eero Plus. Free ones are verified on the free tier (2026-10-08);
# premium ones are fetched only when the network reports an active subscription, and stored raw until
# their shape is known.
CAPABILITIES = {
    "snapshot": False, "events": False, "usage_network": False, "usage_devices": False,
    "speedtests": False, "notifications": False,
    "channel_utilization_history": True, "insights_adblock": True, "insights_blocked": True,
    "insights_inspected": True,
}
PREMIUM_INSIGHTS = ("adblock", "blocked", "inspected")
CATCH_UP_HOURS = 6


@dataclass
class Settings:
    poll_seconds: int = 300
    events_seconds: int = 120
    sample_retention_days: int = 35
    backfill: bool = True
    backfill_device_hourly: bool = True
    backfill_pause: int = 15
    daily_hour: int = 3          # local hour after which the daily task runs
    timezone: str = "America/Chicago"


class Collector:
    def __init__(self, client, store, settings=None, clock=time.time, sleep=time.sleep):
        self.client, self.store, self.s = client, store, settings or Settings()
        self.ingest = Ingestor(store)
        self.clock, self.sleep = clock, sleep
        self.network_url = None
        self.network = None
        self.tz = ZoneInfo(self.s.timezone)
        self.paused_until = 0
        self.rate_backoff = 0
        self.task_failures = {}
        self.next_due = {}
        self.state = "starting"

    # --- helpers ----------------------------------------------------------------------------------
    def now(self):
        return int(self.clock())

    def day_of(self, epoch):
        return datetime.datetime.fromtimestamp(epoch, self.tz).strftime("%Y-%m-%d")

    def local_midnight(self, day):
        d = datetime.date.fromisoformat(day)
        return int(datetime.datetime(d.year, d.month, d.day, tzinfo=self.tz).timestamp())

    def window(self, start, end, cadence=None):
        p = {"start": models.iso_utc(start), "end": models.iso_utc(end), "timezone": self.s.timezone}
        if cadence:
            p["cadence"] = cadence
        return p

    def set_state(self, state):
        if state != self.state:
            quiet = {state, self.state} <= {"starting", "collecting", "backfilling"}
            log.log(logging.DEBUG if quiet else logging.INFO, "state: %s", state)
            self.state = state
            self.store.set_meta("state", state)

    # --- tasks ------------------------------------------------------------------------------------
    def discover(self):
        account = self.client.get("/2.2/account")
        networks = ((account or {}).get("networks") or {}).get("data") or []
        if not networks:
            raise EeroError("account has no networks")
        self.network_url = networks[0]["url"]
        self.store.set_meta("account_networks", len(networks))

    def task_snapshot(self):
        if not self.network_url:
            self.discover()
        net = self.client.get(self.network_url)
        res = net.get("resources") or {}
        eeros = self.client.get(res.get("eeros") or f"{self.network_url}/eeros")
        devices = self.client.get(res.get("devices") or f"{self.network_url}/devices")
        self.network = net
        tz = (net.get("timezone") or {}).get("value")
        if tz and tz != self.s.timezone:
            self.s.timezone, self.tz = tz, ZoneInfo(tz)
        self.ingest.snapshot(net, eeros, devices, self.now())
        self.store.set_meta("premium_status", net.get("premium_status"))
        self.store.set_capability("snapshot", False, "ok")

    def task_events(self):
        if not self.network_url:
            self.discover()
        data = self.client.get(f"{self.network_url}/app_events", {"page_size": 50})
        self.ingest.events((data or {}).get("events"))
        self.store.set_capability("events", False, "ok")

    def task_usage_hourly(self):
        now = self.now()
        hour = now // 3600 * 3600
        data = self.client.get(f"{self.network_url}/data_usage", self.window(hour - 47 * 3600, hour + 3600, "hourly"))
        self.ingest.network_usage(data, "hourly", now)
        self._remember_limit(data)
        self.store.set_capability("usage_network", False, "ok")
        # Per-device totals for complete hours not fetched yet, newest first, at most CATCH_UP_HOURS per run
        # (normally just one; older gaps are left to the paced backfill).
        fetched = {int(r[0]) for r in self.store.db.execute(
            "SELECT start FROM usage_device_windows WHERE kind='hourly' AND CAST(start AS INTEGER) >= ?",
            (hour - 48 * 3600,))}
        missing = [h for h in range(hour - 3600, hour - 49 * 3600, -3600) if h not in fetched]
        for h in missing[:CATCH_UP_HOURS]:
            self._device_hour(h)
        self.store.set_capability("usage_devices", False, "ok")

    def _device_hour(self, h):
        data = self.client.get(f"{self.network_url}/data_usage/devices", self.window(h, h + 3600, "hourly"))
        self.ingest.device_usage(data, "hourly", h, True, self.now())

    def _device_day(self, day, complete):
        start = self.local_midnight(day)
        end = min(start + 86400, self.now())
        data = self.client.get(f"{self.network_url}/data_usage/devices", self.window(start, end, "daily"))
        self.ingest.device_usage(data, "daily", day, complete, self.now())

    def task_daily(self):
        now = self.now()
        today = self.day_of(now)
        data = self.client.get(f"{self.network_url}/data_usage", self.window(now - 30 * 86400, now, "daily"))
        self.ingest.network_usage(data, "daily", now, self.day_of)
        self._remember_limit(data)
        done = {r[0] for r in self.store.db.execute(
            "SELECT start FROM usage_device_windows WHERE kind='daily'")}
        for back in range(1, 8):
            day = self.day_of(now - back * 86400)
            if day not in done or back == 1:
                self._device_day(day, complete=True)
        speed = self.client.get(f"{self.network_url}/speedtest", {"limit": 100})
        self.ingest.speedtests(speed if isinstance(speed, list) else [])
        self.store.set_capability("speedtests", False, "ok")
        notes = self.client.get(f"{self.network_url}/notifications_history")
        self.ingest.notifications((notes or {}).get("notifications"))
        self.store.set_capability("notifications", False, "ok")
        self.ingest.rollup_and_prune(self.s.sample_retention_days, now)
        self.store.set_meta("last_daily_day", today)
        self.premium()

    def premium(self):
        """Eero Plus sources: fetched only while the network reports an active subscription."""
        active = models.premium_active(self.network)
        status = (self.network or {}).get("premium_status")
        if not active:
            for name, needs in CAPABILITIES.items():
                if needs:
                    self.store.set_capability(name, True, "not_entitled", f"premium_status={status}")
            return
        now = self.now()
        start, end = now - 86400, now
        try:
            data = self.client.get(f"{self.network_url}/channel_utilization",
                                   {"start": models.iso_utc(start), "end": models.iso_utc(end)})
            self.ingest.premium_raw("channel_utilization", models.iso_utc(start), models.iso_utc(end), data, now)
            self.store.set_capability("channel_utilization_history", True, "ok")
        except PremiumRequired as e:
            self.store.set_capability("channel_utilization_history", True, "not_entitled", e.error)
        for kind in PREMIUM_INSIGHTS:
            try:
                p = self.window(now - 86400, now, "hourly")
                p["insight_type"] = kind
                data = self.client.get(f"{self.network_url}/insights", p)
                self.ingest.premium_raw(f"insights_{kind}", p["start"], p["end"], data, now)
                self.store.set_capability(f"insights_{kind}", True, "ok")
            except PremiumRequired as e:
                self.store.set_capability(f"insights_{kind}", True, "not_entitled", e.error)

    def _remember_limit(self, data):
        limit = models.ts((data or {}).get("limit"))
        if limit:
            self.store.set_meta("usage_limit", limit)

    # --- backfill ---------------------------------------------------------------------------------
    def current_backfill(self):
        for stage in ("network_daily", "network_hourly", "device_daily", "device_hourly"):
            if not self.store.backfill_state(stage)[1]:
                return stage
        return None

    def backfill_step(self):
        """One request of history backfill. Returns False when there's nothing left to do."""
        limit = self.store.meta("usage_limit")
        if not limit or not self.network_url:
            return False
        now = self.now()
        first_day = self.day_of(limit)
        # 1. network daily, 31-day windows
        cursor, done = self.store.backfill_state("network_daily")
        if not done:
            start = self.local_midnight(cursor or first_day)
            end = min(start + 31 * 86400, now)
            data = self.client.get(f"{self.network_url}/data_usage", self.window(start, end, "daily"))
            self.ingest.network_usage(data, "daily", now, self.day_of)
            self.store.set_backfill("network_daily", self.day_of(end), end >= now - 86400)
            return True
        # 2. network hourly, 7-day windows
        cursor, done = self.store.backfill_state("network_hourly")
        if not done:
            start = int(cursor) if cursor else limit // 3600 * 3600
            end = min(start + 7 * 86400, now // 3600 * 3600)
            data = self.client.get(f"{self.network_url}/data_usage", self.window(start, end, "hourly"))
            self.ingest.network_usage(data, "hourly", now)
            self.store.set_backfill("network_hourly", str(end), end >= now // 3600 * 3600 - 48 * 3600)
            return True
        # 3. per-device daily, one day per request, oldest first
        cursor, done = self.store.backfill_state("device_daily")
        if not done:
            day = cursor or first_day
            if day >= self.day_of(now - 7 * 86400):
                self.store.set_backfill("device_daily", day, True)
                return True
            self._device_day(day, complete=True)
            nxt = self.day_of(self.local_midnight(day) + 36 * 3600)  # next local day, DST-safe
            self.store.set_backfill("device_daily", nxt)
            return True
        # 4. per-device hourly, one hour per request (optional; ~2,400 requests for 100 days)
        if self.s.backfill_device_hourly:
            cursor, done = self.store.backfill_state("device_hourly")
            if not done:
                h = int(cursor) if cursor else limit // 3600 * 3600
                fetched = {int(r[0]) for r in self.store.db.execute(
                    "SELECT start FROM usage_device_windows WHERE kind='hourly' AND CAST(start AS INTEGER) >= ?", (h,))}
                while h in fetched:
                    h += 3600
                if h >= now // 3600 * 3600 - 3600:
                    self.store.set_backfill("device_hourly", str(h), True)
                    return True
                self._device_hour(h)
                self.store.set_backfill("device_hourly", str(h + 3600))
                return True
        return False

    # --- loop ---------------------------------------------------------------------------------------
    def due(self):
        """The name of the next task that is due, or None."""
        now = self.now()
        hour = now // 3600 * 3600
        local = datetime.datetime.fromtimestamp(now, self.tz)
        plan = [
            ("snapshot", self.s.poll_seconds),
            ("events", self.s.events_seconds),
        ]
        for name, interval in plan:
            if now >= self.next_due.get(name, 0):
                return name
        if self.network_url and now >= self.next_due.get("usage_hourly", 0) and now - hour >= 7 * 60 and \
                self.store.meta("usage_hour_done") != hour:
            return "usage_hourly"
        if self.network_url and now >= self.next_due.get("daily", 0) and local.hour >= self.s.daily_hour and \
                self.store.meta("last_daily_day") != self.day_of(now):
            return "daily"
        return None

    def run_task(self, name):
        intervals = {"snapshot": self.s.poll_seconds, "events": self.s.events_seconds, "usage_hourly": 600, "daily": 1800}
        fn = {"snapshot": self.task_snapshot, "events": self.task_events,
              "usage_hourly": self.task_usage_hourly, "daily": self.task_daily}[name]
        now = self.now()
        try:
            fn()
        except (AuthRequired, RateLimited):
            raise
        except Exception as e:  # one failing task must not stop the others
            n = self.task_failures.get(name, 0) + 1
            self.task_failures[name] = n
            delay = min(intervals[name] * 2 ** (n - 1), 1800)
            self.next_due[name] = now + delay
            self.store.task_result(name, e, now)
            log.warning("%s failed (%s); retry in %d s", name, e, delay)
            return
        self.task_failures[name] = 0
        if name == "usage_hourly":
            self.store.set_meta("usage_hour_done", now // 3600 * 3600)
        self.next_due[name] = now + intervals[name]
        self.store.task_result(name, None, now)

    def step(self):
        """Runs at most one request-bearing unit of work. Returns seconds to sleep before the next step."""
        now = self.now()
        if now < self.paused_until:
            return min(self.paused_until - now, 30)
        try:
            name = self.due()
            if name:
                self.run_task(name)
                self.set_state("collecting")
                self.rate_backoff = 0
                return 1
            if self.s.backfill and now >= self.next_due.get("backfill", 0):
                try:
                    if self.backfill_step():
                        self.set_state("backfilling")
                        return self.s.backfill_pause
                except PremiumRequired as e:
                    stage = self.current_backfill()
                    self.store.set_backfill(stage, self.store.backfill_state(stage)[0], True)
                    self.store.set_capability(f"backfill_{stage}", True, "not_entitled", e.error)
                    log.info("backfill %s needs Eero Plus; skipped", stage)
                    return 1
                except (AuthRequired, RateLimited):
                    raise
                except Exception as e:
                    self.next_due["backfill"] = now + 600
                    self.store.task_result("backfill", e, now)
                    log.warning("backfill paused for 10 min: %s", e)
                    return 5
            self.set_state("collecting")
            return 5
        except RateLimited as e:
            self.rate_backoff = min(max(self.rate_backoff * 2, 300), 3600)
            wait = max(e.retry_after or 0, self.rate_backoff)
            self.paused_until = now + int(wait)
            self.set_state("rate_limited")
            log.warning("rate limited by eero; pausing %d s", wait)
            return 30
        except AuthRequired as e:
            self.set_state("login_required")
            log.warning("eero session is not valid (%s); waiting for a new login", e)
            raise
