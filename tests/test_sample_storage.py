from contextlib import closing
import gzip
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import reliability
import server

BASE = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)
HEARTBEAT = server.SAMPLE_HEARTBEAT_SECONDS


def at(seconds):
    return (BASE + timedelta(seconds=seconds)).isoformat(timespec='seconds')


def reading(key, value, unit='%', label='Reading'):
    return {key: {'value': value, 'unit': unit, 'label': label}}


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'samples.sqlite3'
        self.store = server.Store(self.path)
        self.ip = '10.23.2.10'
        self.store.add_device(self.ip, 'Speaker')
        self.device_id = self.store.devices()[0]['device_id']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def record(self, seconds, **metrics):
        self.store.record(self.ip, {'status': 'online', 'reachable': True, 'checked_at': at(seconds),
                                    'metrics': {key: reading(key, value)[key] for key, value in metrics.items()}})

    def rows(self, metric):
        return [(r['at'], r['value']) for r in self.store.history(hours=24 * 365, ip=self.ip) if r['metric'] == metric]


class HeldMetricTests(unittest.TestCase):
    def test_only_slow_changing_readings_are_held(self):
        for key in ('muted', 'volume_pct', 'rssi_dbm', 'battery_pct', 'relay_0', 'relay_3', 'headphones_connected'):
            self.assertTrue(server.held_metric(key), key)
        for key in ('latency_ms', 'power_w', 'uptime_s', 'energy_kwh', 'temperature_c', 'humidity_pct'):
            self.assertFalse(server.held_metric(key), key)


class IngestTests(StoreCase):
    def test_repeats_are_skipped_changes_are_kept_and_the_heartbeat_repeats_once(self):
        for n in range(0, HEARTBEAT * 2 + 1, 30):
            self.record(n, volume_pct=20)
        self.record(HEARTBEAT * 2 + 30, volume_pct=35)
        stamps = [stamp for stamp, _ in self.rows('volume_pct')]
        self.assertEqual(stamps, [at(0), at(HEARTBEAT), at(HEARTBEAT * 2), at(HEARTBEAT * 2 + 30)])
        self.assertEqual([value for _, value in self.rows('volume_pct')], [20, 20, 20, 35])

    def test_continuous_readings_are_never_deduplicated(self):
        for n in range(5):
            self.record(n * 30, latency_ms=12.5, power_w=0)
        self.assertEqual(len(self.rows('latency_ms')), 5)
        self.assertEqual(len(self.rows('power_w')), 5)

    def test_cache_is_per_device_and_per_metric(self):
        other = '10.23.2.11'
        self.store.add_device(other, 'Other')
        self.record(0, volume_pct=20, rssi_dbm=-60)
        self.store.record(other, {'status': 'online', 'reachable': True, 'checked_at': at(30), 'metrics': reading('volume_pct', 20)})
        self.record(30, volume_pct=20, rssi_dbm=-61)
        by_ip = {}
        for row in self.store.history(hours=24 * 365):
            by_ip.setdefault((row['ip'], row['metric']), []).append(row['value'])
        self.assertEqual(by_ip[(self.ip, 'volume_pct')], [20])
        self.assertEqual(by_ip[(other, 'volume_pct')], [20])
        self.assertEqual(by_ip[(self.ip, 'rssi_dbm')], [-60, -61])

    def test_restart_remembers_recent_values_but_not_stale_ones(self):
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(seconds=60)).isoformat(timespec='seconds')
        self.store.record(self.ip, {'status': 'online', 'reachable': True, 'checked_at': recent, 'metrics': reading('volume_pct', 20)})
        self.store.db.close()
        self.store = server.Store(self.path)
        self.store.record(self.ip, {'status': 'online', 'reachable': True, 'checked_at': now.isoformat(timespec='seconds'),
                                    'metrics': reading('volume_pct', 20)})
        self.assertEqual(len(self.rows('volume_pct')), 1)
        later = (now + timedelta(seconds=HEARTBEAT)).isoformat(timespec='seconds')
        self.store.record(self.ip, {'status': 'online', 'reachable': True, 'checked_at': later, 'metrics': reading('volume_pct', 20)})
        self.assertEqual(len(self.rows('volume_pct')), 2)

    def test_a_failed_transaction_does_not_suppress_the_next_reading(self):
        with patch.object(server.energy, 'identity', side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):
                self.record(0, volume_pct=20)
        self.assertEqual(self.rows('volume_pct'), [])
        self.record(30, volume_pct=20)
        self.assertEqual(len(self.rows('volume_pct')), 1)


class CompactionTests(StoreCase):
    def seed(self, key, values):
        """Insert one row every 30 seconds, as the collector did before deduplication."""
        with self.store.lock:
            self.store.db.executemany('INSERT INTO samples(ip,at,metric,value,unit,label,device_id) VALUES(?,?,?,?,?,?,?)',
                                      [(self.ip, at(n * 30), key, value, '', key, self.device_id) for n, value in enumerate(values)])
            self.store.db.commit()

    @staticmethod
    def held_value_at(rows, stamp):
        """Value a sample-and-hold reader sees at a stamp: the latest stored row at or before it."""
        known = [value for when, value in rows if when <= stamp]
        return known[-1] if known else None

    def test_compaction_is_lossless_and_keeps_the_chart_continuous(self):
        values = [1] * 200 + [0] * 7 + [1] * 120 + [0] * 3 + [1] * 90
        self.seed('muted', values)
        self.seed('latency_ms', [12.5] * len(values))
        original = self.rows('muted')
        removed, remaining = self.store.compact_history()
        kept = self.rows('muted')
        self.assertGreater(removed, 0)
        self.assertLess(len(kept), len(original) / 5)
        for stamp, value in original:
            self.assertEqual(self.held_value_at(kept, stamp), value, stamp)
        gaps = [(datetime.fromisoformat(b[0]) - datetime.fromisoformat(a[0])).total_seconds() for a, b in zip(kept, kept[1:])]
        self.assertLessEqual(max(gaps), HEARTBEAT * 1.5)

    def test_compaction_leaves_continuous_metrics_and_other_devices_alone(self):
        self.seed('latency_ms', [12.5] * 100)
        self.seed('volume_pct', [20] * 100)
        removed, _ = self.store.compact_history()
        self.assertEqual(len(self.rows('latency_ms')), 100)
        self.assertGreater(removed, 0)
        self.assertGreaterEqual(len(self.rows('volume_pct')), 2)

    def test_compaction_matches_relay_prefix_exactly(self):
        self.seed('relay_0', [1] * 100)
        self.seed('relayed_value', [1] * 100)
        self.store.compact_history()
        self.assertLess(len(self.rows('relay_0')), 100)
        self.assertEqual(len(self.rows('relayed_value')), 100)

    def test_compaction_shrinks_the_database_file(self):
        self.seed('muted', [1] * 20000)
        # Measure the main file alone: a WAL checkpoint shrinks the -wal file but never the database.
        self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = self.path.stat().st_size
        self.store.compact_history()
        self.assertLess(self.path.stat().st_size, before / 2)


class BackupFileTests(unittest.TestCase):
    def test_backups_are_single_files_and_pruning_removes_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'live.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE history (value)')
                db.commit()
                backups = Path(folder) / 'backups'
                backups.mkdir()
                for suffix in ('-wal', '-shm'):  # Orphans left by an older version.
                    (backups / f'hearth-2026-09-01.sqlite3{suffix}').write_bytes(b'x')
                for day in range(9):
                    reliability.backup_database(source, backups, datetime(2026, 10, 1 + day, tzinfo=timezone.utc))
            names = sorted(path.name for path in backups.iterdir())
            self.assertEqual(names, [f'hearth-2026-10-{day:02d}.sqlite3.gz' for day in range(3, 10)])
            restored_path = Path(folder) / 'restored.sqlite3'
            restored_path.write_bytes(gzip.decompress((backups / names[-1]).read_bytes()))
            with closing(sqlite3.connect(restored_path)) as restored:
                self.assertEqual(restored.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                self.assertEqual(restored.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_a_stray_sidecar_does_not_displace_a_real_backup(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'live.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE history (value)')
                db.commit()
            backups = Path(folder) / 'backups'
            backups.mkdir()
            (backups / 'hearth-2026-10-20.sqlite3-wal').write_bytes(b'x')
            for day in range(7):
                reliability.backup_database(source, backups, datetime(2026, 10, 1 + day, tzinfo=timezone.utc))
            self.assertEqual(len(list(backups.glob('hearth-????-??-??.sqlite3.gz'))), 7)
            self.assertFalse(list(backups.glob('*-wal')))

    def test_older_uncompressed_backups_count_toward_seven_and_age_out(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'live.sqlite3'
            with closing(sqlite3.connect(source)) as db:
                db.execute('CREATE TABLE history (value)')
                db.execute('INSERT INTO history VALUES (7)')
                db.commit()
            backups = Path(folder) / 'backups'
            backups.mkdir()
            for day in range(1, 8):  # A full week of uncompressed copies from the previous version.
                (backups / f'hearth-2026-10-{day:02d}.sqlite3').write_bytes(b'old')
            (backups / 'hearth-2026-10-07.sqlite3-wal').write_bytes(b'old')
            result = reliability.backup_database(source, backups, datetime(2026, 10, 7, tzinfo=timezone.utc))
            names = sorted(path.name for path in backups.iterdir())
            # Today's compressed copy replaces today's uncompressed one and its sidecar; the rest are kept.
            self.assertEqual(names, [f'hearth-2026-10-{day:02d}.sqlite3' for day in range(1, 7)] + ['hearth-2026-10-07.sqlite3.gz'])
            self.assertEqual(result['file'], 'hearth-2026-10-07.sqlite3.gz')
            reliability.backup_database(source, backups, datetime(2026, 10, 8, tzinfo=timezone.utc))
            self.assertNotIn('hearth-2026-10-01.sqlite3', [path.name for path in backups.iterdir()])
            self.assertEqual(len(list(backups.iterdir())), 7)

    def test_compressed_backup_is_much_smaller_than_the_database(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / 'live.sqlite3')
            with store.lock:
                store.db.executemany('INSERT INTO samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                                     [('10.0.0.5', at(n * 30), 'latency_ms', n % 50, 'ms', 'Network latency') for n in range(20000)])
                store.db.commit()
            store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            result = reliability.backup_database(store.path, Path(folder) / 'backups')
            store.db.close()
            packed = Path(folder) / 'backups' / result['file']
            self.assertLess(packed.stat().st_size, store.path.stat().st_size / 3)


class HistoryAPITests(unittest.TestCase):
    def test_history_reports_held_metrics_for_the_chart(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / 'api.sqlite3')
            ip = '10.23.2.20'
            store.add_device(ip, 'Speaker')
            stamp = server.now()
            store.record(ip, {'status': 'online', 'reachable': True, 'checked_at': stamp,
                              'metrics': {**reading('volume_pct', 20), **reading('latency_ms', 4, 'ms')}})
            with patch.object(server, 'network_info', return_value={'local_ip': '10.23.2.2', 'subnet': '10.23.2.0/24'}):
                collector = server.Collector(store)
            httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store, httpd.collector = store, collector
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=3)
                conn.request('GET', '/api/history?hours=24')
                body = json.loads(conn.getresponse().read())
                conn.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                store.db.close()
            self.assertEqual(body['held'], ['volume_pct'])
            self.assertEqual(body['heartbeat_seconds'], HEARTBEAT)



def ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec='seconds')


class RetentionTests(StoreCase):
    def seed_ages(self, *days):
        with self.store.lock:
            for age in days:
                self.store.db.execute('INSERT INTO samples(ip,at,metric,value,unit,label,device_id) VALUES(?,?,?,?,?,?,?)',
                                      (self.ip, ago(age), 'latency_ms', age, 'ms', 'Network latency', self.device_id))
                self.store.db.execute('INSERT INTO energy_readings VALUES(?,?,?,?)', ('AABBCCDDEEFF', ago(age), age, None))
            self.store.db.commit()

    def test_defaults_to_thirty_days_and_persists_a_shorter_choice(self):
        self.assertEqual(self.store.retention_days(), 30)
        self.assertEqual(self.store.set_retention_days(7), 30)
        self.store.db.close()
        self.store = server.Store(self.path)
        self.assertEqual(self.store.retention_days(), 7)

    def test_only_whole_days_from_one_to_thirty_are_accepted(self):
        for bad in (0, 31, -1, True, 7.0, '7', None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                self.store.set_retention_days(bad)
        self.assertEqual(self.store.retention_days(), 30)
        for good in (1, 30):
            self.store.set_retention_days(good)
            self.assertEqual(self.store.retention_days(), good)

    def test_cleanup_keeps_only_the_chosen_number_of_days(self):
        self.seed_ages(1, 6, 8, 20, 29)
        self.store.cleanup()
        self.assertEqual(len(self.store.history(hours=24 * 30, ip=self.ip)), 5)
        self.store.set_retention_days(7)
        self.store.cleanup()
        self.assertEqual(sorted(r['value'] for r in self.store.history(hours=24 * 30, ip=self.ip)), [1, 6])
        energy = [r[0] for r in self.store.db.execute('SELECT counter_kwh FROM energy_readings ORDER BY counter_kwh')]
        self.assertEqual(energy, [1, 6])

    def test_a_corrupt_saved_value_falls_back_to_thirty_days(self):
        self.store.save_setting('retention_days', 900)
        self.assertEqual(self.store.retention_days(), 30)

    def test_shortening_history_returns_space_to_the_disk(self):
        with self.store.lock:
            self.store.db.executemany('INSERT INTO samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                                      [(self.ip, ago(20), 'latency_ms', n, 'ms', 'Network latency') for n in range(20000)])
            self.store.db.commit()
        self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = self.path.stat().st_size
        self.store.set_retention_days(7)
        self.store.cleanup()
        self.store.reclaim_space()
        self.assertLess(self.path.stat().st_size, before / 2)


class RetentionAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name) / 'api.sqlite3')
        with patch.object(server, 'network_info', return_value={'local_ip': '10.23.2.2', 'subnet': '10.23.2.0/24'}):
            self.collector = server.Collector(self.store)
        self.httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        self.httpd.store, self.httpd.collector = self.store, self.collector
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.store.db.close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.httpd.server_address[1], timeout=3)
        conn.request(method, path, body=body, headers=headers if headers is not None else
                     {'Content-Type': 'application/json', 'X-Hearth-Request': '1'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def test_setting_is_saved_and_reported_to_the_dashboard(self):
        self.assertEqual(self.request('GET', '/api/state')[1]['collector']['retention_days'], 30)
        self.assertEqual(self.request('POST', '/api/settings/retention', json.dumps({'days': 14})), (200, {'ok': True}))
        self.assertEqual(self.request('GET', '/api/state')[1]['collector']['retention_days'], 14)

    def test_invalid_requests_are_rejected_and_change_nothing(self):
        for body in ({'days': 0}, {'days': 31}, {'days': '7'}, {'days': 7, 'extra': 1}, {}):
            status, reply = self.request('POST', '/api/settings/retention', json.dumps(body))
            self.assertEqual(status, 400, body)
            self.assertIn('error', reply)
        status, _ = self.request('POST', '/api/settings/retention', json.dumps({'days': 7}),
                                 headers={'Content-Type': 'application/json'})  # Missing the dashboard header.
        self.assertEqual(status, 400)
        self.assertEqual(self.store.retention_days(), 30)

    def test_shortening_history_deletes_older_readings_and_frees_the_space(self):
        ip = '10.23.2.30'
        self.store.add_device(ip, 'Plug')
        device_id = self.store.devices()[0]['device_id']
        with self.store.lock:
            self.store.db.execute('INSERT INTO samples(ip,at,metric,value,unit,label,device_id) VALUES(?,?,?,?,?,?,?)',
                                  (ip, ago(2), 'latency_ms', 2, 'ms', 'Network latency', device_id))
            self.store.db.executemany('INSERT INTO samples(ip,at,metric,value,unit,label,device_id) VALUES(?,?,?,?,?,?,?)',
                                      [(ip, ago(12), 'latency_ms', 12, 'ms', 'Network latency', device_id)] * 20000)
            self.store.db.commit()
        self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        before = self.store.path.stat().st_size
        self.request('POST', '/api/settings/retention', json.dumps({'days': 7}))
        for _ in range(200):  # The deletion and space reclaim run in the background.
            if self.store.path.stat().st_size < before / 2:
                break
            time.sleep(.05)
        self.assertEqual([r['value'] for r in self.store.history(hours=24 * 30, ip=ip)], [2])
        self.assertLess(self.store.path.stat().st_size, before / 2)

if __name__ == '__main__':
    unittest.main()
