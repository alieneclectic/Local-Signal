# Local Signal installer previews

Briefcase builds a Windows MSI and a macOS PKG on their respective operating systems. Both include Python and the collector's dependencies. For regular Mac collection, use [install-background.command](../install-background.command) while the PKG is in preview.

## Windows

From the repo root in PowerShell, with Git and Python 3.14 installed:

```powershell
py -3.14 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
& $python -m pip install -r requirements.txt 'briefcase==0.4.7'
& $python -m unittest discover -s tests -v
```

To try the collector before packaging, use a separate database and port. Open `http://127.0.0.1:8800`, run **Scan network**, then check readings on a supported device. Press Ctrl+C when finished.

```powershell
$testDb = Join-Path $env:TEMP "local-signal-$([guid]::NewGuid()).sqlite3"
& $python server.py --port 8800 --data $testDb
```

Build a local test MSI. `--adhoc-sign` leaves the Windows build unsigned. Use a signed build for distribution.

```powershell
& .\.venv\Scripts\briefcase.exe package windows app --update -p msi --adhoc-sign --no-input
```

Open the MSI under `dist` as your regular user. It registers a Task Scheduler entry to run the collector at sign-in. Sign out and back in, then open **Local Signal** from the Start menu or go to `http://127.0.0.1:8799`. Check the task and the collector in PowerShell:

```powershell
$task = 'Local Signal Collector ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Get-ScheduledTask -TaskName $task
(Invoke-RestMethod http://127.0.0.1:8799/api/state).collector |
    Select-Object background, health, sensor_listener, database_path
```

Verify that `background` is `True` and health becomes `healthy`. Scan for devices and verify at least one reading and one history entry. If you use Shelly CoIoT sensors, check that a sensor report arrives after it wakes. If Windows Firewall prompts, select your private network for device discovery and sensor reception. To check upgrades, add a label, run a newer MSI, and verify the label and history survive. Uninstall should remove the task and keep the database and backups in `%LOCALAPPDATA%\Local Signal\data`.

## macOS

Build a local test PKG from the repo root with Xcode and `uv` available:

```sh
uvx --python 3.14 --from briefcase==0.4.7 briefcase package macOS app --update -p pkg --adhoc-sign --no-sign-installer --no-notarize --no-input
```

The build targets the Mac's current architecture. Test the app bundle in `build/collector/macos/app` on a separate port and with collection turned off:

```sh
test_dir=$(mktemp -d)
"build/collector/macos/app/Local Signal.app/Contents/MacOS/Local Signal" \
    --port 8800 --data "$test_dir/preview.sqlite3" --no-collector
```

Open `http://127.0.0.1:8800`. Leave scan and refresh controls alone for a test without device access. The app installed by the PKG normally uses `~/Library/Application Support/Local Signal/data`.

On a test Mac, install the PKG before checking login startup. Use the app executable's `--enable-background` option to register its login agent and `--disable-background` to remove it. Use `--port 8800` with `--enable-background` if port 8799 is occupied. Use a signed, notarized package to verify Local Network permission, read-only device collection, restart after failure, and upgrades before distribution.
