"""Compare local Sonos firmware with the public release for its track."""
import re

from firmware.checks import Observation, identify_media, mac_identity, read_release_page, version_text


SOURCE = 'Sonos device description'
RELEASES_URL = 'https://support.sonos.com/en-us/article/release-notes-sonos-system-updates'
# Arc, Sub (Gen 3), and One SL, validated on the modern Sonos track.
MODERN_MODELS = {'S19', 'S26', 'S38'}
BUILD = re.compile(r'([0-9]+)\.([0-9]+)-([0-9]+)')


def identify(device):
    if device.get('protocol') == 'sonos-upnp':
        return identify_media(device, SOURCE)
    return None


def observe(mac, installed, at):
    device_id = mac_identity(mac)
    return Observation(device_id=device_id, installed_version=version_text(installed) if device_id else None,
                       available_version=None, channel='stable', source=SOURCE, checked_at=at, status='unknown')


def catalog_model(device):
    return 'modern' if device.get('firmware_model') in MODERN_MODELS else None


def fetch_stable_versions(opener):
    page = read_release_page(opener, RELEASES_URL)
    header = re.search(r'<h2\b[^>]*>\s*Current system version\s*</h2>\s*(.*?)<h[23]\b([^>]*)>', page, re.S)
    match = re.search(r'\bid="([0-9]+)_([0-9]+-[0-9]+)"', header[2]) if header else None
    if not match:
        raise ValueError('Sonos did not identify the current modern system build.')
    return {'modern': f'{match[1]}.{match[2]}'}


def compare_stable(installed, latest):
    current = BUILD.fullmatch(installed or '')
    published = BUILD.fullmatch(latest or '')
    if not current or not published:
        return 'unverified'
    if installed == latest:
        return 'current'
    if tuple(map(int, published.groups())) > tuple(map(int, current.groups())):
        return 'newer_published'
    return 'unverified'
