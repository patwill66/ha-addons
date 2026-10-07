"""Normalize official API payloads into flat records. Status strings are preserved exactly."""

import json

DROP_FROM_RAW = ("wifiIp",)  # no analytics value; keep the LAN address out of history


def _int(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value):
    return None if value is None else str(value)


def raw_detail_json(data: dict) -> str:
    """The API's `data` object (compact JSON) minus fields we deliberately don't keep."""
    raw = {k: v for k, v in data.items() if k not in DROP_FROM_RAW}
    if isinstance(raw.get("network"), dict):
        raw["network"] = {k: v for k, v in raw["network"].items() if k not in DROP_FROM_RAW}
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=False, sort_keys=True)


def normalize_detail(data: dict, observed_at: str) -> dict:
    """Map GET /v1/mower/{id} `data` to a telemetry sample. Unknown statuses pass through untouched."""
    net = data.get("network") if isinstance(data.get("network"), dict) else {}
    status = data.get("status")
    return {
        "observed_at": observed_at,
        "online": _int(data.get("online")),
        "raw_status": status if isinstance(status, str) else _text(status),
        "battery_level": _int(data.get("batteryLevel")),
        "charge_status": _int(data.get("chargeStatus")),
        "used_network": _text(net.get("usedNetwork")),
        "wifi_available": _int(net.get("wifiAvailable")),
        "wifi_rssi": _int(net.get("wifiRssi")),
        "cellular_available": _int(net.get("cellularAvailable")),
        "cellular_rssi": _int(net.get("cellularRssi")),
        "firmware_version": _text(data.get("version")),
    }


def device_meta(data: dict) -> dict:
    return {
        "name": _text(data.get("name")),
        "nickname": _text(data.get("nickname")),
        "model": _text(data.get("model")),
        "firmware_version": _text(data.get("version")),
    }


WORK_PARAM_COLUMNS = {
    "knifeHeight": "knife_height", "speed": "speed", "channelWidth": "channel_width",
    "channelMode": "channel_mode", "jobContent": "job_content", "edgeMode": "edge_mode",
    "toward": "toward", "towardMode": "toward_mode", "towardIncludedAngle": "toward_included_angle",
    "ultraWave": "ultra_wave", "boundaryZigzagOrder": "boundary_zigzag_order",
    "forbiddenAreaCircleTimes": "forbidden_area_circle_times", "dumpPeriodSqm": "dump_period_sqm",
    "rideBoundaryDistance": "ride_boundary_distance",
}


def normalize_work_params(data: dict) -> dict:
    row = {col: data.get(key) for key, col in WORK_PARAM_COLUMNS.items()}
    row["raw_json"] = json.dumps(data, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return row
