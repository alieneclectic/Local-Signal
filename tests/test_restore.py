from contextlib import closing
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import reliability
import server


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.sqlite3'
        self.target = self.root / 'target.sqlite3'
        self.create_database(self.source, 'Saved device')

    def tearDown(self):
        self.temp.cleanup()

    def create_database(self, path, name):
        store = server.Store(path)
        try:
            store.add_device('10.23.0.40', name, room='Test room', pinned=True)
            store.save_setting('retention_days', 14)
            store.record('10.23.0.40', {'status': 'online', 'reachable': True,
                                      'checked_at': '2026-11-15T12:00:00+00:00',
                                      'metrics': {'power_w': {'value': 10, 'unit': 'W', 'label': 'Power'}}})
        finally:
            store.db.close()

    def backup(self):
        result = reliability.backup_database(self.source, self.root / 'backups', manual=True)
        return Path(result['path'])

    def name(self, path):
        with closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT name FROM devices').fetchone()[0]

    def test_manual_copies_are_unique_and_survive_daily_rotation(self):
        at = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
        first = reliability.backup_database(self.source, self.root / 'backups', at, manual=True)
        second = reliability.backup_database(self.source, self.root / 'backups', at, manual=True)
        self.assertNotEqual(first['path'], second['path'])
        for day in range(1, 10):
            reliability.backup_database(self.source, self.root / 'backups', datetime(2026, 12, day, tzinfo=timezone.utc))
        self.assertTrue(Path(first['path']).is_file())
        self.assertTrue(Path(second['path']).is_file())
        self.assertEqual(len(list((self.root / 'backups').glob('hearth-*.sqlite3.gz'))), 7)
        self.assertEqual(reliability.inspect_backup(first['path'])['counts']['devices'], 1)

    def test_plain_inspection_includes_committed_wal_without_touching_source(self):
        with closing(sqlite3.connect(self.source)) as writer:
            writer.execute('PRAGMA journal_mode=WAL')
            writer.execute("UPDATE devices SET name='Committed in WAL'")
            writer.execute("INSERT INTO samples(ip,at,metric,value,unit,label,device_id) VALUES('10.23.0.40','2026-11-15T12:01:00+00:00','power_w',20,'W','Power',1)")
            writer.commit()
            result = reliability.inspect_backup(self.source)
            self.assertEqual(result['counts']['samples'], 2)
            self.assertEqual(result['format'], 'sqlite')
            self.assertEqual(writer.execute('SELECT name FROM devices').fetchone()[0], 'Committed in WAL')

    def test_gzip_restore_preserves_original_and_does_not_touch_credentials(self):
        self.create_database(self.target, 'Original device')
        credentials = Path(str(self.target) + '.alert-webhook.json')
        credentials.write_text('private companion unchanged')
        backup = self.backup()
        preview = reliability.inspect_backup(backup)
        self.assertTrue(preview['valid'])
        self.assertEqual(self.name(self.target), 'Original device')
        result = reliability.restore_database(backup, self.target)
        self.assertEqual(self.name(self.target), 'Saved device')
        self.assertEqual(credentials.read_text(), 'private companion unchanged')
        self.assertEqual(result['counts']['devices'], 1)
        self.assertIsNone(result['rollback_directory'])
        original = self.root / 'original.sqlite3'
        original.write_bytes(gzip.decompress(Path(result['rollback_backup']).read_bytes()))
        self.assertEqual(self.name(original), 'Original device')
        with closing(sqlite3.connect(self.target)) as db:
            self.assertEqual(json.loads(db.execute("SELECT value FROM settings WHERE key='retention_days'").fetchone()[0]), 14)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_restore_cancels_pending_retry_and_sending_without_executing_triggers(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            for status in ('pending', 'retry', 'sending', 'sent', 'failed', 'cancelled'):
                db.execute('INSERT INTO alert_deliveries(event_id,alert_key,payload,destination_key,created_at,status,next_attempt_at) VALUES(?,?,?,?,?,?,?)',
                           (status, status, '{}', 'destination', '2026-11-15T12:00:00+00:00', status, '2026-11-15T12:00:00+00:00'))
            db.execute('CREATE TRIGGER unsafe_notification AFTER UPDATE ON alert_deliveries BEGIN DELETE FROM devices; END')
        result = reliability.restore_database(self.backup(), self.target)
        self.assertEqual(result['cancelled_deliveries'], 3)
        self.assertEqual(self.name(self.target), 'Saved device')
        with closing(sqlite3.connect(self.target)) as db:
            states = dict(db.execute('SELECT event_id,status FROM alert_deliveries'))
            self.assertEqual({states[key] for key in ('pending', 'retry', 'sending')}, {'cancelled'})
            self.assertEqual(states['sent'], 'sent')
            self.assertEqual(states['failed'], 'failed')
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger'").fetchone())
        reopened = server.Store(self.target)
        try:
            self.assertTrue(reopened.db.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='samples_legacy_device_id'").fetchone())
        finally:
            reopened.db.close()

    def test_invalid_or_foreign_backup_leaves_target_and_sidecars_untouched(self):
        self.create_database(self.target, 'Original device')
        original = self.target.read_bytes()
        invalid = self.root / 'invalid.gz'
        for contents in (b'not sqlite', gzip.compress(b'not sqlite'), b'\x1f\x8btruncated'):
            invalid.write_bytes(contents)
            with self.assertRaises(reliability.RecoveryError):
                reliability.restore_database(invalid, self.target)
            self.assertEqual(self.target.read_bytes(), original)
        foreign = self.root / 'foreign.sqlite3'
        with closing(sqlite3.connect(foreign)) as db:
            db.execute('CREATE TABLE unrelated(value)')
        with self.assertRaisesRegex(reliability.RecoveryError, 'Local Signal'):
            reliability.restore_database(foreign, self.target)
        self.assertEqual(self.target.read_bytes(), original)

    def test_views_and_malformed_json_are_rejected_before_replacement(self):
        self.create_database(self.target, 'Original device')
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('CREATE VIEW unexpected AS SELECT * FROM devices')
        with self.assertRaisesRegex(reliability.RecoveryError, 'views'):
            reliability.restore_database(self.source, self.target)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('DROP VIEW unexpected')
            db.execute("UPDATE devices SET payload='not json'")
        with self.assertRaises(reliability.RecoveryError):
            reliability.restore_database(self.source, self.target)
        self.assertEqual(self.name(self.target), 'Original device')

    def test_corrupt_original_is_preserved_byte_for_byte_with_sidecars(self):
        self.target.write_bytes(b'broken database original bytes')
        journal = Path(str(self.target) + '-journal')
        journal.write_bytes(b'original journal evidence')
        result = reliability.restore_database(self.backup(), self.target)
        self.assertIsNone(result['rollback_backup'])
        rollback = Path(result['rollback_directory'])
        self.assertEqual((rollback / self.target.name).read_bytes(), b'broken database original bytes')
        self.assertEqual((rollback / journal.name).read_bytes(), b'original journal evidence')
        self.assertFalse(journal.exists())
        self.assertEqual(self.name(self.target), 'Saved device')

    def test_committed_wal_original_survives_in_rollback_and_stale_sidecars_are_removed(self):
        # Abrupt process exit leaves committed WAL present without any live SQLite handles.
        script = "import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1]); d.execute('PRAGMA journal_mode=WAL'); d.execute(\"UPDATE devices SET name='Original WAL value'\"); d.commit(); os._exit(0)"
        self.create_database(self.target, 'Original device')
        subprocess.run([sys.executable, '-c', script, str(self.target)], check=True)
        self.assertTrue(Path(str(self.target) + '-wal').exists())
        result = reliability.restore_database(self.backup(), self.target)
        original = self.root / 'original-wal.sqlite3'
        original.write_bytes(gzip.decompress(Path(result['rollback_backup']).read_bytes()))
        self.assertEqual(self.name(original), 'Original WAL value')
        self.assertFalse(Path(str(self.target) + '-wal').exists())
        self.assertFalse(Path(str(self.target) + '-shm').exists())
        self.assertEqual(self.name(self.target), 'Saved device')

    def test_existing_legacy_schema_can_be_inspected_and_migrated_after_restore(self):
        legacy = self.root / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as db, db:
            db.executescript('''CREATE TABLE devices(ip TEXT PRIMARY KEY,name TEXT,room TEXT,notes TEXT,pinned INTEGER,expected_sleep INTEGER,
                created_at TEXT,last_seen TEXT,last_telemetry TEXT,payload TEXT);
                CREATE TABLE samples(id INTEGER PRIMARY KEY,ip TEXT,at TEXT,metric TEXT,value REAL,unit TEXT,label TEXT);
                CREATE TABLE events(id INTEGER PRIMARY KEY,at TEXT,ip TEXT,type TEXT,message TEXT);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);''')
            db.execute("INSERT INTO devices VALUES('10.23.0.50','Legacy','Office','',1,0,'2026-11-15',NULL,NULL,'{}')")
        self.assertEqual(reliability.inspect_backup(legacy)['schema'], 'legacy-ip')
        reliability.restore_database(legacy, self.target)
        migrated = server.Store(self.target)
        try:
            self.assertEqual(migrated.devices()[0]['name'], 'Legacy')
        finally:
            migrated.db.close()

    def test_symlinks_same_file_and_wrong_lease_are_rejected(self):
        linked = self.root / 'linked.sqlite3'
        linked.symlink_to(self.source)
        with self.assertRaises(reliability.RecoveryError):
            reliability.inspect_backup(linked)
        with self.assertRaises(reliability.RecoveryError):
            reliability.restore_database(self.source, self.source)
        with self.assertRaises(reliability.RecoveryError):
            reliability.restore_database(self.source, linked)
        with reliability.DatabaseLease(self.root / 'another.sqlite3') as lease:
            with self.assertRaises(reliability.RecoveryError):
                reliability.restore_database(self.source, self.target, lease=lease)

    def test_matching_held_lease_supports_restore_and_blocks_competing_owner(self):
        with reliability.DatabaseLease(self.target) as lease:
            with self.assertRaises(reliability.DatabaseBusyError):
                with reliability.DatabaseLease(self.target):
                    pass
            result = reliability.restore_database(self.source, self.target, lease=lease)
            self.assertEqual(result['counts']['devices'], 1)
        with reliability.DatabaseLease(self.target, check_legacy=True):
            pass

    def test_live_legacy_sqlite_connection_refuses_restore_before_target_changes(self):
        self.create_database(self.target, 'Original device')
        with closing(sqlite3.connect(self.target)) as legacy:
            legacy.execute('SELECT COUNT(*) FROM devices').fetchone()
            with self.assertRaises(reliability.DatabaseBusyError):
                reliability.restore_database(self.source, self.target)
            self.assertEqual(legacy.execute('SELECT name FROM devices').fetchone()[0], 'Original device')

    def test_failed_replace_keeps_complete_original_and_rollback(self):
        self.create_database(self.target, 'Original device')
        original_replace = Path.replace
        def replace(path, target):
            if path.name == 'restore.sqlite3':
                raise OSError('synthetic replace failure')
            return original_replace(path, target)
        with patch.object(Path, 'replace', replace), self.assertRaises(reliability.RecoveryError):
            reliability.restore_database(self.source, self.target)
        self.assertEqual(self.name(self.target), 'Original device')
        self.assertEqual(len(list((self.root / 'backups').glob('rollback-*.sqlite3.gz'))), 1)


if __name__ == '__main__':
    unittest.main()
