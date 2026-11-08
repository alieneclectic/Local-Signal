"""Bounded, read-only BACnet/IP for explicitly configured, directly addressed devices.

Only unicast ReadProperty is sent. This deliberately small client does not do
discovery, routing, segmentation, registration, subscriptions, or device writes.
See docs/commercial-devices.md for scope and protocol references.
"""
from __future__ import annotations

import ipaddress
import math
import secrets
import socket
import struct
import time

OBJECT_TYPES = {'analog-input': 0, 'analog-output': 1, 'analog-value': 2,
                'binary-input': 3, 'binary-output': 4, 'binary-value': 5,
                'multi-state-input': 13, 'multi-state-output': 14, 'multi-state-value': 19}
MAX_INSTANCE = 4_194_302  # The next value is the wildcard, not a configured identity.
MAX_POINTS = 12
POLL_BUDGET = 10
REQUEST_TIMEOUT = .75
UNITS = {2: 'mA', 3: 'A', 5: 'V', 12: 'kvar', 16: 'J', 17: 'kJ', 18: 'Wh', 19: 'kWh', 27: 'Hz',
         29: '% RH', 31: 'm', 37: 'lx', 47: 'W', 48: 'kW', 49: 'MW',
         53: 'Pa', 54: 'kPa', 55: 'bar', 56: 'psi', 62: '°C', 63: 'K',
         64: '°F', 70: 'days', 71: 'h', 72: 'min', 73: 's', 74: 'm/s',
         84: 'ft³/min', 85: 'm³/s', 87: 'L/s', 88: 'L/min', 135: 'm³/h',
         95: '', 96: 'ppm', 97: 'ppb', 98: '%'}


class BACnetError(ValueError):
    """A device response is unavailable, unsupported, or invalid."""


def integer(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{label} must be an integer from {lower} to {upper}.')
    return value


def validate_config(value):
    if value is None or value == {}:
        return {}
    if not isinstance(value, dict) or set(value) - {'protocol', 'device_instance', 'port', 'points'}:
        raise ValueError('Invalid device monitoring configuration.')
    if value.get('protocol') != 'bacnet-ip':
        raise ValueError('Choose a supported monitoring protocol.')
    instance = integer(value.get('device_instance'), 0, MAX_INSTANCE, 'BACnet device instance')
    port = integer(value.get('port', 47808), 1, 65535, 'BACnet UDP port')
    points = value.get('points')
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f'Configure 1–{MAX_POINTS} BACnet points.')
    normalized, seen = [], set()
    for point in points:
        if not isinstance(point, dict) or set(point) - {'object_type', 'instance', 'label'}:
            raise ValueError('Invalid BACnet point properties.')
        kind = point.get('object_type')
        if not isinstance(kind, str) or kind not in OBJECT_TYPES:
            raise ValueError('Choose a supported BACnet point type.')
        index = integer(point.get('instance'), 0, MAX_INSTANCE, 'BACnet point instance')
        label = point.get('label')
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('Each BACnet point needs a label of 1–80 characters.')
        if (kind, index) in seen:
            raise ValueError('Each BACnet point must have a different type and instance.')
        seen.add((kind, index))
        normalized.append({'object_type': kind, 'instance': index, 'label': label.strip()})
    return {'protocol': 'bacnet-ip', 'device_instance': instance, 'port': port, 'points': normalized}


def private_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError('BACnet requires a private IPv4 device address.') from None
    if not any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) or address.packed[-1] in (0, 255):
        raise ValueError('BACnet requires a private IPv4 device address.')
    return str(address)


def object_id(kind, instance):
    return struct.pack('!I', (kind << 22) | instance)


def read_request(invoke, kind, instance, prop):
    # Original-Unicast-NPDU; version 1, expecting reply; confirmed ReadProperty.
    # Advertise no segmentation and a maximum response APDU of 480 bytes.
    body = b'\x01\x04\x00\x03' + bytes([invoke, 12, 0x0c]) + object_id(kind, instance) + bytes([0x19, prop])
    return b'\x81\x0a' + struct.pack('!H', len(body) + 4) + body


def scalar(data):
    """Decode exactly one small BACnet application value (tag, value)."""
    if not data or data[0] & 8:
        raise BACnetError('Expected a BACnet application value.')
    tag, size, start = data[0] >> 4, data[0] & 7, 1
    if tag == 1:
        if len(data) != 1 or size not in (0, 1):
            raise BACnetError('Invalid BACnet boolean.')
        return tag, bool(size)
    if size == 5:
        if len(data) < 2 or data[1] >= 254:
            raise BACnetError('Unsupported BACnet value length.')
        size, start = data[1], 2
    elif size > 5:
        raise BACnetError('Unsupported BACnet value tag.')
    raw = data[start:]
    if len(raw) != size:
        raise BACnetError('Truncated BACnet value.')
    if tag in (2, 3, 9) and 1 <= size <= 4:
        return tag, int.from_bytes(raw, 'big', signed=tag == 3)
    if tag in (4, 5) and size == (4 if tag == 4 else 8):
        value = struct.unpack('!f' if tag == 4 else '!d', raw)[0]
        if math.isfinite(value):
            return tag, value
    if tag == 7 and size >= 1 and raw[0] in (0, 5):
        try:
            return tag, raw[1:].decode('utf-8' if raw[0] == 0 else 'latin-1')
        except UnicodeDecodeError:
            pass
    if tag == 8 and size == 2 and raw[0] == 4 and not raw[1] & 15:
        return tag, raw[1]  # Four status flags in the high bits.
    if tag == 12 and size == 4:
        return tag, int.from_bytes(raw, 'big')
    raise BACnetError('Unsupported or invalid BACnet value.')


def read_response(packet, invoke, kind, instance, prop):
    if len(packet) < 9 or packet[:2] != b'\x81\x0a' or int.from_bytes(packet[2:4], 'big') != len(packet) or len(packet) > 486:
        raise BACnetError('Invalid or unsupported BACnet/IP packet.')
    # Direct, unsegmented responses only; never interpret routed/forwarded data.
    if packet[4:6] not in (b'\x01\x00', b'\x01\x01', b'\x01\x02', b'\x01\x03'):
        raise BACnetError('Routed BACnet responses are not supported.')
    apdu = packet[6:]
    if apdu[1] != invoke:
        raise BACnetError('BACnet response transaction does not match.')
    if apdu[0] in (0x50, 0x60, 0x70, 0x71):
        raise BACnetError('Device returned a BACnet error, reject, or abort.')
    prefix = bytes([0x30, invoke, 12, 0x0c]) + object_id(kind, instance) + bytes([0x19, prop, 0x3e])
    if not apdu.startswith(prefix) or not apdu.endswith(b'\x3f'):
        raise BACnetError('BACnet response property does not match or requires segmentation.')
    return scalar(apdu[len(prefix):-1])


class Reader:
    def __init__(self, sock, deadline):
        self.sock, self.deadline = sock, deadline
        self.invoke = secrets.randbelow(256)

    def read(self, kind, instance, prop):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BACnetError('BACnet polling time limit reached; remaining points were not read.')
        self.invoke = (self.invoke + 1) % 256
        self.sock.settimeout(min(REQUEST_TIMEOUT, remaining))
        self.sock.send(read_request(self.invoke, kind, instance, prop))
        # A connected UDP socket accepts replies only from the configured endpoint.
        packet = self.sock.recv(487)
        return read_response(packet, self.invoke, kind, instance, prop)


def read(device, checked_at):
    config = validate_config(device.get('monitor_config'))
    if not config:
        raise ValueError('BACnet monitoring is not configured.')
    ip = private_address(device['ip'])
    result = {'protocol': 'bacnet-ip', 'kind': 'commercial', 'model': 'BACnet/IP device',
              'firmware': None, 'firmware_check': None, 'reachable': False, 'status': 'unreachable',
              'checked_at': checked_at, 'metrics': {}, 'error': None, 'telemetry_note': None,
              'bacnet_device_instance': config['device_instance'], 'point_errors': []}
    errors = result['point_errors']
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((ip, config['port']))
            reader = Reader(sock, time.monotonic() + POLL_BUDGET)
            tag, identity = reader.read(8, config['device_instance'], 75)
            result.update(reachable=True, status='reachable')
            if tag != 12 or identity != (8 << 22) | config['device_instance']:
                raise BACnetError('BACnet device identity does not match the configured instance.')
            # Read points before optional device metadata so missing metadata does
            # not consume the time budget needed for useful observations.
            for point in config['points']:
                kind, instance = OBJECT_TYPES[point['object_type']], point['instance']
                try:
                    tag, flags = reader.read(kind, instance, 111)
                    if tag != 8:
                        raise BACnetError('Point quality flags are unavailable.')
                    if flags & 0x70:
                        raise BACnetError('Point is faulted, overridden, or out of service.')
                    unit = ''
                    code = None
                    if kind in (0, 1, 2):
                        tag, code = reader.read(kind, instance, 117)
                        if tag != 9:
                            raise BACnetError('Point engineering units are unavailable.')
                        unit = UNITS.get(code, f'BACnet unit {code}')
                    tag, value = reader.read(kind, instance, 85)
                    if kind in (0, 1, 2):
                        if tag not in (4, 5):
                            raise BACnetError('Analog point did not return a real number.')
                    elif kind in (3, 4, 5):
                        if tag != 9 or value not in (0, 1):
                            raise BACnetError('Binary point did not return inactive or active.')
                    elif tag != 2 or value < 1:
                        raise BACnetError('Multi-state point did not return a positive state number.')
                    key = f'bacnet_{config["device_instance"]}_{kind}_{instance}'
                    if code is not None:
                        key += f'_u{code}'
                    metric = {'value': value, 'unit': unit, 'label': point['label']}
                    if kind in (3, 4, 5):
                        metric['kind'] = 'boolean'
                    result['metrics'][key] = metric
                except (OSError, BACnetError) as exc:
                    errors.append({'label': point['label'], 'error': str(exc)[:160]})
            for prop, field in ((70, 'model'), (44, 'firmware')):
                try:
                    tag, value = reader.read(8, config['device_instance'], prop)
                    if tag == 7 and value.strip():
                        result[field] = value.strip()[:100]
                except (OSError, BACnetError):
                    pass
            if result['metrics']:
                result.update(status='online', last_telemetry=checked_at, telemetry_source='BACnet/IP')
            elif not errors:
                result['error'] = 'No valid BACnet point readings were returned.'
            if errors:
                result['telemetry_note'] = f'{len(errors)} of {len(config["points"])} BACnet points unavailable. See point details below.'
    except (OSError, BACnetError) as exc:
        result['error'] = f'BACnet telemetry unavailable: {str(exc)[:160]}'
    return result
