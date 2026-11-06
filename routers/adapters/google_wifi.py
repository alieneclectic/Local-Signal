"""Optional read-only client telemetry from Google's unofficial Wifi API."""
from datetime import datetime, timezone
from functools import partial
import getpass
import ipaddress
import json
import math
import os
import re
import secrets
import stat
import tempfile
import time
from urllib.parse import quote
import warnings

from ..monitor import Capability, RouterError

NAME = 'Google Wifi (experimental)'
CAPABILITIES: tuple[Capability, ...] = ('upload_bps', 'download_bps', 'paused')
AUTH_URL = 'https://android.clients.google.com/auth'
WIFI_URL = 'https://googlehomefoyer-pa.googleapis.com/v2/'
MAX_RESPONSE = 4 * 1024 * 1024
CONFIG_KEYS = ('email', 'master_token', 'android_id', 'group_id')


def fields(value):
    return value if isinstance(value, dict) else {}


def label(value, default):
    if isinstance(value, str):
        value = ''.join(c for c in value[:160] if c.isprintable()).strip()
        if value:
            return value
    return default


def is_token(value):
    return isinstance(value, str) and re.fullmatch(r'[!-~]{1,8192}', value) is not None


def config_path(database):
    database = database.resolve()
    return database.with_suffix(database.suffix + '.google-wifi.json')


def load_config(database):
    path = config_path(database)
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and
                    (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise RouterError('Google Wifi credentials must be a private file owned by the collector user.',
                                  status='setup_required')
            raw = handle.read(16_385)
        config = json.loads(raw) if len(raw) <= 16_384 else None
        if not isinstance(config, dict) or not all(is_token(config.get(key)) for key in CONFIG_KEYS):
            raise ValueError
        return {key: config[key] for key in CONFIG_KEYS}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise RouterError('Google Wifi credentials could not be read. Run setup again.', status='setup_required') from None


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = tempfile.NamedTemporaryFile(dir=path.parent, suffix='.google-wifi.json', delete=False)
    pending = path.parent / os.path.basename(handle.name)
    try:
        with handle:
            handle.write(json.dumps(config).encode())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


class Client:
    def __init__(self, email, master_token, android_id):
        self.email, self.master_token, self.android_id = email, master_token, android_id
        self.access_token, self.expires = None, 0

    def _request(self, url, *, auth_fields=None):
        try:
            import gpsoauth
            import requests
        except ImportError:
            raise RouterError('Install the Google Wifi dependency in the collector’s Python environment.',
                              status='setup_required') from None

        try:
            with requests.Session() as session:
                session.trust_env = False
                if auth_fields is not None:
                    session.mount(AUTH_URL, gpsoauth.AuthHTTPAdapter())
                    request = session.post(url, data=auth_fields,
                                           headers={'User-Agent': 'GoogleAuth/1.4', 'Accept-Encoding': 'identity'},
                                           timeout=(5, 10), allow_redirects=False, stream=True)
                else:
                    request = session.get(url, headers={'Authorization': f'Bearer {self.access_token}'},
                                          timeout=(5, 10), allow_redirects=False, stream=True)
                with request as response:
                    if response.status_code == 429:
                        retry = response.headers.get('Retry-After', '')
                        delay = min(3600, max(60, int(retry))) if retry.isascii() and retry.isdecimal() and len(retry) <= 6 else 300
                        raise RouterError('Google Wifi is limiting requests. Collection will retry automatically.', retry_after=delay)
                    if response.status_code == 401:
                        self.access_token = None
                        raise RouterError('Google rejected the Wifi sign-in. Run setup again if this continues.',
                                          status='setup_required', retry_after=300)
                    if response.status_code != 200:
                        raise RouterError(f'Google Wifi returned HTTP {response.status_code}.')
                    limit = 65_536 if auth_fields is not None else MAX_RESPONSE
                    body = bytearray()
                    deadline = time.monotonic() + 15
                    for chunk in response.iter_content(65_536):
                        body.extend(chunk)
                        if len(body) > limit or time.monotonic() > deadline:
                            raise RouterError('The Google Wifi response exceeded its size or time limit.')
            if auth_fields is not None:
                return gpsoauth.google.parse_auth_response(body.decode())
            result = json.loads(body)
            if isinstance(result, dict):
                return result
        except requests.RequestException:
            raise RouterError('Could not reach Google Wifi.') from None
        except (ValueError, RecursionError):
            pass
        raise RouterError('Google Wifi returned an unexpected response.')

    def _authenticate(self, credential, *, exchange=False):
        # Protocol fields follow the upstream Google Wifi Insights authentication research.
        data = {'accountType': 'HOSTED_OR_GOOGLE', 'Email': self.email, 'androidId': self.android_id,
                'has_permission': 1, 'source': 'android', 'device_country': 'us', 'operatorCountry': 'us',
                'lang': 'en', 'sdk_version': 17, 'google_play_services_version': 240913000}
        if exchange:
            signature = '38918a453d07199354f8b19af05ec6562ced5788'
            data.update(Token=credential, ACCESS_TOKEN=1, add_account=1, service='ac2dm',
                        client_sig=signature, callerSig=signature, droidguard_results='dummy123')
        else:
            data.update(EncryptedPasswd=credential, service='oauth2:https://www.googleapis.com/auth/accesspoints',
                        app='com.google.android.apps.chromecast.app',
                        client_sig='24bb24c05e47e0aefa68a58a766179d9b613a600')
        result = self._request(AUTH_URL, auth_fields=data)
        if result.get('Error') or not is_token(result.get('Token' if exchange else 'Auth')):
            raise RouterError('Google rejected the Wifi sign-in. Run setup again with a fresh sign-in token.',
                              status='setup_required', retry_after=300)
        return result

    def get(self, path):
        if not self.access_token or time.monotonic() >= self.expires:
            result = self._authenticate(self.master_token)
            self.access_token = result['Auth']
            try:
                lifetime = min(3600, max(1, int(result.get('ExpiresInDurationSec', 3600))))
            except (TypeError, ValueError):
                lifetime = 3600
            self.expires = time.monotonic() + max(1, lifetime - 30)
        return self._request(WIFI_URL + path + '?prettyPrint=false')

    def groups(self):
        groups = self.get('groups').get('groups', [])
        if not isinstance(groups, list):
            raise RouterError('Google returned an unexpected Wifi network list.')
        return {row['id']: row for row in groups if isinstance(row, dict)
                and is_token(row.get('id')) and row['id'] not in ('.', '..')}

    def collect(self, group_id):
        group = self.groups().get(group_id)
        if group is None:
            raise RouterError('The selected Google Wifi network is unavailable. Run setup to select it again.',
                              status='setup_required')
        prefix = 'groups/' + quote(group_id, safe='') + '/'
        stations = self.get(prefix + 'stations').get('stations')
        if not isinstance(stations, list):
            raise RouterError('Google returned an unexpected Wifi device list.')
        try:
            metrics = self.get(prefix + 'realtimeMetrics').get('stationMetrics', [])
            if not isinstance(metrics, list):
                raise RouterError('Google returned unexpected traffic readings.')
            message = None
        except RouterError as exc:
            if exc.retry_after or exc.status != 'unavailable':
                raise
            metrics, message = [], 'Google Wifi traffic readings are temporarily unavailable.'
        at = datetime.now(timezone.utc)
        return {'message': message, 'devices': normalize(group, stations, metrics, at)}


def pause_status(policies, station_id, at):
    if not isinstance(policies, list):
        return None
    matches = [policy for policy in policies if fields(policy).get('stationId') == station_id]
    if not matches:
        return False if all(is_token(fields(policy).get('stationId')) for policy in policies) else None
    if len(matches) != 1:
        return None
    blocking = matches[0].get('blockingPolicy')
    if blocking is None:
        return False
    try:
        expiry = datetime.fromisoformat(fields(blocking).get('expiryTimestamp', '').replace('Z', '+00:00'))
        if expiry.tzinfo is not None:
            return expiry.timestamp() == 0 or expiry > at
    except (AttributeError, OSError, ValueError, OverflowError):
        pass
    return None


def traffic_rate(traffic, direction):
    # In a present ProtoJSON traffic message, omitted scalar fields represent zero.
    value = traffic.get(direction, 0)
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
        return rate if math.isfinite(rate) and rate >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def station_ip(station):
    if station.get('connected') is True:
        alternatives = station.get('ipAddresses')
        for value in [station.get('ipAddress'), *(alternatives if isinstance(alternatives, list) else [])]:
            if isinstance(value, str):
                try:
                    return str(ipaddress.IPv4Address(value))
                except ValueError:
                    pass
    return None


def normalize(group, stations, metrics, at):
    family = fields(fields(group.get('groupSettings')).get('familyHubSettings'))
    policies = family.get('stationPolicies')
    by_id = {row['station']['id']: row for row in metrics if isinstance(row, dict)
             and is_token(fields(row.get('station')).get('id'))}
    devices = []
    for station in stations:
        station_id = fields(station).get('id')
        if not is_token(station_id):
            continue
        metric = by_id.get(station_id, {})
        sample = fields(metric.get('station'))
        traffic = metric.get('traffic')
        current = (station.get('connected') is True and sample.get('connected') is True
                   and isinstance(traffic, dict) and station.get('apId')
                   and sample.get('apId') == station['apId'])
        ip = station_ip(station)
        devices.append({'name': label(station.get('friendlyName'), ip or 'Unnamed device'), 'ip': ip,
                        'connected': station.get('connected') is True,
                        'upload_bps': traffic_rate(traffic, 'transmitSpeedBps') if current else None,
                        'download_bps': traffic_rate(traffic, 'receiveSpeedBps') if current else None,
                        'paused': pause_status(policies, station_id, at)})
    return sorted(devices, key=lambda device: (not device['connected'], device['name'].casefold()))


def reader(config):
    client = Client(config['email'], config['master_token'], config['android_id'])
    return partial(client.collect, config['group_id'])


def configure(database, action):
    path = config_path(database)
    if action == 'disconnect':
        path.unlink(missing_ok=True)
        print('Google Wifi monitoring disconnected.')
        return
    print('Google Wifi uses an unofficial Google API. Setup stores a reusable Google credential on this computer.')
    print('Sign in at https://accounts.google.com/EmbeddedSetup with the account that manages your mesh.')
    print('In browser developer tools, copy the oauth_token cookie for accounts.google.com.')
    email = input('Google account email: ').strip()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            cookie = getpass.getpass('Google sign-in token (hidden): ').strip()
    except getpass.GetPassWarning:
        raise RouterError('Run Google Wifi setup in an interactive terminal that can hide input.') from None
    if not is_token(email) or '@' not in email or not is_token(cookie) or not cookie.startswith('oauth2_4/'):
        raise RouterError('Enter your account email and the value of the oauth_token cookie.')
    client = Client(email, None, secrets.token_hex(8))
    client.master_token = client._authenticate(cookie, exchange=True)['Token']
    networks = list(client.groups().items())
    if not networks:
        raise RouterError('No Google Wifi networks were found for that account.')
    selected = 0
    if len(networks) > 1:
        for index, (_, group) in enumerate(networks, 1):
            print(f'{index}. {label(group.get("name"), "Unnamed network")}')
        choice = input('Select a network number: ').strip()
        if not choice.isascii() or not choice.isdecimal() or len(choice) > 6 or not 1 <= int(choice) <= len(networks):
            raise RouterError('Choose a number from the network list.')
        selected = int(choice) - 1
    save_config(path, {'email': email, 'master_token': client.master_token, 'android_id': client.android_id,
                       'group_id': networks[selected][0]})
    print('Google Wifi monitoring connected. Open Network from localhost on the collector computer.')
