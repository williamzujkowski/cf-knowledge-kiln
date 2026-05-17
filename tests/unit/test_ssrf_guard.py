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
