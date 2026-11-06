from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from routers import RouterError
from routers.adapters import google_wifi as wifi


class GoogleWifiTests(unittest.TestCase):
    def test_collect_reads_one_mesh_and_preserves_unknown_and_zero(self):
        client = wifi.Client('reader@example.test', 'master-fixture', 'android-fixture')
        stations = [
            {'id': '1', 'friendlyName': 'Speaker', 'connected': True, 'apId': 'a', 'ipAddresses': ['fd00::1', '192.168.86.10']},
            {'id': '2', 'friendlyName': 'TV', 'connected': True, 'apId': 'b'},
            {'id': '3', 'friendlyName': 'Tablet', 'connected': False, 'ipAddress': '192.168.86.11'},
            {'id': '4', 'friendlyName': 'Switch', 'connected': True, 'apId': 'a'},
        ]
        group = {'id': 'fixture/mesh', 'groupSettings': {'familyHubSettings': {'stationPolicies': [
            {'stationId': '1', 'blockingPolicy': {'expiryTimestamp': '1970-01-01T00:00:00Z'}},
            {'stationId': '2', 'blockingPolicy': {'expiryTimestamp': '2001-01-01T00:00:00Z'}},
            {'stationId': '3', 'blockingPolicy': {'expiryTimestamp': 'invalid'}},
            {'stationId': '4', 'blockingPolicy': {'expiryTimestamp': '2099-01-01T00:00:00Z'}},
        ]}}, 'ignored': 'private-mesh-data'}
        metrics = {'stationMetrics': [
            {'station': stations[0], 'traffic': {'receiveSpeedBps': '2500000'}},
            {'station': {**stations[1], 'apId': 'old-point'}, 'traffic': {'receiveSpeedBps': '1000'}},
            {'station': stations[2], 'traffic': {}},
            {'station': stations[3], 'traffic': {'transmitSpeedBps': '800', 'receiveSpeedBps': 'NaN'}},
        ]}
        requests = {
            'groups': {'groups': [group, {'id': 'other-mesh'}]},
            'groups/fixture%2Fmesh/stations': {'stations': stations},
            'groups/fixture%2Fmesh/realtimeMetrics': metrics,
        }
        with patch.object(client, 'get', side_effect=requests.__getitem__) as get:
            result = client.collect('fixture/mesh')
        self.assertEqual([call.args[0] for call in get.call_args_list], list(requests))
        devices = {device['name']: device for device in result['devices']}
        self.assertEqual(devices['Speaker']['upload_bps'], 0)
        self.assertEqual(devices['Speaker']['download_bps'], 2_500_000)
        self.assertEqual(devices['Speaker']['ip'], '192.168.86.10')
        self.assertIs(devices['Speaker']['paused'], True)
        self.assertIs(devices['TV']['paused'], False)
        self.assertIsNone(devices['TV']['download_bps'])
        self.assertIsNone(devices['Tablet']['paused'])
        self.assertIsNone(devices['Tablet']['ip'])
        self.assertIs(devices['Switch']['paused'], True)
        self.assertEqual(devices['Switch']['upload_bps'], 800)
        self.assertIsNone(devices['Switch']['download_bps'])
        self.assertNotIn('private-mesh-data', json.dumps(result))

    def test_pause_policy_and_unavailable_metrics_are_independent(self):
        at = datetime(2026, 11, 6, tzinfo=timezone.utc)
        self.assertIsNone(wifi.pause_status(None, 'device', at))
        self.assertIs(wifi.pause_status([], 'device', at), False)
        client = wifi.Client('reader@example.test', 'master-fixture', 'android-fixture')
        with patch.object(client, 'get', side_effect=[
            {'groups': [{'id': 'mesh', 'groupSettings': {'familyHubSettings': {'stationPolicies': []}}}]},
            {'stations': [{'id': '1', 'connected': True}]},
            RouterError('HTTP 503'),
        ]):
            result = client.collect('mesh')
        self.assertIn('traffic readings', result['message'])
        self.assertIs(result['devices'][0]['paused'], False)
        self.assertIsNone(result['devices'][0]['upload_bps'])

    def test_setup_selects_mesh_and_disconnect_removes_private_config(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / 'preview.sqlite3'
            path = wifi.config_path(database)
            with (patch('builtins.input', side_effect=['reader@example.test', '2']),
                  patch.object(wifi.getpass, 'getpass', return_value='oauth2_4/browser-fixture'),
                  patch.object(wifi.Client, '_authenticate', return_value={'Token': 'master-fixture'}) as auth,
                  patch.object(wifi.Client, 'groups', return_value={'one': {'name': 'First'}, 'two': {'name': 'Second'}}),
                  patch('builtins.print') as output):
                wifi.configure(database, 'connect')
                auth.assert_called_once_with('oauth2_4/browser-fixture', exchange=True)
                self.assertNotIn('fixture', str(output.call_args_list))
            self.assertEqual(path.name, 'preview.sqlite3.google-wifi.json')
            self.assertEqual(wifi.load_config(database)['group_id'], 'two')
            self.assertNotIn('browser-fixture', path.read_text())
            if os.name == 'posix':
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                path.chmod(0o644)
                with self.assertRaises(RouterError) as failure:
                    wifi.load_config(database)
                self.assertEqual(failure.exception.status, 'setup_required')
            with patch('builtins.print'):
                wifi.configure(database, 'disconnect')
            self.assertIsNone(wifi.load_config(database))

    def test_reader_uses_existing_companion_config(self):
        with tempfile.TemporaryDirectory() as root:
            database = Path(root) / 'existing.sqlite3'
            wifi.save_config(wifi.config_path(database), {
                'email': 'reader@example.test', 'master_token': 'master-fixture',
                'android_id': 'android-fixture', 'group_id': 'existing-mesh',
            })
            with patch.object(wifi.Client, 'collect', return_value={'devices': []}) as collect:
                self.assertEqual(wifi.reader(wifi.load_config(database))(), {'devices': []})
                collect.assert_called_once_with('existing-mesh')
