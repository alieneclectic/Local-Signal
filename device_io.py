"""Small validation helpers for explicitly configured local device readers."""
import ipaddress
import json
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


def private_address(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise ValueError('Enter a private IPv4 device address.') from None
    if not any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')) or address.packed[-1] in (0, 255):
        raise ValueError('Only private IPv4 device addresses are supported.')
    return str(address)


def port(value):
    if type(value) is not int or not 1 <= value <= 65535:
        raise ValueError('Port must be an integer from 1 to 65535.')
    return value


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_json(ip, path, timeout=2, *, tcp_port=80):
    """Read one bounded JSON object from a private device without redirects or proxies."""
    address = private_address(ip)
    authority = address if tcp_port == 80 else f'{address}:{port(tcp_port)}'
    opener = build_opener(ProxyHandler({}), NoRedirects())
    request = Request(f'http://{authority}{path}', headers={'Accept': 'application/json', 'User-Agent': 'Hearth-Local/1.0'})
    with opener.open(request, timeout=timeout) as response:
        raw = response.read(262_145)
        if len(raw) > 262_144:
            raise ValueError('Device response exceeds 256 KB.')
        try:
            value = json.loads(raw)
        except (ValueError, RecursionError):
            raise ValueError('Device did not return valid JSON.') from None
        if not isinstance(value, dict):
            raise ValueError('Device did not return a JSON object.')
        return value
