"""Bounded local logging and consistent, rotating SQLite snapshots."""
from datetime import datetime, timezone
from contextlib import closing
import gzip
import logging
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


def backup_database(source, folder, at=None):
    """Publish a complete, gzip-compressed backup atomically; keep the latest seven daily copies."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = (at or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
    target = folder / f'hearth-{stamp}.sqlite3.gz'
    temporary = folder / f'.hearth-{stamp}.tmp'
    compressed = folder / f'.hearth-{stamp}.gz.tmp'
    try:
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(temporary)) as destination:
                live.backup(destination)
                if destination.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise sqlite3.DatabaseError('Backup integrity check failed.')
                # backup() carries over the live WAL mode; a restored copy should be one self-contained file.
                destination.execute('PRAGMA journal_mode=DELETE')
        with temporary.open('rb') as raw, gzip.open(compressed, 'wb', compresslevel=6) as packed:
            shutil.copyfileobj(raw, packed, 1 << 20)
        compressed.chmod(0o600)
        compressed.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        compressed.unlink(missing_ok=True)
    # An uncompressed copy from an earlier version for the same day is now redundant.
    for superseded in [folder / f'hearth-{stamp}.sqlite3', *folder.glob(f'hearth-{stamp}.sqlite3-*')]:
        superseded.unlink(missing_ok=True)
    # Prune by date, counting compressed and older uncompressed backups alike, so a backup's
    # -wal/-shm files go with it and a stray sidecar cannot displace a real backup.
    prefix = len('hearth-0000-00-00')
    dates = {file.name[:prefix] for pattern in ('hearth-????-??-??.sqlite3', 'hearth-????-??-??.sqlite3.gz')
             for file in folder.glob(pattern)}
    keep = set(sorted(dates)[-7:])
    for old in folder.glob('hearth-????-??-??.sqlite3*'):
        if old.name[:prefix] not in keep:
            old.unlink(missing_ok=True)
    return {'at': (at or datetime.now(timezone.utc)).isoformat(timespec='seconds'), 'file': target.name, 'copies_kept': 7}
