"""Exercise the device HTTP wire requests and dashboard API using loopback peers."""
from contextlib import closing, contextmanager, ExitStack
import csv
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

import background
import iot
import server

STAMP = '2026-11-21T17:00:00+00:00'
TASMOTA_SENSORS = {'StatusSNS': {
    'Time': '2026-11-21T12:00:00',
    'ENERGY': {'Power': 0, 'Voltage': 121.5, 'Current': 0, 'Today': 0.123, 'Total': 19.4},
    'AM2301': {'Temperature': 32, 'Humidity': 39.1},
    'DS18B20-2': {'Temperature': 50},
    'ESP32': {'Temperature': 140}, 'TempUnit': 'F',
}}
TASMOTA_STATE = {'StatusSTS': {'POWER1': 'OFF', 'POWER2': 'ON', 'UptimeSec': 103,
                              'Wifi': {'Signal': -63, 'RSSI': 75, 'SSId': 'fixture-private'}}}
WLED_STATE = {'state': {'on': False, 'bri': 128, 'seg': []},
              'info': {'ver': '0.15.0', 'leds': {'count': 60}, 'mac': '021122334455', 'uptime': 0}}
FRONIUS_FLOW = {'Head': {'Status': {'Code': 0}}, 'Body': {'Data': {'Site': {
    'P_PV': 0, 'P_Grid': -250, 'P_Load': -800, 'P_Akku': None, 'E_Day': 640, 'E_Total': 43021.5,
}}}}
OPENDTU_STATUS = {
    'inverters': [{'serial': 'fixture-private', 'poll_enabled': True, 'reachable': True, 'data_age': 4},
                  {'poll_enabled': False, 'reachable': False, 'data_age': 957}],
    'total': {'Power': {'v': 0, 'u': 'W'}, 'YieldDay': {'v': 490, 'u': 'Wh'}, 'YieldTotal': {'v': 12.2, 'u': 'kWh'}},
}
ESP_TEMPERATURE = {'domain': 'sensor', 'name': 'Température', 'subdevice': 'Cold Room', 'unit': '°C'}
ESP_BINARY = {'domain': 'binary_sensor', 'name': 'door'}
CASES = (
    ('tasmota', '10.23.0.41', {'/cm?cmnd=Status%2010': TASMOTA_SENSORS, '/cm?cmnd=Status%2011': TASMOTA_STATE},
     {}, 'tasmota_power_w'),
    ('esphome', '10.23.0.42', {
        '/sensor/Cold%20Room/Temp%C3%A9rature': {'id': 'sensor/Cold Room/Température', 'state': '0 °C', 'value': 0},
        '/binary_sensor/door': {'id': 'binary_sensor-door', 'state': 'OFF', 'value': False},
    }, {'points': [ESP_TEMPERATURE, ESP_BINARY]}, None),
    ('wled', '10.23.0.43', {'/json': WLED_STATE}, {}, 'wled_brightness_pct'),
    ('fronius-solar', '10.23.0.44', {'/solar_api/v1/GetPowerFlowRealtimeData.fcgi': FRONIUS_FLOW}, {}, 'fronius_pv_w'),
    ('opendtu', '10.23.0.45', {'/api/livedata/status': OPENDTU_STATUS}, {}, 'opendtu_ac_w'),
)


@contextmanager
def serve(httpd):
    worker = threading.Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    worker.start()
    try:
        yield httpd.server_port
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(2)


class FixtureDevices(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests = []
        self.routes = {(ip, path): payload for _, ip, responses, _, _ in CASES for path, payload in responses.items()}
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                host = self.headers.get('Host', '')
                peer.requests.append((self.command, host, self.path, self.headers.get('Authorization')))
                payload = peer.routes.get((host.partition(':')[0], self.path), (404, b''))
                status, body = payload if isinstance(payload, tuple) else (200, json.dumps(payload).encode())
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                if status == 302:
                    self.send_header('Location', 'http://203.0.113.42/redirected')
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        super().__init__(('127.0.0.1', 0), Handler)

    @contextmanager
    def redirect(self):
        original = socket.create_connection
        targets = {ip for _, ip, _, _, _ in CASES}

        def connect(address, *args, **kwargs):
            host, port = address
            if host in targets:
                if port != 8080:
                    raise AssertionError(f'Unexpected fixture port {address}.')
                return original(self.server_address, *args, **kwargs)
            if host == '127.0.0.1' and port != 1:
                return original(address, *args, **kwargs)
            raise AssertionError(f'Unexpected connection {address}.')

        with patch.object(socket, 'create_connection', side_effect=connect), patch.dict(
            'os.environ', {'http_proxy': 'http://127.0.0.1:1', 'no_proxy': ''}
        ):
            yield


def probe(index, **overrides):
    protocol, ip, _, config, _ = CASES[index]
    return iot.read({'ip': ip, 'monitor_config': {'protocol': protocol, 'port': 8080, **config, **overrides}}, STAMP)


class IoTTests(unittest.TestCase):
    def test_configuration_validation_and_runtime_packaging(self):
        pyproject = Path(server.ROOT, 'pyproject.toml').read_text()
        self.assertIn('iot.py', tomllib.loads(pyproject)['tool']['briefcase']['app']['collector']['sources'])
        self.assertIn('iot.py', background.FILES)
        for protocol, ip, _, config, _ in CASES:
            value = {'protocol': protocol, **config}
            normalized = server.validate_monitor_config(value)
            self.assertEqual(normalized['port'], 80)
            self.assertEqual(server.validate_monitor_config(normalized), normalized)
            for extra in ({'port': True}, {'port': 0}, {'port': '80'}, {'path': '/settings'}, {'password': 'fixture'}):
                with self.subTest(protocol=protocol, extra=extra), self.assertRaises(ValueError):
                    server.validate_monitor_config({**value, **extra})
            with patch.object(iot, 'read_json') as transport:
                for host in ('127.0.0.1', '169.254.0.1', '8.8.8.8', '10.0.0.255', 'example.local'):
                    with self.subTest(protocol=protocol, host=host), self.assertRaises(ValueError):
                        iot.read({'ip': host, 'monitor_config': value}, STAMP)
                transport.assert_not_called()
        for points in ([], [ESP_TEMPERATURE]*2, [{'name': str(i)} for i in range(9)],
                       [{'name': 'outside/enable'}], [{'name': '%2F'}], [{'name': '..'}],
                       [{'name': 'room\nnext'}], [{'name': 'sensor', 'subdevice': '../device'}],
                       [{'domain': 'switch', 'name': 'plug'}]):
            with self.subTest(points=points), self.assertRaises(ValueError):
                server.validate_monitor_config({'protocol': 'esphome', 'points': points})

    def test_device_metrics_preserve_zero_signs_identity_types_and_units(self):
        peer = FixtureDevices()
        with serve(peer), peer.redirect():
            tasmota, esp, wled, fronius, opendtu = [probe(i) for i in range(len(CASES))]
        for result in (tasmota, esp, wled, fronius, opendtu):
            self.assertEqual((result['status'], result['last_telemetry']), ('online', STAMP))
            self.assertNotIn('fixture-private', json.dumps(result))
        t = tasmota['metrics']
        self.assertEqual((t['tasmota_power_w']['value'], t['tasmota_total_kwh']['value']), (0, 19.4))
        self.assertAlmostEqual(t['tasmota_sensor_AM2301_c']['value'], 0)
        self.assertAlmostEqual(t['tasmota_sensor_DS18B20-2_c']['value'], 10)
        self.assertNotIn('tasmota_sensor_ESP32_c', t)
        self.assertEqual((t['tasmota_output_1']['value'], t['tasmota_output_2']['value']), (0, 1))
        self.assertEqual(t['rssi_dbm']['value'], -63)
        self.assertEqual([(m['label'], m['value'], m['unit'], m.get('kind')) for m in esp['metrics'].values()],
                         [('Cold Room/Température', 0, '°C', None), ('door', 0, '', 'boolean')])
        self.assertEqual((wled['firmware'], wled['mac']), ('0.15.0', '021122334455'))
        self.assertEqual(wled['metrics']['wled_brightness_pct']['value'], 0)
        self.assertEqual(wled['metrics']['wled_led_count']['value'], 60)
        self.assertEqual(fronius['metrics']['fronius_grid_w']['value'], -250)
        self.assertEqual(fronius['metrics']['fronius_load_w']['value'], 800)
        self.assertNotIn('fronius_battery_w', fronius['metrics'])
        self.assertAlmostEqual(fronius['metrics']['fronius_total_kwh']['value'], 43.0215)
        self.assertAlmostEqual(opendtu['metrics']['opendtu_daily_kwh']['value'], .49)
        self.assertAlmostEqual(opendtu['metrics']['opendtu_total_kwh']['value'], 12.2)
        self.assertEqual(peer.requests, [('GET', f'{ip}:8080', path, None)
                                         for _, ip, paths, _, _ in CASES for path in paths])

    def test_redirect_auth_oversized_and_invalid_payloads(self):
        peer = FixtureDevices()
        with serve(peer), peer.redirect():
            for payload, status in (
                ((302, b''), 'reachable'), ((401, b''), 'auth_required'), ((403, b''), 'auth_required'),
                ((200, b'{}'+b' '*262_144), 'reachable'), ((200, b'{"state":'), 'reachable'),
                ((200, b'[]'), 'reachable'), ({'state': {'on': True}}, 'reachable'),
            ):
                peer.routes[('10.23.0.43', '/json')] = payload
                result = probe(2)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['metrics'], {})
                self.assertNotIn('last_telemetry', result)
        self.assertEqual(len(peer.requests), 7)

    def test_missing_points_do_not_replace_successful_esp_and_tasmota_readings(self):
        peer = FixtureDevices()
        with serve(peer), peer.redirect():
            before = probe(1)
            peer.routes[('10.23.0.42', '/binary_sensor/door')] = (404, b'')
            payload = peer.routes[('10.23.0.42', '/sensor/Cold%20Room/Temp%C3%A9rature')]
            peer.routes[('10.23.0.42', '/sensor/Cold%20Room/Temp%C3%A9rature')] = {
                **payload, 'id': 'sensor-temperature', 'name_id': payload['id'],
            }
            after = probe(1)
            self.assertEqual((after['status'], len(after['metrics'])), ('online', 1))
            self.assertEqual(after['point_errors'], [{'label': 'door', 'error': 'Device returned HTTP 404.'}])
            self.assertEqual(next(iter(after['metrics'])), next(iter(before['metrics'])))
            changed = probe(1, points=[{**ESP_TEMPERATURE, 'unit': '°F'}])
            self.assertNotEqual(next(iter(changed['metrics'])), next(iter(after['metrics'])))
            for invalid in ({**payload, 'id': 'sensor/Other'}, {**payload, 'value': None},
                            {**payload, 'value': True}, {**payload, 'value': float('nan')}):
                peer.routes[('10.23.0.42', '/sensor/Cold%20Room/Temp%C3%A9rature')] = invalid
                self.assertEqual(probe(1)['metrics'], {})
            peer.routes[('10.23.0.41', '/cm?cmnd=Status%2010')] = {'StatusSNS': {
                'ENERGY': {'Power': [10, 20], 'Today': True, 'Voltage': float('inf')},
                'AM2301': {'Temperature': 15, 'Humidity': 110},
            }}
            tasmota = probe(0)
            self.assertEqual(tasmota['status'], 'online')
            self.assertFalse(any(key.startswith(('tasmota_sensor', 'tasmota_power', 'tasmota_voltage',
                                                  'tasmota_daily')) for key in tasmota['metrics']))

    def test_solar_rejects_stale_totals_and_api_error_responses(self):
        peer = FixtureDevices()
        with serve(peer), peer.redirect():
            first = OPENDTU_STATUS['inverters'][0]
            route = ('10.23.0.45', '/api/livedata/status')
            for inverters in ([], [{**first, 'poll_enabled': False}], [{**first, 'reachable': False}],
                              [first, {**first, 'data_age': 121}], [{**first, 'data_age': None}]):
                peer.routes[route] = {**OPENDTU_STATUS, 'inverters': inverters}
                result = probe(4)
                self.assertEqual((result['status'], result['metrics']), ('reachable', {}))
                self.assertNotIn('last_telemetry', result)
            peer.routes[route] = {**OPENDTU_STATUS, 'hints': {'radio_problem': True}}
            self.assertEqual(probe(4)['metrics'], {})
            peer.routes[route] = {**OPENDTU_STATUS, 'total': {'Power': {'v': 10, 'u': 'kW'}}}
            self.assertEqual(probe(4)['metrics'], {})
            peer.routes[('10.23.0.44', '/solar_api/v1/GetPowerFlowRealtimeData.fcgi')] = {
                **FRONIUS_FLOW, 'Head': {'Status': {'Code': 255}},
            }
            self.assertEqual(probe(3)['metrics'], {})

    def test_collector_and_dashboard_api_store_all_five_and_keep_last_known_data(self):
        peer = FixtureDevices()
        with tempfile.TemporaryDirectory() as folder, serve(peer), peer.redirect(), patch.object(
            server, 'network_info', return_value={}
        ), ExitStack() as cleanup:
            store = server.Store(Path(folder, 'test.sqlite3'))
            cleanup.callback(store.db.close)
            collector = server.Collector(store)
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store, httpd.collector = store, collector
            with serve(httpd) as port:
                def request(path, method='GET', data=None):
                    connection = HTTPConnection('127.0.0.1', port, timeout=3)
                    try:
                        headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
                        connection.request(method, path, json.dumps(data) if data else None, headers)
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                with patch.object(collector, 'start_poll'):
                    for protocol, ip, _, config, _ in CASES:
                        self.assertEqual(request('/api/devices', 'POST', {'ip': ip, 'name': protocol,
                            'monitor_config': {'protocol': protocol, 'port': 8080, **config}})[0], 201)
                with patch.object(server, 'ping') as ping, patch.object(server, 'read_json') as fallback:
                    collector.poll()
                    ping.assert_not_called()
                    fallback.assert_not_called()
                devices = {d['protocol']: d for d in json.loads(request('/api/state')[1])['devices']}
                for protocol, _, _, _, metric in CASES:
                    device = devices[protocol]
                    self.assertEqual(device['status'], 'online')
                    metric = metric or next(iter(device['metrics']))
                    device_filter = f"device_id={device['device_id']}"
                    status, history = request(f'/api/history?{device_filter}&metric={metric}')
                    self.assertEqual(status, 200)
                    self.assertEqual([row['value'] for row in json.loads(history)['samples']], [0])
                    status, exported = request(f'/api/export.csv?{device_filter}')
                    self.assertEqual(status, 200)
                    self.assertIn(metric, {row['metric'] for row in csv.DictReader(io.StringIO(exported.decode('utf-8-sig')))})
                reopened = server.Store(store.path)
                with closing(reopened.db):
                    self.assertEqual({d['protocol']: d['monitor_config'] for d in reopened.devices()},
                                     {p: d['monitor_config'] for p, d in devices.items()})
                old = devices['opendtu']
                peer.routes[(old['ip'], '/api/livedata/status')] = {**OPENDTU_STATUS, 'inverters': []}
                collector.poll()
                current = next(d for d in json.loads(request('/api/state')[1])['devices'] if d['ip'] == old['ip'])
                self.assertEqual(current['metrics'], {})
                self.assertEqual(current['last_metrics'], old['metrics'])
                self.assertEqual(current['last_telemetry'], old['last_telemetry'])
