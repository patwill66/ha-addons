"""Publishes the collector's database to Home Assistant through MQTT discovery.

Every `interval` seconds it reads the database read-only, asks eero-network's collector.report for the
current summaries, and publishes one retained JSON state per HA device:
  eero_collector/network            -> device "eero Network"
  eero_collector/node/<node id>     -> one device per eero node
  eero_collector/client/<device id> -> one device per tracked client (option tracked_devices), including its
                                       recent connection events (report.device_timeline) as attributes
  eero_collector/picker             -> the device picker (a select on "eero Network") and the chosen
                                       device's details (sensor "Selected device", attributes)
  eero_collector/mesh_map/<layout>  -> the mesh drawn as an SVG (meshmap.py; images "Mesh map" and
                                       "Mesh map (phone)" on "eero Network"), sent only when it changes
  eero_collector/event_filter       -> switches on "eero Network" choosing which kinds of device events the
                                       dashboard lists (kept in /data/event_filter.json; display only)
  eero_collector/mesh_test[/...]    -> device "eero Mesh Test": backhaul placement tests (testpanel.py);
                                       its controls send commands on eero_collector/mesh_test/<key>/set
Discovery configs (homeassistant/<component>/eero_collector/<object>/config, retained) are sent at
start and again whenever Home Assistant announces itself on homeassistant/status. Configs of entities
that disappear (a client no longer tracked) are cleared. Availability follows the MQTT last will.
The picker only chooses what this add-on shows; the choice is kept in /data/picker.json. Mesh test
commands are the only writes: they record test sessions in the collector database. Nothing here talks to eero.
"""

import datetime
import json
import pathlib
import sqlite3
import time
import urllib.request
from zoneinfo import ZoneInfo

import meshmap
import mqtt
import testpanel
from collector import report

BASE = "eero_collector"
DISCOVERY = "homeassistant"
AVAIL = f"{BASE}/status"
APP = pathlib.Path(__file__).resolve().parent
KNOWN_TOPICS = pathlib.Path("/data/discovery_topics.json")
PICKER = pathlib.Path("/data/picker.json")
PICKER_SET = f"{BASE}/picker/set"
EVENT_FILTER = pathlib.Path("/data/event_filter.json")
FILTER_TOPIC = f"{BASE}/event_filter"
EVENT_KINDS = (("connect", "connects"), ("disconnect", "disconnects"), ("roam", "roams"), ("band", "band changes"),
               ("channel", "channel changes"), ("ip", "IP changes"))
DEVICE_EVENTS = 100  # recent connection events per tracked device (the dashboard filters them by kind)
MAP_LAYOUTS = (("wide", "mesh_map", "Mesh map"), ("tall", "mesh_map_phone", "Mesh map (phone)"))


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
            category=None, attrs=None, enabled=True, payload_on=None, state_topic=None, attrs_topic=None, **extra):
        object_id = f"{self.prefix}_{key}"
        c = {"name": name, "unique_id": object_id, "object_id": object_id, "state_topic": state_topic or self.state_topic,
             "value_template": value, "availability_topic": AVAIL, "device": self.device, **extra}
        for k, v in (("unit_of_measurement", unit), ("device_class", device_class), ("state_class", state_class),
                     ("icon", icon), ("entity_category", category)):
            if v:
                c[k] = v
        if component in ("binary_sensor", "switch"):
            c["payload_on"], c["payload_off"] = payload_on or "ON", "OFF"
        if attrs_topic:
            c["json_attributes_topic"] = attrs_topic
        elif attrs:
            c["json_attributes_topic"] = self.state_topic
            c["json_attributes_template"] = "{{ {" + ", ".join(f'"{a}": value_json.{a}' for a in attrs) + "} | tojson }}"
        if not enabled:
            c["enabled_by_default"] = False
        if component in ("image", "button"):  # no state of their own (an image has image_topic)
            del c["state_topic"], c["value_template"]
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
    b.add("sensor", "speedtest_at", "Last speed test", v("speedtest_at_iso"), device_class="timestamp",
          attrs=["speedtests"])
    for s, label in (("2_4", "2.4 GHz"), ("5", "5 GHz"), ("6", "6 GHz")):
        b.add("sensor", f"clients_{s}", f"{label} clients", v(f"clients_{s}"), unit="clients",
              state_class="measurement", icon="mdi:wifi")
    b.add("sensor", "network_event", "Last network event", "{{ value_json.network_event_text }}",
          icon="mdi:format-list-bulleted", attrs=["network_events"])
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
    for kind, label in EVENT_KINDS:
        b.add("switch", f"event_filter_{kind}", f"Show {label}", v(kind), state_topic=FILTER_TOPIC,
              command_topic=f"{FILTER_TOPIC}/{kind}/set", icon="mdi:filter-outline", category="config")
    b.add("sensor", "addon_version", "Add-on version", v("addon_version"), category="diagnostic", icon="mdi:tag",
          attrs=["collector_source"])
    for layout, key, name in MAP_LAYOUTS:
        b.add("image", key, name, None, icon="mdi:sitemap", image_topic=f"{BASE}/mesh_map/{layout}",
              content_type="image/svg+xml")
    return b.configs


def picker_configs(version, options):
    """The device picker: a select listing every recent device, and the chosen device's details."""
    b = Builder(network_device(version), f"{BASE}/picker", "eero_network")
    b.add("select", "device_picker", "Device", "{{ value_json.label }}", icon="mdi:devices",
          command_topic=PICKER_SET, options=options)
    b.add("sensor", "selected_device", "Selected device", "{{ value_json.label }}", icon="mdi:devices",
          attrs_topic=f"{BASE}/picker/detail")
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
    if not node.get("is_gateway"):
        b.add("sensor", "upstream_eero", "Upstream eero", v("upstream_eero"), icon="mdi:router-wireless")
        b.add("sensor", "backhaul_radio", "Primary backhaul radio", v("backhaul_radio_text"), icon="mdi:wifi")
        b.add("sensor", "direct_24h", "Direct to gateway (24 h)", v("direct_to_gateway_24h"), unit="%",
              state_class="measurement", icon="mdi:arrow-up-bold")
    for w, label in (("24h", "24 h"), ("7d", "7 days")):
        b.add("sensor", f"upstream_changes_{w}", f"Upstream switches ({label})", v(f"upstream_changes_{w}"),
              unit="switches", state_class="measurement", icon="mdi:swap-horizontal")
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
          state_class="measurement")
    b.add("sensor", "week", "Data, last 7 days", v("week_mb"), unit="MB", device_class="data_size",
          state_class="measurement")
    b.add("sensor", "retries", "Wi-Fi retries", v("tx_retry_pct"), unit="%", state_class="measurement",
          icon="mdi:repeat")
    for k, label, icon in (("drops", "Disconnects", "mdi:lan-disconnect"), ("roams", "Roams", "mdi:swap-horizontal")):
        b.add("sensor", f"{k}_24h", f"{label} (24 h)", v(f"{k}_24h"), unit=k, state_class="measurement", icon=icon)
    b.add("sensor", "last_event", "Last connection event", v("last_event"), icon="mdi:history", attrs=["events"])
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
        net["speedtests"] = [{"at": iso(s["ts"]), "down": s["down_mbps"], "up": s["up_mbps"]} for s in net["speedtests"]]
        net["network_events"] = [{"at": iso(e["ts"]), "text": e["text"]} for e in net["network_events"]]
        net["network_event_text"] = (net["network_events"][0]["text"][:250] if net["network_events"] else None)
        nodes = report.nodes(db, now)
        node_states = {}
        for n in nodes:
            st = {k: n.get(k) for k in ("online", "mesh_bars", "clients", "clients_wired", "clients_wireless",
                                        "os_version", "update_available", "location", "upstream_changes_24h",
                                        "upstream_changes_7d", "upstream_eero", "direct_to_gateway_24h")}
            st["backhaul_radio_text"] = f"{n['backhaul_radio']} GHz" if n.get("backhaul_radio") else None
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
                                "tx_retry_pct": s.get("tx_retry_pct"), "today_mb": d["today_mb"],
                                "last_hour_mb": d["last_hour_mb"], "week_mb": d["week_mb"],
                                **{k: d[k] for k in ("drops_24h", "roams_24h")}}
                timeline = report.device_timeline(db, did, now, limit=DEVICE_EVENTS)
                clients[did]["events"] = [{"at": iso(e["ts"]), "kind": e["kind"], "text": e["text"],
                                           "source": e["source"]} for e in timeline]
                clients[did]["last_event"] = timeline[0]["text"][:250] if timeline else None
        choices = report.device_choices(db, now)
        topo = report.topology(nodes)
        as_of = max((n["ts"] for n in nodes if n.get("ts")), default=None)
        maps = {layout: meshmap.render(topo, net.get("isp_up"), as_of, tz, layout) for layout, _, _ in MAP_LAYOUTS}
    finally:
        db.close()
    return net, nodes, node_states, clients, missing, choices, maps


def picker_detail(db_path, device_id, label, tz):
    """The chosen device's details as attributes for the "Selected device" sensor."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        d = report.device_detail(db, device_id, int(time.time()), tz)
    finally:
        db.close()
    if not d:
        return {"label": label}
    s = d["sample"] or {}
    return {"label": label, "name": d["name"], "connected": bool(d.get("connected")), "node": d.get("node"),
            "band": d.get("band"), "channel": d.get("channel"), "connection_type": d.get("connection_type"),
            "manufacturer": d.get("manufacturer"), "device_type": d.get("device_type"), "ip": d.get("ip"),
            "mac": d.get("mac"), "private_mac": bool(d.get("is_private")),
            "signal": s.get("signal"), "snr": s.get("snr"), "score_bars": s.get("score_bars"),
            "rx_rate_mbps": s.get("rx_rate_mbps"), "tx_rate_mbps": s.get("tx_rate_mbps"), "tx_retry_pct": s.get("tx_retry_pct"),
            "today_mb": d["today_mb"], "last_hour_mb": d["last_hour_mb"], "week_mb": d["week_mb"],
            **{k: d[k] for k in ("drops_24h", "roams_24h", "drops_7d", "roams_7d")},
            "first_seen": d.get("eero_first_seen"), "last_connected": iso(d.get("last_connected_at")),
            "link_24h": d["link_24h"], "daily": d["daily"],
            "changes": [{"at": iso(c["ts"]), **{k: c[k] for k in ("kind", "old", "new")}} for c in d["changes"]]}


class Publisher:
    def __init__(self, broker, db_path, tracked, interval, timezone, version, source, log, test_node=None,
                 scenarios=()):
        self.broker, self.db_path, self.tracked = broker, db_path, tracked
        self.test_node, self.scenarios = test_node, testpanel.parse_scenarios(scenarios)
        self.commands = []  # mesh test commands from MQTT, applied in the publishing loop
        self.test_message = None
        self.test_shape = None  # view options and other satellites last announced in discovery
        self.test_images = {}
        try:
            self.event_filter = json.loads(EVENT_FILTER.read_text())
        except (OSError, ValueError):
            self.event_filter = {}
        self.event_filter = {k: self.event_filter.get(k, "ON") for k, _ in EVENT_KINDS}
        self.filter_changed = False
        self.interval, self.tz, self.version, self.source, self.log = interval, ZoneInfo(timezone), version, source, log
        self.client = None
        self.resend = True
        self.warned_missing = set()
        self.options = None
        self.picked = None  # label requested over MQTT, handled in the publishing loop
        self.maps = {}  # mesh map SVGs last sent, so an unchanged picture isn't sent again
        try:
            self.picker = json.loads(PICKER.read_text())
        except (OSError, ValueError):
            self.picker = {}

    def on_message(self, topic, payload):
        if topic == f"{DISCOVERY}/status" and payload == b"online":
            self.resend = True  # Home Assistant restarted: send discovery again
        elif topic == PICKER_SET:
            self.picked = payload.decode(errors="replace")
        elif topic.startswith(FILTER_TOPIC + "/") and topic.endswith("/set"):
            kind, value = topic[len(FILTER_TOPIC) + 1:-4], payload.decode(errors="replace").strip().upper()
            if kind in self.event_filter and value in ("ON", "OFF"):
                self.event_filter[kind] = value
                self.filter_changed = True
        elif topic.startswith(testpanel.TOPIC + "/") and topic.endswith("/set"):
            self.commands.append((topic[len(testpanel.TOPIC) + 1:-4], payload))

    def connect(self):
        self.client = mqtt.Client(self.broker["host"], int(self.broker["port"]), self.broker.get("username"),
                                  self.broker.get("password"), client_id="eero_collector", will_topic=AVAIL,
                                  will_payload="offline", keepalive=120, on_message=self.on_message)
        self.client.connect()
        self.client.subscribe(f"{DISCOVERY}/status")
        self.client.subscribe(PICKER_SET)
        self.client.subscribe(testpanel.SET)
        self.client.subscribe(f"{FILTER_TOPIC}/+/set")
        self.client.publish(AVAIL, "online", retain=True)
        self.resend = True

    def apply_commands(self):
        while self.commands:
            key, payload = self.commands.pop(0)
            try:
                msg = testpanel.handle(self.db_path, key, payload, self.scenarios, self.test_node, self.tz,
                                       int(time.time()))
            except Exception as e:  # a bad command must not stop publishing
                msg = f"{key} failed: {e}"
            if msg:
                self.test_message = f"{time.strftime('%H:%M')} {msg}"
                self.log(f"mesh test: {msg}")

    def publish_filter(self):
        if self.filter_changed:
            EVENT_FILTER.write_text(json.dumps(self.event_filter))
            self.filter_changed = False
        self.client.publish(FILTER_TOPIC, json.dumps(self.event_filter), retain=True)

    def publish_once(self):
        self.apply_commands()
        net, nodes, node_states, clients, missing, choices, maps = collect(self.db_path, self.tracked, self.tz,
                                                                           self.version, self.source)
        test = testpanel.state(self.db_path, self.test_node, self.scenarios, self.tz, int(time.time()),
                               self.test_message) if self.test_node else None
        if test and (test[3], test[4]) != self.test_shape:
            self.resend = True
        options = [label for _, label in choices]
        ids = {label: did for did, label in choices}
        if self.picked is not None:
            if self.picked in ids:
                self.picker = {"id": ids[self.picked], "label": self.picked}
                PICKER.write_text(json.dumps(self.picker))
            self.picked = None
        # Labels can change (a device renamed, a name becoming shared): follow the device id.
        if self.picker.get("id") in ids.values() and self.picker.get("label") not in ids:
            self.picker["label"] = next(label for label, did in ids.items() if did == self.picker["id"])
        if self.picker.get("label") not in ids and options:
            first = next((did for did in clients if did in ids.values()), ids[options[0]])
            self.picker = {"id": first, "label": next(label for label, did in ids.items() if did == first)}
        if options != self.options:
            self.resend = True
        for key in set(missing) - self.warned_missing:
            self.log(f"tracked device not found: {key!r} (use its eero name or MAC)")
        self.warned_missing = set(missing)
        if self.resend:
            configs = network_configs(self.version)
            for n in nodes:
                configs.update(node_configs(n, self.version))
            for did, c in clients.items():
                configs.update(client_configs({"id": did, **c}, self.version))
            configs.update(picker_configs(self.version, options))
            if test:
                configs.update(testpanel.configs(Builder, self.version, self.scenarios, test[3], test[4]))
                self.test_shape = (test[3], test[4])
            self.options = options
            previous = set(json.loads(KNOWN_TOPICS.read_text())) if KNOWN_TOPICS.exists() else set()
            for topic in previous - set(configs):
                self.client.publish(topic, "", retain=True)  # removes the entity from HA
            for topic, config in configs.items():
                self.client.publish(topic, json.dumps(config), retain=True)
            KNOWN_TOPICS.write_text(json.dumps(sorted(configs)))
            self.resend = False
            self.maps = {}
            self.test_images = {}
            time.sleep(1)  # let HA create the entities before the first states arrive
        self.client.publish(f"{BASE}/network", json.dumps(net, default=str), retain=True)
        self.publish_filter()
        for nid, st in node_states.items():
            self.client.publish(f"{BASE}/node/{nid}", json.dumps(st), retain=True)
        for did, st in clients.items():
            self.client.publish(f"{BASE}/client/{did}", json.dumps(st), retain=True)
        for layout, svg in maps.items():
            if self.maps.get(layout) != svg:
                self.client.publish(f"{BASE}/mesh_map/{layout}", svg, retain=True)
                self.maps[layout] = svg
        if test:
            payload, selected, comparison, _, _, charts = test
            self.client.publish(testpanel.TOPIC, json.dumps(payload, default=str), retain=True)
            self.client.publish(f"{testpanel.TOPIC}/selected", json.dumps(selected, default=str), retain=True)
            self.client.publish(f"{testpanel.TOPIC}/comparison", json.dumps(comparison, default=str), retain=True)
            for key, svg in charts.items():
                if self.test_images.get(key) != svg:
                    self.client.publish(f"{testpanel.TOPIC}/{key}", svg, retain=True)
                    self.test_images[key] = svg
        if self.picker.get("id"):
            self.client.publish(f"{BASE}/picker", json.dumps({"label": self.picker["label"]}), retain=True)
            self.client.publish(f"{BASE}/picker/detail", json.dumps(
                picker_detail(self.db_path, self.picker["id"], self.picker["label"], self.tz), default=str), retain=True)

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
                    if self.filter_changed:  # a switch flipped: answer at once, without a full refresh
                        self.publish_filter()
                    if stop.wait(1) or self.resend or self.picked is not None or self.commands:
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
