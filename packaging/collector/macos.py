"""Manage the packaged app's own login agent from the signed app."""

import argparse
from pathlib import Path
import plistlib
import socket

import background


def manage_login(action, argv, data):
    parser = argparse.ArgumentParser(prog=f'Local Signal {action}')
    if action == '--enable-background':
        parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
        parser.add_argument('--port', type=int, default=8799, metavar='PORT')
    args = parser.parse_args(argv)

    contents = Path(__file__).resolve().parents[3]
    info = plistlib.loads((contents / 'Info.plist').read_bytes())
    label = info['CFBundleIdentifier']
    launcher = contents / 'MacOS' / info['CFBundleExecutable']
    plist = Path.home() / 'Library' / 'LaunchAgents' / f'{label}.plist'
    target = f'{background.DOMAIN}/{label}'

    if action == '--disable-background':
        if background.launchctl('print', target, check=False).returncode == 0:
            background.launchctl('bootout', target)
        plist.unlink(missing_ok=True)
        print('Login startup removed. The database and backups were kept.')
        return

    if not 1 <= args.port <= 65535:
        parser.error('Choose a port between 1 and 65535.')
    config = plistlib.dumps(background.agent_config(
        None, data.parent, data, launcher=launcher, label=label,
        launcher_args=['--background', '--host', args.host, '--port', str(args.port)],
    ))
    if plist.exists() and plist.read_bytes() != config:
        parser.error('Run --disable-background before changing the login configuration.')
    if background.launchctl('print', target, check=False).returncode == 0:
        print('Login startup is already enabled.')
        return
    try:
        with socket.socket() as listener:
            listener.bind((args.host, args.port))
    except OSError as exc:
        parser.error(f'Cannot use port {args.port}: {exc}. Choose an available port.')

    (data.parent / 'logs').mkdir(parents=True, exist_ok=True)
    plist.parent.mkdir(parents=True, exist_ok=True)
    temporary = plist.with_suffix('.tmp')
    temporary.write_bytes(config)
    temporary.replace(plist)
    try:
        background.launchctl('enable', target)
        background.launchctl('bootstrap', background.DOMAIN, str(plist))
    except Exception:
        plist.unlink(missing_ok=True)
        raise
    print(f'Login startup enabled. Open http://127.0.0.1:{args.port}/ to check the collector.')
