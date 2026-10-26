# eero Collector

Keeps the history of an **eero mesh network** in SQLite and publishes it to Home Assistant. It uses
eero's cloud API **read-only**: the client refuses anything but GET requests (and the login calls), so
it can't pause, reboot or reconfigure the network.

## What it collects

| Every | What |
|---|---|
| 5 min | Network health, every eero node (online, mesh quality, upstream, uptime, per-band channel, utilization and clients) and every client (node, band, channel, signal, link quality, retries) |
| 2 min | eero's connection event feed (connects, disconnects, roams with exact times) |
| hour | Network and per-device data usage for the last complete hour (eero's own counters) |
| day | Daily usage, eero's speed tests, eero notifications (including "new device"), Eero Plus check |

On first start it also backfills eero's stored history (about 100 days of usage) slowly, one request
every 15 seconds. Live per-device bandwidth isn't available without the eero app's live view, so
"last hour" is the freshest accurate rate.

**Eero Plus:** without a subscription, Plus-only sources (channel-utilization history, security
insights) show as *locked* on the status page and the `Eero Plus` sensor. If the network gets Plus,
the collector starts fetching them on its next daily run, with no reconfiguration.

**Mesh map:** the images *Mesh map* and *Mesh map (phone)* on the eero Network device draw the mesh as it
is now: internet, the gateway, each eero under the eero it connects through (wired or wireless, band and
mesh bars), and per band its channel, clients and channel utilization (amber from 50 %, red from 75 %).
Show them with a Picture entity card; they refresh when new eero data arrives.

**Upstream:** each satellite eero has *Upstream eero* (the eero it connects through), *Primary backhaul radio* (the
band of that link) and *Direct to gateway (24 h)*. eero often flips the band (5 ↔ 6 GHz) while staying on the same
eero; those flips are not upstream switches. *Upstream switches* counts changes of eero only.

**Troubleshooting:** the Web UI's *What happened?* page takes a device and a moment (a dropped call, a stalled
page) and lines up, for a window around it: the device's polls (eero, band, channel, signal, rates), its events,
its usual signal per eero, each eero's status, upstream and channel use, mesh changes and what other devices did,
with findings on top (a roam to a weaker eero, a link-rate drop, mesh changes or none, whether other devices
changed at the same moment). Device pages show roams to a weaker eero, time spent on one, and the signal per eero,
band and channel over 30 days, which shows what a channel change did to that device. Each tracked device gets
*Roams to a weaker eero (24 h)*, *Time on a weaker eero (24 h)* and *Best eero* (with the per-channel table as
attributes). *Last mesh change* on eero Network lists the last 7 days of restarts, firmware and channel changes,
upstream eero changes, collector gaps and internet outages.

**Mesh Test:** a device for controlled backhaul tests, such as moving an eero or opening a cabinet. Pick a
*Scenario*, add *Notes*, press *Start test*, and *End test* when done. The first *Stabilization period* minutes
aren't measured. Results count only valid time: 5-minute samples with a reported upstream; missed polls don't count
as a connection. Each test records the 5 and 6 GHz channels it ran on and warns when they differ
from the baseline's. A test with fewer valid hours than *Preliminary below* is flagged preliminary, and coverage below
*Coverage warning below* is flagged too. Changing a setting applies to the next test; each test keeps its own.
*View* chooses what the metric sensors and the *Test chart* image show. *Comparison chart* and *Completed tests*
(attributes) compare finished tests; the optional historical baseline is shown apart. Latency is Home Assistant's
Ping to the gateway, so it describes the path from wherever Home Assistant is connected. eero's API reports no
backhaul signal strength, PHY rate or throughput; throughput is only what you enter (*Throughput down / up*, then
*Save throughput result*, which attaches it to the running test or the last completed one).

## Setup

1. The **Mosquitto broker** add-on and the MQTT integration must be running. MQTT credentials come from
   the Supervisor automatically.
2. Start the add-on and open its **Web UI** (sidebar: *eero Network*).
3. Enter the email or phone of an eero account that's an admin of the network. Amazon-login accounts
   aren't supported by eero's API, so use a dedicated eero account. eero sends a code; enter it.
   The session is stored in the add-on's private `/data`. If eero ever expires it, the
   `eero login needed` sensor turns on and the Web UI asks for a new code.

## Options

| Option | Meaning |
|---|---|
| `poll_seconds` | Snapshot interval (default 300; minimum 120) |
| `publish_seconds` | How often sensors are refreshed in HA (default 60) |
| `tracked_devices` | Clients that get their own HA device (and a page on the Network dashboard) (connected, node, band, signal, link quality, data today/last hour). Each entry is an eero name, hostname or MAC, optionally with a friendly name: `desktop-1006 = Array Desktop`. The Web UI's Devices page shows the names. |
| `backfill`, `backfill_device_hourly` | Fetch eero's stored history (network and per-device) on first run |
| `sample_retention_days` | Keep 5-minute client samples this long; older ones are kept as hourly summaries |
| `timezone` | Time zone for "today" (eero's day boundaries) |
| `import_statistics` | Import network usage and eero speed-test history into HA's long-term statistics (`eero_collector:*`), then keep it current every hour (default on) |
| `import_ping` | Copy Home Assistant's Ping readings (`sensor.ping_eero_gateway_*`, `sensor.ping_eero_<eero>_*`, `sensor.ping_internet_google_*` / `_cloudflare_*` and their binary sensors) into the database every 5 minutes, for mesh tests (default on) |
| `mesh_test_node` | The eero the mesh tests are about (its location name in eero) |
| `mesh_test_scenarios` | The scenarios offered by *Scenario*, each `CODE: description` |
| `mesh_test_clients` | Stationary clients whose stability each mesh test also measures: roams and drops per 24 h connected, and time off their main eero (the one they spend the most time on). Same format as `tracked_devices`. Tests that ended before a client was added get it filled in while its 5-minute samples are kept (`sample_retention_days`) |
| `baseline_start`, `baseline_end` | Optional ISO 8601 window of normal operation, stored as the historical baseline for context |

## Data

`/share/eero_collector/eero.db` (part of HA's full backups). Expect about 2–4 MB a day while client
samples are kept, then roughly 60 MB a year for hourly summaries and usage. **Open Web UI → Download a
database snapshot** gives a consistent copy.
