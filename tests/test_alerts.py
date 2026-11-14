from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import alerts
import background
import server

AT = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
DEFAULTS = alerts.settings_from(None)


def ago(minutes):
    return (AT - timedelta(minutes=minutes)).isoformat(timespec='seconds')


def device(device_id=1, **values):
    return {'device_id': device_id, 'ip': f'10.23.0.{device_id + 10}', 'name': f'Device {device_id}', 'room': 'Office',
            'pinned': True, 'expected_sleep': False, 'status': 'online', 'last_seen': AT.isoformat(),
            'metrics': {}, 'latest_metrics': {}, **values}


def ups(device_id=1, state='On line', charge=100, status='online'):
    metrics = {}
    if state is not None:
        metrics['ups_state'] = {'value': state, 'unit': '', 'label': 'UPS status'}
    if charge is not None:
        metrics['ups_battery_pct'] = {'value': charge, 'unit': '%', 'label': 'Battery charge'}
    return device(device_id, protocol='nut-ups', status=status, metrics=metrics if status == 'online' else {})


class Ledger:
    """An in-memory alerts table with the production schema."""

    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(alerts.SCHEMA)

    def run(self, devices, settings=DEFAULTS, at=AT):
        evaluation = alerts.evaluate(devices, settings, at, alerts.open_keys(self.db))
        return alerts.sync(self.db, evaluation, devices, at.isoformat(timespec='seconds'))

    def open(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM alerts WHERE resolved_at IS NULL ORDER BY id')]


class RuleTests(unittest.TestCase):
    def test_pinned_device_offline_after_threshold_opens_once_and_resolves_when_reachable(self):
        ledger = Ledger()
        self.assertEqual(ledger.run([device(status='unreachable', last_seen=ago(5))]), [])
        notes = ledger.run([device(status='unreachable', last_seen=ago(11))])
        self.assertEqual([(n['event'], n['rule'], n['text']) for n in notes],
                         [('opened', 'device_offline', 'Device 1: Has not responded for at least 10 minutes.')])
        self.assertEqual(ledger.run([device(status='unreachable', last_seen=ago(40))]), [])
        self.assertEqual(len(ledger.open()), 1)
        notes = ledger.run([device(status='reachable')])
        self.assertEqual([(n['event'], n['text']) for n in notes], [('resolved', 'Device 1 is responding again.')])
        self.assertEqual(ledger.open(), [])

    def test_offline_ignores_unpinned_sleeping_and_never_seen_devices(self):
        ledger = Ledger()
        devices = [device(1, pinned=False, status='unreachable', last_seen=ago(60)),
                   device(2, expected_sleep=True, status='unreachable', last_seen=ago(60)),
                   device(3, status='unreachable', last_seen=None),
                   device(4, status='pending', last_seen=ago(60))]
        self.assertEqual(ledger.run(devices), [])

    def test_device_without_a_current_address_counts_as_offline(self):
        notes = Ledger().run([device(ip=None, status='reachable', last_seen=ago(30))])
        self.assertEqual([n['rule'] for n in notes], ['device_offline'])

    def test_unpinning_or_disabling_a_rule_resolves_quietly(self):
        ledger = Ledger()
        ledger.run([device(status='unreachable', last_seen=ago(30))])
        self.assertEqual(ledger.run([device(pinned=False, status='unreachable', last_seen=ago(30))]), [])
        self.assertEqual(ledger.open(), [])
        ledger.run([device(status='unreachable', last_seen=ago(30))])
        disabled = alerts.validate_settings({'device_offline': {'enabled': False}}, DEFAULTS)
        self.assertEqual(ledger.run([device(status='unreachable', last_seen=ago(30))], disabled), [])
        self.assertEqual(ledger.open(), [])

    def test_raising_the_threshold_keeps_an_open_offline_alert(self):
        ledger = Ledger()
        ledger.run([device(status='unreachable', last_seen=ago(15))])
        longer = alerts.validate_settings({'device_offline': {'minutes': 60}}, DEFAULTS)
        self.assertEqual(ledger.run([device(status='unreachable', last_seen=ago(16))], longer), [])
        self.assertEqual(len(ledger.open()), 1)

    def test_removed_device_resolves_quietly(self):
        ledger = Ledger()
        ledger.run([device(status='unreachable', last_seen=ago(30))])
        self.assertEqual(ledger.run([]), [])
        self.assertEqual(ledger.open(), [])

    def test_ups_on_battery_low_charge_and_hysteresis(self):
        ledger = Ledger()
        notes = ledger.run([ups(state='On battery · Discharging', charge=48)])
        self.assertEqual(sorted(n['rule'] for n in notes), ['ups_battery_low', 'ups_on_battery'])
        self.assertEqual({n['severity'] for n in notes}, {'critical'})
        # Charging back to just above the threshold stays open until it clears the hysteresis band.
        notes = ledger.run([ups(state='On line · Charging', charge=52)])
        self.assertEqual([(n['event'], n['rule']) for n in notes], [('resolved', 'ups_on_battery')])
        self.assertEqual(ledger.open()[0]['summary'], 'Battery charge is 52%.')
        notes = ledger.run([ups(state='On line', charge=55)])
        self.assertEqual([(n['event'], n['rule']) for n in notes], [('resolved', 'ups_battery_low')])

    def test_ups_low_battery_flag_without_charge_and_replace_battery(self):
        notes = Ledger().run([ups(state='On line · Low battery · Replace battery', charge=None)])
        by_rule = {n['rule']: n for n in notes}
        self.assertEqual(set(by_rule), {'ups_battery_low', 'ups_replace_battery'})
        self.assertEqual(by_rule['ups_battery_low']['summary'], 'The UPS reports a low battery.')
        self.assertEqual(by_rule['ups_replace_battery']['severity'], 'warning')

    def test_unreachable_ups_keeps_alerts_open_instead_of_clearing(self):
        ledger = Ledger()
        ledger.run([ups(state='On battery', charge=30)])
        self.assertEqual(ledger.run([ups(status='unreachable')]), [])
        self.assertEqual(len(ledger.open()), 2)
        # A fresh reading without the charge field still knows the battery flags.
        notes = ledger.run([ups(state='On line', charge=None)])
        self.assertEqual(sorted(n['rule'] for n in notes), ['ups_battery_low', 'ups_on_battery'])

    def test_printer_supplies_alert_per_supply_and_missing_supply_is_unknown(self):
        def printer(**supplies):
            metrics = {'ipp_state': {'value': 'Idle', 'unit': '', 'label': 'Printer state'}}
            for key, (label, value) in supplies.items():
                metrics[key] = {'value': value, 'unit': '%', 'label': label}
            return device(protocol='ipp', metrics=metrics)
        black, cyan = 'ipp_supply_0_0123abcd_pct', 'ipp_supply_1_89abcdef_pct'
        ledger = Ledger()
        notes = ledger.run([printer(**{black: ('Black toner', 8), cyan: ('Cyan toner', 60)})])
        self.assertEqual([(n['rule'], n['summary']) for n in notes], [('printer_supply_low', 'Black toner is at 8%.')])
        self.assertEqual(notes[0]['severity'], 'info')
        self.assertEqual(ledger.run([printer(**{cyan: ('Cyan toner', 60)})]), [])
        self.assertEqual(len(ledger.open()), 1)
        notes = ledger.run([printer(**{black: ('Black toner', 100)})])
        self.assertEqual([n['event'] for n in notes], ['resolved'])

    def test_sensor_battery_uses_recent_last_known_reading(self):
        def sensor(value, minutes):
            return device(status='unreachable', expected_sleep=True, last_seen=ago(minutes),
                          latest_metrics={'battery_pct': {'value': value, 'unit': '%', 'label': 'Battery',
                                                          'reported_at': ago(minutes)}})
        ledger = Ledger()
        self.assertEqual(ledger.run([sensor(12, 3 * 24 * 60)]), [])  # Too old to judge.
        notes = ledger.run([sensor(12, 60)])
        self.assertEqual([(n['rule'], n['summary']) for n in notes], [('sensor_battery_low', 'Battery is at 12%.')])
        self.assertEqual(ledger.run([sensor(23, 30)]), [])  # Inside the hysteresis band.
        self.assertEqual([n['event'] for n in ledger.run([sensor(100, 10)])], ['resolved'])

    def test_changed_summary_updates_without_notifying_again(self):
        ledger = Ledger()
        ledger.run([ups(state='On line', charge=40)])
        self.assertEqual(ledger.run([ups(state='On line', charge=35)]), [])
        self.assertEqual(ledger.open()[0]['summary'], 'Battery charge is 35%.')

    def test_settings_validation_is_strict(self):
        for bad in ({}, {'unknown': True}, {'device_offline': {'minutes': 1}}, {'device_offline': {'minutes': True}},
                    {'ups_battery': {'pct': 100}}, {'ups_power': {'pct': 10}}, {'notify_resolved': 'yes'},
                    {'printer_supply': {'enabled': 1}}, {'sensor_battery': {}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                alerts.validate_settings(bad, DEFAULTS)
        updated = alerts.validate_settings({'ups_battery': {'pct': 30}, 'notify_resolved': False}, DEFAULTS)
        self.assertEqual((updated['ups_battery'], updated['notify_resolved']), ({'enabled': True, 'pct': 30}, False))
        self.assertEqual(alerts.settings_from({'device_offline': {'minutes': -5, 'enabled': 'no'}}), DEFAULTS)


class Capture(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.received.append((self.path, dict(self.headers), body))
        status = self.server.reply
        self.send_response(status)
        if status == 302:
            self.send_header('Location', 'http://127.0.0.1:1/elsewhere')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def log_message(self, *args):
        pass


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Capture)
        self.httpd.received, self.httpd.reply = [], 200
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}/alerts'
        self.note = Ledger().run([ups(state='On battery', charge=90)])[0]

    def test_url_validation(self):
        for url in ('https://ntfy.sh/topic', 'http://192.168.1.20:8123/api/webhook/x', 'http://localhost:8080/x'):
            self.assertEqual(alerts.validate_url(url), url)
        for url in ('http://ntfy.sh/topic', 'http://8.8.8.8/x', 'ftp://192.168.1.2/x', 'https://user:pw@example.com/x',
                    'https://example.com/a b', 'not a url', 'https://', None, 'https://example.com:0/x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                alerts.validate_url(url)

    def test_ntfy_delivery_sends_text_title_and_priority(self):
        alerts.send({'url': self.url, 'format': 'ntfy'}, {**self.note, 'title': 'Café UPS'})
        path, headers, body = self.httpd.received[0]
        self.assertEqual(path, '/alerts')
        self.assertEqual(body.decode(), 'Device 1: Running on battery power.')
        self.assertEqual((headers['Priority'], headers['Tags']), ('4', 'rotating_light'))
        self.assertTrue(headers['Title'].startswith('=?UTF-8?B?'))

    def test_json_delivery_excludes_network_identifiers(self):
        alerts.send({'url': self.url, 'format': 'json'}, self.note)
        payload = json.loads(self.httpd.received[0][2])
        self.assertEqual((payload['event'], payload['rule'], payload['device']), ('opened', 'ups_on_battery',
                                                                                  {'name': 'Device 1', 'room': 'Office'}))
        self.assertNotIn('10.23.0', json.dumps(payload))

    def test_errors_and_redirects_are_reported_not_followed(self):
        self.httpd.reply = 302
        with self.assertRaisesRegex(alerts.WebhookError, 'redirects are not followed'):
            alerts.send({'url': self.url, 'format': 'ntfy'}, self.note)
        self.assertEqual(len(self.httpd.received), 1)
        self.httpd.reply = 500
        with self.assertRaisesRegex(alerts.WebhookError, 'HTTP 500'):
            alerts.send({'url': self.url, 'format': 'json'}, self.note)

    def test_saved_webhook_is_private_and_removable(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'alerts.sqlite3'
            self.assertIsNone(alerts.load_webhook(database))
            alerts.save_webhook(database, self.url, 'json')
            path = alerts.webhook_path(database)
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o077, 0)
            self.assertEqual(alerts.load_webhook(database), {'url': self.url, 'format': 'json'})
            notifier = alerts.Notifier(database)
            self.assertTrue(notifier.deliver(self.note))
            self.assertIsNotNone(notifier.snapshot()['last_sent_at'])
            if os.name == 'posix':
                path.chmod(0o644)
                with self.assertRaises(alerts.WebhookError):
                    alerts.load_webhook(database)
            alerts.save_webhook(database, None, None)
            self.assertFalse(path.exists())
            self.assertFalse(notifier.deliver(self.note))

    def test_alerts_module_is_packaged_for_the_background_collector(self):
        self.assertIn('alerts.py', background.FILES)
        pyproject = (Path(server.__file__).parent / 'pyproject.toml').read_text()
        self.assertIn('"alerts.py"', pyproject)


class StoreAlertTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = server.Store(Path(self.folder.name) / 'alerts.sqlite3')
        self.addCleanup(self.store.db.close)
        self.store.add_device('10.23.0.50', 'Rack UPS', pinned=True, monitor_config={'protocol': 'nut-ups', 'ups_name': 'rack'})
        self.device_id = self.store.devices()[0]['device_id']

    def record(self, state):
        self.store.record('10.23.0.50', {'status': 'online', 'reachable': True, 'checked_at': server.now(),
                                          'metrics': {'ups_state': {'value': state, 'unit': '', 'label': 'UPS status'}}})

    def test_collection_opens_alert_logs_event_and_acknowledges(self):
        self.record('On battery')
        notes = self.store.update_alerts()
        self.assertEqual([n['rule'] for n in notes], ['ups_on_battery'])
        self.assertEqual(self.store.events(kind='alert_opened')[0]['message'], 'Rack UPS: Running on battery power.')
        listed = self.store.alerts()
        self.assertEqual([(a['device_name'], a['title']) for a in listed['open']], [('Rack UPS', 'UPS on battery')])
        alert_id = listed['open'][0]['id']
        self.assertTrue(self.store.acknowledge_alert(alert_id))
        self.assertFalse(self.store.acknowledge_alert(alert_id))
        self.record('On line')
        self.store.alert_settings({'notify_resolved': False})
        self.assertEqual(self.store.update_alerts(), [])  # Resolved quietly per the setting, but logged.
        self.assertEqual(len(self.store.events(kind='alert_resolved')), 1)
        self.assertEqual([a['id'] for a in self.store.alerts()['recent']], [alert_id])
        self.assertFalse(self.store.acknowledge_alert(alert_id))

    def test_existing_database_gains_the_alerts_table_and_keeps_settings(self):
        self.store.save_setting('retention_days', 9)
        self.store.db.execute('DROP TABLE alerts')
        self.store.db.commit()
        reopened = server.Store(self.store.path)
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.retention_days(), 9)
        self.assertEqual(reopened.alerts(), {'open': [], 'recent': []})
        self.assertEqual(reopened.alert_settings(), DEFAULTS)

    def test_cleanup_removes_only_old_resolved_alerts(self):
        old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec='seconds')
        self.store.db.execute('''INSERT INTO alerts(rule,device_id,subject,severity,summary,opened_at,updated_at,resolved_at)
                                 VALUES('device_offline',?,'','warning','x',?,?,?)''', (self.device_id, old, old, old))
        self.store.db.execute('''INSERT INTO alerts(rule,device_id,subject,severity,summary,opened_at,updated_at)
                                 VALUES('ups_on_battery',?,'','critical','y',?,?)''', (self.device_id, old, old))
        self.store.db.commit()
        self.store.cleanup()
        self.assertEqual([r[0] for r in self.store.db.execute('SELECT rule FROM alerts')], ['ups_on_battery'])

    def test_collector_waits_for_a_completed_cycle_and_survives_alert_errors(self):
        with patch.object(server, 'network_info', return_value={}):
            collector = server.Collector(self.store)
        self.record('On battery')
        collector.evaluate_alerts()
        self.assertEqual(collector.notifier.queue.qsize(), 0)
        collector.has_completed_cycle = True
        with patch.object(self.store, 'update_alerts', side_effect=sqlite3.OperationalError('locked')):
            collector.evaluate_alerts()
        self.assertEqual(collector.state['alert_error'], 'locked')
        collector.evaluate_alerts()
        self.assertEqual((collector.notifier.queue.qsize(), collector.state['alert_error']), (1, None))


class AlertAPITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = server.Store(Path(cls.tmp.name) / 'api.sqlite3')
        with patch.object(server, 'network_info', return_value={'local_ip': '10.23.0.2', 'subnet': '10.23.0.0/24'}):
            cls.collector = server.Collector(cls.store)
        cls.httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.httpd.store, cls.httpd.collector = cls.store, cls.collector
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.db.close()
        cls.tmp.cleanup()

    def post(self, path, payload):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('POST', path, body=json.dumps(payload),
                     headers={'Content-Type': 'application/json', 'X-Hearth-Request': '1'})
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result

    def state(self):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        conn.request('GET', '/api/state')
        result = json.loads(conn.getresponse().read())
        conn.close()
        return result

    def test_state_and_settings(self):
        state = self.state()['alerts']
        self.assertEqual((state['open'], state['settings']), ([], DEFAULTS))
        self.assertEqual((state['webhook']['configured'], state['webhook']['editable']), (False, True))
        status, body = self.post('/api/alerts/settings', {'device_offline': {'enabled': True, 'minutes': 30}})
        self.assertEqual((status, body['alerts']['settings']['device_offline']['minutes']), (200, 30))
        self.assertEqual(self.post('/api/alerts/settings', {'device_offline': {'minutes': 0}})[0], 400)
        self.assertEqual(self.post('/api/alerts/999/acknowledge', {})[0], 404)

    def test_webhook_url_is_never_returned_and_edits_require_loopback(self):
        status, body = self.post('/api/alerts/webhook/test', {})
        self.assertEqual((status, body['error']), (400, 'Save a webhook before sending a test.'))
        secret = 'https://ntfy.sh/private-topic-123'
        status, body = self.post('/api/alerts/webhook', {'url': secret, 'format': 'ntfy'})
        self.assertEqual((status, body['alerts']['webhook']['destination']), (200, 'https://ntfy.sh'))
        self.assertNotIn('private-topic', json.dumps(self.state()))
        self.assertEqual(self.post('/api/alerts/webhook', {'url': 'http://ntfy.sh/x', 'format': 'ntfy'})[0], 400)
        with patch.object(server.Handler, 'loopback_client', return_value=False):
            self.assertEqual(self.post('/api/alerts/webhook', {'url': None})[0], 403)
            self.assertEqual(self.post('/api/alerts/webhook/test', {})[0], 403)
            self.assertFalse(self.state()['alerts']['webhook']['editable'])
        status, body = self.post('/api/alerts/webhook', {'url': None})
        self.assertEqual((status, body['alerts']['webhook']['configured']), (200, False))


if __name__ == '__main__':
    unittest.main()
