"""Source allowlist schema + loader (issue #13).

The ingestion worker refuses to fetch any source that isn't listed in
``config/sources.yaml``. This module is the single read-time gate:
:class:`SourceAllowlist` loads the YAML, validates the schema, and
hands out :class:`GitSource` / :class:`LocalSource` records by name.
Callers that try to look up an unknown name get a
:class:`SourceNotAllowedError`.

The schema is the public contract documented in
``docs/data-sources.md``. Pydantic's ``extra="forbid"`` rejects unknown
fields so typos surface at load time, not silently at runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Status = Literal["active", "inactive"]
Authority = Literal["standard", "reference", "informational"]
Sensitivity = Literal["public", "internal", "restricted"]


class _SourceBase(BaseModel):
    """Fields common to every source type."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    status: Status = "active"
    authority: Authority = "standard"
    default_owner: str | None = None
    default_sensitivity: Sensitivity = "internal"
    last_reviewed_required: bool = False


_REPO_PATTERN = (
    r"^(?:[A-Za-z0-9_./:@+][A-Za-z0-9_./:@+-]*"
    r"|https?://[^\s]+"
    r"|git@[^\s]+"
    r"|file://[^\s]+)$"
)
_BRANCH_PATTERN = r"^[A-Za-z0-9._/][A-Za-z0-9._/-]*$"


class GitSource(_SourceBase):
    """A git-hosted source. ``repo`` is ``owner/name`` shorthand or a full URL.

    Both ``repo`` and ``branch`` are constrained so they cannot start with
    ``-`` (which the ``git`` CLI would parse as an option), closing the
    obvious arg-injection door even though the connector passes args as
    a list, not a shell string.
    """

    type: Literal["git"]
    repo: str = Field(min_length=1, pattern=_REPO_PATTERN)
    branch: str = Field(default="main", pattern=_BRANCH_PATTERN)


class LocalSource(_SourceBase):
    """A local-filesystem source. ``path`` is the directory to walk."""

    type: Literal["local"]
    path: str = Field(min_length=1)


class HttpSource(_SourceBase):
    """An HTTP-hosted source — fetch a list of documents over HTTPS.

    Phase 7 (#27) addition. Each URL is fetched with an SSRF guard that
    refuses RFC1918 / link-local / loopback / metadata-service IPs;
    the ``host_allowlist`` is enforced both before DNS resolution AND
    after redirect chains so a 302 to an internal host cannot smuggle
    a request past the guard.

    The connector limits per-response size via the same
    :class:`IngestionCaps.max_file_bytes` knob used by local + git
    sources, and refuses any response > the cap rather than partially
    indexing.

    URLs MUST use the ``https`` scheme by default; ``http`` is
    rejected unless the host is explicitly allowed via
    ``allow_http_hosts``. The default is `[]` — operators must opt
    each plain-HTTP host in by name.
    """

    type: Literal["http"]
    urls: list[str] = Field(min_length=1)
    host_allowlist: list[str] = Field(min_length=1)
    allow_http_hosts: list[str] = Field(default_factory=list)


Source = GitSource | LocalSource | HttpSource


class _Registry(BaseModel):
    """Top-level shape of ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    sources: list[Source]


class SourceAllowlistError(ValueError):
    """Raised when the allowlist file is missing, malformed, or invalid."""


class SourceNotAllowedError(ValueError):
    """Raised when an ingestion request names a source that isn't allowlisted."""


class SourceAllowlist:
    """Frozen view of the allowlisted sources.

    Use :meth:`from_yaml` to load. The instance is iterable, sized,
    and looks up sources by name. Inactive sources are returned by
    :meth:`get` and iteration but excluded from :meth:`active`.
    """

    def __init__(self, sources: list[Source]) -> None:
        seen: set[str] = set()
        for s in sources:
            if s.name in seen:
                raise SourceAllowlistError(f"duplicate source name {s.name!r} in allowlist")
            seen.add(s.name)
        self._sources: list[Source] = list(sources)
        self._by_name: dict[str, Source] = {s.name: s for s in sources}

    @classmethod
    def from_yaml(cls, path: Path | str) -> SourceAllowlist:
        """Parse ``path`` and validate. Raises :class:`SourceAllowlistError`."""
        p = Path(path)
        if not p.exists():
            raise SourceAllowlistError(f"source allowlist file not found: {p}")
        try:
            raw = yaml.safe_load(p.read_text())
        except yaml.YAMLError as exc:
            raise SourceAllowlistError(f"invalid YAML in {p}: {exc}") from exc
        if not isinstance(raw, dict) or "sources" not in raw:
            raise SourceAllowlistError(f"{p} must be a mapping with a top-level 'sources' key")
        try:
            registry = _Registry.model_validate(raw)
        except ValidationError as exc:
            raise SourceAllowlistError(f"invalid source allowlist in {p}: {exc}") from exc
        return cls(registry.sources)

    def __len__(self) -> int:
        return len(self._sources)

    def __iter__(self) -> Iterator[Source]:
        return iter(self._sources)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._by_name

    def get(self, name: str) -> Source:
        """Return the source by ``name`` or raise :class:`SourceNotAllowedError`."""
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise SourceNotAllowedError(
                f"source {name!r} is not in the allowlist; "
                f"add it to config/sources.yaml to enable ingestion"
            ) from exc

    def active(self) -> Iterator[Source]:
        """Iterate only over sources with ``status == 'active'``."""
        return (s for s in self._sources if s.status == "active")
