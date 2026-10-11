# Changelog

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
