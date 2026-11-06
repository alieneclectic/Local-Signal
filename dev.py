#!/usr/bin/env python3
"""Run Local Signal and restart it on backend edits."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent


def backend_snapshot(source=ROOT):
    paths = [*source.glob('*.py'), *source.glob('firmware/**/*.py'), *source.glob('firmware/catalog/*.json'),
             *source.glob('routers/**/*.py')]
    snapshot = {}
    for path in paths:
        if path == source / 'dev.py':
            continue
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        snapshot[path] = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    return snapshot


def stop_server(worker):
    if worker.poll() is None:
        worker.terminate()
        try:
            worker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            worker.kill()
            worker.wait()


def checkout_snapshot(checkout):
    if checkout:
        try:
            if (checkout / 'server.py').is_file() and (checkout / 'web/index.html').is_file():
                return checkout, backend_snapshot(checkout)
        except OSError:
            pass
    return ROOT, backend_snapshot(ROOT)


def start_server(source, args, cache):
    env = {**os.environ, 'LOCAL_SIGNAL_DEV_RELOAD': uuid4().hex,
           'LOCAL_SIGNAL_CODE_SOURCE': 'installed' if args.checkout and source == ROOT else 'checkout'}
    command = [str(source / 'server.py'), '--host', args.host, '--port', str(args.port),
               '--data', str(args.data.resolve())]
    if args.no_collector:
        command.append('--no-collector')
    if args.background:
        command.append('--background')
    return subprocess.Popen([sys.executable, '-B', '-X', f'pycache_prefix={cache}', *command],
                            cwd=source, env=env)


def checkout_loads(source, cache):
    try:
        subprocess.run([sys.executable, '-B', '-X', f'pycache_prefix={cache}', '-c', 'import server'],
                       cwd=source, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        print('The checkout could not load. Using the installed snapshot.', flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8800)
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'), default='127.0.0.1')
    parser.add_argument('--data', type=Path, default=ROOT / '.data' / 'dev' / 'hearth.sqlite3')
    parser.add_argument('--no-collector', action='store_true', help='Skip automatic polling and the sensor listener.')
    parser.add_argument('--background', action='store_true', help='Use background collection and logging.')
    parser.add_argument('--checkout', type=Path, help='Follow this checkout, with this script’s directory as the fallback.')
    args = parser.parse_args()
    stopping = False

    def stop_on_term(_signum, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop_on_term)
    worker = worker_source = None
    previous = None
    # Ignore cached bytecode even when a module changes twice within a second.
    with TemporaryDirectory(prefix='local-signal-dev-') as cache:
        try:
            while not stopping:
                revision = checkout_snapshot(args.checkout)
                if revision != previous:
                    previous = revision
                    source, _ = revision
                    if source != ROOT and not checkout_loads(source, cache):
                        if worker_source == ROOT and worker.poll() is None:
                            continue
                        source = ROOT
                    if stopping:
                        break
                    if worker is not None:
                        print('Backend source changed. Restarting Local Signal.', flush=True)
                        stop_server(worker)
                    try:
                        worker = start_server(source, args, cache)
                        worker_source = source
                    except OSError:
                        if source == ROOT:
                            raise
                        worker = start_server(ROOT, args, cache)
                        worker_source = ROOT
                elif worker is not None and worker.poll() is not None:
                    if args.checkout and worker_source != ROOT:
                        print('The checkout collector exited. Using the installed snapshot.', flush=True)
                        worker = start_server(ROOT, args, cache)
                        worker_source = ROOT
                        continue
                    if args.background:
                        raise SystemExit(worker.returncode or 1)
                    print('Server exited. Waiting for a backend edit or Control-C.', flush=True)
                    worker = None
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            if worker is not None:
                stop_server(worker)


if __name__ == '__main__':
    main()
