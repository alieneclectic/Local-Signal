"""Read-only HTTP telemetry for explicitly configured LAN devices."""
from functools import partial
import hashlib
import http.client
import json
import math
import re
import time
from urllib.error import HTTPError
from urllib.parse import quote

from device_io import port, private_address, read_json
from firmware.checks import mac_identity


INTEGRATIONS = {
    'tasmota': ('Tasmota', 'iot'),
    'esphome': ('ESPHome', 'sensor'),
    'wled': ('WLED', 'iot'),
    'fronius-solar': ('Fronius Solar API', 'commercial'),
    'opendtu': ('OpenDTU', 'commercial'),
    'airgradient': ('AirGradient', 'sensor'),
    'ecowitt': ('Ecowitt weather gateway', 'sensor'),
    'homewizard-p1': ('HomeWizard P1 Meter', 'iot'),
    'moonraker': ('Klipper / Moonraker', 'printer'),
    'openevse': ('OpenEVSE', 'iot'),
}
DEFAULT_PORTS = {'moonraker': 7125}
ESP_MAX_POINTS = 8
OPENDTU_MAX_AGE = 120
POLL_BUDGET = 6


def esphome_segment(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 120 or not value.isprintable()
            or not value.strip() or value in ('.', '..') or any(char in value for char in '/\\?#%')):
        raise ValueError('Use the exact ESPHome name without slashes, backslashes, ?, #, or %.')
    return value


def esphome_points(points):
    if not isinstance(points, list) or not 1 <= len(points) <= ESP_MAX_POINTS:
        raise ValueError(f'Configure 1–{ESP_MAX_POINTS} ESPHome sensors.')
    normalized, seen = [], set()
    for point in points:
        if not isinstance(point, dict) or set(point) - {'domain', 'name', 'subdevice', 'unit'}:
            raise ValueError('Invalid ESPHome sensor configuration.')
        domain = point.get('domain', 'sensor')
        if domain not in ('sensor', 'binary_sensor'):
            raise ValueError('Choose a numeric or binary ESPHome sensor.')
        name = esphome_segment(point.get('name'))
        subdevice = point.get('subdevice', '')
        if subdevice != '':
            esphome_segment(subdevice)
        unit = point.get('unit', '')
        if not isinstance(unit, str) or len(unit) > 20 or (unit and not unit.isprintable()):
            raise ValueError('Use an ESPHome unit of up to 20 printable characters.')
        identity = (domain, subdevice, name)
        if identity in seen:
            raise ValueError('Configure each ESPHome sensor only once.')
        seen.add(identity)
        normalized.append({'domain': domain, 'name': name, 'subdevice': subdevice,
                           'unit': unit.strip() if domain == 'sensor' else ''})
    return normalized


def validate_config(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid local device monitoring configuration.')
    protocol = value.get('protocol')
    keys = {'protocol', 'port', 'points'} if protocol == 'esphome' else {'protocol', 'port'}
    if not isinstance(protocol, str) or protocol not in INTEGRATIONS or set(value) - keys:
        raise ValueError('Invalid local device monitoring configuration.')
    result = {'protocol': protocol, 'port': port(value.get('port', DEFAULT_PORTS.get(protocol, 80)))}
    if protocol == 'esphome':
        result['points'] = esphome_points(value.get('points'))
    return result


def section(data, *keys):
    for key in keys:
        value = data.get(key)
        if not isinstance(value, dict):
            return {}
        data = value
    return data


def numeric(value, low=-math.inf, high=math.inf):
    if type(value) not in (float, int):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) and low <= value <= high else None


def add(metrics, key, value, unit, label, *, low=-math.inf, high=math.inf, factor=1, offset=0):
    value = numeric(value, low, high)
    if value is not None:
        metrics[key] = {'value': value * factor + offset, 'unit': unit, 'label': label}


def binary(metrics, key, value, label):
    metrics[key] = {'value': int(value), 'unit': '', 'label': label, 'kind': 'boolean'}


def state_metric(metrics, key, value, label):
    metrics[key] = {'value': value, 'unit': '', 'label': label}


def tasmota_sensors(payload, result):
    sensors = section(payload, 'StatusSNS')
    if not isinstance(payload.get('StatusSNS'), dict):
        raise ValueError('Tasmota did not return StatusSNS.')
    metrics = result['metrics']
    meter = section(sensors, 'ENERGY')
    for field, key, unit, label, low in (
        ('Power', 'power_w', 'W', 'Metered power', -math.inf),
        ('Voltage', 'voltage_v', 'V', 'Voltage', 0),
        ('Current', 'current_a', 'A', 'Current', 0),
        ('Today', 'daily_kwh', 'kWh', 'Metered energy today', 0),
        ('Total', 'total_kwh', 'kWh', 'Metered energy counter', 0),
    ):
        add(metrics, f'tasmota_{key}', meter.get(field), unit, label, low=low)
    temperature_unit = sensors.get('TempUnit')
    sensor_count = 0
    for name, entry in sorted(sensors.items()):
        if (not isinstance(entry, dict) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,47}', name)
                or name in ('ENERGY', 'ESP32', 'ESP8266', 'ANALOG')):
            continue
        key = f'tasmota_sensor_{name}'
        before = len(metrics)
        if temperature_unit == 'C':
            add(metrics, f'{key}_c', entry.get('Temperature'), '°C', f'{name} temperature', low=-273.15)
        elif temperature_unit == 'F':
            add(metrics, f'{key}_c', entry.get('Temperature'), '°C', f'{name} temperature',
                low=-459.67, factor=5/9, offset=-32*5/9)
        add(metrics, f'{key}_humidity', entry.get('Humidity'), '%', f'{name} humidity', low=0, high=100)
        if len(metrics) > before:
            sensor_count += 1
            if sensor_count == 16:
                break


def tasmota_state(payload, result):
    state = section(payload, 'StatusSTS')
    if not isinstance(payload.get('StatusSTS'), dict):
        raise ValueError('Tasmota did not return StatusSTS.')
    metrics = result['metrics']
    for key, value in state.items():
        match = re.fullmatch(r'POWER([1-9]|[12][0-9]|3[0-2])?', key)
        if match and value in ('ON', 'OFF'):
            output = match[1] or '1'
            binary(metrics, f'tasmota_output_{output}', value == 'ON', f'Output {output} on')
    add(metrics, 'uptime_s', state.get('UptimeSec'), 's', 'Uptime', low=0)
    add(metrics, 'rssi_dbm', section(state, 'Wifi').get('Signal'), 'dBm', 'Wi-Fi signal', low=-130, high=0)


def wled(payload, result):
    info, state = section(payload, 'info'), section(payload, 'state')
    version = info.get('ver')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,64}', version):
        raise ValueError('WLED did not report a valid firmware version.')
    result['firmware'] = version
    result['mac'] = mac_identity(info.get('mac'))
    metrics = result['metrics']
    on = state.get('on')
    if isinstance(on, bool):
        binary(metrics, 'wled_light_on', on, 'Light on')
        brightness = state.get('bri') if on else 0
        add(metrics, 'wled_brightness_pct', brightness, '%', 'Brightness', low=0, high=255, factor=100/255)
    add(metrics, 'wled_led_count', section(info, 'leds').get('count'), '', 'LED count', low=1)
    add(metrics, 'uptime_s', info.get('uptime'), 's', 'Uptime', low=0)


def fronius(payload, result):
    code = section(payload, 'Head', 'Status').get('Code')
    if type(code) is not int or code != 0:
        raise ValueError('Fronius Solar API did not report a successful power-flow response.')
    site = section(payload, 'Body', 'Data', 'Site')
    metrics = result['metrics']
    add(metrics, 'fronius_pv_w', site.get('P_PV'), 'W', 'PV power')
    add(metrics, 'fronius_grid_w', site.get('P_Grid'), 'W', 'Grid power (+ import)')
    add(metrics, 'fronius_load_w', site.get('P_Load'), 'W', 'Site load (+ consumption)', factor=-1)
    add(metrics, 'fronius_battery_w', site.get('P_Akku'), 'W', 'Battery power (+ discharge)')
    add(metrics, 'fronius_daily_kwh', site.get('E_Day'), 'kWh', 'Solar energy today', low=0, factor=0.001)
    add(metrics, 'fronius_total_kwh', site.get('E_Total'), 'kWh', 'Lifetime solar energy', low=0, factor=0.001)


def opendtu(payload, result):
    inverters = payload.get('inverters')
    if not isinstance(inverters, list) or not inverters or any(
        not isinstance(item, dict) or type(item.get('poll_enabled')) is not bool for item in inverters
    ):
        raise ValueError('OpenDTU did not report inverter polling status.')
    enabled = [item for item in inverters if item['poll_enabled']]
    if not enabled:
        raise ValueError('OpenDTU has no polling-enabled inverters.')
    if section(payload, 'hints').get('radio_problem') is True or any(
        item.get('reachable') is not True or numeric(item.get('data_age'), 0, OPENDTU_MAX_AGE) is None
        for item in enabled
    ):
        raise ValueError('OpenDTU totals are unavailable until every polling-enabled inverter has a recent reading.')
    for field, key, raw_unit, unit, label, factor in (
        ('Power', 'opendtu_ac_w', 'W', 'W', 'Solar AC power', 1),
        ('YieldDay', 'opendtu_daily_kwh', 'Wh', 'kWh', 'Solar energy today', 0.001),
        ('YieldTotal', 'opendtu_total_kwh', 'kWh', 'kWh', 'Lifetime solar energy', 1),
    ):
        reading = section(payload, 'total', field)
        if reading.get('u') == raw_unit:
            add(result['metrics'], key, reading.get('v'), unit, label, low=0, factor=factor)


def esphome(payload, result, *, point, entity):
    identity = payload.get('id')
    legacy = f"{point['domain']}-{point['name']}"
    if entity not in (identity, payload.get('name_id')) and (point['subdevice'] or identity != legacy):
        raise ValueError('ESPHome returned a different sensor.')
    key = 'esphome_' + hashlib.sha256(json.dumps((entity, point['unit'])).encode()).hexdigest()[:20]
    label = entity.partition('/')[2]
    value = payload.get('value')
    if point['domain'] == 'binary_sensor':
        if isinstance(value, bool):
            binary(result['metrics'], key, value, label)
            return
    elif (value := numeric(value)) is not None:
        result['metrics'][key] = {'value': value, 'unit': point['unit'], 'label': label}
        return
    raise ValueError('ESPHome did not report a valid sensor value.')


def airgradient(payload, result):
    metrics = result['metrics']
    add(metrics, 'airgradient_pm25', payload.get('pm02'), 'µg/m³', 'PM2.5', low=0)
    add(metrics, 'airgradient_pm1', payload.get('pm01'), 'µg/m³', 'PM1', low=0)
    add(metrics, 'airgradient_pm10', payload.get('pm10'), 'µg/m³', 'PM10', low=0)
    add(metrics, 'airgradient_co2_ppm', payload.get('rco2'), 'ppm', 'Carbon dioxide', low=1)
    add(metrics, 'airgradient_air_c', payload.get('atmp'), '°C', 'Air temperature', low=-80, high=100)
    add(metrics, 'airgradient_humidity_pct', payload.get('rhum'), '%', 'Relative humidity', low=0, high=100)
    add(metrics, 'airgradient_voc_index', payload.get('tvocIndex'), '', 'VOC index', low=1, high=500)
    add(metrics, 'airgradient_nox_index', payload.get('noxIndex'), '', 'NOₓ index', low=1, high=500)


WEATHER_CONVERSIONS = {
    '°C': ({'c': 1, '°c': 1, '℃': 1, 'f': 5/9, '°f': 5/9, '℉': 5/9}, -273.15, math.inf),
    '%': ({'%': 1, '': 1}, 0, 100),
    'hPa': ({'hpa': 1, 'inhg': 33.8638867, 'mmhg': 1.3332239}, 1, math.inf),
    'm/s': ({'m/s': 1, 'mph': 0.44704, 'km/h': 1/3.6, 'knots': 1852/3600}, 0, math.inf),
    '°': ({'°': 1, '': 1}, 0, 360),
    'mm': ({'mm': 1, 'in': 25.4}, 0, math.inf),
    'mm/h': ({'mm/h': 1, 'mm/hr': 1, 'in/h': 25.4, 'in/hr': 25.4}, 0, math.inf),
}
WEATHER_FIELDS = {
    '0x01': ('indoor_c', '°C', 'Indoor air temperature'),
    '0x02': ('outdoor_c', '°C', 'Outdoor air temperature'),
    '0x06': ('indoor_humidity_pct', '%', 'Indoor humidity'),
    '0x07': ('outdoor_humidity_pct', '%', 'Outdoor humidity'),
    '0x08': ('pressure_hpa', 'hPa', 'Absolute pressure'),
    '0x0a': ('wind_direction', '°', 'Wind direction'),
    '0x0b': ('wind_speed_ms', 'm/s', 'Wind speed'),
    '0x0c': ('wind_gust_ms', 'm/s', 'Wind gust'),
}


def weather_value(metrics, key, unit, label, raw, explicit_unit=''):
    if not isinstance(raw, str) or len(raw) > 40 or not isinstance(explicit_unit, str):
        return
    match = re.fullmatch(r'([+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+))\s*([^\s]*)', raw.strip())
    if not match:
        return
    value, suffix = match.groups()
    suffix, explicit_unit = suffix.casefold(), explicit_unit.strip().casefold()
    factors, low, high = WEATHER_CONVERSIONS[unit]
    # Firmware supplies the unit either inside the value or in a separate field.
    if suffix and explicit_unit and factors.get(suffix) != factors.get(explicit_unit):
        return
    raw_unit = suffix or explicit_unit
    if raw_unit in factors:
        value = float(value)
        if unit == '°C' and raw_unit in ('f', '°f', '℉'):
            value -= 32
        add(metrics, key, value * factors[raw_unit], unit, label, low=low, high=high)


def ecowitt(payload, result):
    metrics = result['metrics']
    weather = payload.get('common_list')
    if isinstance(weather, list):
        for entry in weather:
            if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
                continue
            field = WEATHER_FIELDS.get(entry['id'].lower())
            if field:
                key, unit, label = field
                weather_value(metrics, f'ecowitt_{key}', unit, label, entry.get('val'), entry.get('unit', ''))
    indoor = payload.get('wh25')
    if isinstance(indoor, list) and len(indoor) == 1 and isinstance(indoor[0], dict):
        readings = indoor[0]
        weather_value(metrics, 'ecowitt_indoor_c', '°C', 'Indoor air temperature',
                      readings.get('intemp'), readings.get('unit', ''))
        weather_value(metrics, 'ecowitt_indoor_humidity_pct', '%', 'Indoor humidity', readings.get('inhumi'))
        weather_value(metrics, 'ecowitt_pressure_hpa', 'hPa', 'Absolute pressure', readings.get('abs'))
    for group, label in (('rain', 'Rain'), ('piezoRain', 'Piezo rain')):
        rain = payload.get(group)
        if not isinstance(rain, list):
            continue
        key = 'ecowitt_rain' if group == 'rain' else 'ecowitt_piezo_rain'
        for entry in rain:
            if not isinstance(entry, dict) or not isinstance(entry.get('id'), str):
                continue
            code = entry['id'].lower()
            if code == '0x10':
                weather_value(metrics, f'{key}_today_mm', 'mm', f'{label} today', entry.get('val'), entry.get('unit', ''))
            elif code == '0x0e':
                weather_value(metrics, f'{key}_rate_mmh', 'mm/h', f'{label} rate', entry.get('val'), entry.get('unit', ''))


def homewizard_identity(payload, result):
    if payload.get('product_type') != 'HWE-P1' or payload.get('api_version') != 'v1':
        raise ValueError('This endpoint did not identify as a HomeWizard P1 Meter with API v1.')
    result['mac'] = mac_identity(payload.get('serial'))


def homewizard_p1(payload, result):
    metrics = result['metrics']
    add(metrics, 'homewizard_grid_w', payload.get('active_power_w'), 'W', 'Grid power (+ import)')
    add(metrics, 'homewizard_import_kwh', payload.get('total_power_import_kwh'), 'kWh', 'Imported energy', low=0)
    add(metrics, 'homewizard_export_kwh', payload.get('total_power_export_kwh'), 'kWh', 'Exported energy', low=0)
    for phase in ('l1', 'l2', 'l3'):
        label = phase.upper()
        add(metrics, f'homewizard_{phase}_w', payload.get(f'active_power_{phase}_w'), 'W', f'{label} power (+ import)')
        add(metrics, f'homewizard_{phase}_v', payload.get(f'active_voltage_{phase}_v'), 'V', f'{label} voltage', low=0)
        add(metrics, f'homewizard_{phase}_a', payload.get(f'active_current_{phase}_a'), 'A', f'{label} current', low=0)


def moonraker(payload, result):
    status = section(payload, 'result', 'status')
    controller = section(status, 'webhooks').get('state')
    if controller not in ('ready', 'startup', 'shutdown', 'error'):
        raise ValueError('Moonraker did not report a Klipper controller state.')
    metrics = result['metrics']
    state_metric(metrics, 'moonraker_controller_state', controller.capitalize(), 'Controller state')
    if controller != 'ready':
        return
    job = section(status, 'print_stats').get('state')
    if job in ('standby', 'printing', 'paused', 'complete', 'cancelled', 'error'):
        state_metric(metrics, 'moonraker_print_state', job.capitalize(), 'Print state')
    if job in ('printing', 'paused'):
        progress = section(status, 'virtual_sdcard').get('progress')
        add(metrics, 'moonraker_progress_pct', progress, '%', 'Print progress', low=0, high=1, factor=100)
    for name, key, label in (('extruder', 'hotend', 'Hotend'), ('heater_bed', 'bed', 'Bed')):
        heater = section(status, name)
        add(metrics, f'moonraker_{key}_c', heater.get('temperature'), '°C', f'{label} temperature', low=-273.15)
        add(metrics, f'moonraker_{key}_target_c', heater.get('target'), '°C', f'{label} target', low=0)


EVSE_STATES = {
    0: 'Starting', 1: 'Ready', 2: 'Vehicle connected', 3: 'Charging',
    4: 'Ventilation required', 5: 'Diode check failed', 6: 'GFCI fault',
    7: 'No earth ground', 8: 'Stuck relay', 9: 'GFCI self-test failed',
    10: 'Over temperature', 11: 'Over current', 254: 'Sleeping', 255: 'Disabled',
}


def openevse(payload, result):
    links = [payload[key] for key in ('evse_connected', 'rapi_connected') if key in payload]
    if not links or any(type(value) is not int or value != 1 for value in links):
        raise ValueError('The OpenEVSE gateway did not report a live controller connection.')
    code = payload.get('state')
    if type(code) is not int or code not in EVSE_STATES:
        raise ValueError('OpenEVSE did not report a recognized charge state.')
    metrics = result['metrics']
    state_metric(metrics, 'openevse_state', EVSE_STATES[code], 'Charge state')
    if code == 0:
        return
    add(metrics, 'openevse_current_a', payload.get('amp'), 'A', 'Charging current', low=0, factor=0.001)
    add(metrics, 'openevse_power_w', payload.get('power'), 'W', 'Charging power', low=0)
    add(metrics, 'openevse_voltage_v', payload.get('voltage'), 'V', 'Reported charge voltage', low=1)
    add(metrics, 'openevse_pilot_a', payload.get('pilot'), 'A', 'Pilot current', low=0)
    add(metrics, 'openevse_session_kwh', payload.get('session_energy'), 'kWh', 'Session energy', low=0, factor=0.001)
    add(metrics, 'openevse_total_kwh', payload.get('total_energy'), 'kWh', 'Lifetime charge energy', low=0)
    add(metrics, 'openevse_internal_temperature_c', payload.get('temp'), '°C', 'Charger internal temperature',
        low=-1000, high=2000, factor=0.1)


FIXED_READS = {
    'tasmota': (
        ('/cm?cmnd=Status%2010', 'Sensor readings', tasmota_sensors),
        ('/cm?cmnd=Status%2011', 'Device status', tasmota_state),
    ),
    'wled': (('/json', 'Light status', wled),),
    'fronius-solar': (('/solar_api/v1/GetPowerFlowRealtimeData.fcgi', 'Site power flow', fronius),),
    'opendtu': (('/api/livedata/status', 'Inverter totals', opendtu),),
    'airgradient': (('/measures/current', 'Air quality', airgradient),),
    'ecowitt': (('/get_livedata_info?', 'Weather readings', ecowitt),),
    'homewizard-p1': (
        ('/api', 'Meter identification', homewizard_identity),
        ('/api/v1/data', 'Meter readings', homewizard_p1),
    ),
    'moonraker': (
        ('/printer/objects/query?webhooks=state&print_stats=state&virtual_sdcard=progress'
         '&extruder=temperature,target&heater_bed=temperature,target', 'Printer status', moonraker),
    ),
    'openevse': (('/status', 'Charger status', openevse),),
}


def requests(config):
    if config['protocol'] != 'esphome':
        yield from FIXED_READS[config['protocol']]
        return
    for point in config['points']:
        parts = [part for part in (point['domain'], point['subdevice'], point['name']) if part]
        path = '/' + '/'.join(quote(part, safe='') for part in parts)
        yield path, '/'.join(parts[1:]), partial(esphome, point=point, entity='/'.join(parts))


def read(device, checked_at):
    config = validate_config(device.get('monitor_config'))
    ip = private_address(device['ip'])
    protocol = config['protocol']
    name, kind = INTEGRATIONS[protocol]
    result = {'protocol': protocol, 'model': name, 'kind': kind, 'firmware': None, 'firmware_check': None,
              'reachable': False, 'status': 'unreachable', 'checked_at': checked_at, 'metrics': {},
              'error': None, 'point_errors': [], 'telemetry_note': None}
    deadline = time.monotonic() + POLL_BUDGET
    for path, label, parse in requests(config):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result['telemetry_note'] = 'Some readings were skipped because the poll reached its time limit.'
            break
        try:
            payload = read_json(ip, path, min(2, remaining), tcp_port=config['port'])
            result.update(reachable=True, status='reachable')
            parse(payload, result)
        except HTTPError as exc:
            exc.close()
            result.update(reachable=True, status='reachable')
            if exc.code in (401, 403):
                result.update(status='auth_required', error=f'{name} requires authenticated access.')
                break
            result['point_errors'].append({'label': label, 'error': f'Device returned HTTP {exc.code}.'})
        except ValueError as exc:
            result.update(reachable=True, status='reachable')
            result['point_errors'].append({'label': label, 'error': str(exc)})
        except (OSError, http.client.HTTPException):
            result['point_errors'].append({'label': label, 'error': 'The request failed or timed out.'})
            break
        if protocol == 'homewizard-p1' and path == '/api' and result['point_errors']:
            break  # Other HomeWizard products expose the same measurement URL.
    if result['metrics']:
        if result['error']:
            result['telemetry_note'] = result['error']
        result.update(status='online', last_telemetry=checked_at, telemetry_source=name, error=None)
    elif result['error'] is None:
        result['error'] = f'{name} returned no supported readings.' if result['reachable'] else f'{name} did not respond.'
    return result
