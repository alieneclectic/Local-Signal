"""Heating summaries from identified meters, with explicit observation coverage."""
from datetime import datetime, time, timedelta, timezone
from bisect import bisect_right
import math
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_INTERVAL_SECONDS = 120
DEFAULT_SETTINGS = {'rate_per_kwh': None, 'timezone': 'America/New_York', 'meter_macs': []}


def identity(mac):
    value = re.sub('[^0-9A-F]', '', str(mac or '').upper())
    return value if len(value) == 12 else None


def meter_devices(devices):
    meters = {}
    for device in devices:
        mac = identity(device.get('mac'))
        metrics = {**device.get('last_metrics', {}), **device.get('latest_metrics', {}), **device.get('metrics', {})}
        if mac and 'energy_kwh' in metrics:
            if mac not in meters or (device.get('last_telemetry') or '') > (meters[mac].get('last_telemetry') or ''):
                meters[mac] = device
    return meters


def validate_settings(data, devices):
    if set(data) != set(DEFAULT_SETTINGS):
        raise ValueError('Provide an electricity rate, time zone, and heating meters.')
    rate = data['rate_per_kwh']
    if rate is not None and (isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not 0 <= rate <= 10):
        raise ValueError('Enter a rate from $0 to $10 per kWh, or leave it blank.')
    try:
        ZoneInfo(data['timezone'])
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        raise ValueError('Enter a valid time zone, such as America/New_York.')
    macs = data['meter_macs']
    if not isinstance(macs, list) or len(macs) > 100 or any(not isinstance(m, str) or m not in meter_devices(devices) for m in macs):
        raise ValueError('Choose identified energy meters from your inventory.')
    return {'rate_per_kwh': rate, 'timezone': data['timezone'], 'meter_macs': sorted(set(macs))}


def window(days, zone, at=None):
    at = at or datetime.now(timezone.utc)
    local_day = at.astimezone(ZoneInfo(zone)).date()
    dates = [local_day - timedelta(days=i) for i in reversed(range(days))]
    edges = [datetime.combine(d, time.min, ZoneInfo(zone)).astimezone(timezone.utc) for d in dates]
    return at, dates, edges + [at]


def summarize(readings, devices, settings, days=7, at=None):
    if days not in (1, 7, 30):
        raise ValueError('Choose Today, 7 days, or 30 days.')
    at, dates, edges = window(days, settings['timezone'], at)
    meters = meter_devices(devices)
    selected = settings['meter_macs']
    per_source = {mac: [{'kwh': 0.0, 'seconds': 0.0, 'intervals': 0} for _ in dates] for mac in selected}
    previous, issues, first, latest = {}, {m: {'gaps': 0, 'resets': 0} for m in selected}, {}, {}
    for row in readings:
        mac = row['source_id']
        if mac not in per_source:
            continue
        stamp = datetime.fromisoformat(row['at'])
        if stamp > at:
            continue
        first.setdefault(mac, row['at'])
        latest[mac] = row['at']
        before = previous.get(mac)
        previous[mac] = row
        if before is None:
            continue  # The first cumulative counter is a baseline, never usage.
        start = datetime.fromisoformat(before['at'])
        seconds = (stamp - start).total_seconds()
        if seconds <= 0 or stamp <= edges[0]:
            continue
        delta = row['counter_kwh'] - before['counter_kwh']
        rebooted = row['uptime_s'] is not None and before['uptime_s'] is not None and row['uptime_s'] < before['uptime_s']
        if delta < 0 or rebooted:
            issues[mac]['resets'] += 1
            continue
        if seconds > MAX_INTERVAL_SECONDS:
            issues[mac]['gaps'] += 1
            continue
        # Split only short observed intervals at local day boundaries. The daily
        # allocation assumes even use within this <=120-second interval.
        first_day = max(0, bisect_right(edges, start) - 1)
        for i in range(first_day, min(days, first_day + 2)):
            overlap = (min(stamp, edges[i + 1]) - max(start, edges[i])).total_seconds()
            if overlap > 0:
                bucket = per_source[mac][i]
                bucket['kwh'] += delta * overlap / seconds
                bucket['seconds'] += overlap
                bucket['intervals'] += 1

    rate = settings['rate_per_kwh']

    def totals(buckets, possible):
        observed = sum(b['seconds'] for b in buckets)
        kwh = sum(b['kwh'] for b in buckets) if any(b['intervals'] for b in buckets) else None
        return {'kwh': round(kwh, 6) if kwh is not None else None,
                'cost': round(kwh * rate, 4) if kwh is not None and rate is not None else None,
                'coverage_pct': round(min(100, observed / possible * 100), 1) if possible else 0,
                'observed_hours': round(observed / 3600, 3)}

    period = (at - edges[0]).total_seconds()
    sources = []
    for mac, buckets in per_source.items():
        device = meters.get(mac, {})
        sources.append({'source_id': mac, 'name': device.get('name', 'Saved heating meter'),
                        'room': device.get('room', 'Unassigned'), 'ip': device.get('ip'),
                        'first_reading': first.get(mac), 'last_reading': latest.get(mac),
                        **totals(buckets, period), **issues[mac]})
    daily = []
    for i, date in enumerate(dates):
        day_seconds = (edges[i + 1] - edges[i]).total_seconds()
        daily.append({'date': date.isoformat(), **totals([per_source[m][i] for m in selected], day_seconds * len(selected))})
    return {'days': days, 'start': edges[0].isoformat(), 'end': at.isoformat(), 'settings': settings,
            'sources': sources, 'daily': daily,
            'total': totals([b for buckets in per_source.values() for b in buckets], period * len(selected)),
            'max_interval_seconds': MAX_INTERVAL_SECONDS}
