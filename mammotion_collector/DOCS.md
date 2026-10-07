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
- The sensors are set through the Home Assistant API. They have no unique ID, and after a Home
  Assistant restart they come back on the next poll.

The collector itself is maintained in a separate repository; `SOURCE` records the commit it was built from.
