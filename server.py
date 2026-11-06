#!/usr/bin/env python3
"""Local Signal is a dependency-free, read-only LAN inventory and telemetry collector."""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
from datetime import datetime, timezone
import errno
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
import webbrowser
import coiot
import network_names
import media_readers
import energy
import reliability
from firmware import checks as firmware_checks
from firmware.adapters import cast, nest, roku, shelly, sonos, wiim
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
WEB_ASSETS = ('/index.html', '/app.js', '/theme.js', '/style.css', '/favicon.svg')
DEV_RELOAD = os.environ.get('LOCAL_SIGNAL_DEV_RELOAD')
POLL_SECONDS = 30
RETENTION_DAYS = 30
ROOM_NAMES = {'first-floor': 'First floor', 'second-floor': 'Second floor'}
MAX_BODY = 16_384
FIRMWARE_CATALOGS = {'shelly': shelly, 'wiim': wiim, 'sonos': sonos, 'nest': nest, 'cast': cast}
FIRMWARE_ADAPTERS = {**FIRMWARE_CATALOGS, 'roku': roku}
FIRMWARE_CATALOG_KEYS = {source: f'{source}_stable_catalog' for source in FIRMWARE_CATALOGS}
FIRMWARE_CATALOG_KEYS['nest'] = 'nest_production_catalog'  # Refresh the old Mini-only cache on upgrade.
RFC1918 = [ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def local_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('Enter a private IPv4 address, such as 192.168.1.44.')
    if address.version != 4 or not any(address in net for net in RFC1918):
        raise ValueError('Only private IPv4 addresses are supported.')
    if address.packed[-1] in (0, 255):
        raise ValueError('Use a device address, not a network or broadcast address.')
    return str(address)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def csv_value(value):
    # Preserve numeric negative readings; escape spreadsheet formulas in device-supplied text.
    if isinstance(value, str) and value.startswith(('=', '+', '-', '@', '\t', '\r')):
        return "'" + value
    return value


def windows_command(program, *args, timeout):
    system32 = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
    return subprocess.run([str(system32 / f'{program}.exe'), *args],
                          capture_output=True, text=True, errors='replace', timeout=timeout,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def windows_default_route():
    routes = windows_command('route', 'print', '-4', timeout=3).stdout
    candidates = []
    for line in routes.splitlines():
        fields = line.split()
        if len(fields) != 5 or fields[:2] != ['0.0.0.0', '0.0.0.0']:
            continue
        try:
            gateway, source = local_ip(fields[2]), local_ip(fields[3])
            candidates.append((int(fields[4]), gateway, source))
        except ValueError:
            continue
    if candidates:
        _, gateway, source = min(candidates)
        return {'gateway': gateway, 'local_ip': source, 'interface': None}
    return {}


def network_info():
    info = {'gateway': None, 'local_ip': None, 'interface': None, 'subnet': None}
    try:
        system = platform.system()
        if system == 'Windows':
            info.update(windows_default_route())
        elif system == 'Darwin':
            out = subprocess.run(['route', '-n', 'get', 'default'], capture_output=True, text=True, timeout=3).stdout
            for key in ('gateway', 'interface'):
                match = re.search(rf'{key}:\s*(\S+)', out)
                if match:
                    info[key] = match.group(1)
            if info['interface']:
                out = subprocess.run(['ipconfig', 'getifaddr', info['interface']], capture_output=True, text=True, timeout=3).stdout.strip()
                info['local_ip'] = local_ip(out)
        else:
            out = subprocess.run(['ip', '-json', 'route', 'show', 'default'], capture_output=True, text=True, timeout=3).stdout
            route = json.loads(out)[0]
            info.update(gateway=route.get('gateway'), interface=route.get('dev'))
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((info['gateway'], 80))
                info['local_ip'] = sock.getsockname()[0]
        # Discovery is deliberately bounded to a /24 even on larger home subnets.
        if info['local_ip']:
            info['subnet'] = str(ipaddress.ip_network(info['local_ip'] + '/24', strict=False))
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return info


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(ip, path, timeout=2):
    # Do not follow redirects to public or unexpected hosts. Never use proxy env settings.
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(f'http://{local_ip(ip)}{path}', headers={'Accept': 'application/json', 'User-Agent': 'Hearth-Local/1.0'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(262_145)
        if len(raw) > 262_144:
            raise ValueError('Device response exceeds 256 KB.')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('Device did not return a JSON object.')
        return value


def ping(ip):
    system = platform.system()
    try:
        if system == 'Windows':
            result = windows_command('ping', '-4', '-n', '1', '-w', '850', ip, timeout=2)
            # Windows can exit successfully for an ICMP unreachable response.
            if not re.search(r'\bTTL=\d+\b', result.stdout, re.I):
                return False, None
        else:
            command = ['ping', '-n', '-c', '1', '-W', '850' if system == 'Darwin' else '1', ip]
            result = subprocess.run(command, capture_output=True, text=True, timeout=2)
        if result.returncode:
            return False, None
        match = re.search(r'\btime[=<]([\d.]+)\s*ms', result.stdout, re.I)
        return True, float(match.group(1)) if match else None
    except (OSError, subprocess.SubprocessError):
        return False, None


def port_open(ip, port=80):
    try:
        with socket.create_connection((ip, port), timeout=.65):
            return True
    except OSError:
        return False


def arp_entries(interface_ip=None):
    try:
        if platform.system() == 'Windows':
            if not interface_ip:
                return {}
            result = windows_command('arp', '-a', '-N', interface_ip, timeout=4)
            pattern = r'^\s*(\d+\.\d+\.\d+\.\d+)\s+((?:[0-9a-f]{2}-){5}[0-9a-f]{2})\s'
        else:
            result = subprocess.run(['arp', '-an'], capture_output=True, text=True, timeout=4)
            pattern = r'\((\d+\.\d+\.\d+\.\d+)\) at ([0-9a-f:]+)'
        found = {}
        for line in result.stdout.splitlines():
            match = re.search(pattern, line, re.I)
            if match:
                try:
                    ip = local_ip(match[1])
                    mac = ':'.join(x.zfill(2) for x in match[2].replace('-', ':').split(':')).upper()
                    if len(mac) == 17 and mac not in ('00:00:00:00:00:00', 'FF:FF:FF:FF:FF:FF'):
                        found[ip] = mac
                except ValueError:
                    pass
        return found
    except (OSError, subprocess.SubprocessError):
        return {}


def normalize(info, status):
    """Return explicitly labeled metrics; never treat a relay's board temperature as room temperature."""
    metrics = {}
    def add(key, value, unit, label, channel=None):
        if number(value):
            metrics[key] = {'value': round(value, 4), 'unit': unit, 'label': label}
            if channel is not None:
                metrics[key]['channel'] = channel
    gen = int(info.get('gen') or 1)
    if gen == 1:
        is_sensor = info.get('type') == 'SHHT-1'
        tmp = status.get('tmp') or {}
        if tmp.get('is_valid', True):
            tc = tmp.get('tC')
            if tc is None and number(tmp.get('value')):
                tc = tmp['value'] if tmp.get('units', 'C') == 'C' else (tmp['value'] - 32) * 5 / 9
            if tc is None:
                tc = status.get('temperature')
            add('temperature_c' if is_sensor else 'internal_temperature_c', tc, '°C', 'Room temperature' if is_sensor else 'Internal device temperature')
        hum = status.get('hum') or {}
        if hum.get('is_valid', True):
            add('humidity_pct', hum.get('value'), '%', 'Humidity')
        bat = status.get('bat') or {}
        add('battery_pct', bat.get('value'), '%', 'Battery')
        add('rssi_dbm', (status.get('wifi_sta') or {}).get('rssi'), 'dBm', 'Wi-Fi signal')
        valid_meters = [m for m in status.get('meters', []) if m.get('is_valid', True)]
        powers = [m.get('power') for m in valid_meters if number(m.get('power'))]
        if powers:
            add('power_w', sum(powers), 'W', 'Power')
        totals = [m['total'] for m in valid_meters if number(m.get('total'))]
        if totals:
            # Gen1 meters.total is watt-minutes, not Wh. Convert to kWh.
            add('energy_kwh', sum(totals) / 60_000, 'kWh', 'Device energy counter')
        for i, meter in enumerate(status.get('meters', [])):
            if meter.get('is_valid', True):
                add(f'power_{i}_w', meter.get('power'), 'W', f'Channel {i + 1} power', i)
        for i, relay in enumerate(status.get('relays', [])):
            if isinstance(relay.get('ison'), bool):
                add(f'relay_{i}', int(relay['ison']), '', f'Channel {i + 1} relay', i)
        for key, value in (status.get('ext_temperature') or {}).items():
            if isinstance(value, dict) and value.get('is_valid', True):
                add(f'external_temperature_{key}_c', value.get('tC'), '°C', f'External temperature {key}')
        for key, value in (status.get('ext_humidity') or {}).items():
            if isinstance(value, dict) and value.get('is_valid', True):
                add(f'external_humidity_{key}_pct', value.get('hum'), '%', f'External humidity {key}')
        add('uptime_s', status.get('uptime'), 's', 'Uptime')
    else:
        add('rssi_dbm', (status.get('wifi') or {}).get('rssi'), 'dBm', 'Wi-Fi signal')
        add('uptime_s', (status.get('sys') or {}).get('uptime'), 's', 'Uptime')
        powers, energies = [], []
        for key, component in status.items():
            if not isinstance(component, dict):
                continue
            if key.startswith(('switch:', 'pm1:', 'em1:', 'em:')):
                idx = key.split(':')[1]
                power = component.get('apower', component.get('total_act_power'))
                if number(power):
                    powers.append(power)
                    add(f'{key}_power_w', power, 'W', f'{key} power')
                energy = (component.get('aenergy') or {}).get('total')
                if number(energy):
                    energies.append(energy)
                if isinstance(component.get('output'), bool):
                    add(f'relay_{idx}', int(component['output']), '', f'Channel {int(idx) + 1} relay')
                add(f'{key}_voltage_v', component.get('voltage'), 'V', f'{key} voltage')
                add(f'{key}_current_a', component.get('current'), 'A', f'{key} current')
                add(f'{key}_internal_temperature_c', (component.get('temperature') or {}).get('tC'), '°C', f'{key} internal temperature')
            elif key.startswith('temperature:'):
                add(f'{key}_c', component.get('tC'), '°C', f'Temperature sensor {key.split(":")[1]}')
            elif key.startswith('humidity:'):
                add(f'{key}_pct', component.get('rh'), '%', 'Humidity')
            elif key.startswith('devicepower:'):
                add('battery_pct', (component.get('battery') or {}).get('percent'), '%', 'Battery')
        if powers:
            add('power_w', sum(powers), 'W', 'Power')
        if energies:
            add('energy_kwh', sum(energies) / 1000, 'kWh', 'Device energy counter')
    return metrics


def probe_device(device):
    ip = device['ip']
    alive, latency = ping(ip)
    result = {'reachable': alive, 'latency_ms': latency, 'metrics': {}, 'error': None, 'checked_at': now()}
    if latency is not None:
        result['metrics']['latency_ms'] = {'value': latency, 'unit': 'ms', 'label': 'Network latency'}
    reader = media_readers.select_reader(device)
    if reader:
        try:
            media = media_readers.read(device)
            media['metrics'] = {**result['metrics'], **media['metrics']}
            result.update(media)
        except Exception as exc:
            result.update(status='reachable' if alive else 'unreachable', kind='media', protocol=reader,
                          error=f'Local media telemetry unavailable: {str(exc)[:160]}')
        expected = cast.family({**device, **result})
        if expected:
            result.update(media_readers.read_cast_firmware(ip, expected))
            if result['firmware_peer_mac']:
                result.update(reachable=True, mac=result['firmware_peer_mac'])
                if result['status'] == 'unreachable':
                    result['status'] = 'reachable'
        return result
    if not port_open(ip):
        result.update(status='reachable' if alive else 'unreachable', error='No local HTTP API on port 80.' if alive else 'No response. The device may be asleep, disconnected, or filtering probes.')
        return result
    result['reachable'] = True
    try:
        info = read_json(ip, '/shelly')
        if not any(k in info for k in ('type', 'gen')):
            raise ValueError('HTTP responds, but a Shelly API was not identified.')
        gen = int(info.get('gen') or 1)
        result.update(protocol='shelly-gen1' if gen == 1 else 'shelly-rpc',
                      model=shelly.MODELS.get(info.get('type'), info.get('model') or info.get('app') or info.get('type')),
                      mac=info.get('mac'), firmware=info.get('ver') or info.get('fw'),
                      kind='sensor' if (info.get('type') == 'SHHT-1' or 'ht' in str(info.get('app', '')).lower()) else 'switch',
                      info=info)
        status = None
        try:
            status = read_json(ip, '/status' if gen == 1 else '/rpc/Shelly.GetStatus')
        except HTTPError as exc:
            if exc.code in (401, 403):
                result.update(status='auth_required', error='Device authentication is enabled. Credentials are not configured.')
                return result
            raise
        finally:
            result['firmware_check'] = shelly.observe(result['protocol'], info, status, result['checked_at'])
        result['metrics'].update(normalize(info, status))
        result.update(status='online', raw_status=status, raw_status_at=now(), last_telemetry=now(), telemetry_source='HTTP')
    except HTTPError as exc:
        result.update(status='auth_required' if exc.code in (401, 403) else 'reachable', error='Device authentication required.' if exc.code in (401, 403) else f'HTTP {exc.code}; no supported telemetry API identified.')
    except (URLError, OSError, ValueError, TypeError, OverflowError) as exc:
        result.update(status='reachable', error=f'Telemetry unavailable: {str(exc)[:160]}')
    return result


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS devices (ip TEXT PRIMARY KEY, name TEXT NOT NULL, room TEXT NOT NULL DEFAULT 'Unassigned',
            notes TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0, expected_sleep INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, last_seen TEXT, last_telemetry TEXT, payload TEXT NOT NULL DEFAULT '{}');
          CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL, metric TEXT NOT NULL,
            value REAL NOT NULL, unit TEXT NOT NULL, label TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS samples_lookup ON samples(at, ip, metric);
          CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, ip TEXT, type TEXT NOT NULL, message TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS firmware_alerts (device_id TEXT NOT NULL, channel TEXT NOT NULL,
            version TEXT NOT NULL, PRIMARY KEY(device_id, channel, version));
          CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS text_samples (id INTEGER PRIMARY KEY, ip TEXT NOT NULL, at TEXT NOT NULL,
            metric TEXT NOT NULL,value TEXT NOT NULL,unit TEXT NOT NULL,label TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS text_samples_lookup ON text_samples(at,ip,metric);
          CREATE TABLE IF NOT EXISTS energy_readings (source_id TEXT NOT NULL, at TEXT NOT NULL,
            counter_kwh REAL NOT NULL, uptime_s REAL, PRIMARY KEY(source_id,at));
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(devices)')}
        if 'name_mode' not in columns:
            self.db.execute("ALTER TABLE devices ADD COLUMN name_mode TEXT NOT NULL DEFAULT 'manual'")
            for row in self.db.execute('SELECT ip,name FROM devices').fetchall():
                if row['name'] in (f'Device .{row["ip"].split(".")[-1]}', f'Unidentified .{row["ip"].split(".")[-1]}'):
                    self.db.execute("UPDATE devices SET name_mode='auto' WHERE ip=?", (row['ip'],))
        if not self.setting('energy_migrated'):
            # Existing samples predate hardware-keyed energy history. Import their
            # identified meter identity once; new readings carry identity at capture.
            self.db.execute('''INSERT OR IGNORE INTO energy_readings
              SELECT REPLACE(UPPER(json_extract(d.payload,'$.mac')),':',''),s.at,s.value,u.value
              FROM samples s JOIN devices d ON d.ip=s.ip
              LEFT JOIN samples u ON u.ip=s.ip AND u.at=s.at AND u.metric='uptime_s'
              WHERE s.metric='energy_kwh' AND s.value>=0
              AND LENGTH(REPLACE(json_extract(d.payload,'$.mac'),':',''))=12''')
            self.save_setting('energy_migrated', True)
        if not self.setting('heating'):
            self.save_setting('heating', energy.DEFAULT_SETTINGS)
        self.db.commit()

    def setting(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def save_setting(self, key, value):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (key, json.dumps(value)))
            self.db.commit()

    def heating_settings(self, data=None):
        if data is not None:
            normalized = energy.validate_settings(data, self.devices())
            if normalized['rate_per_kwh'] != self.heating_settings()['rate_per_kwh']:
                self.save_setting('heating_rate_note', None)
            self.save_setting('heating', normalized)
        return self.setting('heating', energy.DEFAULT_SETTINGS)

    def energy_summary(self, days=7):
        settings = self.heating_settings()
        at, dates, edges = energy.window(days, settings['timezone'])
        cutoff = datetime.fromtimestamp(edges[0].timestamp() - energy.MAX_INTERVAL_SECONDS, timezone.utc).isoformat()
        with self.lock:
            rows = [dict(r) for r in self.db.execute('SELECT * FROM energy_readings WHERE at>=? ORDER BY source_id,at', (cutoff,))]
        devices = self.devices()
        result = energy.summarize(rows, devices, settings, days, at)
        result['rate_note'] = self.setting('heating_rate_note')
        result['available_meters'] = [{'source_id': mac, 'name': d['name'], 'room': d['room']}
                                      for mac, d in energy.meter_devices(devices).items()]
        return result

    def add_device(self, ip, name=None, room='Unassigned', notes='', pinned=False, expected_sleep=False, name_mode=None):
        ip = local_ip(ip)
        if name_mode is None:
            name_mode = 'auto' if not name or name in (f'Device .{ip.split(".")[-1]}', f'Unidentified .{ip.split(".")[-1]}') else 'manual'
        with self.lock:
            exists = self.db.execute('SELECT ip FROM devices WHERE ip=?', (ip,)).fetchone()
            if not exists:
                self.db.execute('INSERT INTO devices(ip,name,room,notes,pinned,expected_sleep,created_at,name_mode) VALUES(?,?,?,?,?,?,?,?)',
                                (ip, name or f'Device .{ip.split(".")[-1]}', room, notes, int(pinned), int(expected_sleep), now(), name_mode))
                self.event(ip, 'discovered', f'{name or ip} added to your inventory.')
                self.db.commit()
            return not exists

    def event(self, ip, kind, message):
        self.db.execute('INSERT INTO events(at,ip,type,message) VALUES(?,?,?,?)', (now(), ip, kind, message))

    def devices(self):
        with self.lock:
            rows = self.db.execute('SELECT * FROM devices ORDER BY pinned DESC, created_at, ip').fetchall()
            devices = []
            for row in rows:
                item = dict(row)
                payload = json.loads(item.pop('payload'))
                item.update(payload)
                item['pinned'] = bool(item['pinned'])
                item['expected_sleep'] = bool(item['expected_sleep'])
                item.setdefault('status', 'pending')
                item.setdefault('metrics', {})
                devices.append(item)
            return devices

    def record(self, ip, result):
        with self.lock, self.db:
            row = self.db.execute('SELECT * FROM devices WHERE ip=?', (ip,)).fetchone()
            if not row:
                return
            old = json.loads(row['payload'])
            if result.get('checked_at', '') < old.get('checked_at', ''):
                return
            # Keep identity and the last successful API payload when a device sleeps.
            payload = {**old, **result}
            latest = dict(old.get('latest_metrics', {}))
            for key, reading in result.get('metrics', {}).items():
                latest[key] = {**reading, 'reported_at': result.get('last_telemetry') or result['checked_at'],
                               'source': result.get('telemetry_source', 'HTTP')}
            payload['latest_metrics'] = latest
            if result.get('status') != 'online':
                payload['last_metrics'] = old.get('last_metrics') or old.get('metrics', {})
            else:
                payload['last_metrics'] = result['metrics']
            last_seen = now() if result.get('reachable') else row['last_seen']
            last_telemetry = result.get('last_telemetry') or row['last_telemetry']
            payload.pop('last_telemetry', None)
            if result.get('mac'):
                value = result['mac'].replace(':', '')
                if len(value) == 12:
                    payload['mac'] = ':'.join(value[i:i+2] for i in range(0, 12, 2)).upper()
            if old.get('status') != result.get('status'):
                text = {'online': 'is reporting live telemetry', 'reachable': 'is reachable; telemetry unavailable',
                        'unreachable': 'did not respond', 'auth_required': 'requires authentication'}.get(result.get('status'), 'was checked')
                self.event(ip, result.get('status', 'checked'), f'{row["name"]} {text}.')
            if result.get('status') == 'online':
                for key, reading in result.get('metrics', {}).items():
                    before = old.get('last_metrics', old.get('metrics', {})).get(key)
                    if key.startswith('relay_') and before and before.get('value') != reading['value']:
                        relay_state = 'on' if reading['value'] else 'off'
                        self.event(ip, 'relay_changed', f'{row["name"]}: {reading["label"].lower()} is now {relay_state}.')
            self.db.execute('UPDATE devices SET payload=?,last_seen=?,last_telemetry=? WHERE ip=?', (json.dumps(payload), last_seen, last_telemetry, ip))
            for key, metric in result.get('metrics', {}).items():
                if number(metric.get('value')):
                    self.db.execute('INSERT INTO samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                                    (ip, result['checked_at'], key, metric['value'], metric['unit'], metric['label']))
                elif isinstance(metric.get('value'), str) and len(metric['value']) <= 200:
                    previous = old.get('latest_metrics', {}).get(key, {}).get('value')
                    if previous != metric['value']:
                        self.db.execute('INSERT INTO text_samples(ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?)',
                                        (ip, result['checked_at'], key, metric['value'], metric['unit'], metric['label']))
            counter = result.get('metrics', {}).get('energy_kwh', {}).get('value')
            mac = energy.identity(payload.get('mac'))
            if mac and number(counter) and counter >= 0:
                uptime = result.get('metrics', {}).get('uptime_s', {}).get('value')
                self.db.execute('INSERT OR IGNORE INTO energy_readings VALUES(?,?,?,?)',
                                (mac, result['checked_at'], counter, uptime if number(uptime) else None))
            check = result.get('firmware_check')
            if check and check['status'] == 'available':
                version = check['available_version']
                inserted = self.db.execute('INSERT OR IGNORE INTO firmware_alerts VALUES (?,?,?)',
                                           (check['device_id'], check['channel'], version))
                if inserted.rowcount:
                    self.event(ip, 'firmware_update_available', f'Firmware {version} is available for {row["name"]}.')
        if result.get('reported_name'):
            self.apply_names([{'ip':ip, 'name':result['reported_name'], 'source':'Device API'}])

    def patch(self, ip, data):
        allowed = {'name', 'room', 'notes', 'pinned', 'expected_sleep', 'name_mode'}
        if not data or set(data) - allowed:
            raise ValueError('Only workspace labels, name mode, pinning, and sleep expectation can be updated.')
        normalized = {}
        for key, value in data.items():
            if key == 'name_mode':
                if value not in ('manual', 'auto'):
                    raise ValueError('Name mode must be manual or auto.')
                normalized[key] = value
                continue
            if key in ('pinned', 'expected_sleep'):
                if not isinstance(value, bool):
                    raise ValueError(f'{key} must be true or false.')
                normalized[key] = int(value)
            else:
                if not isinstance(value, str) or len(value) > (1000 if key == 'notes' else 100):
                    raise ValueError(f'{key} is too long or invalid.')
                if key in ('name', 'room') and not value.strip():
                    raise ValueError(f'{key} cannot be blank.')
                normalized[key] = value.strip()
        with self.lock:
            row = self.db.execute('SELECT * FROM devices WHERE ip=?', (ip,)).fetchone()
            if not row:
                raise ValueError('Device not found.')
            if 'name' in data and 'name_mode' not in data:
                normalized['name_mode'] = 'manual'
            if normalized.get('name_mode') == 'auto':
                normalized['name'] = json.loads(row['payload']).get('discovered_name') or f'Device .{ip.split(".")[-1]}'
            self.db.execute(f'UPDATE devices SET {", ".join(k+"=?" for k in normalized)} WHERE ip=?', [*normalized.values(), ip])
            self.db.commit()

    def apply_names(self, observations):
        grouped = {}
        for entry in observations:
            name = network_names.clean_name(entry.get('name'))
            if name and entry.get('source') in network_names.PRIORITY:
                grouped.setdefault(entry['ip'], []).append({**entry, 'name': name})
        changed = 0
        with self.lock:
            for ip, names in grouped.items():
                row = self.db.execute('SELECT * FROM devices WHERE ip=?', (ip,)).fetchone()
                if not row:
                    continue
                names = sorted(names, key=network_names.name_rank)[:12]
                best = names[0]
                payload = json.loads(row['payload'])
                previous = {'name':payload.get('discovered_name', ''), 'source':payload.get('discovered_name_source', '')}
                fresh = True
                if previous['name'] and previous['source'] in network_names.PRIORITY and network_names.name_rank(previous)[0] < network_names.name_rank(best)[0]:
                    best = previous
                    fresh = False
                payload.update(discovered_name=best['name'], discovered_name_source=best['source'],
                               advertised_names=names, name_discovered_at=now() if fresh else payload.get('name_discovered_at'))
                hostname = next((n['hostname'] for n in names if n.get('hostname')), None)
                if hostname:
                    payload['hostname'] = hostname
                description = next((n['description_url'] for n in names if n.get('description_url')), None)
                if description:
                    payload['upnp_description_url'] = description
                model = next((n['model'] for n in names if n.get('model')), None)
                if model:
                    payload['advertised_model'] = model
                name = best['name'] if row['name_mode'] == 'auto' else row['name']
                if name != row['name']:
                    changed += 1
                self.db.execute('UPDATE devices SET name=?,payload=? WHERE ip=?', (name, json.dumps(payload), ip))
            self.db.commit()
        return changed

    def history(self, hours=24, ip=None, chart=False):
        cutoff = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat(timespec='seconds')
        if chart:
            bucket = max(POLL_SECONDS, int(hours * 3600 / 300))
            sql = 'SELECT ip,MIN(at) AS at,metric,AVG(value) AS value,unit,label FROM samples WHERE at>=?'
        else:
            sql = 'SELECT ip,at,metric,value,unit,label FROM samples WHERE at>=?'
        params = [cutoff]
        if ip:
            sql += ' AND ip=?'
            params.append(ip)
        if chart:
            sql += " GROUP BY ip,metric,CAST(strftime('%s',at) AS INTEGER)/? ORDER BY at"
            params.append(bucket)
        else:
            text_sql = 'SELECT ip,at,metric,value,unit,label FROM text_samples WHERE at>=?'
            text_params = [cutoff]
            if ip:
                text_sql += ' AND ip=?'
                text_params.append(ip)
            sql += ' UNION ALL ' + text_sql + ' ORDER BY at'
            params.extend(text_params)
        with self.lock:
            if chart:
                span_query = 'SELECT MIN(at) AS first,MAX(at) AS last FROM samples WHERE at>=?'
                span_params = [cutoff]
                if ip:
                    span_query += ' AND ip=?'
                    span_params.append(ip)
                span = self.db.execute(span_query, span_params).fetchone()
                if span['first'] and span['last']:
                    seconds = (datetime.fromisoformat(span['last']) - datetime.fromisoformat(span['first'])).total_seconds()
                    params[-1] = max(POLL_SECONDS, int(seconds / 300))
            return [dict(r) for r in self.db.execute(sql, params)]

    def events(self, limit=50, kind=None):
        with self.lock:
            if kind is not None:
                return [dict(r) for r in self.db.execute('SELECT * FROM events WHERE type=? ORDER BY id DESC LIMIT ?', (kind, limit))]
            return [dict(r) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT ?', (limit,))]

    def link_room(self, room_id, ip, metric='temperature_c'):
        if room_id not in ROOM_NAMES:
            raise ValueError('Unknown floor.')
        key = 'room_sensor:' + room_id
        with self.lock:
            if ip is None:
                self.db.execute('DELETE FROM settings WHERE key=?', (key,))
                self.db.commit()
                return
            ip = local_ip(ip)
            device = next((d for d in self.devices() if d['ip'] == ip), None)
            if not device:
                raise ValueError('Sensor not found in the inventory.')
            environmental = metric == 'temperature_c' or (metric.startswith(('temperature:', 'external_temperature_')) and metric.endswith('_c'))
            readings = {**device.get('last_metrics', {}), **device.get('latest_metrics', {}), **device.get('metrics', {})}
            if not environmental or metric not in readings or readings[metric].get('unit') != '°C':
                raise ValueError('Choose a measured room-temperature sensor; internal device temperatures cannot be linked.')
            mac = re.sub('[^0-9A-F]', '', str(device.get('mac', '')).upper())
            if len(mac) != 12:
                raise ValueError('The sensor needs an identified hardware address before linking.')
            value = json.dumps({'mac': mac, 'metric': metric})
            self.db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
            self.db.execute('UPDATE devices SET room=?,pinned=1,expected_sleep=CASE WHEN json_extract(payload,\'$.info.sleep_mode\')=1 THEN 1 ELSE expected_sleep END WHERE ip=?', (ROOM_NAMES[room_id], ip))
            self.db.commit()

    def rooms(self):
        devices = self.devices()
        with self.lock:
            links = {r['key']: json.loads(r['value']) for r in self.db.execute("SELECT key,value FROM settings WHERE key LIKE 'room_sensor:%'")}
        result = []
        for room_id, name in ROOM_NAMES.items():
            link = links.get('room_sensor:' + room_id)
            room = {'id': room_id, 'name': name, 'sensor': None, 'temperature': None, 'humidity': None, 'battery': None}
            if link:
                matches = [d for d in devices if re.sub('[^0-9A-F]', '', str(d.get('mac', '')).upper()) == link['mac']]
                device = max(matches, key=lambda d: d.get('last_telemetry') or '', default=None)
                if device:
                    room['sensor'] = {k: device.get(k) for k in ('ip', 'name', 'mac', 'status', 'expected_sleep')}
                    for field, key in (('temperature', link['metric']), ('humidity', 'humidity_pct'), ('battery', 'battery_pct')):
                        reading = device.get('latest_metrics', {}).get(key)
                        if not reading:
                            reading = {**device.get('last_metrics', {}), **device.get('metrics', {})}.get(key)
                            if reading:
                                reading = {**reading, 'reported_at': device.get('last_telemetry'), 'source': device.get('telemetry_source', 'HTTP')}
                        if reading and reading.get('reported_at'):
                            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(reading['reported_at'])).total_seconds())
                            room[field] = {**reading, 'age_seconds': round(age), 'stale': age > 1800}
            heaters = [d for d in devices if d.get('kind') == 'switch' and d.get('room', '').casefold() == name.casefold()]
            room['heaters'] = [{'ip': d['ip'], 'name': d['name'], 'power_w': d.get('metrics', {}).get('power_w'),
                                'relay_on': bool(d['metrics']['relay_0']['value']) if 'relay_0' in d.get('metrics', {}) else None} for d in heaters]
            result.append(room)
        return result

    def receive_sensor(self, ip, report):
        ip = local_ip(ip)
        identity = report['device_id']
        # Match a previously identified MAC. A bare LAN packet cannot invent a floor assignment.
        candidates = [d for d in self.devices() if (d.get('info') or {}).get('type') == 'SHHT-1'
                      and re.sub('[^0-9A-F]', '', str(d.get('mac', '')).upper()).endswith(identity)]
        macs = {d.get('mac') for d in candidates}
        if len(macs) != 1:
            return False
        device = max(candidates, key=lambda d: d.get('last_telemetry') or '')
        if device['ip'] != ip:
            existing = next((d for d in self.devices() if d['ip'] == ip), None)
            if existing and existing.get('mac') and existing['mac'] != device['mac']:
                return False
            self.add_device(ip, device['name'], device['room'], notes=device['notes'], pinned=True, expected_sleep=True)
        stamp = now()
        self.record(ip, {'status': 'online', 'reachable': True, 'checked_at': stamp, 'last_telemetry': stamp,
                         'telemetry_source': 'CoIoT', 'metrics': report['metrics'], 'error': None,
                         'info': device['info'], 'model': 'Shelly H&T', 'kind': 'sensor', 'mac': device['mac'],
                         'protocol': 'shelly-gen1', 'raw_coiot': report['raw'], 'raw_coiot_at': stamp})
        return True

    def cleanup(self):
        cutoff = datetime.fromtimestamp(time.time() - RETENTION_DAYS * 86400, timezone.utc).isoformat(timespec='seconds')
        with self.lock:
            self.db.execute('DELETE FROM samples WHERE at<?', (cutoff,))
            self.db.execute('DELETE FROM text_samples WHERE at<?', (cutoff,))
            self.db.execute('DELETE FROM energy_readings WHERE at<?', (cutoff,))
            self.db.execute('DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 1000)')
            self.db.commit()


class Collector:
    def __init__(self, store):
        self.firmware_catalog_locks = {source: threading.Lock() for source in FIRMWARE_CATALOGS}
        self.store = store
        self.network = network_info()
        self.stop = threading.Event()
        self.poll_lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self.names_lock = threading.Lock()
        self.state_lock = threading.RLock()
        self.state = {'polling': False, 'scanning': False, 'last_poll': None, 'last_scan': None,
                      'scan_progress': 0, 'scan_total': 0, 'scan_error': None, 'started_at': now(),
                      'sensor_listener': False, 'sensor_listener_error': None, 'sensor_reports': 0, 'last_sensor_report': None}
        self.state.update(naming=False, last_name_discovery=None, names_found=0, name_discovery_errors=[])
        self.state.update(collection_error=None, background=False, backup=self.store.setting('backup'), backup_error=None,
                          last_poll=self.store.setting('last_completed_poll'), last_gap=self.store.setting('last_collection_gap'))
        self.last_success = time.monotonic()
        self.has_completed_cycle = False
        self.next_backup_attempt = 0
        with self.store.lock:
            previous_scan = self.store.db.execute("SELECT value FROM settings WHERE key='last_scan'").fetchone()
            if previous_scan:
                self.state['last_scan'] = previous_scan['value']
            previous_names = self.store.db.execute("SELECT value FROM settings WHERE key='last_name_discovery'").fetchone()
            if previous_names:
                self.state['last_name_discovery'] = previous_names['value']

    def snapshot(self):
        with self.state_lock:
            last = self.state['last_poll'] or self.state['started_at']
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds())
            health = 'delayed' if age > 120 or self.state['collection_error'] else 'healthy' if self.has_completed_cycle else 'starting'
            return {**self.state, 'network': self.network.copy(), 'poll_seconds': POLL_SECONDS, 'retention_days': RETENTION_DAYS,
                    'health': health, 'seconds_since_poll': round(age), 'database_path': str(self.store.path)}

    def poll(self):
        if not self.poll_lock.acquire(blocking=False):
            return
        try:
            self.state['polling'] = True
            self.network = network_info()
            devices = self.store.devices()
            failures = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                futures = {pool.submit(probe_device, device): device['ip'] for device in devices}
                for future in concurrent.futures.as_completed(futures):
                    try:
                        self.store.record(futures[future], future.result())
                    except Exception as exc:
                        failures += 1
                        print(f'Collector: {futures[future]}: {exc}', flush=True)
            if failures:
                raise RuntimeError(f'{failures} device readings could not be saved. Retrying next cycle.')
            self.store.cleanup()
            completed = now()
            previous = self.state['last_poll']
            if previous and (datetime.fromisoformat(completed) - datetime.fromisoformat(previous)).total_seconds() > 120:
                gap = {'start': previous, 'end': completed}
                self.state['last_gap'] = gap
                self.store.save_setting('last_collection_gap', gap)
            self.store.save_setting('last_completed_poll', completed)
            self.state.update(last_poll=completed, collection_error=None)
            self.has_completed_cycle = True
            self.last_success = time.monotonic()
        except Exception as exc:
            self.state['collection_error'] = str(exc)
            print(f'Collection cycle failed: {exc}', flush=True)
        finally:
            self.state['polling'] = False
            self.poll_lock.release()

    def start_poll(self):
        threading.Thread(target=self.poll, daemon=True).start()

    def listen_sensors(self):
        def received(ip, report):
            try:
                subnet = self.network.get('subnet')
                if subnet and ipaddress.ip_address(ip) not in ipaddress.ip_network(subnet):
                    return
                if self.store.receive_sensor(ip, report):
                    with self.state_lock:
                        self.state['sensor_reports'] += 1
                        self.state['last_sensor_report'] = now()
            except (ValueError, OSError, sqlite3.Error):
                return
        def listener_state(active, error):
            with self.state_lock:
                self.state['sensor_listener'] = active
                self.state['sensor_listener_error'] = error
        coiot.listen(self.network.get('local_ip'), self.stop, received, listener_state)

    def run(self):
        self.start_naming()
        next_names = time.monotonic() + 600
        while not self.stop.is_set():
            self.poll()
            self.backup_if_due()
            if time.monotonic() >= next_names:
                self.start_naming()
                next_names = time.monotonic() + 600
            self.stop.wait(POLL_SECONDS)

    def backup_if_due(self):
        if time.monotonic() < self.next_backup_attempt:
            return
        self.next_backup_attempt = time.monotonic() + 300
        previous = self.state['backup']
        folder = self.store.path.parent / 'backups'
        if previous and previous['at'][:10] == now()[:10] and (folder / previous['file']).is_file():
            return
        try:
            backup = reliability.backup_database(self.store.path, folder)
            self.store.save_setting('backup', backup)
            self.state.update(backup=backup, backup_error=None)
        except (OSError, sqlite3.Error) as exc:
            self.state['backup_error'] = str(exc)
            print(f'Database backup failed: {exc}', flush=True)

    def watchdog(self):
        while not self.stop.wait(30):
            if time.monotonic() - self.last_success > 300:
                print('Collector stalled for five minutes. Exiting for automatic restart.', flush=True)
                os._exit(1)

    def start_naming(self):
        if not self.names_lock.acquire(blocking=False):
            return False
        self.state['naming'] = True
        threading.Thread(target=self.discover_names, daemon=True).start()
        return True

    def discover_names(self):
        try:
            devices = self.store.devices()
            found, errors = network_names.discover([d['ip'] for d in devices], self.network)
            self.store.apply_names(found)
            self.state.update(last_name_discovery=now(), names_found=len({x['ip'] for x in found}), name_discovery_errors=errors)
            with self.store.lock:
                self.store.db.execute("INSERT INTO settings(key,value) VALUES('last_name_discovery',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.state['last_name_discovery'],))
                self.store.db.commit()
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.state['name_discovery_errors'] = [str(exc)[:200]]
        finally:
            self.state['naming'] = False
            self.names_lock.release()

    def start_scan(self):
        if not self.scan_lock.acquire(blocking=False):
            return False
        with self.state_lock:
            self.state.update(scanning=True, scan_progress=0, scan_total=0, scan_error=None)
        threading.Thread(target=self.scan, daemon=True).start()
        return True

    def scan(self):
        try:
            network = network_info()
            self.network = network
            if not network.get('subnet'):
                raise ValueError('Cannot detect the local subnet. Add devices by their private IP address instead.')
            subnet = ipaddress.ip_network(network['subnet'])
            targets = [str(ip) for ip in subnet.hosts() if str(ip) != network.get('local_ip')]
            self.state['scan_total'] = len(targets)
            reachable = set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=28) as pool:
                futures = {pool.submit(ping, ip): ip for ip in targets}
                for future in concurrent.futures.as_completed(futures):
                    if future.result()[0]:
                        reachable.add(futures[future])
                    self.state['scan_progress'] += 1
            entries = arp_entries(network.get('local_ip'))
            # ARP cache membership is discovery evidence, not evidence a host is online now.
            targets = reachable | {ip for ip in entries if ipaddress.ip_address(ip) in subnet}
            for ip in sorted(targets, key=ipaddress.ip_address):
                if ip == network.get('local_ip'):
                    continue
                name = 'Network gateway' if ip == network.get('gateway') else None
                self.store.add_device(ip, name, notes='Discovered on your local network')
                if entries.get(ip) or ip == network.get('gateway'):
                    with self.store.lock:
                        row = self.store.db.execute('SELECT payload FROM devices WHERE ip=?', (ip,)).fetchone()
                        payload = json.loads(row['payload'])
                        if entries.get(ip):
                            payload['mac'] = entries[ip]
                        if ip == network.get('gateway'):
                            payload['kind'] = 'gateway'
                        self.store.db.execute('UPDATE devices SET payload=? WHERE ip=?', (json.dumps(payload), ip))
                        self.store.db.commit()
            self.state['last_scan'] = now()
            with self.store.lock:
                self.store.db.execute("INSERT INTO settings(key,value) VALUES('last_scan',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (self.state['last_scan'],))
                self.store.db.commit()
            self.start_poll()
            self.start_naming()
        except Exception as exc:
            self.state['scan_error'] = str(exc)[:200]
        finally:
            self.state['scanning'] = False
            self.scan_lock.release()


class Handler(SimpleHTTPRequestHandler):
    server_version = 'Hearth/1.0'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / 'web'), **kwargs)

    @property
    def store(self):
        return self.server.store

    @property
    def collector(self):
        return self.server.collector

    def log_message(self, fmt, *args):
        if not args or str(args[0]).startswith(('POST', 'PATCH')):
            super().log_message(fmt, *args)

    def valid_host(self):
        host = self.headers.get('Host', '').split(':')[0].lower()
        if host in ('localhost', '127.0.0.1'):
            return True
        try:
            return host == self.collector.network.get('local_ip') and local_ip(host) == host
        except ValueError:
            return False

    def send_json(self, data, code=200):
        body = json.dumps(data, allow_nan=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        super().end_headers()

    def do_GET(self):
        if not self.valid_host():
            return self.send_json({'error': 'Unrecognized dashboard host.'}, 403)
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == '/api/state':
            with self.store.lock:
                devices = self.store.devices()
                events = self.store.events()
                firmware_events = self.store.events(kind='firmware_update_available')
                catalogs = {source: self.store.setting(key, {}) for source, key in FIRMWARE_CATALOG_KEYS.items()}
            at = datetime.now(timezone.utc)
            catalogs = {source: catalog for source, catalog in catalogs.items() if firmware_checks.catalog_is_fresh(catalog, at)}
            candidates, firmware_sources = [], {}
            for device in devices:
                for source, adapter in FIRMWARE_ADAPTERS.items():
                    identity = adapter.identify(device)
                    if identity is None:
                        continue
                    candidates.append((device, identity))
                    model = adapter.catalog_model(device) if source in FIRMWARE_CATALOGS and identity['device_id'] else None
                    guide = 'riva' if source == 'cast' and cast.family(device) == 'riva' else source
                    firmware_sources[device['ip']] = (source, model, guide)
                    break
            firmware = firmware_checks.inventory(candidates, at)
            for row in firmware:
                source, model, guide = firmware_sources[row['ip']]
                catalog = catalogs.get(source, {}) if model else {}
                latest = catalog.get('versions', {}).get(model)
                row['adapter_id'] = source
                row['guide_id'] = guide
                row['catalog_id'] = source if model else None
                row['catalog_checked_at'] = catalog.get('checked_at')
                row['latest_stable'] = latest
                if row['status'] == 'available':
                    row['stable_status'] = 'update_needed'
                elif latest and row['channel'] == 'stable' and row['status'] != 'stale':
                    row['stable_status'] = FIRMWARE_CATALOGS[source].compare_stable(row['installed_version'], latest)
                else:
                    row['stable_status'] = 'unverified'
            data = {'devices': devices, 'rooms': self.store.rooms(), 'collector': self.collector.snapshot(),
                    'events': events, 'firmware': firmware, 'firmware_events': firmware_events}
            if DEV_RELOAD:
                revision = [DEV_RELOAD]
                for asset in WEB_ASSETS:
                    try:
                        stat = (ROOT / 'web' / asset[1:]).stat()
                        revision.append(f'{stat.st_mtime_ns}-{stat.st_size}-{stat.st_ino}')
                    except FileNotFoundError:
                        revision.append('missing')
                data['dev_revision'] = ':'.join(revision)
            return self.send_json(data)
        if parsed.path in ('/api/energy', '/api/energy.csv'):
            try:
                days = int(query.get('days', ['7'])[0])
                if days not in (1, 7, 30):
                    raise ValueError('Choose Today, 7 days, or 30 days.')
                summary = self.store.energy_summary(days)
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 400)
            if parsed.path == '/api/energy':
                return self.send_json(summary)
            output = io.StringIO(newline='')
            writer = csv.writer(output)
            writer.writerow(['date', 'observed_kwh', 'estimated_cost_usd', 'coverage_pct', 'rate_usd_per_kwh', 'timezone'])
            for row in summary['daily']:
                writer.writerow([row['date'], row['kwh'], row['cost'], row['coverage_pct'], summary['settings']['rate_per_kwh'], csv_value(summary['settings']['timezone'])])
            body = output.getvalue().encode('utf-8-sig')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="local-signal-heating.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in ('/api/history', '/api/export.csv'):
            try:
                hours = float(query.get('hours', ['24'])[0])
                if not math.isfinite(hours) or hours < 0.05 or hours > RETENTION_DAYS * 24:
                    raise ValueError('Choose a range between 3 minutes and 30 days.')
                ip = query.get('ip', [None])[0]
                if ip:
                    ip = local_ip(ip)
                rows = self.store.history(hours, ip, chart=parsed.path == '/api/history')
            except ValueError as exc:
                return self.send_json({'error': str(exc)}, 400)
            if parsed.path == '/api/history':
                return self.send_json({'samples': rows, 'hours': hours})
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=['ip', 'at', 'metric', 'value', 'unit', 'label'])
            writer.writeheader()
            writer.writerows({key:csv_value(value) for key,value in row.items()} for row in rows)
            body = output.getvalue().encode('utf-8-sig')
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv; charset=utf-8')
            self.send_header('Content-Disposition', 'attachment; filename="local-signal-telemetry.csv"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith('/api/'):
            return self.send_json({'error': 'Endpoint not found.'}, 404)
        if parsed.path != '/' and parsed.path not in WEB_ASSETS:
            return self.send_json({'error': 'File not found.'}, 404)
        super().do_GET()

    def mutation(self):
        if not self.valid_host():
            raise ValueError('Unrecognized dashboard host.')
        origin = self.headers.get('Origin')
        if origin and origin != f'http://{self.headers.get("Host")}':
            raise ValueError('Requests must come from this dashboard.')
        if self.headers.get('X-Hearth-Request') != '1':
            raise ValueError('Dashboard request header is required.')
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('Use application/json.')
        length = int(self.headers.get('Content-Length', '0'))
        if length < 0 or length > MAX_BODY:
            raise ValueError('Request body is too large.')
        body = json.loads(self.rfile.read(length) or b'{}')
        if not isinstance(body, dict):
            raise ValueError('Expected a JSON object.')
        return body

    def do_POST(self):
        try:
            data = self.mutation()
            if self.path == '/api/settings/heating':
                self.store.heating_settings(data)
                return self.send_json({'ok': True})
            if self.path == '/api/refresh':
                self.collector.start_poll()
                return self.send_json({'ok': True}, 202)
            if self.path == '/api/firmware/catalog/refresh':
                source = data.get('source', 'shelly')
                if not isinstance(source, str) or source not in FIRMWARE_CATALOGS:
                    raise ValueError('Unknown firmware catalog.')
                lock = self.collector.firmware_catalog_locks[source]
                if not lock.acquire(blocking=False):
                    return self.send_json({'ok': True, 'pending': True}, 202)
                try:
                    key = FIRMWARE_CATALOG_KEYS[source]
                    catalog = self.store.setting(key, {})
                    if firmware_checks.catalog_is_fresh(catalog, datetime.now(timezone.utc)):
                        return self.send_json({'ok': True})
                    versions = FIRMWARE_CATALOGS[source].fetch_stable_versions(build_opener(ProxyHandler({}), NoRedirects()))
                    self.store.save_setting(key, {'checked_at': now(), 'versions': versions})
                    return self.send_json({'ok': True})
                except (OSError, ValueError) as exc:
                    if isinstance(exc, HTTPError):
                        exc.close()
                    return self.send_json({'error': 'Could not read the firmware catalog.'}, 502)
                finally:
                    lock.release()
            if self.path == '/api/scan':
                started = self.collector.start_scan()
                return self.send_json({'ok': True, 'started': started}, 202)
            if self.path == '/api/names/refresh':
                return self.send_json({'ok': True, 'started': self.collector.start_naming()}, 202)
            if self.path == '/api/devices':
                allowed = {'ip', 'name', 'room', 'expected_sleep'}
                if set(data) - allowed:
                    raise ValueError('Unexpected device properties.')
                ip = local_ip(data.get('ip', ''))
                if not isinstance(data.get('name'), str) or not data['name'].strip() or len(data['name']) > 100:
                    raise ValueError('Enter a device name of 1–100 characters.')
                room = data.get('room', 'Unassigned')
                if not isinstance(room, str) or len(room) > 100 or not room.strip():
                    raise ValueError('Enter a room name of 1–100 characters.')
                sleeping = data.get('expected_sleep', False)
                if not isinstance(sleeping, bool):
                    raise ValueError('expected_sleep must be true or false.')
                added = self.store.add_device(ip, data['name'].strip(), room.strip(), pinned=True, expected_sleep=sleeping, name_mode='manual')
                if not added:
                    raise ValueError('This IP address is already in your inventory.')
                self.collector.start_poll()
                return self.send_json({'ok': True, 'ip': ip}, 201)
            room_match = re.fullmatch(r'/api/rooms/(first-floor|second-floor)/sensor', self.path)
            if room_match:
                if set(data) - {'ip', 'metric'}:
                    raise ValueError('Unexpected room sensor properties.')
                metric = data.get('metric', 'temperature_c')
                if not isinstance(metric, str):
                    raise ValueError('Invalid sensor metric.')
                self.store.link_room(room_match[1], data.get('ip'), metric)
                return self.send_json({'ok': True})
            return self.send_json({'error': 'Endpoint not found.'}, 404)
        except (ValueError, TypeError) as exc:
            return self.send_json({'error': str(exc)}, 400)

    def do_PATCH(self):
        try:
            data = self.mutation()
            match = re.fullmatch(r'/api/devices/([\d.]+)', self.path)
            if not match:
                return self.send_json({'error': 'Endpoint not found.'}, 404)
            self.store.patch(local_ip(match[1]), data)
            return self.send_json({'ok': True})
        except (ValueError, TypeError) as exc:
            return self.send_json({'error': str(exc)}, 400)


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != 'nt'

    def server_bind(self):
        # HTTPServer normally reverse-resolves the bind address; home DNS can block
        # startup for minutes. The dashboard never needs a resolved server name.
        if os.name == 'nt':
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


def main(argv=None, *, default_data=ROOT / '.data' / 'hearth.sqlite3', open_browser=False):
    parser = argparse.ArgumentParser(description='Local Signal device monitoring dashboard')
    parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'], help='Use 0.0.0.0 to allow access from the trusted local network.')
    parser.add_argument('--port', type=int, default=8799)
    parser.add_argument('--data', type=Path, default=default_data)
    parser.add_argument('--no-collector', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--background', action='store_true', help='Enable managed logging and the collection watchdog.')
    args = parser.parse_args(argv)
    open_browser = open_browser and not args.background and not args.no_collector
    url = f'http://127.0.0.1:{args.port}'
    try:
        httpd = LocalHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        port_conflict = exc.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(exc, 'winerror', None) in (10048, 10013)
        if open_browser and port_conflict:
            # The installed shortcut also opens the dashboard if the login task
            # already owns this port. Do not open an unrelated local server.
            try:
                with build_opener(ProxyHandler({}), NoRedirects()).open(url + '/api/state', timeout=2) as response:
                    state = json.load(response)
                if Path(state['collector']['database_path']) == args.data.resolve():
                    webbrowser.open(url)
                    return
            except (OSError, ValueError, KeyError, TypeError):
                pass
        raise
    with httpd:
        if args.background:
            os.umask(0o077)
            reliability.configure_logging(args.data.parent / 'logs')
        httpd.store = Store(args.data)
        collector = httpd.collector = Collector(httpd.store)
        collector.state['background'] = args.background
        if not args.no_collector:
            threading.Thread(target=collector.listen_sensors, daemon=True).start()
            threading.Thread(target=collector.run, daemon=True).start()
            if args.background:
                threading.Thread(target=collector.watchdog, daemon=True).start()
        port = httpd.server_port
        print(f'Local Signal is ready at http://localhost:{port}', flush=True)
        if args.host == '0.0.0.0' and collector.network.get('local_ip'):
            print(f'Local network access: http://{collector.network["local_ip"]}:{port}', flush=True)
        if open_browser:
            threading.Thread(target=webbrowser.open, args=(f'http://127.0.0.1:{port}',), daemon=True).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            collector.stop.set()


if __name__ == '__main__':
    main()
