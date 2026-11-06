import json
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import unittest

import network_names as names
import server


class DNSTests(unittest.TestCase):
    def test_compressed_a_record(self):
        wire_name = b'\x07speaker\x05local\0'
        raw = struct.pack('!6H', 17, 0x8400, 1, 1, 0, 0) + wire_name + struct.pack('!HH', 1, 1)
        raw += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 120, 4) + socket.inet_aton('10.23.1.20')
        records = names.dns_records(raw)
        self.assertEqual(records[0]['name'], 'speaker.local')
        self.assertEqual(records[0]['address'], '10.23.1.20')

    def test_bad_dns_compression_and_truncation_are_bounded(self):
        for raw in (b'\xc0\0', b'\xff\xff', b'\x05ab'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                names.dns_name(raw, 0)
        with self.assertRaises(ValueError):
            names.dns_records(b'too short')

    def test_txt_keeps_only_name_and_model_fields(self):
        items = [b'fn=Living room', b'md=Speaker', b'password=do-not-store']
        body = b''.join(bytes([len(x)]) + x for x in items)
        raw = struct.pack('!6H', 0, 0x8400, 0, 1, 0, 0) + names.encode_name('Speaker._googlecast._tcp.local')
        raw += struct.pack('!HHIH', 16, 1, 120, len(body)) + body
        txt = names.dns_records(raw)[0]['txt']
        self.assertEqual(txt, {'fn':'Living room', 'md':'Speaker'})

    def test_friendly_service_is_tied_to_its_srv_host_address(self):
        records = [
            {'type':1, 'name':'speaker.local', 'address':'10.23.1.20'},
            {'type':1, 'name':'other.local', 'address':'10.23.1.21'},
            {'type':33, 'name':'uuid._googlecast._tcp.local', 'labels':['uuid','_googlecast','_tcp','local'], 'target':'speaker.local'},
            {'type':16, 'name':'uuid._googlecast._tcp.local', 'txt':{'fn':'Living room','md':'Nest Mini'}},
        ]
        found = names.names_from_mdns(records, {'10.23.1.20','10.23.1.21'})
        friendly = [x for x in found if x['source']=='mDNS friendly name']
        self.assertEqual(len(friendly), 1)
        self.assertEqual(friendly[0]['ip'], '10.23.1.20')
        self.assertEqual(friendly[0]['name'], 'Living room')
        self.assertEqual(friendly[0]['model'], 'Nest Mini')

    def test_opaque_ids_do_not_override_human_names(self):
        observations = [{'name':'07225AFF-C131-482C-9861-336B90949DF4','source':'mDNS service'},
                        {'name':'Living room','source':'mDNS service'},
                        {'name':'10.23.1.20 - Sonos Arc - RINCON_ABC123','source':'UPnP friendly name'}]
        self.assertEqual(min(observations, key=names.name_rank)['name'], 'Living room')

    def test_names_are_sanitized_without_guessing_identity(self):
        self.assertEqual(names.clean_name('  Den\x00 TV\u202e  '), 'Den TV')
        self.assertIsNone(names.clean_name('a'*101))
        self.assertTrue(names.private_ip('10.23.1.20'))
        self.assertFalse(names.private_ip('8.8.8.8'))
        self.assertFalse(names.private_ip('127.0.0.1'))


class StoredNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name)/'names.sqlite3')
        self.ip = '10.23.1.88'
        self.store.add_device(self.ip)
        self.observation = {'ip':self.ip,'name':'Den TV','hostname':'tv.local','source':'mDNS friendly name','model':'Chromecast'}

    def tearDown(self):
        self.store.db.close();self.tmp.cleanup()

    def device(self):
        return next(d for d in self.store.devices() if d['ip']==self.ip)

    def test_auto_names_store_source_and_do_not_mark_host_online(self):
        self.store.apply_names([self.observation])
        d = self.device()
        self.assertEqual(d['name'], 'Den TV')
        self.assertEqual(d['name_mode'], 'auto')
        self.assertEqual(d['discovered_name_source'], 'mDNS friendly name')
        self.assertEqual(d['hostname'], 'tv.local')
        self.assertEqual(d['status'], 'pending')

    def test_custom_label_survives_future_discovery_and_polling(self):
        self.store.apply_names([self.observation])
        self.store.patch(self.ip, {'name':'My favorite TV'})
        self.store.apply_names([{**self.observation, 'name':'New broadcast name'}])
        self.store.record(self.ip, {'status':'reachable','reachable':True,'checked_at':server.now(),'metrics':{}})
        d = self.device()
        self.assertEqual(d['name'], 'My favorite TV')
        self.assertEqual(d['discovered_name'], 'New broadcast name')
        self.assertEqual(d['name_mode'], 'manual')

    def test_switch_back_to_auto_uses_saved_advertised_name(self):
        self.store.patch(self.ip, {'name':'Custom TV'})
        self.store.apply_names([self.observation])
        self.store.patch(self.ip, {'name_mode':'auto'})
        self.assertEqual(self.device()['name'], 'Den TV')
        self.store.apply_names([{**self.observation,'name':'Bedroom TV'}])
        self.assertEqual(self.device()['name'], 'Bedroom TV')

    def test_explicit_manual_placeholder_is_preserved(self):
        self.store.patch(self.ip, {'name':'Device .88'})
        self.store.apply_names([self.observation])
        self.assertEqual(self.device()['name'], 'Device .88')

    def test_no_name_keeps_placeholder_and_manual_heater_names(self):
        heater_ip = '10.23.0.10'
        self.store.add_device(heater_ip, 'Workshop heater')
        self.store.apply_names([{'ip': heater_ip, 'name': 'shelly1pm-1234', 'source': 'Local DNS'}])
        self.assertEqual(next(d for d in self.store.devices() if d['ip'] == heater_ip)['name'], 'Workshop heater')
        self.assertEqual(self.device()['name'], 'Device .88')

    def test_existing_database_migrates_generated_labels_only(self):
        path = Path(self.tmp.name)/'legacy.sqlite3'
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE devices(ip TEXT PRIMARY KEY,name TEXT NOT NULL,room TEXT DEFAULT 'Unassigned',notes TEXT DEFAULT '',pinned INTEGER DEFAULT 0,expected_sleep INTEGER DEFAULT 0,created_at TEXT NOT NULL,last_seen TEXT,last_telemetry TEXT,payload TEXT DEFAULT '{}')")
        for ip, name in [('10.23.1.90','Device .90'),('10.23.1.91','Workshop heater')]:
            db.execute('INSERT INTO devices(ip,name,created_at) VALUES(?,?,?)', (ip,name,server.now()))
        db.commit();db.close()
        legacy = server.Store(path)
        records = {d['ip']:d for d in legacy.devices()}
        self.assertEqual(records['10.23.1.90']['name_mode'], 'auto')
        self.assertEqual(records['10.23.1.91']['name_mode'], 'manual')
        self.assertEqual(records['10.23.1.91']['name'], 'Workshop heater')
        legacy.db.close()


if __name__=='__main__':
    unittest.main()
