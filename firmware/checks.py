"""Build firmware findings from observations tied to device identity."""
from datetime import datetime, timedelta
import re
from typing import TypedDict


MAX_CHECK_AGE = timedelta(hours=24)


class Observation(TypedDict):
    device_id: str | None
    installed_version: str | None
    available_version: str | None
    channel: str
    source: str
    checked_at: str | None
    status: str


def mac_identity(value):
    if not isinstance(value, str):
        return None
    mac = value.strip().replace(':', '').replace('-', '').upper()
    if re.fullmatch('[0-9A-F]{12}', mac) and mac not in ('000000000000', 'FFFFFFFFFFFF'):
        return mac
    return None


def version_text(value):
    if isinstance(value, str):
        version = value.strip()
        if 0 < len(version) <= 128 and version.isprintable():
            return version
    return None


def identify_media(device, source):
    check = device.get('firmware_check')
    current_mac = mac_identity(device.get('mac'))
    checked_mac = mac_identity(device.get('firmware_peer_mac'))
    if current_mac and checked_mac and current_mac != checked_mac:
        return None
    if check and check['source'] == source:
        return check
    return Observation(device_id=None, installed_version=None, available_version=None, channel='stable',
                       source=source, checked_at=None, status='unknown')


def read_release_page(opener, url, max_bytes=1_048_576):
    with opener.open(url, timeout=5) as response:
        raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError('Firmware release page exceeds the size limit.')
    return raw.decode('utf-8')


def catalog_is_fresh(catalog, at):
    checked_at = catalog.get('checked_at')
    return bool(checked_at and at - datetime.fromisoformat(checked_at) < MAX_CHECK_AGE)


def inventory(candidates, at):
    """Join observations to current addresses, choosing one row per device."""
    findings = {}
    for device, identity in candidates:
        if identity is None:
            continue
        device_id = identity['device_id']
        saved = device.get('firmware_check')
        check = dict(identity)
        if saved and device_id and device_id == saved['device_id']:
            check.update(saved)
        checked_at = check['checked_at']
        if check['status'] in ('available', 'no_update') and (
            at - datetime.fromisoformat(checked_at) >= MAX_CHECK_AGE
            or (device['status'] != 'online' and not device['expected_sleep'] and device['checked_at'] > checked_at)
        ):
            check['status'] = 'stale'

        key = device_id or device['ip']
        # A new HTTP or sensor report can identify the same device at a new IP.
        rank = (max(checked_at or '', device.get('last_telemetry') or ''), checked_at or '', device['status'] == 'online')
        if key not in findings or rank > findings[key][0]:
            findings[key] = (rank, {**check, 'name': device['name'], 'model': device.get('model'), 'ip': device['ip']})

    status_order = {'available': 0, 'stale': 1, 'unknown': 2, 'no_update': 3}
    return sorted((row for _, row in findings.values()),
                  key=lambda row: (status_order[row['status']], row['name'].casefold(), row['ip']))
