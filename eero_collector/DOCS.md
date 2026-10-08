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
| `tracked_devices` | Clients that get their own HA device (connected, node, band, signal, link quality, data today/last hour). Each entry is an eero name, hostname or MAC, optionally with a friendly name: `desktop-1006 = Array Desktop`. The Web UI's Devices page shows the names. |
| `backfill`, `backfill_device_hourly` | Fetch eero's stored history (network and per-device) on first run |
| `sample_retention_days` | Keep 5-minute client samples this long; older ones are kept as hourly summaries |
| `timezone` | Time zone for "today" (eero's day boundaries) |

## Data

`/share/eero_collector/eero.db` (part of HA's full backups). Expect about 2–4 MB a day while client
samples are kept, then roughly 60 MB a year for hourly summaries and usage. **Open Web UI → Download a
database snapshot** gives a consistent copy.
