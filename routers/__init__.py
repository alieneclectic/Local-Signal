"""Registry of opt-in router monitoring adapters."""
from .adapters import google_wifi
from .monitor import Adapter, Monitor, RouterError


ADAPTERS: dict[str, Adapter] = {'google-wifi': google_wifi}
