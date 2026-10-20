# Changelog

## 0.5.1 (2026-10-20)

- Fixes the Ping import failing at start ("WebSocket closed by Home Assistant"): the WebSocket client could drop
  Home Assistant's first message when it arrived together with the handshake, then wait until the Supervisor gave
  up. The statistics import used the same client and could hit this too.
- The Ping import fetches first and writes afterwards, so a slow database write can't stall the connection;
  5-minute aggregates are computed in one pass.

## 0.5.0 (2026-10-20)

- **Upstream, split in two:** each satellite eero adds "Upstream eero" (the eero it connects through) and "Primary
  backhaul radio" (2.4 / 5 / 6 GHz, the band of that link), plus "Direct to gateway (24 h)" (share of the last day
  spent directly on the gateway). Band flips on the same eero are not upstream switches; the switch counters are
  unchanged. The combined "Upstream" sensor stays.
- **Mesh Test** (new device): run backhaul placement tests from Home Assistant. Pick a scenario (add-on option
  `mesh_test_scenarios`), press Start / End, add notes and hand-measured throughput. Target duration, stabilization
  period, preliminary threshold and coverage warning are adjustable numbers; each test keeps the values it ran with.
  Sessions and their final results are stored in the collector database, so they outlast HA's recorder.
- Metrics for the viewed session (current, any completed test, the historical baseline, or all tests): direct /
  via-eero shares of valid time, upstream changes per 24 h, band shares, gateway RTT median and p95, packet loss and
  outages, coverage, valid samples, time since the last upstream change; warnings for restarts, firmware or channel
  changes, collector gaps and outages. Two chart images: the session on one time axis, and the completed tests compared.
- Latency comes from HA's Ping integration: the add-on copies its readings into the database every 5 minutes
  (`import_ping`, on by default; raw readings kept `sample_retention_days`, 5-minute aggregates for good).
- Optional historical baseline: set `baseline_start` / `baseline_end` (ISO 8601) to store a window of normal operation
  for context. It is labelled as such and never mixed with controlled tests.

## 0.4.0 (2026-10-11)

- **Mesh map:** two new images on eero Network, "Mesh map" (a tree, for wide screens) and "Mesh map (phone)" (a
  stacked list). They show the internet, the gateway and each eero under the eero it connects through (wired or
  wireless, band, mesh bars, upstream switches in 24 h), with every band's channel, width, clients and channel
  utilization; busy (50 %+) and congested (75 %+) channels are marked. Light or dark follows the viewer's system
  setting. A picture is sent only when it changes.

## 0.3.0 (2026-10-11)

- **Device picker:** "eero Network → Device" lists every device seen in the last 30 days (shared names get the
  manufacturer and the end of the MAC). "Selected device" carries the chosen device's details as attributes:
  connection, signal, link rate, retries, data used, disconnects and roams, a 24-hour link summary, daily usage for
  14 days and its latest changes. The picker only changes what the add-on shows; nothing is sent to eero.
- Tracked clients add: data in the last 7 days, Wi-Fi retries, disconnects and roams in the last 24 hours.
- Each eero adds "Upstream switches" (24 h and 7 days): how often it changed the eero it connects through.
- eero Network adds clients per band (all eeros), "Last network event" (eero's own feed: channel changes and
  more; the latest 15 in its attributes) and the last 10 speed tests (attributes of "Last speed test").

## 0.2.1 (2026-10-11)

- Fixes the statistics import: Home Assistant names the unit class for data sizes `information`; 0.2.0's
  import was refused and wrote nothing.

## 0.2.0 (2026-10-11)

- Imports the collector's history into Home Assistant's long-term statistics: `eero_collector:network_download`
  and `network_upload` (hourly, from when eero's records begin) and `speedtest_download` and `speedtest_upload`
  (eero's speed tests). Statistics cards can show months of history; the import repeats every hour.
  Needs access to Home Assistant's API (`homeassistant_api`). Option `import_statistics` turns it off.

## 0.1.1 (2026-10-08)

- `tracked_devices` entries can carry a friendly name: `desktop-1006 = Array Desktop` names the HA device.
- Hostnames match with or without eero's trailing dot, and the dot is dropped from displayed names.

## 0.1.0 (2026-10-08)

- First version. Read-only eero collector: network, node and client snapshots every 5 minutes, eero's
  connection events every 2 minutes, hourly per-device and network usage, daily speed tests and
  notifications, and a paced backfill of eero's history (about 100 days).
- Home Assistant sensors over MQTT discovery: an "eero Network" device, one device per eero node,
  and one per tracked client.
- Web UI: status and eero login, a sortable device inventory, per-device history pages, database snapshots.
- Eero Plus sources (channel utilization history, security insights) are detected and stay locked
  without a subscription.
