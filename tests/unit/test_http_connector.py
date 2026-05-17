"""Tests for the HTTP source connector + HttpSource schema (#27)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from cf_knowledge_kiln.ingestion.connectors import (
    FetchResult,
    HttpConnector,
    IngestionCapExceeded,
    IngestionCaps,
    SkippedFile,
)
from cf_knowledge_kiln.ingestion.sources import HttpSource

# ─── Schema ─────────────────────────────────────────────────────────


class TestHttpSourceSchema:
    def test_minimum_valid_source(self) -> None:
        s = HttpSource(
            name="docs",
            type="http",
            urls=["https://docs.example.com/a.md"],
            host_allowlist=["docs.example.com"],
        )
        assert s.allow_http_hosts == []

    def test_requires_at_least_one_url(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HttpSource(name="x", type="http", urls=[], host_allowlist=["x.example.com"])

    def test_requires_at_least_one_allowlisted_host(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HttpSource(
                name="x", type="http", urls=["https://x.example.com/a.md"], host_allowlist=[]
            )

    def test_extras_forbidden(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            HttpSource(  # type: ignore[call-arg]
                name="x",
                type="http",
                urls=["https://x.example.com/a.md"],
                host_allowlist=["x.example.com"],
                surprise=1,
            )


# ─── Connector fetch behavior ──────────────────────────────────────


def _caps() -> IngestionCaps:
    return IngestionCaps(max_file_bytes=1_048_576, max_files=10, max_repo_bytes=10_000_000)


def _src(**kwargs: object) -> HttpSource:
    defaults = {
        "name": "tests",
        "type": "http",
        "urls": ["https://docs.example.com/a.md"],
        "host_allowlist": ["docs.example.com"],
    }
    defaults.update(kwargs)
    return HttpSource.model_validate(defaults)


def _public_dns():
    """Patch DNS resolution to a public IP so the SSRF guard passes."""
    return patch(
        "cf_knowledge_kiln.ingestion.ssrf.socket.getaddrinfo",
        side_effect=lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 0))],
    )


def _mock_response(status_code: int = 200, content: bytes = b"# Doc\nbody.", headers=None):  # type: ignore[no-untyped-def]
    return httpx.Response(
        status_code=status_code,
        content=content,
        headers=headers or {},
        request=httpx.Request("GET", "https://docs.example.com/a.md"),
    )


class TestHttpConnectorHappyPath:
    def test_fetches_allowlisted_https_url(self) -> None:
        source = _src()
        with _public_dns(), patch.object(httpx.Client, "get", return_value=_mock_response()):
            result = HttpConnector(_caps()).fetch(source)
        assert isinstance(result, FetchResult)
        assert len(result.files) == 1
        assert result.files[0].content == b"# Doc\nbody."
        assert result.files[0].path == "docs.example.com/a.md"


class TestSsrfBlocks:
    def test_refuses_unlisted_host_before_dns(self) -> None:
        source = _src(urls=["https://evil.example.com/a.md"], host_allowlist=["docs.example.com"])
        with _public_dns(), patch.object(httpx.Client, "get") as get:
            result = HttpConnector(_caps()).fetch(source)
        # No HTTP call should have been made — guard fires before .get().
        get.assert_not_called()
        assert result.files == []
        assert len(result.skipped) == 1
        assert "not in source host_allowlist" in (result.skipped[0].detail or "")

    def test_refuses_http_scheme_when_not_opted_in(self) -> None:
        source = _src(urls=["http://docs.example.com/a.md"])
        with _public_dns(), patch.object(httpx.Client, "get") as get:
            result = HttpConnector(_caps()).fetch(source)
        get.assert_not_called()
        assert len(result.skipped) == 1
        assert "http://" in (result.skipped[0].detail or "")

    def test_accepts_http_for_opted_in_host(self) -> None:
        source = _src(
            urls=["http://legacy.example.com/a.md"],
            host_allowlist=["legacy.example.com"],
            allow_http_hosts=["legacy.example.com"],
        )
        with _public_dns(), patch.object(httpx.Client, "get", return_value=_mock_response()):
            result = HttpConnector(_caps()).fetch(source)
        assert len(result.files) == 1

    def test_refuses_when_host_resolves_to_private_ip(self) -> None:
        source = _src(
            urls=["https://internal.example.com/a.md"], host_allowlist=["internal.example.com"]
        )
        with (
            patch(
                "cf_knowledge_kiln.ingestion.ssrf.socket.getaddrinfo",
                side_effect=lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 0))],
            ),
            patch.object(httpx.Client, "get") as get,
        ):
            result = HttpConnector(_caps()).fetch(source)
        get.assert_not_called()
        assert len(result.skipped) == 1
        assert "non-public" in (result.skipped[0].detail or "")

    def test_refuses_redirect_to_internal_host(self) -> None:
        """A 302 → internal host must not bypass the guard.

        The connector re-runs assert_host_allowlisted + assert_addresses_public
        on every redirect hop. The redirect target's host isn't in the
        original source's allowlist, so the second guard call refuses.
        """
        source = _src()
        redirect = httpx.Response(
            status_code=302,
            content=b"",
            headers={"location": "https://internal.attacker.com/a.md"},
            request=httpx.Request("GET", "https://docs.example.com/a.md"),
        )
        with _public_dns(), patch.object(httpx.Client, "get", return_value=redirect):
            result = HttpConnector(_caps()).fetch(source)
        assert len(result.skipped) == 1
        assert "not in source host_allowlist" in (result.skipped[0].detail or "")


class TestSizeAndCount:
    def test_response_over_per_file_cap_is_skipped(self) -> None:
        source = _src()
        big = _mock_response(content=b"x" * 200)
        caps = IngestionCaps(max_file_bytes=100, max_files=10, max_repo_bytes=10_000)
        with _public_dns(), patch.object(httpx.Client, "get", return_value=big):
            result = HttpConnector(caps).fetch(source)
        assert result.files == []
        assert len(result.skipped) == 1
        assert result.skipped[0].reason == "too_large"

    def test_cumulative_size_cap_raises(self) -> None:
        source = _src(
            urls=[
                "https://docs.example.com/a.md",
                "https://docs.example.com/b.md",
            ]
        )
        # 100-byte body x 2 = 200 > 150 cap.
        caps = IngestionCaps(max_file_bytes=1_000, max_files=10, max_repo_bytes=150)
        body = _mock_response(content=b"x" * 100)
        with (
            _public_dns(),
            patch.object(httpx.Client, "get", return_value=body),
            pytest.raises(IngestionCapExceeded, match="exceeded total cap"),
        ):
            HttpConnector(caps).fetch(source)

    def test_file_count_cap_raises(self) -> None:
        source = _src(urls=[f"https://docs.example.com/{i}.md" for i in range(5)])
        caps = IngestionCaps(max_file_bytes=1_000, max_files=2, max_repo_bytes=10_000)
        body = _mock_response(content=b"x")
        with (
            _public_dns(),
            patch.object(httpx.Client, "get", return_value=body),
            pytest.raises(IngestionCapExceeded, match="file count cap"),
        ):
            HttpConnector(caps).fetch(source)


class TestHttpErrors:
    def test_4xx_is_skipped_not_raised(self) -> None:
        source = _src()
        with (
            _public_dns(),
            patch.object(httpx.Client, "get", return_value=_mock_response(status_code=404)),
        ):
            result = HttpConnector(_caps()).fetch(source)
        assert result.files == []
        assert isinstance(result.skipped[0], SkippedFile)
        assert "HTTP 404" in (result.skipped[0].detail or "")

    def test_network_error_is_skipped_not_raised(self) -> None:
        source = _src()
        with (
            _public_dns(),
            patch.object(httpx.Client, "get", side_effect=httpx.ConnectError("boom")),
        ):
            result = HttpConnector(_caps()).fetch(source)
        assert result.files == []
        assert "boom" in (result.skipped[0].detail or "")
