# Commercial end-to-end tests

The standalone harness exercises real BACnet/Modbus packets through the production collector, a temporary SQLite database, and the production HTTP state/history/CSV routes. It can also serve the normal dashboard for browser verification. It never opens your installed database or starts the Launch Agent, discovery, CoIoT listener, router polling, or firmware vendor lookups.

## Install the test-only simulator

From the repository root, with Python 3.11 or newer:

```sh
python3 -m venv .venv-e2e
.venv-e2e/bin/python -m pip install -r requirements-e2e.txt
```

Only BACpypes3 0.0.110 is installed for the independent BACnet simulator. This dependency is not added to the app or its packaged collector. On Windows, use `.venv-e2e\Scripts\python.exe` in place of `.venv-e2e/bin/python`.

## Run the automated checks

Local BACnet only, with no public requests:

```sh
.venv-e2e/bin/python tools/commercial_e2e.py
```

BACnet plus the explicitly enabled Pult.Online public Modbus demo:

```sh
.venv-e2e/bin/python tools/commercial_e2e.py --pult --report /tmp/local-signal-commercial-e2e.json
```

The runner prints `PASS`/`FAIL` checks, exits nonzero on failure, and writes a JSON report. Without `--report`, it prints a unique report path in the OS temporary directory. Temporary databases are removed on exit, including after failure. The ordinary `python3 -m unittest discover -s tests -v` suite never runs this public test or requires BACpypes3.

Automated coverage:

- Creating inventory through the real Add Device API, then collecting via the Refresh API.
- BACnet analog, binary, and multi-state values; changes; observed zero; fault/out-of-service flags; missing objects; a real stopped UDP simulator and recovery after restart.
- Historical readings remain distinct from fresh measurements, and invalid points produce no invented zero samples.
- History/CSV contains the observed changes and zero; stored sample counts match export rows.
- Closing and reopening the database/server preserves device identity, labels, notes, map, timestamps, and observations. A new collection succeeds after reopening.
- With `--pult`, live generated input-register and discrete-input readings reach the collector, database, and CSV. An injected connection failure is followed by actual external recovery reads. No public device is stopped or changed.
- Normal production validators and the Add Device API still reject public addresses; origin and request-header checks remain enforced.

An external outage can make the public checks fail even when local checks pass. The report includes the failed stage and device/point error. The first Pult availability failure exits with code 2 and status `external_unavailable`; other assertion/setup failures exit with code 1. There are no automatic network retries. These checks establish interoperability with a simulator/public demo, not manufacturer or physical-device compatibility.

## Observe the full path in a browser

```sh
.venv-e2e/bin/python tools/commercial_e2e.py --pult --serve --report /tmp/local-signal-commercial-e2e.json
```

After all checks pass, open the printed `PREVIEW_URL`. The port is selected automatically on loopback and the database is disposable. Both devices should be **Reporting**. The private addresses displayed in this dashboard are test aliases, not devices on your LAN:

| Inventory alias | Test-process destination |
| --- | --- |
| `10.255.250.2:47808` | BACpypes3 on `127.0.0.1`, on a temporary UDP port |
| `10.255.250.1:502` | Pult.Online's documented `45.8.248.56:502` endpoint, only with `--pult` |

Use **History** to select either device's point, and **Export CSV** to inspect original values. The live public counter is synthetic and does not represent power, temperature, or real plant data. **Refresh** polls both fixtures. Collection is manual in preview mode, so the normal delayed-collection notice can appear after two minutes without a refresh.

The harness also exposes a test-only scenario endpoint. Replace `PORT` with the port in `PREVIEW_URL`:

```sh
curl -sS http://127.0.0.1:PORT/__e2e__/bacnet \
  -H 'Content-Type: application/json' -H 'X-Hearth-Request: 1' \
  --data '{"scenario":"changed"}'
```

It changes the independent simulator and starts real collection. The normal dashboard picks up the resulting database/API readings on its next five-second UI check. The endpoint exists only in this standalone harness, uses normal host/origin/header validation, and accepts only these named scenarios:

| Scenario | Expected observation |
| --- | --- |
| `healthy` / `recover` | 22.5 °C, fan inactive, mode 2; restarts the simulator if needed |
| `changed` | 24 °C, fan active, mode 3 |
| `zero` | 0 °C, fan inactive, mode 1; zero is fresh telemetry |
| `fault` | Temperature omitted as invalid; other points still report |
| `out_of_service` | Temperature omitted as invalid; other points still report |
| `offline` | Actual simulator stopped; device unreachable, previous readings historical |

The browser can show °F according to its saved preference: 22.5 °C → 72.5 °F, 24 °C → 75.2 °F, and 0 °C → 32 °F. Reopen an already open details dialog to inspect a fresh snapshot; the inventory/overview updates automatically.

Each refresh/scenario also reads Pult when enabled. The entire process permits at most **24 public input requests**, including automated checks and manual preview refreshes. Once that cap is reached, further public reads fail locally without sending requests. Restart explicitly if more testing is needed. Stop with Ctrl-C; the final report includes request/connection counts and the temporary database is removed. In `--serve` mode the report is written when the preview exits.

## Network boundaries

All address validation is unchanged. The test process replaces only the BACnet/Modbus modules' socket references with narrow transport facades. It does not patch the global `socket` module, the readers, normalizers, private-address validators, Store, or production HTTP handlers' mutation logic. The test Handler adds the scenario route and blocks unrelated POST routes.

The Pult facade accepts only the fixed private alias/port, connects only to the fixed published public demo, and permits exactly two read shapes: unit 1, input register 1 (function 04, one uint16) and discrete input 1 (function 02, one bit). It rejects writes, other units/maps/targets, and excess requests before sending. The BACnet facade connects only the fixed BACnet alias to its loopback simulator and permits only unicast ReadProperty packets. Unexpected inventory addresses/protocols are blocked before any production probe. There is no public-target option, production setting, environment bypass, public listen address, or installed-app mode.

Pult connection/map documentation: [operator's public Modbus test-server page](https://modbus.pult.online/en.html). Read-only input tables are generated by the service; no writes to shared coils or holding registers are part of these tests.
