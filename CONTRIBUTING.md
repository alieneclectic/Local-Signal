# Contributing to Local Signal

You can contribute without smart-home hardware. The tests use mock device responses, temporary SQLite databases, and loopback HTTP servers. See [the project README](README.md#data-and-implementation) for an overview of the code.

## Get started locally

Use Python 3.11 or newer for development and Node.js for the JavaScript syntax check. The dashboard uses plain HTML, CSS, and JavaScript served by Python. The tests and preview below run without the optional Cast dependency or an installed macOS Launch Agent. If you need live Cast telemetry, create a virtual environment and install the pinned dependency.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

To preview UI changes from the repo root, start the development server with a disposable database.

```sh
local_signal_preview_dir=$(mktemp -d)
python3 dev.py --data "$local_signal_preview_dir/preview.sqlite3" --no-collector
```

Open [the local preview](http://127.0.0.1:8800/). The dashboard reloads on its next five-second check after a frontend edit or a backend restart. Python and firmware catalog edits restart the server and its collector, ending any scan in progress. Frontend edits leave the server running. Press Control-C to stop the development server.

The command above starts with an empty inventory and skips automatic polling and the sensor listener. Manual actions such as **Refresh**, **Scan network**, **Refresh names**, and **Add device** can still contact the network. Leave those controls alone for a UI-only preview. For development with live collection, run `python3 dev.py` with maintainer authorization. It uses the separate `.data/dev/hearth.sqlite3` database, polls known devices every 30 seconds after the previous cycle finishes, and scans for new devices when you select **Scan network**.

The macOS background collector at `http://127.0.0.1:8799` serves an installed copy, which `start.command` opens when it is running. Use the development URL to see edits from this checkout.

## Working with an LLM

Open the repository in your coding assistant and give it [AGENTS.md](AGENTS.md). For example, paste this prompt and fill in the change you want.

> Read `AGENTS.md` and `CONTRIBUTING.md`. Help me [describe the change]. Run the relevant local checks and summarize what you verified.

## Report a bug or share a device sample

Include what you expected, what happened, and steps to reproduce it. For an integration, include the exact model ID, a link to the manufacturer's protocol documentation, and a small redacted response. Include hardware revision, region, and release channel when relevant. Replace real IP and MAC addresses with consistent synthetic values across related samples. Remove serial numbers, Wi-Fi details, tokens, and account or household information from fixtures, logs, and screenshots.

## Add firmware support for a device model

For a Shelly Gen 1 device that uses the existing endpoints, add its exact `/shelly` `type` and display name to [the Shelly catalog](firmware/catalog/shelly.json). The firmware reader already uses the standard Gen 1 status fields for any identified Shelly that returns them. Gen 2+ devices usually supply their own `model` in `/shelly`. Add a small fixture to [the Shelly examples](tests/fixtures/shelly/) if the model supplies a new response shape. Use the fixture to show how Local Signal handles the response.

## Add a firmware interface

1. Add an adapter in [`firmware/adapters/`](firmware/adapters/) and document the read-only device or controller requests it uses. Use a documented device identity that survives IP address changes. The current LAN integration uses a MAC reported by the device itself.
2. Return an [`Observation`](firmware/checks.py) containing the device identity, installed version, offered version, channel, source, status, and observation time. Use `unknown` for unavailable, ambiguous, or malformed update data. An adapter should report a stable release only when the device or vendor identifies it as such.
3. Wire the adapter into the collector and firmware inventory in [`server.py`](server.py). Keep vendor parsing in the adapter so findings and alert deduplication work the same way for every device. Include new runtime files in the [background installer](background.py).
4. Add redacted fixtures for a supported response and the relevant failure or unknown case. Include an available release and a response without one when the interface can report both. Use [the firmware tests](tests/test_firmware.py) as an example.

## Add a router adapter

1. Add a module under [`routers/adapters/`](routers/adapters/) and register its stable ID in [`routers/__init__.py`](routers/__init__.py). Use the [Google Wifi adapter](routers/adapters/google_wifi.py) as a reference. Add the file to [the macOS background installer](background.py) and document the supported models and setup in the README.
2. Implement the [adapter contract](routers/monitor.py). Declare `NAME` and the `CAPABILITIES` the adapter can read. Implement `configure(database, action)` for the local CLI and `load_config(database)` so monitoring is off until configured. Keep credentials outside the database and API, and make disconnect remove the adapter's local configuration.
3. Have `reader(config)` return a callable that reads and normalizes one snapshot. Report per-device internet upload and download in bits per second and pause as a boolean. Return `None` for missing or ambiguous readings and reserve zero for observed zero. If the selected model exposes fewer capabilities than the adapter supports, return a narrower `capabilities` tuple in the snapshot. The shared monitor handles polling, backoff, stale readings, and the Network page.
4. Use bounded read-only requests. LAN adapters should target validated private IPv4 addresses and disable redirects and proxies. Document the endpoint and any required authentication. Use `RouterError` with a message safe for display. Reserve `unsupported` for a model the adapter has identified as incompatible, and use `setup_required` for configuration or authentication failures.
5. Add synthetic or redacted fixtures for normal, incomplete, and failed reads. Include a test for any partial capability or model restriction and use a temporary database. The [router tests](tests/test_routers.py) show how to exercise the shared contract without contacting a router.

## Verify and submit

Run these checks from the repo root before opening a pull request.

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

In the pull request, explain the change, how you reproduced or verified it, and any remaining checks. For UI changes, check the affected view at desktop and mobile widths. For device changes, distinguish fixture results from live hardware results. If you tested with hardware, record the model, the read-only request, and what it returned after redacting private data.
