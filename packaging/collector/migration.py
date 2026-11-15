"""Explicit, reversible handoff from the managed Mac collector to the packaged app."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import socket
import tempfile
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler

import background
import reliability
from . import macos

COMPANIONS = ('.google-wifi.json', '.alert-webhook.json')
RETIRED_PLIST = 'migration-legacy-agent.plist'


class MigrationError(RuntimeError):
    pass


def legacy_database(home=None):
    home = Path.home() if home is None else Path(home)
    return home / 'Library' / 'Application Support' / 'Hearth' / 'data' / 'hearth.sqlite3'


def safe_path(path):
    """Reject links before copying private configuration or publishing data."""
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise MigrationError('Migration paths must not contain symbolic links.')
    return path


def private_write(path, content):
    with path.open('xb') as stream:
        try:
            os.chmod(path, 0o600)
            stream.write(content)
        except BaseException:
            path.unlink(missing_ok=True)
            raise


def verify_legacy_config(raw, source):
    try:
        config = plistlib.loads(raw)
        support = source.parent.parent
        args = config['ProgramArguments']
        launcher = support / 'Hearth.app' / 'Contents' / 'MacOS' / 'Hearth'
        if config.get('Label') != background.LABEL or config.get('WorkingDirectory') != str(support / 'runtime'):
            raise ValueError()
        if args == [str(launcher)]:
            return config, launcher
        # Older installations could run Python directly instead of the signed launcher.
        prefix = [str(support / '.venv' / 'bin' / 'python'), '-u', str(support / 'runtime' / 'server.py')]
        if args[:3] == prefix and args[3:] in [
                ['--host', host, '--data', str(source), '--background'] for host in ('127.0.0.1', '0.0.0.0')]:
            return config, Path(args[0])
    except (ValueError, TypeError, KeyError, AttributeError, plistlib.InvalidFileException):
        pass
    raise MigrationError('The legacy login agent does not match this installation; no agent was changed.')


def plan(data, *, home=None, host=None, port=8799):
    home = Path.home() if home is None else Path(home)
    data, source = safe_path(data), safe_path(legacy_database(home))
    if not source.is_file():
        raise MigrationError('No legacy database was found to migrate.')
    if data == source:
        raise MigrationError('Source and destination must be different databases.')
    if not 1 <= port <= 65535:
        raise MigrationError('Choose a port between 1 and 65535.')
    legacy_plist = safe_path(home / 'Library' / 'LaunchAgents' / f'{background.LABEL}.plist')
    raw = legacy_plist.read_bytes() if legacy_plist.exists() else None
    config, program = verify_legacy_config(raw, source) if raw is not None else ({}, None)
    host = background.collector_host(host, config)
    label, launcher, plist, login = macos.login_config(data, host, port, home=home)
    plist = safe_path(plist)
    if label == background.LABEL or plist == legacy_plist:
        raise MigrationError('The packaged app must have its own login agent identity.')
    if plist.exists():
        raise MigrationError('The packaged login agent already exists. Disable it before migrating.')
    if data.parent.exists():
        if not data.parent.is_dir():
            raise MigrationError('The destination data directory is not a directory.')
        entries = list(data.parent.iterdir())
        if any(not ((entry.name == data.name + '.lock' and entry.is_file()) or
                    (entry.name == 'logs' and entry.is_dir())) or entry.is_symlink() for entry in entries):
            raise MigrationError('The destination already contains data. Migration never overwrites it; restore or move it deliberately first.')
    files = []
    for suffix in COMPANIONS:
        candidate = safe_path(Path(str(source) + suffix))
        if candidate.exists():
            if not candidate.is_file() or candidate.stat().st_uid != os.getuid():
                raise MigrationError('A legacy credential companion is not a regular file owned by this user.')
            files.append(candidate)
    backups = safe_path(source.parent / 'backups')
    if backups.exists():
        if not backups.is_dir():
            raise MigrationError('The legacy backups path is not a directory.')
        for entry in backups.rglob('*'):
            safe_path(entry)
            if not entry.is_dir() and not entry.is_file():
                raise MigrationError('Legacy backups contain an unsupported file type.')
    return {'source': source, 'data': data, 'host': host, 'port': port, 'companions': files,
            'backups': backups if backups.exists() else None, 'legacy_plist': legacy_plist,
            'legacy_raw': raw, 'legacy_program': program, 'label': label, 'launcher': launcher,
            'plist': plist, 'login': login}


def agent_state(label, plist, program):
    result = background.launchctl('print', f'{background.DOMAIN}/{label}', check=False)
    if result.returncode:
        return False
    fields = {key: value.strip().strip('"') for key, value in
              re.findall(r'^\s*(path|program) = (.+)$', result.stdout, re.MULTILINE)}
    if program is None or fields.get('path') != str(plist) or fields.get('program') != str(program):
        raise MigrationError('A running login agent has an unexpected configuration; it was not stopped.')
    return True


def stop_agent(label, plist, program, timeout=20):
    if not agent_state(label, plist, program):
        return
    background.launchctl('bootout', f'{background.DOMAIN}/{label}')
    deadline = time.monotonic() + timeout
    while agent_state(label, plist, program):
        if time.monotonic() >= deadline:
            raise MigrationError('The login agent did not stop within 20 seconds.')
        time.sleep(.1)


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def wait_for_handoff(data, port, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            opener = build_opener(ProxyHandler({}), NoRedirects())
            with opener.open(f'http://127.0.0.1:{port}/api/state', timeout=1) as response:
                state = json.loads(response.read(1_048_577))
            collector = state['collector']
            if collector.get('background') and Path(collector['database_path']).resolve() == data.resolve():
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
        time.sleep(.1)
    raise MigrationError('The packaged collector did not confirm the migrated database within 20 seconds.')


def snapshot(source, destination):
    reliability._sqlite_snapshot(source.resolve(), destination)
    reliability.inspect_backup(destination)  # Shared schema and JSON validation before publication.
    destination.chmod(0o600)


def stage_files(details, stage):
    snapshot(details['source'], stage / details['data'].name)
    for source in details['companions']:
        suffix = str(source)[len(str(details['source'])):]
        private_write(stage / (details['data'].name + suffix), source.read_bytes())
    if details['backups'] is not None:
        shutil.copytree(details['backups'], stage / 'backups')
        for path in (stage / 'backups').rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        (stage / 'backups').chmod(0o700)
    if details['legacy_raw'] is not None:
        private_write(stage / RETIRED_PLIST, details['legacy_raw'])


def apply(details):
    """Copy a fresh snapshot, hand off login startup, and undo our changes on failure."""
    source, data = details['source'], details['data']
    # Revalidate the plan immediately before any mutation.
    current = plan(data, home=details['legacy_plist'].parents[2], host=details['host'], port=details['port'])
    if current != details:
        raise MigrationError('The migration plan changed. Inspect it again before applying.')
    old_running = agent_state(background.LABEL, details['legacy_plist'], details['legacy_program'])
    if agent_state(details['label'], details['plist'], details['launcher']):
        raise MigrationError('The packaged collector is already running. Stop it before migrating.')
    source_guard, destination_guard = ExitStack(), ExitStack()
    destination_created = new_plist_written = new_started = legacy_disabled = legacy_removed = False
    published = []
    try:
        if old_running:
            stop_agent(background.LABEL, details['legacy_plist'], details['legacy_program'])
        source_guard.enter_context(reliability.DatabaseLease(source, check_legacy=True))
        data.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination_created = True
        destination_guard.enter_context(reliability.DatabaseLease(data, check_legacy=True))
        with tempfile.TemporaryDirectory(prefix='.migration-', dir=data.parent.parent) as folder:
            stage = Path(folder)
            stage_files(details, stage)
            for artifact in stage.iterdir():
                target = data.parent / artifact.name
                if target.exists():
                    raise MigrationError('A destination file appeared during migration; it was not overwritten.')
                if artifact.is_dir():
                    target.mkdir(mode=0o700)  # Exclusive reservation; never replace another directory.
                    published.append(target)
                    shutil.copytree(artifact, target, dirs_exist_ok=True)
                else:
                    os.link(artifact, target)  # Atomic, no-overwrite publication on the same volume.
                    published.append(target)
                    artifact.unlink()
        with socket.socket() as listener:
            listener.bind((details['host'], details['port']))
        details['plist'].parent.mkdir(parents=True, exist_ok=True)
        private_write(details['plist'], details['login'])
        new_plist_written = True
        destination_guard.close()  # The new collector acquires the same lease at startup.
        background.launchctl('enable', f'{background.DOMAIN}/{details["label"]}')
        new_started = True
        background.launchctl('bootstrap', background.DOMAIN, str(details['plist']))
        wait_for_handoff(data, details['port'])
        if not agent_state(details['label'], details['plist'], details['launcher']):
            raise MigrationError('The packaged login agent exited during handoff.')
        if details['legacy_raw'] is not None:
            if details['legacy_plist'].read_bytes() != details['legacy_raw']:
                raise MigrationError('The legacy login configuration changed during migration.')
            background.launchctl('disable', f'{background.DOMAIN}/{background.LABEL}')
            legacy_disabled = True
            details['legacy_plist'].unlink()
            legacy_removed = True
        return details
    except BaseException as error:
        rollback_errors = []
        stopped = not new_started
        if new_started:
            try:
                stop_agent(details['label'], details['plist'], details['launcher'])
                stopped = True
            except Exception:
                rollback_errors.append('The packaged collector could not be stopped; its copied data was retained.')
        if stopped:
            try:
                if new_plist_written:
                    details['plist'].unlink(missing_ok=True)
                if destination_created:
                    destination_guard.close()
                    with reliability.DatabaseLease(data, check_legacy=True):
                        # Remove only our published artifacts. An unrelated file that
                        # appeared during staging must survive the failed migration.
                        cleanup = published + ([Path(str(data) + suffix) for suffix in ('-wal', '-shm', '-journal')]
                                               if data in published else [])
                        for path in cleanup:
                            if path.is_dir() and not path.is_symlink():
                                shutil.rmtree(path)
                            else:
                                path.unlink(missing_ok=True)
            except Exception:
                rollback_errors.append('The copied destination could not be fully removed; inspect it before retrying.')
        source_guard.close()
        try:
            if legacy_removed:
                private_write(details['legacy_plist'], details['legacy_raw'])
            if legacy_disabled:
                background.launchctl('enable', f'{background.DOMAIN}/{background.LABEL}')
            if old_running and stopped and not agent_state(background.LABEL, details['legacy_plist'], details['legacy_program']):
                if details['legacy_plist'].read_bytes() != details['legacy_raw']:
                    raise MigrationError('The legacy configuration changed and was not restarted.')
                with reliability.DatabaseLease(source, check_legacy=True):
                    pass  # A child from the previous agent must also have released SQLite.
                background.launchctl('bootstrap', background.DOMAIN, str(details['legacy_plist']))
            elif old_running and not stopped:
                rollback_errors.append('The legacy collector was left stopped to avoid running both collectors.')
        except Exception:
            rollback_errors.append('The legacy login agent could not be restarted; its original database is retained.')
        message = str(error) if isinstance(error, (MigrationError, reliability.RecoveryError)) else 'Migration failed before completing the handoff.'
        if rollback_errors:
            message += ' ' + ' '.join(rollback_errors)
        raise MigrationError(message) from None
    finally:
        destination_guard.close()
        source_guard.close()


def main(argv, data):
    parser = argparse.ArgumentParser(prog='Local Signal --migrate-legacy')
    parser.add_argument('--apply', action='store_true', help='Apply the inspected migration and hand off login startup.')
    parser.add_argument('--host', choices=['127.0.0.1', '0.0.0.0'])
    parser.add_argument('--port', type=int, default=8799)
    args = parser.parse_args(argv)
    try:
        details = plan(data, host=args.host, port=args.port)
        print(f'Legacy database: {details["source"]}')
        print(f'Packaged database: {details["data"]}')
        print('Database integrity, schema, and saved settings are validated in a staged snapshot before handoff.')
        print(f'Preserve: database, {len(details["companions"])} private credential companion(s), and '
              f'{"existing backups" if details["backups"] else "no existing backup folder"}.')
        print(f'Dashboard access: {details["host"]}:{details["port"]}. Source data is retained.')
        if not args.apply:
            print('Plan only. No data or login agents changed. Run again with --apply to migrate and verify the new login agent.')
            return
        apply(details)
        print('Migration completed and packaged login startup verified. Legacy data is retained.')
        if details['legacy_raw'] is not None:
            print(f'Previous login configuration: {details["data"].parent / RETIRED_PLIST}')
    except (MigrationError, OSError, ValueError) as error:
        parser.exit(1, f'{error}\n')
