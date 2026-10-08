"""Publishes the collector's database to Home Assistant through MQTT discovery.

Every `interval` seconds it reads the database read-only, asks eero-network's collector.report for the
current summaries, and publishes one retained JSON state per HA device:
  eero_collector/network            -> device "eero Network"
  eero_collector/node/<node id>     -> one device per eero node
  eero_collector/client/<device id> -> one device per tracked client (option tracked_devices)
Discovery configs (homeassistant/<component>/eero_collector/<object>/config, retained) are sent at
start and again whenever Home Assistant announces itself on homeassistant/status. Configs of entities
that disappear (a client no longer tracked) are cleared. Availability follows the MQTT last will.
Nothing here talks to eero.
"""

import datetime
import json
import pathlib
import sqlite3
import time
import urllib.request
from zoneinfo import ZoneInfo

import mqtt
from collector import report

BASE = "eero_collector"
DISCOVERY = "homeassistant"
AVAIL = f"{BASE}/status"
APP = pathlib.Path(__file__).resolve().parent
KNOWN_TOPICS = pathlib.Path("/data/discovery_topics.json")


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat() if epoch else None


def band_slug(band):
    return band.replace(".", "_")


class Builder:
    """Discovery configs for one HA device."""

    def __init__(self, device, state_topic, prefix):
        self.device, self.state_topic, self.prefix = device, state_topic, prefix
        self.configs = {}

    def add(self, component, key, name, value, unit=None, device_class=None, state_class=None, icon=None,
            category=None, attrs=None, enabled=True, payload_on=None):
        object_id = f"{self.prefix}_{key}"
        c = {"name": name, "unique_id": object_id, "object_id": object_id, "state_topic": self.state_topic,
             "value_template": value, "availability_topic": AVAIL, "device": self.device}
        for k, v in (("unit_of_measurement", unit), ("device_class", device_class), ("state_class", state_class),
                     ("icon", icon), ("entity_category", category)):
            if v:
                c[k] = v
        if component == "binary_sensor":
            c["payload_on"], c["payload_off"] = payload_on or "ON", "OFF"
        if attrs:
            c["json_attributes_topic"] = self.state_topic
            c["json_attributes_template"] = "{{ {" + ", ".join(f'"{a}": value_json.{a}' for a in attrs) + "} | tojson }}"
        if not enabled:
            c["enabled_by_default"] = False
        self.configs[f"{DISCOVERY}/{component}/{BASE}/{object_id}/config"] = c


def on_off(field):
    # "None" is MQTT's payload for "unknown": a missing value must not read as off.
    return "{{ 'None' if value_json.%s is none else ('ON' if value_json.%s else 'OFF') }}" % (field, field)


def v(field):
    return "{{ value_json.%s }}" % field


def network_device(version):
    return {"identifiers": [f"{BASE}_network"], "name": "eero Network", "manufacturer": "eero",
            "model": "Mesh network (eero Collector)", "sw_version": version}


def network_configs(version):
    b = Builder(network_device(version), f"{BASE}/network", "eero_network")
    b.add("binary_sensor", "internet", "Internet", on_off("isp_up"), device_class="connectivity")
    b.add("sensor", "status", "Network status", v("status"), icon="mdi:router-network")
    b.add("sensor", "mesh_status", "Mesh status", v("mesh_status"), icon="mdi:access-point-network")
    b.add("sensor", "eeros_online", "eeros online", v("online_eeros"), unit="eeros", state_class="measurement",
          icon="mdi:router-wireless")
    for key, name in (("clients_connected", "Connected clients"), ("clients_wireless", "Wi-Fi clients"),
                      ("clients_wired", "Wired clients")):
        b.add("sensor", key, name, v(key), unit="clients", state_class="measurement", icon="mdi:devices")
    for d, word in (("down", "Download"), ("up", "Upload")):
        b.add("sensor", f"last_hour_{d}_mbps", f"{word} rate, last hour", v(f"last_hour_{d}_mbps"), unit="Mbit/s",
              device_class="data_rate", state_class="measurement", attrs=["last_hour_start_iso"])
        b.add("sensor", f"last_hour_{d}_mb", f"{word}ed, last hour", v(f"last_hour_{d}_mb"), unit="MB",
              device_class="data_size", state_class="measurement")
        b.add("sensor", f"today_{d}_gb", f"{word}ed today", v(f"today_{d}_gb"), unit="GB",
              device_class="data_size", state_class="total_increasing")
        b.add("sensor", f"speedtest_{d}", f"Speed test {word.lower()}", v(f"speedtest_{d}_mbps"), unit="Mbit/s",
              device_class="data_rate", state_class="measurement")
    b.add("sensor", "speedtest_at", "Last speed test", v("speedtest_at_iso"), device_class="timestamp")
    b.add("sensor", "weak_signal", "Weak-signal devices", v("weak_signal_count"), unit="devices",
          state_class="measurement", icon="mdi:wifi-strength-1-alert", attrs=["weak_signal"])
    b.add("sensor", "poor_link", "Poor-link devices", v("poor_link_count"), unit="devices",
          state_class="measurement", icon="mdi:wifi-alert", attrs=["poor_link"])
    b.add("sensor", "new_devices", "New devices (24 h)", v("new_devices_24h"), unit="devices",
          state_class="measurement", icon="mdi:new-box", attrs=["new_devices"])
    b.add("sensor", "collector_state", "Collector state", v("collector_state"), category="diagnostic",
          icon="mdi:database-sync")
    b.add("binary_sensor", "login_needed", "eero login needed",
          "{{ 'ON' if value_json.collector_state == 'login_required' else 'OFF' }}", device_class="problem",
          category="diagnostic")
    b.add("sensor", "last_sample", "Last snapshot", v("sample_at_iso"), device_class="timestamp", category="diagnostic")
    b.add("sensor", "api_calls", "eero API calls, last hour", v("api_calls_last_hour"), unit="calls",
          state_class="measurement", category="diagnostic", icon="mdi:api")
    b.add("sensor", "api_errors", "eero API errors, last hour", v("api_errors_last_hour"), unit="errors",
          state_class="measurement", category="diagnostic", icon="mdi:api-off")
    b.add("sensor", "database_size", "Collector database", v("database_mb"), unit="MB", device_class="data_size",
          state_class="measurement", category="diagnostic")
    b.add("sensor", "eero_plus", "Eero Plus", v("premium_status"), category="diagnostic", icon="mdi:star-circle",
          attrs=["premium_locked"])
    b.add("sensor", "addon_version", "Add-on version", v("addon_version"), category="diagnostic", icon="mdi:tag",
          attrs=["collector_source"])
    return b.configs


def node_configs(node, version):
    nid = node["id"]
    dev = {"identifiers": [f"{BASE}_node_{nid}"], "name": f"eero {node['location']}", "manufacturer": "eero",
           "model": node.get("model"), "sw_version": node.get("os_version"), "via_device": f"{BASE}_network"}
    b = Builder(dev, f"{BASE}/node/{nid}", f"eero_node_{nid}")
    b.add("binary_sensor", "online", "Online", on_off("online"), device_class="connectivity")
    b.add("sensor", "mesh_bars", "Mesh quality", v("mesh_bars"), unit="bars", state_class="measurement",
          icon="mdi:signal-cellular-3")
    for key, name in (("clients", "Clients"), ("clients_wireless", "Wi-Fi clients"), ("clients_wired", "Wired clients")):
        b.add("sensor", key, name, v(key), unit="clients", state_class="measurement", icon="mdi:devices")
    b.add("sensor", "last_reboot", "Last reboot", v("last_reboot_iso"), device_class="timestamp")
    b.add("sensor", "cloud_since", "Connected to eero cloud since", v("cloud_since_iso"), device_class="timestamp",
          category="diagnostic")
    b.add("sensor", "upstream", "Upstream", v("upstream_text"), icon="mdi:transit-connection-variant")
    b.add("sensor", "firmware", "Firmware", v("os_version"), category="diagnostic", icon="mdi:chip")
    b.add("binary_sensor", "update", "Firmware update available", on_off("update_available"), device_class="update",
          category="diagnostic")
    for r in node.get("radios", []):
        s, label = band_slug(r["band"]), f"{r['band']} GHz"
        b.add("sensor", f"util_{s}", f"{label} channel utilization", v(f"radio_{s}.utilization"), unit="%",
              state_class="measurement", icon="mdi:chart-donut")
        b.add("sensor", f"clients_{s}", f"{label} clients", v(f"radio_{s}.clients"), unit="clients",
              state_class="measurement", icon="mdi:wifi")
        b.add("sensor", f"channel_{s}", f"{label} channel", v(f"radio_{s}.channel"), icon="mdi:numeric",
              attrs=[f"radio_{s}"])
    return b.configs


def client_configs(d, version):
    did = d["id"]
    dev = {"identifiers": [f"{BASE}_client_{did}"], "name": d["name"], "manufacturer": d.get("manufacturer"),
           "model": d.get("device_type"), "via_device": f"{BASE}_network"}
    b = Builder(dev, f"{BASE}/client/{did}", f"eero_client_{did}")
    b.add("binary_sensor", "connected", "Connected", on_off("connected"), device_class="connectivity")
    b.add("sensor", "node", "eero node", v("node"), icon="mdi:router-wireless")
    b.add("sensor", "band", "Wi-Fi band", v("band"), icon="mdi:wifi")
    b.add("sensor", "signal", "Signal", v("signal"), unit="dBm", device_class="signal_strength",
          state_class="measurement")
    b.add("sensor", "link_bars", "Link quality", v("score_bars"), unit="bars", state_class="measurement",
          icon="mdi:signal")
    b.add("sensor", "today", "Data today", v("today_mb"), unit="MB", device_class="data_size",
          state_class="total_increasing")
    b.add("sensor", "last_hour", "Data, last hour", v("last_hour_mb"), unit="MB", device_class="data_size",
          state_class="measurement")
    b.add("sensor", "link_rate", "Wi-Fi link rate", v("rx_rate_mbps"), unit="Mbit/s", device_class="data_rate",
          state_class="measurement", category="diagnostic")
    return b.configs


def parse_tracked(entry):
    """'desktop-1006 = Array Desktop' -> ('desktop-1006', 'Array Desktop'); 'Living Room TV' -> (it, None)."""
    key, _, alias = entry.partition("=")
    return key.strip(), alias.strip() or None


# --- states -----------------------------------------------------------------------------------------------
def collect(db_path, tracked, tz, version, source):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    now = int(time.time())
    try:
        net = report.network(db, now, tz, db_path)
        net.update(sample_at_iso=iso(net["sample_at"]), speedtest_at_iso=iso(net["speedtest_at"]),
                   last_hour_start_iso=iso(net["last_hour_start"]), addon_version=version, collector_source=source)
        nodes = report.nodes(db)
        node_states = {}
        for n in nodes:
            st = {k: n.get(k) for k in ("online", "mesh_bars", "clients", "clients_wired", "clients_wireless",
                                        "os_version", "update_available", "location")}
            st["last_reboot_iso"] = iso(n.get("last_reboot_at"))
            st["cloud_since_iso"] = iso(n["ts"] - n["cloud_uptime_s"]) if n.get("ts") and n.get("cloud_uptime_s") else None
            st["upstream_text"] = "wired" if n.get("connection_type") == "WIRED" else \
                f"{n.get('upstream')} ({n.get('upstream_radio')})" if n.get("upstream") else n.get("connection_type")
            for r in n["radios"]:
                st[f"radio_{band_slug(r['band'])}"] = {k: r[k] for k in ("channel", "width_mhz", "tx_power",
                                                                         "utilization", "clients")}
            node_states[n["id"]] = st
        entries = [parse_tracked(t) for t in tracked]
        ids, missing = report.find_devices(db, [key for key, _ in entries])
        aliases = {}
        for (key, alias), did in zip([e for e in entries if e[0] not in missing], ids):
            aliases[did] = alias
        clients = {}
        for did in ids:
            d = report.device_now(db, did, now, tz)
            if d:
                s = d["sample"] or {}
                clients[did] = {"name": aliases.get(did) or d["name"], "eero_name": d["name"],
                                "manufacturer": d.get("manufacturer"),
                                "device_type": d.get("device_type"), "connected": bool(d.get("connected")),
                                "node": d.get("node"), "band": d.get("band"), "signal": s.get("signal"),
                                "score_bars": s.get("score_bars"), "rx_rate_mbps": s.get("rx_rate_mbps"),
                                "today_mb": d["today_mb"], "last_hour_mb": d["last_hour_mb"]}
    finally:
        db.close()
    return net, nodes, node_states, clients, missing


class Publisher:
    def __init__(self, broker, db_path, tracked, interval, timezone, version, source, log):
        self.broker, self.db_path, self.tracked = broker, db_path, tracked
        self.interval, self.tz, self.version, self.source, self.log = interval, ZoneInfo(timezone), version, source, log
        self.client = None
        self.resend = True
        self.warned_missing = set()

    def on_message(self, topic, payload):
        if topic == f"{DISCOVERY}/status" and payload == b"online":
            self.resend = True  # Home Assistant restarted: send discovery again

    def connect(self):
        self.client = mqtt.Client(self.broker["host"], int(self.broker["port"]), self.broker.get("username"),
                                  self.broker.get("password"), client_id="eero_collector", will_topic=AVAIL,
                                  will_payload="offline", keepalive=120, on_message=self.on_message)
        self.client.connect()
        self.client.subscribe(f"{DISCOVERY}/status")
        self.client.publish(AVAIL, "online", retain=True)
        self.resend = True

    def publish_once(self):
        net, nodes, node_states, clients, missing = collect(self.db_path, self.tracked, self.tz, self.version, self.source)
        for key in set(missing) - self.warned_missing:
            self.log(f"tracked device not found: {key!r} (use its eero name or MAC)")
        self.warned_missing = set(missing)
        if self.resend:
            configs = network_configs(self.version)
            for n in nodes:
                configs.update(node_configs(n, self.version))
            for did, c in clients.items():
                configs.update(client_configs({"id": did, **c}, self.version))
            previous = set(json.loads(KNOWN_TOPICS.read_text())) if KNOWN_TOPICS.exists() else set()
            for topic in previous - set(configs):
                self.client.publish(topic, "", retain=True)  # removes the entity from HA
            for topic, config in configs.items():
                self.client.publish(topic, json.dumps(config), retain=True)
            KNOWN_TOPICS.write_text(json.dumps(sorted(configs)))
            self.resend = False
            time.sleep(1)  # let HA create the entities before the first states arrive
        self.client.publish(f"{BASE}/network", json.dumps(net, default=str), retain=True)
        for nid, st in node_states.items():
            self.client.publish(f"{BASE}/node/{nid}", json.dumps(st), retain=True)
        for did, st in clients.items():
            self.client.publish(f"{BASE}/client/{did}", json.dumps(st), retain=True)

    def run(self, stop):
        failing = None
        while not stop.is_set():
            try:
                if not (self.client and self.client.alive):
                    self.connect()
                    self.log(f"publishing to Home Assistant over MQTT ({self.broker['host']}) every {self.interval} s")
                if pathlib.Path(self.db_path).exists():
                    self.publish_once()
                if failing:
                    self.log("publishing to Home Assistant again")
                failing = False
                for _ in range(self.interval):
                    if stop.wait(1) or self.resend:
                        break
                    self.client.ping_if_idle()
            except Exception as e:  # never let publishing affect collection
                if not failing:
                    self.log(f"MQTT publishing failed: {e}; retrying every 30 s")
                failing = True
                if self.client:
                    self.client.close()
                stop.wait(30)
        if self.client and self.client.alive:
            self.client.publish(AVAIL, "offline", retain=True)
            self.client.disconnect()


def broker_from_supervisor(token):
    """MQTT credentials the Supervisor hands to add-ons that declare `services: mqtt:need`."""
    req = urllib.request.Request("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)["data"]
