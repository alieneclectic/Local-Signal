# Local Signal contributor instructions for coding agents

Local Signal is a dashboard for devices on local networks. Python serves the API and static frontend, polls supported devices, and stores observations in SQLite. Read [CONTRIBUTING.md](CONTRIBUTING.md) for setup and submission steps and [README.md](README.md) for product behavior.

## Workflow

1. Run `git status --short` and preserve existing work. Locate the code and tests for the requested change using the map below.
2. Trace the affected path through device input, normalization, storage, API, and UI as applicable. Use the existing entry points and fixture patterns.
3. Run the relevant local checks. Report what passed, what was skipped, and whether device behavior was verified with fixtures or actual hardware.

## Code map

| Change | Start here | Test examples |
| --- | --- | --- |
| Shelly telemetry, discovery, persistence, or HTTP API | [`server.py`](server.py) (`probe_device`, `normalize`, `Collector`, `Store`, `Handler`) | [`tests/test_collector.py`](tests/test_collector.py) |
| Room sensors and passive CoIoT reports | [`coiot.py`](coiot.py), `Store.receive_sensor`, `Store.link_room` | [`tests/test_room_sensors.py`](tests/test_room_sensors.py) |
| Device names or media status | [`network_names.py`](network_names.py), [`media_readers.py`](media_readers.py) | [`tests/test_network_names.py`](tests/test_network_names.py), [`tests/test_media_readers.py`](tests/test_media_readers.py) |
| Firmware evidence and update alerts | [`firmware/`](firmware/), `Store.record`, `/api/state` | [`tests/test_firmware.py`](tests/test_firmware.py), [`tests/fixtures/shelly/`](tests/fixtures/shelly/) |
| Heating energy, coverage, or cost | [`energy.py`](energy.py), `Store.energy_summary`, `/api/energy` | [`tests/test_energy.py`](tests/test_energy.py) |
| Dashboard UI | [`web/index.html`](web/index.html), [`web/app.js`](web/app.js), [`web/style.css`](web/style.css) | API tests in [`tests/test_collector.py`](tests/test_collector.py) and a loopback browser preview |
| Alert rules, alert history, or webhook notifications | [`alerts.py`](alerts.py), `Store.update_alerts`, `Collector.evaluate_alerts`, `/api/alerts/*` | [`tests/test_alerts.py`](tests/test_alerts.py) |
| Backups or macOS background collection | [`reliability.py`](reliability.py), [`background.py`](background.py), [`macos/Collector.swift`](macos/Collector.swift) | [`tests/test_reliability.py`](tests/test_reliability.py) |

## Behavior to preserve

- **Read-only device access.** Reuse bounded requests to validated private IPv4 addresses for device access. Preserve redirect and proxy restrictions. Keep relay, playback, firmware, and device configuration controls out of the collector. Dashboard writes edit local data and must retain the existing host, origin, and request-header checks.
- **Observed data.** Keep invalid or missing readings distinct from zero and last-known readings distinct from fresh ones. Keep board temperature separate from room temperature. Exclude counter resets and long gaps from observed energy. Use established hardware identity when tying observations across IP changes.
- **Firmware evidence.** Preserve unknown and stale states. Use the adapter's stable release evidence and the shared alert deduplication keyed by device identity, channel, and version. Keep the public catalog request free of device data.
- **Existing installations.** Preserve labels, settings, and history when changing `Store` or its schema. Verify migrations against a temporary database. Add new Python modules and runtime data to `background.py`'s `FILES` list when the managed collector needs them. If adding a web asset, update the static allowlist in `Handler`.
- **Local data.** Use synthetic fixtures, mocked device/vendor requests, and temporary SQLite files. Keep real `.data/`, installed databases, backups, and unredacted responses out of the change. Get maintainer authorization before probing a live LAN or changing the installed Launch Agent.

## Local checks

Use Python 3.11+ and run from the repository root. These commands need no device access, Cast dependency, or frontend build step.

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

For a focused run, use `python3 -m unittest discover -s tests -p 'test_firmware.py' -v` and substitute the affected test file. For UI changes, use the [loopback preview](CONTRIBUTING.md#get-started-locally) with a fresh database. `--no-collector` skips startup collection, but manual refresh, scan, and device-add requests can still trigger network activity. Mock vendor lookups when testing with firmware fixtures.

## Cloud CI

Run suitable checks locally and batch fixes. Ask before starting or rerunning quota-consuming cloud CI, including any push that triggers it, unless that run was already authorized. Keep GitHub Actions disabled for `alieneclectic` repositories unless the maintainer approves enabling it. For new `alieneclectic` repositories, disable Actions before pushing workflow files. Keep paid overage at $0 with the hard stop enabled unless the maintainer approves a change. Preserve required release, signing, security, and performance checks and report any cloud gates deferred for lack of authorization or quota.
