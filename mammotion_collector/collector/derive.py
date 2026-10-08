"""Derived history: state-change events plus mowing and charging sessions.

Principle: raw samples are stored first; everything here is derived from them and can be
rebuilt from telemetry at any time (see `rebuild_derived`). The same logic is used for live
ingestion and for rebuilds, so both produce identical events and sessions.

"No telemetry" samples: the API sometimes returns a payload without status/battery/charge/
network — while offline (online=0) and occasionally even while online=1 (seen 2026-10-07 for
~70 s while docked). Such samples are stored but carry no state information: they never start
or end sessions, and events compare against the last *known* value, so
TaskPaused → (missing) → TaskPaused produces no event. A long run of them shows up as a gap
between real samples and marks open sessions uncertain.
"""

import json

from .config import seconds_between

# Bump whenever any rule in this module changes. The collector compares it with the value stored
# in the database at startup and rebuilds all events and sessions if they differ.
DERIVATION_VERSION = 2

# Raw status -> behavioral class. Extend freely; unknown statuses classify as "other" and are
# still stored verbatim in telemetry_samples.raw_status. Only exact strings seen live (or
# documented with clear meaning) are mapped.
STATUS_CLASSES = {
    "Mowing": "mowing",          # live value (spec says "Working"; not yet seen live)
    "TaskPaused": "paused",      # live value, undocumented
    "Paused": "paused",          # documented
    "Returning": "returning",    # live + documented
}
JOB_CLASSES = {"mowing", "paused", "returning"}  # states that keep a mowing session open

# chargeStatus values that mean "charging". The spec documents only 1 (0 = not charging), but
# live data shows 2 while docked mid-job (status TaskPaused) with the battery rising 38→46 % in
# 5 min (2026-10-07). 5 (also undocumented) appeared exactly when the battery reached 100 %
# and held there: docked, fully charged, *not* charging, so it is deliberately excluded.
# Raw values are stored unchanged; extend this set if others appear.
CHARGING_VALUES = {1, 2}

# Columns whose changes become state_events rows.
TRACKED_FIELDS = (
    ("status_changed", "raw_status"),
    ("charge_status_changed", "charge_status"),
    ("online_changed", "online"),
    ("active_network_changed", "used_network"),
    ("firmware_changed", "firmware_version"),
)


def classify(sample) -> str:
    if sample is None:
        return None
    if sample["online"] == 0:
        return "offline"
    return STATUS_CLASSES.get(sample["raw_status"], "other")


def is_charging(sample) -> bool:
    return sample["charge_status"] in CHARGING_VALUES


# chargeStatus values seen only while on the dock (1/2 charging, 5 charge complete). 0 means not
# on charge — every TaskPaused + 0 observed so far was the mower stuck off the dock (user-
# confirmed 2026-10-08). See docs/DATA_INTERPRETATION.md.
DOCKED_VALUES = {1, 2, 5}

# Operational states (derived per sample from status + chargeStatus + online):
#   mowing, returning, docked_charging, docked_waiting (job pending, e.g. do-not-operate window),
#   docked_charged, paused_off_dock (needs attention), offline, no_data, other
ATTENTION_STATES = {"paused_off_dock"}


def operational_state(sample):
    """What the mower is doing, combining status, chargeStatus and online. None for no sample."""
    if sample is None:
        return None
    if sample["online"] == 0:
        return "offline"
    if not has_telemetry(sample):
        return "no_data"
    cls = classify(sample)
    if cls == "mowing":
        return "mowing"
    if cls == "returning":
        return "returning"
    if sample["charge_status"] in DOCKED_VALUES:
        if is_charging(sample):
            return "docked_charging"
        return "docked_waiting" if cls == "paused" else "docked_charged"
    if cls == "paused" and sample["charge_status"] == 0:
        return "paused_off_dock"
    return "other"


# Display names for dashboards. "stuck_powered_off" only comes from current_condition().
STATE_LABELS = {
    "mowing": "Mowing",
    "returning": "Returning to dock",
    "docked_charging": "Docked, charging",
    "docked_waiting": "Docked, job pending",
    "docked_charged": "Docked, charged",
    "paused_off_dock": "Stuck (paused off the dock)",
    "stuck_powered_off": "Powered off in the yard after getting stuck",
    "offline": "Offline",
    "no_data": "No data",
    "other": "Unknown state",
}
CONDITION_ATTENTION = {"paused_off_dock", "stuck_powered_off"}


def current_condition(latest, last_real) -> dict:
    """What a dashboard should show right now, from the latest sample and the last sample that
    had device state. An offline mower whose last known state was paused_off_dock has powered
    itself off while stuck (user-confirmed 2026-10-08): someone must press its power button."""
    state = operational_state(latest)
    last_known = operational_state(last_real)
    if state == "offline" and last_known == "paused_off_dock":
        state = "stuck_powered_off"
    elif state == "no_data" and last_known is not None:
        state = last_known  # incomplete response: keep showing the last known state
    return {
        "state": state,
        "label": STATE_LABELS.get(state, state),
        "attention": state in CONDITION_ATTENTION,
        "last_known_state": last_known,
        "last_known_at": last_real["observed_at"] if last_real else None,
        "last_known_battery": last_real["battery_level"] if last_real else None,
    }


def has_telemetry(sample) -> bool:
    """False for payloads that omit the device state (offline or incomplete responses)."""
    return sample is not None and sample["raw_status"] is not None


def detect_changes(known: dict, sample) -> list[tuple[str, object, object]]:
    """(event_type, old, new) for each tracked field whose value differs from the last *known*
    (non-null) value. Missing values never create events; a field's first value doesn't either."""
    changes = []
    for event_type, col in TRACKED_FIELDS:
        old, new = known.get(col), sample[col]
        if new is not None and old is not None and old != new:
            changes.append((event_type, old, new))
    return changes


def _reasons(existing, *new) -> str:
    items = json.loads(existing) if existing else []
    items.extend(r for r in new if r and r not in items)
    return json.dumps(items)


class SessionDeriver:
    MOWING, CHARGING = "mowing_sessions", "charging_sessions"

    def __init__(self, store, device_id: int, gap_threshold_seconds: int = 180, offline_close_seconds: int = 600):
        self.store = store
        self.device_id = device_id
        self.gap_threshold = gap_threshold_seconds
        self.offline_close = offline_close_seconds

    def apply(self, sample, prev) -> list[str]:
        """Feed one stored sample and the last earlier sample that *had* telemetry.
        Samples without telemetry don't change sessions, except that a mowing session is closed
        once the mower has been offline for longer than offline_close_seconds."""
        if not has_telemetry(sample):
            return self._close_if_lost(sample, prev)
        gap = seconds_between(prev["observed_at"], sample["observed_at"]) if prev else None
        gap_note = (f"{gap}s without observations before {sample['observed_at']}"
                    if gap is not None and gap > self.gap_threshold else None)
        notes = self._mowing(sample, prev, gap_note)
        notes += self._charging(sample, prev, gap_note)
        return notes

    # --- mowing -------------------------------------------------------------------------------
    def _mowing(self, s, prev, gap_note) -> list[str]:
        notes = []
        cls, prev_cls = classify(s), classify(prev)
        open_ = self.store.open_session(self.MOWING, self.device_id)
        if open_:
            elapsed = seconds_between(open_["started_at"], s["observed_at"])
            if cls in JOB_CLASSES and not is_charging(s):
                fields = {
                    "last_sample_id": s["id"], "last_class": cls, "end_battery": s["battery_level"],
                    "lowest_battery": _min(open_["lowest_battery"], s["battery_level"]), "elapsed_seconds": elapsed,
                    "battery_used": _diff(open_["start_battery"], s["battery_level"]),
                    "pause_count": open_["pause_count"] + (1 if cls == "paused" and open_["last_class"] != "paused" else 0),
                }
                if gap_note:
                    fields.update(uncertain=1, uncertain_reasons=_reasons(open_["uncertain_reasons"], gap_note))
                self.store.update_session(self.MOWING, open_["id"], fields)
            else:
                reason = "charging" if is_charging(s) else "offline" if cls == "offline" else "status"
                extra = [gap_note, "ended by going offline" if cls == "offline" else None]
                fields = {
                    "ended_at": s["observed_at"], "end_sample_id": s["id"], "last_sample_id": s["id"],
                    "end_battery": s["battery_level"], "battery_used": _diff(open_["start_battery"], s["battery_level"]),
                    "lowest_battery": _min(open_["lowest_battery"], s["battery_level"]),
                    "elapsed_seconds": elapsed, "end_status": s["raw_status"], "end_reason": reason,
                }
                if any(extra):
                    fields.update(uncertain=1, uncertain_reasons=_reasons(open_["uncertain_reasons"], *extra))
                self.store.update_session(self.MOWING, open_["id"], fields)
                notes.append(f"mowing session #{open_['id']} ended ({reason}: {s['raw_status']}), "
                             f"{elapsed // 60} min, battery {open_['start_battery']}→{s['battery_level']}%")
                open_ = None
        if open_ is None and cls == "mowing" and not is_charging(s):
            reasons = []
            if prev is None:
                reasons.append("no earlier observation; job may have started before collection")
            elif gap_note:
                reasons.append(gap_note)
            elif prev_cls in JOB_CLASSES:
                reasons.append(f"job already in progress when first seen (previous status {prev['raw_status']})")
            sid = self.store.insert_session(self.MOWING, {
                "device_id": self.device_id, "started_at": s["observed_at"], "start_sample_id": s["id"],
                "last_sample_id": s["id"], "last_class": cls, "start_battery": s["battery_level"],
                "end_battery": s["battery_level"], "lowest_battery": s["battery_level"], "battery_used": 0,
                "elapsed_seconds": 0, "uncertain": 1 if reasons else 0,
                "uncertain_reasons": json.dumps(reasons) if reasons else None})
            notes.append(f"mowing session #{sid} started at {s['battery_level']}%"
                         + (" (start uncertain)" if reasons else ""))
        return notes

    def _close_if_lost(self, s, last_real) -> list[str]:
        """Offline long enough mid-job: end the mowing session at the last real observation.
        If the mower was stuck off the dock, it has powered itself off (user-confirmed)."""
        if s["online"] != 0 or last_real is None:
            return []
        open_ = self.store.open_session(self.MOWING, self.device_id)
        if not open_ or seconds_between(last_real["observed_at"], s["observed_at"]) <= self.offline_close:
            return []
        stuck = operational_state(last_real) == "paused_off_dock"
        reason = "stuck_powered_off" if stuck else "offline"
        fields = {
            "ended_at": last_real["observed_at"], "end_sample_id": last_real["id"], "last_sample_id": last_real["id"],
            "end_battery": last_real["battery_level"], "battery_used": _diff(open_["start_battery"], last_real["battery_level"]),
            "lowest_battery": _min(open_["lowest_battery"], last_real["battery_level"]),
            "elapsed_seconds": seconds_between(open_["started_at"], last_real["observed_at"]),
            "end_status": last_real["raw_status"], "end_reason": reason,
        }
        if not stuck:
            fields.update(uncertain=1, uncertain_reasons=_reasons(
                open_["uncertain_reasons"], "went offline mid-job; end time is the last observation"))
        self.store.update_session(self.MOWING, open_["id"], fields)
        return [f"mowing session #{open_['id']} ended ({reason}) at {last_real['observed_at']}, "
                f"battery {open_['start_battery']}→{last_real['battery_level']}%"]

    # --- charging -----------------------------------------------------------------------------
    def _charging(self, s, prev, gap_note) -> list[str]:
        notes = []
        if s["charge_status"] is None:
            return notes
        open_ = self.store.open_session(self.CHARGING, self.device_id)
        if open_:
            elapsed = seconds_between(open_["started_at"], s["observed_at"])
            fields = {"last_sample_id": s["id"], "end_battery": s["battery_level"], "elapsed_seconds": elapsed,
                      "highest_battery": _max(open_["highest_battery"], s["battery_level"]),
                      "battery_gained": _diff(s["battery_level"], open_["start_battery"])}
            if gap_note:
                fields.update(uncertain=1, uncertain_reasons=_reasons(open_["uncertain_reasons"], gap_note))
            if not is_charging(s):
                fields.update(ended_at=s["observed_at"], end_sample_id=s["id"])
                notes.append(f"charging session #{open_['id']} ended, {elapsed // 60} min, "
                             f"battery {open_['start_battery']}→{s['battery_level']}%")
                self.store.update_session(self.CHARGING, open_["id"], fields)
                return notes
            self.store.update_session(self.CHARGING, open_["id"], fields)
            return notes
        if is_charging(s):
            reasons = []
            if prev is None:
                reasons.append("no earlier observation; charging may have started before collection")
            elif gap_note:
                reasons.append(gap_note)
            sid = self.store.insert_session(self.CHARGING, {
                "device_id": self.device_id, "started_at": s["observed_at"], "start_sample_id": s["id"],
                "last_sample_id": s["id"], "start_battery": s["battery_level"], "end_battery": s["battery_level"],
                "highest_battery": s["battery_level"], "battery_gained": 0, "elapsed_seconds": 0,
                "uncertain": 1 if reasons else 0, "uncertain_reasons": json.dumps(reasons) if reasons else None})
            notes.append(f"charging session #{sid} started at {s['battery_level']}%"
                         + (" (start uncertain)" if reasons else ""))
        return notes


def _diff(a, b):
    return None if a is None or b is None else a - b


def _min(a, b):
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def _max(a, b):
    vals = [x for x in (a, b) if x is not None]
    return max(vals) if vals else None


def ensure_derived_current(store, gap_threshold_seconds: int = 180) -> bool:
    """Rebuild events and sessions for every device if they were derived by older rules."""
    if store.get_meta("derivation_version") == str(DERIVATION_VERSION):
        return False
    for (device_id,) in store.conn.execute("SELECT id FROM devices").fetchall():
        rebuild_derived(store, device_id, gap_threshold_seconds)
    store.set_meta("derivation_version", str(DERIVATION_VERSION))
    return True


def rebuild_derived(store, device_id: int, gap_threshold_seconds: int = 180) -> None:
    """Recompute all state events and sessions for a device from stored telemetry, e.g. after
    changing STATUS_CLASSES / CHARGING_VALUES or this module's rules."""
    store.clear_derived(device_id)
    deriver = SessionDeriver(store, device_id, gap_threshold_seconds)
    prev, prev_real, known = None, None, {}
    with store.conn:
        for sample in store.iter_samples(device_id).fetchall():
            gap = seconds_between(prev["observed_at"], sample["observed_at"]) if prev else None
            for event_type, old, new in detect_changes(known, sample):
                store.insert_event(device_id, sample, prev, event_type, old, new, gap)
            deriver.apply(sample, prev_real)
            for _, col in TRACKED_FIELDS:
                if sample[col] is not None:
                    known[col] = sample[col]
            prev = sample
            if has_telemetry(sample):
                prev_real = sample


rebuild_sessions = rebuild_derived  # backwards-compatible name
