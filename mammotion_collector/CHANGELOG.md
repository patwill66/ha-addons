# Changelog

## 0.2.2 (2026-10-07)
- New `sensor.luba_addon_version`: the running version, the collector commit and this changelog, shown on the LUBA Mower dashboard's **About** tab.
- This changelog now also appears in Home Assistant's update dialog.

## 0.2.1 (2026-10-07)
- Fixed: the sensors are published now. 0.2.0 found no Home Assistant API token, because the base image clears the environment before starting the add-on.
- First version installed from GitHub (`patwill66/ha-addons`) instead of as a local add-on. Auto update is on.

## 0.2.0 (2026-10-07)
- Mirrors what has been logged into 16 `sensor.luba_*` / `binary_sensor.luba_*` states for the LUBA Mower dashboard (status, battery, charging, Wi-Fi, samples logged, failed polls, events, errors, sessions).
- The database and lock moved from the add-on's own storage to `/share/mammotion_collector/`, so a GitHub install can reach them. The old database was migrated on first start.
- Last version installed by uploading files by hand.

## 0.1.1 (2026-10-07)
- Fixed the first-start import of the Mac collector's database: `/share` was read-only, so the file is now copied before it's opened.
- First version that collected: running on the Green since 11:21 UTC. The history from 09:25 to 11:21 UTC is missing, because the Mac database's `-wal` file wasn't copied (decided not to recover).

## 0.1.0 (2026-10-07)
- First local add-on: runs the collector on the Green instead of the Mac, so it keeps collecting while the Mac sleeps. It crashed on start, before logging in (fixed in 0.1.1).
