# Local Signal

Local Signal is a dashboard for discovering and monitoring devices on a local network. Use it to see what's reachable, check readings from supported devices, and compare up to 30 days of history. The collector runs on a computer on the network and stores its data in SQLite. No cloud account is required.

## What you can do

- **Build an inventory.** Scan the local subnet or add a device by its private IPv4 address. Use advertised names or add your own labels, rooms, and notes.
- **Monitor devices.** See available power, energy, temperature, humidity, and media readings. Pin devices to the overview for quick access.
- **Explore history.** Compare readings across devices and export collected samples as CSV. The collector records what it observes, so downtime and missed reports leave gaps.
- **Check firmware.** See installed versions and update notices where supported. A newer published version and an update offered by the device are shown separately.

## Get started

From the repository root, run the collector with Python 3.11 or newer:

```sh
python3 server.py
```

Open [http://127.0.0.1:8799](http://127.0.0.1:8799). Select **Scan network** to discover devices on the local subnet, or **Add device** if you know the address. Keep the computer awake and the server running to collect readings. You can close the browser while collection continues.

For Google Cast telemetry, install the dependencies into a virtual environment and start the server with that environment's Python. On macOS and Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

On macOS, [install-background.command](install-background.command) can set up collection at login. It requires Xcode's Swift compiler and an Apple code signing identity. The running collector keeps up to seven daily database snapshots locally; check **Settings** for collection and backup status.

## Device support

| Devices | What Local Signal can read |
| --- | --- |
| Shelly Gen 1 and Gen 2+ | Power, energy counters, relay state, and sensor readings, depending on the model. Supported Shelly H&T sensors can also report over CoIoT when they wake. |
| Roku | Player status, active app, firmware, and other details exposed by the local API. |
| Sonos and WiiM | Volume, mute, playback state, and available track metadata through UPnP. |
| Google Cast receivers | Receiver activity, active app, volume, and available media metadata with PyChromecast installed. |

Discovery can find devices that don't expose telemetry. Available fields depend on the device model and what its local API exposes. Devices that sleep or sit on another subnet may be missed by a scan. Last reported readings are shown separately from fresh ones, and internal board temperature is separate from room temperature.

## Data and network access

Device access is read-only. Local Signal can report relay, playback, and firmware status; labels and settings are saved in its own database. Firmware checks may fetch public vendor release information without sending device data.

The dashboard listens on the computer running it by default. To make it available on a trusted local network, start it with `--host 0.0.0.0` and use the local network URL printed at startup. The dashboard has no login, so anyone who can reach it can view readings and edit dashboard data. Keep it off the public internet. Use `--port` to choose a different port or `--data` to choose a database file.

## Contributing

You can help by [reporting a bug or suggesting an improvement](https://github.com/alieneclectic/Local-Signal/issues), sharing a redacted device response, improving the documentation, or opening a pull request. Include steps to reproduce a bug. For a device integration, include the model, relevant protocol documentation, and a sample with network addresses and other private details removed.

Start with the [contributor guide](CONTRIBUTING.md) for a local preview, tests, and submission steps. You can work on the dashboard without smart-home hardware: the tests use mocked device responses and temporary databases. If you use a coding assistant, point it to [AGENTS.md](AGENTS.md).

## Data and implementation

[server.py](server.py) serves the API and dashboard, collects readings, and writes to SQLite. The frontend lives in [web/](web/) and uses plain HTML, CSS, and JavaScript. [media_readers.py](media_readers.py) handles local media status, [firmware/](firmware/) contains firmware adapters, and [tests/](tests/) covers collection, storage, and device behavior.
