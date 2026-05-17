"""Hybrid retrieval (Phase 5).

Public surface:

* :class:`~cf_knowledge_kiln.retrieval.types.RetrievalFilters` /
  :class:`~cf_knowledge_kiln.retrieval.types.Warning` /
  :class:`~cf_knowledge_kiln.retrieval.types.Conflict` — Pydantic
  models matching the hand-authored OpenAPI contract.
* :class:`~cf_knowledge_kiln.retrieval.config.RetrievalConfig` and
  :func:`~cf_knowledge_kiln.retrieval.config.load_retrieval_config`
  — read status_weights + stale_after_days from
  ``config/security.yaml``.
* :func:`~cf_knowledge_kiln.retrieval.filters.build_predicates` —
  translate :class:`RetrievalFilters` to a list of SQLAlchemy WHERE
  fragments.
* :mod:`~cf_knowledge_kiln.retrieval.ranking` — :class:`RankedChunk`,
  RRF fusion, boost application, warning emission, conflict detection,
  and the single :func:`requires_human_review` decision function.
* :class:`~cf_knowledge_kiln.retrieval.engine.HybridRetriever` (slice
  2) — DB-touching engine that wires the CTE in
  :class:`ChunksRepository.hybrid_search` to ranking + warnings.
"""

from cf_knowledge_kiln.retrieval.config import (
    DEFAULT_STALE_AFTER_DAYS,
    DEFAULT_STATUS_WEIGHTS,
    RetrievalConfig,
    RetrievalConfigError,
    load_retrieval_config,
)
from cf_knowledge_kiln.retrieval.engine import (
    EmbeddingProvider,
    HybridRetriever,
    SearchResult,
)
from cf_knowledge_kiln.retrieval.filters import build_predicates
from cf_knowledge_kiln.retrieval.ranking import (
    DEFAULT_RRF_K,
    WEAK_EVIDENCE_SCORE_THRESHOLD,
    RankedChunk,
    apply_boosts,
    deprecated_warnings,
    detect_conflicts,
    prompt_injection_warnings,
    requires_human_review,
    rrf_fuse,
    stale_warnings,
    weak_evidence_warning,
)
from cf_knowledge_kiln.retrieval.types import (
    Confidence,
    Conflict,
    ContextPackRequest,
    ContextPackResponse,
    EvidenceChunk,
    RelatedSource,
    Relationship,
    ResultCard,
    RetrievalFilters,
    SearchRequest,
    SearchResponse,
    Status,
    TokenBudget,
    Warning,
    WarningType,
)

__all__ = [
    "DEFAULT_RRF_K",
    "DEFAULT_STALE_AFTER_DAYS",
    "DEFAULT_STATUS_WEIGHTS",
    "WEAK_EVIDENCE_SCORE_THRESHOLD",
    "Confidence",
    "Conflict",
    "ContextPackRequest",
    "ContextPackResponse",
    "EmbeddingProvider",
    "EvidenceChunk",
    "HybridRetriever",
    "RankedChunk",
    "RelatedSource",
    "Relationship",
    "ResultCard",
    "RetrievalConfig",
    "RetrievalConfigError",
    "RetrievalFilters",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "Status",
    "TokenBudget",
    "Warning",
    "WarningType",
    "apply_boosts",
    "build_predicates",
    "deprecated_warnings",
    "detect_conflicts",
    "load_retrieval_config",
    "prompt_injection_warnings",
    "requires_human_review",
    "rrf_fuse",
    "stale_warnings",
    "weak_evidence_warning",
]
