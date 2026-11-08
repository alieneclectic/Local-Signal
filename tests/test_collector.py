from contextlib import closing
import csv
from datetime import datetime, timedelta, timezone
import http.client
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import server


ORIGINAL_MAC = '02:00:00:00:00:01'
REPLACEMENT_MAC = '02:00:00:00:00:02'


def observation(mac, power, at):
    return {'status': 'online', 'reachable': True, 'checked_at': at, 'last_telemetry': at, 'mac': mac,
            'metrics': {'power_w': {'value': power, 'unit': 'W', 'label': 'Power'}}}


def write_legacy_samples(db, ip, at, power, app):
    # These are the INSERT statements used by the installed IP-based collector.
    db.execute('INSERT INTO samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
               (ip, at, 'power_w', power, 'W', 'Power'))
    db.execute('INSERT INTO text_samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
               (ip, at, 'active_app', app, '', 'Active app'))


class MetricsTests(unittest.TestCase):
    def test_relay_board_temperature_is_never_room_temperature(self):
        metrics = server.normalize({'type':'SHSW-PM'}, {'tmp':{'tC':27.14,'is_valid':True},
            'meters':[{'power':0,'total':60_000,'is_valid':True}], 'relays':[{'ison':False}], 'uptime':0})
        self.assertNotIn('temperature_c', metrics)
        self.assertEqual(metrics['internal_temperature_c']['value'],27.14)
        self.assertEqual(metrics['power_w']['value'],0)
        self.assertEqual(metrics['energy_kwh']['value'],1)
        self.assertEqual(metrics['relay_0']['value'],0)
        self.assertEqual(metrics['uptime_s']['value'],0)

    def test_sleeping_ht_sensor_format_and_invalid_readings(self):
        valid = server.normalize({'type':'SHHT-1'}, {'tmp':{'value':68,'units':'F','is_valid':True},
            'hum':{'value':45,'is_valid':True}, 'bat':{'value':82}})
        self.assertEqual(valid['temperature_c']['value'],20)
        self.assertEqual(valid['humidity_pct']['value'],45)
        self.assertEqual(valid['battery_pct']['value'],82)
        invalid = server.normalize({'type':'SHHT-1'}, {'tmp':{'value':999,'is_valid':False},
            'hum':{'value':999,'is_valid':False}, 'meters':[{'power':999,'total':60_000,'is_valid':False}]})
        self.assertNotIn('temperature_c',invalid)
        self.assertNotIn('humidity_pct',invalid)
        self.assertNotIn('power_w',invalid)
        self.assertNotIn('energy_kwh',invalid)

    def test_gen2_keeps_sensor_channels_and_converts_wh(self):
        metrics=server.normalize({'gen':3}, {'switch:0':{'apower':125.6,'output':True,'aenergy':{'total':1500},'temperature':{'tC':44}},
            'switch:1':{'apower':20,'output':False,'aenergy':{'total':100}},
            'temperature:0':{'tC':21},'temperature:1':{'tC':25},'humidity:0':{'rh':44},
            'devicepower:0':{'battery':{'percent':95}},'wifi':{'rssi':-60}})
        self.assertEqual(metrics['power_w']['value'],145.6)
        self.assertEqual(metrics['energy_kwh']['value'],1.6)
        self.assertEqual(metrics['temperature:0_c']['value'],21)
        self.assertEqual(metrics['temperature:1_c']['value'],25)
        self.assertEqual(metrics['switch:0_internal_temperature_c']['value'],44)

    def test_non_finite_and_boolean_measurements_are_rejected(self):
        metrics=server.normalize({'type':'SHHT-1'}, {'tmp':{'value':float('nan')},'hum':{'value':True},'bat':{'value':float('inf')}})
        self.assertFalse(metrics)

    def test_probe_uses_only_read_only_endpoints(self):
        device={'ip':'10.23.0.10'}
        with patch.object(server,'ping',return_value=(True,12)), patch.object(server,'port_open',return_value=True), patch.object(server,'read_json',side_effect=[{'type':'SHSW-PM'},{'relays':[{'ison':True}],'meters':[{'power':400}]}]) as read:
            result=server.probe_device(device)
        self.assertEqual(result['status'],'online')
        self.assertEqual([c.args[1] for c in read.call_args_list],['/shelly','/status'])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=server.Store(Path(self.tmp.name)/'test.sqlite3')
        self.ip = '10.23.0.10'
        self.store.add_device(self.ip, 'Test relay')

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_unreachable_preserves_last_telemetry_separately(self):
        ip=self.ip;at=server.now()
        self.store.record(ip,{'status':'online','reachable':True,'checked_at':at,'last_telemetry':at,'metrics':{'power_w':{'value':50,'unit':'W','label':'Power'}}})
        self.store.record(ip,{'status':'unreachable','reachable':False,'checked_at':at,'metrics':{}})
        device=next(d for d in self.store.devices() if d['ip']==ip)
        self.assertFalse(device['metrics'])
        self.assertEqual(device['last_metrics']['power_w']['value'],50)
        self.assertEqual(device['last_telemetry'],at)
        self.assertEqual(device['last_seen'],at)
        self.assertEqual(len(self.store.history(ip=ip)),1)

    def test_labels_persist_and_cannot_mutate_device_hardware(self):
        self.store.patch(self.ip, {'name':'Office power','room':'Office','pinned':True})
        self.store.record(self.ip, {'status':'reachable','reachable':True,'checked_at':server.now(),'metrics':{}})
        self.store.db.close()
        self.store = server.Store(Path(self.tmp.name) / 'test.sqlite3')
        [device] = self.store.devices()
        self.assertEqual((device['ip'], device['name'], device['room']), (self.ip, 'Office power', 'Office'))
        self.assertTrue(device['pinned'])
        with self.assertRaises(ValueError):
            self.store.patch(self.ip, {'relay':True})
        self.assertFalse(self.store.add_device(self.ip, 'Duplicate'))

    def test_relay_transitions_create_observation_events(self):
        ip=self.ip;at=server.now()
        for value in (0,1,1):
            self.store.record(ip,{'status':'online','reachable':True,'checked_at':at,'metrics':{'relay_0':{'value':value,'unit':'','label':'Channel 1 relay'}}})
        changes=[event for event in self.store.events() if event['type']=='relay_changed']
        self.assertEqual(len(changes),1)
        self.assertIn('is now on',changes[0]['message'])

    def test_chart_aggregation_does_not_replace_csv_samples(self):
        ip=self.ip;at=server.now()
        for n in range(4):
            self.store.record(ip,{'status':'online','reachable':True,'checked_at':at,'metrics':{'power_w':{'value':n*10,'unit':'W','label':'Power'}}})
        self.assertEqual(len(self.store.history(ip=ip)),4)
        chart=self.store.history(ip=ip,chart=True)
        self.assertEqual(len(chart),1)
        self.assertEqual(chart[0]['value'],15)

    def test_labels_and_numeric_and_text_history_follow_a_device_to_its_new_ip(self):
        original_id = self.store.devices()[0]['device_id']
        new_ip = '10.23.0.11'
        at = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.store.patch(self.ip, {'name': 'Office power', 'room': 'Office', 'notes': 'Desk',
                                   'pinned': True, 'expected_sleep': True})
        first = observation(ORIGINAL_MAC, 10, at.isoformat())
        first['metrics']['active_app'] = {'value': 'First app', 'unit': '', 'label': 'Active app'}
        self.store.record(self.ip, first)
        self.store.add_device(new_ip)
        # A later failed probe at the old address does not contradict the observed move.
        self.store.record(self.ip, {'status': 'unreachable', 'reachable': False, 'metrics': {},
                                    'checked_at': (at + timedelta(seconds=90)).isoformat()})
        second = observation('020000000001', 20, (at + timedelta(seconds=60)).isoformat())
        second['metrics']['active_app'] = {'value': 'Second app', 'unit': '', 'label': 'Active app'}
        self.store.record(new_ip, second)

        device, = self.store.devices()
        self.assertEqual((device['device_id'], device['ip'], device['name'], device['room'], device['notes']),
                         (original_id, new_ip, 'Office power', 'Office', 'Desk'))
        self.assertTrue(device['pinned'])
        self.assertTrue(device['expected_sleep'])
        history = self.store.history(ip=new_ip)
        self.assertEqual([(row['ip'], row['value']) for row in history if row['metric'] == 'power_w'],
                         [(self.ip, 10), (new_ip, 20)])
        self.assertEqual([row['value'] for row in history if row['metric'] == 'active_app'],
                         ['First app', 'Second app'])
        self.assertEqual({row['device_id'] for row in self.store.history(chart=True)}, {original_id})

    def test_reused_ip_gets_a_new_record_and_keeps_histories_separate(self):
        original_id = self.store.devices()[0]['device_id']
        at = server.now()
        first = observation(ORIGINAL_MAC, 10, at)
        first['metrics']['volume_pct'] = {'value': 30, 'unit': '%', 'label': 'Volume'}
        self.store.record(self.ip, first)
        replacement = observation(REPLACEMENT_MAC, 90, at)
        self.store.record(self.ip, replacement, expected_device_id=original_id)

        original, new = sorted(self.store.devices(), key=lambda d: d['device_id'])
        self.assertEqual((original['device_id'], original['ip'], original['name'], original['status']),
                         (original_id, None, 'Test relay', 'unreachable'))
        self.assertEqual((new['ip'], new['name'], new['room']), (self.ip, 'Device .10', 'Unassigned'))
        self.assertNotIn('volume_pct', new['latest_metrics'])
        self.assertFalse(self.store.record(self.ip, first, expected_device_id=original_id))
        self.assertEqual([row['value'] for row in self.store.history(ip=self.ip)], [90])
        self.assertEqual({row['device_id']: row['value'] for row in self.store.history(chart=True)
                          if row['metric'] == 'power_w'}, {original_id: 10, new['device_id']: 90})

        new_ip = '10.23.0.11'
        self.store.record(new_ip, observation(ORIGINAL_MAC, 20, at))
        self.assertEqual([row['value'] for row in self.store.history(ip=new_ip) if row['metric'] == 'power_w'], [10, 20])

    def test_scan_moves_an_identified_device_and_ignores_its_old_cached_address(self):
        new_ip = '10.23.0.11'
        self.store.record(self.ip, observation(ORIGINAL_MAC, 10, server.now()))
        network = {'local_ip': '10.23.0.9', 'subnet': '10.23.0.8/29'}
        with patch.object(server, 'network_info', return_value=network), \
             patch.object(server, 'ping', side_effect=lambda ip: (ip == new_ip, None)), \
             patch.object(server, 'arp_entries', return_value={self.ip: ORIGINAL_MAC, new_ip: ORIGINAL_MAC}):
            collector = server.Collector(self.store)
            with patch.object(collector, 'start_poll'), patch.object(collector, 'start_naming'):
                for _ in range(2):
                    collector.scan_lock.acquire()
                    collector.scan()
        self.assertIsNone(collector.state['scan_error'])
        device, = self.store.devices()
        self.assertEqual((device['ip'], device['name']), (new_ip, 'Test relay'))
        self.assertEqual([row['value'] for row in self.store.history(ip=new_ip)], [10])

    def test_legacy_history_and_labels_survive_migration_and_ip_reuse(self):
        path = Path(self.tmp.name) / 'legacy.sqlite3'
        at = server.now()
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript('''
                CREATE TABLE devices(ip TEXT PRIMARY KEY, name TEXT NOT NULL, room TEXT DEFAULT 'Unassigned',
                    notes TEXT DEFAULT '', pinned INTEGER DEFAULT 0, expected_sleep INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL, last_seen TEXT, last_telemetry TEXT, payload TEXT DEFAULT '{}');
                CREATE TABLE samples(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL,
                    metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, label TEXT NOT NULL);
                CREATE TABLE text_samples(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL,
                    metric TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, label TEXT NOT NULL);
                CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            ''')
            db.execute('INSERT INTO devices(ip,name,room,created_at,payload) VALUES(?,?,?,?,?)',
                       (self.ip, 'Legacy relay', 'Office', at, json.dumps({'mac': ORIGINAL_MAC})))
            db.execute('INSERT INTO samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                       (self.ip, at, 'power_w', 5, 'W', 'Power'))
            db.execute('INSERT INTO text_samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                       (self.ip, at, 'active_app', 'Legacy app', '', 'Active app'))
            db.execute('INSERT INTO settings VALUES(?,?)', ('retention_days', '7'))
        legacy = server.Store(path)
        with closing(legacy.db):
            original, = legacy.devices()
            self.assertEqual(legacy.retention_days(), 7)
            self.assertEqual((original['name'], original['room']), ('Legacy relay', 'Office'))
            legacy.record(self.ip, observation(REPLACEMENT_MAC, 90, at))
        legacy = server.Store(path)
        with closing(legacy.db):
            self.assertEqual([row['value'] for row in legacy.history(ip=self.ip)], [90])
            self.assertEqual({row['value'] for row in legacy.history(device_id=original['device_id'])}, {5, 'Legacy app'})

    def test_upgrade_fallback_and_return_keep_numeric_and_text_history_linked(self):
        path = Path(self.tmp.name) / 'fallback.sqlite3'
        at = datetime.now(timezone.utc) - timedelta(minutes=4)
        # Create the original schema before either history table had device_id.
        with closing(sqlite3.connect(path)) as db:
            db.executescript('''
                CREATE TABLE devices(ip TEXT PRIMARY KEY, name TEXT NOT NULL, room TEXT DEFAULT 'Unassigned',
                    notes TEXT DEFAULT '', pinned INTEGER DEFAULT 0, expected_sleep INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL, last_seen TEXT, last_telemetry TEXT, payload TEXT DEFAULT '{}');
                CREATE TABLE samples(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL,
                    metric TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, label TEXT NOT NULL);
                CREATE TABLE text_samples(id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL,
                    metric TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, label TEXT NOT NULL);
            ''')
            with db:
                db.execute('INSERT INTO devices(ip,name,room,created_at,payload) VALUES(?,?,?,?,?)',
                           (self.ip, 'Office speaker', 'Office', at.isoformat(), json.dumps({'mac': ORIGINAL_MAC})))
                write_legacy_samples(db, self.ip, at.isoformat(), 10, 'Before upgrade')

        upgraded = server.Store(path)
        with closing(upgraded.db):
            device_id = upgraded.devices()[0]['device_id']
            upgraded.record(self.ip, observation(ORIGINAL_MAC, 20, (at + timedelta(minutes=1)).isoformat()))
            upgraded.save_setting('retention_days', 7)

        # An older fallback opens the migrated database and omits device_id.
        with closing(sqlite3.connect(path)) as db, db:
            write_legacy_samples(db, self.ip, (at + timedelta(minutes=2)).isoformat(), 30, 'During fallback')
            self.assertEqual(db.execute('SELECT device_id FROM samples WHERE value=30').fetchone()[0], device_id)
            self.assertEqual(db.execute("SELECT device_id FROM text_samples WHERE value='During fallback'").fetchone()[0], device_id)

        returned = server.Store(path)
        with closing(returned.db):
            device, = returned.devices()
            self.assertEqual((device['device_id'], device['name'], device['room']), (device_id, 'Office speaker', 'Office'))
            self.assertEqual(returned.retention_days(), 7)
            returned.record('10.23.0.11', observation(ORIGINAL_MAC, 40, (at + timedelta(minutes=3)).isoformat()))
            returned.record(self.ip, observation(REPLACEMENT_MAC, 90, (at + timedelta(minutes=4)).isoformat()))
            rows = returned.history(device_id=device_id)
            self.assertEqual([row['value'] for row in rows if row['metric'] == 'power_w'], [10, 20, 30, 40])
            self.assertEqual([row['value'] for row in rows if row['metric'] == 'active_app'], ['Before upgrade', 'During fallback'])
            self.assertEqual([row['value'] for row in returned.history(device_id=device_id, chart=True)], [10, 20, 30, 40])
            self.assertEqual([row['value'] for row in returned.history(ip=self.ip)], [90])
            self.assertEqual(returned.db.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_unlinked_fallback_history_is_repaired_only_for_unambiguous_addresses(self):
        at = datetime.now(timezone.utc) - timedelta(minutes=4)
        self.store.record(self.ip, observation(ORIGINAL_MAC, 10, at.isoformat()))
        device_id = self.store.devices()[0]['device_id']
        # Reproduce databases written before compatibility triggers were installed.
        with self.store.db:
            for table in ('samples', 'text_samples'):
                self.store.db.execute(f'DROP TRIGGER IF EXISTS {table}_legacy_device_id')
            write_legacy_samples(self.store.db, self.ip, (at + timedelta(seconds=30)).isoformat(), 11, 'Ambiguous')
        second_ip = '10.23.0.11'
        self.store.record(second_ip, observation(ORIGINAL_MAC, 20, (at + timedelta(minutes=1)).isoformat()))
        with self.store.db:
            write_legacy_samples(self.store.db, second_ip, (at + timedelta(seconds=90)).isoformat(), 21, 'Recoverable')
        self.store.record('10.23.0.12', observation(ORIGINAL_MAC, 30, (at + timedelta(minutes=2)).isoformat()))
        self.store.record(self.ip, observation(REPLACEMENT_MAC, 90, (at + timedelta(minutes=3)).isoformat()))
        path = self.store.path
        self.store.db.close()
        self.store = server.Store(path)
        rows = self.store.history(device_id=device_id)
        self.assertEqual([row['value'] for row in rows if row['metric'] == 'power_w'], [10, 20, 21, 30])
        self.assertEqual([row['value'] for row in rows if row['metric'] == 'active_app'], ['Recoverable'])
        self.assertEqual([row['value'] for row in self.store.history(ip=self.ip)], [90])
        self.assertEqual({row['value'] for row in self.store.history() if row['device_id'] is None}, {11, 'Ambiguous'})
        # Pruning older evidence must not make an ambiguous reading look recoverable.
        with self.store.db:
            self.store.db.execute('DELETE FROM samples WHERE ip=? AND device_id=?', (self.ip, device_id))
        self.store.db.close()
        self.store = server.Store(path)
        self.assertEqual([row['value'] for row in self.store.history(ip=self.ip)], [90])
        self.assertEqual({row['value'] for row in self.store.history() if row['device_id'] is None}, {11, 'Ambiguous'})

    def test_fallback_uses_observed_hardware_without_mixing_a_replacement(self):
        at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.store.record(self.ip, observation(ORIGINAL_MAC, 10, at))
        device_id = self.store.devices()[0]['device_id']
        new_ip = '10.23.0.11'
        # Legacy discovery creates another IP-based record for the same hardware.
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('INSERT INTO devices(ip,name,created_at,payload) VALUES(?,?,?,?)',
                       (new_ip, 'Discovered speaker', at, json.dumps({'mac': ORIGINAL_MAC})))
            write_legacy_samples(db, new_ip, at, 20, 'Moved during fallback')
            # Its next probe finds different hardware at the previous address.
            db.execute('UPDATE devices SET payload=? WHERE ip=?',
                       (json.dumps({'mac': REPLACEMENT_MAC}), self.ip))
            write_legacy_samples(db, self.ip, at, 90, 'Unidentified replacement')
        self.assertEqual({row['value'] for row in self.store.history(device_id=device_id)},
                         {10, 20, 'Moved during fallback'})
        path = self.store.path
        self.store.db.close()
        self.store = server.Store(path)
        self.assertEqual({row['value'] for row in self.store.history() if row['device_id'] is None},
                         {90, 'Unidentified replacement'})


class APITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.store=server.Store(Path(cls.tmp.name)/'test.sqlite3')
        with patch.object(server,'network_info',return_value={'local_ip':'10.23.0.2','subnet':'10.23.0.0/24'}):
            cls.collector=server.Collector(cls.store)
        cls.httpd=server.LocalHTTPServer(('127.0.0.1',0),server.Handler)
        cls.httpd.store=cls.store
        cls.httpd.collector=cls.collector
        cls.port=cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever,daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown();cls.httpd.server_close();cls.store.db.close();cls.tmp.cleanup()

    def request(self,path='/',method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        conn.request(method,path,body=body,headers=headers or {})
        response=conn.getresponse();result=(response.status,dict(response.headers),response.read());conn.close();return result

    def test_assets_state_history_and_csv(self):
        self.assertEqual(self.request()[0],200)
        self.assertEqual(self.request('/theme.js')[0],200)
        state=json.loads(self.request('/api/state')[2])
        self.assertEqual((state['devices'], state['events']), ([], []))
        self.assertEqual((state['firmware'], state['firmware_events']), ([], []))
        self.assertNotIn('dev_revision', state)
        self.assertEqual(self.request('/api/history?hours=24')[0],200)
        status,headers,body=self.request('/api/export.csv?hours=24')
        self.assertEqual(status,200);self.assertIn('text/csv',headers['Content-Type']);self.assertIn(b'ip,at,metric,value,unit,label',body)

    def test_development_revision_changes_with_assets_and_server_restart(self):
        with tempfile.TemporaryDirectory() as root, patch.object(server, 'ROOT', Path(root)):
            web = Path(root) / 'web'
            web.mkdir()
            asset = web / 'style.css'
            asset.write_text('body {}')
            with patch.object(server, 'DEV_RELOAD', 'first-worker'):
                before = json.loads(self.request('/api/state')[2])['dev_revision']
                asset.write_text('body { color: black; }')
                after_edit = json.loads(self.request('/api/state')[2])['dev_revision']
            with patch.object(server, 'DEV_RELOAD', 'second-worker'):
                after_restart = json.loads(self.request('/api/state')[2])['dev_revision']
            self.assertNotEqual(before, after_edit)
            self.assertNotEqual(after_edit, after_restart)

    def test_device_id_filters_history_and_edits_the_right_device_after_ip_reuse(self):
        ip = '10.23.0.20'
        self.store.add_device(ip, 'Original')
        self.store.record(ip, observation(ORIGINAL_MAC, 10, server.now()))
        original = next(d for d in self.store.devices() if d['ip'] == ip)
        self.store.record(ip, observation(REPLACEMENT_MAC, 90, server.now()))
        query = f'device_id={original["device_id"]}'
        status, _, body = self.request('/api/history?' + query)
        self.assertEqual(status, 200)
        self.assertEqual([row['value'] for row in json.loads(body)['samples']], [10])
        status, _, body = self.request('/api/export.csv?' + query)
        self.assertEqual(status, 200)
        row, = csv.DictReader(io.StringIO(body.decode('utf-8-sig')))
        self.assertEqual((row['device_id'], row['ip'], float(row['value'])), (str(original['device_id']), ip, 10))
        headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
        self.assertEqual(self.request(f'/api/devices/{original["device_id"]}', 'PATCH',
                                      json.dumps({'name': 'Renamed original'}), headers)[0], 200)
        devices = json.loads(self.request('/api/state')[2])['devices']
        self.assertEqual({d['ip']: d['name'] for d in devices}, {None: 'Renamed original', ip: 'Device .20'})

    def test_cross_origin_mutations_and_dns_rebinding_are_blocked(self):
        status,_,_=self.request('/api/state',headers={'Host':'attacker.example'})
        self.assertEqual(status,403)
        status,_,_=self.request('/api/scan','POST','{}',{'Content-Type':'application/json','X-Hearth-Request':'1','Origin':'https://attacker.example'})
        self.assertEqual(status,400)
        self.assertEqual(self.request('/api/scan','POST','{}',{'Content-Type':'application/json'})[0],400)

    def test_router_readings_are_only_served_to_loopback(self):
        monitor, = self.collector.router_monitors
        result = {'id': 'google-wifi', 'status': 'ready',
                  'devices': [{'name': 'Fixture speaker', 'upload_bps': 0, 'paused': True}]}
        accept = self.httpd.get_request

        def simulate_lan_client():
            connection, (_, port) = accept()
            return connection, ('10.23.0.4', port)

        with patch.object(monitor, 'snapshot', return_value=result):
            state = json.loads(self.request('/api/state')[2])
            self.assertEqual(state['router_monitoring'], [result])
            with patch.object(self.httpd, 'get_request', side_effect=simulate_lan_client):
                state = json.loads(self.request('/api/state')[2])
            self.assertNotIn('router_monitoring', state)

    def test_public_targets_and_invalid_ranges_are_rejected(self):
        headers={'Content-Type':'application/json','X-Hearth-Request':'1'}
        for ip in ['8.8.8.8','127.0.0.1','169.254.169.254','example.com','10.23.1.255']:
            with self.subTest(ip=ip):
                self.assertEqual(self.request('/api/devices','POST',json.dumps({'ip':ip,'name':'Test'}),headers)[0],400)
        for hours in ['NaN','inf','-1','721','bad']:
            self.assertEqual(self.request('/api/history?hours='+hours)[0],400)
        self.assertEqual(self.request('/../server.py')[0],404)


if __name__=='__main__':
    unittest.main()
