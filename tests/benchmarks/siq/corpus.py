"""Trace schema, loader, authoring lint, and corpus planting for SIQ (#459).

A :class:`SiqTrace` is an ordered list of :class:`SiqTurn`. Some turns
*establish* a memory (a :class:`SiqMemory`); some turns declare a
``should_recall`` set — the ids of memories that are ground-truth-useful at
that turn, decided by the fixture author from the narrative (never computed).

The authoring discipline mirrors ``tests/benchmarks/csr/corpus.py``: the
mechanical, un-gameable half of ground truth is enforced at load time by
:func:`lint_trace` using the *real* BM25 tokenizer
(``src.popoto.fields._tokenizer.tokenize`` — the single source of truth BM25
uses). The core rule is **cue-blindness**: at a turn that declares a memory in
``should_recall``, the turn's own ``message`` must share **no** indexed token
with that memory's content. That is the mechanical proof that a query-driven
retriever has nothing to match on — which is exactly the regime SIQ measures.

Planting mirrors the proven per-case key-isolation pattern: each trace gets a
uniquely-named Popoto Model class (``type()`` so the metaclass captures the
unique ``db_class_key`` from the start), so records / sorted-index / any
secondary keys can never contaminate another trace, and :func:`cleanup` scans
and deletes by the unique class name (+ agent prefix backstop).

The model declares **no** ``BM25Field`` / ``EmbeddingField`` / ``CyclicDecayField``
/ ``ExistenceFilter`` — so ``ContextAssembler(retrieval_mode="composite")`` is
the only retrieval mode in play, the push path stays inert, and ranking is
query-blind and deterministic (a plain ``importance`` SortedField, no
wall-clock decay).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src import popoto
from src.popoto.fields._tokenizer import tokenize
from src.popoto.redis_db import POPOTO_REDIS_DB

FIXTURES_DIR = Path(__file__).parent / "fixtures"


class SiqAuthoringError(ValueError):
    """A trace fixture violates an authoring rule (raised at load/plant)."""


@dataclass(frozen=True)
class SiqMemory:
    """One memory a turn establishes.

    ``importance`` is the query-blind composite ranking signal (a plain
    SortedField value, deterministic — no wall-clock decay). Higher importance
    ranks earlier under the budget.
    """

    id: str
    content: str
    importance: float = 0.5


@dataclass(frozen=True)
class SiqTurn:
    """One turn of a trace.

    ``establishes`` is the memory this turn writes into store (``None`` if the
    turn establishes nothing). ``should_recall`` is the set of memory ids that
    are ground-truth-useful at this turn (empty for most turns).
    """

    index: int
    speaker: str
    message: str
    establishes: Optional[SiqMemory] = None
    should_recall: frozenset = frozenset()


@dataclass(frozen=True)
class SiqTrace:
    """A full multi-turn trace fixture.

    ``max_items`` / ``max_tokens`` are the injection budget handed to the
    assembler (``max_tokens=None`` → item-count budget only).
    """

    trace_id: str
    description: str
    turns: Tuple[SiqTurn, ...]
    max_items: int = 5
    max_tokens: Optional[int] = None

    def memories(self) -> Dict[str, SiqMemory]:
        """Map memory id -> SiqMemory for every memory the trace establishes."""
        out: Dict[str, SiqMemory] = {}
        for turn in self.turns:
            if turn.establishes is not None:
                out[turn.establishes.id] = turn.establishes
        return out

    def establish_turn(self) -> Dict[str, int]:
        """Map memory id -> the turn index that establishes it."""
        out: Dict[str, int] = {}
        for turn in self.turns:
            if turn.establishes is not None:
                out[turn.establishes.id] = turn.index
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_trace(raw: dict) -> SiqTrace:
    """Build a :class:`SiqTrace` from a decoded fixture dict (no linting)."""
    turns: List[SiqTurn] = []
    for i, t in enumerate(raw.get("turns", [])):
        establishes = None
        if t.get("establishes") is not None:
            m = t["establishes"]
            establishes = SiqMemory(
                id=str(m["id"]),
                content=str(m["content"]),
                importance=float(m.get("importance", 0.5)),
            )
        turns.append(
            SiqTurn(
                index=i,
                speaker=str(t.get("speaker", "user")),
                message=str(t.get("message", "")),
                establishes=establishes,
                should_recall=frozenset(str(x) for x in t.get("should_recall", [])),
            )
        )
    return SiqTrace(
        trace_id=str(raw["trace_id"]),
        description=str(raw.get("description", "")),
        turns=tuple(turns),
        max_items=int(raw.get("max_items", 5)),
        max_tokens=(None if raw.get("max_tokens") is None else int(raw["max_tokens"])),
    )


def load_trace(path) -> SiqTrace:
    """Load and lint a single trace fixture from ``path``.

    Raises:
        SiqAuthoringError: If the fixture violates an authoring rule.
    """
    raw = json.loads(Path(path).read_text())
    trace = _parse_trace(raw)
    lint_trace(trace)
    return trace


def load_all_traces(directory=FIXTURES_DIR) -> List[SiqTrace]:
    """Load and lint every ``*.json`` trace in ``directory`` (sorted by name).

    Also enforces globally-unique ``trace_id`` across the suite.
    """
    traces = [load_trace(p) for p in sorted(Path(directory).glob("*.json"))]
    seen = set()
    for tr in traces:
        if tr.trace_id in seen:
            raise SiqAuthoringError(f"duplicate trace_id {tr.trace_id!r}")
        seen.add(tr.trace_id)
    return traces


# ---------------------------------------------------------------------------
# Authoring lint (the un-gameable, mechanical half of ground truth)
# ---------------------------------------------------------------------------


def lint_trace(trace: SiqTrace) -> None:
    """Pure fixture-load lint. Raises :class:`SiqAuthoringError` on violation.

    Rules (all token checks use the real BM25 tokenizer):

    1. Trace has at least one turn.
    2. Memory ids are unique; each turn's ``establishes`` id is unique across
       the trace.
    3. Every ``should_recall`` id was ``establishes``-ed at a **strictly
       earlier** turn (a turn cannot recall a memory it just wrote or a memory
       that never exists).
    4. **Cue-blindness** — at every turn ``T`` that lists memory ``m`` in
       ``should_recall``, ``T.message`` shares **no** indexed token with
       ``m.content``. This is the mechanical proof the current message does not
       lexically cue the target memory.
    5. **Anticipation window** — at least one ``should_recall`` in the trace
       occurs ``>= 2`` turns after the memory's establishing turn, so the trace
       can actually exercise anticipation lead time.
    """
    if not trace.turns:
        raise SiqAuthoringError(f"trace {trace.trace_id!r}: no turns")

    establish_turn = trace.establish_turn()
    memories = trace.memories()

    # Rule 2: unique establishes ids.
    seen_ids: List[str] = [
        t.establishes.id for t in trace.turns if t.establishes is not None
    ]
    dups = {i for i in seen_ids if seen_ids.count(i) > 1}
    if dups:
        raise SiqAuthoringError(
            f"trace {trace.trace_id!r}: duplicate established memory ids "
            f"{sorted(dups)}"
        )

    max_gap = -1
    for turn in trace.turns:
        for mem_id in sorted(turn.should_recall):
            # Rule 3: established strictly earlier.
            if mem_id not in establish_turn:
                raise SiqAuthoringError(
                    f"trace {trace.trace_id!r} turn {turn.index}: should_recall "
                    f"{mem_id!r} was never established"
                )
            e_turn = establish_turn[mem_id]
            if e_turn >= turn.index:
                raise SiqAuthoringError(
                    f"trace {trace.trace_id!r} turn {turn.index}: should_recall "
                    f"{mem_id!r} is established at turn {e_turn} (must be a "
                    f"strictly earlier turn)"
                )
            # Rule 4: cue-blindness.
            msg_tokens = set(tokenize(turn.message))
            content_tokens = set(tokenize(memories[mem_id].content))
            shared = msg_tokens & content_tokens
            if shared:
                raise SiqAuthoringError(
                    f"trace {trace.trace_id!r} turn {turn.index}: message shares "
                    f"indexed token(s) {sorted(shared)} with should_recall memory "
                    f"{mem_id!r} — the turn lexically cues the memory, so this is "
                    f"a query-driven case, not a subconscious one"
                )
            max_gap = max(max_gap, turn.index - e_turn)

    # Rule 5: anticipation window.
    if max_gap < 2:
        raise SiqAuthoringError(
            f"trace {trace.trace_id!r}: no should_recall occurs >= 2 turns after "
            f"its establishing turn — trace cannot exercise anticipation lead time"
        )


# ---------------------------------------------------------------------------
# Corpus planting (per-trace key-isolated Model class)
# ---------------------------------------------------------------------------


def build_trace_model_class(safe_prefix: str):
    """Build a fresh per-trace Popoto Model class for Redis key isolation.

    Composite-only by construction: a plain ``importance`` SortedField is the
    query-blind ranking signal (deterministic — no wall-clock decay), and there
    is **no** BM25Field / EmbeddingField / CyclicDecayField / ExistenceFilter,
    so ``ContextAssembler(retrieval_mode="composite")`` is the only path and the
    push path stays inert.

    Built with ``type()`` so the metaclass captures the unique
    ``db_class_key`` from class creation (a post-hoc ``__name__`` rename would
    leave the SortedField index shared across traces — cross-trace
    contamination).
    """
    return type(
        f"SiqMem{safe_prefix}",
        (popoto.Model,),
        {
            "__module__": __name__,
            "__qualname__": f"SiqMem{safe_prefix}",
            "memory_key": popoto.AutoKeyField(),
            "agent_id": popoto.KeyField(),
            "content": popoto.StringField(default=""),
            "importance": popoto.SortedField(type=float, default=0.5),
        },
    )


def plant_memory(model_class, agent_id: str, memory: SiqMemory):
    """Plant a single memory into the current Redis DB.

    Returns the saved record.

    Raises:
        SiqAuthoringError: If ``save()`` reports failure.
    """
    record = model_class(
        agent_id=agent_id,
        content=memory.content,
        importance=memory.importance,
    )
    if record.save() is False:
        raise SiqAuthoringError(f"save() returned False planting memory {memory.id!r}")
    return record


def new_trace_namespace() -> Tuple[object, str, str]:
    """Allocate a fresh (model_class, agent_id, safe_prefix) for one replay.

    The prefix is random per replay so repeated runs and parallel pytest
    workers never collide on Redis keys (matches ``external_base``).
    """
    safe_prefix = uuid.uuid4().hex[:8]
    model_class = build_trace_model_class(safe_prefix)
    agent_id = f"siq{safe_prefix}"
    return model_class, agent_id, safe_prefix


def cleanup(model_class, agent_id: str) -> None:
    """Delete every Redis key created for one planted trace.

    Scans ``*{ClassName}*`` (the unique per-trace class name appears in record
    keys and the ``:_importance`` sorted index) then the agent prefix as a
    backstop — mirrors ``external_base.teardown`` / ``csr.corpus.cleanup``.
    Uses non-blocking ``SCAN`` (Valkey-safe, no modules).
    """
    class_name = model_class.__name__
    for pattern in (f"*{class_name}*", f"*{agent_id}*"):
        cursor = 0
        while True:
            cursor, keys = POPOTO_REDIS_DB.scan(cursor, match=pattern, count=200)
            if keys:
                POPOTO_REDIS_DB.delete(*keys)
            if cursor == 0:
                break
