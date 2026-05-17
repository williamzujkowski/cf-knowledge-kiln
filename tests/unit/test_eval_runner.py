"""Unit tests for the eval runner's argument validation (#31)."""

from __future__ import annotations

import asyncio

import pytest

from cf_knowledge_kiln.eval.runner import run_eval


class _DummyRetriever:
    """The validation we care about fires before any retriever call.

    A stub is enough; if the runner ever reaches this object the test
    failed (and pytest will surface the AttributeError clearly).
    """


def test_rejects_empty_k_values() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(run_eval(_DummyRetriever(), [], k_values=()))  # type: ignore[arg-type]


def test_rejects_non_positive_k_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(run_eval(_DummyRetriever(), [], k_values=(0, 5)))  # type: ignore[arg-type]


def test_rejects_negative_k_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(run_eval(_DummyRetriever(), [], k_values=(-1, 3)))  # type: ignore[arg-type]
