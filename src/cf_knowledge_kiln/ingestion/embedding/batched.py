"""Batched concurrent embedding fan-out (PR C, prep for #108 item 2).

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
loud :class:`ValueError` — silent vector drift is worse than a
failed ingest.

This helper is provider-agnostic. It works with :class:`MockEmbeddingProvider`,
``OpenAICompatibleEmbeddingProvider``, and the in-flight
``LocalSentenceTransformersProvider`` equally — they all honor the
same ``embed(list[str]) -> list[list[float]]`` contract.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cf_knowledge_kiln.ingestion.embedding import EmbeddingProvider

logger = logging.getLogger(__name__)


async def embed_chunks_concurrently(
    *,
    texts: list[str],
    provider: EmbeddingProvider,
    batch_size: int,
    concurrency: int,
) -> list[list[float]]:
    """Embed ``texts`` in batched, concurrent fan-out.

    Returns one vector per input text, in input order. Provider is
    called ``ceil(len(texts) / batch_size)`` times across the run;
    at most ``concurrency`` of those calls are in flight at once.

    :raises ValueError: when ``batch_size`` or ``concurrency`` is
        non-positive, or when the provider returns the wrong number
        of vectors for a batch.
    """
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive, got {batch_size}")
    if concurrency <= 0:
        raise ValueError(f"concurrency must be positive, got {concurrency}")
    if not texts:
        return []

    # Pre-slice into (start_index, batch_texts) pairs so the gather
    # step can write straight into the output array by absolute
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

    await asyncio.gather(*(_run_one(start, btxts) for start, btxts in batches))

    # Final defensive check: every slot must be filled. Reachable only
    # if a provider returned a "wrong" count that happened to match
    # something other than its batch — but we already enforce that
    # above. Belt-and-suspenders for the load-bearing order invariant.
    if any(v is None for v in out):
        raise ValueError("embed_chunks_concurrently left holes in the output")
    return [v for v in out if v is not None]


__all__ = ["embed_chunks_concurrently"]
