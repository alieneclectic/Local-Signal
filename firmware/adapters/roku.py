"""Installed builds from Roku's read-only ECP device information."""
import re
from uuid import UUID

from firmware.checks import Observation, identify_media


SOURCE = 'Roku device report'
MODELS = {'4660X', '4800X', '3810X'}  # Ultra, Ultra, Streaming Stick+


def identify(device):
    if device.get('protocol') == 'roku-ecp' and (device.get('firmware_check') or {}).get('source') == SOURCE:
        return identify_media(device, SOURCE)
    return None


def observe(info, checked_at) -> Observation | None:
    if info.get('vendor-name') != 'Roku' or info.get('model-number') not in MODELS:
        return None

    udn = (info.get('udn') or '').removeprefix('uuid:')
    try:
        device_id = f'roku:{UUID(udn)}'
    except ValueError:
        device_id = None

    version = info.get('software-version') or ''
    build = info.get('software-build') or ''
    installed = None
    if device_id and re.fullmatch(r'[0-9]{1,4}(?:\.[0-9]{1,4}){2}', version):
        installed = f'{version} (build {build})' if re.fullmatch(r'[0-9]{1,8}', build) else version

    # ECP reports the installed version, without checking rollout eligibility.
    return Observation(device_id=device_id, installed_version=installed, available_version=None,
                       channel='stable', source=SOURCE, checked_at=checked_at, status='unknown')
