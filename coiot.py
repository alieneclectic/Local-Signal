"""Receive existing Shelly H&T CoIoT v2 broadcasts without changing devices."""
from __future__ import annotations

import json
import math
import re
import socket

GROUP = '224.0.1.187'
PORT = 5683


def _extended(raw, pos, nibble):
    if nibble < 13:
        return nibble, pos
    size = {13: 1, 14: 2}.get(nibble)
    if size is None or pos + size > len(raw):
        raise ValueError('Invalid CoAP option length.')
    value = int.from_bytes(raw[pos:pos + size], 'big') + (13 if size == 1 else 269)
    return value, pos + size


def parse_ht_report(raw):
    """Parse only H&T status publishes; relay board temperatures cannot enter this path."""
    if len(raw) < 4 or len(raw) > 16_384 or raw[0] >> 6 != 1:
        return None
    token_length = raw[0] & 15
    if token_length > 8 or (raw[0] >> 4) & 3 != 1 or raw[1] != 30:
        return None
    pos, option_number, options = 4 + token_length, 0, {}
    if pos > len(raw):
        raise ValueError('Truncated CoAP token.')
    while pos < len(raw) and raw[pos] != 255:
        byte = raw[pos]
        pos += 1
        delta, pos = _extended(raw, pos, byte >> 4)
        length, pos = _extended(raw, pos, byte & 15)
        if pos + length > len(raw):
            raise ValueError('Truncated CoAP option.')
        option_number += delta
        options.setdefault(option_number, []).append(raw[pos:pos + length])
        pos += length
    if pos >= len(raw) or options.get(11) != [b'cit', b's']:
        return None
    identities = options.get(3332, [])
    if len(identities) != 1 or len(identities[0]) > 50:
        return None
    match = re.fullmatch(rb'SHHT-1#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{12})#2', identities[0])
    if not match:
        return None
    body = json.loads(raw[pos + 1:])
    if not isinstance(body, dict) or not isinstance(body.get('G'), list) or len(body['G']) > 64:
        raise ValueError('Invalid H&T status payload.')
    values = {}
    for row in body['G']:
        if isinstance(row, list) and len(row) == 3 and row[0] == 0 and isinstance(row[1], int):
            values[row[1]] = row[2]
    metrics = {}
    def add(key, value, minimum, maximum, unit, label):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and minimum <= value <= maximum:
            metrics[key] = {'value': round(value, 4), 'unit': unit, 'label': label}
    if values.get(3115, 0) == 0:
        add('temperature_c', values.get(3101), -55, 125, '°C', 'Room temperature')
        if 'temperature_c' not in metrics:
            fahrenheit = values.get(3102)
            if isinstance(fahrenheit, (int, float)) and not isinstance(fahrenheit, bool) and -67 <= fahrenheit <= 257:
                add('temperature_c', (fahrenheit - 32) * 5 / 9, -55, 125, '°C', 'Room temperature')
        add('humidity_pct', values.get(3103), 0, 100, '%', 'Humidity')
    add('battery_pct', values.get(3111), 0, 100, '%', 'Battery')
    if not metrics:
        return None
    return {'device_id': match[1].decode().upper(), 'metrics': metrics, 'raw': body}


def listen(interface_ip, stop, on_report, on_state):
    """Join the existing multicast group; never send a device command or ACK."""
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.bind(('', PORT))
        membership = socket.inet_aton(GROUP) + socket.inet_aton(interface_ip or '0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        sock.settimeout(1)
        on_state(True, None)
        while not stop.is_set():
            try:
                raw, peer = sock.recvfrom(16_385)
                try:
                    report = parse_ht_report(raw)
                    if report:
                        on_report(peer[0], report)
                except (ValueError, UnicodeError, TypeError, KeyError, IndexError):
                    continue
            except socket.timeout:
                continue
    except OSError as exc:
        on_state(False, f'Local sensor listener unavailable: {exc}')
    finally:
        if sock:
            sock.close()
