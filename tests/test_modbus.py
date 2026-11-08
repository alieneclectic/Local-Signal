"""Synthetic register maps and loopback TCP only; never contacts a LAN device."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import socket
import socketserver
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import background
import modbus
import server


def configuration():
    return {'protocol': 'modbus-tcp', 'port': 502, 'unit_id': 1, 'points': [
        {'table': 'holding', 'address': 0, 'data_type': 'float32', 'scale': .001, 'unit': 'kW', 'label': 'Active power'},
        {'table': 'input', 'address': 10, 'data_type': 'int16', 'scale': .1, 'unit': '°C', 'label': 'Supply air'},
        {'table': 'coil', 'address': 2, 'label': 'Fan running'},
        {'table': 'discrete', 'address': 3, 'label': 'Alarm input'}]}


def normalized_point(**changes):
    point = {'table': 'holding', 'address': 0, 'data_type': 'uint16', 'label': 'Test value', **changes}
    return modbus.validate_config({'protocol': 'modbus-tcp', 'points': [point]})['points'][0]


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        try:
            part = sock.recv(size - len(data))
        except (ConnectionResetError, socket.timeout):
            return None
        if not part:
            return None
        data.extend(part)
    return bytes(data)


class Peer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self):
        self.requests, self.failures = [], []
        self.values = {(3, 0): bytes.fromhex('449a5000'), (4, 10): bytes.fromhex('ff83'),
                       (1, 2): b'\x00', (2, 3): b'\x01'}
        self.modify = lambda frame: frame
        peer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(2)
                while data := read_exact(self.request, 12):
                    peer.requests.append(data)
                    transaction, protocol, length, unit, function, address, count = struct.unpack('!HHHBBHH', data)
                    if protocol != 0 or length != 6 or function not in (1, 2, 3, 4) or not 1 <= count <= 4:
                        peer.failures.append(data)
                        return
                    value = peer.values.get((function, address))
                    if value is None:
                        body = bytes([function | 0x80, 2])
                    elif isinstance(value, tuple):
                        body = bytes([function | 0x80, value[0]])
                    else:
                        body = bytes([function, len(value)]) + value
                    frame = struct.pack('!HHHB', transaction, 0, len(body) + 1, unit) + body
                    frame = peer.modify(frame)
                    if frame is None:
                        return
                    try:
                        # The reader must handle TCP fragmentation, even within MBAP.
                        for byte in frame:
                            self.request.sendall(bytes([byte]))
                    except (BrokenPipeError, ConnectionResetError):
                        return

        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=2)


class ConfigAndDecodeTests(unittest.TestCase):
    def test_only_supported_explicit_read_maps_are_accepted(self):
        config = modbus.validate_config(configuration())
        self.assertEqual(modbus.validate_config(config), config)
        self.assertEqual(config['points'][0]['offset'], 0)
        for field, value in (('protocol', 'write'), ('unit_id', True), ('unit_id', 256), ('unit_id', -1),
                             ('port', 0), ('port', '502'), ('points', []), ('points', 'all'), ('function', 6)):
            data = configuration()
            data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                modbus.validate_config(data)
        for changes in ({'table': 'write'}, {'table': []}, {'data_type': 'int64'}, {'data_type': []},
                        {'address': -1}, {'address': True}, {'address': 65535, 'data_type': 'float32'},
                        {'scale': 0}, {'scale': float('nan')}, {'offset': float('inf')}, {'invalid_value': True},
                        {'invalid_value': 65536}, {'invalid_value': .5}, {'data_type': 'float32', 'invalid_value': 1e100},
                        {'invalid_value': float('nan')}, {'word_order': 'guess'}, {'byte_order': 'guess'},
                        {'label': ''}, {'unit': 'x' * 21}, {'write': 123}, {'table': 'coil', 'data_type': 'uint16'},
                        {'table': 'coil', 'data_type': 'bit', 'scale': 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalized_point(**changes)
        for points in ([{'table': 'input', 'address': 0, 'label': 'A'}] * 13,
                       [{'table': 'input', 'address': 0, 'label': 'A'}] * 2):
            with self.assertRaises(ValueError):
                modbus.validate_config({'protocol': 'modbus-tcp', 'points': points})
        for unit in (0, 1, 247, 255):
            self.assertEqual(modbus.validate_config({'protocol': 'modbus-tcp', 'unit_id': unit,
                             'points': [{'table': 'coil', 'address': 65535, 'label': 'State'}]})['unit_id'], unit)

    def test_all_numeric_formats_and_four_byte_word_orders(self):
        for kind, raw, expected in [('uint16', 'abcd', 43981), ('int16', 'ff85', -123),
                                    ('uint32', 'ffffffff', 4294967295), ('int32', '80000000', -2147483648),
                                    ('float32', '42f70000', 123.5), ('float64', '400921fb54442d18', 3.141592653589793)]:
            with self.subTest(kind=kind):
                self.assertEqual(modbus.decode(bytes.fromhex(raw), normalized_point(data_type=kind)), expected)
        for byte_order, word_order, raw in [('big', 'big', '12345678'), ('big', 'little', '56781234'),
                                            ('little', 'big', '34127856'), ('little', 'little', '78563412')]:
            with self.subTest(byte_order=byte_order, word_order=word_order):
                point = normalized_point(data_type='uint32', byte_order=byte_order, word_order=word_order)
                self.assertEqual(modbus.decode(bytes.fromhex(raw), point), 0x12345678)
        point = normalized_point(data_type='int16', scale=.1, offset=-5)
        self.assertAlmostEqual(modbus.decode(bytes.fromhex('ff85'), point), -17.3)

    def test_invalid_markers_nonfinite_and_overflow_are_not_zero(self):
        for raw, point in [(b'\xff\xff', normalized_point(invalid_value=65535, scale=.1)),
                           (bytes.fromhex('3dcccccd'), normalized_point(data_type='float32', invalid_value=.1)),
                           (bytes.fromhex('7fc00000'), normalized_point(data_type='float32')),
                           (bytes.fromhex('7ff0000000000000'), normalized_point(data_type='float64')),
                           (b'\x00\x02', normalized_point(scale=1e308)),
                           (b'\x02', normalized_point(table='coil', data_type='bit')),
                           (b'\x00', normalized_point())]:
            with self.subTest(raw=raw.hex()), self.assertRaises(modbus.InvalidReading):
                modbus.decode(raw, point)
        self.assertEqual(modbus.decode(b'\x00\x00', normalized_point()), 0)
        self.assertEqual(modbus.decode(b'\x00', normalized_point(table='coil', data_type='bit')), 0)

    def test_metric_identity_changes_with_mapping_but_not_labels_or_observed_ip_moves(self):
        device = {'device_id': 4, 'ip': '10.23.0.10'}
        config = modbus.validate_config(configuration())
        point = config['points'][0]
        key = modbus.metric_key(device, config, point)
        self.assertEqual(key, modbus.metric_key({**device, 'ip': '10.23.0.11'}, config, {**point, 'label': 'Renamed'}))
        for change in ({'scale': 2}, {'offset': 2}, {'unit': 'W'}, {'data_type': 'uint32'}, {'word_order': 'little'}, {'invalid_value': 999}):
            self.assertNotEqual(key, modbus.metric_key(device, config, {**point, **change}))
        self.assertNotEqual(key, modbus.metric_key({'device_id': 5, 'ip': device['ip']}, config, point))
        self.assertNotEqual(key, modbus.metric_key(device, {**config, 'unit_id': 2}, point))
        self.assertNotEqual(key, modbus.metric_key(device, {**config, 'port': 503}, point))

    def test_private_address_validation_precedes_any_connection(self):
        for ip in ('8.8.8.8', '127.0.0.1', '169.254.169.254', '224.0.0.1', '10.23.0.255', 'example.com', '::1'):
            with self.subTest(ip=ip), patch.object(modbus.socket, 'create_connection', side_effect=AssertionError('No connection')), self.assertRaises(ValueError):
                modbus.read({'ip': ip, 'monitor_config': configuration()}, server.now())
        self.assertEqual(modbus.private_address('10.23.0.10'), '10.23.0.10')

    def test_read_requests_never_use_write_functions_and_addresses_stay_zero_based(self):
        point = normalized_point(address=108, data_type='float32')
        self.assertEqual(modbus.request(7, 1, point).hex(), '0007000000060103006c0002')
        for table, function in modbus.FUNCTIONS.items():
            point = normalized_point(table=table, data_type='bit' if function in (1, 2) else 'uint16')
            self.assertEqual(modbus.request(7, 1, point)[7], function)

    def test_deadline_is_absolute_across_fragmented_receives(self):
        class SlowSocket:
            def settimeout(self, value): pass
            def recv(self, size): return b'\x00'
        with patch.object(modbus.time, 'monotonic', side_effect=[0, .3, .6, 1.1]), self.assertRaises(modbus.ModbusError):
            modbus.receive(SlowSocket(), 7, 1)


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.peer = Peer()
        self.addCleanup(self.peer.close)
        self.config = configuration()
        self.config['port'] = self.peer.server_address[1]
        self.device = {'device_id': 42, 'ip': '10.23.0.10', 'monitor_config': self.config}

    def read(self):
        with patch.object(modbus, 'private_address', return_value='127.0.0.1'), \
             patch.object(server, 'ping', side_effect=AssertionError('Unexpected ping')), \
             patch.object(server, 'port_open', side_effect=AssertionError('Unexpected HTTP probe')):
            result = server.probe_device(self.device)
        self.assertFalse(self.peer.failures)
        return result

    def test_real_fragmented_tcp_reads_all_tables_and_preserves_zero(self):
        result = self.read()
        self.assertEqual((result['status'], result['telemetry_source']), ('online', 'Modbus TCP'))
        readings = {m['label']: m for m in result['metrics'].values()}
        self.assertAlmostEqual(readings['Active power']['value'], 1.2345)
        self.assertEqual(readings['Active power']['unit'], 'kW')
        self.assertEqual(readings['Supply air']['value'], -12.5)
        self.assertEqual(readings['Fan running']['value'], 0)
        self.assertEqual(readings['Alarm input']['value'], 1)
        self.assertEqual(readings['Fan running']['kind'], 'boolean')
        self.assertEqual(result['point_errors'], [])
        self.assertNotIn('mac', result)
        self.assertNotIn('power_w', result['metrics'])
        self.assertEqual([request[7] for request in self.peer.requests], [3, 4, 1, 2])

    def test_exceptions_are_point_failures_and_allow_subsequent_reads(self):
        self.peer.values[(3, 0)] = (2,)
        result = self.read()
        self.assertEqual(result['status'], 'online')
        self.assertEqual(len(result['metrics']), 3)
        self.assertIn('Illegal data address', result['point_errors'][0]['error'])
        self.assertEqual(len(self.peer.requests), 4)

    def test_invalid_values_and_quality_sentinels_do_not_turn_into_fresh_samples(self):
        self.peer.values[(3, 0)] = bytes.fromhex('7fc00000')
        self.config['points'][1]['invalid_value'] = -125
        result = self.read()
        self.assertEqual(len(result['metrics']), 2)
        self.assertEqual(len(result['point_errors']), 2)
        self.assertEqual(len(self.peer.requests), 4)

    def test_bad_headers_functions_lengths_and_truncated_frames_stop_the_stream(self):
        def replace(frame, at, data): return frame[:at] + data + frame[at + len(data):]
        for mutate in (lambda f: replace(f, 0, b'\xff\xff'), lambda f: replace(f, 2, b'\x00\x01'),
                       lambda f: replace(f, 4, b'\xff\xff'), lambda f: replace(f, 6, b'\x02'),
                       lambda f: replace(f, 7, b'\x06'), lambda f: replace(f, 8, b'\x02'), lambda f: None):
            self.peer.modify = mutate
            self.peer.requests.clear()
            result = self.read()
            self.assertEqual(result['status'], 'reachable')
            self.assertEqual(result['metrics'], {})
            self.assertNotIn('last_telemetry', result)
            self.assertEqual(len(result['point_errors']), 4)
            self.assertEqual(len(self.peer.requests), 1)

    def test_connect_failure_is_unreachable_and_expired_poll_does_not_send(self):
        with patch.object(modbus.socket, 'create_connection', side_effect=ConnectionRefusedError('refused')):
            result = self.read()
        self.assertEqual(result['status'], 'unreachable')
        with socket.socket() as sock, self.assertRaises(modbus.ModbusError):
            modbus.Reader(sock, time.monotonic() - 1, 1).read(normalized_point())


class StorageAndAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = server.Store(self.path)
        self.addCleanup(lambda: self.store.db.close())
        self.store.add_device('10.23.0.10', 'Plant meter', room='Mechanical', notes='Preserve', monitor_config=configuration())

    def test_restart_reconfiguration_and_missing_readings_preserve_history(self):
        device, = self.store.devices()
        config = device['monitor_config']
        key = modbus.metric_key(device, config, config['points'][0])
        self.store.record(device['ip'], {'checked_at': server.now(), 'status': 'online', 'reachable': True,
                          'last_telemetry': server.now(), 'metrics': {key: {'value': 0, 'unit': 'kW', 'label': 'Active power'}}})
        self.store.record(device['ip'], {'checked_at': server.now(), 'status': 'reachable', 'reachable': True, 'metrics': {}})
        self.store.db.close()
        self.store = server.Store(self.path)
        device, = self.store.devices()
        self.assertEqual(device['last_metrics'][key]['value'], 0)
        self.assertEqual(device['monitor_config'], config)
        changed = deepcopy(config)
        changed['points'][0]['scale'] = 2
        self.store.patch(None, {'monitor_config': changed}, device_id=device['device_id'])
        self.assertFalse(self.store.record(device['ip'], {'checked_at': server.now(), 'metrics': {}}, expected_monitor_config=config))
        device, = self.store.devices()
        self.assertEqual((device['status'], device['metrics'], device['notes']), ('pending', {}, 'Preserve'))
        self.assertEqual([row['value'] for row in self.store.history()], [0])
        self.store.patch(device['ip'], {'monitor_config': {}})
        self.assertEqual(self.store.devices()[0]['monitor_config'], {})
        self.assertIn('modbus.py', background.FILES)

    def test_api_configuration_and_export_keep_existing_write_guards(self):
        with patch.object(server, 'network_info', return_value={}):
            collector = server.Collector(self.store)
        httpd = server.LocalHTTPServer(('127.0.0.1', 0), server.Handler)
        httpd.store, httpd.collector = self.store, collector
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)

        def request(path, method='GET', data=None, headers=None):
            conn = http.client.HTTPConnection('127.0.0.1', httpd.server_port, timeout=3)
            self.addCleanup(conn.close)
            conn.request(method, path, body=json.dumps(data) if data is not None else None, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()

        headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
        data = {'ip': '10.23.0.11', 'name': 'Boiler meter', 'monitor_config': configuration()}
        with patch.object(collector, 'start_poll') as poll:
            self.assertEqual(request('/api/devices', 'POST', data, {'Content-Type': 'application/json'})[0], 400)
            self.assertEqual(request('/api/devices', 'POST', data, {**headers, 'Origin': 'https://attacker.example'})[0], 400)
            bad = deepcopy(data)
            bad['monitor_config']['points'][0]['function'] = 6
            self.assertEqual(request('/api/devices', 'POST', bad, headers)[0], 400)
            self.assertEqual(request('/api/devices', 'POST', data, headers)[0], 201)
            poll.assert_called_once()
        device = next(d for d in json.loads(request('/api/state')[1])['devices'] if d['ip'] == data['ip'])
        self.assertEqual((device['protocol'], device['kind']), ('modbus-tcp', 'commercial'))
        config = device['monitor_config']
        point = config['points'][0]
        key = modbus.metric_key(device, config, point)
        self.store.record(device['ip'], {'checked_at': server.now(), 'status': 'online', 'reachable': True,
                          'metrics': {key: {'value': 1.2, 'unit': 'kW', 'label': '=unsafe label'}}})
        status, csv = request(f'/api/export.csv?device_id={device["device_id"]}')
        self.assertEqual(status, 200)
        self.assertIn(b"'=unsafe label", csv)
        config['unit_id'] = 2
        self.assertEqual(request(f'/api/devices/{device["device_id"]}', 'PATCH', {'monitor_config': config}, headers)[0], 200)
        self.assertEqual(request(f'/api/devices/{device["device_id"]}', 'PATCH', {'write_register': 1}, headers)[0], 400)
