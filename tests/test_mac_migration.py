from contextlib import ExitStack, closing, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import plistlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import background
import reliability
import server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packaging'))
from collector import migration, macos
entry = importlib.import_module('collector.__main__')
sys.path.pop(0)


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'getuid'), 'Mac migration requires POSIX ownership and file permissions')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / 'home'
        self.source = migration.legacy_database(self.home)
        self.data = self.home / 'Library/Application Support/Local Signal/data/hearth.sqlite3'
        self.label = 'local.signal.collector'
        self.launcher = self.home / 'Applications/Local Signal.app/Contents/MacOS/Local Signal'
        self.legacy_plist = self.home / 'Library/LaunchAgents/local.hearth.collector.plist'
        self.legacy_plist.parent.mkdir(parents=True)
        self.source.parent.mkdir(parents=True)
        self.store = server.Store(self.source)
        self.store.add_device('10.23.0.10', name='Fixture heater', pinned=True)
        self.store.save_setting('fixture-setting', {'value': 42})
        self.store.db.execute("INSERT INTO samples(device_id,ip,at,metric,value,unit,label) VALUES(1,'10.23.0.10','2026-11-15T12:00:00+00:00','power_w',50,'W','Power')")
        self.store.db.execute("INSERT INTO alert_deliveries(event_id,alert_key,payload,destination_key,created_at,next_attempt_at) VALUES('event','alert','{}','destination','2026-11-15','2026-11-15')")
        self.store.db.commit()
        self.store.db.close()
        for suffix in migration.COMPANIONS:
            path = Path(str(self.source) + suffix)
            path.write_text(json.dumps({'fixture': 'private-test-value'}))
            path.chmod(0o600)
        backup = self.source.parent / 'backups/hearth-2026-11-14.sqlite3.gz'
        backup.parent.mkdir()
        backup.write_bytes(b'fixture backup bytes')
        support = self.source.parent.parent
        self.old_launcher = support / 'Hearth.app/Contents/MacOS/Hearth'
        self.legacy_raw = plistlib.dumps(background.agent_config(None, support / 'runtime', self.source, self.old_launcher))
        self.legacy_plist.write_bytes(self.legacy_raw)
        self.running = {background.LABEL: (self.legacy_plist, self.old_launcher)}
        self.calls = []
        self.context = ExitStack()
        self.addCleanup(self.context.close)
        self.context.enter_context(patch.object(macos, 'app_identity', return_value=(self.label, self.launcher)))
        self.context.enter_context(patch.object(background, 'DOMAIN', 'gui/501'))
        self.context.enter_context(patch.object(background, 'launchctl', side_effect=self.launchctl))
        # Tests use real leases and temporary SQLite files, without inspecting host processes.
        self.context.enter_context(patch.object(reliability, '_check_legacy_users'))
        self.context.enter_context(patch.object(migration.socket, 'socket'))
        self.handoff = self.context.enter_context(patch.object(migration, 'wait_for_handoff'))

    def launchctl(self, action, *args, check=True):
        self.calls.append((action, *args))
        label = args[0].split('/')[-1] if args else None
        if action == 'print':
            value = self.running.get(label)
            output = f'path = {value[0]}\nprogram = {value[1]}\n' if value else ''
            return subprocess.CompletedProcess([], 0 if value else 113, output, '')
        if action == 'bootout':
            self.running.pop(label, None)
        if action == 'bootstrap':
            path = Path(args[1])
            config = plistlib.loads(path.read_bytes())
            self.running[config['Label']] = (path, Path(config['ProgramArguments'][0]))
        return subprocess.CompletedProcess([], 0, '', '')

    def plan(self, **kwargs):
        return migration.plan(self.data, home=self.home, **kwargs)

    def test_plan_is_read_only_and_never_exposes_credentials(self):
        before = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        output = io.StringIO()
        with patch.object(migration.Path, 'home', return_value=self.home), redirect_stdout(output):
            migration.main([], self.data)
        after = {path.relative_to(self.home): path.read_bytes() for path in self.home.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.calls, [])
        self.assertNotIn('private-test-value', output.getvalue())
        self.assertIn('Plan only', output.getvalue())

    def test_handoff_preserves_database_credentials_backups_and_pending_notifications(self):
        details = self.plan()
        migration.apply(details)
        with closing(sqlite3.connect(self.data)) as db:
            self.assertEqual(db.execute('SELECT name FROM devices').fetchone()[0], 'Fixture heater')
            self.assertEqual(db.execute("SELECT value FROM settings WHERE key='fixture-setting'").fetchone()[0], '{"value": 42}')
            self.assertEqual(db.execute('SELECT value FROM samples').fetchone()[0], 50)
            self.assertEqual(db.execute('SELECT status FROM alert_deliveries').fetchone()[0], 'pending')
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        for suffix in migration.COMPANIONS:
            copied = Path(str(self.data) + suffix)
            self.assertEqual(copied.read_bytes(), Path(str(self.source) + suffix).read_bytes())
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.data.parent / 'backups/hearth-2026-11-14.sqlite3.gz').read_bytes(), b'fixture backup bytes')
        self.assertTrue(self.source.exists())
        self.assertFalse(self.legacy_plist.exists())
        self.assertEqual((self.data.parent / migration.RETIRED_PLIST).read_bytes(), self.legacy_raw)
        self.assertNotIn(background.LABEL, self.running)
        self.assertIn(self.label, self.running)
        self.handoff.assert_called_once_with(self.data, 8799)
        disable = self.calls.index(('disable', 'gui/501/' + background.LABEL))
        bootstrap = self.calls.index(('bootstrap', 'gui/501', str(details['plist'])))
        self.assertGreater(disable, bootstrap)

    def test_existing_target_and_unverified_agent_are_rejected_without_mutation(self):
        self.data.parent.mkdir(parents=True)
        self.data.write_bytes(b'existing data')
        with self.assertRaisesRegex(migration.MigrationError, 'never overwrites'):
            self.plan()
        self.assertEqual(self.data.read_bytes(), b'existing data')
        self.data.unlink()
        self.legacy_plist.write_bytes(plistlib.dumps({'Label': background.LABEL, 'ProgramArguments': ['/unrelated']}))
        with self.assertRaisesRegex(migration.MigrationError, 'does not match'):
            self.plan()
        self.assertEqual(self.calls, [])

    def test_unverified_loaded_agent_is_never_stopped(self):
        self.running[background.LABEL] = (self.legacy_plist, Path('/unrelated/program'))
        with self.assertRaisesRegex(migration.MigrationError, 'unexpected configuration'):
            migration.apply(self.plan())
        self.assertFalse(any(call[0] == 'bootout' for call in self.calls))
        self.assertFalse(self.data.exists())

    def test_failed_handoff_restores_original_agent_and_keeps_source_bytes(self):
        original = self.source.read_bytes()
        self.handoff.side_effect = migration.MigrationError('fixture startup failure')
        details = self.plan()
        with self.assertRaisesRegex(migration.MigrationError, 'fixture startup failure'):
            migration.apply(details)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.legacy_plist.read_bytes(), self.legacy_raw)
        self.assertIn(background.LABEL, self.running)
        self.assertNotIn(self.label, self.running)
        self.assertFalse(self.data.exists())
        self.assertFalse(details['plist'].exists())
        self.assertEqual([path.name for path in self.data.parent.iterdir()], ['hearth.sqlite3.lock'])

    def test_cannot_restart_legacy_when_new_collector_cannot_be_stopped(self):
        self.handoff.side_effect = migration.MigrationError('fixture startup failure')
        real_stop = migration.stop_agent
        def stop(label, *args, **kwargs):
            if label == self.label:
                raise migration.MigrationError('fixture stop failure')
            return real_stop(label, *args, **kwargs)
        with patch.object(migration, 'stop_agent', side_effect=stop), self.assertRaisesRegex(migration.MigrationError, 'left stopped'):
            migration.apply(self.plan())
        self.assertNotIn(background.LABEL, self.running)
        self.assertIn(self.label, self.running)
        self.assertTrue(self.data.exists())

    def test_interrupted_handoff_also_restores_the_legacy_agent(self):
        self.handoff.side_effect = KeyboardInterrupt()
        with self.assertRaises(migration.MigrationError):
            migration.apply(self.plan())
        self.assertIn(background.LABEL, self.running)
        self.assertNotIn(self.label, self.running)
        self.assertFalse(self.data.exists())

    def test_file_appearing_during_staging_is_preserved(self):
        real_stage = migration.stage_files
        def stage(details, folder):
            real_stage(details, folder)
            self.data.write_bytes(b'unrelated concurrent data')
        with patch.object(migration, 'stage_files', side_effect=stage), self.assertRaisesRegex(migration.MigrationError, 'not overwritten'):
            migration.apply(self.plan())
        self.assertEqual(self.data.read_bytes(), b'unrelated concurrent data')
        self.assertIn(background.LABEL, self.running)

    def test_active_source_lease_refuses_copy_and_restores_legacy_agent(self):
        with reliability.DatabaseLease(self.source):
            with self.assertRaisesRegex(migration.MigrationError, 'already in use'):
                migration.apply(self.plan())
        self.assertFalse(self.data.exists())
        self.assertNotIn(self.label, self.running)

    def test_symlink_companions_and_backup_files_are_rejected(self):
        companion = Path(str(self.source) + migration.COMPANIONS[0])
        companion.unlink()
        companion.symlink_to(Path(str(self.source) + migration.COMPANIONS[1]))
        with self.assertRaisesRegex(migration.MigrationError, 'symbolic links'):
            self.plan()

    def test_staged_settings_validation_failure_rolls_back_before_new_agent_starts(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE settings SET value='{invalid' WHERE key='fixture-setting'")
        with self.assertRaises(migration.MigrationError):
            migration.apply(self.plan())
        self.assertFalse(self.data.exists())
        self.assertIn(background.LABEL, self.running)
        self.assertNotIn(self.label, self.running)
        self.handoff.assert_not_called()

    def test_failure_retiring_legacy_agent_restores_its_enabled_running_state(self):
        unlink = Path.unlink
        def fail_legacy(path, *args, **kwargs):
            if path == self.legacy_plist:
                raise OSError('fixture removal error')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', fail_legacy), self.assertRaises(migration.MigrationError):
            migration.apply(self.plan())
        self.assertIn(('enable', 'gui/501/' + background.LABEL), self.calls)
        self.assertIn(background.LABEL, self.running)
        self.assertNotIn(self.label, self.running)
        self.assertEqual(self.legacy_plist.read_bytes(), self.legacy_raw)
        self.assertFalse(self.data.exists())

    def test_stop_wait_is_bounded(self):
        def cannot_stop(action, *args, check=True):
            if action == 'bootout':
                return subprocess.CompletedProcess([], 0, '', '')
            return self.launchctl(action, *args, check=check)
        with patch.object(background, 'launchctl', side_effect=cannot_stop), self.assertRaisesRegex(migration.MigrationError, 'did not stop'):
            migration.stop_agent(background.LABEL, self.legacy_plist, self.old_launcher, timeout=0)

    def test_explicit_sharing_is_preserved_or_can_be_overridden(self):
        config = plistlib.loads(self.legacy_raw)
        config['EnvironmentVariables']['LOCAL_SIGNAL_HOST'] = '0.0.0.0'
        self.legacy_plist.write_bytes(plistlib.dumps(config))
        self.assertEqual(self.plan()['host'], '0.0.0.0')
        self.assertEqual(self.plan(host='127.0.0.1', port=8800)['host'], '127.0.0.1')
        self.assertEqual(self.plan(port=8800)['port'], 8800)


class PackagedEntryTests(unittest.TestCase):
    def test_normal_launch_requires_explicit_migration_but_recovery_and_help_work(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(entry.sys, 'platform', 'darwin'), \
                patch.object(entry.Path, 'home', return_value=Path(folder)), patch.object(server, 'main') as run, \
                patch.object(macos, 'show_migration_dialog') as dialog, \
                patch.object(entry.sys.stdin, 'isatty', return_value=False):
            source = migration.legacy_database(Path(folder))
            source.parent.mkdir(parents=True)
            source.write_bytes(b'legacy')
            for args in ([], ['-psn_0_12345'], ['--enable-background'], ['--background'], ['--port', '8800']):
                with self.subTest(args=args), patch.object(entry.sys, 'argv', ['Local Signal', *args]), self.assertRaisesRegex(SystemExit, '--migrate-legacy'):
                    entry.main()
                run.assert_not_called()
            self.assertEqual(dialog.call_count, 2)
            for args in (['--help'], ['--list-backups'], ['--backup'], ['--inspect-backup', '/tmp/backup.gz'],
                         ['--restore-backup', '/tmp/backup.gz', '--apply'], ['--data', str(Path(folder) / 'separate.db')]):
                with self.subTest(args=args), patch.object(entry.sys, 'argv', ['Local Signal', *args]):
                    entry.main()
            self.assertEqual(run.call_count, 6)

    def test_finder_dialog_failure_keeps_concrete_terminal_guidance(self):
        launcher = Path('/Applications/Local Signal.app/Contents/MacOS/Local Signal')
        for failure in (subprocess.TimeoutExpired('osascript', 35), OSError('dialog unavailable')):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as folder, \
                    patch.object(entry.sys, 'platform', 'darwin'), patch.object(entry.sys, 'argv', ['Local Signal']), \
                    patch.object(entry.Path, 'home', return_value=Path(folder)), \
                    patch.object(entry.sys.stdin, 'isatty', return_value=False), \
                    patch.object(macos, 'app_identity', return_value=('local.signal.collector', launcher)), \
                    patch.object(macos.subprocess, 'run', side_effect=failure) as dialog, \
                    patch.object(server, 'main') as run:
                source = migration.legacy_database(Path(folder))
                source.parent.mkdir(parents=True)
                source.write_bytes(b'legacy')
                command = macos.migration_preview_command()
                with self.assertRaises(SystemExit) as stopped:
                    entry.main()
                self.assertIn(command, str(stopped.exception))
                self.assertIn('No empty database was created', str(stopped.exception))
                arguments = dialog.call_args.args[0]
                self.assertEqual(arguments[0:2], ['/usr/bin/osascript', '-e'])
                self.assertNotIn(str(launcher), arguments[2])  # Fixed AppleScript, never interpolated code.
                self.assertEqual(arguments[-1], command)
                self.assertEqual(dialog.call_args.kwargs['timeout'], 35)
                run.assert_not_called()

    def test_dialog_command_is_passed_as_data_and_terminal_launch_stays_text_only(self):
        command = "'/Applications/A \"quoted\" $(name).app/Contents/MacOS/Local Signal' --migrate-legacy"
        with patch.object(macos.subprocess, 'run') as dialog:
            self.assertTrue(macos.show_migration_dialog(command))
            self.assertEqual(dialog.call_args.args[0][-1], command)
            self.assertNotIn(command, dialog.call_args.args[0][2])
        with tempfile.TemporaryDirectory() as folder, patch.object(entry.sys, 'platform', 'darwin'), \
                patch.object(entry.sys, 'argv', ['Local Signal']), patch.object(entry.Path, 'home', return_value=Path(folder)), \
                patch.object(entry.sys.stdin, 'isatty', return_value=True), patch.object(macos, 'show_migration_dialog') as dialog:
            source = migration.legacy_database(Path(folder))
            source.parent.mkdir(parents=True)
            source.write_bytes(b'legacy')
            with self.assertRaises(SystemExit):
                entry.main()
            dialog.assert_not_called()
