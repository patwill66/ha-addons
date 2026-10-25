# Mammotion Collector

Collects the history of one Mammotion LUBA mower through the **official Mammotion Developer API**:
REST polling of the mower's status, battery, charging and network every 30 s, plus its error history,
saved tasks and work parameters. It never sends control commands. The data goes into SQLite at
`/share/mammotion_collector/mammotion.db`, together with derived state events and mowing and charging
sessions.

## Configuration

| Option | Meaning |
|---|---|
| `client_id`, `client_secret` | Credentials from the Mammotion Developer portal |
| `poll_seconds` | Poll interval (default 30) |
| `verbose` | Log every poll |
| `publish_sensors` | Mirror what was logged into `sensor.luba_*` / `binary_sensor.luba_*` states, plus the analysis and long-term statistics below |
| `timezone` | Time zone for local days and for label times given without a UTC offset (default `America/Chicago`) |
| `eero_database` | The eero Collector's database, read-only (default `/share/eero_collector/eero.db`) |
| `eero_device` | The mower's name in eero. With it, a job's zone and a stuck incident's location come from the eero node and band the mower used. |
| `zone_fingerprints` | eero node and band → zone, for example `{node: Living Room, band: "2.4", zone: Front Yard}`. Leave out a node and band that several zones share. |
| `zone_labels` | Zones you know: the job's start time and zone, for example `{start: "2026-10-24T10:54", zone: Front Yard}`. A label wins over eero. |

Zone settings live only in these options (written to `/data/zones.json` at start), never in the add-on's code.

Mammotion allows only **two live tokens per client**, and a third login revokes the oldest. Run only one
collector with these credentials. A lock file next to the database stops two installs of this add-on
from collecting at once.

## Data

- On first start, if there's no database yet, it restores one from `/data/mammotion.db` (versions
  before 0.2.0) or from `/share/mammotion/mammotion.db` (a copy of an existing collector database).
- `/share` is part of Home Assistant's full backups. The database grows by about 1 MB a day.
- `sensor.luba_condition` and `binary_sensor.luba_needs_attention` come from the collector's
  `derive.current_condition()`. It's documented in mammotion-luba's `docs/DATA_INTERPRETATION.md`, so
  Home Assistant shows the same interpretation as the analysis. `stuck_powered_off` means the mower went
  offline while it was stuck off the dock. It has powered itself off, and someone must free it and press
  its power button.
- When the collector's derivation rules change, it rebuilds state events and sessions from the stored
  samples on its next start. The log then says "Derivation rules changed".
- The sensors are set through the Home Assistant API. They have no unique ID, and after a Home
  Assistant restart they come back on the next poll.

## Analysis

The collector's `report` module turns the history into jobs (sessions joined by `visualHashs`, with
zone, mowing and stuck time, battery drain, incidents and outcome), stuck incidents (what followed and,
with eero, roughly where), daily rollups in `timezone`, and battery rates. It runs at most every 5
minutes and publishes:

| Entity | State |
|---|---|
| `sensor.luba_current_job`, `sensor.luba_last_job` | The current job's zone (or `none`); the last finished job's outcome. Attributes: the job's details |
| `sensor.luba_completion_rate_30d` | % of jobs finished in 30 days that completed (attributes: 7- and 30-day figures) |
| `sensor.luba_stuck_incidents_7d`, `sensor.luba_last_stuck` | Stuck incidents in 7 days; when the last one started (attributes: its details) |
| `sensor.luba_mowing_hours_7d`, `sensor.luba_off_in_yard_hours_30d` | Hours mowing; hours powered off in the yard after getting stuck |
| `sensor.luba_faults_7d` | Error records that are faults (not routine, schedule or weather notices); attributes by category |
| `sensor.luba_drain_rate`, `sensor.luba_charge_rate`, `sensor.luba_mowing_minutes_per_charge` | 30-day median battery rates |
| `sensor.luba_zones`, `sensor.luba_job_history`, `sensor.luba_incident_history`, `sensor.luba_daily` | Per-zone figures, the last 15 jobs and incidents, the last 14 complete days (attributes) |
| `sensor.luba_analysis` | When the analysis last ran; eero samples used, zones configured, or the error |

**Long-term statistics:** once an hour, the daily history is imported as external statistics
`mammotion_collector:<key>`, one row per local day since collection began: `mowing_hours`,
`mowing_hours_<zone>`, `stuck_hours`, `off_in_yard_hours`, `stuck_incidents`, `jobs_started`,
`jobs_completed`, `mowing_battery_used`, `offline_blips`, `cellular_hours` (running sums; use the
"change" statistic), and `drain_rate`, `charge_rate` (daily means). Show them with Statistics graph cards.

## Snapshots

**Open Web UI** shows a link that downloads a consistent copy of the database (`mammotion-<UTC time>.db`).
It's made with SQLite's online backup API from a read-only connection, so it includes rows that are still
in the `-wal` file, and the collector keeps running. The copy is made in the container's temporary storage
and deleted after it's sent. The response's `X-Snapshot-Sha256` header carries its SHA-256.

The server listens on the ingress port (8099) and only answers Home Assistant's ingress proxy, so every
download needs a logged-in Home Assistant user (or a token that can open an ingress session). It has no
credentials of its own and never writes to the database. Outside the UI, open an ingress session through
the Supervisor API (`POST /ingress/session`) and request `<ingress_url>snapshot` with the
`ingress_session` cookie.

The collector itself is maintained in a separate repository; `SOURCE` records the commit it was built from.
