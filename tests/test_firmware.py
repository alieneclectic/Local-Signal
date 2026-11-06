from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError
import xml.etree.ElementTree as ET

from firmware import checks
from firmware.adapters import cast, nest, roku, shelly, sonos, wiim
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


class CastFirmwareTests(unittest.TestCase):
    def setUp(self):
        directory = FIXTURES.parent / 'cast'
        self.status = json.loads((directory / 'ultra-status.json').read_text())
        self.description = ET.parse(directory / 'ultra-description.xml').getroot()
        self.releases = (directory / 'release-notes.html').read_bytes()
        self.opener = Mock()
        self.read_xml = Mock(return_value=self.description)

    def read(self, family='chromecast'):
        response = BytesIO(json.dumps(self.status).encode())
        if family == 'chromecast' and 'ssdp_udn' in self.status:
            self.opener.open.side_effect = [URLError('Legacy device'), response]
        else:
            self.opener.open.return_value = response
        return cast.read(FIRST_IP, family, server.now(), self.opener, self.read_xml)

    def test_legacy_chromecast_links_dial_identity_to_production_build(self):
        check, model = self.read()
        self.assertEqual(self.opener.open.call_args_list, [
            call(f'https://{FIRST_IP}:8443{cast.STATUS_PATH}', timeout=2),
            call(f'http://{FIRST_IP}:8008{cast.STATUS_PATH}', timeout=2),
        ])
        self.read_xml.assert_called_once_with(FIRST_IP, f'http://{FIRST_IP}:8008/ssdp/device-desc.xml')
        self.assertEqual(model, 'Chromecast Ultra')
        self.assertEqual((check['device_id'], check['installed_version'], check['channel']),
                         ('020000000008', '1.56.469779', 'stable'))
        self.assertEqual((check['status'], check['available_version']), ('unknown', None))
        self.opener.open.side_effect = None
        self.opener.open.return_value = BytesIO(self.releases)
        versions = cast.fetch_stable_versions(self.opener)
        self.assertEqual(versions, {'Chromecast (2nd gen)': '1.56.467165', 'Chromecast Ultra': '1.56.469779'})
        self.assertEqual(cast.compare_stable(check['installed_version'], versions[model]), 'current')
        self.assertEqual(cast.compare_stable('1.56.281627', versions[model]), 'unverified')

    def test_flat_response_for_another_udn_is_rejected(self):
        self.status['ssdp_udn'] = '260e9e33-49d5-a3f6-4f79-000000000002'
        with self.assertRaisesRegex(ValueError, 'did not match'):
            self.read()

    def test_auth_failure_does_not_retry_over_http(self):
        blocked = HTTPError('https://local', 403, 'Forbidden', {}, None)
        self.addCleanup(blocked.close)
        self.opener.open.side_effect = blocked
        with self.assertRaises(HTTPError):
            cast.read(FIRST_IP, 'chromecast', server.now(), self.opener, self.read_xml)
        self.opener.open.assert_called_once_with(f'https://{FIRST_IP}:8443{cast.STATUS_PATH}', timeout=2)
        self.read_xml.assert_not_called()

    def test_generic_chromecast_reports_version_without_guessing_generation(self):
        self.status = {
            'build_info': {'cast_build_revision': '1.56.467165', 'release_track': 'stable-channel'},
            'device_info': {'manufacturer': 'Google Inc.', 'model_name': 'Chromecast',
                            'mac_address': '02:00:00:00:00:09'},
        }
        check, model = self.read()
        self.read_xml.assert_not_called()
        device = {'protocol': 'google-cast', 'model': model, 'firmware_model': model, 'firmware_check': check}
        self.assertEqual(check['installed_version'], '1.56.467165')
        self.assertIsNone(cast.catalog_model(device))

    def test_riva_cast_firmware_stays_separate_from_chromecast_production(self):
        self.status = {
            'build_info': {'cast_build_revision': '1.52.272222'},
            'device_info': {'manufacturer': 'RIVA Audio', 'model_name': 'RIVA Festival',
                            'mac_address': '02:00:00:00:00:0A'},
        }
        device = {'protocol': 'google-cast', 'advertised_model': '0,1,2',
                  'advertised_names': [{'source': 'Local DNS', 'name': 'riva-festival-example'}]}
        self.assertEqual(cast.family(device), 'riva')
        check, model = self.read('riva')
        self.opener.open.assert_called_once_with(f'http://{FIRST_IP}:8008{cast.STATUS_PATH}', timeout=2)
        self.assertEqual(check['installed_version'], '1.52.272222')
        self.assertEqual((check['status'], check['channel']), ('unknown', 'unknown'))
        self.assertIsNone(cast.catalog_model({**device, 'firmware_model': model, 'firmware_check': check}))
        with self.assertRaisesRegex(ValueError, 'expected receiver'):
            self.read('chromecast')


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

    def test_cast_poll_and_inventory_keep_catalog_matches_tied_to_fresh_local_evidence(self):
        directory = FIXTURES.parent / 'cast'
        opener = Mock()
        opener.open.return_value = BytesIO((directory / 'ultra-status.json').read_bytes())
        description = ET.parse(directory / 'ultra-description.xml').getroot()
        device = {'ip': FIRST_IP, 'protocol': 'google-cast', 'advertised_model': 'Chromecast'}
        self.store.apply_names([{'ip': FIRST_IP, 'source': 'mDNS friendly name',
                                 'name': 'Test Chromecast', 'model': 'Chromecast'}])
        with patch.object(server, 'ping', return_value=(False, None)), \
             patch.object(server.media_readers, 'read', side_effect=OSError('Cast playback unavailable')), \
             patch.object(server.media_readers, 'local_status_opener', return_value=opener), \
             patch.object(server.media_readers, 'read_xml', return_value=description):
            self.store.record(FIRST_IP, server.probe_device(device))

        handler = Mock(store=self.store, path='/api/state')
        handler.valid_host.return_value = True
        handler.collector.snapshot.return_value = {}
        saved, = [d for d in self.store.devices() if d['ip'] == FIRST_IP]
        self.assertEqual(saved['status'], 'reachable')
        for age, expected in ((timedelta(), 'current'), (checks.MAX_CHECK_AGE, 'unverified')):
            with self.subTest(age=age):
                at = datetime.fromisoformat(saved['firmware_check']['checked_at']) + age
                self.store.save_setting(server.FIRMWARE_CATALOG_KEYS['cast'], {
                    'checked_at': at.isoformat(), 'versions': {'Chromecast Ultra': '1.56.469779'},
                })
                with patch.object(server, 'datetime') as clock:
                    clock.now.return_value = at
                    server.Handler.do_GET(handler)
                state = handler.send_json.call_args.args[0]
                finding, = state['firmware']
                self.assertEqual((finding['adapter_id'], finding['stable_status']), ('cast', expected))
                self.assertEqual(state['firmware_events'], [])
