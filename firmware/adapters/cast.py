"""Read the Cast firmware revision on standalone Chromecast and RIVA Festival."""
import json
import re
from urllib.error import HTTPError, URLError
from uuid import UUID

from firmware.checks import Observation, identify_media, mac_identity, read_release_page
from network_names import private_ip


SOURCE = 'Google Cast status'
RELEASES_URL = 'https://support.google.com/chromecast/answer/7124014?hl=en'
PRODUCTION_MODELS = (
    'Chromecast (1st gen)', 'Chromecast (2nd gen)', 'Chromecast (3rd gen)',
    'Chromecast Ultra', 'Chromecast Audio',
)
CHROMECAST_MODELS = {*PRODUCTION_MODELS, 'Chromecast', 'Eureka Dongle'}
RIVA_MODEL = 'riva festival'
RIVA_HOST = re.compile(r'riva[-_ ]festival(?:[-_ .].*)?', re.I)
GOOGLE = ('Google Inc.', 'Google LLC')
VERSION = re.compile(r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,12}')
STATUS_PATH = (
    '/setup/eureka_info?params=cast_build_revision,release_track,mac_address,ssdp_udn,'
    'build_info.cast_build_revision,build_info.release_track,device_info.manufacturer,'
    'device_info.model_name,device_info.mac_address'
)


def family(device):
    if device.get('protocol') != 'google-cast':
        return None
    model = device.get('advertised_model') or device.get('model') or ''
    if model in CHROMECAST_MODELS:
        return 'chromecast'
    if model.casefold().replace('-', ' ') == RIVA_MODEL or any(
        entry.get('source') == 'Local DNS' and RIVA_HOST.fullmatch(entry.get('name') or '')
        for entry in device.get('advertised_names', [])
    ):
        return 'riva'
    return None


def unknown(checked_at):
    return Observation(device_id=None, installed_version=None, available_version=None,
                       channel='unknown', source=SOURCE, checked_at=checked_at, status='unknown')


def identify(device):
    if family(device):
        return identify_media(device, SOURCE)
    return None


def catalog_model(device):
    model = device.get('firmware_model')
    check = device.get('firmware_check') or {}
    if family(device) == 'chromecast' and check.get('source') == SOURCE and model in PRODUCTION_MODELS:
        return model
    return None


def open_status(opener, ip, expected):
    if expected == 'chromecast':
        try:
            return opener.open(f'https://{ip}:8443{STATUS_PATH}', timeout=2)
        except HTTPError as exc:
            if exc.code not in (404, 405):
                raise
            exc.close()
        except (URLError, TimeoutError):
            pass
    return opener.open(f'http://{ip}:8008{STATUS_PATH}', timeout=2)


def read(ip, expected, checked_at, opener, read_xml):
    if not private_ip(ip) or expected not in ('chromecast', 'riva'):
        raise ValueError('Cast firmware requires an identified local receiver.')
    with open_status(opener, ip, expected) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise ValueError('Cast firmware status exceeds 64 KB.')
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('Invalid Cast firmware status.')
    device, build = data.get('device_info'), data.get('build_info')
    if isinstance(device, dict) and isinstance(build, dict):
        model = device.get('model_name')
        manufacturer = device.get('manufacturer')
        mac = mac_identity(device.get('mac_address'))
    else:
        # Older receivers use a flat response. Match its UDN to the DIAL
        # description before attributing a build to a model.
        status_udn = data.get('ssdp_udn')
        if not isinstance(status_udn, str):
            raise ValueError('Cast status did not report a receiver identity.')
        root = read_xml(ip, f'http://{ip}:8008/ssdp/device-desc.xml')
        descriptor = root.find('./{*}device')
        if descriptor is None or descriptor.findtext('./{*}deviceType') != 'urn:dial-multiscreen-org:device:dial:1':
            raise ValueError('Cast receiver did not return a DIAL description.')
        udn = descriptor.findtext('./{*}UDN') or ''
        if not udn.startswith('uuid:') or UUID(udn[5:]) != UUID(status_udn):
            raise ValueError('Cast status did not match the receiver description.')
        model = descriptor.findtext('./{*}modelName')
        manufacturer = descriptor.findtext('./{*}manufacturer')
        mac = mac_identity(data.get('mac_address'))
        build = data

    if not isinstance(model, str):
        raise ValueError('Cast status did not report a receiver model.')
    if expected == 'chromecast':
        matched = model in CHROMECAST_MODELS and manufacturer in GOOGLE
    else:
        matched = model.casefold().replace('-', ' ') == RIVA_MODEL
    if not matched:
        raise ValueError('Cast status did not identify the expected receiver model.')
    check = unknown(checked_at)
    check['device_id'] = mac
    installed = build.get('cast_build_revision')
    if mac and isinstance(installed, str) and VERSION.fullmatch(installed):
        check['installed_version'] = installed
    if build.get('release_track') == 'stable-channel':
        check['channel'] = 'stable'
    return check, model


def fetch_stable_versions(opener):
    page = read_release_page(opener, RELEASES_URL, max_bytes=2 * 1024 * 1024)
    production = re.search(r'>\s*Current production firmware version\s*</(?:a|h[23])>\s*'
                           r'(?:<div\b[^>]*>\s*)*<table\b[^>]*>(.*?)</table>', page, re.S)
    versions, seen = {}, set()
    if production:
        for model, version in re.findall(r'<tr\b[^>]*>\s*<td>\s*([^<]+?)\s*</td>\s*'
                                         r'<td>\s*(?:<p>\s*)?([0-9.]+)\s*<', production[1]):
            if model in PRODUCTION_MODELS and VERSION.fullmatch(version):
                if model in seen:
                    versions.pop(model, None)
                else:
                    versions[model] = version
                    seen.add(model)
    if not versions:
        raise ValueError('Google did not identify any supported Chromecast production releases.')
    return versions


def compare_stable(installed, latest):
    return 'current' if installed and installed == latest else 'unverified'
