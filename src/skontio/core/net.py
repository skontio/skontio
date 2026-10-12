"""Client address behind a reverse proxy (Caddy on TrueNAS).

``X-Forwarded-For`` is only honoured when the direct peer is a trusted proxy; the client is
the rightmost address in the chain that is not itself a trusted proxy.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from django.conf import settings

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def ip_in(ip: IPAddress, networks: Iterable[str]) -> bool:
    return any(ip in ipaddress.ip_network(net, strict=False) for net in networks)


def client_ip(request) -> IPAddress:
    peer = ipaddress.ip_address(request.META.get("REMOTE_ADDR", "0.0.0.0"))  # noqa: S104 - parse fallback only
    trusted = settings.TRUSTED_PROXIES
    if not trusted or not ip_in(peer, trusted):
        return peer
    chain = [part.strip() for part in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if part.strip()]
    for candidate in reversed(chain):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            return peer  # malformed header: fall back to the proxy itself (never trust garbage)
        if not ip_in(address, trusted):
            return address
    return peer
