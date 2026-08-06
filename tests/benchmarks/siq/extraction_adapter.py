"""SIQ adapter that ingests via an extraction provider (#489).

``NativeAdapter`` plants each trace's **fixture-authored** memory verbatim:
hand-written fact text with a hand-tuned ``importance``. Since SIQ's whole
premise is *query-blind* ranking — the injected set is ordered by importance
alone — that hand-tuned importance is the load-bearing input to every metric
SIQ reports.

This adapter replaces that with the real write-time path: it extracts from the
turn's **raw utterance** and lets the provider assign both the fact text and
the importance. So it answers the question the fixture path cannot —
*does model-assigned importance reproduce the human-authored priority that
makes need-to-know injection work?*

Attribution
-----------
An extractor turns one utterance into 0..N facts, but the trace's
``should_recall`` targets are authored per-memory. To keep every metric
exactly as authored:

- On a turn that establishes memory ``M``, the extracted fact with the highest
  token overlap against ``M``'s authored content is the **canonical** record
  and takes id ``M``. That is the fact whose survival the metric is asking
  about.
- Every other record — extra fragments off an establishing turn, and all
  records off non-establishing turns — gets a unique ``noise:*`` id.

``noise:*`` ids are never in ``should_recall``, so they land in the precision
denominator and consume budget in ``budget_efficiency`` exactly as real
injected noise would. Fragmentation is therefore penalized rather than
hidden, and a memory the extractor failed to recover shows up as an
anticipation miss.

Sample size: the committed corpus is 4 traces. Results from this adapter are
directional only.
"""

from __future__ import annotations

import itertools
import re
from typing import Dict, List, Optional

from src.popoto.recipes.context_assembler import ContextAssembler

from .adapters import NATIVE_SCORE_WEIGHTS, _BLIND_CUE
from .corpus import SiqTrace, SiqTurn, cleanup, new_trace_namespace
from .metrics import siq_token_count

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set:
    return set(_WORD.findall(text.lower()))


class ExtractionAdapter:
    """Query-blind composite injection over extractor-written memories."""

    def __init__(self, trace: SiqTrace, provider, arm_label: str) -> None:
        self._trace = trace
        self._provider = provider
        self.name = f"extract:{arm_label}"
        self._model_class, self._agent_id, self._prefix = new_trace_namespace()
        self._assembler = ContextAssembler(
            self._model_class,
            score_weights=NATIVE_SCORE_WEIGHTS,
            max_items=trace.max_items,
            max_tokens=trace.max_tokens,
            retrieval_mode="composite",
        )
        # Same query-blind invariant the NativeAdapter guards: if the model
        # ever grew a BM25Field/EmbeddingField this would silently stop
        # measuring subconscious injection.
        assert self._assembler._effective_mode == "composite", (
            "ExtractionAdapter must run composite (query-blind) mode; got "
            f"{self._assembler._effective_mode!r}"
        )
        self._reverse_map: Dict[str, str] = {}  # redis_key -> metric id
        self.extra_token_counts: Dict[str, int] = {}
        self._noise = itertools.count()
        # Diagnostics surfaced alongside the scores.
        self.stats = {
            "turns": 0,
            "records_written": 0,
            "establishing_turns": 0,
            "memories_recovered": 0,
            "noise_records": 0,
        }

    def ingest_turn(self, turn: SiqTurn) -> None:
        self.stats["turns"] += 1
        facts = self._provider.extract(turn.message or "")
        facts = [f for f in facts if f.text and f.text.strip()]

        canonical_idx = None
        if turn.establishes is not None:
            self.stats["establishing_turns"] += 1
            if facts:
                target = _tokens(turn.establishes.content)
                canonical_idx = max(
                    range(len(facts)),
                    key=lambda i: len(_tokens(facts[i].text) & target),
                )
                self.stats["memories_recovered"] += 1

        for i, fact in enumerate(facts):
            record = self._model_class(
                agent_id=self._agent_id,
                content=fact.text.strip(),
                importance=(
                    fact.importance if fact.importance is not None else 0.5
                ),
            )
            if record.save() is False:
                continue
            self.stats["records_written"] += 1
            if i == canonical_idx:
                mem_id = turn.establishes.id
            else:
                mem_id = f"noise:{next(self._noise)}"
                self.stats["noise_records"] += 1
            self._reverse_map[record.db_key.redis_key] = mem_id
            self.extra_token_counts[mem_id] = siq_token_count(fact.text)

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
