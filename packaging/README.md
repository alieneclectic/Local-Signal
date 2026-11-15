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

Login startup listens on `127.0.0.1` by default. To share the dashboard on a trusted local network, add `--host 0.0.0.0` to `--enable-background`. To change an existing login configuration, run `--disable-background`, then enable it with the desired host and port. Use `--host 127.0.0.1` to return to this computer only. The shared dashboard has no login; anyone who can reach it can view readings and edit local dashboard data.

## Upgrade from the managed Mac collector

The managed collector installed by `install-background.command` stores data under `~/Library/Application Support/Hearth/data`. The packaged app uses `~/Library/Application Support/Local Signal/data`. Installing the PKG does not move data automatically. If the packaged app finds a managed database and no packaged database, opening it from Finder shows a migration dialog with a command you can copy into Terminal to preview the move. Terminal and background launches print the same guidance without a dialog. It does not start an empty inventory or migrate automatically.

Install the package on the test Mac, then run its executable from Terminal. Adjust this path if you installed the app elsewhere:

```sh
local_signal_app="/Applications/Local Signal.app/Contents/MacOS/Local Signal"
"$local_signal_app" --migrate-legacy
```

Without `--apply`, the command prints a plan and makes no changes. Check the source, destination, service configuration, and any blocking conditions. Migration refuses to overwrite an existing destination database, backup, or private configuration; it does not merge two existing installations. It also refuses symlinks or an unrecognized legacy service configuration. Resolve those conditions before applying; do not delete either data directory to force the migration through.

Apply the reviewed plan:

```sh
"$local_signal_app" --migrate-legacy --apply
```

Migration retains a previously explicit dashboard access choice from the managed agent; otherwise it uses `127.0.0.1`. The default port is 8799. Check the access address in the plan, and use `--port PORT` if another application owns that port. To deliberately share the dashboard on a trusted local network, include `--host 0.0.0.0` in both the plan and apply commands; use `--host 127.0.0.1` to keep access on this computer.

During apply, migration stops the verified managed collector and acquires source and destination database leases. It copies a consistent database snapshot, existing backups, and the private Google Wifi and webhook companion files. It keeps the original source directory. After the packaged login service starts, migration verifies that its API reports the new database path, then retires the managed Launch Agent. If the source had an agent configuration, it is kept privately at `~/Library/Application Support/Local Signal/data/migration-legacy-agent.plist`.

This is a fresh handoff, so queued notifications retain their delivery state and bound destination. It differs from restoring an older backup, which cancels waiting deliveries to prevent stale replay. Delivery remains at least once: a request accepted during an interrupted handoff may be retried with the same event ID. Credentials remain private local files; the migration plan and output do not reveal their contents.

Check the dashboard after migration: verify labels, rooms, custom alert rules, a known history range, and collection health. Device collection, Local Network permission, login startup, and clean-machine upgrades still require validation on a test Mac with the signed, notarized package. The offline migration tests use synthetic files and mocked service operations.

### If migration fails

A failed handoff attempts to stop the newly started service, remove only artifacts created by that attempt, and restore the managed service's prior configuration and running state. The original database and private files remain in the source directory. If cleanup or service recovery cannot finish, the command reports what needs attention and retains data rather than forcing removal. Read that output before trying again; an occupied port or a running manual collector may need to be resolved first.

### Return to the managed collector after a successful migration

Automatic rollback covers an unsuccessful handoff. Returning after a successful migration is a deliberate switch back to the preserved source database, whose history ends at migration. It does not carry back observations or edits collected by the packaged app.

First [create a manual backup](../docs/recovery.md#create-list-and-inspect-backups) of the packaged database. Disable its login service and stop any manually started packaged collector:

```sh
"$local_signal_app" --disable-background
```

Keep both data directories. Before restarting the preserved source, cancel its old notification backlog through the restore workflow. Those queued events may already have been delivered by the packaged app. With both collectors stopped, create a source snapshot:

```sh
local_signal_legacy_db="$HOME/Library/Application Support/Hearth/data/hearth.sqlite3"
"$local_signal_app" --backup --data "$local_signal_legacy_db"
```

Use the exact snapshot path printed by that command to preview and apply a restore to the same source database:

```sh
"$local_signal_app" --restore-backup "/path/printed/by/backup.sqlite3.gz" \
  --data "$local_signal_legacy_db"
"$local_signal_app" --restore-backup "/path/printed/by/backup.sqlite3.gz" \
  --data "$local_signal_legacy_db" --apply
```

This preserves the source snapshot's inventory and history while cancelling its pending deliveries. It also creates a rollback copy. Once complete, restore the saved managed agent configuration and start it. If the source never had a managed agent, use its original manual launch command instead.

```sh
cp "$HOME/Library/Application Support/Local Signal/data/migration-legacy-agent.plist" \
  "$HOME/Library/LaunchAgents/local.hearth.collector.plist"
chmod 600 "$HOME/Library/LaunchAgents/local.hearth.collector.plist"
launchctl enable "gui/$(id -u)/local.hearth.collector"
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.hearth.collector.plist"
```

Check that `/api/state` reports the preserved `Hearth/data/hearth.sqlite3` path. Keep the packaged data and backup available if you decide to return to it. Do not overwrite either database to combine their histories.

## Backup and recovery for packaged installations

The app executable also accepts `--backup`, `--list-backups`, `--inspect-backup FILE`, and `--restore-backup FILE`. Specify `--data PATH` when targeting a database other than the package default. Restore previews changes until `--apply` is added. Follow the [recovery guide](../docs/recovery.md) for stopping the service, checking the backup, retaining rollback copies, and restoring credentials separately.
