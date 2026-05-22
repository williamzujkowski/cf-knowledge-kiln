"""Ingestion-run result type, shared across the pipeline (#169).

:class:`IngestionSummary` is produced by
:func:`cf_knowledge_kiln.ingestion.pipeline.run_source` and threaded
through the per-file processing helpers in
:mod:`cf_knowledge_kiln.ingestion._file_processing`. It lives in its own
module so both can import it without a circular dependency — the import
graph is ``_summary`` ← ``_file_processing`` ← ``pipeline``.

``pipeline`` re-exports :class:`IngestionSummary`, so external callers
keep importing it from ``cf_knowledge_kiln.ingestion.pipeline``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID


@dataclass
class IngestionSummary:
    """Per-source ingestion result. Mirrors `ingestion_runs.stats`.

    ``run_id`` is set after :func:`run_source` writes the initial
    ``ingestion_runs`` row, so a caller (e.g. the worker) can link
    a downstream ``ingestion_jobs.mark_done`` to the exact run that
    produced the work — important for the crash-recovery sweep
    described in issue #47.
    """

    files_scanned: int = 0
    files_indexed: int = 0
    files_skipped: int = 0
    chunks_created: int = 0
    chunks_unchanged: int = 0
    chunks_with_prompt_injection: int = 0
    chunks_with_sensitive_content: int = 0
    embeddings_created: int = 0
    embeddings_unchanged: int = 0
    embeddings_failed: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)
    run_id: UUID | None = None

    def as_stats(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "files_indexed": self.files_indexed,
            "files_skipped": self.files_skipped,
            "chunks_created": self.chunks_created,
            "chunks_unchanged": self.chunks_unchanged,
            "chunks_with_prompt_injection": self.chunks_with_prompt_injection,
            "chunks_with_sensitive_content": self.chunks_with_sensitive_content,
            "embeddings_created": self.embeddings_created,
            "embeddings_unchanged": self.embeddings_unchanged,
            "embeddings_failed": self.embeddings_failed,
            "skip_reasons": self.skip_reasons,
        }


def _bump(d: dict[str, int], key: str) -> None:
    d[key] = d.get(key, 0) + 1
