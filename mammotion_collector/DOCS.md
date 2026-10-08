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
| `publish_sensors` | Mirror what was logged into `sensor.luba_*` / `binary_sensor.luba_*` states |

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
