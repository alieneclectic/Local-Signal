"""Alert rules, open/resolved alert history, and optional webhook delivery.

Rules only read what the collector already stored. A rule opens an alert when it observes
the condition, keeps the alert open while the reading is unknown or stale, and resolves it
only when a fresh reading shows the condition has cleared. Missing data is never treated
as a recovery.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import os
import queue
import re
import secrets
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SCHEMA = '''
  CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, rule TEXT NOT NULL, device_id INTEGER NOT NULL,
    subject TEXT NOT NULL DEFAULT '', severity TEXT NOT NULL, summary TEXT NOT NULL, opened_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, resolved_at TEXT, acknowledged_at TEXT);
  CREATE UNIQUE INDEX IF NOT EXISTS alerts_open ON alerts(rule, device_id, subject) WHERE resolved_at IS NULL;
  CREATE INDEX IF NOT EXISTS alerts_resolved ON alerts(resolved_at);
'''

DEFAULT_SETTINGS = {
    'device_offline': {'enabled': True, 'minutes': 10},
    'ups_power': {'enabled': True},
    'ups_battery': {'enabled': True, 'pct': 50},
    'printer_supply': {'enabled': True, 'pct': 10},
    'sensor_battery': {'enabled': True, 'pct': 20},
    'notify_resolved': True,
    'custom': [],
}
LIMITS = {'minutes': (2, 1440), 'pct': (1, 99)}
MAX_CUSTOM_RULES = 25
CUSTOM_FIELDS = ('id', 'enabled', 'name', 'device_id', 'metric', 'label', 'unit', 'op', 'value', 'minutes', 'severity')
CUSTOM_OPERATORS = ('below', 'above')
SEVERITIES = ('info', 'warning', 'critical')
# A custom rule only judges readings this recent. Older readings are unknown, not a recovery.
FRESH_SECONDS = 30 * 60
SLEEPING_FRESH_SECONDS = 12 * 3600
# Rule -> (settings group, severity, title, phrase used when it clears).
RULES = {
    'device_offline': ('device_offline', 'warning', 'Device stopped responding', 'is responding again'),
    'ups_on_battery': ('ups_power', 'critical', 'UPS on battery', 'is back on line power'),
    'ups_overloaded': ('ups_power', 'critical', 'UPS overloaded', 'is no longer overloaded'),
    'ups_battery_low': ('ups_battery', 'critical', 'UPS battery low', 'battery charge has recovered'),
    'ups_replace_battery': ('ups_battery', 'warning', 'UPS battery needs replacement', 'no longer reports a battery replacement'),
    'printer_supply_low': ('printer_supply', 'info', 'Printer supply low', 'supply level has recovered'),
    'sensor_battery_low': ('sensor_battery', 'warning', 'Sensor battery low', 'battery level has recovered'),
    'custom': ('custom', 'warning', 'Custom alert', 'is back within its limit'),
}
REACHABLE = ('online', 'reachable', 'auth_required')
SUPPLY_METRIC = re.compile(r'ipp_supply_\d+_[0-9a-f]{8}_pct')
BATTERY_HYSTERESIS = 5  # Points above the threshold before a battery alert clears, so it doesn't flap.
SENSOR_READING_MAX_AGE = 48 * 3600  # Sleeping sensors report rarely; older battery readings are unknown.
RESOLVED_HISTORY_DAYS = 7
WEBHOOK_TIMEOUT = 5
WEBHOOK_FORMATS = ('ntfy', 'json')
RFC1918 = [ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]


class WebhookError(Exception):
    """A delivery or configuration problem with a message safe to show in the dashboard."""


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float('inf')


def settings_from(stored):
    """Merge saved settings over the defaults, ignoring anything malformed."""
    result = json.loads(json.dumps(DEFAULT_SETTINGS))
    if not isinstance(stored, dict):
        return result
    for group, defaults in DEFAULT_SETTINGS.items():
        saved = stored.get(group)
        if isinstance(defaults, bool):
            if isinstance(saved, bool):
                result[group] = saved
            continue
        if group == 'custom':
            if isinstance(saved, list):
                for item in saved[:MAX_CUSTOM_RULES]:
                    try:
                        result['custom'].append(custom_rule(item))
                    except ValueError:
                        continue  # Skip a damaged rule rather than losing the others.
            continue
        if not isinstance(saved, dict):
            continue
        if isinstance(saved.get('enabled'), bool):
            result[group]['enabled'] = saved['enabled']
        for field, (low, high) in LIMITS.items():
            value = saved.get(field)
            if field in defaults and isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                result[group][field] = value
    return result


def custom_rule(item):
    """Return a complete, normalized custom rule or raise ValueError."""
    if not isinstance(item, dict) or set(item) != set(CUSTOM_FIELDS):
        raise ValueError('Custom rules need a device, reading, condition, limit and duration.')
    if not isinstance(item['id'], str) or not re.fullmatch(r'[0-9a-f]{8}', item['id']):
        raise ValueError('Unknown custom rule.')
    if not isinstance(item['enabled'], bool):
        raise ValueError('enabled must be true or false.')
    for field, limit in (('name', 80), ('label', 80), ('unit', 20)):
        value = item[field]
        if not isinstance(value, str) or len(value) > limit or re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError(f'Use a {field} of up to {limit} characters.')
    device_id = item['device_id']
    if isinstance(device_id, bool) or not isinstance(device_id, int) or not 0 < device_id <= 2**63 - 1:
        raise ValueError('Choose a device from your inventory.')
    metric = item['metric']
    if not isinstance(metric, str) or not 1 <= len(metric) <= 200 or re.search(r'[\x00-\x1f\x7f]', metric):
        raise ValueError('Choose a reading from the device.')
    if item['op'] not in CUSTOM_OPERATORS:
        raise ValueError('Choose below or above.')
    if not number(item['value']) or abs(item['value']) > 1e9:
        raise ValueError('Enter a limit between -1,000,000,000 and 1,000,000,000.')
    minutes = item['minutes']
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 1440:
        raise ValueError('Choose 0–1440 minutes.')
    if item['severity'] not in SEVERITIES:
        raise ValueError('Choose info, warning or critical.')
    return {**{field: item[field] for field in CUSTOM_FIELDS}, 'name': item['name'].strip(), 'value': float(item['value'])}


def _custom_rules(update, previous, devices):
    """Validate the dashboard's full custom rule list. Label and unit come from the device's own reading."""
    if not isinstance(update, list) or len(update) > MAX_CUSTOM_RULES:
        raise ValueError(f'Add up to {MAX_CUSTOM_RULES} custom rules.')
    previous = {rule['id']: rule for rule in previous}
    by_id = {device['device_id']: device for device in devices}
    rules, seen = [], set()
    for item in update:
        if not isinstance(item, dict) or set(item) - set(CUSTOM_FIELDS):
            raise ValueError('Unexpected custom rule properties.')
        item = {'enabled': True, 'name': '', 'minutes': 0, 'severity': 'warning', **item}
        if 'id' in item and item['id'] not in previous:
            raise ValueError('That custom rule no longer exists. Reload the page.')
        rule_id = item.get('id') or secrets.token_hex(4)
        if rule_id in seen:
            raise ValueError('Each custom rule can appear once.')
        seen.add(rule_id)
        device = by_id.get(item.get('device_id')) if type(item.get('device_id')) is int else None
        if device is None:
            raise ValueError('Choose a device from your inventory.')
        reading = (device.get('latest_metrics') or {}).get(item.get('metric'))
        old = previous.get(rule_id)
        if reading and number(reading.get('value')):
            label, unit = str(reading.get('label') or item['metric'])[:80], str(reading.get('unit') or '')[:20]
        elif old and (old['device_id'], old['metric']) == (item['device_id'], item.get('metric')):
            label, unit = old['label'], old['unit']  # A rule keeps working while its device is quiet.
        else:
            raise ValueError('Choose a numeric reading this device has reported.')
        rules.append(custom_rule({**item, 'id': rule_id, 'label': label, 'unit': unit}))
    return rules


def validate_settings(data, current, devices=()):
    """Apply a partial settings update strictly. Raises ValueError with a message for the dashboard."""
    if not isinstance(data, dict) or not data or set(data) - set(DEFAULT_SETTINGS):
        raise ValueError('Unexpected alert settings.')
    result = settings_from(current)
    for group, update in data.items():
        defaults = DEFAULT_SETTINGS[group]
        if group == 'custom':
            result['custom'] = _custom_rules(update, result['custom'], devices)
            continue
        if isinstance(defaults, bool):
            if not isinstance(update, bool):
                raise ValueError(f'{group} must be true or false.')
            result[group] = update
            continue
        if not isinstance(update, dict) or not update or set(update) - set(defaults):
            raise ValueError(f'Unexpected settings for {group}.')
        if 'enabled' in update and not isinstance(update['enabled'], bool):
            raise ValueError('enabled must be true or false.')
        for field, (low, high) in LIMITS.items():
            if field in update:
                value = update[field]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    unit = 'minutes' if field == 'minutes' else 'percent'
                    raise ValueError(f'Choose {low}–{high} {unit}.')
        result[group].update(update)
    return result


def _age_seconds(value, at):
    try:
        stamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return max(0.0, (at - stamp).total_seconds())


def quantity(value, unit):
    text = f'{value:.1f}'.rstrip('0').rstrip('.')
    text = '0' if text == '-0' else text
    text = f'{text} {unit}' if unit else text
    if unit == '°C':
        text += f' ({quantity(value * 9 / 5 + 32, "°F")})'
    return text


def custom_title(rule):
    return rule['name'] or f'{rule["label"]} {rule["op"]} {quantity(rule["value"], rule["unit"])}'


def title_for(rule, subject, settings):
    if rule == 'custom':
        custom = next((item for item in (settings or {}).get('custom', []) if item['id'] == subject), None)
        return custom_title(custom) if custom else 'Custom alert'
    return RULES.get(rule, ('', '', 'Alert', ''))[2]


def _evaluate_custom(devices, settings, at, open_keys, tracker, active, cleared, scope):
    """Custom rules must hold for their duration before opening, and be clear as long before clearing.

    tracker maps a rule's key to (breaching, since, limit): when the latest fresh reading entered
    its current state. It lives in memory, so a restart waits a full duration again.
    """
    by_id = {device['device_id']: device for device in devices}
    live = set()
    for rule in settings.get('custom', []):
        device = by_id.get(rule['device_id'])
        if not rule['enabled'] or device is None:
            continue
        key = ('custom', rule['device_id'], rule['id'])
        live.add(key)
        scope.add(key)
        reading = (device.get('latest_metrics') or {}).get(rule['metric']) or {}
        value, reported = reading.get('value'), reading.get('reported_at')
        age = _age_seconds(reported, at)
        fresh_for = SLEEPING_FRESH_SECONDS if device.get('expected_sleep') else FRESH_SECONDS
        if not number(value) or age is None or age > fresh_for:
            continue  # Unknown: leave any open alert and the timer as they are.
        breaching = value < rule['value'] if rule['op'] == 'below' else value > rule['value']
        limit = (rule['op'], rule['value'])
        state = tracker.get(key)
        if state is None or state[0] != breaching or state[2] != limit:
            state = tracker[key] = (breaching, reported, limit)
        held = (_age_seconds(state[1], at) or 0) >= rule['minutes'] * 60
        if breaching and (held or key in open_keys):
            relation = 'below' if rule['op'] == 'below' else 'above'
            active[key] = {'severity': rule['severity'], 'summary':
                           f'{rule["label"]} is {quantity(value, rule["unit"])}, {relation} the '
                           f'{quantity(rule["value"], rule["unit"])} limit.'}
        elif not breaching and key in open_keys and held:
            cleared.add(key)
    for key in list(tracker):
        if key not in live:
            del tracker[key]


def _ups_flags(metrics):
    state = metrics.get('ups_state', {}).get('value')
    return set(state.split(' · ')) if isinstance(state, str) and state else None


def evaluate(devices, settings, at, open_keys=frozenset(), tracker=None):
    """Return (active, cleared, scope) for the current device inventory.

    active maps (rule, device_id, subject) to {'summary': text} for conditions observed now.
    cleared holds keys whose condition a fresh reading shows is not present.
    scope holds (rule, device_id) pairs the enabled rules apply to. A key that is neither
    active nor cleared but still in scope is unknown; its open alert stays open.
    """
    active, cleared, scope = {}, set(), set()
    enabled = {group for group, value in settings.items() if isinstance(value, dict) and value.get('enabled')}
    for device in devices:
        device_id = device['device_id']
        status = device.get('status')
        fresh = (device.get('metrics') or {}) if status == 'online' else None  # Last-known readings aren't fresh.

        if 'device_offline' in enabled and device.get('pinned') and not device.get('expected_sleep'):
            scope.add(('device_offline', device_id))
            key = ('device_offline', device_id, '')
            minutes = settings['device_offline']['minutes']
            if device.get('ip') and status in REACHABLE:
                cleared.add(key)
            elif (not device.get('ip') or status == 'unreachable') and device.get('last_seen'):
                age = _age_seconds(device['last_seen'], at)
                if age is not None and age >= minutes * 60:
                    active[key] = {'summary': f'Has not responded for at least {minutes} minutes.'}

        if device.get('protocol') == 'nut-ups':
            flags = _ups_flags(fresh) if fresh is not None else None
            checks = []
            if 'ups_power' in enabled:
                checks += [('ups_on_battery', 'On battery', 'Running on battery power.'),
                           ('ups_overloaded', 'Overloaded', 'The UPS reports an overload.')]
            if 'ups_battery' in enabled:
                checks.append(('ups_replace_battery', 'Replace battery', 'The UPS reports that its battery needs replacement.'))
            for rule, flag, summary in checks:
                scope.add((rule, device_id))
                if flags is None:
                    continue  # No fresh status flags: unknown.
                if flag in flags:
                    active[(rule, device_id, '')] = {'summary': summary}
                else:
                    cleared.add((rule, device_id, ''))
            if 'ups_battery' in enabled:
                scope.add(('ups_battery_low', device_id))
                key = ('ups_battery_low', device_id, '')
                charge = fresh.get('ups_battery_pct', {}).get('value') if fresh is not None else None
                charge = charge if number(charge) else None
                threshold = settings['ups_battery']['pct']
                below = charge is not None and (charge < threshold + BATTERY_HYSTERESIS if key in open_keys
                                                else charge <= threshold)
                if (flags is not None and 'Low battery' in flags) or below:
                    active[key] = {'summary': f'Battery charge is {charge:.0f}%.' if charge is not None
                                   else 'The UPS reports a low battery.'}
                elif charge is not None or flags is not None:
                    cleared.add(key)

        if device.get('protocol') == 'ipp' and 'printer_supply' in enabled:
            scope.add(('printer_supply_low', device_id))
            if fresh is not None:
                threshold = settings['printer_supply']['pct']
                for metric, reading in fresh.items():
                    if not SUPPLY_METRIC.fullmatch(metric) or not number(reading.get('value')):
                        continue  # A supply that isn't reported is unknown, never replaced.
                    key = ('printer_supply_low', device_id, metric)
                    if reading['value'] <= threshold:
                        active[key] = {'summary': f'{reading.get("label") or "Supply"} is at {reading["value"]:.0f}%.'}
                    else:
                        cleared.add(key)

        battery = (device.get('latest_metrics') or {}).get('battery_pct')
        if 'sensor_battery' in enabled and battery and device.get('protocol') != 'nut-ups':
            scope.add(('sensor_battery_low', device_id))
            age = _age_seconds(battery.get('reported_at'), at)
            if number(battery.get('value')) and age is not None and age <= SENSOR_READING_MAX_AGE:
                key = ('sensor_battery_low', device_id, '')
                threshold = settings['sensor_battery']['pct']
                low = (battery['value'] < threshold + BATTERY_HYSTERESIS if key in open_keys
                       else battery['value'] <= threshold)
                if low:
                    active[key] = {'summary': f'Battery is at {battery["value"]:.0f}%.'}
                else:
                    cleared.add(key)
    _evaluate_custom(devices, settings, at, open_keys, {} if tracker is None else tracker, active, cleared, scope)
    return active, cleared, scope


def open_keys(db):
    return {(row[0], row[1], row[2]) for row in db.execute(
        'SELECT rule, device_id, subject FROM alerts WHERE resolved_at IS NULL')}


def _notification(event, row, device, at, settings):
    rule = row['rule']
    cleared_phrase = RULES.get(rule, ('', '', '', 'has cleared'))[3]
    title = title_for(rule, row['subject'], settings)
    name = device['name'] if device else 'A removed device'
    if event == 'opened':
        text = f'{name}: {row["summary"]}'
    elif rule == 'custom':
        text = f'{name}: {title} has cleared.'
    else:
        text = f'{name} {cleared_phrase}.'
    return {'event': event, 'id': row['id'], 'rule': rule, 'severity': row['severity'], 'title': title, 'text': text,
            'summary': row['summary'], 'device': {'name': name, 'room': device['room'] if device else None},
            'ip': device.get('ip') if device else None, 'opened_at': row['opened_at'],
            'resolved_at': at if event == 'resolved' else None}


def sync(db, evaluation, devices, at, settings=None):
    """Open, update and resolve alert rows inside the caller's transaction.

    Returns notifications for alerts that opened or cleared. Alerts whose rule was turned
    off or deleted, whose device left the rule's scope, or whose device was removed resolve quietly.
    """
    active, cleared, scope = evaluation
    by_id = {device['device_id']: device for device in devices}
    rows = {(row['rule'], row['device_id'], row['subject']): dict(row) for row in db.execute(
        'SELECT id, rule, device_id, subject, severity, summary, opened_at FROM alerts WHERE resolved_at IS NULL')}
    notes = []
    for key, info in sorted(active.items()):
        rule, device_id, subject = key
        severity = info.get('severity') or RULES[rule][1]
        if key in rows:
            if (rows[key]['summary'], rows[key]['severity']) != (info['summary'], severity):
                db.execute('UPDATE alerts SET summary=?, severity=?, updated_at=? WHERE id=?',
                           (info['summary'], severity, at, rows[key]['id']))
            continue
        alert_id = db.execute('''INSERT INTO alerts(rule, device_id, subject, severity, summary, opened_at, updated_at)
                                 VALUES(?,?,?,?,?,?,?)''', (rule, device_id, subject, severity, info['summary'], at, at)).lastrowid
        row = {'id': alert_id, 'rule': rule, 'device_id': device_id, 'subject': subject, 'severity': severity,
               'summary': info['summary'], 'opened_at': at}
        notes.append(_notification('opened', row, by_id.get(device_id), at, settings))
    for key, row in rows.items():
        if key in active:
            continue
        rule, device_id, _ = key
        if key in cleared:
            db.execute('UPDATE alerts SET resolved_at=?, updated_at=? WHERE id=?', (at, at, row['id']))
            notes.append(_notification('resolved', row, by_id.get(device_id), at, settings))
        elif (rule, device_id) not in scope and key not in scope:  # Custom rules are scoped per rule.
            db.execute('UPDATE alerts SET resolved_at=?, updated_at=? WHERE id=?', (at, at, row['id']))
    return notes


def list_alerts(db, devices, at, settings=None):
    by_id = {device['device_id']: device for device in devices}
    since = datetime.fromtimestamp(at.timestamp() - RESOLVED_HISTORY_DAYS * 86400, timezone.utc).isoformat(timespec='seconds')

    def shape(row):
        item = dict(row)
        device = by_id.get(item['device_id'])
        item['title'] = title_for(item['rule'], item['subject'], settings)
        item['device_name'] = device['name'] if device else 'Removed device'
        item['room'] = device['room'] if device else None
        return item
    fields = 'id, rule, device_id, subject, severity, summary, opened_at, updated_at, resolved_at, acknowledged_at'
    severity = "CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END"
    current = [shape(row) for row in db.execute(
        f'SELECT {fields} FROM alerts WHERE resolved_at IS NULL ORDER BY {severity}, opened_at DESC, id DESC')]
    recent = [shape(row) for row in db.execute(
        f'SELECT {fields} FROM alerts WHERE resolved_at >= ? ORDER BY resolved_at DESC, id DESC LIMIT 25', (since,))]
    return {'open': current, 'recent': recent}


def acknowledge(db, alert_id, at):
    return db.execute('UPDATE alerts SET acknowledged_at=? WHERE id=? AND resolved_at IS NULL AND acknowledged_at IS NULL',
                      (at, alert_id)).rowcount > 0


def cleanup(db, cutoff):
    db.execute('DELETE FROM alerts WHERE resolved_at IS NOT NULL AND resolved_at < ?', (cutoff,))


# Webhook delivery ------------------------------------------------------------------------

def webhook_path(database):
    database = Path(database).resolve()
    return database.with_suffix(database.suffix + '.alert-webhook.json')


def validate_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048 or re.search(r'[\s\x00-\x1f\x7f]', url):
        raise ValueError('Enter a webhook URL of up to 2,048 characters.')
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ValueError('Enter a valid webhook URL.') from None
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        raise ValueError('Use an http:// or https:// webhook URL.')
    if parts.username or parts.password:
        raise ValueError('Put credentials in the URL path or token, not before the host name.')
    if port == 0:
        raise ValueError('Enter a valid webhook port.')
    if parts.scheme == 'http':
        # Alert text names devices and rooms. Unencrypted delivery stays on this computer or private network.
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        private = address is not None and address.version == 4 and (address.is_loopback or any(address in net for net in RFC1918))
        if not private and parts.hostname.lower() != 'localhost':
            raise ValueError('Use https:// for webhooks outside your local network. Plain http:// is limited to private IPv4 addresses.')
    return url


def destination(url):
    parts = urlsplit(url)
    host = parts.hostname or ''
    if ':' in host:
        host = f'[{host}]'
    return f'{parts.scheme}://{host}' + (f':{parts.port}' if parts.port else '')


def load_webhook(database):
    """Return {'url', 'format'} or None. The file is private to the collector's OS account."""
    path = webhook_path(database)
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
        with os.fdopen(os.open(path, flags), 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or (os.name == 'posix' and
                    (info.st_uid != os.getuid() or info.st_mode & 0o077)):
                raise WebhookError('The webhook file must be a private file owned by the collector user. Save it again.')
            raw = handle.read(8193)
        config = json.loads(raw) if len(raw) <= 8192 else None
        if not isinstance(config, dict) or config.get('format') not in WEBHOOK_FORMATS:
            raise ValueError
        return {'url': validate_url(config.get('url')), 'format': config['format']}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        raise WebhookError('The webhook settings could not be read. Save them again.') from None


def save_webhook(database, url, fmt):
    path = webhook_path(database)
    if url is None:
        path.unlink(missing_ok=True)
        return None
    if fmt not in WEBHOOK_FORMATS:
        raise ValueError('Choose ntfy or JSON delivery.')
    config = {'url': validate_url(url), 'format': fmt}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # NamedTemporaryFile creates the file readable only by this account.
    handle = tempfile.NamedTemporaryFile(dir=path.parent, suffix='.alert-webhook.json', delete=False)
    pending = path.parent / os.path.basename(handle.name)
    try:
        with handle:
            handle.write(json.dumps(config).encode())
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return config


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _header_text(value):
    """HTTP header values are Latin-1; ntfy accepts RFC 2047 encoding for anything else."""
    value = value.replace('\r', ' ').replace('\n', ' ')[:250]
    if value.isascii():
        return value
    return '=?UTF-8?B?' + base64.b64encode(value.encode()).decode() + '?='


def request_for(config, note):
    if config['format'] == 'ntfy':
        headers = {'Content-Type': 'text/plain; charset=utf-8', 'Title': _header_text(note['title']),
                   'Priority': '4' if note['severity'] == 'critical' and note['event'] != 'resolved' else '3',
                   'Tags': 'white_check_mark' if note['event'] == 'resolved' else
                           {'critical': 'rotating_light', 'warning': 'warning'}.get(note['severity'], 'information_source')}
        body = note['text'].encode()
    else:
        headers = {'Content-Type': 'application/json'}
        body = json.dumps({'source': 'Local Signal', 'text': note['text'],
                           **{key: note.get(key) for key in ('event', 'rule', 'severity', 'title', 'summary', 'device',
                                                             'opened_at', 'resolved_at')}}).encode()
    headers['User-Agent'] = 'Local-Signal-Alerts/1.0'
    return Request(config['url'], data=body, headers=headers, method='POST')


def send(config, note, timeout=WEBHOOK_TIMEOUT):
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(request_for(config, note), timeout=timeout) as response:
            response.read(4096)
            if not 200 <= response.status < 300:
                raise WebhookError(f'The webhook returned HTTP {response.status}.')
    except HTTPError as exc:
        exc.close()
        raise WebhookError(f'The webhook returned HTTP {exc.code}; redirects are not followed.' if 300 <= exc.code < 400
                           else f'The webhook returned HTTP {exc.code}.') from None
    except (URLError, OSError) as exc:
        reason = getattr(exc, 'reason', exc)
        raise WebhookError(f'Could not reach the webhook: {str(reason)[:160]}') from None


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class Notifier:
    """Deliver alert notifications in the background so slow webhooks never delay collection."""

    def __init__(self, database):
        self.database = Path(database)
        self.queue = queue.Queue(maxsize=200)
        self.lock = threading.Lock()
        self.state = {'last_sent_at': None, 'last_error': None, 'last_error_at': None, 'dropped': 0}

    def enqueue(self, notes):
        for note in notes:
            try:
                self.queue.put_nowait(note)
            except queue.Full:
                with self.lock:
                    self.state['dropped'] += 1

    def deliver(self, note):
        try:
            config = load_webhook(self.database)
            if config is None:
                return False
            send(config, note)
        except WebhookError as exc:
            with self.lock:
                self.state.update(last_error=str(exc), last_error_at=now())
            raise
        with self.lock:
            self.state.update(last_sent_at=now(), last_error=None, last_error_at=None)
        return True

    def run(self, stop):
        while not stop.is_set():
            try:
                note = self.queue.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.deliver(note)
            except WebhookError as exc:
                print(f'Alert notification failed: {exc}', flush=True)

    def snapshot(self):
        with self.lock:
            return dict(self.state)


TEST_NOTE = {'event': 'test', 'rule': 'test', 'severity': 'info', 'title': 'Local Signal test',
             'text': 'Local Signal can deliver alerts to this webhook.', 'summary': 'Test notification.',
             'device': None, 'opened_at': None, 'resolved_at': None}
