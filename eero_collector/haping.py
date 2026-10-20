"""Copies Home Assistant's Ping readings into the collector database, so mesh tests keep their latency data
after HA's recorder has purged it, and computes the historical baseline once that data is there.

Targets and entities (missing entities are skipped):
  gateway                      sensor.ping_eero_gateway_rtt_average / _packet_loss, binary_sensor.ping_eero_gateway
  each satellite eero          sensor.ping_eero_<location slug>_rtt_average / _packet_loss, binary_sensor.ping_eero_<slug>
  internet_google, _cloudflare sensor.ping_internet_<name>_rtt_average / _packet_loss, binary_sensor.ping_internet_<name>
Metrics: rtt (ms), loss (%), up (1 reachable, 0 not, None unavailable). eero-network's collector.meshtest stores
them (raw for sample_retention_days, 5-minute aggregates for good) and decides what they mean.

Delivery: HA Core's WebSocket API through the Supervisor proxy (`homeassistant_api: true`), command
history/history_during_period, every INTERVAL. The first run backfills what the recorder still holds (up to
BACKFILL_DAYS). The cursor lives in /data/ping.json. Nothing here talks to eero.
"""

import datetime
import json
import pathlib
import re
import time

import hastats
from collector import db as store
from collector import meshtest

STATE = pathlib.Path("/data/ping.json")
INTERVAL = 300
BACKFILL_DAYS = 10
CHUNK = 86400  # fetch at most a day per request
OVERLAP = 120


def slug(location):
    return re.sub(r"[^a-z0-9]+", "_", location.lower()).strip("_")


def targets(satellites):
    """target -> {metric: entity_id}."""
    prefixes = {"gateway": "ping_eero_gateway", "internet_google": "ping_internet_google",
                "internet_cloudflare": "ping_internet_cloudflare"}
    prefixes.update({loc: f"ping_eero_{slug(loc)}" for loc in satellites})
    return {t: {"rtt": f"sensor.{p}_rtt_average", "loss": f"sensor.{p}_packet_loss", "up": f"binary_sensor.{p}"}
            for t, p in prefixes.items()}


def value(metric, state):
    if metric == "up":
        return {"on": 1, "off": 0}.get(state)
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def parse(result, entity_map):
    """history_during_period result -> {(target, metric): [(ts, value)]}."""
    by_entity = {e: (t, m) for t, ms in entity_map.items() for m, e in ms.items()}
    out = {}
    for entity, states in (result or {}).items():
        if entity not in by_entity:
            continue
        rows = []
        for s in states:
            t = s.get("lu") or s.get("lc")
            if t is not None:
                rows.append((int(t), value(by_entity[entity][1], s.get("s"))))
        out[by_entity[entity]] = rows
    return out


class Ingester:
    def __init__(self, token, db_path, log, node=None, baseline=None, tz=datetime.timezone.utc, retention_days=35,
                 connect=hastats.HAWebSocket, state=STATE):
        self.token, self.db_path, self.log, self.connect, self.state = token, db_path, log, connect, state
        self.node, self.baseline, self.tz, self.retention_days = node, baseline, tz, retention_days

    def cursor(self):
        try:
            return json.loads(self.state.read_text()).get("through")
        except (OSError, ValueError):
            return None

    def ingest_once(self, now=None):
        now = int(now or time.time())
        through = self.cursor()
        start = (through - OVERLAP) if through else now - BACKFILL_DAYS * 86400
        db = store.connect(self.db_path)
        rows = 0
        try:
            entity_map = targets(meshtest.satellites(db))
            fetched = []
            # Fetch everything, close, then write: the Supervisor's proxy drops a connection that stops answering
            # its pings, which a slow database write on the Green could cause.
            ws = self.connect(self.token)
            try:
                t = start
                while t < now:
                    end = min(t + CHUNK, now)
                    fetched.append(ws.call({"type": "history/history_during_period",
                                            "start_time": hastats.iso(t), "end_time": hastats.iso(end),
                                            "entity_ids": [e for ms in entity_map.values() for e in ms.values()],
                                            "include_start_time_state": True, "significant_changes_only": False,
                                            "minimal_response": True, "no_attributes": True}))
                    t = end
            finally:
                ws.close()
            for result in fetched:
                for (target, metric), readings in parse(result, entity_map).items():
                    rows += meshtest.ingest_ping(db, target, metric, readings, now, self.retention_days)
            self.state.write_text(json.dumps({"through": now}))
            if self.baseline and self.node:
                meshtest.ensure_baseline(db, self.node, self.baseline[0], self.baseline[1], now, self.tz)
        finally:
            db.close()
        return rows, through is None

    def run(self, stop):
        failing = False
        while not stop.is_set():
            try:
                if pathlib.Path(self.db_path).exists():
                    rows, first = self.ingest_once()
                    if first or failing:
                        self.log(f"stored {rows} Home Assistant ping readings" + (" (backfill)" if first else ""))
                failing = False
            except Exception as e:  # never let this affect collection or publishing
                if not failing:
                    self.log(f"ping import failed: {e}; retrying every {INTERVAL // 60} min")
                failing = True
            stop.wait(INTERVAL)


def parse_window(start, end):
    """Add-on options baseline_start / baseline_end (ISO 8601) -> (epoch, epoch) or None."""
    if not start or not end:
        return None
    a, b = (int(datetime.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()) for v in (start, end))
    return (a, b) if b > a else None
