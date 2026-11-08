"""Read-only IPP printer monitoring: Get-Printer-Attributes only, never jobs."""
import hashlib
import http.client
import re
import secrets
import ssl
import struct

from device_io import port, private_address

MAX_RESPONSE = 131072
TIMEOUT = 3
ATTRIBUTES = ('printer-make-and-model', 'printer-state', 'printer-state-reasons',
              'printer-is-accepting-jobs', 'queued-job-count', 'marker-names', 'marker-levels')


class IPPError(ValueError):
    pass


def validate_config(value):
    if not isinstance(value, dict) or set(value) - {'protocol', 'port', 'path', 'tls'} or value.get('protocol') != 'ipp':
        raise ValueError('Invalid IPP monitoring configuration.')
    path = value.get('path', '/ipp/print')
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9._~!$&()*+,;=:%/-]{0,255}', path):
        raise ValueError('Enter a printer path such as /ipp/print, without a hostname or query.')
    tls = value.get('tls', False)
    if type(tls) is not bool:
        raise ValueError('IPP TLS must be true or false.')
    return {'protocol': 'ipp', 'port': port(value.get('port', 631)), 'path': path, 'tls': tls}


def attribute(tag, name, value):
    name, value = name.encode('ascii'), value.encode('utf-8')
    return bytes([tag]) + struct.pack('!H', len(name)) + name + struct.pack('!H', len(value)) + value


def request(ip, config, request_id):
    uri = f'{"ipps" if config["tls"] else "ipp"}://{ip}:{config["port"]}{config["path"]}'
    body = struct.pack('!BBHI', 1, 1, 11, request_id) + b'\x01'
    body += attribute(0x47, 'attributes-charset', 'utf-8') + attribute(0x48, 'attributes-natural-language', 'en')
    body += attribute(0x45, 'printer-uri', uri)
    for index, name in enumerate(ATTRIBUTES):
        body += attribute(0x44, 'requested-attributes' if index == 0 else '', name)
    return body + b'\x03'


def parse(raw, request_id):
    if not 9 <= len(raw) <= MAX_RESPONSE:
        raise IPPError('Invalid IPP response size.')
    major, minor, status, identity = struct.unpack('!BBHI', raw[:8])
    if major not in (1, 2) or identity != request_id:
        raise IPPError('IPP response version or request ID does not match.')
    if status not in (0, 1, 2):
        if status in (0x0401, 0x0402, 0x0403):
            raise PermissionError('Printer requires authenticated access.')
        raise IPPError(f'Printer returned IPP status 0x{status:04x}.')
    pos, group, name, count, depth = 8, None, None, 0, 0
    result = {}
    while pos < len(raw):
        tag = raw[pos]
        pos += 1
        if tag == 3:
            if pos != len(raw) or depth:
                raise IPPError('Unexpected data after IPP attributes.')
            return result
        if tag < 0x10:
            if depth or tag not in (1, 2, 4, 5):
                raise IPPError('Unsupported IPP attribute group.')
            group, name = tag, None
            continue
        count += 1
        if count > 512 or pos + 2 > len(raw):
            raise IPPError('Too many or truncated IPP attributes.')
        size = int.from_bytes(raw[pos:pos+2], 'big')
        pos += 2
        if size > 255 or pos + size + 2 > len(raw):
            raise IPPError('Invalid IPP attribute name.')
        if size:
            name = raw[pos:pos+size].decode('ascii')
        elif name is None:
            raise IPPError('IPP additional value has no attribute name.')
        pos += size
        size = int.from_bytes(raw[pos:pos+2], 'big')
        pos += 2
        if pos + size > len(raw):
            raise IPPError('Truncated IPP attribute value.')
        value = raw[pos:pos+size]
        pos += size
        if tag == 0x34:
            depth += 1
            if depth > 8:
                raise IPPError('IPP collection nesting is too deep.')
            continue
        if tag == 0x37:
            if not depth:
                raise IPPError('Unexpected IPP collection end.')
            depth -= 1
            continue
        if depth or group != 4 or name not in ATTRIBUTES:
            continue
        if tag in (0x21, 0x23) and size == 4:
            decoded = int.from_bytes(value, 'big', signed=True)
        elif tag == 0x22 and value in (b'\x00', b'\x01'):
            decoded = bool(value[0])
        elif tag in (0x41, 0x42, 0x44) and size <= 512:
            decoded = value.decode('utf-8')
        else:
            continue  # Unknown/no-value and unsupported syntax are not measurements.
        result.setdefault(name, []).append(decoded)
    raise IPPError('IPP response has no end-of-attributes marker.')


def normalize(attributes):
    metrics = {}
    def first(name):
        values = attributes.get(name, [])
        return values[0] if len(values) == 1 else None
    state = first('printer-state')
    if type(state) is not int or state not in (3, 4, 5):
        raise IPPError('Printer did not report a valid printer state.')
    metrics['ipp_state'] = {'value': {3: 'Idle', 4: 'Processing', 5: 'Stopped'}[state], 'unit': '', 'label': 'Printer state'}
    for attribute_name, key, label in (('queued-job-count', 'ipp_queued_jobs', 'Queued jobs'),
                                      ('printer-is-accepting-jobs', 'ipp_accepting_jobs', 'Accepting jobs')):
        value = first(attribute_name)
        if (attribute_name == 'queued-job-count' and type(value) is int and value >= 0) or (attribute_name != 'queued-job-count' and type(value) is bool):
            metrics[key] = {'value': int(value), 'unit': '', 'label': label}
            if type(value) is bool:
                metrics[key]['kind'] = 'boolean'
    reasons = [v for v in attributes.get('printer-state-reasons', []) if isinstance(v, str) and v != 'none']
    if reasons:
        metrics['ipp_reasons'] = {'value': ', '.join(reasons)[:200], 'unit': '', 'label': 'Printer status details'}
    levels, names = attributes.get('marker-levels', []), attributes.get('marker-names', [])
    for index, value in enumerate(levels[:16]):
        if type(value) is not int or not 0 <= value <= 100:
            continue  # Negative supply sentinels mean unavailable, never empty.
        label = names[index][:64] if len(names) == len(levels) and isinstance(names[index], str) else f'Supply {index + 1}'
        identity = hashlib.sha256(label.encode()).hexdigest()[:8]
        metrics[f'ipp_supply_{index}_{identity}_pct'] = {'value': value, 'unit': '%', 'label': label}
    return metrics


def read(device, checked_at):
    config = validate_config(device.get('monitor_config'))
    ip = private_address(device['ip'])
    result = {'protocol': 'ipp', 'kind': 'printer', 'model': 'IPP printer', 'firmware': None, 'firmware_check': None,
              'reachable': False, 'status': 'unreachable', 'checked_at': checked_at, 'metrics': {}, 'error': None,
              'point_errors': [], 'telemetry_note': None}
    connection = None
    try:
        if config['tls']:
            connection = http.client.HTTPSConnection(ip, config['port'], timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            connection = http.client.HTTPConnection(ip, config['port'], timeout=TIMEOUT)
        identity = secrets.randbelow(2**31 - 1) + 1
        connection.request('POST', config['path'], body=request(ip, config, identity), headers={
            'Content-Type': 'application/ipp', 'Accept': 'application/ipp', 'User-Agent': 'Local-Signal/1.0'})
        response = connection.getresponse()
        result.update(reachable=True, status='reachable')
        if response.status in (401, 403):
            raise PermissionError('Printer requires authenticated access.')
        if response.status != 200:
            raise IPPError(f'Printer returned HTTP {response.status}; redirects are not followed.')
        if response.getheader('Content-Type', '').split(';')[0].strip().lower() != 'application/ipp':
            raise IPPError('Printer did not return an IPP response.')
        raw = response.read(MAX_RESPONSE + 1)
        attributes = parse(raw, identity)
        metrics = normalize(attributes)
        models = attributes.get('printer-make-and-model', [])
        if len(models) == 1 and isinstance(models[0], str) and models[0].strip():
            result['model'] = models[0].strip()[:100]
        result.update(status='online', metrics=metrics, last_telemetry=checked_at, telemetry_source='IPP')
    except PermissionError as exc:
        result.update(status='auth_required', reachable=True, error=str(exc))
    except (OSError, ValueError, http.client.HTTPException) as exc:
        result['error'] = f'IPP telemetry unavailable: {str(exc)[:160]}'
    finally:
        if connection:
            connection.close()
    return result
