"""Checks that Home Assistant handles tunnelled requests safely. Pure functions, no Home Assistant imports.

Tunnelled requests always arrive from 127.0.0.1, because the tunnel client runs on this host. Two
settings matter:

- trusted_networks: requests from a trusted network can skip the password. If loopback is in there,
  every remote visitor would look trusted.
- trusted_proxies: Home Assistant only reads X-Forwarded-For from these addresses. Without loopback
  here, logs and IP checks see 127.0.0.1 for every remote visitor.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

LOOPBACK_ADDRESSES = (ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1"))


def _as_network(value: object) -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    return ipaddress.ip_network(str(value), strict=False)


def covers_loopback(networks: Iterable[object]) -> bool:
    """True if any network contains IPv4 or IPv6 loopback."""
    parsed = [_as_network(n) for n in networks]
    return any(address in network for network in parsed for address in LOOPBACK_ADDRESSES)
