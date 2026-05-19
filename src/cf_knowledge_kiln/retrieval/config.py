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

# Weak-evidence floor for the RRF fused score. Top-1 in BOTH arms with
# default k=60 yields ~0.0328 (= 2/61); top-1 in ONE arm only is ~0.0164.
# Threshold 0.015 catches "no match in either arm" (true weak evidence)
# without false-failing single-arm hits on clean queries — measured against
# the docs/_eval corpus on Nomic Embed v1.5. Tuned for the RRF k=60 scale
# (ADR-0009); the pre-#160 value of 0.5 was a holdover from before fusion
# and tripped on every real result. Override per deployment via
# `retrieval.weak_evidence_score_threshold` in `config/security.yaml`.
DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD: float = 0.015


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
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    status_weights: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_STATUS_WEIGHTS))
    stale_after_days: int | None = DEFAULT_STALE_AFTER_DAYS
    weak_evidence_score_threshold: float = Field(
        default=DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD, gt=0.0
    )

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
    try:
        return RetrievalConfig.model_validate(payload)
    except ValidationError as exc:
        raise RetrievalConfigError(f"{p}: {exc}") from exc


__all__ = [
    "DEFAULT_STALE_AFTER_DAYS",
    "DEFAULT_STATUS_WEIGHTS",
    "DEFAULT_WEAK_EVIDENCE_SCORE_THRESHOLD",
    "RetrievalConfig",
    "RetrievalConfigError",
    "load_retrieval_config",
]
