"""HTTP source connector (Phase 7, issue #27).

Split out of ``connectors.py`` to keep that file under the
≤400-line code budget. Kept module-private (``_http_connector``)
because the public surface is :class:`HttpConnector` re-exported
from ``connectors``.

The connector follows redirects manually (max 5 hops) so the SSRF
guard re-runs on every hop. See :mod:`cf_knowledge_kiln.ingestion.ssrf`
for the host-allowlist + IP-range checks.

KNOWN LIMITATIONS:

* TOCTOU window between :func:`assert_addresses_public` (our DNS
  lookup) and the connect inside ``httpx.Client.get`` (httpx's own
  lookup). An attacker who controls authoritative DNS with a
  near-zero TTL can return a public IP first + a private IP
  second. Mitigation requires a custom transport with IP pinning;
  filed as a follow-up issue. The current code rejects the obvious
  multi-A case but cannot stop a same-host rebind.
* HTTP response bodies are auto-decompressed by httpx (gzip/deflate)
  before the ``max_file_bytes`` check, so a gzip bomb expands in
  memory before being refused. The cap still applies post-decode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from cf_knowledge_kiln.ingestion.ssrf import (
    SsrfRefused,
    assert_addresses_public,
    assert_host_allowlisted,
    pin_dns,
)

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.connectors import (
        FetchedFile,
        FetchResult,
        IngestionCaps,
        SkippedFile,
    )
    from cf_knowledge_kiln.ingestion.sources import HttpSource


class HttpConnector:
    """Fetch each URL in :class:`HttpSource.urls` over HTTPS."""

    _TIMEOUT_SECONDS: Final[float] = 20.0
    _MAX_REDIRECTS: Final[int] = 5

    def __init__(self, caps: IngestionCaps) -> None:
        self._caps = caps

    def fetch(self, source: HttpSource) -> FetchResult:
        import httpx

        # Lazy import to break the cycle with connectors.py.
        from cf_knowledge_kiln.ingestion.connectors import (
            FetchResult,
            IngestionCapExceeded,
            SkippedFile,
        )

        result = FetchResult()
        total_bytes = 0
        with httpx.Client(
            timeout=self._TIMEOUT_SECONDS,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4),
            headers={"User-Agent": "cf-knowledge-kiln/1.0 (+ingestion)"},
        ) as client:
            for url in source.urls:
                outcome = self._fetch_one(client, url, source)
                if isinstance(outcome, SkippedFile):
                    result.skipped.append(outcome)
                    continue
                fetched, size = outcome
                total_bytes += size
                if total_bytes > self._caps.max_repo_bytes:
                    raise IngestionCapExceeded(
                        f"http source {source.name!r} exceeded total cap "
                        f"({total_bytes} > {self._caps.max_repo_bytes} bytes)"
                    )
                if len(result.files) >= self._caps.max_files:
                    raise IngestionCapExceeded(
                        f"http source {source.name!r} exceeded file count cap "
                        f"({len(result.files)} >= {self._caps.max_files})"
                    )
                result.files.append(fetched)
        return result

    def _fetch_one(
        self, client: object, url: str, source: HttpSource
    ) -> SkippedFile | tuple[FetchedFile, int]:
        """Fetch a single URL, following redirects (with SSRF re-guard each hop)."""
        import httpx

        from cf_knowledge_kiln.ingestion.connectors import SkippedFile

        try:
            current: object = httpx.URL(url)
            for _ in range(self._MAX_REDIRECTS):
                outcome = self._fetch_hop(client, current, source)
                if isinstance(outcome, httpx.URL):
                    current = outcome  # follow this redirect
                    continue
                # outcome is SkippedFile | tuple[FetchedFile, int]
                return outcome  # type: ignore[return-value]
            return SkippedFile(path=url, reason="excluded_by_pattern", detail="too many redirects")
        except SsrfRefused as exc:
            return SkippedFile(path=url, reason="excluded_by_pattern", detail=str(exc))
        except httpx.HTTPError as exc:
            return SkippedFile(path=url, reason="excluded_by_pattern", detail=str(exc))

    def _fetch_hop(self, client: object, current: object, source: HttpSource) -> object:
        """One redirect-hop. Returns httpx.URL (redirect), SkippedFile, or (FetchedFile, int)."""
        from cf_knowledge_kiln.ingestion.connectors import FetchedFile, SkippedFile

        # Issue #81 — pin DNS for the duration of the connect so an
        # attacker who controls the upstream nameserver can't rebind
        # the hostname to a private IP between our SSRF check and
        # httpx's own getaddrinfo call. pinned_ips is the list our
        # SSRF guard already verified as public.
        pinned_ips = _guard_url(current, source)
        host = getattr(current, "host", "") or ""
        with pin_dns(host, pinned_ips):
            response = client.get(str(current))  # type: ignore[attr-defined]
        if response.is_redirect:
            return _next_redirect(response, current, _source=source)
        if response.status_code >= 400:
            return SkippedFile(
                path=str(current),
                reason="excluded_by_pattern",
                detail=f"HTTP {response.status_code}",
            )
        body = response.content
        if len(body) > self._caps.max_file_bytes:
            return SkippedFile(
                path=str(current),
                reason="too_large",
                detail=f"{len(body)} > {self._caps.max_file_bytes} bytes",
            )
        host = getattr(current, "host", "")
        path = getattr(current, "path", "") or "/"
        rel = f"{host}{path}"
        return FetchedFile(path=rel, content=body, size_bytes=len(body), commit_sha=None), len(body)


def _next_redirect(
    response: object,
    current: object,
    _source: HttpSource,  # kept for symmetry with _fetch_hop / future per-source policy
) -> object:  # actually SkippedFile | httpx.URL; mypy widens to object
    """Compute the next URL for a redirect — or a SkippedFile if it's unsafe."""
    import httpx

    from cf_knowledge_kiln.ingestion.connectors import SkippedFile

    loc = response.headers.get("location")  # type: ignore[attr-defined]
    if not loc:
        return SkippedFile(
            path=str(current),
            reason="excluded_by_pattern",
            detail="redirect without Location header",
        )
    # Protocol-relative redirects (`//evil.com/x`) try to switch host
    # silently. Refuse rather than relying on the SSRF re-guard to
    # catch the substitution after the fact.
    if loc.startswith("//"):
        return SkippedFile(
            path=str(current),
            reason="excluded_by_pattern",
            detail=f"refused protocol-relative redirect to {loc!r}",
        )
    return httpx.URL(loc) if "://" in loc else current.copy_with(path=loc)  # type: ignore[attr-defined]


def _guard_url(url: object, source: HttpSource) -> list[str]:
    """Run the SSRF guard for the current hop's host. Returns the verified IPs.

    The IPs are then handed to :func:`pin_dns` so the subsequent
    ``httpx.Client.get`` connects to one of THESE addresses, closing
    the TOCTOU window (#81).
    """
    host = getattr(url, "host", None) or ""
    scheme = getattr(url, "scheme", None) or ""
    assert_host_allowlisted(
        host,
        allowlist=source.host_allowlist,
        allow_http_hosts=source.allow_http_hosts,
        scheme=scheme,
    )
    return assert_addresses_public(host)


__all__ = ["HttpConnector"]
