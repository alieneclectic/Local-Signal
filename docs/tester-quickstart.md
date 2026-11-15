# Try Local Signal with sample data

Local Signal is a dashboard for monitoring devices and exploring their readings. This preview runs a simulated BACnet controller and stores its sample data in a temporary database. You can try it without a Local Signal account, device credentials, or smart-home hardware. Downloading the code and simulator needs internet access. Once launched, this preview runs entirely on your computer.

## Start the preview

On macOS or Linux, open a terminal with Git and Python 3.11 or newer. If you already downloaded Local Signal, open a terminal in the folder containing `requirements-e2e.txt` and skip the first two lines.

```sh
git clone https://github.com/alieneclectic/Local-Signal.git
cd Local-Signal
python3 -m venv .venv-e2e
.venv-e2e/bin/python -m pip install -r requirements-e2e.txt
.venv-e2e/bin/python tools/commercial_e2e.py --serve
```

After the startup checks pass, copy the URL after `PREVIEW_URL=` into a browser on the same computer. It starts with `http://127.0.0.1:` and includes a port chosen for this run. Keep the terminal open while you test. For the Windows interpreter path, see the [simulator setup guide](commercial-e2e.md#install-the-test-only-simulator).

## Explore the sample readings

1. On **Devices → Commercial**, expand **E2E BACnet controller** and choose **Device details**. It should show **Reporting**, with a supply air temperature of 22.5 °C (72.5 °F), a fan reading, and an operating mode.
2. Open **History** and choose **E2E BACnet controller: Supply air temperature** from **Device metric**. Select **Export CSV** to download the sample readings. Look for temperatures of 0, 22.5, and 24 °C collected during the startup checks.
3. Click **Refresh device readings** in the top bar to collect another sample. With this preview already running, you can change the simulated readings using the [BACnet scenarios](commercial-e2e.md#observe-the-full-path-in-a-browser).

## Troubleshoot or reset

- **Python is missing or too old.** Run `python3 --version`. Use Python 3.11 or newer to create the virtual environment. If `.venv-e2e` was created with an older Python, rename that folder and repeat setup with the newer Python.
- **The runner asks for the optional simulator.** From the Local Signal folder, repeat the `pip install` line and launch using the same `.venv-e2e` interpreter.
- **The page won’t load or shows another inventory.** Check that the preview terminal is still running and use its latest `PREVIEW_URL`. The sample controller identifies the sandbox. Port `8799` is the normal collector default and `8800` is the development preview default.
- **Collection delayed.** The preview collects on demand. After about two minutes without a refresh, the notice can appear. Click **Refresh device readings** and give the dashboard a few seconds to update.

To undo your changes or start fresh, press **Ctrl+C** in the preview terminal and wait for the `REPORT=` line. The runner removes its temporary database. Run the `--serve` line again and open the new `PREVIEW_URL` to start with fresh samples.

## Share feedback

Open an issue in [Local Signal’s GitHub Issues](https://github.com/alieneclectic/Local-Signal/issues). Include your operating system, Python version, the step you tried, what you expected, and what happened. If the terminal showed an error, paste the relevant lines after removing any credentials, real IP and MAC addresses, serial numbers, and household details. Check screenshots and logs for the same information before posting.
