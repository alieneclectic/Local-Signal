"""Small validation helpers for explicitly configured local device readers."""
import ipaddress


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
