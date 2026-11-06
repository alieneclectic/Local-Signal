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

Open [http://127.0.0.1:8799](http://127.0.0.1:8799). A fresh installation starts with an empty device list. Select **Scan network** to discover devices on the local subnet, or **Add device** if you know the address. Keep the computer awake and the server running to collect readings. You can close the browser while collection continues.

For Google Cast telemetry, install the dependencies into a virtual environment and start the server with that environment's Python. On macOS and Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python server.py
```

On macOS, [install-background.command](install-background.command) can set up collection at login. It requires Xcode's Swift compiler and an Apple code signing identity. The running collector keeps up to seven daily database snapshots locally. Check **Settings** for the code source and collection and backup status.

Run `./install-background.command --follow-checkout` if you want the background collector at port 8799 to follow edits in this checkout. It reloads the page after frontend edits and restarts collection after backend edits. The collector uses the installed snapshot when the checkout is unavailable or fails to load. Both sources use the same installed history. Rerun the installer when dependencies or the launcher change. Use `--installed-snapshot` to switch back to manual updates.

For Windows collector setup and the Briefcase installer previews, see the [packaging guide](packaging/README.md).

## Device support

| Devices | What Local Signal can read |
| --- | --- |
| Shelly Gen 1 and Gen 2+ | Power, energy counters, relay state, and sensor readings, depending on the model. Supported Shelly H&T sensors can also report over CoIoT when they wake. |
| Roku | Player status, active app, firmware, and other details exposed by the local API. |
| Sonos and WiiM | Volume, mute, playback state, and available track metadata through UPnP. |
| Google Cast receivers | Receiver activity, active app, volume, and available media metadata with PyChromecast installed. |

Discovery can find devices that don't expose telemetry. Available fields depend on the device model and what its local API exposes. Devices that sleep or sit on another subnet may be missed by a scan. Last reported readings are shown separately from fresh ones, and internal board temperature is separate from room temperature.

## Router monitoring

The **Network** page can show per-device upload and download rates and internet pause status through optional read-only router adapters. The page lists the adapters available in your build. An adapter reports **Unsupported** for measurements it cannot provide and **Unknown** when an individual reading is missing. Local network discovery is available independently.

The bundled adapter is **Google Wifi (experimental)**. You can request support for your router model or contribute an adapter. [Request support](https://github.com/alieneclectic/Local-Signal/issues/new) · [Add a router adapter](CONTRIBUTING.md#add-a-router-adapter)

### Google Wifi monitoring (experimental)

This adapter uses Google's unofficial cloud API to read one Google Wifi mesh. It refreshes about once a minute. Pause status reflects the device policy Google reports.

1. Install the dependencies from `requirements.txt` in the collector's Python environment. If you use the virtual environment above, use `.venv/bin/python` in the next step.
2. On the collector computer, open [Google sign-in](https://accounts.google.com/EmbeddedSetup) in a desktop browser and sign in with the Google account that manages the mesh. After accepting Google's terms, open the browser's developer tools. Under **Application → Storage → Cookies**, select `https://accounts.google.com` and copy the value of `oauth_token`.
3. In a terminal, run `python3 server.py --router google-wifi connect`. Enter your email and paste the cookie at the hidden prompt. If Google lists multiple meshes, select the one to monitor.
4. Open the dashboard on the collector computer at `http://localhost:8799/#network` (or the configured port). Router readings are available over loopback only.

If the collector uses a custom or installed database, add `--data "/path/to/hearth.sqlite3"` to the setup command. The collector's `database_path` appears in its `/api/state` response. The collector picks up the connection automatically. To disconnect, use `--router google-wifi disconnect` with the same database. The earlier `--google-wifi connect` and `--google-wifi disconnect` commands still work.

Setup stores a reusable Google credential in a private companion file next to the database. The credential itself can grant broader access than the dashboard uses. Use a database directory accessible only to your OS account and keep the file and sign-in cookie out of backups you share, screenshots, and chat. Disconnecting removes the local credential file. Google's undocumented API or sign-in flow can change. [Protocol reference](https://github.com/spkon/ha_google_wifi/blob/main/docs/API_ENDPOINTS.md)

## Data and network access

Device access is read-only. Local Signal can report relay, playback, and firmware status; labels and settings are saved in its own database. Firmware checks may fetch public vendor release information without sending device data.

The dashboard listens on the computer running it by default. To make it available on a trusted local network, start it with `--host 0.0.0.0` and use the local network URL printed at startup. The dashboard has no login, so anyone who can reach it can view readings and edit dashboard data. Keep it off the public internet. Use `--port` to choose a different port or `--data` to choose a database file.

## Contributing

You can help by [reporting a bug or suggesting an improvement](https://github.com/alieneclectic/Local-Signal/issues), sharing a redacted device response, improving the documentation, or opening a pull request. Include steps to reproduce a bug. For a device integration, include the model, relevant protocol documentation, and a sample with network addresses and other private details removed.

Start with the [contributor guide](CONTRIBUTING.md) for a local preview, tests, and submission steps. You can work on the dashboard without smart-home hardware: the tests use mocked device responses and temporary databases. If you use a coding assistant, point it to [AGENTS.md](AGENTS.md).

## Data and implementation

[server.py](server.py) serves the API and dashboard, collects readings, and writes to SQLite. The frontend lives in [web/](web/) and uses plain HTML, CSS, and JavaScript. [media_readers.py](media_readers.py) handles local media status. [firmware/](firmware/) and [routers/](routers/) contain their respective adapters. [tests/](tests/) covers collection, storage, and device behavior.

## License

Local Signal is available under the [MIT License](LICENSE).
