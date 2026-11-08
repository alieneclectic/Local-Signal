"""IPP and NUT fixtures over loopback; private target validation stays enabled."""
from contextlib import contextmanager
import csv
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import background
import ipp
import nut
import server

HTTPConnection = http.client.HTTPConnection
create_connection = socket.create_connection
IP = '10.23.0.41'
STAMP = '2026-11-08T12:00:00+00:00'


def attribute(tag, name, value):
    name = name.encode('ascii')
    return bytes([tag]) + struct.pack('!H', len(name)) + name + struct.pack('!H', len(value)) + value


def printer_response(identity):
    return (struct.pack('!BBHI', 1, 1, 0, identity) + b'\x04'
            + attribute(0x23, 'printer-state', struct.pack('!i', 3))
            + attribute(0x21, 'queued-job-count', struct.pack('!i', 0))
            + attribute(0x22, 'printer-is-accepting-jobs', b'\x01')
            + attribute(0x42, 'printer-make-and-model', b'Synthetic Office Printer')
            + attribute(0x44, 'printer-state-reasons', b'media-low-warning')
            + attribute(0x42, 'marker-names', b'Black') + attribute(0x42, '', b'Cyan')
            + attribute(0x42, '', b'Magenta')
            + attribute(0x21, 'marker-levels', struct.pack('!i', 0))
            + attribute(0x21, '', struct.pack('!i', -3))
            + attribute(0x21, '', struct.pack('!i', 85)) + b'\x03')


class PrinterPeer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.requests = []
        self.status, self.content_type, self.modify = 200, 'application/ipp', lambda body: body
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                peer.requests.append((self.command, self.path, dict(self.headers), body))
                identity = struct.unpack('!I', body[4:8])[0]
                reply = peer.modify(printer_response(identity))
                self.send_response(peer.status)
                self.send_header('Content-Type', peer.content_type)
                self.send_header('Content-Length', str(len(reply)))
                self.send_header('Location', 'http://example.invalid/never-follow')
                self.end_headers()
                try:
                    for start in range(0, len(reply), 7):
                        self.wfile.write(reply[start:start+7])
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()

    @contextmanager
    def redirect(self):
        def connect(address, port, **kwargs):
            assert address == IP and port == 631
            return HTTPConnection('127.0.0.1', self.server_port, **kwargs)
        with patch.object(ipp.http.client, 'HTTPConnection', side_effect=connect):
            yield

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(2)


NUT_REPLY = ('BEGIN LIST VAR office-ups\nVAR office-ups ups.status "OL CHRG"\n'
             'VAR office-ups battery.charge "0"\nVAR office-ups battery.runtime "600"\n'
             'VAR office-ups ups.load "25"\nVAR office-ups input.voltage "230.4"\n'
             'VAR office-ups output.voltage "nan"\nVAR office-ups ups.temperature "35"\n'
             'VAR office-ups battery.temperature "-1"\nVAR office-ups ups.mfr "Synthetic"\n'
             'VAR office-ups ups.model "Office UPS"\nVAR office-ups ups.serial "not-stored"\n'
             'END LIST VAR office-ups\n').encode()


class UPSPeer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self):
        self.requests, self.reply = [], NUT_REPLY
        peer = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(1)
                peer.requests.append(self.rfile.readline(1024))
                try:
                    for start in range(0, len(peer.reply), 3):
                        self.wfile.write(peer.reply[start:start+3])
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        super().__init__(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=lambda: self.serve_forever(poll_interval=.01), daemon=True)
        self.thread.start()

    @contextmanager
    def redirect(self):
        def connect(address, **kwargs):
            assert address == (IP, 3493)
            return create_connection(self.server_address, **kwargs)
        with patch.object(nut, 'socket', SimpleNamespace(create_connection=connect)):
            yield

    def close(self):
        self.shutdown()
        self.server_close()
        self.thread.join(2)


class OfficeReaderTests(unittest.TestCase):
    def test_configuration_is_strict_and_packaged(self):
        for module, config in ((ipp, {'protocol': 'ipp'}), (nut, {'protocol': 'nut-ups', 'ups_name': 'office-ups'})):
            normalized = server.validate_monitor_config(config)
            self.assertEqual(server.validate_monitor_config(normalized), normalized)
            for extra in ({'port': True}, {'port': 0}, {'port': '631'}, {'command': 'shutdown'}):
                with self.subTest(module=module.__name__, extra=extra), self.assertRaises(ValueError):
                    server.validate_monitor_config({**config, **extra})
            self.assertIn(module.__name__ + '.py', background.FILES)
        for path in ('http://public.example/', '/x?host=public', '/x\r\nHost:x', '/'+('x'*256)):
            with self.assertRaises(ValueError):
                ipp.validate_config({'protocol': 'ipp', 'path': path})
        for name in ('', 'ups\nFSD ups', 'ups name', 'ups"', '../ups'):
            with self.assertRaises(ValueError):
                nut.validate_config({'protocol': 'nut-ups', 'ups_name': name})
        self.assertIn('device_io.py', background.FILES)

    def test_private_addresses_are_validated_before_transport(self):
        for module, config in ((ipp, {'protocol': 'ipp'}), (nut, {'protocol': 'nut-ups', 'ups_name': 'office-ups'})):
            for address in ('127.0.0.1', '169.254.1.2', '8.8.8.8', '192.168.1.255', '10.0.0.0', '::1', 'printer.local'):
                with self.subTest(module=module.__name__, address=address), self.assertRaises(ValueError):
                    module.read({'ip': address, 'monitor_config': config}, STAMP)

    def test_ipp_request_is_read_only_and_zero_and_unknown_supplies_stay_distinct(self):
        peer = PrinterPeer()
        try:
            with peer.redirect():
                result = ipp.read({'ip': IP, 'monitor_config': {'protocol': 'ipp'}}, STAMP)
            self.assertEqual((result['status'], result['kind'], result['model']), ('online', 'printer', 'Synthetic Office Printer'))
            self.assertEqual(result['metrics']['ipp_queued_jobs']['value'], 0)
            self.assertEqual(result['metrics']['ipp_accepting_jobs']['kind'], 'boolean')
            supplies = [m for k, m in result['metrics'].items() if k.startswith('ipp_supply_')]
            self.assertEqual([(m['label'], m['value']) for m in supplies], [('Black', 0), ('Magenta', 85)])
            method, path, headers, body = peer.requests[0]
            self.assertEqual((method, path, headers['Content-Type']), ('POST', '/ipp/print', 'application/ipp'))
            self.assertEqual(struct.unpack('!H', body[2:4])[0], 11)  # Get-Printer-Attributes, not a print operation.
            self.assertIn(b'ipp://10.23.0.41:631/ipp/print', body)
            self.assertNotIn('Authorization', headers)
            self.assertEqual(len(peer.requests), 1)
        finally:
            peer.close()

    def test_ipp_errors_redirects_and_wrong_content_never_become_fresh_metrics(self):
        peer = PrinterPeer()
        try:
            scenarios = [(302, 'application/ipp', 'reachable'), (401, 'application/ipp', 'auth_required'),
                         (200, 'text/html', 'reachable')]
            for status, content, expected in scenarios:
                peer.status, peer.content_type = status, content
                with peer.redirect():
                    result = ipp.read({'ip': IP, 'monitor_config': {'protocol': 'ipp'}}, STAMP)
                self.assertEqual(result['status'], expected)
                self.assertEqual(result['metrics'], {})
                self.assertNotIn('last_telemetry', result)
            self.assertEqual(len(peer.requests), len(scenarios))
        finally:
            peer.close()

    def test_ipp_tls_uses_certificate_verification(self):
        with patch.object(ipp.http.client, 'HTTPSConnection', side_effect=OSError('fixture')) as connect:
            ipp.read({'ip': IP, 'monitor_config': {'protocol': 'ipp', 'tls': True}}, STAMP)
        context = connect.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)

    def test_ipp_rejects_malformed_bounded_responses_and_ignores_no_value(self):
        valid = printer_response(7)
        invalid = [b'', valid[:-1], valid+b'document', valid[:4]+struct.pack('!I', 8)+valid[8:],
                   valid[:8]+b'\x04\x21\xff\xff', b'x'*(ipp.MAX_RESPONSE+1)]
        for raw in invalid:
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                ipp.parse(raw, 7)
        absent = struct.pack('!BBHI', 1, 1, 0, 7)+b'\x04'+attribute(0x13, 'printer-state', b'')+b'\x03'
        self.assertEqual(ipp.parse(absent, 7), {})
        with self.assertRaises(ValueError):
            ipp.normalize(ipp.parse(absent, 7))
        for status in (0x0401, 0x0402, 0x0403):
            with self.assertRaises(PermissionError):
                ipp.parse(struct.pack('!BBHI', 1, 1, status, 7)+b'\x03', 7)

    def test_nut_fragmented_read_has_no_control_commands_or_stored_serials(self):
        peer = UPSPeer()
        try:
            with peer.redirect():
                result = nut.read({'ip': IP, 'monitor_config': {'protocol': 'nut-ups', 'ups_name': 'office-ups'}}, STAMP)
            self.assertEqual(peer.requests, [b'LIST VAR office-ups\n'])
            self.assertEqual((result['status'], result['model']), ('online', 'Synthetic Office UPS'))
            self.assertEqual(result['metrics']['ups_battery_pct']['value'], 0)
            self.assertEqual(result['metrics']['ups_runtime_min']['value'], 10)
            self.assertEqual(result['metrics']['ups_state']['value'], 'On line · Charging')
            self.assertEqual(result['metrics']['ups_battery_c']['value'], -1)  # A valid temperature, not a sentinel.
            self.assertNotIn('ups_output_v', result['metrics'])
            self.assertNotIn('not-stored', json.dumps(result))
            self.assertNotIn('temperature_c', result['metrics'])
        finally:
            peer.close()

    def test_nut_protocol_quotes_are_not_shell_quotes(self):
        self.assertEqual(nut.tokens('VAR ups ups.model "Vendor\'s \\"UPS\\" \\\\ unit" #comment'),
                         ['VAR', 'ups', 'ups.model', 'Vendor\'s "UPS" \\ unit'])
        for raw in ('VAR "unterminated', 'VAR a\\', ' '.join(['x']*9)):
            with self.assertRaises(ValueError):
                nut.tokens(raw)

    def test_nut_stale_auth_mismatch_duplicates_and_oversized_lines_fail_closed(self):
        peer = UPSPeer()
        try:
            for reply, status in ((b'ERR DATA-STALE\n', 'reachable'), (b'ERR ACCESS-DENIED\n', 'auth_required'),
                                  (b'BEGIN LIST VAR another-ups\n', 'reachable'),
                                  (NUT_REPLY.replace(b'END LIST', b'VAR office-ups battery.charge "25"\nEND LIST'), 'reachable'),
                                  (b'x'*4097+b'\n', 'reachable'), (NUT_REPLY[:-5], 'reachable')):
                peer.reply = reply
                with peer.redirect():
                    result = nut.read({'ip': IP, 'monitor_config': {'protocol': 'nut-ups', 'ups_name': 'office-ups'}}, STAMP)
                self.assertEqual(result['status'], status)
                self.assertEqual(result['metrics'], {})
                self.assertNotIn('last_telemetry', result)
        finally:
            peer.close()
        self.assertEqual(nut.normalize({'battery.charge': '-1', 'ups.load': 'inf', 'battery.runtime': 'NaN'}), {})

    def test_nut_total_deadline_and_response_limits(self):
        class Sock:
            sendall = lambda *_: None
            settimeout = lambda *_: None
            recv = lambda *_: b'x'*4096+b'\n'
        with self.assertRaises(TimeoutError):
            nut.read_variables(Sock(), 'office-ups', time.monotonic()-1)
        with patch.object(nut, 'MAX_RESPONSE', 32), self.assertRaisesRegex(ValueError, '128 KB'):
            nut.read_variables(Sock(), 'office-ups', time.monotonic()+1)


class OfficeIntegrationTests(unittest.TestCase):
    def test_collector_storage_restart_and_http_history_csv_for_both_readers(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'network_info', return_value={}):
            store = server.Store(Path(folder)/'fixture.sqlite3')
            collector = server.Collector(store)
            httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
            httpd.store, httpd.collector = store, collector
            thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=.01), daemon=True)
            thread.start()
            def request(path, method='GET', data=None, headers=None):
                connection = HTTPConnection('127.0.0.1', httpd.server_address[1], timeout=3)
                connection.request(method, path, body=json.dumps(data) if data else None, headers=headers or {})
                response = connection.getresponse()
                status, body = response.status, response.read()
                connection.close()
                return status, body
            try:
                for protocol, Peer, config, metric, kind in (
                    ('ipp', PrinterPeer, {'protocol': 'ipp'}, 'ipp_queued_jobs', 'printer'),
                    ('nut-ups', UPSPeer, {'protocol': 'nut-ups', 'ups_name': 'office-ups'}, 'ups_battery_pct', 'ups')):
                    peer = Peer()
                    try:
                        # Remove previous target so each poll uses only its loopback transport fixture.
                        with store.lock:
                            store.db.execute('UPDATE devices SET ip=NULL')
                            store.db.commit()
                        headers = {'Content-Type': 'application/json', 'X-Hearth-Request': '1'}
                        payload = {'ip': IP, 'name': 'Office fixture', 'monitor_config': config}
                        with patch.object(collector, 'start_poll') as start:
                            self.assertEqual(request('/api/devices', 'POST', payload)[0], 400)
                            self.assertEqual(request('/api/devices', 'POST', {**payload, 'ip': '8.8.8.8'}, headers)[0], 400)
                            self.assertEqual(request('/api/devices', 'POST', payload, headers)[0], 201)
                            start.assert_called_once()
                        with peer.redirect(), patch.object(server, 'ping') as ping, patch.object(server, 'read_json') as fallback:
                            collector.poll()
                            ping.assert_not_called()
                            fallback.assert_not_called()
                        inventory = json.loads(request('/api/state')[1])['devices']
                        device = next(d for d in inventory if d['ip'] == IP)
                        self.assertEqual((device['kind'], device['protocol'], device['status']), (kind, protocol, 'online'))
                        self.assertEqual(device['metrics'][metric]['value'], 0)
                        history = json.loads(request('/api/history?device_id='+str(device['device_id']))[1])['samples']
                        self.assertIn(0, [r['value'] for r in history if r['metric'] == metric])
                        data = request('/api/export.csv?device_id='+str(device['device_id']))[1]
                        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
                        self.assertIn(metric, [r['metric'] for r in rows])
                        self.assertTrue(any(r['metric'].endswith('_state') for r in rows))
                        reopened = server.Store(store.path)
                        self.assertEqual(next(d for d in reopened.devices() if d['ip'] == IP)['monitor_config']['protocol'], protocol)
                        reopened.db.close()
                        # Failed telemetry moves successful readings into last-known data, never current zeroes.
                        if protocol == 'ipp':
                            peer.status = 503
                        else:
                            peer.reply = b'ERR DATA-STALE\n'
                        with peer.redirect():
                            collector.poll()
                        device = next(d for d in store.devices() if d['ip'] == IP)
                        self.assertEqual(device['metrics'], {})
                        self.assertEqual(device['last_metrics'][metric]['value'], 0)
                    finally:
                        peer.close()
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(2)
                store.db.close()
