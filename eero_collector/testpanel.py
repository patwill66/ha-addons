"""The Mesh Testing panel: MQTT entities to run backhaul placement tests, and what they publish.

Device "eero Mesh Test":
- controls: Scenario (select), Start / End (buttons), Notes (text), the test settings (numbers: target duration,
  stabilization, preliminary threshold, coverage warning), hand-measured throughput (two numbers and a button);
- View (select): which session the metrics and charts show: the current test, a completed one, the historical
  baseline, or all tests on one timeline;
- Status, Active scenario and metric sensors for the viewed session; "Completed tests" carries the comparison rows;
- images: the session chart and the comparison chart (testchart.py), wide and phone layouts.
Watched clients (option mesh_test_clients) get their roams, drops and time off their main eero measured per session.
Commands arrive on eero_collector/mesh_test/<key>/set; publish.py queues them and calls handle() in its loop.
Sessions live in the collector database (eero-network's collector.meshtest decides what is measured), so the
results outlast Home Assistant's recorder. Nothing here talks to eero.
"""

import datetime
import sqlite3

import testchart
from collector import db as store
from collector import meshtest
from collector import report

BASE = "eero_collector"
TOPIC = f"{BASE}/mesh_test"
SET = f"{TOPIC}/+/set"
CURRENT, ALL, BASELINE_VIEW = "Current test", "All tests (timeline)", "Baseline (historical)"
SETTINGS = [("target_hours", "Target duration", "h", 1, 720, 1),
            ("stabilization_min", "Stabilization period", "min", 0, 1440, 5),
            ("preliminary_hours", "Preliminary below", "h", 1, 720, 1),
            ("coverage_warn_pct", "Coverage warning below", "%", 0, 100, 1)]
THROUGHPUT = [("throughput_down", "Throughput down"), ("throughput_up", "Throughput up")]
UNAVAILABLE = ["Backhaul signal (RSSI/SNR): eero's API doesn't report it",
               "Backhaul PHY rate: not reported",
               "Backhaul throughput: not measured automatically; enter speed tests by hand"]
CHART_LAYOUTS = (("wide", "chart", "Test chart"), ("tall", "chart_phone", "Test chart (phone)"))
COMPARE_LAYOUTS = (("wide", "comparison_chart", "Comparison chart"),
                   ("tall", "comparison_chart_phone", "Comparison chart (phone)"))


def iso(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat() if epoch else None


def parse_scenarios(entries):
    """['A: Original placement, cabinet closed', ...] -> [('A', 'Original placement, cabinet closed'), ...]."""
    out = []
    for e in entries:
        code, sep, name = e.partition(":")
        code, name = code.strip(), name.strip()
        if sep and code and name and " " not in code:
            out.append((code, name))
    return out


def scenario_label(code, name):
    return f"{code}: {name}"


def session_label(s, tz):
    day = datetime.datetime.fromtimestamp(s["started_at"], tz).strftime("%b %-d")
    return f"{s['code']} · {s['name']} ({day})"[:120]


# --- discovery ---------------------------------------------------------------------------------------------------
def device(version):
    return {"identifiers": [f"{BASE}_mesh_test"], "name": "eero Mesh Test", "manufacturer": "eero",
            "model": "Backhaul placement tests (eero Collector)", "sw_version": version, "via_device": f"{BASE}_network"}


def configs(builder_cls, version, scenarios, view_options, others):
    """Discovery configs; builder_cls is publish.Builder (shared conventions, availability, naming)."""
    b = builder_cls(device(version), TOPIC, "eero_mesh_test")
    cmd = lambda key: f"{TOPIC}/{key}/set"  # noqa: E731
    b.add("select", "scenario", "Scenario", "{{ value_json.draft_scenario }}", icon="mdi:flask-outline",
          command_topic=cmd("scenario"), options=[scenario_label(c, n) for c, n in scenarios])
    b.add("button", "start", "Start test", None, icon="mdi:play", command_topic=cmd("start"), payload_press="PRESS")
    b.add("button", "end", "End test", None, icon="mdi:stop", command_topic=cmd("end"), payload_press="PRESS")
    b.add("text", "notes", "Notes", "{{ value_json.notes or '' }}", icon="mdi:note-text-outline",
          command_topic=cmd("notes"), max=255, mode="text")
    for key, name, unit, lo, hi, step in SETTINGS:
        b.add("number", key, name, "{{ value_json.settings.%s }}" % key, unit=unit, icon="mdi:tune",
              command_topic=cmd(key), min=lo, max=hi, step=step, mode="box", category="config")
    for key, name in THROUGHPUT:
        b.add("number", key, name, "{{ value_json.draft_throughput.%s }}" % key.split("_")[1], unit="Mbit/s",
              device_class="data_rate", icon="mdi:speedometer", command_topic=cmd(key), min=0, max=10000, step=0.1,
              mode="box")
    b.add("button", "throughput_save", "Save throughput result", None, icon="mdi:content-save",
          command_topic=cmd("throughput_save"), payload_press="PRESS")
    b.add("select", "view", "View", "{{ value_json.view }}", icon="mdi:eye-outline", command_topic=cmd("view"),
          options=view_options)
    b.add("sensor", "status", "Status", "{{ value_json.status }}", icon="mdi:flask",
          attrs=["test", "message", "unavailable"])
    b.add("sensor", "scenario_active", "Active scenario", "{{ value_json.scenario_active }}", icon="mdi:flask-outline")
    b.add("sensor", "started", "Started", "{{ value_json.test.start }}", device_class="timestamp")
    b.add("sensor", "elapsed", "Elapsed", "{{ value_json.test.elapsed_hours }}", unit="h", icon="mdi:timer-outline")
    sel = lambda f: "{{ value_json.selected.%s }}" % f  # noqa: E731
    b.add("sensor", "selected", "Viewed session", sel("label"), icon="mdi:clipboard-text-outline",
          attrs_topic=f"{TOPIC}/selected")
    pct = dict(unit="%", state_class="measurement", icon="mdi:percent")
    b.add("sensor", "direct_pct", "Direct to gateway", sel("direct_pct"), **{**pct, "icon": "mdi:arrow-up-bold"})
    for o in others:
        key = "via_" + "".join(ch if ch.isalnum() else "_" for ch in o.lower())
        b.add("sensor", key, f"Via {o}", "{{ value_json.selected.via.get(%r) }}" % o, **{**pct, "icon": "mdi:call-split"})
    b.add("sensor", "changes_per_24h", "Upstream changes per 24 h", sel("changes_per_24h"), unit="per 24 h",
          state_class="measurement", icon="mdi:swap-horizontal")
    b.add("sensor", "rtt_median", "Gateway RTT median", sel("rtt_median_ms"), unit="ms", device_class="duration",
          state_class="measurement")
    b.add("sensor", "rtt_p95", "Gateway RTT p95", sel("rtt_p95_ms"), unit="ms", device_class="duration",
          state_class="measurement")
    b.add("sensor", "loss", "Gateway packet loss", sel("loss_pct"), unit="%", state_class="measurement",
          icon="mdi:package-variant-remove")
    b.add("sensor", "outages", "Ping outages", sel("outages_total"), unit="outages", state_class="measurement",
          icon="mdi:lan-disconnect")
    b.add("sensor", "coverage", "Data coverage", sel("coverage_pct"), **{**pct, "icon": "mdi:database-check"})
    b.add("sensor", "valid_samples", "Valid samples", sel("valid_samples"), unit="samples", state_class="measurement",
          icon="mdi:counter")
    b.add("sensor", "valid_hours", "Valid hours", sel("valid_hours"), unit="h", state_class="measurement",
          icon="mdi:clock-check-outline")
    for band in ("6", "5"):
        b.add("sensor", f"band_{band}", f"{band} GHz backhaul share", "{{ value_json.selected.band_pct.get('%s', 0) "
              "if value_json.selected.measured else None }}" % band, **{**pct, "icon": "mdi:wifi"})
    b.add("sensor", "last_change", "Last upstream change", sel("last_upstream_change"), device_class="timestamp")
    b.add("sensor", "completed", "Completed tests", "{{ value_json.completed_count }}", unit="tests",
          icon="mdi:clipboard-list-outline", attrs_topic=f"{TOPIC}/comparison")
    for layout, key, name in CHART_LAYOUTS + COMPARE_LAYOUTS:
        b.add("image", key, name, None, icon="mdi:chart-timeline-variant", image_topic=f"{TOPIC}/{key}",
              content_type="image/svg+xml")
    return b.configs


def watched(db, entries):
    """Option mesh_test_clients ('eero name, hostname or MAC [= label]') -> ([(device id, label)], [unknown keys])."""
    pairs = [(key.strip(), alias.strip() or None) for key, _, alias in (e.partition("=") for e in entries or [])]
    ids, missing = report.find_devices(db, [key for key, _ in pairs if key])
    found = [(key, alias) for key, alias in pairs if key and key not in missing]
    out = []
    for (key, alias), did in zip(found, ids):
        row = db.execute("SELECT * FROM devices WHERE id = ?", (did,)).fetchone()
        out.append((did, alias or (report.device_name(dict(row)) if row else key)))
    return out, missing


def add_clients(db_path, entries, now):
    """Adds watched clients to sessions that ended before they were watched (see meshtest.add_clients)."""
    if not entries:
        return []
    db = store.connect(db_path)
    try:
        return meshtest.add_clients(db, watched(db, entries)[0], now)
    finally:
        db.close()


# --- commands ----------------------------------------------------------------------------------------------------
def handle(db_path, key, payload, scenarios, node, tz, now, clients=()):
    """Applies one command and returns a short message for the Status sensor."""
    text = payload.decode(errors="replace").strip() if isinstance(payload, bytes) else str(payload).strip()
    db = store.connect(db_path)
    try:
        if key == "scenario":
            code = next((c for c, n in scenarios if scenario_label(c, n) == text), None)
            if code:
                meshtest.set_draft(db, scenario=code)
            return None
        if key == "start":
            code = meshtest.draft(db).get("scenario") or (scenarios[0][0] if scenarios else "X")
            name = dict(scenarios).get(code, code)
            s = meshtest.start(db, code, name, node, now)
            meshtest.set_draft(db, view=CURRENT)
            return f"Started {s['code']} ({name})."
        if key == "end":
            s = meshtest.end(db, now, tz, watched(db, clients)[0])
            hours = (s["ended_at"] - s["started_at"]) / 3600
            return f"Ended {s['code']} after {hours:.1f} h."
        if key == "notes":
            meshtest.set_notes(db, text[:255], now)
            return None
        if key in {k for k, *_ in SETTINGS}:
            meshtest.set_setting(db, key, float(text))
            return None
        if key in dict(THROUGHPUT):
            meshtest.set_draft(db, **{key: float(text)})
            return None
        if key == "throughput_save":
            d = meshtest.draft(db)
            down, up = d.get("throughput_down"), d.get("throughput_up")
            if not down and not up:
                return "Enter throughput down and/or up first."
            code = meshtest.add_throughput(db, down or None, up or None, now)
            return f"Saved throughput {down or '—'} / {up or '—'} Mbit/s to {code}."
        if key == "view":
            meshtest.set_draft(db, view=text)
            return None
        return None
    except meshtest.MeshTestError as e:
        return f"{key.capitalize()} ignored: {e}."
    except ValueError:
        return f"{key} ignored: not a number."
    finally:
        db.close()


# --- state -------------------------------------------------------------------------------------------------------
def _flat(m, kind, label, code, name, measured_by):
    """The viewed session's metrics as flat fields for sensors and markdown (None when not measured)."""
    lat = m.get("latency") or {}
    measuring = bool(m.get("measuring"))
    route = m.get("route_pct") or {}
    return {
        "label": label, "code": code, "name": name, "kind": kind, "measured_by": measured_by, "measured": measuring,
        "start": iso(m.get("start")), "end": iso(m.get("end")), "measure_from": iso(m.get("measure_from")),
        "measure_to": iso(m.get("measure_to")), "node": m.get("node"), "gateway": m.get("gateway"),
        "direct_pct": m.get("direct_pct") if measuring else None,
        "via": {k: v for k, v in route.items() if k != m.get("gateway")} if measuring else {},
        "route_pct": route, "band_pct": m.get("band_pct") or {}, "channels": m.get("channels") or {},
        "changes_per_24h": m.get("changes_per_24h"), "upstream_changes": m.get("upstream_changes"),
        "band_only_changes": m.get("band_only_changes"),
        "rtt_median_ms": lat.get("rtt_median_ms"), "rtt_p95_ms": lat.get("rtt_p95_ms"), "rtt_n": lat.get("rtt_n"),
        "rtt_by_upstream": lat.get("rtt_by_upstream") or {}, "latency_source": lat.get("source"),
        "loss_pct": lat.get("loss_pct"), "loss_events": lat.get("loss_events"),
        "outages": lat.get("outages"), "outage_minutes": lat.get("outage_minutes"),
        "internet_outages": lat.get("internet_outages"), "internet_outage_minutes": lat.get("internet_outage_minutes"),
        "outages_total": None if not lat else (lat.get("outages") or 0) + (lat.get("internet_outages") or 0),
        "coverage_pct": m.get("coverage_pct"), "valid_samples": m.get("valid_samples"),
        "valid_hours": m.get("valid_hours"), "elapsed_hours": m.get("elapsed_hours"),
        "last_upstream_change": iso(m.get("last_upstream_change")),
        "flags": m.get("flags") or [], "warnings": m.get("warnings") or [],
        "events": [{"at": iso(e["ts"]), "text": e["text"]} for e in (m.get("events") or [])][-15:],
        "others": m.get("others") or {}, "settings": m.get("settings") or {},
        "clients": m.get("clients") or [],
    }


def _row(s, m):
    lat = m.get("latency") or {}
    tp = s.get("throughput") or []
    downs = sorted(t["down_mbps"] for t in tp if t.get("down_mbps"))
    ups = sorted(t["up_mbps"] for t in tp if t.get("up_mbps"))
    return {"code": s["code"], "name": s["name"], "scenario": s.get("scenario"), "kind": s["kind"],
            "start": iso(s["started_at"]), "end": iso(s["ended_at"]),
            "hours": round((s["ended_at"] - s["started_at"]) / 3600, 1) if s.get("ended_at") else None,
            "valid_hours": m.get("valid_hours"), "valid_samples": m.get("valid_samples"),
            "coverage_pct": m.get("coverage_pct"), "direct_pct": m.get("direct_pct"), "route_pct": m.get("route_pct") or {},
            "changes_per_24h": m.get("changes_per_24h"), "band_pct": m.get("band_pct") or {},
            "channels": m.get("channels") or {}, "rtt_median_ms": lat.get("rtt_median_ms"), "rtt_p95_ms": lat.get("rtt_p95_ms"),
            "loss_pct": lat.get("loss_pct"), "outages": lat.get("outages"),
            "internet_outages": lat.get("internet_outages"),
            "throughput_down_median": downs[len(downs) // 2] if downs else None,
            "throughput_up_median": ups[len(ups) // 2] if ups else None, "throughput_n": len(tp),
            "flags": m.get("flags") or [], "warnings": m.get("warnings") or [], "notes": s.get("notes"),
            "settings": s.get("settings") or {}, "clients": {str(c["device_id"]): c for c in m.get("clients") or []}}


def state(db_path, node, scenarios, tz, now, message=None, clients=()):
    """Everything publish.py sends: (main payload, viewed-session attributes, comparison attributes, view options,
    other satellites, charts {key: svg})."""
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        try:
            cfg, d, act = meshtest.settings(db), meshtest.draft(db), meshtest.active(db)
            trials, base = meshtest.sessions(db, "trial"), meshtest.session(db, meshtest.BASELINE)
        except sqlite3.OperationalError:  # database not migrated yet
            return None
        gateway = meshtest.gateway_name(db)
        sats = meshtest.satellites(db)
        watch, unknown = watched(db, clients)
        others = [s for s in sats if s != node]
        completed = [t for t in trials if t["ended_at"]]
        labels = {session_label(t, tz): t for t in completed}
        options = [CURRENT, *labels, *([BASELINE_VIEW] if base else []), ALL]
        view = d.get("view") if d.get("view") in options else CURRENT

        live = meshtest.metrics(db, act["node"], act["started_at"], None, act["settings"], now, tz, watch) if act else None
        if view in (CURRENT, ALL):
            sel = act or (completed[-1] if completed else None)
        elif view == BASELINE_VIEW:
            sel = base
        else:
            sel = labels[view]
        if sel is None:
            selected = _flat({}, None, "No test yet", None, None, None)
        elif act and sel["id"] == act["id"]:
            selected = _flat(live, "trial", session_label(act, tz), act["code"], act["name"], "measuring now")
        else:
            by = "historical baseline (not a controlled trial)" if sel["kind"] == "baseline" else "frozen when the test ended"
            selected = _flat(sel["summary"] or {}, sel["kind"], BASELINE_VIEW if sel["kind"] == "baseline"
                             else session_label(sel, tz), sel["code"], sel["name"], by)
        if selected.get("last_upstream_change") is None:  # useful before any test, too
            selected["last_upstream_change"] = iso(meshtest.last_upstream_change(db, node, now))
        if sel is not None:
            selected["notes"] = sel.get("notes")
            selected["throughput"] = [{"at": iso(t["ts"]), "down": t["down_mbps"], "up": t["up_mbps"]}
                                      for t in sel.get("throughput") or []]

        test = {}
        if act:
            stab_end = act["started_at"] + act["settings"]["stabilization_min"] * 60
            elapsed = (now - act["started_at"]) / 3600
            test = {"code": act["code"], "name": act["name"], "scenario": act["scenario"], "start": iso(act["started_at"]),
                    "measure_from": iso(stab_end), "elapsed_hours": round(elapsed, 2),
                    "target_hours": act["settings"]["target_hours"],
                    "progress_pct": round(min(100, 100 * elapsed / act["settings"]["target_hours"]), 1),
                    "phase": "stabilizing" if now < stab_end else "measuring", "settings": act["settings"],
                    "preliminary": "preliminary" in (live.get("flags") or []) or now < stab_end}
        status = ("Stabilizing" if test.get("phase") == "stabilizing" else "Measuring") if act else "No test running"
        draft_code = d.get("scenario") or (scenarios[0][0] if scenarios else None)
        payload = {
            "status": status, "running": bool(act), "test": test or {"start": None, "elapsed_hours": None},
            "scenario_active": f"{act['code']} · {act['name']}" if act else "No test",
            "draft_scenario": scenario_label(draft_code, dict(scenarios).get(draft_code, draft_code)) if draft_code else None,
            "notes": act["notes"] if act else d.get("notes"),
            "settings": cfg, "draft_throughput": {"down": d.get("throughput_down") or 0, "up": d.get("throughput_up") or 0},
            "view": view, "message": message, "unavailable": UNAVAILABLE, "selected": selected,
            "completed_count": len(completed),
        }
        rows = [_row(t, t["summary"] or {}) for t in completed]
        baseline_row = _row(base, base["summary"] or {}) if base else None
        if base and not baseline_row["channels"]:  # baselines frozen before 0.6.0 have no channels: work them out
            bs = base["summary"] or {}
            baseline_row["channels"] = meshtest.channels(db, bs.get("measure_from") or base["started_at"],
                                                         bs.get("measure_to") or base["ended_at"] or now)
            if sel is not None and sel["id"] == base["id"]:
                selected["channels"] = baseline_row["channels"]
        comparison = {"trials": rows, "baseline": baseline_row, "clients": [{"id": str(did), "name": name} for did, name in watch],
                      "clients_unknown": unknown,
                      "baseline_note": "Normal operation before testing; uncontrolled, not a comparable trial."}

        # charts end at the latest eero data, not at "now", so a picture only changes when new data arrives
        latest = db.execute("SELECT max(ts) FROM node_samples").fetchone()[0] or now
        live_end = min(now, latest + meshtest.POLL)
        sessions = [{"code": t["code"], "scenario": t["scenario"], "name": t["name"], "start": t["started_at"],
                     "end": t["ended_at"], "measure_from": t["started_at"] + t["settings"]["stabilization_min"] * 60}
                    for t in trials]
        if view == ALL or sel is None:
            lo = min([t["started_at"] for t in trials] + [now - 3 * 86400]) - 3600
            hi, title, measure_from = live_end, "All tests" if trials else "Last 3 days (no test yet)", lo
            daily = meshtest.routes(db, node, lo, hi, gateway, tz)["daily"]
            flags = []
        else:
            m = live if act and sel["id"] == act["id"] else (sel["summary"] or {})
            lo, hi = sel["started_at"], sel["ended_at"] or live_end
            measure_from = m.get("measure_from") or lo
            title = BASELINE_VIEW if sel["kind"] == "baseline" else f"{sel['code']} · {sel['name']}"
            daily = m.get("daily") or []
            flags = [f.replace("_", " ").upper() for f in (m.get("flags") or [])]
            if sel["kind"] == "baseline":
                sessions = [{"code": "Baseline", "scenario": None, "name": sel["name"], "start": lo, "end": hi,
                             "measure_from": lo}]
        when = lambda t: datetime.datetime.fromtimestamp(t, tz).strftime("%a %b %-d %H:%M")  # noqa: E731
        sub = f"{node} · {when(lo)} → {when(hi)}"
        if selected.get("valid_hours") is not None and view != ALL and sel is not None:
            sub += f" · {selected['valid_hours']:g} valid h, coverage {selected['coverage_pct']:g} %"
        series = meshtest.series(db, node, lo, hi)
        data = {"title": title, "subtitle": sub, "flags": flags, "lo": lo, "hi": hi, "measure_from": measure_from,
                "gateway": gateway, "nodes": [gateway, *sats], "sessions": sessions, "timeline": series["timeline"],
                "rtt": series["rtt"], "daily": daily}
        charts = {key: testchart.session(data, tz, layout) for layout, key, _ in CHART_LAYOUTS}
        charts.update({key: testchart.comparison(rows, baseline_row, gateway or "gateway", layout)
                       for layout, key, _ in COMPARE_LAYOUTS})
    finally:
        db.close()
    return payload, selected, comparison, options, others, charts
