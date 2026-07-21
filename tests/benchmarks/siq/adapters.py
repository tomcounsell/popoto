"""SIQ adapters — the competitor-fair extension point (issue #459).

An :class:`SiqAdapter` is a memory system replayed turn-by-turn against a
:class:`~tests.benchmarks.siq.corpus.SiqTrace`. The runner calls
:meth:`ingest_turn` for every turn in order (the adapter writes any established
memory into its own store), then :meth:`injected_for_turn` to ask *what the
adapter would place in context at this turn* — the injected set the SIQ metrics
score. Token accounting and metric computation live in the runner/metrics, so
every adapter is scored on identical footing.

This mirrors the Tier-5 ``JudgeProtocol`` pattern (``tests/benchmarks/judge.py``):
a ``Protocol`` plus a reference implementation, no hard third-party dependency.

Two adapters ship here:

- :class:`NativeAdapter` — Popoto's query-blind composite mode via
  ``ContextAssembler(retrieval_mode="composite")``. This is the system under
  test. It injects by importance/decay ranking with **no** lexical cue from the
  current turn's message, so it can surface a memory before the user restates it.
- :class:`QueryOnlyStubAdapter` — a dependency-free, deterministic *query-driven*
  baseline standing in for the Mem0/Zep/Hindsight family. It injects only
  memories whose content lexically overlaps the current turn's message. By the
  cue-blindness authoring lint, at a ``should_recall`` turn the message shares
  no token with the target memory, so this adapter injects nothing there — it
  scores ~0 recall / all-miss anticipation *by construction*. That near-zero
  baseline is the harness's own validity proof and is asserted in ``test_siq.py``.

Real Mem0/Zep/Hindsight adapters (heavyweight optional deps + live API keys)
are a tracked follow-up, not implemented here — no competitor numbers are
fabricated.
"""

from __future__ import annotations

from typing import Dict, List, Protocol

from src.popoto.fields._tokenizer import tokenize
from src.popoto.recipes.context_assembler import ContextAssembler

from .corpus import (
    SiqTrace,
    SiqTurn,
    cleanup,
    new_trace_namespace,
    plant_memory,
)

#: Composite score weights for the native adapter — a single query-blind signal
#: over the model's ``importance`` SortedField. Deterministic (no wall-clock),
#: matching the CSR composite-control convention.
NATIVE_SCORE_WEIGHTS = {"importance": 1.0}

#: Content-free cue used only to enter the composite pull path (``assemble``
#: skips the pull path when ``query_cues`` is falsy). Its value never affects
#: composite ranking — that is the query-blind property SIQ measures.
_BLIND_CUE = {"topic": "_"}


class SiqAdapter(Protocol):
    """Turn-by-turn memory-system interface the SIQ runner drives."""

    name: str

    def ingest_turn(self, turn: SiqTurn) -> None:
        """Observe one turn; write its established memory (if any) to store."""
        ...

    def injected_for_turn(self, turn: SiqTurn) -> List[str]:
        """Return the rank-ordered, budget-capped memory ids to inject now."""
        ...

    def teardown(self) -> None:
        """Release any resources (Redis keys, clients)."""
        ...


class NativeAdapter:
    """Popoto query-blind composite mode (the system under test).

    Builds a per-trace key-isolated Model + a ``ContextAssembler`` in
    ``composite`` mode. ``injected_for_turn`` calls ``assemble`` with a
    content-free cue, so the injected set is ranked purely by importance over
    whatever has been planted so far — never by the current message.
    """

    name = "native"

    def __init__(self, trace: SiqTrace) -> None:
        self._trace = trace
        self._model_class, self._agent_id, self._prefix = new_trace_namespace()
        self._assembler = ContextAssembler(
            self._model_class,
            score_weights=NATIVE_SCORE_WEIGHTS,
            max_items=trace.max_items,
            max_tokens=trace.max_tokens,
            retrieval_mode="composite",
        )
        # Guard the query-blind invariant: if the model ever grew a BM25Field /
        # EmbeddingField this would flip to lexical/hybrid and silently stop
        # measuring subconscious injection. Fail loud instead.
        assert self._assembler._effective_mode == "composite", (
            "NativeAdapter must run composite (query-blind) mode; got "
            f"{self._assembler._effective_mode!r}"
        )
        self._reverse_map: Dict[str, str] = {}  # redis_key -> memory id

    def ingest_turn(self, turn: SiqTurn) -> None:
        if turn.establishes is not None:
            record = plant_memory(self._model_class, self._agent_id, turn.establishes)
            self._reverse_map[record.db_key.redis_key] = turn.establishes.id

    def injected_for_turn(self, turn: SiqTurn) -> List[str]:
        result = self._assembler.assemble(
            query_cues=_BLIND_CUE, agent_id=self._agent_id
        )
        injected: List[str] = []
        for record in result.records:
            mem_id = self._reverse_map.get(record.db_key.redis_key)
            if mem_id is not None:
                injected.append(mem_id)
        return injected

    def teardown(self) -> None:
        cleanup(self._model_class, self._agent_id)


class QueryOnlyStubAdapter:
    """Deterministic query-driven baseline (Mem0/Zep/Hindsight stand-in).

    Holds established memories in-process and injects only those whose content
    shares an indexed token with the current turn's message, ranked by
    importance (desc), tie-broken by id, capped at ``max_items``. No Redis, no
    network, no RNG — fully deterministic and dependency-free.
    """

    name = "query_stub"

    def __init__(self, trace: SiqTrace) -> None:
        self._trace = trace
        self._max_items = trace.max_items
        self._store: Dict[str, tuple] = {}  # id -> (importance, content_tokens)

    def ingest_turn(self, turn: SiqTurn) -> None:
        if turn.establishes is not None:
            m = turn.establishes
            self._store[m.id] = (m.importance, frozenset(tokenize(m.content)))

    def injected_for_turn(self, turn: SiqTurn) -> List[str]:
        msg_tokens = set(tokenize(turn.message))
        hits = [
            (imp, mem_id)
            for mem_id, (imp, content_tokens) in self._store.items()
            if msg_tokens & content_tokens
        ]
        # importance desc, then id asc for a stable deterministic order.
        hits.sort(key=lambda x: (-x[0], x[1]))
        return [mem_id for _, mem_id in hits[: self._max_items]]

    def teardown(self) -> None:  # no external resources
        return None


#: Adapter registry for the CLI (``run_siq.py --adapter``). Real competitor
#: adapters register here in the tracked follow-up.
ADAPTERS = {
    "native": NativeAdapter,
    "query_stub": QueryOnlyStubAdapter,
}
