"""Tests for the SSRF guard used by the HTTP source connector (#27)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from cf_knowledge_kiln.ingestion.ssrf import (
    SsrfRefused,
    assert_addresses_public,
    assert_host_allowlisted,
)

# ─── assert_host_allowlisted ────────────────────────────────────────


class TestHostAllowlist:
    def test_accepts_allowlisted_https_host(self) -> None:
        assert_host_allowlisted(
            "docs.example.com",
            allowlist=["docs.example.com"],
            allow_http_hosts=[],
            scheme="https",
        )

    def test_rejects_unlisted_host(self) -> None:
        with pytest.raises(SsrfRefused, match="not in source host_allowlist"):
            assert_host_allowlisted(
                "evil.example.com",
                allowlist=["docs.example.com"],
                allow_http_hosts=[],
                scheme="https",
            )

    def test_rejects_http_for_non_opted_in_host(self) -> None:
        with pytest.raises(SsrfRefused, match="http://"):
            assert_host_allowlisted(
                "docs.example.com",
                allowlist=["docs.example.com"],
                allow_http_hosts=[],
                scheme="http",
            )

    def test_accepts_http_for_opted_in_host(self) -> None:
        assert_host_allowlisted(
            "legacy.example.com",
            allowlist=["legacy.example.com"],
            allow_http_hosts=["legacy.example.com"],
            scheme="http",
        )

    def test_rejects_unsupported_schemes(self) -> None:
        for scheme in ("ftp", "gopher", "file", "javascript", "data"):
            with pytest.raises(SsrfRefused, match="unsupported scheme"):
                assert_host_allowlisted(
                    "docs.example.com",
                    allowlist=["docs.example.com"],
                    allow_http_hosts=["docs.example.com"],
                    scheme=scheme,
                )

    def test_case_insensitive_match(self) -> None:
        """Hosts are case-insensitive per DNS — allow MIXED.case input."""
        assert_host_allowlisted(
            "Docs.EXAMPLE.com",
            allowlist=["docs.example.com"],
            allow_http_hosts=[],
            scheme="https",
        )

    def test_rejects_empty_host(self) -> None:
        with pytest.raises(SsrfRefused, match="empty host"):
            assert_host_allowlisted("", allowlist=["x"], allow_http_hosts=[], scheme="https")


# ─── assert_addresses_public ────────────────────────────────────────


def _fake_resolve(addrs: list[str]):
    """Patch socket.getaddrinfo to return ``addrs``."""

    def fake(*_a, **_kw):  # type: ignore[no-untyped-def]
        return [(0, 0, 0, "", (a, 0)) for a in addrs]

    return patch("cf_knowledge_kiln.ingestion.ssrf.socket.getaddrinfo", side_effect=fake)


class TestAddressGuard:
    def test_accepts_public_v4(self) -> None:
        with _fake_resolve(["93.184.216.34"]):  # example.com's real IP
            assert assert_addresses_public("example.com") == ["93.184.216.34"]

    def test_accepts_public_v6(self) -> None:
        with _fake_resolve(["2606:2800:220:1:248:1893:25c8:1946"]):
            assert assert_addresses_public("example.com")

    @pytest.mark.parametrize(
        "ip",
        [
            # RFC1918
            "10.0.0.5",
            "10.255.255.254",
            "172.16.0.1",
            "172.31.255.254",
            "192.168.1.1",
            # Loopback
            "127.0.0.1",
            "127.255.255.254",
            # Link-local
            "169.254.1.1",
            # Cloud metadata (also link-local, but called out explicitly)
            "169.254.169.254",
            # Multicast
            "224.0.0.1",
            # Reserved
            "240.0.0.1",
            # Unspecified
            "0.0.0.0",  # noqa: S104 — string literal, not a bind
        ],
    )
    def test_refuses_non_public_v4(self, ip: str) -> None:
        with _fake_resolve([ip]), pytest.raises(SsrfRefused):
            assert_addresses_public("internal.local")

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",  # loopback
            "fe80::1",  # link-local
            "fc00::1",  # ULA / private
            "ff02::1",  # multicast
            "::",  # unspecified
        ],
    )
    def test_refuses_non_public_v6(self, ip: str) -> None:
        with _fake_resolve([ip]), pytest.raises(SsrfRefused):
            assert_addresses_public("internal.local")

    def test_refuses_if_ANY_resolved_ip_is_non_public(self) -> None:
        """DNS rebinding / multi-A defense: one bad IP fails the whole check."""
        with (
            _fake_resolve(["1.1.1.1", "10.0.0.5"]),
            pytest.raises(SsrfRefused, match=r"10\.0\.0\.5"),
        ):
            assert_addresses_public("mixed.example.com")

    def test_metadata_ip_called_out_by_name(self) -> None:
        """169.254.169.254 → message mentions the explicit cloud-metadata phrasing."""
        with (
            _fake_resolve(["169.254.169.254"]),
            pytest.raises(SsrfRefused, match="cloud-metadata"),
        ):
            assert_addresses_public("metadata.example.com")

    @pytest.mark.parametrize(
        "ip",
        [
            "2002::1",  # 6to4 prefix; embedded IPv4 in low 32 bits
            "2002:7f00:0001::",  # 6to4 of 127.0.0.1 — direct loopback proxy
            "2002:0a00:0001::",  # 6to4 of 10.0.0.1 — RFC1918 proxy
            "64:ff9b::a00:1",  # NAT64 well-known prefix
        ],
    )
    def test_refuses_ipv6_transition_ranges_6to4_and_nat64(self, ip: str) -> None:
        """HIGH (PR #80 review): Python's stock checks don't cover 2002::/16.

        6to4 addresses embed an IPv4 in the low 32 bits. An attacker
        who can craft a DNS record pointing to 2002:7f00:1:: routes
        to 127.0.0.1 via a 6to4 relay — a real SSRF bypass.
        2002::/16 passes is_private + is_reserved + is_global=True,
        so we explicitly reject the range in _REFUSED_IPV6_RANGES.
        (NAT64 64:ff9b::/96 happens to also be is_reserved=True, so
        it's caught by the general public-IP check; we include it
        in the test for documentation regardless.)
        """
        with _fake_resolve([ip]), pytest.raises(SsrfRefused):
            assert_addresses_public("attacker.example.com")

    @pytest.mark.parametrize(
        "ip",
        [
            "2002::1",  # 6to4 prefix
            "2002:7f00:0001::",  # 6to4 of 127.0.0.1
        ],
    )
    def test_refuses_6to4_with_transition_range_message(self, ip: str) -> None:
        """The 6to4 message MUST cite "transition range" so an audit log makes intent clear."""
        with _fake_resolve([ip]), pytest.raises(SsrfRefused, match="transition range"):
            assert_addresses_public("attacker.example.com")

    @pytest.mark.parametrize(
        "literal_ip_in_allowlist_match",
        [
            "127.1",  # shorthand
            "0x7f.0x0.0x0.0x1",  # hex octets
            "2130706433",  # decimal IPv4
        ],
    )
    def test_encoding_bypasses_blocked_by_allowlist_string_match(
        self, literal_ip_in_allowlist_match: str
    ) -> None:
        """LOW (PR #80 review): non-standard IP encodings are blocked.

        Even if getaddrinfo resolves "127.1" → 127.0.0.1, the literal
        string never matches an allowlist of real hostnames. Document
        + lock down by test so a future refactor can't drop the
        pre-DNS string compare.
        """
        with pytest.raises(SsrfRefused, match="not in source host_allowlist"):
            assert_host_allowlisted(
                literal_ip_in_allowlist_match,
                allowlist=["docs.example.com"],
                allow_http_hosts=[],
                scheme="https",
            )

    def test_dns_failure_is_refused(self) -> None:
        import socket as socket_mod

        with (
            patch(
                "cf_knowledge_kiln.ingestion.ssrf.socket.getaddrinfo",
                side_effect=socket_mod.gaierror("nope"),
            ),
            pytest.raises(SsrfRefused, match="DNS resolution failed"),
        ):
            assert_addresses_public("nonexistent.example")


# ─── pin_dns (#81 TOCTOU mitigation) ────────────────────────────────


class TestPinDns:
    def test_pinned_lookup_returns_only_pinned_ips(self) -> None:
        """Inside the with block, getaddrinfo(host) returns ONLY pinned IPs."""
        import socket as socket_mod

        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        with pin_dns("safe.example.com", ["93.184.216.34"]):
            results = socket_mod.getaddrinfo("safe.example.com", 443)
            assert any(r[4][0] == "93.184.216.34" for r in results)

    def test_other_hosts_resolve_normally_inside_pin(self) -> None:
        """The pin is keyed on hostname; unrelated hosts pass through."""
        import socket as socket_mod

        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        captured: dict[str, str | bytes | None] = {}
        original = socket_mod.getaddrinfo

        def fake(host, port, *args, **kwargs):  # type: ignore[no-untyped-def]
            captured["host"] = host
            return [(socket_mod.AF_INET, socket_mod.SOCK_STREAM, 0, "", ("1.1.1.1", port))]

        socket_mod.getaddrinfo = fake  # type: ignore[assignment]
        try:
            with pin_dns("pinned.example.com", ["93.184.216.34"]):
                socket_mod.getaddrinfo("other.example.com", 80)
            assert captured["host"] == "other.example.com"
        finally:
            socket_mod.getaddrinfo = original  # type: ignore[assignment]

    def test_resolver_restored_after_with_block(self) -> None:
        import socket as socket_mod

        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        original = socket_mod.getaddrinfo
        with pin_dns("x.example.com", ["1.1.1.1"]):
            assert socket_mod.getaddrinfo is not original
        assert socket_mod.getaddrinfo is original

    def test_resolver_restored_even_when_inner_raises(self) -> None:
        import socket as socket_mod

        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        original = socket_mod.getaddrinfo
        with (
            pytest.raises(RuntimeError, match="boom"),
            pin_dns("x.example.com", ["1.1.1.1"]),
        ):
            raise RuntimeError("boom")
        assert socket_mod.getaddrinfo is original

    def test_empty_ip_list_refuses(self) -> None:
        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        with pytest.raises(SsrfRefused, match="empty IP list"), pin_dns("x.example.com", []):
            pass

    def test_pinned_lookup_supports_ipv6(self) -> None:
        import socket as socket_mod

        from cf_knowledge_kiln.ingestion.ssrf import pin_dns

        with pin_dns("safe.example.com", ["2606:2800:220:1:248:1893:25c8:1946"]):
            results = socket_mod.getaddrinfo("safe.example.com", 443)
            assert results[0][0] == socket_mod.AF_INET6
            # IPv6 sockaddr is a 4-tuple (host, port, flowinfo, scopeid).
            assert len(results[0][4]) == 4
