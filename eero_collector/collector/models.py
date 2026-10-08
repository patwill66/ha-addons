"""Turns eero API responses into rows. Pure functions: no I/O, no clock unless passed in.

Every field is optional in practice (fields vary with firmware, model and subscription), so all
access goes through .get() and missing values become None, never 0.
"""

import datetime
import re

BANDS = {"2_4GHz": "2.4", "5GHz_full": "5", "5GHz_low": "5L", "5GHz_high": "5H", "6GHz": "6"}
EVENT_CONNECTED = re.compile(
    r"^(?P<name>.+?) connected to (?P<node>.+?) eero\b.*? on (?P<band>[\d.]+) ?GHz channel (?P<channel>\d+)\s*$")
EVENT_DISCONNECTED = re.compile(r"^(?P<name>.+?) disconnected from (?P<node>.+?) eero\b")


def ts(value):
    """ISO 8601 in any of eero's forms ('Z', '+0000', 9-digit fractions) -> UTC epoch seconds."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    v = re.sub(r"(\.\d{6})\d+", r"\1", v)                 # nanoseconds -> microseconds
    v = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", v)       # +0000 -> +00:00
    try:
        dt = datetime.datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp())


def iso_utc(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def id_from_url(url):
    """'/2.2/eeros/12345' -> 12345; '/2.2/networks/1/devices/aabbccddeeff' -> 'aabbccddeeff'."""
    if not url:
        return None
    last = url.rstrip("/").rsplit("/", 1)[-1]
    return int(last) if last.isdigit() else last


def dbm(value):
    """'-56 dBm' -> -56."""
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"\s*(-?\d+)", value or "")
    return int(m.group(1)) if m else None


def band_of(frequency):
    """interface.frequency '2.4' / '5' / '6' (GHz) -> the same short label used for radio bands."""
    if frequency in (None, ""):
        return None
    f = str(frequency)
    return "2.4" if f.startswith("2") else "6" if f.startswith("6") else "5" if f.startswith("5") else f


def width_mhz(value):
    """'WIDTH_20MHz' or 20 -> 20."""
    if isinstance(value, int):
        return value
    m = re.search(r"(\d+)", value or "")
    return int(m.group(1)) if m else None


def mbps(bps):
    return round(bps / 1e6, 1) if isinstance(bps, (int, float)) else None


def premium_active(network):
    """Eero Plus is active when premium_status says so; anything else (trial_ineligible, inactive...) isn't."""
    return (network or {}).get("premium_status") in ("active", "trialing")


# --- network ------------------------------------------------------------------------------------
def network_sample(network, devices, now):
    health = network.get("health") or {}
    connected = [d for d in devices or [] if d.get("connected")]
    return {
        "ts": now,
        "status": network.get("status"),
        "internet_status": (health.get("internet") or {}).get("status"),
        "isp_up": _bool((health.get("internet") or {}).get("isp_up")),
        "mesh_status": (health.get("eero_network") or {}).get("status"),
        "online_eeros": network.get("online_eeros_count"),
        "offline_eeros": network.get("offline_eeros_count"),
        "clients_connected": len(connected) if devices is not None else None,
        "clients_wireless": sum(1 for d in connected if d.get("wireless")) if devices is not None else None,
        "clients_wired": sum(1 for d in connected if not d.get("wireless")) if devices is not None else None,
        "clients_guest": sum(1 for d in connected if d.get("is_guest")) if devices is not None else None,
        "wan_ip": (network.get("ip_settings") or {}).get("public_ip") or network.get("wan_ip"),
        "premium_status": network.get("premium_status"),
    }


def latest_speedtest(network):
    speed = network.get("speed") or {}
    t = ts(speed.get("date"))
    down, up = (speed.get("down") or {}).get("value"), (speed.get("up") or {}).get("value")
    return {"ts": t, "down_mbps": down, "up_mbps": up} if t and (down is not None or up is not None) else None


def speedtests(items):
    out = []
    for item in items or []:
        t = ts(item.get("date"))
        if t:
            out.append({"ts": t, "down_mbps": item.get("down_mbps"), "up_mbps": item.get("up_mbps")})
    return out


# --- nodes --------------------------------------------------------------------------------------
def node(eero, now):
    up = eero.get("wireless_upstream_node") or {}
    uptime = eero.get("uptime") or {}
    node_id = id_from_url(eero.get("url"))
    record = {
        "id": node_id, "serial": eero.get("serial"), "mac": eero.get("mac_address"), "model": eero.get("model"),
        "location": eero.get("location"), "is_gateway": _bool(eero.get("gateway")), "ip": eero.get("ip_address"),
        "os_version": eero.get("os_version"),
    }
    sample = {
        "node_id": node_id, "ts": now, "status": eero.get("status"), "state": eero.get("state"),
        "heartbeat_ok": _bool(eero.get("heartbeat_ok")), "last_heartbeat_at": ts(eero.get("last_heartbeat")),
        "uptime_s": uptime.get("since_last_reboot_s"), "cloud_uptime_s": uptime.get("since_cloud_connection_s"),
        "last_reboot_at": ts(eero.get("last_reboot")), "mesh_bars": eero.get("mesh_quality_bars"),
        "connection_type": eero.get("connection_type"),
        "upstream": up.get("name") if isinstance(up, dict) else None,
        "upstream_radio": up.get("primary_mesh_radio") if isinstance(up, dict) else None,
        "clients": eero.get("connected_clients_count"), "clients_wired": eero.get("connected_wired_clients_count"),
        "clients_wireless": eero.get("connected_wireless_clients_count"),
        "os_version": eero.get("os_version"), "update_available": _bool(eero.get("update_available")),
    }
    radios = []
    for key, stats in (eero.get("radio_channel_stats") or {}).items():
        if not isinstance(stats, dict):
            continue
        name = key[5:] if key.startswith("band_") else key
        radios.append({
            "node_id": node_id, "band": BANDS.get(name, name), "ts": now, "channel": stats.get("channel"),
            "width_mhz": width_mhz(stats.get("channel_width")), "tx_power": stats.get("tx_power"),
            "utilization": stats.get("channel_utilization"), "clients": stats.get("client_count"),
        })
    return record, sample, radios


# --- devices ------------------------------------------------------------------------------------
IDENTITY_FIELDS = ("url", "nickname", "display_name", "hostname", "manufacturer", "device_type", "model_name",
                   "inferred_make", "inferred_model", "connection_type")


def device_identity(d):
    """Identity fields shared by the devices list and the data_usage/devices response."""
    out = {k: d.get(k) for k in IDENTITY_FIELDS}
    out["mac"] = (d.get("mac") or "").lower() or None
    out["is_private"] = _bool(d.get("is_private"))
    out["is_guest"] = _bool(d.get("is_guest"))
    profile = d.get("profile")
    out["profile"] = profile.get("name") if isinstance(profile, dict) else profile
    return out


def device_state(d, node_ids_by_location):
    """Current association and link quality of one device from the devices list."""
    source = d.get("source") or {}
    node_id = id_from_url(source.get("url")) if source.get("url") else None
    if node_id is None or isinstance(node_id, str):
        node_id = node_ids_by_location.get(source.get("location"))
    c = d.get("connectivity") or {}
    stats = c.get("packet_stats") or {}
    score = c.get("score")
    wireless = bool(d.get("wireless"))
    return {
        "connected": _bool(d.get("connected")),
        "node_id": node_id,
        "band": band_of((d.get("interface") or {}).get("frequency")) if wireless else None,
        "channel": d.get("channel") if wireless else None,
        "ip": d.get("ip") or d.get("ipv4"),
        "eero_first_seen": d.get("first_seen") or d.get("first_active"),
        "eero_last_active": d.get("last_active"),
        "sample": {
            "node_id": node_id,
            "band": band_of((d.get("interface") or {}).get("frequency")) if wireless else None,
            "channel": d.get("channel") if wireless else None,
            "signal": dbm(c.get("signal")) if wireless else None,
            "snr": c.get("snr") if wireless else None,
            "score_bars": c.get("score_bars") if wireless else None,
            "score_milli": int(round(score * 1000)) if isinstance(score, (int, float)) and wireless else None,
            "tx_retry_pct": c.get("tx_retry_pct") if wireless else None,
            "rx_rate_mbps": mbps((c.get("rx_rate_info") or {}).get("rate_bps")) if wireless else None,
            "tx_rate_mbps": mbps((c.get("tx_rate_info") or {}).get("rate_bps")) if wireless else None,
            "rx_drop_ppm": stats.get("rx_drop_ppm") if wireless else None,
            "tx_fail_ppm": stats.get("tx_fail_ppm") if wireless else None,
        },
    }


# --- events, usage, notifications ---------------------------------------------------------------
def event(e):
    desc = e.get("description") or ""
    out = {"hash": e.get("hash"), "ts_ms": e.get("timestamp_ms"), "category": e.get("category"),
           "description": desc, "action": None, "device_name": None, "node_location": None, "band": None,
           "channel": None}
    m = EVENT_CONNECTED.match(desc)
    if m:
        out.update(action="connected", device_name=m["name"], node_location=m["node"],
                   band=band_of(m["band"]), channel=int(m["channel"]))
    else:
        m = EVENT_DISCONNECTED.match(desc)
        if m:
            out.update(action="disconnected", device_name=m["name"], node_location=m["node"])
    return out


def usage_series(data):
    """Network data_usage -> {bucket epoch: (down, up)} from its download/upload series."""
    out = {}
    for series in (data or {}).get("series") or []:
        kind = series.get("type")
        if kind not in ("download", "upload"):
            continue
        for v in series.get("values") or []:
            t = ts(v.get("time"))
            if t is None:
                continue
            down, up = out.get(t, (0, 0))
            out[t] = (v.get("value") or 0, up) if kind == "download" else (down, v.get("value") or 0)
    return out


def device_usage(data):
    """data_usage/devices -> [(identity, down, up)] for one window."""
    out = []
    for v in (data or {}).get("values") or []:
        ident = device_identity(v)
        if ident["mac"]:
            out.append((ident, v.get("download") or 0, v.get("upload") or 0))
    return out


def notification(n):
    meta = n.get("meta") or {}
    return {"uuid": n.get("uuid"), "ts": ts(n.get("created_at")), "category": n.get("category"),
            "title": n.get("title"), "body": n.get("body"), "device_url": meta.get("device_url")}


def _bool(v):
    return None if v is None else int(bool(v))
