"""Synthetic corpus + query-set builder for the RLT harness (#460).

Builds a flat corpus of ``n`` memory records into a uniquely-prefixed Popoto
Model class (mirrors ``scenarios/external_base.py`` /
``tests/benchmarks/siq/corpus.py``'s per-run key-isolation pattern) plus a
small, fixed, deterministic query set with **authored** (not derived) ground
truth — the CSR/SIQ convention — so ``recall_at_k()`` from
``tests/benchmarks/metrics/retrieval.py`` can be called directly against RLT's
own retrieval results for the Pareto step.

The corpus is a lexical (BM25-only) retrieval surface deliberately — no
EmbeddingField, so no model download is needed to run the fast unit-test
corpora, and the real headline measurement runs (a separate, deferred
concern) can add a hybrid/vector variant later without changing this module's
shape.

Records are drawn from a small fixed vocabulary of "topics"; each record's
content embeds exactly one topic token plus filler text, and each query
targets one topic — so the ground-truth relevant set for a query is exactly
the records that embed that query's topic. This makes recall meaningfully
non-trivial (some records are relevant, most are distractors) without any
external dataset dependency.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Dict, List, Tuple

from src import popoto
from src.popoto.fields.bm25_field import BM25Field
from src.popoto.redis_db import POPOTO_REDIS_DB

#: Fixed topic vocabulary. Deterministic — no wall-clock, no randomness in
#: content generation (a corpus of the same size is byte-identical run to run
#: given the same ``seed``, which only affects query/topic assignment).
_TOPICS = (
    "astronomy",
    "botany",
    "cartography",
    "diplomacy",
    "ecology",
    "forestry",
    "geology",
    "horticulture",
    "ichthyology",
    "journalism",
)

#: Number of queries in the default query set. A benchmark tuning constant —
#: small enough that the latency/throughput runners stay fast even against
#: the largest (2x10^4) scaling-curve corpus.
DEFAULT_QUERY_COUNT = 10


class RltCorpusError(ValueError):
    """A corpus-building precondition was violated."""


@dataclass(frozen=True)
class RltQuery:
    """One benchmark query with authored ground truth."""

    text: str
    relevant_ids: frozenset


@dataclass
class RltCorpus:
    """A planted corpus: the Model class, its records' keys, and a query set."""

    model_class: type
    agent_id: str
    safe_prefix: str
    record_keys: List[str]
    queries: Tuple[RltQuery, ...]
    size: int


def build_corpus_model_class(safe_prefix: str):
    """Build a fresh per-run Popoto Model class for Redis key isolation.

    Lexical-only by construction (BM25Field, no EmbeddingField) — see module
    docstring. Built with ``type()`` so the metaclass captures the unique
    ``db_class_key`` from class creation, matching the SIQ/CSR convention (a
    post-hoc ``__name__`` rename would leave the BM25 index shared across
    runs).
    """
    return type(
        f"RltMem{safe_prefix}",
        (popoto.Model,),
        {
            "__module__": __name__,
            "__qualname__": f"RltMem{safe_prefix}",
            "memory_key": popoto.AutoKeyField(),
            "agent_id": popoto.KeyField(),
            "content": popoto.StringField(default=""),
            "importance": popoto.SortedField(type=float, default=0.5),
            "content_index": BM25Field(source="content"),
        },
    )


def build_corpus(
    n: int, seed: int = 0, query_count: int = DEFAULT_QUERY_COUNT
) -> RltCorpus:
    """Build and plant a synthetic corpus of ``n`` records into Redis.

    Args:
        n: Number of records to plant. Must be > 0.
        seed: Deterministic seed for topic assignment (record content itself
            is not randomized — only which topic each record is assigned).
        query_count: Number of queries in the returned query set. Capped at
            ``len(_TOPICS)`` (one query per topic, at most).

    Returns:
        A planted :class:`RltCorpus`. Caller is responsible for calling
        :func:`teardown_corpus` when done.

    Raises:
        RltCorpusError: If ``n <= 0``.
    """
    if n <= 0:
        raise RltCorpusError(f"corpus size must be > 0, got {n}")

    safe_prefix = uuid.uuid4().hex[:8]
    model_class = build_corpus_model_class(safe_prefix)
    agent_id = f"rlt{safe_prefix}"

    n_topics = len(_TOPICS)
    record_keys: List[str] = []
    # topic_id -> record ids embedding that topic (ground truth for queries).
    by_topic: Dict[str, List[str]] = {t: [] for t in _TOPICS}

    for i in range(n):
        topic = _TOPICS[(i + seed) % n_topics]
        content = (
            f"record {i} discusses {topic} in the context of a synthetic "
            f"benchmark corpus entry number {i}"
        )
        record = model_class(
            agent_id=agent_id,
            content=content,
            importance=0.5,
        )
        if record.save() is False:
            raise RltCorpusError(f"save() returned False planting record {i}")
        key = record.db_key.redis_key
        record_keys.append(key)
        by_topic[topic].append(key)

    n_queries = min(query_count, n_topics)
    queries = tuple(
        RltQuery(text=f"tell me about {topic}", relevant_ids=frozenset(by_topic[topic]))
        for topic in _TOPICS[:n_queries]
        if by_topic[topic]
    )

    return RltCorpus(
        model_class=model_class,
        agent_id=agent_id,
        safe_prefix=safe_prefix,
        record_keys=record_keys,
        queries=queries,
        size=n,
    )


def teardown_corpus(corpus: RltCorpus) -> None:
    """Delete every Redis key created for ``corpus``.

    Scans ``*{ClassName}*`` (covers the record keys + the BM25 index keys)
    then the agent-id prefix as a backstop — mirrors
    ``scenarios/external_base.teardown`` / ``tests/benchmarks/siq/corpus.cleanup``.
    Non-blocking ``SCAN`` (Valkey-safe, no modules).
    """
    class_name = corpus.model_class.__name__
    for pattern in (f"*{class_name}*", f"*{corpus.agent_id}*"):
        cursor = 0
        while True:
            cursor, keys = POPOTO_REDIS_DB.scan(cursor, match=pattern, count=200)
            if keys:
                POPOTO_REDIS_DB.delete(*keys)
            if cursor == 0:
                break
