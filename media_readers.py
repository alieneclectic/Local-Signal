"""Read-only local telemetry for Roku, Sonos/UPnP, and Google Cast receivers."""
from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math
import re
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener
import xml.etree.ElementTree as ET

from firmware.adapters import cast as cast_firmware, nest, roku as roku_firmware, sonos as sonos_firmware, wiim
from network_names import private_ip

MAX_RESPONSE = 262_144
READ_ACTIONS = {'GetVolume', 'GetMute', 'GetTransportInfo', 'GetPositionInfo', 'GetMediaInfo', 'GetZoneGroupState'}
_DESCRIPTOR_CACHE = {}
_CACHE_LOCK = threading.Lock()


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def text(value, limit=200):
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def numeric(value):
    try:
        number = float(value)
        return number if not isinstance(value, bool) and math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def add_metric(metrics, key, value, label, unit='', minimum=None, maximum=None, boolean=False):
    value = numeric(value)
    if value is None or minimum is not None and value < minimum or maximum is not None and value > maximum:
        return
    metrics[key] = {'value': round(value, 3), 'unit': unit, 'label': label}
    if boolean:
        metrics[key]['kind'] = 'boolean'


def add_text(metrics, key, value, label):
    value = text(value)
    if value:
        metrics[key] = {'value': value, 'unit': '', 'label': label}


def safe_url(ip, url):
    parts = urlparse(url)
    if not private_ip(ip) or parts.scheme != 'http' or parts.hostname != ip or parts.username or parts.password or parts.fragment:
        raise ValueError('Telemetry HTTP requests must target the identified local device.')
    if not 1 <= (parts.port or 80) <= 65535 or any(c in url for c in ('\r', '\n', '\\')):
        raise ValueError('Invalid local API URL.')
    return url


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_status_opener():
    # WiiM and Google Cast status endpoints use device certificates outside the system trust store.
    # The adapters restrict requests to fixed paths on private IPs and validate the reported identity.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return build_opener(ProxyHandler({}), NoRedirects(), HTTPSHandler(context=context))


def read_xml(ip, url, action=None, service=None, args=None):
    safe_url(ip, url)
    headers = {'User-Agent':'Hearth-Local/1.0'}
    data = None
    if action is not None:
        if action not in READ_ACTIONS or not re.fullmatch(r'urn:schemas-upnp-org:service:[A-Za-z]+:[1-3]', service or ''):
            raise ValueError('Only supported read-only UPnP queries are allowed.')
        if urlparse(url).query:
            raise ValueError('UPnP control paths cannot contain query parameters.')
        envelope = ET.Element('s:Envelope', {'xmlns:s':'http://schemas.xmlsoap.org/soap/envelope/', 's:encodingStyle':'http://schemas.xmlsoap.org/soap/encoding/'})
        body = ET.SubElement(envelope, 's:Body')
        query = ET.SubElement(body, 'u:' + action, {'xmlns:u': service})
        for key, value in (args or {}).items():
            ET.SubElement(query, key).text = str(value)
        data = ET.tostring(envelope, encoding='utf-8', xml_declaration=True)
        headers.update({'Content-Type':'text/xml; charset="utf-8"', 'SOAPACTION':f'"{service}#{action}"'})
    opener = build_opener(ProxyHandler({}), NoRedirects())
    try:
        with opener.open(Request(url, data=data, headers=headers), timeout=2) as response:
            raw = response.read(MAX_RESPONSE + 1)
    except HTTPError as exc:
        exc.close()
        raise
    if len(raw) > MAX_RESPONSE or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Invalid or oversized device XML.')
    return ET.fromstring(raw)


def leaf_values(root):
    return {node.tag.split('}')[-1]: node.text for node in root.iter() if len(node) == 0}


def hhmmss(value):
    if not value or not re.fullmatch(r'\d+:\d{2}:\d{2}(?:\.\d+)?', value):
        return None
    hours, minutes, seconds = (float(n) for n in value.split(':'))
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def finished(protocol, model, metrics, notes=None, mac=None, firmware=None):
    if not metrics:
        raise ValueError('No supported device measurements were returned.')
    when = stamp()
    # Curated values only: no playback URLs, authentication material, or advertising IDs.
    return {'status':'online', 'reachable':True, 'protocol':protocol, 'kind':'media', 'model':model,
            'metrics':metrics, 'error':None, 'telemetry_note':' '.join(notes or []) or None,
            'last_telemetry':when, 'telemetry_source':protocol,
            'raw_status':{key: item['value'] for key, item in metrics.items()}, 'raw_status_at':when,
            **({'mac':mac} if mac else {}), **({'firmware':firmware} if firmware else {})}


def roku(ip):
    root = read_xml(ip, f'http://{ip}:8060/query/device-info')
    if root.tag != 'device-info':
        raise ValueError('Not a Roku device-info response.')
    values, metrics, notes = leaf_values(root), {}, []
    model = values.get('model-name') or 'Roku player'
    power = values.get('power-mode')
    add_text(metrics, 'power_mode', {'Suspend':'Standby', 'PowerOn':'On'}.get(power, power), 'Player power mode')
    add_metric(metrics, 'uptime_s', values.get('uptime'), 'Uptime', 's', minimum=0)
    add_text(metrics, 'firmware_version', values.get('software-version'), 'Firmware version')
    add_text(metrics, 'network_type', values.get('network-type'), 'Network connection')
    if values.get('headphones-connected') in ('true', 'false'):
        add_metric(metrics, 'headphones_connected', int(values['headphones-connected'] == 'true'), 'Headphones connected', boolean=True)
    try:
        active = read_xml(ip, f'http://{ip}:8060/query/active-app')
        app = active.find('app')
        saver = active.find('screensaver')
        add_text(metrics, 'active_app', app.text if app is not None else None, 'Active app')
        add_text(metrics, 'screensaver', saver.text if saver is not None else None, 'Screensaver')
    except (HTTPError, URLError, OSError, ValueError, ET.ParseError):
        notes.append('Active-app details were not available.')
    try:
        media = read_xml(ip, f'http://{ip}:8060/query/media-player')
        add_text(metrics, 'playback_state', media.attrib.get('state'), 'Playback state')
    except HTTPError as exc:
        if exc.code in (401, 403):
            notes.append('Roku restricts detailed playback status; the displayed power mode, app, and uptime remain available.')
        else:
            notes.append('Detailed playback status is unavailable.')
    except (OSError, ValueError, ET.ParseError):
        notes.append('Detailed playback status is unavailable.')
    connection = values.get('network-type')
    mac = values.get('wifi-mac') if connection == 'wifi' else values.get('ethernet-mac')
    result = finished('roku-ecp', model, metrics, notes, mac, values.get('software-version'))
    result['reported_name'] = text(values.get('user-device-name') or values.get('friendly-device-name'))
    result['firmware_check'] = roku_firmware.observe(values, result['last_telemetry'])
    result['firmware_peer_mac'] = mac
    return result


def descriptor(ip, url, fresh=False):
    key = (ip, url)
    with _CACHE_LOCK:
        cached = _DESCRIPTOR_CACHE.get(key)
        if not fresh and cached and time.monotonic() - cached[0] < 900:
            return cached[1]
    root = read_xml(ip, url)
    device = root.find('./{*}device')
    if device is None:
        raise ValueError('No UPnP device description.')
    services = {}
    for service in device.findall('.//{*}service'):
        urn = service.findtext('{*}serviceType') or ''
        if re.fullmatch(r'urn:schemas-upnp-org:service:(AVTransport|RenderingControl|ZoneGroupTopology):[1-3]', urn):
            name = urn.split(':')[-2]
            control = urljoin(url, service.findtext('{*}controlURL') or '')
            safe_url(ip, control)
            if urlparse(control).query:
                continue
            services[name] = (urn, control)
    result = {'model':device.findtext('{*}modelName') or 'Media renderer',
              'model_number':device.findtext('{*}modelNumber'),
              'services':services, 'udn':device.findtext('{*}UDN'),
              'firmware':device.findtext('.//{*}softwareVersion'), 'name':device.findtext('{*}friendlyName'),
              'checked_at':stamp()}
    with _CACHE_LOCK:
        _DESCRIPTOR_CACHE[key] = (time.monotonic(), result)
    return result


def upnp(ip, description_url, sonos=False):
    info = descriptor(ip, description_url, fresh=sonos)
    metrics, notes = {}, []
    def query(service, action, args=None):
        if service not in info['services']:
            return {}
        urn, url = info['services'][service]
        try:
            return leaf_values(read_xml(ip, url, action, urn, args))
        except (HTTPError, URLError, OSError, ValueError, ET.ParseError):
            notes.append(f'{action} was not available.')
            return {}
    args = {'InstanceID':0, 'Channel':'Master'}
    volume = query('RenderingControl', 'GetVolume', args).get('CurrentVolume')
    mute = query('RenderingControl', 'GetMute', args).get('CurrentMute')
    independent = 'AVTransport' in info['services']
    add_metric(metrics, 'volume_pct' if independent else 'component_volume_pct', volume,
               'Volume' if independent else 'Component volume level', '%', 0, 100)
    add_metric(metrics, 'muted', mute, 'Muted', minimum=0, maximum=1, boolean=True)
    if independent:
        transport = query('AVTransport', 'GetTransportInfo', {'InstanceID':0})
        states = {'PLAYING':'Playing','PAUSED_PLAYBACK':'Paused','STOPPED':'Stopped','NO_MEDIA_PRESENT':'No media','TRANSITIONING':'Transitioning'}
        state = transport.get('CurrentTransportState')
        add_text(metrics, 'playback_state', states.get(state, state), 'Playback state' if sonos else 'Network playback')
        position = query('AVTransport', 'GetPositionInfo', {'InstanceID':0})
        add_metric(metrics, 'media_position_s', hhmmss(position.get('RelTime')), 'Track position', 's', minimum=0)
        add_metric(metrics, 'media_duration_s', hhmmss(position.get('TrackDuration')), 'Track duration', 's', minimum=0)
        metadata = position.get('TrackMetaData')
        if metadata and metadata != 'NOT_IMPLEMENTED' and '<!DOCTYPE' not in metadata.upper() and '<!ENTITY' not in metadata.upper():
            try:
                track = ET.fromstring(metadata)
                add_text(metrics, 'media_title', track.findtext('.//{*}title'), 'Title')
                add_text(metrics, 'media_artist', track.findtext('.//{*}creator'), 'Artist')
                add_text(metrics, 'media_album', track.findtext('.//{*}album'), 'Album')
            except ET.ParseError:
                pass
    elif sonos:
        add_text(metrics, 'player_role', 'No independent transport', 'Player role')
        notes.append('This Sonos component follows its room coordinator. Its component level is not the overall room volume.')
    if sonos and 'ZoneGroupTopology' in info['services']:
        groups = query('ZoneGroupTopology', 'GetZoneGroupState').get('ZoneGroupState')
        if groups and '<!DOCTYPE' not in groups.upper() and '<!ENTITY' not in groups.upper():
            try:
                topology = ET.fromstring(groups)
                own = (info.get('udn') or '').removeprefix('uuid:')
                for group in topology.findall('.//ZoneGroup'):
                    for node in group.iter():
                        if node.attrib.get('UUID') == own:
                            role = 'Bonded component' if node.tag == 'Satellite' else 'Coordinator' if group.get('Coordinator') == own else 'Group member'
                            add_text(metrics, 'player_role', role, 'Player role')
                            add_text(metrics, 'speaker_room', node.get('ZoneName'), 'Speaker room')
            except ET.ParseError:
                pass
    udn = info.get('udn') or ''
    match = re.search(r'RINCON_([0-9A-Fa-f]{12})', udn) if sonos else None
    result = finished('sonos-upnp' if sonos else 'upnp-av', info['model'], metrics, notes,
                      match[1] if match else None, info.get('firmware'))
    result['reported_name'] = metrics.get('speaker_room', {}).get('value') if sonos else info.get('name')
    if sonos:
        result['firmware_check'] = sonos_firmware.observe(result.get('mac'), info.get('firmware'), info.get('checked_at'))
        result['firmware_model'] = info.get('model_number')
        result['firmware_peer_mac'] = result.get('mac')
    elif wiim.matches(result):
        try:
            check, peer_mac = wiim.read(ip, info.get('udn'), stamp(), local_status_opener())
            result.update(firmware_check=check, firmware_peer_mac=peer_mac, firmware=check['installed_version'])
            if peer_mac:
                result['mac'] = peer_mac
        except (OSError, ValueError) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            result['firmware_check'] = None
    return result


def cast(ip, device):
    try:
        import pychromecast
        from pychromecast.models import CastInfo, HostServiceInfo
    except ImportError as exc:
        raise ValueError('Google Cast telemetry needs the project requirements installed.') from exc
    client = None
    try:
        # Direct LAN connection: no DIAL discovery, remote artwork, accounts, or app launches.
        info = CastInfo({HostServiceInfo(ip, 8009)}, None, device.get('advertised_model'), device.get('name'), ip, 8009, 'cast', None)
        client = pychromecast.Chromecast(info, tries=1, retry_wait=.3, timeout=2)
        client.wait(timeout=4)
        status = client.status
        metrics = {}
        add_metric(metrics, 'volume_pct', None if status.volume_level is None else status.volume_level * 100, 'Cast receiver volume', '%', 0, 100)
        if status.volume_muted is not None:
            add_metric(metrics, 'muted', int(status.volume_muted), 'Cast receiver muted', boolean=True)
        add_text(metrics, 'receiver_state', 'Idle' if client.is_idle else 'App active', 'Cast receiver')
        add_text(metrics, 'active_app', status.display_name, 'Cast app')
        if not client.is_idle:
            event = threading.Event()
            class Listener:
                def new_media_status(self, _status):
                    event.set()
            client.media_controller.register_status_listener(Listener())
            try:
                client.media_controller.update_status()
                event.wait(.8)
            except Exception:
                pass
            media = client.media_controller.status
            if media.player_state and media.player_state != 'UNKNOWN':
                add_text(metrics, 'playback_state', media.player_state.replace('_',' ').title(), 'Cast playback state')
                add_text(metrics, 'media_title', media.title, 'Title')
                add_text(metrics, 'media_artist', media.artist, 'Artist')
                add_text(metrics, 'media_album', getattr(media, 'album_name', None), 'Album')
                add_metric(metrics, 'media_position_s', media.current_time, 'Track position', 's', minimum=0)
                add_metric(metrics, 'media_duration_s', media.duration, 'Track duration', 's', minimum=0)
        result = finished('google-cast', device.get('advertised_model') or 'Google Cast receiver', metrics,
                          ['Cast status describes the receiver; an idle Cast receiver does not prove the entire device is powered off.'])
        if nest.matches(result):
            model = result['model']
            checked_at = stamp()
            try:
                check = nest.read(ip, model, checked_at, local_status_opener())
            except (OSError, ValueError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
                check = nest.unknown(model, checked_at)
            result.update(firmware_check=check, firmware_peer_mac=check['device_id'], firmware=check['installed_version'])
            if check['device_id']:
                result['mac'] = check['device_id']
        return result
    finally:
        if client:
            try:
                client.disconnect(timeout=1)
            except Exception:
                pass


def read_cast_firmware(ip, expected):
    checked_at = stamp()
    try:
        check, model = cast_firmware.read(ip, expected, checked_at, local_status_opener(), read_xml)
    except (OSError, ValueError, ET.ParseError) as exc:
        if isinstance(exc, HTTPError):
            exc.close()
        check, model = cast_firmware.unknown(checked_at), None
    return {'firmware_check': check, 'firmware_model': model, 'firmware_peer_mac': check['device_id'],
            'firmware': check['installed_version']}


def select_reader(device):
    protocol = device.get('protocol', '')
    if protocol.startswith('shelly') or (device.get('info') or {}).get('type', '').startswith('SH'):
        return None
    if protocol in ('roku-ecp','sonos-upnp','upnp-av','google-cast'):
        return protocol
    identity = ' '.join(str(device.get(k) or '') for k in ('model','advertised_model','hostname','name'))
    identity += ' ' + ' '.join(str(n.get('name','')) for n in device.get('advertised_names', []))
    identity = identity.lower()
    if 'sonos' in identity:
        return 'sonos-upnp'
    if 'roku' in identity:
        return 'roku-ecp'
    if 'wiim' in identity:
        return 'upnp-av'
    if any(word in identity for word in ('chromecast','eureka dongle','google nest','google-nest','google-home','riva festival','riva-festival')):
        return 'google-cast'
    return None


def read(device):
    ip = device['ip']
    if not private_ip(ip):
        raise ValueError('Only private device addresses are supported.')
    reader = select_reader(device)
    if reader == 'roku-ecp':
        return roku(ip)
    if reader == 'sonos-upnp':
        return upnp(ip, device.get('upnp_description_url') or f'http://{ip}:1400/xml/device_description.xml', sonos=True)
    if reader == 'upnp-av':
        return upnp(ip, device.get('upnp_description_url') or f'http://{ip}:49152/description.xml')
    if reader == 'google-cast':
        return cast(ip, device)
    return None
