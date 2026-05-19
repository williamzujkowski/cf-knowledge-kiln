"""Retrieval-side config loader (Phase 5).

Reads the ``retrieval`` and ``freshness`` sections of ``config/security.yaml``
that the ranking layer needs:

* ``retrieval.status_weights`` — per-status multiplier applied to fused
  scores so deprecated/archived/superseded docs lose to active.
* ``freshness.stale_after_days`` — boundary at which a document earns
  a ``stale_source`` warning and starts losing freshness boost.

Same policy as
:func:`cf_knowledge_kiln.ingestion.embedding.factory.build_provider_from_settings`:
a missing file is logged + the loader returns reasonable defaults; a
malformed file raises so the operator fixes it.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


DEFAULT_STATUS_WEIGHTS: dict[str, float] = {
    "active": 1.0,
    "approved": 0.95,
    "draft": 0.5,
    "deprecated": 0.2,
    "archived": 0.1,
    "superseded": 0.05,
}
"""Mirrors config/security.example.yaml — used when no config is present."""

DEFAULT_STALE_AFTER_DAYS: int = 365

# Weak-evidence floor for the fused score, on the normalized [0, 1]
# scale emitted by the hybrid SQL (#164). The SQL rescales the raw
# ``SUM(1/(k+rnk))`` by ``(k+1)/2``, so a both-arm rank-1 hit lands at
# ``1.0`` and a single-arm rank-1 hit at ``0.5``. ``0.46`` is the
# proportional re-baseline of the pre-#164 raw-scale ``0.015`` floor
# (``0.015 * 30.5 ~= 0.46``), so the same fraction of chunks trip
# weak-evidence as before #164 — measured against the docs/_eval corpus
# on Nomic Embed v1.5 — but the constant is interpretable as "below
# ~92% of single-arm rank-1." Override per deployment via
# `retrieval.weak_evidence_score_threshold` in `config/security.yaml`.
DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD: float = 0.46

# Per-query rank cutoff for the per-chunk security warning emitters
# (#161). Only chunks at rank ≤ this cutoff count toward
# ``sensitive_content``, ``prompt_injection_pattern``, and
# ``conflicting_sources`` warnings. A chunk that landed at rank 7 via
# cosine noise no longer trips review on a clean query that happens to
# share a few keywords with an adversarial fixture; the high-relevance
# signal — a sensitive chunk at rank 1 with a both-arm RRF score —
# still trips. See ``docs/security.md`` for the policy framing.
DEFAULT_MAX_WARNING_RANK: int = 3


class RetrievalConfig(BaseModel):
    """Ranking parameters loaded from ``config/security.yaml``.

    Attributes
    ----------
    status_weights:
        Maps a document status to a multiplier in (0, 1]. Statuses not
        in the map fall back to a weight of 1.0 (i.e. no penalty), so
        a future status added at the data layer doesn't silently
        zero-out matches before the operator updates the config.
    stale_after_days:
        Documents not reviewed within this many days are flagged
        ``stale_source`` and lose freshness boost. ``None`` disables
        the check entirely.
    weak_evidence_score_threshold:
        Fused-score floor below which the result set trips
        ``weak_evidence`` + ``requires_human_review``. Calibrated for
        RRF k=60 (#160).
    relevance_floor:
        Score floor below which a chunk is treated as cosine-noise
        for the per-chunk security warning emitters
        (``sensitive_content``, ``prompt_injection_pattern``,
        ``conflicting_sources``). ``None`` (the default) means "use
        :attr:`weak_evidence_score_threshold`" — the common case is
        that operators tune one knob and both gates move together. Set
        explicitly when you want a STRICTER relevance gate on warnings
        than on the weak-evidence short-circuit (e.g. 1.5x the
        weak-evidence floor to demand a clear both-arm hit before
        emitting). (#161)
    max_warning_rank:
        Per-query rank cutoff for the per-chunk security warning
        emitters. Only chunks at rank ≤ this value are eligible to
        trip ``sensitive_content``, ``prompt_injection_pattern``, or
        ``conflicting_sources``. Default 3 keeps a tight head-of-list
        gate. (#161)
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    status_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STATUS_WEIGHTS))
    stale_after_days: int | None = DEFAULT_STALE_AFTER_DAYS
    weak_evidence_score_threshold: float = Field(
        default=DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD, gt=0.0
    )
    relevance_floor: float | None = Field(default=None, gt=0.0)
    max_warning_rank: int = Field(default=DEFAULT_MAX_WARNING_RANK, ge=1)

    @property
    def effective_relevance_floor(self) -> float:
        """Resolve :attr:`relevance_floor` against the weak-evidence default.

        ``None`` means "track the weak-evidence threshold" so an
        operator who tunes ``weak_evidence_score_threshold`` only also
        moves the warning gate without having to set a second knob.
        Callers should use this property rather than reading
        ``relevance_floor`` directly.
        """
        if self.relevance_floor is None:
            return self.weak_evidence_score_threshold
        return self.relevance_floor

    @field_validator("status_weights")
    @classmethod
    def _weights_in_unit_interval(cls, value: dict[str, float]) -> dict[str, float]:
        """Each weight must be in (0, 1].

        A negative weight would invert ranking (deprecated chunks pushed
        to the top); a > 1.0 weight would let one status outrun a
        perfectly-scored peer. Both are bugs we want to catch at config
        load, not at query time. Zero is rejected because that's what
        ``status_weights: {deprecated: 0.0}`` would silently do to
        deprecated docs — if you want to suppress a status entirely,
        use a tiny positive number like the example's 0.05 for
        ``superseded``, or filter at query time.
        """
        for status, weight in value.items():
            if not (0.0 < weight <= 1.0):
                raise ValueError(f"status_weights[{status!r}] = {weight} is outside (0, 1]")
        return value

    def weight_for_status(self, status: str) -> float:
        """Return the multiplier for ``status``; unknown statuses get 1.0."""
        return self.status_weights.get(status, 1.0)


class RetrievalConfigError(ValueError):
    """Raised when ``config/security.yaml`` is malformed.

    Missing file is *not* an error: callers get defaults + a warning.
    """


def load_retrieval_config(path: str | Path | None) -> RetrievalConfig:
    """Read ``retrieval`` + ``freshness`` from ``path`` and return the config.

    ``None`` or a missing path returns defaults (with a warning logged).
    Malformed YAML or schema-violating content raises
    :class:`RetrievalConfigError`.
    """
    if path is None:
        logger.info("no security config path given; using default retrieval config")
        return RetrievalConfig()
    p = Path(path)
    if not p.exists():
        logger.warning(
            "no security config at %s; using default retrieval config (status weights = %s)",
            p,
            DEFAULT_STATUS_WEIGHTS,
        )
        return RetrievalConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise RetrievalConfigError(f"malformed YAML in {p}: {exc}") from exc
    payload: dict[str, Any] = {}
    retrieval = raw.get("retrieval") or {}
    if "status_weights" in retrieval:
        payload["status_weights"] = retrieval["status_weights"]
    freshness = raw.get("freshness") or {}
    if "stale_after_days" in freshness:
        payload["stale_after_days"] = freshness["stale_after_days"]
    if "weak_evidence_score_threshold" in retrieval:
        payload["weak_evidence_score_threshold"] = retrieval["weak_evidence_score_threshold"]
    if "relevance_floor" in retrieval:
        payload["relevance_floor"] = retrieval["relevance_floor"]
    if "max_warning_rank" in retrieval:
        payload["max_warning_rank"] = retrieval["max_warning_rank"]
    try:
        return RetrievalConfig.model_validate(payload)
    except ValidationError as exc:
        raise RetrievalConfigError(f"{p}: {exc}") from exc


__all__ = [
    "DEFAULT_MAX_WARNING_RANK",
    "DEFAULT_STALE_AFTER_DAYS",
    "DEFAULT_STATUS_WEIGHTS",
    "DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD",
    "RetrievalConfig",
    "RetrievalConfigError",
    "load_retrieval_config",
]
