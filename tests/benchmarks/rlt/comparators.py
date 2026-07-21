"""RLT comparator adapters — the competitor-fair extension point (#460).

An :class:`RltAdapter` is a memory system exercised through a uniform
ingest/query contract so latency/throughput can be measured identically
across Popoto and future competitor systems (Mem0, Zep, a vector-DB
baseline). This mirrors the Tier-5 ``JudgeProtocol`` pattern
(``tests/benchmarks/judge.py``) and the SIQ ``SiqAdapter`` pattern
(``tests/benchmarks/siq/adapters.py``): a ``Protocol`` plus a reference
implementation, no hard third-party dependency.

Two adapters ship here:

- :class:`NativeAdapter` — wraps Popoto's ``ContextAssembler`` (the system
  under test).
- :class:`NullAdapter` — a dependency-free, deterministic stub returning
  empty results at a fixed synthetic latency. It exists to prove the
  Protocol contract end to end in a unit test (both adapters satisfy the
  same call shape) without any real competitor — a "Protocol" with only one
  implementation is just a docstring, not a verified contract.

Real Mem0/Zep/vector-DB adapters (heavyweight optional deps + live
services/API keys) are a tracked follow-up, not implemented here — no
competitor numbers are fabricated. See ``docs/benchmarks.md``'s RLT section.
"""

from __future__ import annotations

import time
from typing import List, Protocol, Tuple

from src.popoto.recipes.context_assembler import ContextAssembler


class RltAdapter(Protocol):
    """Uniform ingest/query interface the RLT harness drives."""

    name: str

    def ingest(self, content: str) -> None:
        """Write one record into the adapter's store."""
        ...

    def query(self, text: str) -> Tuple[List[str], float]:
        """Run one query; return (result_ids, latency_ms).

        ``latency_ms`` is measured by the adapter itself around its own
        retrieval call, so a comparator adapter's network/client overhead is
        included exactly the way Popoto's in-process call is — no shared
        external timer that could measure a different boundary per adapter.
        """
        ...


class NativeAdapter:
    """Popoto's ``ContextAssembler`` (the system under test).

    Wraps an already-planted corpus's model class + agent id; ``ingest()``
    plants one more record, ``query()`` calls ``assemble()`` and times it
    itself (per the Protocol contract above).
    """

    name = "native"

    def __init__(self, model_class, agent_id: str, max_items: int = 20):
        self._model_class = model_class
        self._agent_id = agent_id
        self._assembler = ContextAssembler(
            model_class=model_class,
            score_weights={"importance": 1.0},
            max_items=max_items,
            retrieval_mode="auto",
        )

    def ingest(self, content: str) -> None:
        record = self._model_class(
            agent_id=self._agent_id, content=content, importance=0.5
        )
        record.save()

    def query(self, text: str) -> Tuple[List[str], float]:
        t0 = time.perf_counter()
        result = self._assembler.assemble(
            query_cues={"topic": text}, agent_id=self._agent_id
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ids = []
        for record in result.records:
            try:
                ids.append(record.db_key.redis_key)
            except Exception:
                ids.append(str(id(record)))
        return ids, latency_ms


class NullAdapter:
    """Dependency-free stub — always returns empty results at a fixed latency.

    Proves the :class:`RltAdapter` Protocol contract without requiring any
    real competitor system; a placeholder a real Mem0/Zep/vector-DB adapter
    can be dropped in alongside without changing the harness.
    """

    name = "null"

    def __init__(self, synthetic_latency_ms: float = 1.0):
        self._synthetic_latency_ms = synthetic_latency_ms
        self._ingested: List[str] = []

    def ingest(self, content: str) -> None:
        self._ingested.append(content)

    def query(self, text: str) -> Tuple[List[str], float]:
        return [], self._synthetic_latency_ms
