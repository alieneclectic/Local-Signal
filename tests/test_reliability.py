from datetime import datetime, timedelta, timezone
from contextlib import closing
import gzip
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import background
import dev
import reliability
import server


@unittest.skipUnless(os.name == 'posix', 'requires POSIX file permissions')
class DatabasePermissionsTests(unittest.TestCase):
    def test_new_database_and_sidecars_are_private_with_a_standard_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'collector' / 'test.sqlite3'
            previous_umask = os.umask(0o022)
            try:
                with reliability.DatabaseLease(path), closing(server.Store(path).db):
                    self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
                    for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
            finally:
                os.umask(previous_umask)

    def test_opening_an_existing_database_restricts_files_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / 'shared'
            directory.mkdir()
            directory.chmod(0o755)
            path = directory / 'test.sqlite3'
            store = server.Store(path)
            with closing(store.db):
                store.save_setting('test-setting', 'previous value')
                files = (path, Path(str(path) + '-wal'), Path(str(path) + '-shm'))
                for file in files:
                    file.chmod(0o644)
                reopened = server.Store(path)
                with closing(reopened.db):
                    self.assertEqual(reopened.setting('test-setting'), 'previous value')
                    self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
                    for file in files:
                        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)


class BackupTests(unittest.TestCase):
    def test_consistent_wal_backup_restores_and_rotates_seven_daily_copies(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'live.sqlite3'
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE history (value)')
                db.execute('INSERT INTO history VALUES (42)')
                db.commit()
                for day in range(9):
                    result = reliability.backup_database(source, Path(folder) / 'backups', datetime(2026, 10, 1 + day, tzinfo=timezone.utc))
                copies = sorted((Path(folder) / 'backups').glob('*.sqlite3.gz'))
                self.assertEqual(len(copies), 7)
                self.assertEqual(copies[0].name, 'hearth-2026-10-03.sqlite3.gz')
                restored_path = Path(folder) / 'restored.sqlite3'
                restored_path.write_bytes(gzip.decompress(copies[-1].read_bytes()))
                with closing(sqlite3.connect(restored_path)) as restored:
                    self.assertEqual(restored.execute('SELECT value FROM history').fetchone()[0], 42)
                    self.assertEqual(restored.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_update_does_not_overwrite_installed_database(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.db', Path(folder) / 'installed.db'
            with closing(sqlite3.connect(source)) as db, db:
                db.execute('CREATE TABLE inventory (name)'); db.execute("INSERT INTO inventory VALUES ('original')")
            background.copy_database(source, target)
            with closing(sqlite3.connect(target)) as db, db:
                db.execute("UPDATE inventory SET name='newer installed history'")
            background.copy_database(source, target)
            with closing(sqlite3.connect(target)) as db:
                self.assertEqual(db.execute('SELECT name FROM inventory').fetchone()[0], 'newer installed history')

    def test_agent_uses_internal_runtime_and_fixed_environment(self):
        root = Path('/Users/example/Library/Application Support/Hearth')
        config = background.agent_config(root / '.venv/bin/python', root / 'runtime', root / 'data/hearth.sqlite3')
        self.assertTrue(config['KeepAlive'])
        self.assertTrue(config['RunAtLoad'])
        self.assertIn('--background', config['ProgramArguments'])
        self.assertEqual(config['ProgramArguments'][config['ProgramArguments'].index('--host') + 1], '127.0.0.1')
        self.assertEqual(config['EnvironmentVariables']['LOCAL_SIGNAL_HOST'], '127.0.0.1')
        self.assertNotIn('/Volumes/', str(config))
        self.assertIn('/sbin', config['EnvironmentVariables']['PATH'])
        signed = background.agent_config(root / '.venv/bin/python', root / 'runtime', root / 'data/hearth.sqlite3', root / 'Hearth.app/Contents/MacOS/Hearth')
        self.assertEqual(signed['AssociatedBundleIdentifiers'], [background.LABEL])
        self.assertEqual(signed['ProgramArguments'], [str(root / 'Hearth.app/Contents/MacOS/Hearth')])
        self.assertEqual(signed['EnvironmentVariables']['LOCAL_SIGNAL_HOST'], '127.0.0.1')

    def test_checkout_is_optional_and_preserves_the_installed_working_directory(self):
        root = Path('/Users/example/Library/Application Support/Hearth')
        checkout = Path('/Volumes/Projects/Local Signal')
        config = background.agent_config(root / '.venv/bin/python', root / 'runtime', root / 'data/hearth.sqlite3',
                                         root / 'Hearth.app/Contents/MacOS/Hearth', checkout=checkout)
        self.assertEqual(config['WorkingDirectory'], str(root / 'runtime'))
        self.assertEqual(config['EnvironmentVariables']['LOCAL_SIGNAL_CHECKOUT'], str(checkout))
        self.assertIn('dev.py', background.FILES)

    def test_unavailable_checkout_uses_the_installed_backend(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            with patch.object(dev, 'ROOT', checkout / 'installed'), patch.object(dev, 'backend_snapshot') as snapshot:
                source, _ = dev.checkout_snapshot(checkout)
                self.assertEqual(source, dev.ROOT)
                snapshot.assert_called_once_with(dev.ROOT)
                (checkout / 'server.py').touch()
                (checkout / 'web').mkdir()
                (checkout / 'web/index.html').touch()
                source, _ = dev.checkout_snapshot(checkout)
                self.assertEqual(source, checkout)


class DashboardSharingTests(unittest.TestCase):
    def test_only_an_explicit_sharing_choice_survives_reinstall(self):
        root = Path('/Users/example/Library/Application Support/Hearth')
        legacy = {'ProgramArguments': ['python3', 'server.py', '--host', '0.0.0.0']}
        self.assertEqual(background.collector_host(previous=legacy), '127.0.0.1')
        shared = background.agent_config(root / '.venv/bin/python', root / 'runtime', root / 'data/hearth.sqlite3', host='0.0.0.0')
        self.assertEqual(shared['ProgramArguments'][shared['ProgramArguments'].index('--host') + 1], '0.0.0.0')
        self.assertEqual(background.collector_host(previous=shared), '0.0.0.0')
        self.assertEqual(background.collector_host('127.0.0.1', shared), '127.0.0.1')
        with self.assertRaises(ValueError):
            background.collector_host(previous={'EnvironmentVariables': {'LOCAL_SIGNAL_HOST': 'example.com'}})

    def test_installer_forwards_explicit_access_choice_without_installing(self):
        for host in ('127.0.0.1', '0.0.0.0'):
            with self.subTest(host=host), patch.object(background.sys, 'platform', 'darwin'), \
                    patch.object(background.sys, 'argv', ['background.py', 'install', '--host', host, '--follow-checkout']), \
                    patch.object(background, 'install') as install:
                background.main()
                install.assert_called_once_with(True, host=host)

    def test_background_checkout_honors_saved_host_from_older_launcher(self):
        checkout = Path('/example/checkout')
        argv = ['dev.py', '--background', '--checkout', str(checkout), '--host', '0.0.0.0']
        with patch.object(dev.sys, 'argv', argv), \
                patch.dict(os.environ, {'LOCAL_SIGNAL_HOST': '127.0.0.1'}), \
                patch.object(dev, 'checkout_snapshot', return_value=(checkout, {})), \
                patch.object(dev, 'checkout_loads', return_value=True), \
                patch.object(dev, 'start_server') as start, \
                patch.object(dev, 'stop_server'), \
                patch.object(dev.signal, 'signal'), \
                patch.object(dev.time, 'sleep', side_effect=KeyboardInterrupt):
            dev.main()
        self.assertEqual(start.call_args.args[1].host, '127.0.0.1')

    @unittest.skipUnless(shutil.which('zsh'), 'macOS launcher requires zsh')
    def test_start_script_forwards_server_flags_without_reusing_an_installed_collector(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            launcher = root / 'start.command'
            launcher.write_bytes((background.ROOT / 'start.command').read_bytes())
            python = root / '.venv/bin/python'
            python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@"\n')
            python.chmod(0o700)
            result = subprocess.run([shutil.which('zsh'), str(launcher), '--host', '0.0.0.0', '--data', '/tmp/path with spaces/test.sqlite3'],
                                    capture_output=True, text=True, timeout=5, check=True)
            self.assertEqual(result.stdout.splitlines(), ['server.py', '--host', '0.0.0.0', '--data', '/tmp/path with spaces/test.sqlite3'])


class LaunchdTests(unittest.TestCase):
    @staticmethod
    def result(code):
        return subprocess.CompletedProcess(['/bin/launchctl'], code, '', '')

    def test_bootstrap_retries_while_launchd_is_still_removing_the_old_agent(self):
        calls = [self.result(5), self.result(5), self.result(0)]
        with patch.object(background, 'launchctl', side_effect=calls) as run, patch.object(background.time, 'sleep'):
            background.bootstrap(Path('/tmp/agent.plist'))
        self.assertEqual(run.call_count, 3)

    def test_bootstrap_gives_up_with_an_error_instead_of_silently(self):
        with patch.object(background, 'launchctl', return_value=self.result(5)), patch.object(background.time, 'sleep'):
            with self.assertRaises(subprocess.CalledProcessError):
                background.bootstrap(Path('/tmp/agent.plist'), attempts=3)

    def test_install_waits_until_the_old_agent_is_gone(self):
        calls = [self.result(0), self.result(0), self.result(113)]
        with patch.object(background, 'launchctl', side_effect=calls) as run, patch.object(background.time, 'sleep'):
            background.wait_for_unload()
        self.assertEqual(run.call_count, 3)

class CollectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name) / 'test.sqlite3')
        with patch.object(server, 'network_info', return_value={'local_ip': None}):
            self.collector = server.Collector(self.store)

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def test_failed_cycle_reports_delayed_and_next_cycle_recovers(self):
        with patch.object(server, 'network_info', return_value={}), patch.object(self.store, 'devices', side_effect=sqlite3.OperationalError('temporary disk error')):
            self.collector.poll()
        self.assertEqual(self.collector.snapshot()['health'], 'delayed')
        self.assertFalse(self.collector.state['polling'])
        with patch.object(server, 'network_info', return_value={}), patch.object(self.store, 'devices', return_value=[]):
            self.collector.poll()
        self.assertEqual(self.collector.snapshot()['health'], 'healthy')
        self.assertIsNone(self.collector.state['collection_error'])

    def test_restart_does_not_claim_health_from_previous_process_poll(self):
        self.store.save_setting('last_completed_poll', server.now())
        with patch.object(server, 'network_info', return_value={}):
            restarted = server.Collector(self.store)
        self.assertEqual(restarted.snapshot()['health'], 'starting')
        with patch.object(server, 'network_info', return_value={}), patch.object(self.store, 'devices', return_value=[]):
            restarted.poll()
        self.assertEqual(restarted.snapshot()['health'], 'healthy')

    def test_downtime_gap_survives_restart_and_backup_failure_is_visible(self):
        previous = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        self.store.save_setting('last_completed_poll', previous)
        with patch.object(server, 'network_info', return_value={}):
            collector = server.Collector(self.store)
        with patch.object(server, 'network_info', return_value={}), patch.object(self.store, 'devices', return_value=[]):
            collector.poll()
        self.assertEqual(collector.state['last_gap']['start'], previous)
        self.assertEqual(self.store.setting('last_collection_gap')['start'], previous)
        with patch.object(reliability, 'backup_database', side_effect=OSError('disk full')):
            collector.backup_if_due()
        self.assertIn('disk full', collector.snapshot()['backup_error'])
