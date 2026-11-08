"""Export correctness/concurrency and observable collector capacity boundaries."""
import csv
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import server


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = server.Store(Path(self.folder.name)/'synthetic.sqlite3')
        self.addCleanup(self.store.db.close)
        self.ip = '10.23.0.41'
        self.store.add_device(self.ip, 'Synthetic device')
        self.device_id = self.store.devices()[0]['device_id']

    def test_csv_stream_preserves_text_numeric_escaping_and_device_filters(self):
        stamp = server.now()
        self.store.record(self.ip, {'checked_at': stamp, 'status': 'online', 'reachable': True, 'metrics': {
            'ups_load_pct': {'value': 0, 'unit': '%', 'label': '=formula'},
            'ups_state': {'value': 'On battery, low\nReplace battery', 'unit': '', 'label': 'UPS status'}}})
        self.store.add_device('10.23.0.42', 'Unrelated')
        self.store.record('10.23.0.42', {'checked_at': stamp, 'status': 'online', 'reachable': True,
                          'metrics': {'other': {'value': 99, 'unit': '', 'label': 'Other'}}})
        for filters in ({'ip': self.ip}, {'device_id': self.device_id}):
            body = b''.join(self.store.csv_chunks(**filters))
            self.assertEqual(body.count(b'\xef\xbb\xbf'), 1)
            rows = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
            self.assertEqual({r['metric'] for r in rows}, {'ups_load_pct', 'ups_state'})
            self.assertEqual({r['label'] for r in rows}, {"'=formula", 'UPS status'})
            self.assertIn('On battery, low\nReplace battery', [r['value'] for r in rows])
            self.assertEqual({r['device_id'] for r in rows}, {str(self.device_id)})
        self.assertEqual(len(list(csv.DictReader(io.StringIO(b''.join(self.store.csv_chunks(device_id=9999)).decode('utf-8-sig'))))), 0)

    def test_csv_stream_is_batched_consistent_and_does_not_block_collection(self):
        rows = [(self.device_id, self.ip, server.now(), 'load', n, '%', 'Load') for n in range(600)]
        self.store.db.executemany('INSERT INTO samples(device_id,ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?,?)', rows)
        self.store.db.commit()
        stream = self.store.csv_chunks()
        header, first = next(stream), next(stream)
        self.assertEqual(len(first.splitlines()), 256)
        completed = threading.Event()
        def write():
            self.store.record(self.ip, {'checked_at': server.now(), 'status': 'online', 'reachable': True,
                              'metrics': {'new': {'value': 99, 'unit': '', 'label': 'New reading'}}})
            completed.set()
        worker = threading.Thread(target=write, daemon=True)
        worker.start()
        try:
            self.assertTrue(completed.wait(2), 'An open export must not hold the collector lock or block WAL writes.')
            body = header + first + b''.join(stream)
            result = list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))
            self.assertEqual(len(result), 600)  # One consistent snapshot, without the later write.
            self.assertEqual(self.store.history()[-1]['metric'], 'new')
        finally:
            stream.close()
            worker.join(2)

    def test_closed_partial_export_releases_sqlite_snapshot(self):
        self.store.db.execute('INSERT INTO samples(device_id,ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?,?)',
                              (self.device_id, self.ip, server.now(), 'load', 0, '%', 'Load'))
        self.store.db.commit()
        stream = self.store.csv_chunks()
        next(stream)
        self.store.db.execute('UPDATE samples SET value=1')
        self.store.db.commit()
        stream.close()
        busy, _, _ = self.store.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        self.assertEqual(busy, 0)

    def test_collector_exposes_duration_and_warns_for_completed_or_active_slow_cycle(self):
        with patch.object(server, 'network_info', return_value={}):
            collector = server.Collector(self.store)
            collector.router_monitors = []
            result = {'checked_at': server.now(), 'status': 'unreachable', 'reachable': False, 'metrics': {}}
            with patch.object(server, 'probe_device', return_value=result):
                collector.poll()
            snapshot = collector.snapshot()
            self.assertGreaterEqual(snapshot['last_poll_seconds'], 0)
            self.assertFalse(snapshot['poll_over_budget'])
            collector.state['last_poll_seconds'] = server.POLL_SECONDS + .1
            self.assertTrue(collector.snapshot()['poll_over_budget'])
            collector.state['last_poll_seconds'] = 0
            collector.state['polling'] = True
            collector.poll_started = time.monotonic()-server.POLL_SECONDS-1
            self.assertTrue(collector.snapshot()['poll_over_budget'])
            collector.poll_started = time.monotonic()
            self.assertFalse(collector.snapshot()['poll_over_budget'])


class ChartCapacityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = server.Store(Path(self.folder.name)/'charts.sqlite3')
        self.addCleanup(self.store.db.close)
        self.store.add_device('10.23.0.41', 'Original')
        self.device_id = self.store.devices()[0]['device_id']

    def seed(self, devices=50, metrics=2, cycles=400):
        from datetime import datetime, timedelta, timezone
        base = datetime.now(timezone.utc)-timedelta(seconds=cycles*30)
        self.store.db.executemany('INSERT INTO samples(device_id,ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?,?)',
            ((device+1, f'10.23.0.{device+1}', (base+timedelta(seconds=step*30)).isoformat(),
              f'measure_{metric}', 0 if step == 0 else step, '%', f'Synthetic {metric}')
             for step in range(cycles) for device in range(devices) for metric in range(metrics)))
        self.store.db.commit()

    def test_selected_metric_and_device_filters_keep_historical_metrics_discoverable(self):
        self.seed(devices=2, metrics=3, cycles=4)
        chart = self.store.chart_history(metric='measure_1')
        self.assertEqual({r['metric'] for r in chart['samples']}, {'measure_1'})
        self.assertEqual({r['metric'] for r in chart['metrics']}, {'measure_0', 'measure_1', 'measure_2'})
        self.assertEqual({r['device_id'] for r in chart['samples']}, {1, 2})
        self.assertIn(0, [r['value'] for r in chart['samples']])
        for scope in ({'device_id': self.device_id}, {'ip': '10.23.0.41'}):
            scoped = self.store.chart_history(metric='measure_0', **scope)
            self.assertEqual({r['device_id'] for r in scoped['samples']}, {self.device_id})
            self.assertEqual({r['devices'] for r in scoped['metrics']}, {1})
        empty = self.store.chart_history(metric='no-such-metric')
        self.assertEqual(empty['samples'], [])
        self.assertEqual(len(empty['metrics']), 3)
        self.assertEqual(self.store.devices()[0]['metrics'], {})  # Historical metrics do not require a current reading.

    def test_total_point_budget_preserves_all_series_and_leaves_raw_history_untouched(self):
        self.seed()
        for metric in (None, 'measure_0'):
            chart = self.store.chart_history(metric=metric)
            self.assertLessEqual(len(chart['samples']), server.CHART_MAX_POINTS)
            self.assertFalse(chart['points_limited'])
            expected = {(d, m) for d in range(1, 51) for m in (['measure_0'] if metric else ['measure_0', 'measure_1'])}
            self.assertEqual({(r['device_id'], r['metric']) for r in chart['samples']}, expected)
            self.assertGreater(chart['bucket_seconds'], server.POLL_SECONDS)
        self.assertEqual(len(self.store.history()), 40_000)
        body = b''.join(self.store.csv_chunks())
        self.assertEqual(len(list(csv.DictReader(io.StringIO(body.decode('utf-8-sig'))))), 40_000)

    def test_extreme_series_count_is_explicitly_reported_as_limited(self):
        self.seed(devices=25, metrics=1, cycles=2)
        with patch.object(server, 'CHART_MAX_POINTS', 20):
            chart = self.store.chart_history(metric='measure_0')
        self.assertEqual(len(chart['samples']), 20)
        self.assertTrue(chart['points_limited'])
        self.assertEqual(chart['point_budget'], 20)
        self.assertEqual(len(self.store.history()), 50)

    def test_chart_reads_do_not_wait_for_the_collector_lock(self):
        self.seed(devices=1, metrics=1, cycles=2)
        completed, result = threading.Event(), []
        def read():
            try:
                result.append(self.store.chart_history(metric='measure_0'))
            finally:
                completed.set()
        with self.store.lock:
            worker = threading.Thread(target=read, daemon=True)
            worker.start()
            finished = completed.wait(2)
        worker.join(2)
        self.assertTrue(finished, 'Read-only chart grouping should not hold or wait for the collector lock.')
        self.assertEqual(len(result[0]['samples']), 2)

    def test_catalog_and_points_use_one_consistent_wal_snapshot(self):
        import sqlite3
        stamp = server.now()
        self.store.record('10.23.0.41', {'checked_at': stamp, 'status': 'online', 'reachable': True,
            'metrics': {'power_w': {'value': 0, 'unit': 'W', 'label': 'Power'}}})
        real_connect, wrote = sqlite3.connect, []
        def connect(*args, **kwargs):
            db = real_connect(*args, **kwargs)
            def trace(sql):
                if 'SELECT ip,MIN(at)' in sql and not wrote:
                    self.store.record('10.23.0.41', {'checked_at': stamp, 'status': 'online', 'reachable': True,
                        'metrics': {'power_w': {'value': 100, 'unit': 'W', 'label': 'Power'},
                                    'new': {'value': 1, 'unit': '', 'label': 'New'}}})
                    wrote.append(True)
            db.set_trace_callback(trace)
            return db
        with patch.object(server.sqlite3, 'connect', side_effect=connect):
            chart = self.store.chart_history(metric='power_w')
        self.assertTrue(wrote)
        self.assertEqual([row['value'] for row in chart['samples']], [0])
        self.assertEqual([row['metric'] for row in chart['metrics']], ['power_w'])
        self.assertEqual(len(self.store.history()), 3)

    def test_metric_identifiers_are_bounded_and_parameterized(self):
        self.seed(devices=1, metrics=1, cycles=1)
        for metric in ('', 'x'*201, 'x\n', 1):
            with self.subTest(metric=metric), self.assertRaises(ValueError):
                self.store.chart_history(metric=metric)
        self.assertEqual(self.store.chart_history(metric="x' OR 1=1 --")['samples'], [])
        self.assertEqual(len(self.store.history()), 1)


class ChartHistoryAPITests(unittest.TestCase):
    def test_metric_and_device_filters_catalog_and_invalid_identifiers_over_http(self):
        import http.client
        import json
        from urllib.parse import urlencode
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'network_info', return_value={}):
            store = server.Store(Path(folder)/'api.sqlite3')
            for ip in ('10.23.0.41', '10.23.0.42'):
                store.add_device(ip, 'Fixture '+ip)
                store.record(ip, {'checked_at': server.now(), 'status': 'online', 'reachable': True, 'metrics': {
                    'volume_pct': {'value': 0, 'unit': '%', 'label': 'Volume'},
                    'latency_ms': {'value': 2, 'unit': 'ms', 'label': 'Network latency'}}})
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store, httpd.collector = store, server.Collector(store)
            worker = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
            worker.start()
            def request(query):
                connection = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
                connection.request('GET', '/api/history?'+urlencode(query))
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            try:
                selected = store.devices()[0]['device_id']
                status, data = request({'metric': 'volume_pct', 'device_id': selected})
                self.assertEqual(status, 200)
                self.assertEqual([(r['device_id'], r['metric'], r['value']) for r in data['samples']], [(selected, 'volume_pct', 0)])
                self.assertEqual({r['metric'] for r in data['metrics']}, {'volume_pct', 'latency_ms'})
                self.assertEqual(data['held'], ['volume_pct'])
                self.assertEqual(data['point_budget'], server.CHART_MAX_POINTS)
                for query in ({'metric':'x'*201}, {'metric':'x'+chr(10)}, {'device_id':0}, {'ip':'8.8.8.8'}):
                    self.assertEqual(request(query)[0], 400)
            finally:
                httpd.shutdown()
                httpd.server_close()
                worker.join(2)
                store.db.close()
