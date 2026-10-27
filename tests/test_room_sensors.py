from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

import coiot
import server

# An H&T v2 status frame with a synthetic hardware ID and extended URI options.
FRAME = bytes.fromhex('501e97f5b36369740173ed0bec02534848542d31234343444445452332d2439600820200ff7b2247223a5b5b302c393130332c305d2c5b302c333130312c32312e33375d2c5b302c333130322c37302e34375d2c5b302c333130332c35372e305d2c5b302c333131352c305d2c5b302c333131312c39315d2c5b302c393130322c5b22627574746f6e225d5d5d7d')


def frame_values(values):
    return FRAME.split(b'\xff', 1)[0] + b'\xff' + json.dumps({'G': [[0, k, v] for k, v in values.items()]}).encode()


class BroadcastTests(unittest.TestCase):
    def test_ht_frame_has_celsius_room_temp_and_humidity(self):
        report = coiot.parse_ht_report(FRAME)
        self.assertEqual(report['device_id'], 'CCDDEE')
        self.assertEqual(report['metrics']['temperature_c']['value'], 21.37)
        self.assertEqual(report['metrics']['humidity_pct']['value'], 57)
        self.assertEqual(report['metrics']['battery_pct']['value'], 91)

    def test_relay_frames_cannot_become_room_temperature(self):
        self.assertIsNone(coiot.parse_ht_report(FRAME.replace(b'SHHT-1', b'SHSW-1')))
        self.assertIsNone(coiot.parse_ht_report(FRAME.replace(b'cit', b'bad')))

    def test_invalid_sentinels_and_sensor_error_are_omitted(self):
        self.assertIsNone(coiot.parse_ht_report(frame_values({3101: 999, 3102: 999, 3103: 999, 3111: -1})))
        report = coiot.parse_ht_report(frame_values({3101: 21, 3103: 50, 3115: 1, 3111: 80}))
        self.assertNotIn('temperature_c', report['metrics'])
        self.assertNotIn('humidity_pct', report['metrics'])
        self.assertEqual(report['metrics']['battery_pct']['value'], 80)

    def test_fahrenheit_only_report_is_converted(self):
        report = coiot.parse_ht_report(frame_values({3102: 68, 3103: 50}))
        self.assertEqual(report['metrics']['temperature_c']['value'], 20)

    def test_malformed_datagrams_are_rejected(self):
        for raw in (b'', b'bad', FRAME[:12], FRAME[:30], FRAME.split(b'\xff')[0], FRAME[:4] + b'\xf0', FRAME + b'garbage'):
            with self.subTest(raw=raw):
                try:
                    self.assertIsNone(coiot.parse_ht_report(raw))
                except (ValueError, UnicodeError):
                    pass


class RoomSensorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.tmp.name) / 'sensors.sqlite3')
        self.ip = '10.23.1.57'
        self.at = (datetime.now(timezone.utc) - timedelta(minutes=45)).isoformat(timespec='seconds')
        self.store.add_device(self.ip, 'First floor sensor', room='First floor', expected_sleep=True)
        self.store.record(self.ip, {'status': 'online', 'reachable': True, 'checked_at': self.at, 'last_telemetry': self.at,
            'model': 'Shelly H&T', 'kind': 'sensor', 'mac': '02AABBCCDDEE', 'info': {'type': 'SHHT-1', 'sleep_mode': True},
            'metrics': {'temperature_c': {'value': 21.37, 'unit': '°C', 'label': 'Room temperature'},
                        'humidity_pct': {'value': 57, 'unit': '%', 'label': 'Humidity'}}})
        self.store.link_room('first-floor', self.ip)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_sleep_preserves_reading_and_its_original_age(self):
        self.store.record(self.ip, {'status': 'unreachable', 'reachable': False, 'checked_at': server.now(), 'metrics': {}})
        room = self.store.rooms()[0]
        self.assertEqual(room['temperature']['value'], 21.37)
        self.assertEqual(room['temperature']['reported_at'], self.at)
        self.assertTrue(room['temperature']['stale'])
        self.assertGreater(room['temperature']['age_seconds'], 2600)
        self.assertEqual(room['sensor']['status'], 'unreachable')

    def test_room_link_rejects_heater_internal_temperature(self):
        ip = '10.23.1.45'
        self.store.record(ip, {'status': 'online', 'reachable': True, 'checked_at': server.now(), 'mac': 'AABBCCDDEEFF',
            'metrics': {'internal_temperature_c': {'value': 55, 'unit': '°C', 'label': 'Internal temperature'}}})
        with self.assertRaises(ValueError):
            self.store.link_room('second-floor', ip, 'internal_temperature_c')
        self.assertIsNone(self.store.rooms()[1]['temperature'])

    def test_broadcast_refreshes_identified_sensor_only(self):
        self.assertTrue(self.store.receive_sensor(self.ip, coiot.parse_ht_report(FRAME)))
        room = self.store.rooms()[0]
        self.assertEqual(room['temperature']['source'], 'CoIoT')
        self.assertFalse(room['temperature']['stale'])
        self.assertEqual(room['sensor']['ip'], self.ip)
        unknown = {**coiot.parse_ht_report(FRAME), 'device_id': 'FFFFFF'}
        self.assertFalse(self.store.receive_sensor('10.23.1.99', unknown))
        self.assertFalse(any(d['ip'] == '10.23.1.99' for d in self.store.devices()))

    def test_invalid_temperature_report_does_not_refresh_temperature_age(self):
        report = coiot.parse_ht_report(frame_values({3115: 1, 3111: 80}))
        self.store.receive_sensor(self.ip, report)
        room = self.store.rooms()[0]
        self.assertEqual(room['temperature']['reported_at'], self.at)
        self.assertTrue(room['temperature']['stale'])
        self.assertFalse(room['battery']['stale'])

    def test_link_follows_hardware_identity_when_ip_changes(self):
        self.store.receive_sensor('10.23.1.99', coiot.parse_ht_report(FRAME))
        self.assertEqual(self.store.rooms()[0]['sensor']['ip'], '10.23.1.99')
        self.assertTrue(self.store.receive_sensor('10.23.1.99', coiot.parse_ht_report(FRAME)))
        self.assertEqual(self.store.rooms()[0]['temperature']['value'], 21.37)

    def test_unlink_preserves_samples_and_inventory(self):
        count = len(self.store.history(ip=self.ip))
        self.store.link_room('first-floor', None)
        self.assertIsNone(self.store.rooms()[0]['sensor'])
        self.assertEqual(len(self.store.history(ip=self.ip)), count)


if __name__ == '__main__':
    unittest.main()
