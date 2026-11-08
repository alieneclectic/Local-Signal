"""Synthetic BACnet packets and a loopback UDP peer; no LAN access."""
from copy import deepcopy
from datetime import datetime, timezone
import http.client
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch

import bacnet
import background
import server


def configuration():
    return {'protocol': 'bacnet-ip', 'device_instance': 1234, 'port': 47808, 'points': [
        {'object_type': 'analog-input', 'instance': 1, 'label': 'Supply air temperature'},
        {'object_type': 'binary-input', 'instance': 2, 'label': 'Fan running'},
        {'object_type': 'multi-state-value', 'instance': 3, 'label': 'Operating mode'}]}


def ack(request, value):
    # Independently assembled BACnet ReadProperty-ACK, without the client encoder.
    apdu = bytes([0x30, request[8], 12]) + request[10:] + b'\x3e' + value + b'\x3f'
    body = b'\x01\x00' + apdu
    return b'\x81\x0a' + struct.pack('!H', len(body) + 4) + body


class Peer:
    """One directly addressed device, replying to ReadProperty and nothing else."""
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.settimeout(.1)
        self.port = self.sock.getsockname()[1]
        self.requests, self.failures = [], []
        self.stop = threading.Event()
        self.values = {(8, 1234, 75): bytes.fromhex('c4020004d2'),
                       (8, 1234, 70): b'\x75\x0b\x00Test model',
                       (8, 1234, 44): b'\x74\x001.0',
                       (0, 1, 111): bytes.fromhex('820400'),
                       (0, 1, 117): bytes.fromhex('913e'),
                       (0, 1, 85): bytes.fromhex('4441b00000'),
                       (3, 2, 111): bytes.fromhex('820400'),
                       (3, 2, 85): bytes.fromhex('9100'),
                       (19, 3, 111): bytes.fromhex('820400'),
                       (19, 3, 85): bytes.fromhex('2102')}
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                data, address = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            self.requests.append(data)
            if len(data) != 17 or data[:8] != bytes.fromhex('810a001101040003') or data[9:11] != b'\x0c\x0c' or data[15] != 0x19:
                self.failures.append(data)
                continue
            object_number = int.from_bytes(data[11:15], 'big')
            value = self.values.get((object_number >> 22, object_number & 0x3fffff, data[16]))
            if value is None:
                response = bytes([0x50, data[8], 12, 0x91, 2, 0x91, 32])
                response = b'\x81\x0a\x00\x0d\x01\x00' + response
            else:
                response = ack(data, value)
            self.sock.sendto(response, address)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


class ConfigTests(unittest.TestCase):
    def test_strict_configuration_and_private_destinations(self):
        self.assertEqual(bacnet.validate_config(configuration()), configuration())
        self.assertEqual(bacnet.validate_config(None), {})
        for ip in ('8.8.8.8', '127.0.0.1', '169.254.169.254', 'example.com', '224.0.0.1', '10.23.0.255', '::1'):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                bacnet.private_address(ip)
        self.assertEqual(bacnet.private_address('10.23.0.10'), '10.23.0.10')
        invalid = []
        for field, value in (('device_instance', True), ('device_instance', 4194303), ('port', 0),
                             ('port', '47808'), ('points', []), ('points', 'all'), ('protocol', 'write'), ('write_property', 85)):
            config = configuration()
            config[field] = value
            invalid.append(config)
        config = configuration()
        config['points'] *= 5
        invalid.append(config)
        for field, value in (('object_type', 'device'), ('instance', -1), ('instance', False), ('label', ''), ('unit', '°C')):
            config = configuration()
            config['points'][0][field] = value
            invalid.append(config)
        config = configuration()
        config['points'].append(config['points'][0])
        invalid.append(config)
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                bacnet.validate_config(config)


class PacketTests(unittest.TestCase):
    def test_request_is_a_direct_unicast_read_property(self):
        self.assertEqual(bacnet.read_request(7, 0, 1, 85).hex(), '810a001101040003070c0c000000011955')
        request = bacnet.read_request(7, 0, 1, 85)
        self.assertEqual(bacnet.read_response(ack(request, bytes.fromhex('4441b00000')), 7, 0, 1, 85), (4, 22))

    def test_malformed_mismatched_segmented_and_nonfinite_values_are_rejected(self):
        packet = ack(bacnet.read_request(7, 0, 1, 85), bytes.fromhex('4441b00000'))
        invalid = [packet[:i] for i in range(len(packet))]
        for offset, value in ((1, 4), (3, 255), (5, 8), (6, 0x38), (7, 8), (8, 13), (14, 117), (15, 0x3f), (22, 0)):
            modified = bytearray(packet)
            modified[offset] = value
            invalid.append(bytes(modified))
        for value in (b'\x44' + struct.pack('!f', float('nan')), b'\x44' + struct.pack('!f', float('inf')),
                      b'\x44\x00', b'\x82\x04\x01', b'\x11\x00', b'\x75\xff'):
            invalid.append(ack(bacnet.read_request(7, 0, 1, 85), value))
        for data in invalid:
            with self.subTest(data=data.hex()), self.assertRaises(bacnet.BACnetError):
                bacnet.read_response(data, 7, 0, 1, 85)

    def test_poll_deadline_does_not_send_another_request(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            reader = bacnet.Reader(sock, 1)
            with patch.object(bacnet.time, 'monotonic', return_value=2), self.assertRaises(bacnet.BACnetError):
                reader.read(0, 1, 85)


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.peer = Peer()
        self.addCleanup(self.peer.close)
        self.config = configuration()
        self.config['port'] = self.peer.port
        self.device = {'ip': '10.23.0.10', 'monitor_config': self.config}

    def read(self):
        # Only redirect the synthetic private target to our actual loopback peer.
        with patch.object(bacnet, 'private_address', return_value='127.0.0.1'), \
             patch.object(server, 'ping', side_effect=AssertionError('Unexpected ping')), \
             patch.object(server, 'port_open', side_effect=AssertionError('Unexpected HTTP probe')):
            result = server.probe_device(self.device)
        self.assertFalse(self.peer.failures)
        return result

    def test_real_udp_reads_analog_binary_and_multistate_with_units_and_metadata(self):
        result = self.read()
        self.assertEqual((result['status'], result['model'], result['firmware']), ('online', 'Test model', '1.0'))
        self.assertEqual(result['metrics']['bacnet_1234_0_1_u62']['value'], 22)
        self.assertEqual(result['metrics']['bacnet_1234_0_1_u62']['unit'], '°C')
        self.assertEqual(result['metrics']['bacnet_1234_3_2']['value'], 0)
        self.assertEqual(result['metrics']['bacnet_1234_3_2']['kind'], 'boolean')
        self.assertEqual(result['metrics']['bacnet_1234_19_3']['value'], 2)
        self.assertEqual(result['point_errors'], [])
        self.assertNotIn('temperature_c', result['metrics'])  # No assumption of room temperature.
        self.assertNotIn('mac', result)  # A BACnet instance is not a hardware MAC.

    def test_faulted_overridden_and_out_of_service_points_are_missing_not_zero(self):
        for flag in (0x40, 0x20, 0x10):
            self.peer.values[(0, 1, 111)] = bytes([0x82, 4, flag])
            result = self.read()
            self.assertEqual(result['status'], 'online')
            self.assertNotIn('bacnet_1234_0_1_u62', result['metrics'])
            self.assertEqual(len(result['point_errors']), 1)
            self.assertIn('out of service', result['point_errors'][0]['error'])

    def test_wrong_identity_stops_before_any_point_reads(self):
        self.peer.values[(8, 1234, 75)] = bytes.fromhex('c4020004d3')
        result = self.read()
        self.assertFalse(result['metrics'])
        self.assertTrue(result['reachable'])
        self.assertEqual(result['status'], 'reachable')
        self.assertIn('identity', result['error'])
        self.assertEqual(len(self.peer.requests), 1)

    def test_invalid_or_missing_quality_units_and_value_leave_no_telemetry(self):
        self.config['points'] = self.config['points'][:1]
        for prop, value in ((111, None), (117, None), (85, None), (85, b'\x00'), (85, b'\x44'+struct.pack('!f', float('inf')))):
            old = self.peer.values[(0, 1, prop)]
            self.peer.values[(0, 1, prop)] = value
            result = self.read()
            self.assertEqual(result['status'], 'reachable')
            self.assertEqual(result['metrics'], {})
            self.assertNotIn('last_telemetry', result)
            self.assertEqual(len(result['point_errors']), 1)
            self.peer.values[(0, 1, prop)] = old

    def test_observed_zero_unknown_units_and_missing_optional_metadata(self):
        self.peer.values[(0, 1, 85)] = bytes.fromhex('4400000000')
        self.peer.values[(0, 1, 117)] = bytes.fromhex('9203e7')
        self.peer.values.pop((8, 1234, 70))
        self.peer.values.pop((8, 1234, 44))
        result = self.read()
        self.assertEqual(result['metrics']['bacnet_1234_0_1_u999']['value'], 0)
        self.assertEqual(result['metrics']['bacnet_1234_0_1_u999']['unit'], 'BACnet unit 999')
        self.assertEqual(result['model'], 'BACnet/IP device')
        self.assertIsNone(result['firmware'])

    def test_timeouts_are_safe_and_private_validation_happens_before_socket_creation(self):
        with patch.object(bacnet.Reader, 'read', side_effect=socket.timeout('timed out')):
            result = self.read()
        self.assertEqual(result['status'], 'unreachable')
        self.assertEqual(result['metrics'], {})
        self.device['ip'] = '8.8.8.8'
        with patch.object(bacnet.socket, 'socket', side_effect=AssertionError('No public target')), self.assertRaises(ValueError):
            bacnet.read(self.device, server.now())


class StorageAndAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = server.Store(self.path)
        self.addCleanup(lambda: self.store.db.close())
        self.store.add_device('10.23.0.10', 'AHU', monitor_config=configuration())

    def test_config_history_labels_and_last_known_readings_survive_restart_and_config_changes(self):
        at = datetime.now(timezone.utc).isoformat()
        metric = {'value': 0, 'label': 'Fan running', 'unit': '', 'kind': 'boolean'}
        self.store.record('10.23.0.10', {'status': 'online', 'reachable': True, 'checked_at': at,
                          'last_telemetry': at, 'metrics': {'bacnet_1234_3_2': metric}})
        self.store.record('10.23.0.10', {'status': 'unreachable', 'reachable': False, 'checked_at': at, 'metrics': {}})
        self.store.db.close()
        self.store = server.Store(self.path)
        device, = self.store.devices()
        self.assertEqual(device['monitor_config'], configuration())
        self.assertEqual(device['last_metrics']['bacnet_1234_3_2']['value'], 0)
        changed = configuration()
        changed['device_instance'] = 1235
        self.store.patch(None, {'monitor_config': changed, 'room': 'Mechanical'}, device_id=device['device_id'])
        updated, = self.store.devices()
        self.assertEqual((updated['name'], updated['room'], updated['status']), ('AHU', 'Mechanical', 'pending'))
        self.assertEqual(updated['metrics'], {})
        self.assertFalse(self.store.record('10.23.0.10', {'checked_at': at, 'metrics': {}},
                                          expected_monitor_config=configuration()))
        self.assertEqual([row['value'] for row in self.store.history()], [0])
        self.assertIn('bacnet.py', background.FILES)

    def test_existing_database_migrates_without_losing_labels_or_samples(self):
        self.store.patch('10.23.0.10', {'room': 'Plant room', 'notes': 'Keep this'})
        at = datetime.now(timezone.utc).isoformat()
        self.store.record('10.23.0.10', {'status': 'online', 'reachable': True, 'checked_at': at,
                          'metrics': {'bacnet_1234_0_1_u62': {'value': 22, 'unit': '°C', 'label': 'Supply air'}}})
        self.store.save_setting('custom_setting', {'keep': True})
        self.store.db.execute('ALTER TABLE devices DROP COLUMN monitor_config')
        self.store.db.commit()
        self.store.db.close()
        self.store = server.Store(self.path)
        device, = self.store.devices()
        self.assertEqual((device['room'], device['notes'], device['monitor_config']), ('Plant room', 'Keep this', {}))
        self.assertEqual([row['value'] for row in self.store.history()], [22])
        self.assertEqual(self.store.setting('custom_setting'), {'keep': True})

    def test_automatic_monitoring_still_accepts_a_verified_replacement_at_a_vacated_ip(self):
        ip, moved = '10.23.0.12', '10.23.0.13'
        self.store.add_device(ip, 'Original')
        at = datetime.now(timezone.utc).isoformat()
        observed = {'checked_at': at, 'status': 'reachable', 'reachable': True, 'metrics': {}, 'mac': '02:00:00:00:00:01'}
        self.store.record(ip, observed)
        original = next(d for d in self.store.devices() if d['ip'] == ip)
        self.store.record(moved, observed)
        self.assertTrue(self.store.record(ip, {**observed, 'mac': '02:00:00:00:00:02'},
                                         expected_device_id=original['device_id'], expected_monitor_config={}))
        devices = {d['ip']: d for d in self.store.devices()}
        self.assertEqual(devices[moved]['name'], 'Original')
        self.assertNotEqual(devices[ip]['device_id'], original['device_id'])

    def test_api_add_edit_export_and_existing_write_guards(self):
        with patch.object(server, 'network_info', return_value={}):
            collector = server.Collector(self.store)
        httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.store, httpd.collector = self.store, collector
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def request(path, method='GET', data=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            self.addCleanup(conn.close)
            conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()

        headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
        data = {'ip': '10.23.0.11', 'name': 'Zone controller', 'monitor_config': configuration()}
        with patch.object(collector, 'start_poll') as poll:
            self.assertEqual(request('/api/devices', 'POST', data, {**headers, 'Origin': 'https://attacker.example'})[0], 400)
            bad = deepcopy(data)
            bad['monitor_config']['points'][0]['write'] = True
            self.assertEqual(request('/api/devices', 'POST', bad, headers)[0], 400)
            self.assertEqual(request('/api/devices', 'POST', data, headers)[0], 201)
            poll.assert_called_once()
        device = next(d for d in json.loads(request('/api/state')[1])['devices'] if d['ip'] == data['ip'])
        self.assertEqual(device['protocol'], 'bacnet-ip')
        config = configuration()
        config['port'] = 47809
        self.assertEqual(request(f'/api/devices/{device["device_id"]}', 'PATCH', {'monitor_config': config}, headers)[0], 200)
        self.assertEqual(request('/api/devices', 'POST', data, headers)[0], 400)
        self.assertEqual(request('/api/export.csv')[0], 200)
        self.assertEqual(request(f'/api/devices/{device["device_id"]}', 'PATCH', {'relay': True}, headers)[0], 400)
