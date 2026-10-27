"""Bounded, read-only LAN name discovery: DNS, Bonjour/mDNS, and UPnP."""
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import re
import secrets
import socket
import struct
import time
import unicodedata
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET

MDNS = ('224.0.0.251', 5353)
COMMON_SERVICES = ('_googlecast._tcp.local', '_airplay._tcp.local', '_raop._tcp.local',
                   '_sonos._tcp.local', '_http._tcp.local', '_hap._tcp.local',
                   '_spotify-connect._tcp.local', '_linkplay._tcp.local', '_roku-rcp._tcp.local',
                   '_printer._tcp.local', '_ipp._tcp.local', '_smb._tcp.local')
PRIORITY = {'Device API': 0, 'mDNS friendly name': 0, 'UPnP friendly name': 1, 'mDNS service': 2, 'mDNS hostname': 3, 'Local DNS': 4}
PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def private_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and any(address in n for n in PRIVATE)
    except ValueError:
        return False


def clean_name(value):
    if not isinstance(value, str):
        return None
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C')).strip().rstrip('.')
    if not value or len(value) > 100 or value.lower() in ('unknown', 'localhost', 'local', 'device'):
        return None
    return value


def dns_name(raw, pos):
    labels, seen, resume = [], set(), None
    for _ in range(128):
        if pos >= len(raw) or pos in seen:
            raise ValueError('Invalid compressed DNS name.')
        seen.add(pos)
        length = raw[pos]
        pos += 1
        if length == 0:
            return labels, resume if resume is not None else pos
        if length & 0xC0 == 0xC0:
            if pos >= len(raw):
                raise ValueError('Truncated DNS pointer.')
            if resume is None:
                resume = pos + 1
            pos = ((length & 0x3F) << 8) | raw[pos]
            continue
        if length & 0xC0 or length > 63 or pos + length > len(raw):
            raise ValueError('Invalid DNS label.')
        labels.append(raw[pos:pos + length].decode('utf-8', errors='replace'))
        pos += length
        if sum(len(x) + 1 for x in labels) > 255:
            raise ValueError('DNS name too long.')
    raise ValueError('Too many DNS name pointers.')


def encode_name(name):
    parts = name.rstrip('.').split('.')
    encoded = [p.encode('utf-8') for p in parts]
    if any(not p or len(p) > 63 for p in encoded) or sum(len(p) + 1 for p in encoded) > 255:
        raise ValueError('Invalid DNS query name.')
    return b''.join(bytes([len(p)]) + p for p in encoded) + b'\0'


def question(name, kind=12, query_id=0, recursive=False):
    return struct.pack('!6H', query_id, 0x0100 if recursive else 0, 1, 0, 0, 0) + encode_name(name) + struct.pack('!HH', kind, 1)


def dns_records(raw):
    if len(raw) < 12 or len(raw) > 65535:
        raise ValueError('Invalid DNS packet size.')
    ident, flags, questions, answers, auth, extra = struct.unpack('!6H', raw[:12])
    if not flags & 0x8000 or flags & 15:
        return []
    if questions + answers + auth + extra > 1024:
        raise ValueError('Too many DNS records.')
    pos = 12
    for _ in range(questions):
        _, pos = dns_name(raw, pos)
        pos += 4
        if pos > len(raw):
            raise ValueError('Truncated DNS question.')
    result = []
    for _ in range(answers + auth + extra):
        labels, pos = dns_name(raw, pos)
        if pos + 10 > len(raw):
            raise ValueError('Truncated DNS record.')
        kind, cls, ttl, length = struct.unpack('!HHIH', raw[pos:pos + 10])
        pos += 10
        end = pos + length
        if end > len(raw):
            raise ValueError('Truncated DNS data.')
        record = {'name': '.'.join(labels).lower(), 'labels': labels, 'type': kind, 'ttl': ttl}
        if kind == 1 and length == 4:
            record['address'] = socket.inet_ntoa(raw[pos:end])
        elif kind in (12, 5):
            record['target_labels'], _ = dns_name(raw, pos)
            record['target'] = '.'.join(record['target_labels'])
        elif kind == 33 and length >= 7:
            record['port'] = struct.unpack('!H', raw[pos + 4:pos + 6])[0]
            record['target_labels'], _ = dns_name(raw, pos + 6)
            record['target'] = '.'.join(record['target_labels'])
        elif kind == 16:
            txt, cursor = {}, pos
            while cursor < end:
                size = raw[cursor]
                cursor += 1
                if cursor + size > end:
                    raise ValueError('Truncated DNS TXT item.')
                item = raw[cursor:cursor + size].decode('utf-8', errors='replace')
                cursor += size
                key, separator, value = item.partition('=')
                # Keep only public human-readable name/model fields, not arbitrary TXT data.
                if separator and key.lower() in ('fn', 'name', 'md', 'model', 'am'):
                    txt[key.lower()] = value
            record['txt'] = txt
        result.append(record)
        pos = end
    return result


def mdns_names(ips, interface_ip, duration=5):
    allowed = set(ips)
    records, sent = {}, set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(.2)
    sock.bind((interface_ip or '0.0.0.0', 0))
    if interface_ip:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    def ask(name, kind=12):
        key = (name.lower(), kind)
        if key not in sent and len(sent) < 160:
            sent.add(key)
            sock.sendto(question(name, kind), MDNS)
    try:
        ask('_services._dns-sd._udp.local')
        for service in COMMON_SERVICES:
            ask(service)
        for ip in ips:
            ask(ipaddress.ip_address(ip).reverse_pointer)
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                raw, peer = sock.recvfrom(65535)
                if peer[1] != 5353 or not private_ip(peer[0]):
                    continue
                for record in dns_records(raw):
                    if not record['ttl']:
                        continue
                    records[(record['name'], record['type'], record.get('target', record.get('address', '')))] = record
                    if record['type'] == 12 and record.get('target', '').lower().endswith('.local'):
                        if record['name'] == '_services._dns-sd._udp.local':
                            ask(record['target'])
                        else:
                            ask(record['target'], 33)
                            ask(record['target'], 16)
                    elif record['type'] == 33 and record.get('target', '').lower().endswith('.local'):
                        ask(record['target'], 1)
            except (socket.timeout, ValueError, UnicodeError):
                continue
    finally:
        sock.close()
    return names_from_mdns(list(records.values()), allowed)


def names_from_mdns(records, allowed):
    addresses, txt, result = {}, {}, []
    for record in records:
        if record['type'] == 1 and record.get('address') in allowed:
            addresses.setdefault(record['name'], set()).add(record['address'])
        if record['type'] == 16:
            txt[record['name']] = record.get('txt', {})
    for hostname, ips in addresses.items():
        for ip in ips:
            result.append({'ip': ip, 'name': hostname.removesuffix('.local'), 'hostname': hostname,
                           'source': 'mDNS hostname'})
    for record in records:
        if record['type'] != 33:
            continue
        hostname = record.get('target', '').lower()
        for ip in addresses.get(hostname, []):
            fields = txt.get(record['name'], {})
            labels = record['labels']
            instance_parts = []
            for label in labels:
                if label.startswith('_'):
                    break
                instance_parts.append(label)
            instance = '.'.join(instance_parts)
            friendly = clean_name(fields.get('fn') or fields.get('name'))
            name = friendly or clean_name(instance)
            # AirTunes advertises a MAC before '@'; the text after it is the published name.
            if name and re.match(r'^(?:[0-9A-Fa-f]{12}|RINCON_[0-9A-Fa-f]+)@', name):
                name = name.split('@', 1)[1]
            if name:
                result.append({'ip': ip, 'name': name, 'hostname': hostname,
                               'source': 'mDNS friendly name' if friendly else 'mDNS service',
                               'model': clean_name(fields.get('md') or fields.get('model') or fields.get('am'))})
    # Some responders answer reverse lookups but do not include an A record.
    reverse = {ipaddress.ip_address(ip).reverse_pointer: ip for ip in allowed}
    for record in records:
        if record['type'] == 12 and record['name'] in reverse:
            target = record.get('target', '').rstrip('.')
            result.append({'ip': reverse[record['name']], 'name': target.removesuffix('.local'),
                           'hostname': target, 'source': 'mDNS hostname'})
    return result


def reverse_names(ips, gateway):
    if not gateway or not private_ip(gateway):
        return []
    def resolve(ip):
        query_id = secrets.randbelow(65536)
        packet = question(ipaddress.ip_address(ip).reverse_pointer, query_id=query_id, recursive=True)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.9)
            sock.connect((gateway, 53))
            sock.send(packet)
            raw = sock.recv(8192)
        if len(raw) < 2 or struct.unpack('!H', raw[:2])[0] != query_id:
            return []
        result = []
        expected = ipaddress.ip_address(ip).reverse_pointer
        for record in dns_records(raw):
            if record['type'] == 12 and record['name'] == expected and record.get('target'):
                target = record['target'].rstrip('.')
                # Keep the actual host spelling; strip only the local DNS suffix for display.
                name = re.sub(r'\.(lan|local|home|home\.arpa)$', '', target, flags=re.I)
                result.append({'ip': ip, 'name': name, 'hostname': target, 'source': 'Local DNS'})
        return result
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        jobs = [pool.submit(resolve, ip) for ip in ips]
        for job in concurrent.futures.as_completed(jobs):
            try:
                results.extend(job.result())
            except (OSError, ValueError, UnicodeError):
                pass
    return results


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def upnp_names(ips, interface_ip, duration=2):
    allowed, locations = set(ips), {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
        sock.bind((interface_ip or '0.0.0.0', 0))
        sock.settimeout(.2)
        if interface_ip:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(interface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        request = b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: upnp:rootdevice\r\n\r\n'
        sock.sendto(request, ('239.255.255.250', 1900))
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            try:
                raw, peer = sock.recvfrom(8192)
                if peer[0] not in allowed or not raw.startswith(b'HTTP/1.1 200'):
                    continue
                headers = {}
                for line in raw.decode('utf-8', errors='replace').splitlines()[1:]:
                    key, sep, value = line.partition(':')
                    if sep:
                        headers[key.lower().strip()] = value.strip()
                url = headers.get('location', '')
                parts = urlparse(url)
                if parts.scheme == 'http' and parts.hostname == peer[0] and not (parts.username or parts.password or parts.query or parts.fragment) and parts.path.lower().endswith('.xml'):
                    locations[peer[0]] = url
            except (socket.timeout, ValueError):
                continue
    def read(ip, url):
        opener = build_opener(ProxyHandler({}), NoRedirects())
        with opener.open(Request(url, headers={'User-Agent': 'Hearth-Local/1.0'}), timeout=1.5) as response:
            body = response.read(131073)
        if len(body) > 131072 or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
            return []
        tree = ET.fromstring(body)
        device = tree.find('./{*}device')
        name = device.findtext('./{*}friendlyName') if device is not None else None
        return [{'ip': ip, 'name': name, 'source': 'UPnP friendly name', 'model': clean_name(device.findtext('./{*}modelName')), 'description_url': url}] if clean_name(name) else []
    result = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(read, ip, url) for ip, url in locations.items()]
        for job in concurrent.futures.as_completed(jobs):
            try:
                result.extend(job.result())
            except (OSError, ValueError, ET.ParseError):
                pass
    return result


def discover(ips, network):
    ips = sorted({ip for ip in ips if private_ip(ip)})[:254]
    found, errors = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        jobs = {'mDNS': pool.submit(mdns_names, ips, network.get('local_ip')),
                'Local DNS': pool.submit(reverse_names, ips, network.get('gateway')),
                'UPnP': pool.submit(upnp_names, ips, network.get('local_ip'))}
        for name, job in jobs.items():
            try:
                found.extend(job.result())
            except (OSError, ValueError) as exc:
                errors.append(f'{name}: {exc}')
    unique = {}
    for entry in found:
        name = clean_name(entry.get('name'))
        if name and entry['ip'] in ips:
            entry['name'] = name
            unique[(entry['ip'], entry['source'], name)] = entry
    return list(unique.values()), errors


def name_rank(entry):
    name = entry.get('name', '')
    opaque = bool(re.fullmatch(r'[0-9a-fA-F-]{20,}', name) or re.search(r'RINCON_[0-9A-Fa-f]+', name))
    composite = bool(re.match(r'\d+\.\d+\.\d+\.\d+\s+-', name))
    return (PRIORITY.get(entry.get('source'), 20) + (10 if opaque or composite else 0), len(name), name.casefold())
