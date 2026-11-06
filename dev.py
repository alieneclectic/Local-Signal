#!/usr/bin/env python3
"""Run the loopback development server and restart it on backend edits."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent


def backend_snapshot():
    paths = [*ROOT.glob('*.py'), *ROOT.glob('firmware/**/*.py'), *ROOT.glob('firmware/catalog/*.json')]
    snapshot = {}
    for path in paths:
        if path == ROOT / 'dev.py':
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8800)
    parser.add_argument('--data', type=Path, default=ROOT / '.data' / 'dev' / 'hearth.sqlite3')
    parser.add_argument('--no-collector', action='store_true', help='Skip automatic polling and the sensor listener.')
    args = parser.parse_args()
    command = [str(ROOT / 'server.py'), '--host', '127.0.0.1', '--port', str(args.port), '--data', str(args.data.resolve())]
    if args.no_collector:
        command.append('--no-collector')

    worker = None
    previous = None
    # Ignore cached bytecode even when a module changes twice within a second.
    with TemporaryDirectory(prefix='local-signal-dev-') as cache:
        try:
            while True:
                snapshot = backend_snapshot()
                if snapshot != previous:
                    if worker is not None:
                        stop_server(worker)
                    if previous is not None:
                        print('Backend source changed. Restarting Local Signal.', flush=True)
                    env = {**os.environ, 'LOCAL_SIGNAL_DEV_RELOAD': uuid4().hex}
                    worker = subprocess.Popen([sys.executable, '-B', '-X', f'pycache_prefix={cache}', *command], cwd=ROOT, env=env)
                    previous = snapshot
                elif worker is not None and worker.poll() is not None:
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
