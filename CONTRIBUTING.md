# Contributing to Hearth

You can contribute without smart-home hardware. The tests use mock device responses, temporary SQLite databases, and loopback HTTP servers. See [the project README](README.md#data-and-implementation) for an overview of the code.

## Get started locally

Use Python 3.11 or newer for development and Node.js for the JavaScript syntax check. The dashboard uses plain HTML, CSS, and JavaScript served by Python. The tests and preview below run without the optional Cast dependency or an installed macOS Launch Agent. If you need live Cast telemetry, create a virtual environment and install the pinned dependency.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

To preview UI changes from the repo root, start a loopback-only server on a separate port with a disposable database.

```sh
hearth_preview_dir=$(mktemp -d)
python3 server.py --host 127.0.0.1 --port 8800 --data "$hearth_preview_dir/hearth.sqlite3" --no-collector
```

Open [the local preview](http://127.0.0.1:8800/). The preview includes seeded inventory and starts without background polling or the sensor listener. Manual actions such as **Refresh**, **Scan network**, **Refresh names**, and **Add device** can still contact the network. Leave those controls alone for a UI-only preview. Press Control-C to stop the server.

## Working with an LLM

Open the repository in your coding assistant and give it [AGENTS.md](AGENTS.md). For example, paste this prompt and fill in the change you want.

> Read `AGENTS.md` and `CONTRIBUTING.md`. Help me [describe the change]. Run the relevant local checks and summarize what you verified.

## Report a bug or share a device sample

Include what you expected, what happened, and steps to reproduce it. For an integration, include the exact model ID, a link to the manufacturer's protocol documentation, and a small redacted response. Include hardware revision, region, and release channel when relevant. Replace real IP and MAC addresses with consistent synthetic values across related samples. Remove serial numbers, Wi-Fi details, tokens, and account or household information from fixtures, logs, and screenshots.

## Add firmware support for a device model

For a Shelly Gen 1 device that uses the existing endpoints, add its exact `/shelly` `type` and display name to [the Shelly catalog](firmware/catalog/shelly.json). The firmware reader already uses the standard Gen 1 status fields for any identified Shelly that returns them. Gen 2+ devices usually supply their own `model` in `/shelly`. Add a small fixture to [the Shelly examples](tests/fixtures/shelly/) if the model supplies a new response shape. Use the fixture to show how Hearth handles the response.

## Add a firmware interface

1. Add an adapter in [`firmware/adapters/`](firmware/adapters/) and document the read-only device or controller requests it uses. Use a documented device identity that survives IP address changes. The current LAN integration uses a MAC reported by the device itself.
2. Return an [`Observation`](firmware/checks.py) containing the device identity, installed version, offered version, channel, source, status, and observation time. Use `unknown` for unavailable, ambiguous, or malformed update data. An adapter should report a stable release only when the device or vendor identifies it as such.
3. Wire the adapter into the collector and firmware inventory in [`server.py`](server.py). Keep vendor parsing in the adapter so findings and alert deduplication work the same way for every device. Include new runtime files in the [background installer](background.py).
4. Add redacted fixtures for a supported response and the relevant failure or unknown case. Include an available release and a response without one when the interface can report both. Use [the firmware tests](tests/test_firmware.py) as an example.

## Verify and submit

Run these checks from the repo root before opening a pull request.

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
```

In the pull request, explain the change, how you reproduced or verified it, and any remaining checks. For UI changes, check the affected view at desktop and mobile widths. For device changes, distinguish fixture results from live hardware results. If you tested with hardware, record the model, the read-only request, and what it returned after redacting private data.
