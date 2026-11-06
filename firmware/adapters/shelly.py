"""Shelly firmware evidence from the local status requests Local Signal already makes."""
import json
from pathlib import Path
import re

from firmware.checks import Observation, mac_identity, version_text


PROTOCOLS = ('shelly-gen1', 'shelly-rpc')
MODELS = json.loads((Path(__file__).resolve().parents[1] / 'catalog' / 'shelly.json').read_text(encoding='utf-8'))
RELEASES_URL = 'https://api.shelly.cloud/files/firmware'
STABLE_BUILD = re.compile(r'\d{8}-\d{6}/v(\d+)\.(\d+)\.(\d+)(?:-g[0-9a-f]+|@[0-9a-f]+)')


def fetch_stable_versions(opener):
    with opener.open(RELEASES_URL, timeout=5) as response:
        raw = response.read(65_537)
    if len(raw) > 65_536:
        raise ValueError('Shelly firmware catalog exceeds 64 KB.')
    catalog = json.loads(raw)
    entries = catalog.get('data') if isinstance(catalog, dict) and catalog.get('isok') is True else None
    if not isinstance(entries, dict):
        raise ValueError('Shelly did not return a firmware catalog.')
    versions = {}
    for model, entry in entries.items():
        if re.fullmatch(r'[A-Z0-9-]{1,24}', model) and isinstance(entry, dict):
            version = version_text(entry.get('version'))
            if version:
                versions[model] = version
    if not versions:
        raise ValueError('Shelly did not return any stable firmware versions.')
    return versions


def catalog_model(device):
    model = (device.get('info') or {}).get('type')
    if device.get('protocol') == 'shelly-gen1' and isinstance(model, str):
        return model
    return None


def compare_stable(installed, latest):
    if not installed or not latest:
        return 'unverified'
    if installed == latest:
        return 'current'
    current = STABLE_BUILD.fullmatch(installed)
    available = STABLE_BUILD.fullmatch(latest)
    if current and available and tuple(map(int, available.groups())) > tuple(map(int, current.groups())):
        return 'newer_published'
    return 'unverified'


def identify(device) -> Observation | None:
    protocol = device.get('protocol')
    if protocol not in PROTOCOLS:
        return None
    info = device.get('info') or {}
    identity = observe(protocol, info, None, None)
    network_mac = mac_identity(device.get('mac'))
    if identity['device_id'] and network_mac and network_mac != identity['device_id']:
        return None  # Discovery found different hardware at this address.
    return identity


def observe(protocol, info, status, checked_at) -> Observation:
    gen1 = protocol == 'shelly-gen1'
    device_id = mac_identity(info.get('mac'))
    installed = version_text(info.get('ver') or info.get('fw')) if device_id else None
    observation = Observation(
        device_id=device_id, installed_version=installed, available_version=None,
        channel='stable', source='Shelly /status' if gen1 else 'Shelly.GetStatus',
        checked_at=checked_at, status='unknown',
    )
    if not installed or not isinstance(status, dict):
        return observation

    if gen1:
        update = status.get('update')
        if not isinstance(update, dict) or update.get('status', 'idle') != 'idle':
            return observation
        if update.get('has_update') is False:
            observation['status'] = 'no_update'
            return observation
        offered = version_text(update.get('new_version')) if update.get('has_update') is True else None
    else:
        system = status.get('sys')
        updates = system.get('available_updates') if isinstance(system, dict) else None
        if not isinstance(updates, dict):
            return observation
        if 'stable' not in updates:
            observation['status'] = 'no_update'
            return observation
        stable = updates['stable']
        offered = version_text(stable.get('version')) if isinstance(stable, dict) else None

    if offered == installed:
        observation['status'] = 'no_update'
    elif offered:
        observation.update(status='available', available_version=offered)
    return observation
