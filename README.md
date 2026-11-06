# Local Signal

Discover and monitor devices on your network.

A dashboard that runs on your Mac, discovers devices on the local network, monitors supported devices, and saves 30 days of history in a local SQLite database. The core collector uses the Python standard library. Google Cast telemetry uses PyChromecast in a project-local virtual environment. The currently supported local readers do not need a cloud account or Google Home connection.

**Contributing to Local Signal?** Use the [human contributor guide](CONTRIBUTING.md) or point your coding assistant at the [LLM contributor guide](AGENTS.md).

## Open the dashboard

Requires Python 3.11+ for the complete setup (tested with Python 3.14 on macOS). The core Shelly/Roku/UPnP collector can run on Python 3.10+.

Double-click **start.command**. For a fresh checkout, first run **setup.command** to install the Cast dependency in `.venv/`. You can also run the complete collector from this folder:

```sh
.venv/bin/python server.py --host 0.0.0.0
```

- On this Mac: **http://localhost:8799**
- On another device on the same local network: use the local network URL printed at startup.

For dependable collection, double-click **install-background.command** once. It installs a private local runtime, Python environment, and database under `~/Library/Application Support/Hearth/`, then registers the per-user macOS Launch Agent `local.hearth.collector`. Collection starts at login and restarts after failures. This Mac must remain awake, logged in, and connected to the local network to collect readings. Closing the browser has no effect. It resumes after sleep and records collection gaps; it does not prevent sleep or backfill missed readings.

The installer preserves the original `.data/hearth.sqlite3`, migrates a consistent SQLite snapshot on first installation, and never overwrites the installed database on an update. Run the installer again after editing project code to update the installed runtime. Later local/manual sessions use a separate project database unless you explicitly pass the installed database with `--data`; **start.command** opens or restarts the managed collector when installed. Settings shows the active collection mode and backup status.

For a manual session, keep the collector running; press Control-C to stop it. Restarting the server preserves saved devices, labels, and history.

## Background collection and backups

The collector retries failed cycles. The managed process exits if no cycle succeeds for five minutes, allowing macOS to restart it. Collection health is separate from web-server reachability, and the latest detected collection gap is visible in Settings. Changes in the Mac's local network address are checked every cycle.

The installer builds a small launcher displayed as **Local Signal**, signs it using an available Developer ID Application or Apple Development identity, and associates the Launch Agent with that app. This gives macOS a stable name and identity for its Local Network permission. Initial setup requires Xcode's Swift compiler and an installed Apple signing identity. If macOS asks, allow **Local Signal** to find and communicate with devices on the local network. The launcher file is still named `Hearth.app` so existing installations can keep their collector identity and paths. Its permission can be inspected in **System Settings → Privacy & Security → Local Network**. This uses Apple's [documented app/agent attribution](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy); no system-wide network privacy settings are changed.

Once each UTC day, the running collector creates an integrity-checked database snapshot using SQLite's backup API, publishes it atomically, and retains the seven latest daily snapshots. Backups include inventory, labels, settings, and history. Failures appear in Settings and are retried after five minutes. These are local recovery copies on the same disk, not protection against loss of the Mac. Managed paths:

- History: `~/Library/Application Support/Hearth/data/hearth.sqlite3`
- Backups: `~/Library/Application Support/Hearth/data/backups/`
- Logs: `~/Library/Application Support/Hearth/data/logs/collector.log` (rotates at 2 MB; three previous logs)
- Launch Agent: `~/Library/LaunchAgents/local.hearth.collector.plist`

From this project, run `.venv/bin/python background.py status` to inspect the service, or `.venv/bin/python background.py uninstall` to stop it and remove login startup. Uninstall preserves the runtime, database, and backups. To recover a backup, stop collection first, preserve the current database and any WAL/SHM files, and restore the selected complete SQLite snapshot before reinstalling. Do not replace a database while a collector is running.

For access only on this Mac, omit `--host 0.0.0.0`. Local network access has no login; use it only on a trusted local network and do not forward its port to the internet. Other devices on the local network can view readings and edit workspace labels.

## What is included

- **Overview:** inventory, reachable and reporting device counts, reported firmware updates, pinned devices, media status, and recent activity.
- **Devices:** full inventory with search and filters, including **Firmware** and **Updates needed**. Compact cards show the device name, IP, connection, and available firmware status. Expand a card for readings, versions, check source and time, and update instructions. Open device details to edit names, rooms, notes, and pinning. The firmware filters include the alert log, and new offers trigger a notification while the dashboard is open.
- **History:** compare readings across devices by metric and time range, toggle individual series, and export readings as CSV.
- **Network:** bounded /24 discovery, advertised device names, IP/MAC inventory, observed connection state, protocol, and last-seen time.
- **Settings:** Fahrenheit/Celsius preference, collection and backup details, and full-history CSV export.
- **Device details:** telemetry, metadata, timestamped API snapshots, last successful readings when unavailable, and per-device exports.

The collector polls every 30 seconds, with bounded concurrency and request timeouts. Manual refresh is available in the top bar. Address discovery runs when **Scan network** is clicked; it does not repeat automatically. Name discovery runs on startup, after a scan, and about every 10 minutes. **Network → Refresh names** requests a fresh name lookup without a full address scan.

Firmware checks read device status locally. When you open Devices, Local Signal retrieves the applicable public release catalogs and caches each for 24 hours. Those requests contain no device information. **Current** means the last recorded installed build matches the published release. **Update needed** requires a device-reported offer. **Checking** appears while Hearth waits for data and **Unverified** when the evidence is incomplete. **Newer release published** means the vendor lists a newer build for the matched model or track. Sonos Arc, One SL, and Sub (Gen 3) use the modern Sonos track. Roku Ultra (4660X and 4800X) and Streaming Stick+ (3810X) report their installed version and build through `GET /query/device-info`. Use Roku's own Software update menu to verify availability. Firmware details include vendor instructions where available. Sensor broadcasts do not refresh firmware evidence. Each device-reported offer can generate one in-app alert per version. For new devices and integrations, see [Contributing](CONTRIBUTING.md).

Google Nest Mini and Nest Hub (1st gen) use a read-only `GET /setup/eureka_info` on the local device. Local Signal compares the Mini's system build number or the Hub's full system firmware version on the stable channel with Google's production listing for that model. Different builds appear as **Unverified**. These devices receive updates automatically.

Standalone Chromecast and RIVA Festival receivers can report their installed Cast firmware through the same read-only endpoint. Local Signal tries HTTPS on port 8443 for Chromecast and uses the local HTTP endpoint on port 8008 for legacy Chromecast and RIVA Festival. It compares stable builds with [Google's Chromecast release notes](https://support.google.com/chromecast/answer/7124014?hl=en) when the local response identifies the exact Chromecast model or generation. A generic Chromecast identity or a RIVA speaker shows the Cast version as **Unverified**. RIVA's speaker firmware is tracked separately from its Cast receiver by [RIVA Audio](https://www.rivaaudio.com/firmware-updates).

## Supported telemetry and interpretation

Existing heating history and settings are preserved. The legacy local energy summary and CSV APIs remain available at `/api/energy` and `/api/energy.csv`; they report observed intervals from selected meters, excluding long gaps and counter resets.

Shelly Gen 1 uses read-only `/shelly` and `/status` requests. Shelly Gen 2+ uses `/shelly` and `/rpc/Shelly.GetStatus`. Available measurements depend on the model:

- Real power (W), cumulative device energy counter (kWh), relay state, and supported voltage/current measurements.
- Internal device temperature, explicitly separated from environmental sensor temperature.
- Sensor temperature/humidity, battery level, Wi-Fi RSSI, uptime, and measured ping latency.
- Separate temperature sensor and switch channels for newer Shelly devices.

A Gen 1 Shelly relay's internal board temperature **is not room temperature**. Gen 1 `meters.total` is converted from watt-minutes to kWh; Gen 2 `aenergy.total` is converted from Wh to kWh. The energy counter is the device's own cumulative counter, not an inferred daily usage or a whole-home meter. Invalid meter/sensor readings are omitted.

Readings use the collector's UTC timestamps; the browser displays local time. Long chart ranges average readings into at most roughly 300 buckets per device/metric; CSV exports retain every collected numeric sample in the selected range, plus text-state changes. Text states such as active app and playback state are recorded when they change; unchanged text is retained in the latest device snapshot. Device-supplied text that could be interpreted as a spreadsheet formula is escaped in CSV exports. No historical data is fabricated or backfilled. Gaps may represent collector downtime, missed reports, or a device going offline. The collector must run to build history.

Battery sensors can sleep between reports. An unreachable device is not automatically declared a sleeping sensor. Enable **Battery device that sleeps** only for a known battery device; this labels non-response as **May be asleep** and retains its last successful readings separately. Polling can miss short wake windows. The collector also joins the existing Shelly CoIoT multicast group (`224.0.1.187:5683`) to receive H&T reports as the sensors wake, without changing device settings. Only identified H&T hardware identities are accepted. Device cards label last reported temperatures when a sensor does not have a fresh reading. Device details retain the last successful readings separately. Receipt time is not a device-provided measurement timestamp. They do not provide continuous temperature sampling. Data can be missed while this Mac is asleep, off the network, or its server is stopped.

Discovery uses ping and the local ARP cache, followed by read-only Shelly HTTP requests. ARP entries are candidates, not proof of present connectivity. Devices that block ping can still be reachable through their API. Network inventory can include computers, phones, and routers; it is not proof every entry is an IoT device. IPv6-only, sleeping, isolated, and other-subnet devices may be missed. Add known private IPv4 addresses manually.

Device names are collected from Bonjour/mDNS service and host records, local reverse DNS on the network gateway, and advertised UPnP device descriptions. Friendly names are preferred over opaque service IDs; actual device-declared model names are shown when available. Some devices publish only a serial-like hostname, and others publish no name. A discovered name is identity evidence, not confirmation of telemetry support or current connectivity.

Automatically generated labels such as `Device .30` are replaced when an advertised name is found. Custom device labels are preserved; their discovered identity remains visible in device details. Typing a name selects manual naming. Enable **Use discovered name when available** in device details to switch back to automatic naming. Existing generated labels are migrated to automatic mode; other existing names stay manual. Name/source metadata is stored locally with the inventory.

Cloud-only/proprietary devices need their own integration. Authentication-protected Shelly APIs are identified but credentials are not configured by this version. No heater/relay commands, firmware changes, or device configuration changes are sent. Editing labels affects this dashboard's database only.

Official protocol references: [Shelly Gen 1](https://shelly-api-docs.shelly.cloud/gen1/), [Shelly Gen 2+ status](https://shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Shelly/), [battery device sleep behavior](https://shelly-api-docs.shelly.cloud/gen2/General/SleepManagementForBatteryDevices/).

## Local media telemetry

The collector reads local status from:

- **Roku ECP:** player power mode, active app/screensaver, uptime, firmware, network connection, and headphones state. Some Roku devices restrict `/query/media-player` with HTTP 403; other fields remain available. Player power mode describes the Roku, not the connected TV.
- **Sonos UPnP:** volume/mute, transport state, available track metadata, and coordinator/bonded-component roles. Component volume is labeled separately from room volume; a bonded component is not assigned an invented independent playback state.
- **WiiM UPnP:** reported volume/mute and network-renderer transport/track state. Advertised UPnP description URLs are preferred when discovered.
- **Google Cast receivers:** receiver activity, app, receiver volume/mute, and available Cast media metadata. This works with the tested Chromecast, Nest Mini, and RIVA speakers. An idle Cast receiver does not prove the entire device is off or that a different physical input is inactive.

The new readers use fixed Roku query URLs, an explicit allowlist of UPnP `Get*` actions, and Cast status requests. They never launch apps, alter playback/volume, or change device settings. Numeric measurements build chart history; text-state transitions are included in CSV history. API snapshots store curated media fields and omit playback URLs, advertising IDs, and authentication material.

The **Media** filter shows these devices. The overview includes a compact media summary; pin individual media devices to include them in My devices. Unsupported, paired, or cloud-managed devices continue to show network reachability until a suitable authenticated integration is connected. This version does not connect to Google Home, LG webOS pairing, myQ, Tapo, or camera accounts.

References: [Roku ECP](https://developer.roku.com/dev/docs/external-control-api), [WiiM developer APIs](https://wiimhome.com/support), [PyChromecast](https://github.com/home-assistant-libs/pychromecast). UPnP endpoints are read from each local device's own description.

## Data and implementation

```text
server.py               Python standard-library server, discovery, collector, SQLite storage
energy.py               Hardware-keyed energy summaries, coverage, local-day boundaries, rate validation
reliability.py          Consistent daily SQLite backups and bounded managed logs
background.py           macOS Launch Agent installation, update, status, and removal
macos/Collector.swift   Signed app launcher for per-app local-network permission
install-background.command  Double-click background setup
coiot.py                Passive Shelly H&T CoIoT v2 receiver and bounded packet parser
network_names.py        Bonjour, local DNS, and UPnP name discovery
media_readers.py        Read-only Roku, UPnP/Sonos/WiiM, and Cast telemetry
firmware/               Firmware findings, device adapters, and model catalog
requirements.txt        Pinned PyChromecast version
setup.command           Install local Cast requirements and launch
web/                    Buildless HTML, CSS, JavaScript, and local SVG assets
start.command           macOS double-click launcher, with local network access
.data/hearth.sqlite3    Private local inventory, labels, samples, and events (Git-ignored)
tests/                  Normalization, API, room-source, sleep retention, and CoIoT tests
```

No browser/device data goes to a cloud service. Device HTTP requests target private IPv4 addresses, never follow redirects, and ignore HTTP proxy environment settings. Name discovery uses local multicast and queries your private network gateway directly; it does not send private device lookups to a public DNS resolver. UPnP description requests are restricted to XML locations on the responding device, with no redirects or query parameters. Arbitrary DNS TXT fields are not stored; only public name/model fields are retained. API writes require a same-origin request with the dashboard's custom request header. Host validation rejects unexpected hosts. Device records and raw responses are escaped before rendering. The HTTP server serves only explicitly allowed frontend files.

The server supports `--port 8799`, `--host 127.0.0.1|0.0.0.0`, and `--data /absolute/path/to/database.sqlite3`. macOS and Linux network discovery are implemented; only macOS has been validated on a live local network. This is a local monitoring tool, not a hardened internet-facing service.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Automated tests use temporary databases and mocked device responses; they do not change real devices. Live verification used read-only probes against the local network and browser checks at mobile and desktop widths.
