from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import routers
from routers import monitor as monitoring
import server


class RouterMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'test.sqlite3'
        self.read = Mock()
        self.adapter = SimpleNamespace(NAME='Fixture router', CAPABILITIES=('upload_bps', 'download_bps', 'paused'),
                                       load_config=Mock(return_value=None), reader=Mock(return_value=self.read))
        self.monitor = routers.Monitor(self.database, 'fixture', self.adapter)

    def test_opt_in_backoff_staleness_and_disconnect(self):
        self.monitor.update()
        self.adapter.reader.assert_not_called()
        self.assertEqual(self.monitor.snapshot()['status'], 'disabled')
        self.adapter.load_config.return_value = {'router': '192.168.1.1'}
        self.read.side_effect = [routers.RouterError('Throttled', retry_after=300), {'devices': []}]
        with patch.object(monitoring.time, 'monotonic', return_value=100):
            self.monitor.update()
        self.assertEqual(self.monitor.snapshot()['message'], 'Throttled')
        with patch.object(monitoring.time, 'monotonic', return_value=399):
            self.monitor.update()
            self.read.assert_called_once_with()
        with patch.object(monitoring.time, 'monotonic', return_value=400):
            self.monitor.update()
            self.assertEqual(self.monitor.snapshot()['status'], 'ready')
        with patch.object(monitoring.time, 'monotonic', return_value=521):
            self.assertEqual(self.monitor.snapshot()['status'], 'unavailable')
            self.assertNotIn('devices', self.monitor.snapshot())
        self.adapter.load_config.return_value = None
        self.monitor.update()
        self.assertEqual(self.monitor.snapshot()['status'], 'disabled')
        self.assertIsNone(self.monitor.read)

    def test_partial_capabilities_and_public_data(self):
        self.adapter.load_config.return_value = {'secret': 'fixture-token'}
        self.read.return_value = {
            'capabilities': ('upload_bps', 'download_bps'),
            'devices': [{'name': 'Fixture laptop', 'ip': '192.168.1.20', 'upload_bps': 0, 'download_bps': float('nan'),
                         'paused': False, 'raw': 'private-device-data'}],
            'raw': 'private-router-data',
        }
        self.monitor.update()
        state = self.monitor.snapshot()
        self.assertEqual(state['capabilities'], ('upload_bps', 'download_bps'))
        self.assertEqual(state['devices'], [{'name': 'Fixture laptop', 'ip': '192.168.1.20',
                                            'upload_bps': 0, 'download_bps': None}])
        self.assertNotIn('private-', str(state))
        self.assertNotIn('fixture-token', str(state))

    def test_setup_failure_unsupported_and_unexpected_errors_are_distinct(self):
        self.adapter.load_config.side_effect = routers.RouterError('Run setup again.', status='setup_required')
        self.monitor.update()
        self.assertEqual(self.monitor.snapshot()['status'], 'setup_required')
        self.adapter.load_config.side_effect = None
        self.adapter.load_config.return_value = {'model': 'fixture'}
        self.read.side_effect = routers.RouterError('This model cannot report traffic.', status='unsupported')
        self.monitor.update()
        self.assertEqual(self.monitor.snapshot()['status'], 'unsupported')
        self.adapter.load_config.return_value = {'model': 'another fixture'}
        self.read.side_effect = RuntimeError('fixture-token')
        self.monitor.update()
        state = self.monitor.snapshot()
        self.assertEqual(state['status'], 'unavailable')
        self.assertNotIn('fixture-token', str(state))

    def test_config_changed_during_poll_drops_reading(self):
        self.adapter.load_config.side_effect = [{'router': 'fixture'}, None, None]
        self.read.return_value = {'devices': [{'name': 'Fixture phone', 'ip': None}]}
        self.monitor.update()
        self.assertNotIn('devices', self.monitor.snapshot())
        self.monitor.update()
        self.assertEqual(self.monitor.snapshot()['status'], 'disabled')


class RouterSetupTests(unittest.TestCase):
    def test_shared_cli_dispatches_and_keeps_google_wifi_alias(self):
        adapter = routers.ADAPTERS['google-wifi']
        with patch.object(adapter, 'configure') as configure:
            server.main(['--data', 'fixture.sqlite3', '--router', 'google-wifi', 'connect'])
            configure.assert_called_once_with(Path('fixture.sqlite3'), 'connect')
            configure.reset_mock()
            server.main(['--data', 'fixture.sqlite3', '--google-wifi', 'disconnect'])
            configure.assert_called_once_with(Path('fixture.sqlite3'), 'disconnect')

    def test_unknown_cli_adapter_lists_available_adapters(self):
        stderr = StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as exit_result:
            server.main(['--router', 'unsupported-fixture', 'connect'])
        self.assertEqual(exit_result.exception.code, 2)
        self.assertIn('Unsupported router adapter. Available adapters: google-wifi.', stderr.getvalue())
