"""The opt-in E2E tool must not widen production networking permissions."""
import socket
import struct
import unittest
from unittest.mock import Mock, patch

import bacnet
import modbus
import server
from tools import commercial_e2e as e2e


class E2EGuardTests(unittest.TestCase):
    def test_production_validators_still_reject_public_and_loopback_targets(self):
        e2e.private_guards()
        for validate in (server.local_ip, bacnet.private_address, modbus.private_address):
            self.assertEqual(validate(e2e.BACNET_ALIAS), e2e.BACNET_ALIAS)
            self.assertEqual(validate(e2e.PULT_ALIAS), e2e.PULT_ALIAS)

    def test_public_transport_requires_opt_in_and_the_exact_private_alias(self):
        for enabled, address in ((False, (e2e.PULT_ALIAS, 502)), (True, ('8.8.8.8', 502)),
                                  (True, e2e.PULT_ENDPOINT), (True, (e2e.PULT_ALIAS, 503))):
            with self.subTest(enabled=enabled, address=address), patch.object(e2e, 'REAL_CONNECT') as connect:
                with self.assertRaises(OSError):
                    e2e.PultSockets(enabled).create_connection(address, timeout=.1)
                connect.assert_not_called()
        with patch.object(e2e, 'REAL_CONNECT', return_value=Mock()) as connect:
            proxy = e2e.PultSockets(True)
            proxy.create_connection((e2e.PULT_ALIAS, 502), timeout=.1)
            connect.assert_called_once_with(e2e.PULT_ENDPOINT, timeout=.1)

    def test_public_socket_blocks_writes_other_maps_and_request_budget_overruns(self):
        sock, owner = Mock(), e2e.PultSockets(True)
        guarded = e2e.GuardedSocket(sock, 'modbus', owner)
        for function, unit, address, count in ((6, 1, 1, 1), (16, 1, 1, 1), (4, 2, 1, 1),
                                                (4, 1, 0, 1), (4, 1, 1, 125), (3, 1, 1, 1)):
            frame = struct.pack('!HHHBBHH', 1, 0, 6, unit, function, address, count)
            with self.subTest(frame=frame.hex()), self.assertRaises(OSError):
                guarded.sendall(frame)
        sock.sendall.assert_not_called()
        for function in (2, 4):
            frame = struct.pack('!HHHBBHH', 1, 0, 6, 1, function, 1, 1)
            guarded.sendall(frame)
        self.assertEqual(owner.requests, 2)
        owner.requests = e2e.MAX_PUBLIC_REQUESTS
        with self.assertRaises(OSError):
            guarded.sendall(frame)
        self.assertEqual(sock.sendall.call_count, 2)

    def test_bacnet_transport_is_only_loopback_for_the_fixed_fixture(self):
        sock, owner = Mock(), e2e.BacnetSockets(12345)
        guarded = e2e.GuardedSocket(sock, 'bacnet', owner, ('127.0.0.1', 12345))
        for address in (('8.8.8.8', 47808), ('127.0.0.1', 12345), (e2e.BACNET_ALIAS, 47809)):
            with self.subTest(address=address), self.assertRaises(OSError):
                guarded.connect(address)
        sock.connect.assert_not_called()
        guarded.connect((e2e.BACNET_ALIAS, 47808))
        sock.connect.assert_called_once_with(('127.0.0.1', 12345))
        frame = bacnet.read_request(1, 0, 1, 85)
        bad = bytearray(frame)
        bad[9] = 15  # WriteProperty service.
        with self.assertRaises(OSError):
            guarded.send(bytes(bad))
        sock.send.assert_not_called()
        guarded.send(frame)
        self.assertEqual(owner.requests, 1)

    def test_unexpected_inventory_never_reaches_any_device_probe(self):
        with patch.object(e2e, 'REAL_PROBE') as probe:
            for device in ({'ip': '10.23.0.10'}, {'ip': e2e.BACNET_ALIAS, 'monitor_config': {'protocol': 'modbus-tcp'}},
                           {'ip': e2e.PULT_ALIAS, 'monitor_config': {}}):
                with self.subTest(device=device), self.assertRaises(OSError):
                    e2e.guarded_probe(device)
            probe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
