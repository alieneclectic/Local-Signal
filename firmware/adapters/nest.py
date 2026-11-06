"""Compare local Google Nest system builds with Google's production list."""
import json
import re

from firmware.checks import Observation, identify_media, mac_identity, read_release_page
from network_names import private_ip


MODEL = 'Google Nest Mini'
HUB_MODEL = 'Google Nest Hub'
SOURCES = {MODEL: 'Google Nest Mini status', HUB_MODEL: 'Google Nest Hub status'}
RELEASES_URL = 'https://support.google.com/googlehome/answer/7365257?hl=en'
STATUS_PATH = ('/setup/eureka_info?params=build_info.system_build_number,build_info.release_track,'
               'device_info.manufacturer,device_info.model_name,device_info.product_name,device_info.mac_address')
BUILD = re.compile(r'[0-9]{1,12}')
MINI_VERSION = re.compile(r'[0-9]{1,4}\.[0-9]{1,4}\.([0-9]{1,12})')
HUB_VERSION = re.compile(r'[0-9]{1,4}\.[0-9]{8}\.[0-9]{1,4}\.[0-9]{1,12}')


def matches(device):
    return device.get('protocol') == 'google-cast' and device.get('model') in SOURCES


def identify(device):
    return identify_media(device, SOURCES[device['model']]) if matches(device) else None


def catalog_model(device):
    return device['model'] if matches(device) else None


def unknown(model, checked_at):
    return Observation(device_id=None, installed_version=None, available_version=None,
                       channel='unknown', source=SOURCES[model], checked_at=checked_at, status='unknown')


def read(ip, model, checked_at, opener):
    if not private_ip(ip):
        raise ValueError('Google Nest status requests require a local address.')
    with opener.open(f'https://{ip}:8443{STATUS_PATH}', timeout=2) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise ValueError('Google Nest status exceeds 64 KB.')
    data = json.loads(raw)
    device = data.get('device_info') if isinstance(data, dict) else None
    if (not isinstance(device, dict) or device.get('model_name') != model
            or device.get('manufacturer') not in ('Google Inc.', 'Google LLC')
            or (model == HUB_MODEL and device.get('product_name') != 'estelle')):
        raise ValueError('The local status response did not identify the expected Google Nest model.')
    check = unknown(model, checked_at)
    check['device_id'] = mac_identity(device.get('mac_address'))
    build_info = data.get('build_info')
    if check['device_id'] and isinstance(build_info, dict):
        build = build_info.get('system_build_number')
        build_pattern = HUB_VERSION if model == HUB_MODEL else BUILD
        if isinstance(build, str) and build_pattern.fullmatch(build):
            check['installed_version'] = build
        if build_info.get('release_track') == 'stable-channel':
            check['channel'] = 'stable'
    return check


def fetch_stable_versions(opener):
    page = read_release_page(opener, RELEASES_URL, max_bytes=2 * 1024 * 1024)
    production = re.search(r'<h2\b[^>]*>\s*Current production firmware version\s*</h2>(.*?)(?=<h2\b|$)', page, re.S)
    versions = {}
    if production:
        for model, heading, name, pattern in (
            (MODEL, 'Speakers', r'Google Nest Mini', MINI_VERSION),
            (HUB_MODEL, 'Displays', r'Google Nest Hub(?: \(1st gen\))?', HUB_VERSION),
        ):
            section = re.search(rf'<h3\b[^>]*>\s*{heading}\s*</h3>(.*?)(?=<h3\b|$)', production[1], re.S)
            if not section:
                continue
            matches = set(re.findall(rf'<strong>\s*{name}\s*</strong>\s*</p>\s*'
                                     r'(?:<div\b[^>]*>\s*)*<p>\s*([0-9.]+)\s*</p>', section[1]))
            if len(matches) == 1:
                version = matches.pop()
                if pattern.fullmatch(version):
                    versions[model] = version
    if not versions:
        raise ValueError('Google did not identify any supported production releases.')
    return versions


def compare_stable(installed, latest):
    # The Hub reports its full system version. The Mini reports only the
    # system build number, which is the last component of the published version.
    if HUB_VERSION.fullmatch(latest or ''):
        return 'current' if installed == latest else 'unverified'
    published = MINI_VERSION.fullmatch(latest or '')
    return 'current' if published and installed == published[1] else 'unverified'
