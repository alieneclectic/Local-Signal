#!/usr/bin/env python3
"""Opt-in device → collector → SQLite → HTTP/CSV tests, with a browser preview.

python3 tools/commercial_e2e.py             # Local BACpypes3 only
python3 tools/commercial_e2e.py --pult      # Also contact the documented public demo
python3 tools/commercial_e2e.py --pult --serve

No production validator, setting, environment variable, or installed database is
changed. Test-process socket facades map two private inventory aliases to fixed
test endpoints. There is no arbitrary public target option. See the test guide.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import csv
from datetime import datetime, timezone
import http.client
import importlib.metadata
import io
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import bacnet
import modbus
import server

BACNET_ALIAS = '10.255.250.2'
PULT_ALIAS = '10.255.250.1'
PULT_ENDPOINT = ('45.8.248.56', 502)
MAX_PUBLIC_REQUESTS = 24
DEVICE_INSTANCE = 54321
REAL_SOCKET = socket.socket
REAL_CONNECT = socket.create_connection
REAL_PROBE = server.probe_device
NETWORK = {'local_ip': None, 'subnet': None, 'gateway': None, 'interface': None}


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class GuardedSocket:
    """Keep read-only packet guards at the last boundary before real I/O."""
    def __init__(self, sock, transport, owner=None, endpoint=None):
        self.sock, self.transport, self.owner, self.endpoint = sock, transport, owner, endpoint

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.sock.close()

    def __getattr__(self, key):
        return getattr(self.sock, key)

    def connect(self, address):
        if self.transport != 'bacnet' or address != (BACNET_ALIAS, 47808):
            raise OSError('E2E blocked an unapproved BACnet destination.')
        self.sock.connect(self.endpoint)

    def send(self, packet):
        if self.transport != 'bacnet' or len(packet) != 17 or packet[:8] != bytes.fromhex('810a001101040003') or packet[9:11] != b'\x0c\x0c':
            raise OSError('E2E permits only unicast BACnet ReadProperty packets.')
        self.owner.requests += 1
        return self.sock.send(packet)

    def sendall(self, packet):
        if self.transport != 'modbus' or len(packet) != 12:
            raise OSError('E2E blocked a non-read Modbus packet.')
        _, protocol, length, unit, function, address, count = struct.unpack('!HHHBBHH', packet)
        # The public smoke test reads only these documented, generated inputs.
        if (protocol, length, unit, function, address, count) not in ((0, 6, 1, 4, 1, 1), (0, 6, 1, 2, 1, 1)):
            raise OSError('E2E permits only the fixed Pult input-register/discrete-input reads.')
        with self.owner.lock:
            if self.owner.requests >= MAX_PUBLIC_REQUESTS:
                raise OSError('E2E public request budget exhausted; restart explicitly to test again.')
            self.owner.requests += 1
        return self.sock.sendall(packet)


class BacnetSockets:
    AF_INET, SOCK_DGRAM = socket.AF_INET, socket.SOCK_DGRAM

    def __init__(self, port):
        self.port, self.requests = port, 0

    def socket(self, family, kind):
        if (family, kind) != (socket.AF_INET, socket.SOCK_DGRAM):
            raise OSError('E2E BACnet transport is IPv4 UDP only.')
        return GuardedSocket(REAL_SOCKET(family, kind), 'bacnet', self, ('127.0.0.1', self.port))


class PultSockets:
    def __init__(self, enabled):
        self.enabled, self.online = enabled, True
        self.requests, self.connections = 0, 0
        self.lock = threading.Lock()

    def create_connection(self, address, timeout):
        if not self.enabled or not self.online:
            raise ConnectionRefusedError('Pult access disabled in this test process.')
        if address != (PULT_ALIAS, 502):
            raise OSError('E2E blocked an unapproved Modbus destination.')
        with self.lock:
            if self.requests >= MAX_PUBLIC_REQUESTS:
                raise OSError('E2E public request budget exhausted.')
            self.connections += 1
        return GuardedSocket(REAL_CONNECT(PULT_ENDPOINT, timeout=timeout), 'modbus', self)


def guarded_probe(device):
    expected = {BACNET_ALIAS: 'bacnet-ip', PULT_ALIAS: 'modbus-tcp'}
    if device.get('ip') not in expected or device.get('monitor_config', {}).get('protocol') != expected[device['ip']]:
        raise OSError('E2E collection permits only its two configured fixtures.')
    return REAL_PROBE(device)


class BacnetSimulator:
    """Independent BACpypes3 server, bound only to loopback, with mutable objects."""
    def __init__(self):
        with REAL_SOCKET(socket.AF_INET, socket.SOCK_DGRAM) as reservation:
            reservation.bind(('127.0.0.1', 0))
            self.port = reservation.getsockname()[1]
        self.loop = self.thread = self.app = None

    async def open(self):
        from bacpypes3.argparse import SimpleArgumentParser
        from bacpypes3.app import Application
        from bacpypes3.object import AnalogInputObject, BinaryInputObject, MultiStateValueObject
        args = SimpleArgumentParser().parse_args(['--address', f'127.0.0.1:{self.port}', '--instance', str(DEVICE_INSTANCE)])
        self.app = Application.from_args(args)
        self.temperature = AnalogInputObject(objectIdentifier=('analog-input', 1), objectName='E2E supply air',
            presentValue=22.5, units='degrees-celsius', statusFlags=[0, 0, 0, 0], outOfService=False, eventState='normal')
        self.binary = BinaryInputObject(objectIdentifier=('binary-input', 2), objectName='E2E fan',
            presentValue='inactive', statusFlags=[0, 0, 0, 0], outOfService=False, eventState='normal', polarity='normal')
        self.mode = MultiStateValueObject(objectIdentifier=('multi-state-value', 3), objectName='E2E mode',
            presentValue=2, statusFlags=[0, 0, 0, 0], outOfService=False, eventState='normal', numberOfStates=3)
        for obj in (self.temperature, self.binary, self.mode):
            self.app.add_object(obj)
        await asyncio.sleep(.05)  # Allow asyncio UDP endpoint creation to finish.

    def start(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self.open(), self.loop).result(timeout=5)
        except BaseException:
            self.close()
            raise

    def set_values(self, temperature=22.5, binary=False, mode=2, fault=False, out_of_service=False):
        async def update():
            self.temperature.presentValue = temperature
            self.temperature.statusFlags = [0, int(fault), 0, int(out_of_service)]
            self.temperature.outOfService = out_of_service
            self.binary.presentValue = 'active' if binary else 'inactive'
            self.mode.presentValue = mode
        asyncio.run_coroutine_threadsafe(update(), self.loop).result(timeout=3)

    def close(self):
        if self.loop is None:
            return
        async def finish():
            if self.app:
                self.app.close()
                self.app = None
            await asyncio.sleep(.05)
        try:
            asyncio.run_coroutine_threadsafe(finish(), self.loop).result(timeout=3)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=3)
            self.loop.close()
            self.loop = self.thread = None


class TestCollector(server.Collector):
    def __init__(self, store):
        super().__init__(store)
        self.enabled, self.cycles = False, 0
        self.condition = threading.Condition()
        self.workers, self.cycle_lock = [], threading.Lock()

    def start_poll(self):
        if not self.enabled:
            return
        def collect():
            with self.cycle_lock:
                if self.stop.is_set():
                    return
                super(TestCollector, self).poll()
                with self.condition:
                    self.cycles += 1
                    self.condition.notify_all()
        worker = threading.Thread(target=collect, daemon=True)
        self.workers.append(worker)
        worker.start()


class TestHandler(server.Handler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        if self.path == '/__e2e__/bacnet':
            try:
                data = self.mutation()  # Retain normal host/origin/request-header checks.
                if set(data) != {'scenario'} or data['scenario'] not in ('healthy', 'changed', 'zero', 'fault', 'out_of_service', 'offline', 'recover'):
                    raise ValueError('Choose a documented E2E BACnet scenario.')
                self.server.suite.scenario(data['scenario'])
                self.collector.start_poll()
                return self.send_json({'ok': True, 'scenario': data['scenario']}, 202)
            except (ValueError, TypeError) as exc:
                return self.send_json({'error': str(exc)}, 400)
        if self.path not in ('/api/devices', '/api/refresh'):
            return self.send_json({'error': 'This action is disabled in the isolated E2E server.'}, 403)
        super().do_POST()


class Dashboard:
    def __init__(self, path):
        self.path = path
        self.store = server.Store(path)
        self.collector = TestCollector(self.store)
        self.httpd = server.LocalHTTPServer(('127.0.0.1', 0), TestHandler)
        self.httpd.store, self.httpd.collector = self.store, self.collector
        self.port = self.httpd.server_port
        self.url = f'http://127.0.0.1:{self.port}'
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.collector.enabled = False
        self.collector.stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        for worker in self.collector.workers:
            worker.join(timeout=12)
            require(not worker.is_alive(), 'Collector failed to stop before database close.')
        self.store.db.close()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            connection.request(method, path, json.dumps(body) if body is not None else None,
                headers={'Content-Type': 'application/json', 'X-Hearth-Request': '1', **(headers or {})})
            response = connection.getresponse()
            status, raw = response.status, response.read()
            data = json.loads(raw) if 'application/json' in response.getheader('Content-Type', '') else raw
            return status, data
        finally:
            connection.close()

    def state(self):
        status, data = self.request('/api/state')
        require(status == 200, f'State API returned {status}.')
        return data

    def device(self, ip):
        return next(d for d in self.state()['devices'] if d['ip'] == ip)

    def poll(self):
        # Separate observations across wall-clock seconds for history assertions.
        previous = self.collector.state['last_poll']
        while previous and server.now() == previous:
            time.sleep(.05)
        self.collector.enabled = True
        before = self.collector.cycles
        require(self.request('/api/refresh', 'POST', {})[0] == 202, 'Refresh API failed.')
        with self.collector.condition:
            require(self.collector.condition.wait_for(lambda: self.collector.cycles > before, timeout=15), 'Collector cycle timed out.')
        require(not self.collector.state['collection_error'], f'Collection failed: {self.collector.state["collection_error"]}')
        return self.state()


def bacnet_config():
    return {'protocol': 'bacnet-ip', 'device_instance': DEVICE_INSTANCE, 'port': 47808, 'points': [
        {'object_type': 'analog-input', 'instance': 1, 'label': 'Supply air temperature'},
        {'object_type': 'binary-input', 'instance': 2, 'label': 'Fan running'},
        {'object_type': 'multi-state-value', 'instance': 3, 'label': 'Operating mode'}]}


def pult_config():
    return {'protocol': 'modbus-tcp', 'unit_id': 1, 'port': 502, 'points': [
        {'table': 'input', 'address': 1, 'data_type': 'uint16', 'label': 'Pult generated input counter'},
        {'table': 'discrete', 'address': 1, 'label': 'Pult generated discrete input'}]}


def private_guards():
    for validate in (server.local_ip, bacnet.private_address, modbus.private_address):
        for address in (PULT_ENDPOINT[0], '8.8.8.8', '127.0.0.1', '169.254.169.254'):
            try:
                validate(address)
            except ValueError:
                continue
            raise AssertionError(f'{validate.__module__} accepted prohibited address {address}.')


class Suite:
    def __init__(self, public=False):
        self.public, self.dashboard = public, None
        self.simulator = BacnetSimulator()
        self.bacnet_sockets = BacnetSockets(self.simulator.port)
        self.pult_sockets = PultSockets(public)
        self.report = {'started_at': server.now(), 'status': 'running', 'checks': [],
                       'scope': 'device packets → collector → temporary SQLite → HTTP state/history/CSV',
                       'pult_enabled': public, 'pult_endpoint': list(PULT_ENDPOINT), 'hardware_verified': False}

    def check(self, name, action):
        try:
            detail = action()
        except Exception as exc:
            self.report['checks'].append({'name': name, 'status': 'failed', 'detail': f'{type(exc).__name__}: {exc}'})
            print(f'FAIL {name}: {exc}', flush=True)
            raise
        self.report['checks'].append({'name': name, 'status': 'passed', 'detail': detail})
        print(f'PASS {name}', flush=True)

    def values(self):
        return {reading['label']: reading['value'] for reading in self.dashboard.device(BACNET_ALIAS)['metrics'].values()}

    def expected_bacnet(self, temperature, binary, mode):
        self.dashboard.poll()
        require(self.values() == {'Supply air temperature': temperature, 'Fan running': binary, 'Operating mode': mode},
                f'Unexpected BACnet values: {self.values()}')
        return self.values()

    def scenario(self, name):
        # Test-only controls: serialize simulator changes with active collection.
        with self.dashboard.collector.cycle_lock:
            if name == 'offline':
                self.simulator.close()
                return
            if self.simulator.loop is None:
                self.simulator.start()
            values = {'changed': {'temperature': 24, 'binary': True, 'mode': 3},
                      'zero': {'temperature': 0, 'mode': 1}, 'fault': {'fault': True},
                      'out_of_service': {'out_of_service': True}}.get(name, {})
            self.simulator.set_values(**values)

    def api_guards(self):
        private_guards()
        body = {'ip': PULT_ENDPOINT[0], 'name': 'Must be rejected', 'monitor_config': pult_config()}
        status, data = self.dashboard.request('/api/devices', 'POST', body)
        require(status == 400 and 'private' in data['error'].lower(), 'Normal device API allowed a public target.')
        require(not self.dashboard.state()['devices'], 'Rejected target created inventory data.')
        require(self.dashboard.request('/api/refresh', 'POST', {}, {'Origin': 'https://invalid.example'})[0] == 400, 'Origin guard failed.')
        require(self.dashboard.request('/api/refresh', 'POST', {}, {'X-Hearth-Request': ''})[0] == 400, 'Request-header guard failed.')
        require(self.dashboard.request('/api/scan', 'POST', {})[0] == 403, 'E2E server did not block network scanning.')

    def run_bacnet(self):
        self.check('private-address and HTTP mutation restrictions', self.api_guards)
        status, _ = self.dashboard.request('/api/devices', 'POST', {
            'ip': BACNET_ALIAS, 'name': 'E2E BACnet controller', 'room': 'Test lab', 'monitor_config': bacnet_config()})
        require(status == 201, 'Could not create BACnet device via normal API.')
        self.check('BACnet initial analog/binary/multi-state readings', lambda: self.expected_bacnet(22.5, 0, 2))
        self.simulator.set_values(temperature=24, binary=True, mode=3)
        self.check('BACnet changed values reach HTTP state', lambda: self.expected_bacnet(24, 1, 3))
        self.simulator.set_values(temperature=0, mode=1)
        self.check('BACnet observed zero is a fresh measurement', lambda: self.expected_bacnet(0, 0, 1))
        for fault, out_of_service, name in ((True, False, 'faulted'), (False, True, 'out-of-service')):
            self.simulator.set_values(fault=fault, out_of_service=out_of_service)
            def invalid_point():
                self.dashboard.poll()
                d = self.dashboard.device(BACNET_ALIAS)
                require(d['status'] == 'online' and len(d['metrics']) == 2 and len(d['point_errors']) == 1,
                        'Invalid point did not leave a partial read.')
                require('Supply air temperature' not in self.values(), 'Invalid temperature appeared fresh.')
            self.check(f'BACnet {name} point is unavailable, not zero', invalid_point)
        self.simulator.set_values()
        config = bacnet_config()
        config['points'].append({'object_type': 'analog-input', 'instance': 9999, 'label': 'Missing test point'})
        d = self.dashboard.device(BACNET_ALIAS)
        require(self.dashboard.request(f'/api/devices/{d["device_id"]}', 'PATCH', {'monitor_config': config, 'notes': 'E2E notes survive restart'})[0] == 200, 'Device edit failed.')
        def missing():
            self.dashboard.poll()
            d = self.dashboard.device(BACNET_ALIAS)
            require(len(d['metrics']) == 3 and len(d['point_errors']) == 1 and d['point_errors'][0]['label'] == 'Missing test point', 'Missing point handling failed.')
        self.check('BACnet missing object leaves other points reporting', missing)
        self.simulator.close()
        def offline():
            self.dashboard.poll()
            d = self.dashboard.device(BACNET_ALIAS)
            require(d['status'] == 'unreachable' and not d['metrics'] and d['last_metrics'], 'Stopped simulator did not become offline with historical readings retained.')
        self.check('BACnet real UDP disconnect preserves last-known readings', offline)
        self.simulator.start()
        require(self.dashboard.request(f'/api/devices/{d["device_id"]}', 'PATCH', {'monitor_config': bacnet_config()})[0] == 200, 'Restore map failed.')
        self.check('BACnet recovery after simulator restart', lambda: self.expected_bacnet(22.5, 0, 2))
        self.check('BACnet history and CSV retain changed values and zero', self.verify_history)

    def verify_history(self):
        d = self.dashboard.device(BACNET_ALIAS)
        query = f'device_id={d["device_id"]}'
        status, history = self.dashboard.request('/api/history?' + query)
        require(status == 200 and history['samples'], 'History API is empty.')
        status, data = self.dashboard.request('/api/export.csv?' + query)
        require(status == 200, 'CSV API failed.')
        rows = list(csv.DictReader(io.StringIO(data.decode('utf-8-sig'))))
        temperatures = [float(row['value']) for row in rows if row['label'] == 'Supply air temperature']
        require({0, 22.5, 24}.issubset(temperatures), f'CSV lost observed changes: {temperatures}')
        require(not any(row['label'] == 'Missing test point' for row in rows), 'Missing point created a fabricated sample.')
        with self.dashboard.store.lock:
            count = self.dashboard.store.db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]
        require(count == len(rows), 'API export does not match stored observations.')
        return {'stored_samples': count, 'temperature_values_in_csv': temperatures}

    def restart_dashboard(self):
        previous = self.dashboard.device(BACNET_ALIAS)
        path = self.dashboard.path
        self.dashboard.close()
        self.dashboard = Dashboard(path)
        self.dashboard.httpd.suite = self
        current = self.dashboard.device(BACNET_ALIAS)
        for key in ('device_id', 'name', 'room', 'notes', 'monitor_config', 'last_telemetry'):
            require(previous[key] == current[key], f'Restart changed {key}.')
        require(current['notes'] == 'E2E notes survive restart', 'Notes were lost.')
        self.verify_history()
        self.expected_bacnet(22.5, 0, 2)

    def run_pult(self):
        self.dashboard.collector.enabled = False
        status, _ = self.dashboard.request('/api/devices', 'POST', {
            'ip': PULT_ALIAS, 'name': 'E2E Pult.Online public demo', 'room': 'External test sandbox', 'monitor_config': pult_config()})
        require(status == 201, 'Could not create private Pult fixture alias.')
        def live_read():
            self.dashboard.poll()
            d = self.dashboard.device(PULT_ALIAS)
            require(d['status'] == 'online' and len(d['metrics']) == 2 and not d['point_errors'],
                    f'Pult read unavailable: {d.get("error") or d.get("point_errors")}')
            values = {m['label']: m['value'] for m in d['metrics'].values()}
            counter, bit = values['Pult generated input counter'], values['Pult generated discrete input']
            require(math.isfinite(counter) and 0 <= counter <= 65535 and bit in (0, 1), 'Invalid Pult input values.')
            return values
        try:
            self.check('Pult live generated input register and discrete input', live_read)
        except AssertionError:
            self.report['pult_unavailable'] = True
            raise
        time.sleep(1.1)  # Modest public reads; the documented input counter advances each second.
        self.check('Pult second live observation', live_read)
        d = self.dashboard.device(PULT_ALIAS)
        query = f'device_id={d["device_id"]}'
        def history_and_csv():
            status, history = self.dashboard.request('/api/history?' + query)
            require(status == 200 and history['samples'], 'Pult history API is empty.')
            status, csv_data = self.dashboard.request('/api/export.csv?' + query)
            require(status == 200, 'Pult CSV export failed.')
            rows = list(csv.DictReader(io.StringIO(csv_data.decode('utf-8-sig'))))
            require(len(rows) == 4 and {row['label'] for row in rows} == {'Pult generated input counter', 'Pult generated discrete input'},
                    'Pult observations were not persisted and exported.')
            return {'exported_samples': len(rows)}
        self.check('Pult observations persist through history and CSV', history_and_csv)
        self.pult_sockets.online = False
        def offline():
            self.dashboard.poll()
            d = self.dashboard.device(PULT_ALIAS)
            require(d['status'] == 'unreachable' and not d['metrics'] and d['last_metrics'], 'Injected Pult connection failure did not retain historical readings.')
        self.check('Pult injected transport failure does not fabricate zero', offline)
        self.pult_sockets.online = True
        self.check('Pult recovery through collector and HTTP API', live_read)

    def run(self, serve=False):
        with tempfile.TemporaryDirectory(prefix='local-signal-e2e-') as folder, ExitStack() as stack:
            # These patch module references only in this standalone test process.
            stack.enter_context(patch.object(server, 'network_info', return_value=NETWORK.copy()))
            stack.enter_context(patch.object(server, 'probe_device', side_effect=guarded_probe))
            stack.enter_context(patch.object(bacnet, 'socket', self.bacnet_sockets))
            stack.enter_context(patch.object(modbus, 'socket', self.pult_sockets))
            self.simulator.start()
            try:
                self.dashboard = Dashboard(Path(folder) / 'test.sqlite3')
                self.dashboard.httpd.suite = self
                self.run_bacnet()
                self.check('database reopen preserves inventory and history', self.restart_dashboard)
                if self.public:
                    self.run_pult()
                else:
                    self.report['checks'].append({'name': 'Pult public demo', 'status': 'skipped', 'detail': 'Use --pult to opt in to the fixed external test endpoint.'})
                self.check('production address restrictions remain enforced', private_guards)
                self.report['status'] = 'passed'
                if serve:
                    print(f'PREVIEW_URL={self.dashboard.url}/#devices/commercial', flush=True)
                    print('Temporary test dashboard only. Refresh reads fixtures; scans/vendor lookups are disabled. Ctrl-C stops it.', flush=True)
                    try:
                        while True:
                            time.sleep(.5)
                    except KeyboardInterrupt:
                        pass
            finally:
                if self.dashboard:
                    self.dashboard.close()
                self.simulator.close()
                self.report.update(finished_at=server.now(), bacnet_read_requests=self.bacnet_sockets.requests,
                                   pult_read_requests=self.pult_sockets.requests, pult_connections=self.pult_sockets.connections)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pult', action='store_true', help='Opt in to at most 24 read-only input requests to the fixed Pult.Online test server.')
    parser.add_argument('--serve', action='store_true', help='Keep the temporary dashboard open for browser verification after passing.')
    parser.add_argument('--report', type=Path, help='Write a JSON result report (default: a unique file in the system temporary directory).')
    args = parser.parse_args(argv)
    try:
        version = importlib.metadata.version('bacpypes3')
    except importlib.metadata.PackageNotFoundError:
        parser.error('Install the optional simulator first: python3 -m pip install -r requirements-e2e.txt')
    suite = Suite(args.pult)
    suite.report['bacpypes3_version'] = version
    report_path = args.report
    if report_path is None:
        fd, name = tempfile.mkstemp(prefix='local-signal-e2e-', suffix='.json')
        os.close(fd)
        report_path = Path(name)
    exit_code = 0
    try:
        suite.run(args.serve)
    except Exception as exc:
        exit_code = 2 if suite.report.get('pult_unavailable') else 1
        suite.report.update(status='external_unavailable' if exit_code == 2 else 'failed', error=f'{type(exc).__name__}: {exc}')
        print(suite.report['error'], file=sys.stderr)
    finally:
        # Also check after transport patch restoration, not just inside the suite.
        try:
            private_guards()
            require(bacnet.socket is socket and modbus.socket is socket and server.probe_device is REAL_PROBE,
                    'Test transport patches were not restored.')
            suite.report['checks'].append({'name': 'production guards and transports after cleanup', 'status': 'passed'})
        except Exception as exc:
            exit_code = 1
            suite.report.update(status='failed', error=f'Cleanup guard failure: {exc}')
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(suite.report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        print(f'REPORT={report_path.resolve()}', flush=True)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
