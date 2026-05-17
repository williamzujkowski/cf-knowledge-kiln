"""SSRF guard for the HTTP source connector (Phase 7, issue #27).

The single public entry point is :func:`assert_host_safe(host,
*, allowlist, allow_http_hosts, scheme)`. It enforces:

* scheme is ``https`` unless the host is in ``allow_http_hosts``
* host is one of the explicitly-allowlisted names
* every IP the host resolves to is publicly-routable
  (rejecting RFC1918 / link-local / loopback / multicast / metadata-
  service ranges)

Two things to know about this design:

1. The check is split into ``assert_host_allowlisted`` (cheap, no
   DNS) and ``assert_addresses_public`` (does DNS). The connector
   calls the cheap one BEFORE the request, then the IP check after
   the address is resolved (and again after every redirect). That
   sequence is what blocks "DNS rebinding" attempts — the host
   stays allowlisted but the resolved IP must still be public on
   each fetch.
2. We also block 169.254.169.254 explicitly even though it's covered
   by the link-local /16. AWS / GCP / Azure metadata services live
   there; calling them out by name makes the intent clear at audit.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable


class SsrfRefused(ValueError):
    """Raised when the SSRF guard refuses to fetch."""


def assert_host_allowlisted(
    host: str,
    *,
    allowlist: Iterable[str],
    allow_http_hosts: Iterable[str],
    scheme: str,
) -> None:
    """Reject hosts not on the allowlist, or http for hosts not opted in."""
    if not host:
        raise SsrfRefused("empty host")
    allowed = {h.lower() for h in allowlist}
    http_ok = {h.lower() for h in allow_http_hosts}
    h = host.lower()
    if h not in allowed:
        raise SsrfRefused(f"host {host!r} not in source host_allowlist")
    if scheme.lower() == "http" and h not in http_ok:
        raise SsrfRefused(
            f"http:// is refused for host {host!r}; use https or add it to allow_http_hosts"
        )
    if scheme.lower() not in ("http", "https"):
        raise SsrfRefused(f"unsupported scheme {scheme!r}; only http/https allowed")


def assert_addresses_public(host: str) -> list[str]:
    """Resolve ``host`` and refuse if ANY address is non-public.

    Returns the list of resolved IP strings (for logging) when safe.
    """
    addrs = _resolve(host)
    if not addrs:
        raise SsrfRefused(f"host {host!r} did not resolve to any address")
    for ip_str in addrs:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError as exc:  # pragma: no cover - defensive
            raise SsrfRefused(f"could not parse resolved IP {ip_str!r}") from exc
        _assert_ip_public(ip, host=host)
    return addrs


# IPv6 transition ranges that Python's ipaddress library does NOT
# mark via is_private / is_reserved / is_loopback / is_link_local,
# but which we still refuse:
#
#   2002::/16 — 6to4. The low 32 bits are an embedded IPv4 address,
#     so 2002:7f00:1::/48 routes to (the 6to4 anycast relay for) IPv4
#     127.0.0.1 — a real SSRF bypass. Python returns is_global=True
#     for the whole /16, so we MUST list it explicitly here.
#   64:ff9b::/96 — NAT64. Embedded IPv4 in low 32 bits. Similar story.
#
# Anything else we want to deny defensively goes on this list. Reviewed
# on each Phase 7+ change.
_REFUSED_IPV6_RANGES: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("2002::/16"),  # 6to4
    ipaddress.IPv6Network("64:ff9b::/96"),  # NAT64 well-known prefix
)


def _assert_ip_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, *, host: str) -> None:
    """Defense-in-depth IP checks. Includes the cloud metadata IP by name.

    Check order matters: cloud-metadata literal first (most specific
    message), IPv6 transition ranges second (so 6to4 gets the
    "transition range" message regardless of whether the running
    Python flags them via is_reserved), then the generic is_* fence.
    """
    # Cloud metadata service. Already covered by is_link_local but
    # called out so an auditor reading this can see we mean it.
    if str(ip) == "169.254.169.254":
        raise SsrfRefused(f"host {host!r} resolves to the cloud-metadata IP 169.254.169.254")
    # IPv6 transition ranges that route to IPv4 via embedded addresses.
    # Checked BEFORE is_* so the specific "transition range" message
    # always wins — Python's ipaddress flags for 2002::/16 changed
    # between 3.12.3 (False) and 3.12.13 (True for is_reserved); we
    # don't want the test to depend on Python's patch version.
    if isinstance(ip, ipaddress.IPv6Address):
        for net in _REFUSED_IPV6_RANGES:
            if ip in net:
                raise SsrfRefused(
                    f"host {host!r} resolves to IPv6 transition range {net} "
                    f"(IP {ip}); refuse — it can tunnel to private IPv4."
                )
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        raise SsrfRefused(
            f"host {host!r} resolves to non-public IP {ip} "
            f"(private/loopback/link-local/multicast/reserved/unspecified)"
        )


def _resolve(host: str) -> list[str]:
    """Resolve ``host`` to a list of IP strings (v4 + v6 both)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfRefused(f"DNS resolution failed for {host!r}: {exc}") from exc
    out: list[str] = []
    seen: set[str] = set()
    for info in infos:
        raw = info[4][0]
        # getaddrinfo returns sockaddr; index 0 is str for AF_INET/INET6.
        addr = str(raw)
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


__all__ = [
    "SsrfRefused",
    "assert_addresses_public",
    "assert_host_allowlisted",
]
