#!/usr/bin/env python3
"""Synthetic history and polling capacity checks; never uses LAN or real data."""
import argparse
import csv
from datetime import datetime, timedelta, timezone
import gc
import io
import json
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def rss_mib():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == 'darwin' else 1024), 2)


def worker(rows, devices, mode, delay):
    import server
    with tempfile.TemporaryDirectory(prefix='local-signal-capacity-') as folder:
        store = server.Store(Path(folder) / 'synthetic.sqlite3')
        for n in range(devices):
            store.add_device(f'10.{1 + n // 254}.0.{1 + n % 254}', f'Synthetic device {n + 1}')
        inventory = store.devices()
        cycles = (rows + devices * 8 - 1) // (devices * 8)
        base = datetime.now(timezone.utc) - timedelta(seconds=cycles * 30)
        stamps = [(base + timedelta(seconds=step * 30)).isoformat() for step in range(cycles)]
        def values():
            for n in range(rows):
                device = inventory[(n // 8) % devices]
                yield (device['device_id'], device['ip'], stamps[n // (devices * 8)], f'measure_{n % 8}', n % 100, '%', 'Synthetic reading')
        store.db.executemany('INSERT INTO samples(device_id,ip,at,metric,value,unit,label) VALUES(?,?,?,?,?,?,?)', values())
        store.db.commit()
        gc.collect()
        before, started = rss_mib(), time.perf_counter()
        if mode == 'legacy-csv':
            data = store.history(hours=720)
            output = io.StringIO(newline='')
            writer = csv.DictWriter(output, fieldnames=['ip', 'at', 'metric', 'value', 'unit', 'label', 'device_id'])
            writer.writeheader()
            writer.writerows({key: server.csv_value(value) for key, value in row.items()} for row in data)
            size = len(output.getvalue().encode('utf-8-sig'))
        elif mode == 'csv':
            size = sum(len(chunk) for chunk in store.csv_chunks(hours=720))
        elif mode == 'legacy-chart':
            # Reproduce the original all-series query without today's budget.
            span = store.db.execute('SELECT MIN(at),MAX(at) FROM samples').fetchone()
            seconds = (datetime.fromisoformat(span[1])-datetime.fromisoformat(span[0])).total_seconds()
            bucket = max(server.POLL_SECONDS, int(seconds/300))
            data = [dict(row) for row in store.db.execute('''
                SELECT ip,MIN(at) AS at,metric,AVG(value) AS value,unit,label,device_id FROM samples
                GROUP BY COALESCE(device_id,ip),metric,CAST(strftime('%s',at) AS INTEGER)/? ORDER BY at''', (bucket,))]
            size = len(json.dumps(data).encode())
        elif mode in ('chart', 'chart-all', 'chart-device'):
            data = store.chart_history(hours=720, metric=None if mode == 'chart-all' else 'measure_0',
                                       device_id=inventory[0]['device_id'] if mode == 'chart-device' else None)
            size = len(json.dumps(data).encode())
        elif mode == 'state':
            size = len(json.dumps(store.devices()).encode())
        else:
            # Exercise the real pool, normalization storage, cleanup and state
            # without performing any network discovery or device requests.
            def probe(device):
                time.sleep(delay)
                return {'checked_at': server.now(), 'status': 'unreachable', 'reachable': False,
                        'metrics': {}, 'error': 'Synthetic unavailable device'}
            with patch.object(server, 'network_info', return_value={}), patch.object(server, 'probe_device', side_effect=probe):
                collector = server.Collector(store)
                collector.poll()
                size = len(json.dumps(collector.snapshot()).encode())
        result = {'mode': mode, 'rows': rows, 'devices': devices, 'seconds': round(time.perf_counter() - started, 3),
                  'before_peak_rss_mib': before, 'peak_rss_mib': rss_mib(), 'response_mib': round(size / 1048576, 2)}
        if mode == 'poll':
            result.update(delay_seconds=delay, poll_over_budget=collector.snapshot()['poll_over_budget'])
        elif mode in ('chart', 'chart-all', 'chart-device'):
            result.update(chart_points=len(data['samples']), catalog_metrics=len(data['metrics']),
                          points_limited=data['points_limited'])
        store.db.close()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=500_000)
    parser.add_argument('--devices', type=int, default=100)
    parser.add_argument('--mode', choices=['legacy-csv', 'csv', 'legacy-chart', 'chart', 'chart-all', 'chart-device', 'state', 'poll'], default='csv')
    parser.add_argument('--delay', type=float, default=0, help='Synthetic per-device seconds for poll mode (0–10).')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.rows <= 2_000_000 or not 1 <= args.devices <= 1000:
        parser.error('Use 1–2,000,000 rows and 1–1,000 devices.')
    if not 0 <= args.delay <= 10:
        parser.error('Use a delay from zero to ten seconds.')
    if args.worker:
        print(json.dumps(worker(args.rows, args.devices, args.mode, args.delay)))
    else:
        subprocess.run([sys.executable, __file__, '--worker', '--rows', str(args.rows), '--devices', str(args.devices), '--mode', args.mode,
                        '--delay', str(args.delay)], check=True)


if __name__ == '__main__':
    main()
