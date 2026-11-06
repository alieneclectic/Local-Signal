import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import server


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

    def test_cross_origin_mutations_and_dns_rebinding_are_blocked(self):
        status,_,_=self.request('/api/state',headers={'Host':'attacker.example'})
        self.assertEqual(status,403)
        status,_,_=self.request('/api/scan','POST','{}',{'Content-Type':'application/json','X-Hearth-Request':'1','Origin':'https://attacker.example'})
        self.assertEqual(status,400)
        self.assertEqual(self.request('/api/scan','POST','{}',{'Content-Type':'application/json'})[0],400)

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
