"""Bounded local logging and consistent, rotating SQLite snapshots."""
from datetime import datetime, timezone
from contextlib import closing, nullcontext
import gzip
import logging
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
import shutil
import sqlite3
import sys


def configure_logging(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(folder / 'collector.log', maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    class LogStream:
        def __init__(self, level):
            self.level = level
        def write(self, message):
            if message.strip():
                logging.getLogger('hearth').log(self.level, message.rstrip())
        def flush(self):
            handler.flush()

    sys.stdout, sys.stderr = LogStream(logging.INFO), LogStream(logging.ERROR)


class RecoveryError(ValueError):
    """A backup or restore cannot safely proceed."""


class DatabaseBusyError(RecoveryError):
    """The database is already owned by a collector or maintenance operation."""


def _path(value, *, required=False):
    path = Path(value).expanduser().absolute()
    if path.is_symlink():
        raise RecoveryError('Use a regular file, not a symbolic link.')
    if path.exists() and not path.is_file():
        raise RecoveryError('The database or backup path must be a regular file.')
    if required and not path.is_file():
        raise RecoveryError('The database or backup file does not exist.')
    return path.resolve()


def _database_files(path):
    files = [path, *[Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal')]]
    for file in files:
        if file.is_symlink():
            raise RecoveryError('Database sidecars must not be symbolic links.')
        if file.exists() and not file.is_file():
            raise RecoveryError('Database sidecars must be regular files.')
    return [file for file in files if file.exists()]


def prepare_database(database):
    """Restrict an existing database and make new SQLite files private from creation."""
    path = _path(database)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != 'posix':
        return path

    existing = _database_files(path)
    for file in [path, *(file for file in existing if file != path)]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if file == path:
            flags |= os.O_CREAT
        try:
            descriptor = os.open(file, flags, 0o600)
        except FileNotFoundError:
            if file == path:
                raise
            continue  # SQLite may have removed an idle sidecar.
        try:
            mode = os.fstat(descriptor).st_mode
            if not stat.S_ISREG(mode):
                raise RecoveryError('Database files must be regular files.')
            private_mode = mode & 0o600
            if stat.S_IMODE(mode) != private_mode:
                os.fchmod(descriptor, private_mode)
        finally:
            os.close(descriptor)
    return path


def _check_legacy_users(path):
    """Refuse open SQLite handles from collectors predating the advisory lease."""
    files = _database_files(path)
    if not files:
        return
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for file in files:
            # No sharing: an existing SQLite handle prevents this probe. Never write the file.
            handle = kernel.CreateFileW(str(file), 0x80000000, 0, None, 3, 0x80, None)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error in (32, 33):
                    raise DatabaseBusyError('Stop the collector before restoring or maintaining this database.')
                raise RecoveryError('Could not verify that the database is closed.')
            kernel.CloseHandle(handle)
        return
    if sys.platform.startswith('linux') and Path('/proc').is_dir():
        identities = {(file.stat().st_dev, file.stat().st_ino) for file in files}
        for process in Path('/proc').iterdir():
            if not process.name.isdigit():
                continue
            try:
                if process.stat().st_uid != os.getuid():
                    continue
                for descriptor in (process / 'fd').iterdir():
                    try:
                        info = descriptor.stat()
                    except FileNotFoundError:
                        continue
                    if (info.st_dev, info.st_ino) in identities:
                        raise DatabaseBusyError('Stop the collector before restoring or maintaining this database.')
            except FileNotFoundError:
                continue  # The process ended during inspection.
            except PermissionError:
                raise RecoveryError('Could not inspect active database users; stop collectors and retry.') from None
        return
    program = '/usr/sbin/lsof' if sys.platform == 'darwin' else shutil.which('lsof')
    if not program or not Path(program).is_file():
        raise RecoveryError('Cannot verify active database users: lsof is unavailable.')
    try:
        result = subprocess.run([program, '-t', '--', *map(str, files)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        raise RecoveryError('Could not inspect active database users; stop collectors and retry.') from None
    if result.stdout.strip():
        raise DatabaseBusyError('Stop the collector before restoring or maintaining this database.')
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise RecoveryError('Could not verify that the database is closed.')


class DatabaseLease:
    """One collector or maintenance owner per database; the lock file is never unlinked."""

    def __init__(self, database, *, check_legacy=False):
        self.path = _path(database)
        self.check_legacy = check_legacy
        self.handle = None

    @property
    def active(self):
        return self.handle is not None

    def __enter__(self):
        if self.active:
            raise RecoveryError('This database lease is already held.')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = Path(str(self.path) + '.lock')
        if lock.is_symlink():
            raise RecoveryError('The database lock must not be a symbolic link.')
        if lock.exists() and not lock.is_file():
            raise RecoveryError('The database lock must be a regular file.')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(lock, flags, 0o600)
        self.handle = os.fdopen(descriptor, 'r+b', buffering=0)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RecoveryError('The database lock must be a regular file.')
            if os.name == 'nt':
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    self.handle.write(b'0')
                self.handle.seek(0)
                try:
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise DatabaseBusyError('This database is already in use by a collector or maintenance command.') from None
            else:
                import fcntl
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise DatabaseBusyError('This database is already in use by a collector or maintenance command.') from None
            if self.check_legacy:
                _check_legacy_users(self.path)
        except BaseException:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *args):
        if self.handle:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            self.handle.close()
            self.handle = None


def _sqlite_snapshot(source, target):
    deadline = time.monotonic() + 60
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise RecoveryError('The database snapshot timed out; retry when collection is idle.')
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=2)) as live:
        live.execute('PRAGMA trusted_schema=OFF')
        with closing(sqlite3.connect(target, timeout=2)) as destination:
            live.backup(destination, pages=256, progress=progress, sleep=.05)
            destination.execute('PRAGMA trusted_schema=OFF')
            if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RecoveryError('The backup database failed its integrity check.')
            destination.execute('PRAGMA journal_mode=DELETE')


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _unique_name(prefix, at):
    return f'{prefix}-{at.strftime("%Y%m%dT%H%M%S%fZ")}-{secrets.token_hex(3)}'


def _snapshot_file(source, folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.is_symlink():
        raise RecoveryError('The backup destination must not be a symbolic link.')
    with tempfile.TemporaryDirectory(prefix='.local-signal-backup-', dir=folder) as temporary:
        snapshot, compressed = Path(temporary) / 'snapshot.sqlite3', Path(temporary) / 'snapshot.gz'
        _sqlite_snapshot(source, snapshot)
        with snapshot.open('rb') as raw, gzip.open(compressed, 'wb', compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed, 1 << 20)
        compressed.chmod(0o600)
        with compressed.open('rb') as handle:
            os.fsync(handle.fileno())
        compressed.replace(target)
        _sync_directory(folder)
    return target


def backup_database(source, folder, at=None, *, manual=False):
    """Create a consistent snapshot; manual copies are unique and never daily-rotated."""
    source, folder = _path(source, required=True), Path(folder).expanduser().resolve()
    _database_files(source)
    at = at or datetime.now(timezone.utc)
    stamp = at.strftime('%Y-%m-%d')
    name = (_unique_name('manual', at) if manual else f'hearth-{stamp}') + '.sqlite3.gz'
    target = _snapshot_file(source, folder, name)
    if not manual:
        # Keep seven daily copies, counting older uncompressed backups by date too.
        for superseded in [folder / f'hearth-{stamp}.sqlite3', *folder.glob(f'hearth-{stamp}.sqlite3-*')]:
            superseded.unlink(missing_ok=True)
        prefix = len('hearth-0000-00-00')
        dates = {file.name[:prefix] for pattern in ('hearth-????-??-??.sqlite3', 'hearth-????-??-??.sqlite3.gz')
                 for file in folder.glob(pattern)}
        keep = set(sorted(dates)[-7:])
        for old in folder.glob('hearth-????-??-??.sqlite3*'):
            if old.name[:prefix] not in keep:
                old.unlink(missing_ok=True)
    return {'at': at.isoformat(timespec='seconds'), 'file': target.name, 'path': str(target),
            'kind': 'manual' if manual else 'daily', 'copies_kept': None if manual else 7}


_CORE_COLUMNS = {
    'devices': {'ip', 'name', 'room', 'notes', 'pinned', 'expected_sleep', 'created_at', 'last_seen', 'last_telemetry', 'payload'},
    'samples': {'id', 'ip', 'at', 'metric', 'value', 'unit', 'label'},
    'events': {'id', 'at', 'ip', 'type', 'message'},
    'settings': {'key', 'value'},
}
_OPTIONAL_COLUMNS = {
    'text_samples': {'id', 'ip', 'at', 'metric', 'value', 'unit', 'label'},
    'energy_readings': {'source_id', 'at', 'counter_kwh', 'uptime_s'},
    'energy_reading_channels': {'source_id', 'at', 'channels'},
    'firmware_alerts': {'device_id', 'channel', 'version'},
    'alerts': {'id', 'rule', 'device_id', 'subject', 'severity', 'summary', 'opened_at', 'updated_at', 'resolved_at', 'acknowledged_at'},
    'alert_deliveries': {'event_id', 'alert_key', 'payload', 'destination_key', 'created_at', 'status', 'attempts',
                         'next_attempt_at', 'last_attempt_at', 'last_error', 'sent_at'},
}


def _validate_snapshot(path):
    """Read only physical app tables with trusted schema disabled; never invoke backup triggers."""
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA trusted_schema=OFF')
        objects = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if any(kind == 'view' or (kind == 'table' and 'CREATE VIRTUAL TABLE' in (sql or '').upper()) for kind, _, sql in objects):
            raise RecoveryError('Backups containing views or virtual tables are not supported.')
        tables = {name for kind, name, _ in objects if kind == 'table'}
        if not set(_CORE_COLUMNS).issubset(tables):
            raise RecoveryError('This is not a Local Signal database: required app tables are missing.')
        columns = {}
        for name, required in {**_CORE_COLUMNS, **_OPTIONAL_COLUMNS}.items():
            if name not in tables:
                continue
            fields = db.execute(f'PRAGMA table_xinfo("{name}")').fetchall()
            columns[name] = {row[1] for row in fields}
            if not required.issubset(columns[name]) or any(row[6] for row in fields):
                raise RecoveryError(f'The {name} table has an unsupported schema.')
            expected_key = {'devices': ('device_id',) if 'device_id' in columns[name] else ('ip',),
                            'settings': ('key',), 'energy_readings': ('source_id', 'at'),
                            'energy_reading_channels': ('source_id', 'at'),
                            'firmware_alerts': ('device_id', 'channel', 'version'),
                            'alert_deliveries': ('event_id',)}.get(name, ('id',))
            actual_key = tuple(row[1] for row in sorted(fields, key=lambda row: row[5]) if row[5])
            if actual_key != expected_key:
                raise RecoveryError(f'The {name} table has an unsupported primary key.')
        if 'device_id' in columns['devices'] and not {'hardware_id', 'address_checked_at', 'name_mode'}.issubset(columns['devices']):
            raise RecoveryError('The devices table has an incomplete identity schema.')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RecoveryError('The backup database failed its integrity check.')
        for payload, in db.execute('SELECT payload FROM devices'):
            if not isinstance(json.loads(payload), dict):
                raise RecoveryError('The backup contains invalid device data.')
        if 'monitor_config' in columns['devices']:
            for value, in db.execute('SELECT monitor_config FROM devices'):
                config = json.loads(value)
                if not isinstance(config, dict) or (config and config.get('protocol') not in ('bacnet-ip', 'modbus-tcp', 'ipp', 'nut-ups')):
                    raise RecoveryError('The backup contains invalid monitoring settings.')
        for key, value in db.execute('SELECT key,value FROM settings'):
            if key not in ('last_scan', 'last_name_discovery'):
                json.loads(value)
        counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                  for name in ('devices', 'samples', 'text_samples', 'events', 'alerts') if name in tables}
        newest = db.execute('SELECT MAX(at) FROM samples').fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM alert_deliveries WHERE status NOT IN ('sent','failed','cancelled')").fetchone()[0] if 'alert_deliveries' in tables else 0
        return {'valid': True, 'schema': 'inventory-identity' if 'device_id' in columns['devices'] else 'legacy-ip',
                'counts': counts, 'latest_sample_at': newest, 'pending_deliveries': pending,
                'database_bytes': path.stat().st_size}


def _stage_backup(source, staged):
    with source.open('rb') as handle:
        compressed = handle.read(2) == b'\x1f\x8b'
    if compressed:
        total = 0
        with gzip.open(source, 'rb') as incoming, staged.open('wb') as output:
            while chunk := incoming.read(1 << 20):
                total += len(chunk)
                if total > 16 * 1024 ** 3:
                    raise RecoveryError('The expanded backup exceeds the 16 GiB restore limit.')
                output.write(chunk)
    else:
        _database_files(source)
        _sqlite_snapshot(source, staged)  # Include committed WAL data in a consistent read snapshot.
    staged.chmod(0o600)
    return {**_validate_snapshot(staged), 'format': 'gzip' if compressed else 'sqlite'}


def inspect_backup(backup):
    """Validate a gzip/plain SQLite backup and return counts, never inventory or credentials."""
    source = _path(backup, required=True)
    try:
        with tempfile.TemporaryDirectory(prefix='local-signal-inspect-') as folder:
            return {**_stage_backup(source, Path(folder) / 'inspect.sqlite3'), 'backup': str(source)}
    except RecoveryError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError, EOFError):
        raise RecoveryError('The backup could not be read or does not contain a valid Local Signal database.') from None


def _raw_rollback(database, folder, at):
    target = folder / _unique_name('rollback', at)
    target.mkdir(mode=0o700, parents=True)
    for source in _database_files(database):
        destination = target / source.name
        with source.open('rb') as incoming, destination.open('xb') as output:
            shutil.copyfileobj(incoming, output, 1 << 20)
            output.flush()
            os.fsync(output.fileno())
        destination.chmod(0o600)
    _sync_directory(target)
    _sync_directory(folder)
    return target


def restore_database(backup, database, *, lease=None):
    """Stage and validate first, preserve rollback data, then replace a stopped collector's DB."""
    source, target = _path(backup, required=True), _path(database)
    if source == target or (target.exists() and os.path.samefile(source, target)):
        raise RecoveryError('Choose a backup separate from the destination database.')
    if lease is not None and (not isinstance(lease, DatabaseLease) or not lease.active or lease.path != target):
        raise RecoveryError('Restore requires the destination database lease.')
    owner = nullcontext(lease) if lease is not None else DatabaseLease(target, check_legacy=True)
    try:
        with owner:
            _check_legacy_users(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.local-signal-restore-', dir=target.parent) as folder:
                staged = Path(folder) / 'restore.sqlite3'
                metadata = _stage_backup(source, staged)
                at = datetime.now(timezone.utc)
                with closing(sqlite3.connect(staged)) as db:
                    db.execute('PRAGMA trusted_schema=OFF')
                    # The app recreates its identity triggers on startup. Do not execute any
                    # trigger supplied by a backup while cancelling pending deliveries.
                    triggers = db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
                    for name, in triggers:
                        db.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
                    cancelled = 0
                    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alert_deliveries'").fetchone():
                        cancelled = db.execute("""UPDATE alert_deliveries SET status='cancelled',
                            last_error='Cancelled during database restore; historical notifications are not replayed.'
                            WHERE status NOT IN ('sent','failed','cancelled')""").rowcount
                    db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
                               ('last_restore', json.dumps({'at': at.isoformat(timespec='seconds'), 'source': source.name})))
                    db.commit()
                    db.execute('PRAGMA journal_mode=DELETE')
                _validate_snapshot(staged)
                with staged.open('rb') as handle:
                    os.fsync(handle.fileno())
                _check_legacy_users(target)
                existing = _database_files(target)
                rollback_backup, rollback_directory = None, None
                if existing:
                    backups = target.parent / 'backups'
                    backups.mkdir(parents=True, exist_ok=True)
                    try:
                        if not target.is_file():
                            raise RecoveryError('Only orphaned SQLite sidecars remain.')
                        rollback_backup = _snapshot_file(target, backups, _unique_name('rollback', at) + '.sqlite3.gz')
                    except (sqlite3.Error, RecoveryError):
                        _check_legacy_users(target)
                        rollback_directory = _raw_rollback(target, backups, at)
                    _check_legacy_users(target)
                    if rollback_backup:
                        # Flush old WAL into the old main file so interruption before replacement
                        # still leaves a complete original database. All handles were checked closed.
                        with closing(sqlite3.connect(target, timeout=0)) as current:
                            result = current.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                            if result[0]:
                                raise DatabaseBusyError('The original database became busy; restore was cancelled.')
                    for sidecar in _database_files(target):
                        if sidecar != target:
                            sidecar.unlink()
                staged.replace(target)
                _sync_directory(target.parent)
                return {**metadata, 'restored_at': at.isoformat(timespec='seconds'), 'database': str(target),
                        'backup': str(source), 'cancelled_deliveries': cancelled,
                        'rollback_backup': str(rollback_backup) if rollback_backup else None,
                        'rollback_directory': str(rollback_directory) if rollback_directory else None}
    except RecoveryError:
        raise
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError, EOFError):
        raise RecoveryError('Restore could not finish safely. The backup and any rollback copy have been preserved.') from None
