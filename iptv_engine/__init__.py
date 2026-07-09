"""
iptv_engine — the Live TV surface (see service.py for the full story).

A read-only, additive engine over the iptv-org public index of free-to-air
broadcast streams. Exports the router (browse/search/detail + the signed
/iptv_proxy relay) and the per-replica catalogue service that api.py's
scheduler keeps warm.
"""

from .routes import router, service
from .service import IptvService, enabled

__all__ = ["router", "service", "IptvService", "enabled"]
