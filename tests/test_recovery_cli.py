from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

import reliability
import server


class RecoveryCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database = Path(self.folder.name) / 'collector.sqlite3'
        store = server.Store(self.database)
        store.add_device('10.23.0.50', 'Original label', room='Office', pinned=True)
        store.record('10.23.0.50', {'status': 'online', 'reachable': True, 'checked_at': server.now(),
                                  'metrics': {'power_w': {'value': 42, 'unit': 'W', 'label': 'Power'}}})
        store.save_setting('retention_days', 9)
        store.db.close()

    def command(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), patch.object(server, 'LocalHTTPServer', side_effect=AssertionError('HTTP started')), \
                patch.object(server, 'network_info', side_effect=AssertionError('Network discovery started')):
            server.main([*args, '--data', str(self.database)])
        return json.loads(output.getvalue())

    def test_manual_backup_list_and_inspection_without_starting_collection(self):
        # A manual backup is allowed while the collector owns the database.
        with reliability.DatabaseLease(self.database):
            first = self.command('--backup')
            second = self.command('--backup')
        self.assertNotEqual(first['path'], second['path'])
        listed = self.command('--list-backups')
        self.assertEqual({item['file'] for item in listed['backups']}, {first['path'], second['path']})
        inspected = self.command('--inspect-backup', first['path'])
        self.assertTrue(inspected['valid'])
        self.assertEqual(inspected['counts']['devices'], 1)
        self.assertEqual(inspected['counts']['samples'], 1)
        self.assertNotIn('Original label', json.dumps(inspected))

    def test_restore_preview_is_read_only_and_apply_preserves_rollback(self):
        backup = self.command('--backup')['path']
        store = server.Store(self.database)
        store.patch('10.23.0.50', {'name': 'Current label'})
        store.db.close()
        preview = self.command('--restore-backup', backup)
        self.assertTrue(preview['apply_required'])
        self.assertTrue(preview['replaces_existing_database'])
        store = server.Store(self.database)
        self.assertEqual(store.devices()[0]['name'], 'Current label')
        store.db.close()
        result = self.command('--restore-backup', backup, '--apply')
        store = server.Store(self.database)
        self.assertEqual(store.devices()[0]['name'], 'Original label')
        self.assertEqual(store.retention_days(), 9)
        self.assertEqual(store.history()[0]['value'], 42)
        store.db.close()
        self.assertTrue(Path(result['rollback_backup']).is_file())
        alternate = Path(self.folder.name) / 'rollback-check.sqlite3'
        reliability.restore_database(result['rollback_backup'], alternate)
        restored = server.Store(alternate)
        self.assertEqual(restored.devices()[0]['name'], 'Current label')
        restored.db.close()

    def test_restore_cannot_replace_a_database_owned_by_a_collector(self):
        backup = self.command('--backup')['path']
        with reliability.DatabaseLease(self.database), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
            self.command('--restore-backup', backup, '--apply')
        self.assertEqual(result.exception.code, 1)
        store = server.Store(self.database)
        self.assertEqual(store.devices()[0]['name'], 'Original label')
        store.db.close()

    def test_incompatible_commands_are_rejected(self):
        for args in (['--apply'], ['--backup', '--list-backups'], ['--backup', '--router', 'google-wifi', 'connect']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                self.command(*args)
            self.assertEqual(result.exception.code, 2)

    def test_listing_missing_backup_folder_does_not_create_it(self):
        folder = self.database.parent / 'backups'
        self.assertEqual(self.command('--list-backups')['backups'], [])
        self.assertFalse(folder.exists())


class CollectorDatabaseLifetimeTests(unittest.TestCase):
    def test_idle_http_connection_cannot_hold_shutdown_indefinitely(self):
        self.assertTrue(0 < server.Handler.timeout <= 10)
        entered, closed = threading.Event(), threading.Event()
        class ObservedHandler(server.Handler):
            def setup(self):
                super().setup()
                entered.set()
            def log_message(self, *args):
                pass
        with patch.object(server.Handler, 'timeout', .05):
            httpd = server.LocalHTTPServer(('127.0.0.1', 0), ObservedHandler)
            httpd.daemon_threads = False
            serving = threading.Thread(target=httpd.serve_forever)
            serving.start()
            client = socket.create_connection(httpd.server_address, timeout=2)
            try:
                self.assertTrue(entered.wait(2))
                httpd.shutdown()
                closer = threading.Thread(target=lambda: (httpd.server_close(), closed.set()))
                closer.start()
                self.assertTrue(closed.wait(2))
                closer.join(2)
            finally:
                client.close()
                httpd.shutdown()
                httpd.server_close()
                serving.join(2)

    def test_daily_backup_validation_failure_is_reported_without_stopping_collection(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'network_info', return_value={}):
            store = server.Store(Path(folder) / 'collector.sqlite3')
            collector = server.Collector(store)
            with patch.object(reliability, 'backup_database', side_effect=reliability.RecoveryError('Snapshot timed out')):
                collector.backup_if_due()
            self.assertEqual(collector.snapshot()['backup_error'], 'Snapshot timed out')
            self.assertFalse(collector.stop.is_set())
            store.db.close()

    def test_server_refuses_a_second_collector_even_on_a_different_port(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'collector.sqlite3'
            with reliability.DatabaseLease(database), redirect_stderr(io.StringIO()), \
                    patch.object(server, 'network_info', side_effect=AssertionError('Network discovery started')), \
                    self.assertRaises(SystemExit) as result:
                server.main(['--data', str(database), '--port', '0', '--no-collector'])
            self.assertEqual(result.exception.code, 1)

    def test_server_holds_lease_until_background_writers_finish(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / 'collector.sqlite3'
            entered, release, closing = threading.Event(), threading.Event(), threading.Event()
            errors = []
            def serve(httpd):
                def write():
                    entered.set()
                    if not release.wait(5):
                        errors.append('Writer was not released')
                    httpd.store.save_setting('finished_writer', True)
                httpd.collector.start_task(write)
                entered.wait(5)
                closing.set()
                raise KeyboardInterrupt
            def run():
                try:
                    server.main(['--data', str(database), '--port', '0', '--no-collector'])
                except BaseException as exc:
                    errors.append(exc)
            with patch.object(server, 'network_info', return_value={}), \
                    patch.object(server.LocalHTTPServer, 'serve_forever', serve), redirect_stdout(io.StringIO()):
                runner = threading.Thread(target=run)
                runner.start()
                try:
                    self.assertTrue(closing.wait(5))
                    with self.assertRaises(ValueError):
                        with reliability.DatabaseLease(database):
                            pass
                    self.assertTrue(runner.is_alive())
                finally:
                    release.set()
                    runner.join(5)
            self.assertFalse(runner.is_alive())
            self.assertEqual(errors, [])
            with reliability.DatabaseLease(database):
                store = server.Store(database)
                self.assertTrue(store.setting('finished_writer'))
                store.db.close()

    def test_stopping_collector_rejects_new_background_work(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(server, 'network_info', return_value={}):
            store = server.Store(Path(folder) / 'collector.sqlite3')
            collector = server.Collector(store)
            collector.stop.set()
            self.assertFalse(collector.start_poll())
            self.assertFalse(collector.start_scan())
            self.assertFalse(collector.start_naming())
            collector.join_tasks()
            self.assertFalse(collector.scan_lock.locked())
            self.assertFalse(collector.names_lock.locked())
            store.db.close()


if __name__ == '__main__':
    unittest.main()
