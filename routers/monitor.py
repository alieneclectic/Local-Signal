"""Shared router adapter contract, polling, and public snapshot."""
from collections.abc import Callable
from datetime import datetime, timezone
import ipaddress
from pathlib import Path
import time
from typing import Literal, NotRequired, Protocol, TypedDict


POLL_SECONDS = 60
Capability = Literal['upload_bps', 'download_bps', 'paused']


class Device(TypedDict):
    name: str
    ip: str | None
    upload_bps: NotRequired[float | None]
    download_bps: NotRequired[float | None]
    paused: NotRequired[bool | None]


class Reading(TypedDict):
    devices: list[Device]
    message: NotRequired[str | None]
    capabilities: NotRequired[tuple[Capability, ...]]


class Adapter(Protocol):
    NAME: str
    CAPABILITIES: tuple[Capability, ...]

    def load_config(self, database: Path) -> dict | None: ...
    def reader(self, config: dict) -> Callable[[], Reading]: ...
    def configure(self, database: Path, action: str) -> None: ...


class RouterError(Exception):
    """Expected failure with a message safe to show in the dashboard."""

    def __init__(self, message: str, *, retry_after=0,
                 status: Literal['unavailable', 'setup_required', 'unsupported'] = 'unavailable'):
        super().__init__(message)
        self.retry_after = retry_after
        self.status = status


def error_state(error):
    if isinstance(error, RouterError):
        return {'status': error.status, 'message': str(error)}
    return {'status': 'unavailable', 'message': 'The router adapter could not read monitoring data.'}


def public_device(device, capabilities):
    name = device.get('name')
    ip = device.get('ip')
    try:
        ip = str(ipaddress.ip_address(ip)) if isinstance(ip, str) and '%' not in ip else None
    except ValueError:
        ip = None
    row = {'name': name[:160] if isinstance(name, str) and name.strip() else ip or 'Unnamed device', 'ip': ip}
    for key in capabilities:
        value = device.get(key)
        if key == 'paused':
            row[key] = value if isinstance(value, bool) else None
        else:
            row[key] = value if type(value) in (float, int) and 0 <= value <= 2**63 else None
    return row


class Monitor:
    def __init__(self, database: Path, adapter_id: str, adapter: Adapter):
        self.database, self.adapter_id, self.adapter = database, adapter_id, adapter
        self.config = self.read = None
        self.next_poll = 0
        self.sample = ({'status': 'disabled'}, 0)

    def snapshot(self):
        data, collected = self.sample
        if data['status'] == 'ready' and time.monotonic() - collected > 2 * POLL_SECONDS:
            data = {'status': 'unavailable', 'message': 'Router readings are out of date.'}
        return {'id': self.adapter_id, 'name': self.adapter.NAME,
                'capabilities': self.adapter.CAPABILITIES, **data}

    def update(self):
        try:
            config = self.adapter.load_config(self.database)
        except Exception as exc:
            self.config = self.read = None
            self.sample = (error_state(exc), 0)
            return
        if config is None:
            self.config = self.read = None
            self.sample = ({'status': 'disabled'}, 0)
            return
        if config != self.config:
            self.config, self.read = config, None
            self.next_poll = 0
            self.sample = ({'status': 'starting'}, 0)
        if time.monotonic() < self.next_poll:
            return
        delay = POLL_SECONDS
        try:
            if self.read is None:
                self.read = self.adapter.reader(config)
            reading = self.read()
            reported = reading.get('capabilities', self.adapter.CAPABILITIES)
            capabilities = tuple(key for key in self.adapter.CAPABILITIES if key in reported)
            data = {'status': 'ready', 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                    'capabilities': capabilities, 'message': reading.get('message'),
                    'devices': [public_device(device, capabilities) for device in reading['devices']]}
        except Exception as exc:
            data = error_state(exc)
            if isinstance(exc, RouterError):
                delay = max(delay, exc.retry_after)
        completed = time.monotonic()
        try:
            still_configured = self.adapter.load_config(self.database) == config
        except Exception:
            still_configured = False
        if still_configured:
            self.sample = (data, completed)
            self.next_poll = completed + delay

    def run(self, stop):
        while not stop.is_set():
            self.update()
            stop.wait(5)
