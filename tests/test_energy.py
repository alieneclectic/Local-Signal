from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import energy
import server

MAC = 'AABBCCDDEEFF'
DEVICE = {'mac': 'AA:BB:CC:DD:EE:FF', 'name': 'Heater', 'room': 'First floor',
          'metrics': {'energy_kwh': {'value': 10}}, 'ip': '10.23.1.45'}
SETTINGS = {'rate_per_kwh': .25, 'timezone': 'America/New_York', 'meter_macs': [MAC]}


def reading(at, counter, uptime=1000, mac=MAC):
    return {'source_id': mac, 'at': at, 'counter_kwh': counter, 'uptime_s': uptime}


class EnergyTests(unittest.TestCase):
    def summarize(self, readings, days=1, at='2026-10-27T16:00:00+00:00', settings=None):
        return energy.summarize(readings, [DEVICE], settings or SETTINGS, days, datetime.fromisoformat(at))

    def test_first_counter_is_baseline_not_consumption(self):
        result = self.summarize([reading('2026-10-27T15:00:00+00:00', 500)])
        self.assertIsNone(result['total']['kwh'])
        self.assertIsNone(result['total']['cost'])
        self.assertEqual(result['total']['coverage_pct'], 0)

    def test_observed_delta_cost_and_partial_coverage(self):
        result = self.summarize([reading('2026-10-27T15:00:00+00:00', 500),
                                 reading('2026-10-27T15:01:00+00:00', 500.01, 1060)])
        self.assertEqual(result['total']['kwh'], .01)
        self.assertEqual(result['total']['cost'], .0025)
        self.assertLess(result['total']['coverage_pct'], 1)
        self.assertEqual(result['daily'][-1]['kwh'], .01)

    def test_zero_usage_is_distinct_from_no_measurements(self):
        result = self.summarize([reading('2026-10-27T15:00:00+00:00', 5),
                                 reading('2026-10-27T15:01:00+00:00', 5, 1060)])
        self.assertEqual(result['total']['kwh'], 0)
        self.assertEqual(result['total']['cost'], 0)

    def test_counter_reset_reboot_and_long_gap_are_excluded(self):
        rows = [reading('2026-10-27T15:00:00+00:00', 5, 1000),
                reading('2026-10-27T15:01:00+00:00', 0, 1060),
                reading('2026-10-27T15:02:00+00:00', .1, 20),
                reading('2026-10-27T15:20:00+00:00', .9, 1100),
                reading('2026-10-27T15:21:00+00:00', 1, 1160)]
        result = self.summarize(rows)
        self.assertEqual(result['total']['kwh'], .1)
        self.assertEqual(result['sources'][0]['resets'], 2)
        self.assertEqual(result['sources'][0]['gaps'], 1)
        self.assertAlmostEqual(result['total']['observed_hours'], .017)

    def test_local_midnight_split_preserves_total_energy(self):
        rows = [reading('2026-10-27T03:59:30+00:00', 1), reading('2026-10-27T04:00:30+00:00', 1.02, 1060)]
        result = self.summarize(rows, days=7)
        self.assertEqual(result['total']['kwh'], .02)
        self.assertEqual(result['daily'][-2]['kwh'], .01)
        self.assertEqual(result['daily'][-1]['kwh'], .01)
        today = self.summarize(rows)
        self.assertEqual(today['total']['kwh'], .01)

    def test_day_lengths_follow_spring_and_fall_dst(self):
        for date, hours in [('2026-03-09T04:00:00+00:00', 23), ('2026-11-02T05:00:00+00:00', 25)]:
            at, _, edges = energy.window(7, 'America/New_York', datetime.fromisoformat(date))
            self.assertEqual((edges[-2] - edges[-3]).total_seconds() / 3600, hours)

    def test_unconfigured_and_free_rates(self):
        rows = [reading('2026-10-27T15:00:00+00:00', 0), reading('2026-10-27T15:01:00+00:00', 1)]
        self.assertIsNone(self.summarize(rows, settings={**SETTINGS, 'rate_per_kwh': None})['total']['cost'])
        self.assertEqual(self.summarize(rows, settings={**SETTINGS, 'rate_per_kwh': 0})['total']['cost'], 0)

    def test_missing_meter_lowers_combined_coverage(self):
        rows = [reading('2026-10-27T04:00:00+00:00', 0), reading('2026-10-27T04:01:00+00:00', .01, 1060)]
        single = self.summarize(rows, at='2026-10-27T04:01:00+00:00')
        missing = self.summarize(rows, at='2026-10-27T04:01:00+00:00', settings={**SETTINGS, 'meter_macs': [MAC, '112233445566']})
        self.assertLess(missing['total']['coverage_pct'], single['total']['coverage_pct'])
        self.assertIsNone(missing['sources'][1]['kwh'])

    def test_invalid_settings_and_ranges(self):
        for key, value in [('rate_per_kwh', float('nan')), ('rate_per_kwh', True), ('rate_per_kwh', -1),
                           ('rate_per_kwh', 11), ('timezone', 'bad/timezone'), ('meter_macs', ['unknown'])]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                energy.validate_settings({**SETTINGS, key: value}, [DEVICE])
        with self.assertRaises(ValueError):
            self.summarize([], days=31)


class EnergyStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name) / 'test.sqlite3')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def record(self, ip, at, counter, mac=DEVICE['mac']):
        self.store.record(ip, {'status': 'online', 'reachable': True, 'checked_at': at, 'mac': mac,
                              'metrics': {'energy_kwh': {'value': counter, 'unit': 'kWh', 'label': 'Energy'}}})

    def test_hardware_identity_survives_ip_change_without_cross_device_usage(self):
        self.record('10.23.1.45', '2026-10-27T15:00:00+00:00', 10)
        self.store.add_device('10.23.1.99', 'Heater at new IP')
        self.record('10.23.1.99', '2026-10-27T15:01:00+00:00', 10.1)
        self.record('10.23.1.45', '2026-10-27T15:01:00+00:00', 999, '11:22:33:44:55:66')
        rows = [dict(r) for r in self.store.db.execute('SELECT * FROM energy_readings ORDER BY source_id,at')]
        result = energy.summarize(rows, self.store.devices(), SETTINGS, 1, datetime.fromisoformat('2026-10-27T16:00:00+00:00'))
        self.assertEqual(result['total']['kwh'], .1)
        self.assertEqual(result['sources'][0]['ip'], '10.23.1.99')

    def test_settings_persist_and_changing_rate_clears_old_bill_note(self):
        self.record('10.23.1.45', server.now(), 5)
        self.store.heating_settings(SETTINGS)
        self.store.save_setting('heating_rate_note', 'Old bill rate')
        self.store.heating_settings({**SETTINGS, 'rate_per_kwh': .3})
        self.assertIsNone(self.store.setting('heating_rate_note'))
        self.store.db.close()
        self.store = server.Store(Path(self.tmp.name) / 'test.sqlite3')
        self.assertEqual(self.store.heating_settings()['rate_per_kwh'], .3)

    def test_one_time_import_baselines_existing_samples_without_duplicate_history(self):
        self.record('10.23.1.45', server.now(), 5)
        self.store.db.execute('DELETE FROM energy_readings')
        self.store.db.execute("DELETE FROM settings WHERE key='energy_migrated'")
        self.store.db.commit()
        self.store.db.close()
        self.store = server.Store(Path(self.tmp.name) / 'test.sqlite3')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM energy_readings').fetchone()[0], 1)
        self.assertIsNone(self.store.energy_summary()['total']['kwh'])


class EnergyAPITests(unittest.TestCase):
    def test_energy_api_export_and_same_origin_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / 'test.sqlite3')
            with patch.object(server, 'network_info', return_value={'local_ip': '10.23.1.59'}):
                collector = server.Collector(store)
            httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store, httpd.collector = store, collector
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            def request(path, method='GET', body=None, headers=None):
                conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port)
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                result = response.status, response.read()
                conn.close()
                return result
            try:
                self.assertEqual(request('/api/energy?days=7')[0], 200)
                for days in ['0', '31', 'NaN', '1.5']:
                    self.assertEqual(request('/api/energy?days=' + days)[0], 400)
                self.assertIn(b'observed_kwh', request('/api/energy.csv?days=1')[1])
                self.assertEqual(request('/api/settings/heating', 'POST', '{}')[0], 400)
                headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
                self.assertEqual(request('/api/settings/heating', 'POST', json.dumps({**SETTINGS, 'meter_macs': []}), headers)[0], 200)
                self.assertEqual(request('/heating.css')[0], 200)
            finally:
                httpd.shutdown(); httpd.server_close(); store.db.close()
