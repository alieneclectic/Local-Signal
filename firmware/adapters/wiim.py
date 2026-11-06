"""Read-only WiiM Ultra firmware status and published releases."""
import json
import re
from uuid import UUID

from firmware.checks import Observation, identify_media, mac_identity, read_release_page, version_text
from network_names import private_ip


MODEL = 'WiiM_Ultra'
SOURCE = 'WiiM device status'
RELEASES_URL = 'https://faq.wiimhome.com/en/support/solutions/articles/72000631650-wiim-ultra-firmware-release-notes'
VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+')


def matches(device):
    return device.get('protocol') == 'upnp-av' and device.get('model') in ('WiiM Ultra', 'WiiM Ultra Receiver')


def identify(device):
    if matches(device):
        return identify_media(device, SOURCE)
    return None


def catalog_model(device):
    return MODEL if matches(device) else None


def version(value):
    value = version_text(value)
    if value:
        value = value.removeprefix('Linkplay.')
        if VERSION.fullmatch(value):
            return value
    return None


def read(ip, udn, at, opener):
    if not private_ip(ip) or not isinstance(udn, str) or not udn.startswith('uuid:'):
        raise ValueError('WiiM firmware requests require an identified local device.')
    identity = str(UUID(udn.removeprefix('uuid:')))
    with opener.open(f'https://{ip}/httpapi.asp?command=getStatusEx', timeout=2) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise ValueError('WiiM firmware status exceeds 64 KB.')
    data = json.loads(raw)
    if (not isinstance(data, dict) or data.get('project') != MODEL
            or str(data.get('upnp_uuid')).lower() != f'uuid:{identity}'):
        raise ValueError('WiiM status did not match the discovered WiiM Ultra.')

    installed = version(data.get('firmware'))
    offered = version(data.get('NewVer'))
    flag = data.get('VersionUpdate')
    status = 'unknown'
    if installed and flag == '1' and offered and tuple(map(int, offered.split('.'))) > tuple(map(int, installed.split('.'))):
        status = 'available'
    elif installed and flag == '0' and data.get('NewVer') == '0':
        status = 'no_update'
    peer_mac = mac_identity(data.get('ETH_MAC') if data.get('eth0') == ip else data.get('MAC'))
    return Observation(device_id=f'wiim:{identity}', installed_version=installed,
                       available_version=offered if status == 'available' else None,
                       channel='stable', source=SOURCE, checked_at=at, status=status), peer_mac


def fetch_stable_versions(opener):
    page = read_release_page(opener, RELEASES_URL)
    latest = set(re.findall(r'>\s*([0-9]+\.[0-9]+\.[0-9]+)\s+\(Latest\)\s*<', page))
    if len(latest) != 1:
        raise ValueError('WiiM did not identify a single latest Ultra release.')
    return {MODEL: latest.pop()}


def compare_stable(installed, latest):
    if installed and installed == latest:
        return 'current'
    return 'unverified'
