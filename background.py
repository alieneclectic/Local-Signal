#!/usr/bin/env python3
"""Install, inspect, or remove Hearth's per-user macOS background collector."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parent
LABEL = 'local.hearth.collector'
SUPPORT = Path.home() / 'Library' / 'Application Support' / 'Hearth'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / f'{LABEL}.plist'
DOMAIN = f'gui/{os.getuid()}'
FILES = ['server.py', 'coiot.py', 'network_names.py', 'media_readers.py', 'energy.py', 'reliability.py', 'background.py', 'requirements.txt', 'macos/Collector.swift']
FILES += ['firmware/__init__.py', 'firmware/checks.py', 'firmware/adapters/__init__.py',
          'firmware/adapters/shelly.py', 'firmware/adapters/wiim.py', 'firmware/adapters/sonos.py',
          'firmware/adapters/roku.py', 'firmware/adapters/nest.py', 'firmware/catalog/shelly.json']
SYSTEM_PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


def agent_config(python, runtime, data, launcher=None):
    arguments = [str(launcher)] if launcher else [str(python), '-u', str(runtime / 'server.py'),
                                                 '--host', '0.0.0.0', '--data', str(data), '--background']
    return {'Label': LABEL, 'ProgramArguments': arguments, 'AssociatedBundleIdentifiers': [LABEL],
            'WorkingDirectory': str(runtime), 'RunAtLoad': True, 'KeepAlive': True,
            'ThrottleInterval': 15, 'ProcessType': 'Background',
            'EnvironmentVariables': {'PATH': SYSTEM_PATH, 'PYTHONUNBUFFERED': '1'},
            'StandardOutPath': str(data.parent / 'logs' / 'launchd.log'),
            'StandardErrorPath': str(data.parent / 'logs' / 'launchd.log'), 'Umask': 0o077}


def build_app(stage):
    app = SUPPORT / 'Hearth.staging.app'
    if app.exists():
        shutil.rmtree(app)
    executable = app / 'Contents/MacOS/Hearth'
    executable.parent.mkdir(parents=True)
    info = {'CFBundleIdentifier': LABEL, 'CFBundleName': 'Hearth', 'CFBundleDisplayName': 'Hearth',
            'CFBundleExecutable': 'Hearth', 'CFBundlePackageType': 'APPL',
            'CFBundleVersion': '1', 'CFBundleShortVersionString': '1.0', 'LSUIElement': True,
        'NSLocalNetworkUsageDescription': 'Hearth reads device status and measurements on the local network. Readings stay on this Mac.',
            'NSBonjourServices': ['_googlecast._tcp', '_http._tcp', '_sonos._tcp', '_roku-ecp._tcp']}
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', str(stage / 'macos/Collector.swift'), '-o', str(executable)], check=True)
    identities = subprocess.check_output(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'], text=True)
    candidates = re.findall(r'([0-9A-F]{40}) "Developer ID Application:[^"]+"', identities)
    if not candidates:
        candidates = re.findall(r'([0-9A-F]{40}) "Apple Development:[^"]+"', identities)
    if not candidates:
        raise RuntimeError('A macOS signing identity is required for a stable background app identity. Install an Apple signing certificate before background setup.')
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', candidates[0], '--identifier', LABEL, str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(app)], check=True)
    return app


def launchctl(*args, check=True):
    return subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, check=check)


def live_state():
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:8799/api/state', timeout=3) as response:
            return json.load(response)
    except Exception:
        return None


def legacy_pid():
    """Only stop a listener whose command is this exact project's server."""
    result = subprocess.run(['/usr/sbin/lsof', '-t', '-iTCP:8799', '-sTCP:LISTEN'], capture_output=True, text=True)
    pids = set(result.stdout.split())
    if not pids:
        return None
    if len(pids) != 1:
        raise RuntimeError('Port 8799 has multiple listeners; close them before installing.')
    pid = int(pids.pop())
    command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'command='], text=True).strip()
    if str(ROOT / 'server.py') not in command or ' --data ' in command:
        raise RuntimeError('Port 8799 belongs to another server. Installation stopped before changing it.')
    return pid


def copy_database(source, target):
    if target.exists():
        return  # Updating the installed app never overwrites its newer history.
    if not source.is_file():
        return
    temporary = target.with_suffix('.tmp')
    try:
        with closing(sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)) as live:
            with closing(sqlite3.connect(temporary)) as dest:
                live.backup(dest)
                if dest.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Database migration integrity check failed.')
        temporary.chmod(0o600)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def install():
    if sys.platform != 'darwin':
        raise RuntimeError('The background installer requires macOS.')
    os.umask(0o077)
    SUPPORT.mkdir(parents=True, exist_ok=True)
    data_dir = SUPPORT / 'data'
    (data_dir / 'logs').mkdir(parents=True, exist_ok=True)
    stage = SUPPORT / 'runtime.staging'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    for name in FILES:
        (stage / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, stage / name)
    shutil.copytree(ROOT / 'web', stage / 'web')
    python = SUPPORT / '.venv' / 'bin' / 'python'
    if not python.exists():
        base = Path('/opt/homebrew/opt/python@3.14/bin/python3.14')
        subprocess.run([str(base if base.exists() else sys._base_executable), '-m', 'venv', str(SUPPORT / '.venv')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(stage / 'requirements.txt')], check=True)
    subprocess.run([str(python), '-m', 'py_compile', *[str(stage / f) for f in FILES if f.endswith('.py')]], check=True)
    subprocess.run([str(python), '-c', 'import server, pychromecast'], cwd=stage, check=True)
    staged_app = build_app(stage)

    running_agent = launchctl('print', f'{DOMAIN}/{LABEL}', check=False).returncode == 0
    old_pid = None if running_agent else legacy_pid()
    if running_agent:
        launchctl('bootout', f'{DOMAIN}/{LABEL}')
    if old_pid:
        os.kill(old_pid, signal.SIGTERM)
        for _ in range(50):
            try:
                os.kill(old_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.1)
        else:
            raise RuntimeError('The existing collector did not stop; its data was left in place.')
    data = data_dir / 'hearth.sqlite3'
    runtime = SUPPORT / 'runtime'
    previous = SUPPORT / 'runtime.previous'
    app, previous_app = SUPPORT / 'Hearth.app', SUPPORT / 'Hearth.previous.app'
    old_plist = PLIST.read_bytes() if PLIST.exists() else None
    try:
        copy_database(ROOT / '.data' / 'hearth.sqlite3', data)
        if previous.exists():
            shutil.rmtree(previous)
        if runtime.exists():
            runtime.rename(previous)
        stage.rename(runtime)
        if previous_app.exists():
            shutil.rmtree(previous_app)
        if app.exists():
            app.rename(previous_app)
        staged_app.rename(app)
        subprocess.run(['/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister', '-f', str(app)], check=True)
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        temporary_plist = PLIST.with_suffix('.tmp')
        temporary_plist.write_bytes(plistlib.dumps(agent_config(python, runtime, data, app / 'Contents/MacOS/Hearth')))
        temporary_plist.replace(PLIST)
        launchctl('enable', f'{DOMAIN}/{LABEL}')
        launchctl('bootstrap', DOMAIN, str(PLIST))
        for _ in range(80):
            state = live_state()
            if state and state['collector'].get('background') and state['collector'].get('database_path') == str(data):
                print('Background collection is running at http://localhost:8799')
                print(f'History and backups: {data_dir}')
                return
            time.sleep(.25)
        raise RuntimeError('The installed collector did not become reachable. Check the local logs.')
    except Exception:
        launchctl('bootout', f'{DOMAIN}/{LABEL}', check=False)
        if previous_app.exists():
            if app.exists():
                shutil.rmtree(app)
            previous_app.rename(app)
        if previous.exists():
            if runtime.exists():
                shutil.rmtree(runtime)
            previous.rename(runtime)
        if old_plist:
            PLIST.write_bytes(old_plist)
            launchctl('bootstrap', DOMAIN, str(PLIST), check=False)
        else:
            PLIST.unlink(missing_ok=True)
        if old_pid:
            with (ROOT / '.data' / 'server.log').open('ab') as log:
                process = subprocess.Popen([sys.executable, '-u', str(ROOT / 'server.py'), '--host', '0.0.0.0'],
                                           cwd=ROOT, stdout=log, stderr=log, start_new_session=True)
            (ROOT / '.data' / 'server.pid').write_text(str(process.pid))
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'status', 'uninstall'])
    args = parser.parse_args()
    if args.action == 'install':
        install()
    elif args.action == 'status':
        result = launchctl('print', f'{DOMAIN}/{LABEL}', check=False)
        print(result.stdout if result.returncode == 0 else 'Background collector is not loaded.')
        state = live_state()
        if state:
            print(json.dumps(state['collector'], indent=2))
    else:
        launchctl('bootout', f'{DOMAIN}/{LABEL}', check=False)
        PLIST.unlink(missing_ok=True)
        print(f'Background collection removed. History, backups, and runtime preserved in {SUPPORT}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'Background setup failed: {exc}', file=sys.stderr)
        sys.exit(1)
