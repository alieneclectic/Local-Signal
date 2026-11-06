import subprocess
import unittest
from unittest.mock import patch

import server


class WindowsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        system = patch.object(server.platform, 'system', return_value='Windows')
        self.addCleanup(system.stop)
        system.start()

    @patch.object(server, 'windows_command')
    def test_discovery_uses_the_best_private_ipv4_default_route(self, command):
        command.return_value = subprocess.CompletedProcess([], 0, stdout='''
            0.0.0.0    0.0.0.0    On-link       10.8.0.2       1
            0.0.0.0    0.0.0.0    10.0.0.1      10.0.0.20      55
            0.0.0.0    0.0.0.0    192.168.1.1   192.168.1.44   25
            0.0.0.0    0.0.0.0    192.168.2.1   1
        ''')
        self.assertEqual(server.network_info(), {
            'gateway': '192.168.1.1', 'local_ip': '192.168.1.44',
            'interface': None, 'subnet': '192.168.1.0/24',
        })
        command.assert_called_once_with('route', 'print', '-4', timeout=3)

    @patch.object(server, 'windows_command')
    def test_unreachable_icmp_reply_is_not_a_successful_ping(self, command):
        command.side_effect = [
            subprocess.CompletedProcess([], 0, stdout='Reply from 192.168.1.1: Destination host unreachable.'),
            subprocess.CompletedProcess([], 0, stdout='Reply from 192.168.1.2: bytes=32 time<1ms TTL=64'),
        ]
        self.assertEqual(server.ping('192.168.1.2'), (False, None))
        self.assertEqual(server.ping('192.168.1.2'), (True, 1.0))
        command.assert_called_with('ping', '-4', '-n', '1', '-w', '850', '192.168.1.2', timeout=2)

    @patch.object(server, 'windows_command')
    def test_arp_cache_is_scoped_to_selected_interface(self, command):
        command.return_value = subprocess.CompletedProcess([], 0, stdout='''
            Interface: 192.168.1.44 --- 0x5
            192.168.1.2         1a-2b-3c-4d-5e-6f     dynamic
            192.168.1.3         00-00-00-00-00-00     invalid
            192.168.1.255       ff-ff-ff-ff-ff-ff     static
            224.0.0.251         01-00-5e-00-00-fb     static
        ''')
        self.assertEqual(server.arp_entries('192.168.1.44'), {'192.168.1.2': '1A:2B:3C:4D:5E:6F'})
        command.assert_called_once_with('arp', '-a', '-N', '192.168.1.44', timeout=4)
