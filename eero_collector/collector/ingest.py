"""Writes normalized eero data into the Store and derives what changed. No HTTP here.

Change kinds in device_changes: new, connected, disconnected, node (roamed), band, ip.
Poll-derived changes have the resolution of the poll interval; eero's event feed (events table)
has exact times but only covers what eero chose to report.
"""

import time

from . import models

DEVICE_COLUMNS = ("url", "nickname", "display_name", "hostname", "manufacturer", "device_type", "model_name",
                  "inferred_make", "inferred_model", "is_private", "is_guest", "connection_type", "profile")


class Ingestor:
    def __init__(self, store):
        self.store = store
        self.db = store.db

    # --- snapshot of network, nodes and devices (every poll) ---------------------------------------
    def snapshot(self, network, eeros, devices, now=None):
        now = int(now or time.time())
        with self.db:
            self.db.execute("""INSERT OR REPLACE INTO network_samples VALUES(:ts,:status,:internet_status,:isp_up,
                :mesh_status,:online_eeros,:offline_eeros,:clients_connected,:clients_wireless,:clients_wired,
                :clients_guest,:wan_ip,:premium_status)""", models.network_sample(network, devices, now))
            node_ids = {}
            for eero in eeros or []:
                rec, sample, radios = models.node(eero, now)
                if rec["id"] is None:
                    continue
                node_ids[rec["location"]] = rec["id"]
                self.db.execute("""INSERT INTO nodes VALUES(:id,:serial,:mac,:model,:location,:is_gateway,:ip,
                    :os_version,:now,:now) ON CONFLICT(id) DO UPDATE SET serial=excluded.serial, mac=excluded.mac,
                    model=excluded.model, location=excluded.location, is_gateway=excluded.is_gateway, ip=excluded.ip,
                    os_version=excluded.os_version, last_seen_at=excluded.last_seen_at""", {**rec, "now": now})
                self.db.execute("""INSERT OR REPLACE INTO node_samples VALUES(:node_id,:ts,:status,:state,:heartbeat_ok,
                    :last_heartbeat_at,:uptime_s,:cloud_uptime_s,:last_reboot_at,:mesh_bars,:connection_type,:upstream,
                    :upstream_radio,:clients,:clients_wired,:clients_wireless,:os_version,:update_available)""", sample)
                for r in radios:
                    self.db.execute("""INSERT OR REPLACE INTO radio_samples VALUES(:node_id,:band,:ts,:channel,
                        :width_mhz,:tx_power,:utilization,:clients)""", r)
            speed = models.latest_speedtest(network)
            if speed:
                self.db.execute("INSERT OR IGNORE INTO speedtests VALUES(:ts,:down_mbps,:up_mbps)", speed)
            if devices is not None:
                self._devices(devices, node_ids, now)

    def _devices(self, devices, node_ids, now):
        seen = set()
        for d in devices:
            ident = models.device_identity(d)
            if not ident["mac"]:
                continue
            state = models.device_state(d, node_ids)
            device_id, old = self._upsert_device(ident, now)
            seen.add(device_id)
            connected = state["connected"] == 1
            changes = []
            if old is None:
                changes.append(("new", None, ident.get("display_name") or ident.get("hostname") or ident["mac"]))
            if old is not None and bool(old["connected"]) != connected and old["connected"] is not None:
                changes.append(("connected" if connected else "disconnected", None, None))
            if connected and old is not None and old["connected"]:
                for kind, key in (("node", "node_id"), ("band", "band"), ("ip", "ip")):
                    if state[key] is not None and old[key] is not None and str(old[key]) != str(state[key]):
                        changes.append((kind, str(old[key]), str(state[key])))
            for kind, a, b in changes:
                self.db.execute("INSERT INTO device_changes(ts,device_id,kind,old,new,source) VALUES(?,?,?,?,?,'poll')",
                                (now, device_id, kind, a, b))
            sets = {"connected": int(connected), "eero_first_seen": state["eero_first_seen"],
                    "eero_last_active": state["eero_last_active"], "updated_at": now}
            if connected:
                sets.update(last_connected_at=now, node_id=state["node_id"], band=state["band"],
                            channel=state["channel"], ip=state["ip"] or (old["ip"] if old else None))
            self.db.execute(f"UPDATE devices SET {', '.join(f'{k}=:{k}' for k in sets)} WHERE id=:id",
                            {**sets, "id": device_id})
            if connected:
                s = state["sample"]
                self.db.execute("""INSERT OR REPLACE INTO device_samples VALUES(:device_id,:ts,:node_id,:band,:channel,
                    :signal,:snr,:score_bars,:score_milli,:tx_retry_pct,:rx_rate_mbps,:tx_rate_mbps,:rx_drop_ppm,
                    :tx_fail_ppm)""", {**s, "device_id": device_id, "ts": now})
        # Devices that dropped out of the list entirely count as disconnected.
        for row in self.db.execute("SELECT id FROM devices WHERE connected=1").fetchall():
            if row["id"] not in seen:
                self.db.execute("UPDATE devices SET connected=0, updated_at=? WHERE id=?", (now, row["id"]))
                self.db.execute("INSERT INTO device_changes(ts,device_id,kind,source) VALUES(?,?,'disconnected','poll')",
                                (now, row["id"]))

    def _upsert_device(self, ident, now):
        """Returns (device id, previous row or None if new). Identity fields follow eero's latest values."""
        old = self.db.execute("SELECT * FROM devices WHERE mac=?", (ident["mac"],)).fetchone()
        values = {k: ident.get(k) for k in DEVICE_COLUMNS}
        if old is None:
            cols = ("mac",) + DEVICE_COLUMNS + ("first_seen_at", "updated_at")
            device_id = self.db.execute(
                f"INSERT INTO devices({', '.join(cols)}) VALUES({', '.join('?' * len(cols))})",
                (ident["mac"], *values.values(), now, now)).lastrowid
            return device_id, None
        changed = {k: v for k, v in values.items() if v is not None and v != old[k]}
        if changed:
            self.db.execute(f"UPDATE devices SET {', '.join(f'{k}=?' for k in changed)} WHERE id=?",
                            (*changed.values(), old["id"]))
        return old["id"], old

    def device_id_for(self, ident, now=None):
        """Device id for an identity seen outside the devices list (usage responses); creates it if needed."""
        with self.db:
            device_id, _ = self._upsert_device(ident, int(now or time.time()))
        return device_id

    # --- eero's event feed -------------------------------------------------------------------------
    def events(self, items):
        """Inserts new events; returns how many were new. Device names are matched when unambiguous."""
        new = 0
        with self.db:
            for raw in items or []:
                e = models.event(raw)
                if not e["hash"] or e["ts_ms"] is None:
                    continue
                e["device_id"] = self._device_by_name(e["device_name"])
                cur = self.db.execute("""INSERT OR IGNORE INTO events VALUES(:hash,:ts_ms,:category,:description,
                    :action,:device_name,:node_location,:band,:channel,:device_id)""", e)
                if cur.rowcount:
                    new += 1
                    if e["device_id"] and e["action"]:
                        self.db.execute("""INSERT INTO device_changes(ts,device_id,kind,old,new,source)
                            VALUES(?,?,?,NULL,?,'event')""", (e["ts_ms"] // 1000, e["device_id"], e["action"],
                                                              e["node_location"]))
        return new

    def _device_by_name(self, name):
        if not name:
            return None
        rows = self.db.execute("""SELECT id FROM devices WHERE display_name=? OR nickname=? OR hostname=?
            ORDER BY last_connected_at DESC""", (name, name, name)).fetchall()
        return rows[0]["id"] if len(rows) == 1 else None

    # --- usage counters ----------------------------------------------------------------------------
    def network_usage(self, data, cadence, now=None, day_of=None):
        """Upserts eero's network series. A bucket is complete once its end has passed."""
        now = int(now or time.time())
        n = 0
        with self.db:
            for start, (down, up) in models.usage_series(data).items():
                if cadence == "hourly":
                    self.db.execute("""INSERT INTO usage_network_hourly VALUES(?,?,?,?,?) ON CONFLICT(hour) DO UPDATE SET
                        down=excluded.down, up=excluded.up, complete=excluded.complete, fetched_at=excluded.fetched_at""",
                                    (start, down, up, int(start + 3600 <= now), now))
                else:
                    day = day_of(start)
                    self.db.execute("""INSERT INTO usage_network_daily VALUES(?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET
                        down=excluded.down, up=excluded.up, complete=excluded.complete, fetched_at=excluded.fetched_at""",
                                    (day, down, up, int(start + 86400 <= now), now))
                n += 1
        return n

    def device_usage(self, data, kind, start_key, complete, now=None):
        """kind 'hourly' (start_key = hour epoch) or 'daily' (start_key = local day). Returns rows written."""
        now = int(now or time.time())
        rows = models.device_usage(data)
        with self.db:
            for ident, down, up in rows:
                device_id, _ = self._upsert_device(ident, now)
                if kind == "hourly":
                    self.db.execute("INSERT OR REPLACE INTO usage_device_hourly VALUES(?,?,?,?)",
                                    (device_id, start_key, down, up))
                else:
                    self.db.execute("INSERT OR REPLACE INTO usage_device_daily VALUES(?,?,?,?,?)",
                                    (device_id, start_key, down, up, int(complete)))
            self.db.execute("INSERT OR REPLACE INTO usage_device_windows VALUES(?,?,?,?)",
                            (kind, str(start_key), now, len(rows)))
        return len(rows)

    def speedtests(self, items):
        with self.db:
            return sum(self.db.execute("INSERT OR IGNORE INTO speedtests VALUES(:ts,:down_mbps,:up_mbps)", s).rowcount
                       for s in models.speedtests(items))

    def notifications(self, items):
        with self.db:
            return sum(self.db.execute("""INSERT OR IGNORE INTO notifications VALUES(:uuid,:ts,:category,:title,
                :body,:device_url)""", n).rowcount for n in map(models.notification, items or []) if n["uuid"] and n["ts"])

    def premium_raw(self, endpoint, start, end, payload, now=None):
        import json
        with self.db:
            self.db.execute("INSERT INTO premium_raw(ts,endpoint,window_start,window_end,json) VALUES(?,?,?,?,?)",
                            (int(now or time.time()), endpoint, start, end, json.dumps(payload)))

    # --- retention -----------------------------------------------------------------------------------
    def rollup_and_prune(self, keep_days, now=None):
        """Rolls complete hours of device_samples into device_hourly, then deletes raw samples (device and
        radio/node) older than keep_days. Returns (hours rolled up, samples deleted)."""
        now = int(now or time.time())
        last = self.store.meta("rollup_through", 0)
        through = now // 3600 * 3600 - 3600  # last complete hour start
        with self.db:
            rolled = self.db.execute("""INSERT OR REPLACE INTO device_hourly
                SELECT device_id, ts/3600*3600 AS hour, count(*), avg(signal), min(signal), min(score_bars),
                       avg(tx_retry_pct), group_concat(DISTINCT node_id), group_concat(DISTINCT band)
                FROM device_samples WHERE ts >= ? AND ts < ? GROUP BY device_id, hour""", (last, through + 3600)).rowcount
            cutoff = now - keep_days * 86400
            deleted = self.db.execute("DELETE FROM device_samples WHERE ts < ?", (min(cutoff, through),)).rowcount
        self.store.set_meta("rollup_through", through + 3600)
        return rolled, deleted
