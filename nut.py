"""Read-only UPS monitoring through a private Network UPS Tools server."""
import math
import re
import socket
import time

from device_io import port, private_address

TIMEOUT = 3
MAX_RESPONSE = 131072
METRICS = {'battery.charge': ('ups_battery_pct', '%', 'Battery charge', 1, 0, 100),
           'battery.runtime': ('ups_runtime_min', 'min', 'Estimated battery runtime', 1/60, 0, None),
           'ups.load': ('ups_load_pct', '%', 'UPS load', 1, 0, 1000),
           'ups.realpower': ('ups_power_w', 'W', 'UPS real power', 1, 0, None),
           'input.voltage': ('ups_input_v', 'V', 'Input voltage', 1, 0, None),
           'output.voltage': ('ups_output_v', 'V', 'Output voltage', 1, 0, None),
           'input.frequency': ('ups_input_hz', 'Hz', 'Input frequency', 1, 0, None),
           'ups.temperature': ('ups_internal_c', '°C', 'UPS internal temperature', 1, -100, 200),
           'battery.temperature': ('ups_battery_c', '°C', 'Battery temperature', 1, -100, 200)}
WANTED = set(METRICS) | {'ups.status', 'ups.mfr', 'ups.model', 'device.mfr', 'device.model'}
STATES = {'OL': 'On line', 'OB': 'On battery', 'LB': 'Low battery', 'RB': 'Replace battery', 'CHRG': 'Charging',
          'DISCHRG': 'Discharging', 'BYPASS': 'Bypass', 'OFF': 'Off', 'OVER': 'Overloaded', 'TRIM': 'Trimming',
          'BOOST': 'Boosting', 'FSD': 'Forced shutdown reported', 'ALARM': 'Alarm'}


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {'protocol', 'port', 'ups_name'} or value.get('protocol') != 'nut-ups':
        raise ValueError('Invalid NUT UPS monitoring configuration.')
    name = value.get('ups_name', '')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name):
        raise ValueError('Enter the configured NUT UPS name using letters, numbers, dots, underscores, or hyphens.')
    return {'protocol': 'nut-ups', 'port': port(value.get('port', 3493)), 'ups_name': name}


def tokens(line):
    """Decode NUT's double-quote/backslash syntax; apostrophes are ordinary."""
    result, pos = [], 0
    while pos < len(line):
        while pos < len(line) and line[pos].isspace():
            pos += 1
        if pos == len(line) or line[pos] == '#':
            break
        quoted = line[pos] == '"'
        if quoted:
            pos += 1
        value, closed = [], not quoted
        while pos < len(line):
            char = line[pos]
            if char == '\\':
                pos += 1
                if pos == len(line):
                    raise ValueError('Truncated NUT escape.')
                value.append(line[pos])
            elif quoted and char == '"':
                closed = True
                pos += 1
                break
            elif not quoted and (char.isspace() or char == '#'):
                break
            else:
                value.append(char)
            pos += 1
        if not closed:
            raise ValueError('Unterminated NUT quoted value.')
        result.append(''.join(value))
        if len(result) > 8:
            raise ValueError('Too many fields in NUT response.')
    return result


def read_variables(sock, name, deadline):
    sock.sendall(f'LIST VAR {name}\n'.encode('ascii'))
    buffer, total, started, count, values, seen = bytearray(), 0, False, 0, {}, set()
    while True:
        if b'\n' not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('NUT read time limit reached.')
            sock.settimeout(remaining)
            chunk = sock.recv(4096)
            if not chunk:
                raise ValueError('NUT connection closed before the list ended.')
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ValueError('NUT response exceeds 128 KB.')
            buffer.extend(chunk)
            if b'\n' not in buffer and len(buffer) > 4096:
                raise ValueError('NUT response line is too long.')
            continue
        raw, _, rest = buffer.partition(b'\n')
        buffer = bytearray(rest)
        if len(raw) > 4096:
            raise ValueError('NUT response line is too long.')
        fields = tokens(raw.decode('utf-8').rstrip('\r'))
        if fields[:1] == ['ERR']:
            code = fields[1] if len(fields) > 1 else 'UNKNOWN'
            if code in ('ACCESS-DENIED', 'USERNAME-REQUIRED', 'PASSWORD-REQUIRED'):
                raise PermissionError('NUT server requires authenticated access; anonymous reads are not available.')
            raise ValueError(f'NUT server returned {code}.')
        if not started:
            if fields != ['BEGIN', 'LIST', 'VAR', name]:
                raise ValueError('NUT list does not match the configured UPS.')
            started = True
        elif fields == ['END', 'LIST', 'VAR', name]:
            return values
        else:
            count += 1
            if count > 512 or len(fields) != 4 or fields[:2] != ['VAR', name] or fields[2] in seen:
                raise ValueError('Invalid, duplicate, or excessive NUT variables.')
            seen.add(fields[2])
            if fields[2] in WANTED:
                values[fields[2]] = fields[3]


def normalize(values):
    metrics = {}
    status = values.get('ups.status', '')
    if status and re.fullmatch(r'[A-Z0-9 ]{1,100}', status):
        metrics['ups_state'] = {'value': ' · '.join(STATES.get(s, s) for s in status.split())[:200], 'unit': '', 'label': 'UPS status'}
    for source, (key, unit, label, factor, low, high) in METRICS.items():
        try:
            value = float(values[source])
        except (KeyError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value >= low and (high is None or value <= high):
            metrics[key] = {'value': value * factor, 'unit': unit, 'label': label}
    return metrics


def read(device, checked_at):
    config = validate_config(device.get('monitor_config'))
    ip = private_address(device['ip'])
    result = {'protocol': 'nut-ups', 'kind': 'ups', 'model': 'NUT UPS', 'firmware': None, 'firmware_check': None,
              'reachable': False, 'status': 'unreachable', 'checked_at': checked_at, 'metrics': {}, 'error': None,
              'point_errors': [], 'telemetry_note': None}
    try:
        deadline = time.monotonic() + TIMEOUT
        with socket.create_connection((ip, config['port']), timeout=TIMEOUT) as sock:
            result.update(reachable=True, status='reachable')
            values = read_variables(sock, config['ups_name'], deadline)
        metrics = normalize(values)
        if not metrics:
            raise ValueError('NUT returned no supported valid UPS readings.')
        maker = values.get('device.mfr') or values.get('ups.mfr', '')
        model = values.get('device.model') or values.get('ups.model', '')
        if model.strip():
            result['model'] = f'{maker} {model}'.strip()[:100]
        result.update(status='online', metrics=metrics, last_telemetry=checked_at, telemetry_source='NUT')
    except PermissionError as exc:
        result.update(status='auth_required', reachable=True, error=str(exc))
    except (OSError, ValueError) as exc:
        result['error'] = f'NUT telemetry unavailable: {str(exc)[:160]}'
    return result
