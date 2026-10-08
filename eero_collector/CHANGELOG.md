# Changelog

## 0.1.0 (2026-10-08)

- First version. Read-only eero collector: network, node and client snapshots every 5 minutes, eero's
  connection events every 2 minutes, hourly per-device and network usage, daily speed tests and
  notifications, and a paced backfill of eero's history (about 100 days).
- Home Assistant sensors over MQTT discovery: an "eero Network" device, one device per eero node,
  and one per tracked client.
- Web UI: status and eero login, a sortable device inventory, per-device history pages, database snapshots.
- Eero Plus sources (channel utilization history, security insights) are detected and stay locked
  without a subscription.
