"""Unit tests for #205 — frontmatter field-name aliases.

`_resolve_doc_defaults` resolves several canonical column names from a
fixed alias map so common spelling variants (`last-verified`,
`type`, etc.) don't silently drop into null columns. These tests cover
the pure-logic resolver — the integration test in
``tests/integration/test_ingestion_pipeline.py`` covers the end-to-end
write to the documents table.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from cf_knowledge_kiln.ingestion._file_processing import (
    _FRONTMATTER_ALIASES,
    _first_present,
    _resolve_doc_defaults,
)
from cf_knowledge_kiln.ingestion.sources import LocalSource


def _src() -> LocalSource:
    # path is unused — these tests exercise the pure-logic resolver,
    # not the LocalSource fetch path. The dataclass requires a string.
    return LocalSource(
        name="aliases-test",
        type="local",
        path="/tmp",  # noqa: S108 — unused placeholder, not opened.
        include=["**/*.md"],
    )


class TestFirstPresent:
    def test_returns_first_non_none(self) -> None:
        assert _first_present({"a": None, "b": "value", "c": "other"}, ("a", "b", "c")) == "value"

    def test_treats_empty_string_as_missing(self) -> None:
        assert _first_present({"a": "", "b": "real"}, ("a", "b")) == "real"

    def test_returns_none_when_all_missing(self) -> None:
        assert _first_present({}, ("a", "b", "c")) is None

    def test_priority_order_wins(self) -> None:
        """When multiple aliases are present, the first key in the tuple wins."""
        assert _first_present({"a": "first", "b": "second"}, ("a", "b")) == "first"


class TestLastReviewedAliases:
    """#205 / homelab-iac: 'last-verified' silently dropped before this fix."""

    @pytest.mark.parametrize(
        "key",
        [
            "last_reviewed",
            "last-reviewed",
            "last_verified",
            "last-verified",
            "reviewed",
            "verified",
        ],
    )
    def test_each_alias_lands_in_last_reviewed(self, key: str) -> None:
        metadata: dict[str, Any] = {key: "2026-05-17"}
        defaults = _resolve_doc_defaults(metadata, _src())
        assert defaults["last_reviewed"] == date(2026, 5, 17)

    def test_canonical_wins_over_aliases_when_both_present(self) -> None:
        metadata = {"last_reviewed": "2026-05-17", "last-verified": "2024-01-01"}
        defaults = _resolve_doc_defaults(metadata, _src())
        assert defaults["last_reviewed"] == date(2026, 5, 17)

    def test_no_alias_present_is_null(self) -> None:
        defaults = _resolve_doc_defaults({}, _src())
        assert defaults["last_reviewed"] is None


class TestDocTypeAliases:
    """#205 / homelab-iac: 'type:' is the common spelling and silently
    dropped before this fix — `documents.doc_type` was null for every
    document in the homelab-iac corpus.
    """

    @pytest.mark.parametrize("key", ["doc_type", "doc-type", "type"])
    def test_each_alias_lands_in_doc_type(self, key: str) -> None:
        metadata: dict[str, Any] = {key: "component"}
        defaults = _resolve_doc_defaults(metadata, _src())
        assert defaults["doc_type"] == "component"

    def test_canonical_wins(self) -> None:
        metadata = {"doc_type": "runbook", "type": "component"}
        defaults = _resolve_doc_defaults(metadata, _src())
        assert defaults["doc_type"] == "runbook"

    def test_no_alias_present_is_null(self) -> None:
        defaults = _resolve_doc_defaults({}, _src())
        assert defaults["doc_type"] is None


class TestAliasMapShape:
    """Lock the alias map shape so additions are explicit, not silent."""

    def test_canonical_keys_are_in_their_own_alias_list(self) -> None:
        """The canonical name MUST appear in the alias tuple (first wins)."""
        for canonical, aliases in _FRONTMATTER_ALIASES.items():
            assert canonical in aliases, (
                f"{canonical!r} not in its own alias list — "
                f"could silently break setting via the canonical name"
            )
            assert aliases[0] == canonical, (
                f"{canonical!r} must come first in {aliases!r} so it wins ties"
            )
