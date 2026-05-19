"""Batched concurrent embedding fan-out (PR C, #108 item 2 prep).

A single helper, :func:`embed_chunks_concurrently`, takes a list of
texts and returns one vector per text — in input order — by:

1. Slicing the input into batches of ``batch_size``,
2. Calling ``provider.embed(batch_texts)`` once per batch (the
   provider's internal batching does the real work),
3. Running up to ``concurrency`` batches in parallel via
   :func:`asyncio.gather` with an :class:`asyncio.Semaphore` cap.

Order preservation: batches carry their input-slice indices so the
final list can be reassembled regardless of completion order. A
provider that returns the wrong number of vectors for a batch is a
loud :class:`ValueError` raised inside the batch coroutine; the
gather collects it as a per-batch failure rather than tearing down
sibling batches.

Per-batch failure granularity (#151): the helper uses
``asyncio.gather(return_exceptions=True)`` so a single failing batch
does not discard the other batches' successful vectors. The caller
receives a :class:`BatchResults` with two slots:

* ``vectors`` — one entry per input text; ``None`` for chunks whose
  batch raised. The caller decides how to account those (typically:
  upsert the non-None vectors, increment ``embeddings_failed`` for
  the None ones).
* ``failures`` — ``(start_offset, batch_size, exc)`` tuples so the
  caller can log per-batch forensics without re-running the gather.

This helper is provider-agnostic. It works with :class:`MockEmbeddingProvider`,
``OpenAICompatibleEmbeddingProvider``, and ``LocalSentenceTransformersProvider``
equally — they all honor the same ``embed(list[str]) -> list[list[float]]``
contract.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)


@dataclass
class BatchResults:
    """Outcome of a concurrent embedding fan-out.

    ``vectors`` is order-aligned with the input texts: ``vectors[i]``
    is the embedding for ``texts[i]``, or ``None`` if the batch that
    contained ``texts[i]`` raised. ``failures`` carries one entry per
    failed batch so the caller can log per-batch forensics.
    """

    vectors: list[list[float] | None]
    failures: list[tuple[int, int, BaseException]] = field(default_factory=list)


async def embed_chunks_concurrently(
    *,
    texts: list[str],
    provider: EmbeddingProvider,
    batch_size: int,
    concurrency: int,
) -> BatchResults:
    """Embed ``texts`` in batched, concurrent fan-out (partial success).

    Returns a :class:`BatchResults` whose ``vectors`` list is aligned
    1:1 with the input — successful slots hold the vector, slots whose
    batch raised hold ``None``. Per-batch exceptions are reported in
    ``failures`` rather than propagated; callers decide whether a
    partial run is acceptable.

    :raises ValueError: when ``batch_size`` or ``concurrency`` is
        non-positive. Per-batch provider errors are returned, not raised.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    if concurrency <= 0:
        raise ValueError(f"concurrency must be positive, got {concurrency}")
    if not texts:
        return BatchResults(vectors=[])

    # Pre-slice into (start_index, batch_texts) pairs so each batch
    # coroutine can write straight into the output array by absolute
    # position — no post-sort needed.
    batches: list[tuple[int, list[str]]] = [
        (i, texts[i : i + batch_size]) for i in range(0, len(texts), batch_size)
    ]
    out: list[list[float] | None] = [None] * len(texts)
    semaphore = asyncio.Semaphore(concurrency)

    async def _run_one(start: int, batch_texts: list[str]) -> None:
        async with semaphore:
            vectors = await provider.embed(batch_texts)
        if len(vectors) != len(batch_texts):
            raise ValueError(
                f"embedding provider returned {len(vectors)} vectors for "
                f"a batch of {len(batch_texts)} (input offset {start})"
            )
        for offset, vector in enumerate(vectors):
            out[start + offset] = vector

    # return_exceptions=True so a failing batch doesn't cancel siblings;
    # we walk the result list afterwards to surface each per-batch error.
    results = await asyncio.gather(
        *(_run_one(start, btxts) for start, btxts in batches),
        return_exceptions=True,
    )
    failures = _collect_failures(batches, results, out)
    return BatchResults(vectors=out, failures=failures)


def _collect_failures(
    batches: list[tuple[int, list[str]]],
    results: list[BaseException | None],
    out: list[list[float] | None],
) -> list[tuple[int, int, BaseException]]:
    """Pull exceptions off a ``gather(return_exceptions=True)`` result.

    Mutates ``out`` to ensure failed batches' slots are ``None`` (defends
    against a provider that wrote partial state before raising).
    """
    failures: list[tuple[int, int, BaseException]] = []
    for (start, btxts), result in zip(batches, results, strict=True):
        if isinstance(result, BaseException):
            failures.append((start, len(btxts), result))
            for offset in range(len(btxts)):
                out[start + offset] = None
    return failures


__all__ = ["BatchResults", "embed_chunks_concurrently"]
