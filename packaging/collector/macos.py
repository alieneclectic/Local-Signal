"""Manage the packaged app's own login agent from the signed app."""

import argparse
from pathlib import Path
import plistlib
import shlex
import socket
import subprocess
import sys

import background


def app_identity():
    contents = Path(__file__).resolve().parents[3]
    info = plistlib.loads((contents / 'Info.plist').read_bytes())
    return info['CFBundleIdentifier'], contents / 'MacOS' / info['CFBundleExecutable']


def migration_preview_command():
    try:
        _, launcher = app_identity()
    except (OSError, ValueError, KeyError):
        launcher = Path(sys.argv[0])  # Still provide stderr guidance if bundle metadata is damaged.
    return shlex.join([str(launcher), '--migrate-legacy'])


def show_migration_dialog(command):
    script = '''on run argv
        display dialog (item 1 of argv) with title "Local Signal migration needed" default answer (item 2 of argv) buttons {"OK"} default button "OK" giving up after 30
    end run'''
    message = ('Your existing Mac collector has saved history. To preview moving it into this app, '
               'copy the command below and run it in Terminal. The preview makes no changes. '
               'After reviewing it, add --apply to migrate. No empty database was created.')
    try:
        subprocess.run(['/usr/bin/osascript', '-e', script, message, command],
                       capture_output=True, text=True, check=True, timeout=35)
        return True
    except (OSError, subprocess.SubprocessError):
        return False  # The caller always retains the same guidance on stderr.


def login_config(data, host='127.0.0.1', port=8799, *, home=None):
    label, launcher = app_identity()
    plist = (Path.home() if home is None else Path(home)) / 'Library' / 'LaunchAgents' / f'{label}.plist'
    config = plistlib.dumps(background.agent_config(
        None, data.parent, data, launcher=launcher, label=label,
        launcher_args=['--background', '--host', host, '--port', str(port)], host=host))
    return label, launcher, plist, config


def manage_login(action, argv, data):
    parser = argparse.ArgumentParser(prog=f'Local Signal {action}')
    if action == '--enable-background':
        parser.add_argument('--host', default='127.0.0.1', choices=['127.0.0.1', '0.0.0.0'])
        parser.add_argument('--port', type=int, default=8799, metavar='PORT')
    args = parser.parse_args(argv)

    label, launcher, plist, config = login_config(data, getattr(args, 'host', '127.0.0.1'),
                                                  getattr(args, 'port', 8799))
    target = f'{background.DOMAIN}/{label}'

    if action == '--disable-background':
        if background.launchctl('print', target, check=False).returncode == 0:
            background.launchctl('bootout', target)
        plist.unlink(missing_ok=True)
        print('Login startup removed. The database and backups were kept.')
        return

    if not 1 <= args.port <= 65535:
        parser.error('Choose a port between 1 and 65535.')
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
