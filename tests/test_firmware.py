from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from firmware import checks
from firmware.adapters import nest, roku, shelly, sonos, wiim
import server


FIXTURES = Path(__file__).parent / 'fixtures' / 'shelly'
FIRST_IP = '192.168.1.10'
SECOND_IP = '192.168.1.11'


def fixture(name):
    return json.loads((FIXTURES / f'{name}.json').read_text(encoding='utf-8'))


class ShellyFirmwareTests(unittest.TestCase):
    def test_catalog_only_confirms_an_exact_build_or_a_newer_stable_release(self):
        stable = '20230913-112531/v1.14.0-gcb84623'
        for installed, expected in (
            (stable, 'current'),
            ('20230502-112531/v1.13.0-gabcdef', 'newer_published'),
            ('20231107-162243/v1.14.1-rc1-g0617c15', 'unverified'),
            ('20230913-112531/v1.14.0-gabcdef', 'unverified'),
            (None, 'unverified'),
        ):
            with self.subTest(installed=installed):
                self.assertEqual(shelly.compare_stable(installed, stable), expected)

    def test_gen1_unknown_status_is_not_a_negative_update_check(self):
        data = fixture('gen1-unknown')
        check = shelly.observe('shelly-gen1', data['info'], data['status'], server.now())
        self.assertEqual(check['status'], 'unknown')
        self.assertEqual(check['installed_version'], data['info']['fw'])

    def test_gen2_reports_only_the_stable_offer(self):
        data = fixture('gen2-update')
        check = shelly.observe('shelly-rpc', data['info'], data['status'], server.now())
        self.assertEqual((check['status'], check['available_version']), ('available', '1.5.0'))
        data['status']['sys']['available_updates'].pop('stable')
        check = shelly.observe('shelly-rpc', data['info'], data['status'], server.now())
        self.assertEqual((check['status'], check['available_version']), ('no_update', None))
        del data['status']['sys']['available_updates']
        self.assertEqual(shelly.observe('shelly-rpc', data['info'], data['status'], server.now())['status'], 'unknown')


class MediaFirmwareTests(unittest.TestCase):
    def test_nest_compares_system_build_with_production_only(self):
        directory = FIXTURES.parent / 'nest'
        opener = Mock()
        opener.open.return_value = BytesIO((directory / 'release-notes.html').read_bytes())
        latest = nest.fetch_stable_versions(opener)[nest.MODEL]
        self.assertEqual(latest, '3.78.540761')
        opener.open.return_value = BytesIO((directory / 'mini-status.json').read_bytes())
        check = nest.read(FIRST_IP, nest.MODEL, server.now(), opener)
        opener.open.assert_called_with(f'https://{FIRST_IP}:8443{nest.STATUS_PATH}', timeout=2)
        self.assertEqual((check['installed_version'], check['channel']), ('540761', 'stable'))
        self.assertEqual((check['status'], check['available_version']), ('unknown', None))
        self.assertEqual(nest.compare_stable(check['installed_version'], latest), 'current')
        self.assertEqual(nest.compare_stable('550760', latest), 'unverified')

        wrong_model = {'device_info': {'manufacturer': 'Google Inc.', 'model_name': 'Google Home Mini'}}
        opener.open.return_value = BytesIO(json.dumps(wrong_model).encode())
        with self.assertRaisesRegex(ValueError, 'did not identify'):
            nest.read(FIRST_IP, nest.MODEL, server.now(), opener)

    def test_nest_hub_compares_the_full_system_version_for_its_model(self):
        directory = FIXTURES.parent / 'nest'
        opener = Mock()
        opener.open.return_value = BytesIO((directory / 'release-notes.html').read_bytes())
        latest = nest.fetch_stable_versions(opener)[nest.HUB_MODEL]
        self.assertEqual(latest, '31.20260429.103.8713700')

        status = json.loads((directory / 'hub-status.json').read_text(encoding='utf-8'))
        opener.open.return_value = BytesIO(json.dumps(status).encode())
        check = nest.read(FIRST_IP, nest.HUB_MODEL, server.now(), opener)
        self.assertEqual((check['installed_version'], check['channel']), ('32.20260727.103.15100', 'stable'))
        self.assertEqual((check['status'], check['available_version']), ('unknown', None))
        self.assertEqual(nest.compare_stable(check['installed_version'], latest), 'unverified')
        self.assertEqual(nest.compare_stable(latest, latest), 'current')
        self.assertEqual(nest.compare_stable('30.20260121.103.8713700', latest), 'unverified')

        status['device_info']['product_name'] = 'unknown'
        opener.open.return_value = BytesIO(json.dumps(status).encode())
        with self.assertRaisesRegex(ValueError, 'did not identify'):
            nest.read(FIRST_IP, nest.HUB_MODEL, server.now(), opener)

    def test_roku_fails_closed_without_identity_or_a_supported_model(self):
        info = {'vendor-name': 'Roku', 'model-number': '3810X', 'software-version': '15.3.4',
                'software-build': '2402', 'udn': 'not-a-uuid'}
        check = roku.observe(info, server.now())
        self.assertIsNone(check['installed_version'])
        self.assertEqual((check['status'], check['available_version']), ('unknown', None))
        self.assertIsNone(roku.observe({**info, 'model-number': '9999X'}, server.now()))

    def test_wiim_only_offers_an_update_from_the_matching_device(self):
        data = json.loads((FIXTURES.parent / 'wiim' / 'ultra-status.json').read_text())
        opener = Mock()
        opener.open.return_value = BytesIO(json.dumps(data).encode())
        check, mac = wiim.read(FIRST_IP, data['upnp_uuid'], server.now(), opener)
        self.assertEqual((check['installed_version'], check['status'], mac), ('5.2.833832', 'no_update', '020000000001'))
        opener.open.assert_called_once_with(f'https://{FIRST_IP}/httpapi.asp?command=getStatusEx', timeout=2)
        self.assertEqual(wiim.compare_stable('5.2.833832', '5.2.833833'), 'unverified')

        data.update(VersionUpdate='1', NewVer='5.2.833833')
        opener.open.return_value = BytesIO(json.dumps(data).encode())
        check, _ = wiim.read(FIRST_IP, data['upnp_uuid'], server.now(), opener)
        self.assertEqual((check['status'], check['available_version']), ('available', '5.2.833833'))

        opener.open.return_value = BytesIO(json.dumps(data).encode())
        with self.assertRaisesRegex(ValueError, 'did not match'):
            wiim.read(FIRST_IP, 'uuid:12345678-1234-1234-1234-123456789abd', server.now(), opener)

    def test_sonos_publication_is_not_an_update_offer(self):
        opener = Mock()
        opener.open.return_value = BytesIO((FIXTURES.parent / 'sonos' / 'release-notes.html').read_bytes())
        self.assertEqual(sonos.fetch_stable_versions(opener), {'modern': '98.0-82170'})
        self.assertEqual(sonos.compare_stable('97.2-81220', '98.0-82170'), 'newer_published')
        self.assertEqual(sonos.compare_stable('98.0-82170', '98.0-82170'), 'current')
        self.assertIsNone(sonos.catalog_model({'firmware_model': 'unknown'}))


class FirmwareFindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'hearth.sqlite3'
        self.store = server.Store(self.path)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.store.db.close())
        self.store.add_device(FIRST_IP, 'Test relay')
        self.at = datetime.now(timezone.utc).replace(microsecond=0)
        self.data = fixture('gen1-update')

    def poll(self, ip=FIRST_IP):
        with patch.object(server, 'now', return_value=self.at.isoformat()), \
             patch.object(server, 'ping', return_value=(True, None)), \
             patch.object(server, 'port_open', return_value=True), \
             patch.object(server, 'read_json', side_effect=[self.data['info'], self.data['status']]) as read:
            self.store.record(ip, server.probe_device({'ip': ip}))
        self.assertEqual([call.args[1] for call in read.call_args_list], ['/shelly', '/status'])
        self.at += timedelta(seconds=30)

    def findings(self):
        candidates = ((device, shelly.identify(device)) for device in self.store.devices())
        return checks.inventory(candidates, self.at)

    def test_one_alert_per_device_and_version_after_restart_and_address_change(self):
        self.poll()
        self.poll()
        self.store.db.close()
        self.store = server.Store(self.path)
        self.store.add_device(SECOND_IP, 'Same relay')
        self.poll(SECOND_IP)
        self.assertEqual([(row['ip'], row['status']) for row in self.findings()], [(SECOND_IP, 'available')])
        self.assertEqual(len(self.store.events(kind='firmware_update_available')), 1)

        # The release reported as offered is now the installed version.
        self.data['info']['fw'] = self.data['status']['update']['new_version']
        self.poll(SECOND_IP)
        self.assertEqual(self.findings()[0]['status'], 'no_update')
        self.assertEqual(len(self.store.events(kind='firmware_update_available')), 1)

        self.data['status']['update']['new_version'] = '20250301-010101/v1.2.0@3333333'
        self.poll(SECOND_IP)
        self.assertEqual(self.findings()[0]['status'], 'available')
        self.assertEqual(len(self.store.events(kind='firmware_update_available')), 2)

    def test_replacement_device_with_protected_status_does_not_inherit_old_offer(self):
        self.poll()
        self.data['info']['mac'] = '020000000004'
        self.data['status'] = HTTPError('', 401, 'Authentication required', {}, None)
        self.addCleanup(self.data['status'].close)
        self.poll()
        row, = self.findings()
        self.assertEqual((row['device_id'], row['status'], row['available_version']), ('020000000004', 'unknown', None))
        self.assertEqual(len(self.store.events(kind='firmware_update_available')), 1)

    def test_conflicting_network_identity_hides_old_finding(self):
        self.poll()
        with self.store.db:
            self.store.db.execute("UPDATE devices SET payload=json_set(payload, '$.mac', ?) WHERE ip=?",
                                  ('02:00:00:00:00:04', FIRST_IP))
        self.assertEqual(self.findings(), [])

    def test_sensor_telemetry_does_not_refresh_firmware_evidence(self):
        self.poll()
        checked_at = self.findings()[0]['checked_at']
        self.at += checks.MAX_CHECK_AGE
        self.store.record(FIRST_IP, {'status': 'online', 'checked_at': self.at.isoformat(),
                                    'last_telemetry': self.at.isoformat(), 'metrics': {}, 'telemetry_source': 'CoIoT'})
        row, = self.findings()
        self.assertEqual((row['status'], row['checked_at']), ('stale', checked_at))

    def test_missed_poll_marks_awake_device_check_stale(self):
        self.poll()
        self.store.record(FIRST_IP, {'status': 'unreachable', 'checked_at': self.at.isoformat(), 'metrics': {}})
        row, = self.findings()
        self.assertEqual(row['status'], 'stale')
