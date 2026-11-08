"""Bounded Modbus TCP reads for explicitly configured points on private IPv4.

Only functions 01–04 are available. Addresses are zero-based PDU offsets, not
the 30001/40001 reference numbers in some manuals. No model maps are assumed.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import secrets
import socket
import struct
import time

FUNCTIONS = {'coil': 1, 'discrete': 2, 'holding': 3, 'input': 4}
FORMATS = {'uint16': ('H', 1), 'int16': ('h', 1), 'uint32': ('I', 2),
           'int32': ('i', 2), 'float32': ('f', 2), 'float64': ('d', 4)}
MAX_POINTS = 12
POLL_BUDGET = 10
REQUEST_TIMEOUT = .75
EXCEPTIONS = {1: 'Illegal function', 2: 'Illegal data address', 3: 'Illegal data value',
              4: 'Server device failure', 5: 'Acknowledge; value not ready', 6: 'Server device busy',
              8: 'Memory parity error', 10: 'Gateway path unavailable', 11: 'Gateway target did not respond'}


class ModbusError(ValueError):
    """The TCP stream or Modbus frame cannot be trusted for further reads."""


class ModbusException(ValueError):
    """A complete, valid exception response; subsequent requests remain safe."""


class InvalidReading(ValueError):
    """A valid frame contained an unavailable or nonfinite point reading."""


def integer(value, lower, upper, label):
    if type(value) is not int or not lower <= value <= upper:
        raise ValueError(f'{label} must be an integer from {lower} to {upper}.')
    return value


def finite(value, label):
    try:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError()
        return float(value)
    except (ValueError, OverflowError):
        raise ValueError(f'{label} must be a finite number.') from None


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {'protocol', 'port', 'unit_id', 'points'}:
        raise ValueError('Invalid Modbus monitoring configuration.')
    if value.get('protocol') != 'modbus-tcp':
        raise ValueError('Choose a supported monitoring protocol.')
    port = integer(value.get('port', 502), 1, 65535, 'Modbus TCP port')
    unit = integer(value.get('unit_id', 255), 0, 255, 'Modbus unit ID')
    points = value.get('points')
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f'Configure 1–{MAX_POINTS} Modbus points.')
    normalized, seen = [], set()
    for point in points:
        allowed = {'table', 'address', 'data_type', 'byte_order', 'word_order', 'scale', 'offset', 'unit', 'label', 'invalid_value'}
        if not isinstance(point, dict) or set(point) - allowed:
            raise ValueError('Invalid Modbus point properties.')
        table = point.get('table')
        if not isinstance(table, str) or table not in FUNCTIONS:
            raise ValueError('Choose a supported Modbus read table.')
        bit = table in ('coil', 'discrete')
        data_type = point.get('data_type', 'bit' if bit else 'uint16')
        allowed_types = ('bit',) if bit else FORMATS
        if not isinstance(data_type, str) or data_type not in allowed_types:
            raise ValueError('Choose a data type compatible with the Modbus table.')
        count = 1 if bit else FORMATS[data_type][1]
        address = integer(point.get('address'), 0, 65536 - count, 'Zero-based Modbus address')
        label, units = point.get('label'), point.get('unit', '')
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('Each Modbus point needs a label of 1–80 characters.')
        if not isinstance(units, str) or len(units) > 20:
            raise ValueError('Modbus point units must be text of at most 20 characters.')
        byte_order, word_order = point.get('byte_order', 'big'), point.get('word_order', 'big')
        if byte_order not in ('big', 'little') or word_order not in ('big', 'little'):
            raise ValueError('Modbus byte and word order must be big or little.')
        scale, offset = finite(point.get('scale', 1), 'Modbus scale'), finite(point.get('offset', 0), 'Modbus offset')
        invalid = point.get('invalid_value')
        if invalid is not None:
            invalid = finite(invalid, 'Modbus raw invalid value')
            if not bit:
                fmt = FORMATS[data_type][0]
                if data_type.startswith(('int', 'uint')):
                    if not invalid.is_integer():
                        raise ValueError('An integer register needs an integer raw invalid value.')
                    invalid = int(invalid)
                try:
                    # Match the actual numeric format, including float32 rounding.
                    invalid = struct.unpack('!' + fmt, struct.pack('!' + fmt, invalid))[0]
                except (struct.error, OverflowError):
                    raise ValueError('Raw invalid value does not fit the selected Modbus data type.') from None
        if scale == 0:
            raise ValueError('Modbus scale must be nonzero.')
        if bit and (units.strip() or scale != 1 or offset != 0 or invalid is not None or byte_order != 'big' or word_order != 'big'):
            raise ValueError('Bit points use Yes/No without numeric conversions or invalid-value markers.')
        if (table, address) in seen:
            raise ValueError('Each Modbus point must have a different table and start address.')
        seen.add((table, address))
        normalized.append({'table': table, 'address': address, 'data_type': data_type,
                           'byte_order': byte_order, 'word_order': word_order, 'scale': scale, 'offset': offset,
                           'unit': units.strip(), 'label': label.strip(), 'invalid_value': invalid})
    return {'protocol': 'modbus-tcp', 'port': port, 'unit_id': unit, 'points': normalized}


def private_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError('Modbus requires a private IPv4 device address.') from None
    if not any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) or address.packed[-1] in (0, 255):
        raise ValueError('Modbus requires a private IPv4 device address.')
    return str(address)


def request(transaction, unit, point):
    # No caller-provided function code: the table allowlist only contains reads.
    function = FUNCTIONS[point['table']]
    count = 1 if function in (1, 2) else FORMATS[point['data_type']][1]
    return struct.pack('!HHHBBHH', transaction, 0, 6, unit, function, point['address'], count)


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ModbusError('Modbus read time limit reached.')
    return value


def receive(sock, size, deadline):
    data = bytearray()
    while len(data) < size:
        sock.settimeout(remaining(deadline))
        part = sock.recv(size - len(data))
        if not part:
            raise ModbusError('Modbus connection closed before the response was complete.')
        data.extend(part)
    return bytes(data)


class Reader:
    def __init__(self, sock, deadline, unit):
        self.sock, self.deadline, self.unit = sock, deadline, unit
        self.transaction = secrets.randbelow(65536)

    def read(self, point):
        deadline = min(self.deadline, time.monotonic() + REQUEST_TIMEOUT)
        self.transaction = (self.transaction + 1) % 65536
        self.sock.settimeout(remaining(deadline))
        self.sock.sendall(request(self.transaction, self.unit, point))
        header = receive(self.sock, 7, deadline)
        transaction, protocol, length, unit = struct.unpack('!HHHB', header)
        function = FUNCTIONS[point['table']]
        size = 1 if function in (1, 2) else 2 * FORMATS[point['data_type']][1]
        if transaction != self.transaction or protocol != 0 or unit != self.unit:
            raise ModbusError('Modbus response transaction, protocol, or unit ID does not match.')
        if length not in (3, size + 3):
            raise ModbusError('Invalid Modbus response length.')
        body = receive(self.sock, length - 1, deadline)
        if body[0] == (function | 0x80) and len(body) == 2:
            raise ModbusException(f'Modbus exception {body[1]}: {EXCEPTIONS.get(body[1], "Unknown device exception")}.')
        if body[0] != function or len(body) != size + 2 or body[1] != size:
            raise ModbusError('Modbus response function or byte count does not match.')
        return body[2:]


def decode(data, point):
    if point['data_type'] == 'bit':
        if data not in (b'\x00', b'\x01'):
            raise InvalidReading('Invalid Modbus bit value or nonzero padding.')
        return data[0]
    words = [data[i:i + 2] for i in range(0, len(data), 2)]
    if point['word_order'] == 'little':
        words.reverse()
    if point['byte_order'] == 'little':
        words = [word[::-1] for word in words]
    fmt, count = FORMATS[point['data_type']]
    if len(data) != count * 2:
        raise InvalidReading('Modbus numeric value has the wrong width.')
    raw = struct.unpack('!' + fmt, b''.join(words))[0]
    if not math.isfinite(raw) or raw == point['invalid_value']:
        raise InvalidReading('Device reported a nonfinite or configured invalid raw value.')
    value = raw * point['scale'] + point['offset']
    if not math.isfinite(value):
        raise InvalidReading('Scaled Modbus value is not finite.')
    return value


def metric_key(device, config, point):
    # Inventory identity survives an observed IP move. Mapping changes create a
    # separate series, while label edits keep history. Unit IDs are not MACs.
    mapping = {key: value for key, value in point.items() if key != 'label'}
    mapping['port'] = config['port']
    digest = hashlib.sha256(json.dumps(mapping, sort_keys=True, allow_nan=False).encode()).hexdigest()[:12]
    identity = device.get('device_id') or device['ip'].replace('.', '_')
    return f'modbus_{identity}_{config["unit_id"]}_{point["table"]}_{point["address"]}_{digest}'


def read(device, checked_at):
    config = validate_config(device.get('monitor_config'))
    ip = private_address(device['ip'])
    result = {'protocol': 'modbus-tcp', 'kind': 'commercial', 'model': 'Modbus TCP device',
              'firmware': None, 'firmware_check': None, 'reachable': False, 'status': 'unreachable',
              'checked_at': checked_at, 'metrics': {}, 'error': None, 'telemetry_note': None, 'point_errors': []}
    errors, stream_error = result['point_errors'], None
    deadline = time.monotonic() + POLL_BUDGET
    try:
        with socket.create_connection((ip, config['port']), timeout=min(REQUEST_TIMEOUT, remaining(deadline))) as sock:
            result.update(reachable=True, status='reachable')
            reader = Reader(sock, deadline, config['unit_id'])
            for point in config['points']:
                if stream_error:
                    errors.append({'label': point['label'], 'error': 'Not read after a connection or framing failure.'})
                    continue
                try:
                    data = reader.read(point)
                    value = decode(data, point)
                    metric = {'value': value, 'unit': point['unit'], 'label': point['label']}
                    if point['data_type'] == 'bit':
                        metric['kind'] = 'boolean'
                    result['metrics'][metric_key(device, config, point)] = metric
                except (InvalidReading, ModbusException) as exc:
                    errors.append({'label': point['label'], 'error': str(exc)[:160]})
                except (OSError, ModbusError) as exc:
                    stream_error = str(exc) or 'Connection failed.'
                    errors.append({'label': point['label'], 'error': stream_error[:160]})
            if result['metrics']:
                result.update(status='online', last_telemetry=checked_at, telemetry_source='Modbus TCP')
            if errors:
                result['telemetry_note'] = f'{len(errors)} of {len(config["points"])} Modbus points unavailable. See point details below.'
    except (OSError, ModbusError) as exc:
        result['error'] = f'Modbus telemetry unavailable: {str(exc)[:160]}'
    return result
