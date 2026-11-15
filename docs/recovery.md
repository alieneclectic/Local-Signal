# Back up and restore Local Signal

Local Signal keeps its inventory, labels, settings, readings, and alert history in one SQLite database. The collector creates up to seven daily compressed snapshots in the `backups` directory beside that database. A backup is a consistent SQLite snapshot, including committed data still in the live database's write-ahead log.

The commands below use a source checkout and Python 3.11 or newer. A packaged app accepts the same recovery options through its executable; substitute that executable for `python3 server.py`. These commands exit without starting collection or opening a browser.

## Find the right database

Use the running dashboard's `/api/state` response, under `collector.database_path`, to identify its actual database. Common defaults are:

| Installation | Database |
| --- | --- |
| Source checkout | `.data/hearth.sqlite3` under the checkout |
| Managed macOS collector installed by `install-background.command` | `~/Library/Application Support/Hearth/data/hearth.sqlite3` |
| Packaged macOS app | `~/Library/Application Support/Local Signal/data/hearth.sqlite3` |
| Packaged Windows app | `%LOCALAPPDATA%\Local Signal\data\hearth.sqlite3` |

Always pass `--data` when working on an installed collector from a source checkout. Otherwise, the command uses the checkout's database. In the examples, replace the path with the installation you intend to recover:

```sh
local_signal_db="$HOME/Library/Application Support/Hearth/data/hearth.sqlite3"
```

## Create, list, and inspect backups

Create a manual snapshot while the collector is running or stopped:

```sh
python3 server.py --backup --data "$local_signal_db"
python3 server.py --list-backups --data "$local_signal_db"
```

Manual snapshots have unique names and are kept separately from the seven-copy daily rotation. Manual and rollback copies are not automatically rotated; remove them deliberately once you have verified that they are no longer needed. The command prints the saved path. Keep a copy on another disk if you need protection against loss of the collector's disk; the automatic snapshots are local to that disk.

Inspect a candidate before restoring it:

```sh
python3 server.py --inspect-backup "/path/to/backups/manual-copy.sqlite3.gz"
```

Inspection accepts compressed snapshots and plain SQLite files. It checks SQLite integrity and the expected Local Signal structure, then reports summary information without printing device names, addresses, readings, or credentials. Compressed input is limited to 16 GiB after decompression for both inspection and restore. Exceeding that limit stops the operation before the destination database is replaced. Inspection does not replace an installed database.

## Stop the collector before applying a restore

Close any manually started collector with Control-C, including a `dev.py` supervisor. Closing the browser alone does not stop collection. Stop the relevant background service too, so it cannot restart while the database is being replaced.

For the managed macOS collector, unload its Launch Agent without deleting its configuration:

```sh
launchctl bootout "gui/$(id -u)/local.hearth.collector"
```

After recovery, reload the same configuration:

```sh
launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.hearth.collector.plist"
```

For the packaged macOS app, record any custom host and port, then use its executable's `--disable-background` option. After recovery, use `--enable-background` with those settings. See the [packaging guide](../packaging/README.md#macos).

For the Windows package, disable and stop its scheduled task in PowerShell:

```powershell
$task = 'Local Signal Collector ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
Disable-ScheduledTask -TaskName $task
Stop-ScheduledTask -TaskName $task
```

After recovery, run `Enable-ScheduledTask -TaskName $task` and `Start-ScheduledTask -TaskName $task`.

The restore command also checks for another database owner. This protects against a second current collector and checks for older collectors that do not use the database lease. If it reports that the database is busy, stop the process using that database and retry. Do not delete a lock or SQLite `-wal`/`-shm` file to bypass the check.

## Preview and apply a restore

Preview the source and destination first:

```sh
python3 server.py --restore-backup "/path/to/backups/manual-copy.sqlite3.gz" \
  --data "$local_signal_db"
```

The preview does not overwrite the destination. After checking the paths and stopping the collector, append `--apply`:

```sh
python3 server.py --restore-backup "/path/to/backups/manual-copy.sqlite3.gz" \
  --data "$local_signal_db" --apply
```

Apply validates a staged copy before replacing anything. When the destination already exists, it first saves a unique rollback snapshot of that database. It then handles the stopped database's SQLite sidecars and publishes the restored file atomically. The original backup is retained. Read the reported rollback path and keep it until you have checked the restored installation.

If the existing database is corrupt and cannot produce a valid SQLite snapshot, apply preserves its original database and sidecar bytes in a private rollback directory before proceeding. That directory is a recovery artifact, not a validated backup; keep it for further diagnosis. Do not pass the directory itself to `--restore-backup` or assume its damaged database can be reopened.

Restoration returns the database to the snapshot's contents; it does not merge newer readings or edits. Restart the collector and check labels, rooms, alert rules, and a known history range. Check **Settings** for collection health and the next backup result. A gap during the stopped interval is expected.

To undo a restore, stop collection again and use the printed rollback snapshot as the source of another preview and apply. The same validation and protection steps apply.

## Credentials and notification delivery

Database snapshots do not include the private webhook URL or Google Wifi credentials. Those are companion files beside the database. Restoring a database leaves existing companion files untouched. On another computer, configure notifications and router monitoring again; do not include reusable credentials in a shared backup or diagnostic bundle.

Restoring an older snapshot can bring back notifications that were already sent after that snapshot was made. To avoid replaying them, restore cancels the snapshot's pending, retrying, and in-progress deliveries. Alert history and rules remain in the database. New alert transitions after restart can produce new notifications; restoring does not resend existing open alerts. Review the **Alerts** page after recovery, and use **Send test** when you intentionally want to check current webhook delivery.

## Moving from the managed Mac collector to the packaged app

The managed collector and packaged app use different data directories. Installing or launching the package alone does not import the managed collector's history. Use the explicit migration workflow in the [packaging guide](../packaging/README.md#upgrade-from-the-managed-mac-collector), which includes a plan, an apply step, and rollback instructions.
