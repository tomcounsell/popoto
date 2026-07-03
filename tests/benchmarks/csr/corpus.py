"""Corpus planting + test-case schema for the deterministic CSR harness (#418).

A :class:`CsrTestCase` is ``(planted corpus, standard query, adversarial
query, assertions)``. ``plant()`` builds a per-case unique-name Popoto Model
class (the proven ``external_base`` key-isolation pattern), saves each
:class:`PlantedMemory` into the current Redis DB, and returns the maps the
runner needs to translate ``assemble()`` records back to planted ids.

Determinism guarantees enforced here:

- ``plant()`` raises on duplicate planted ids (ids are the assertion key
  space; a collision would silently corrupt scoring).
- ``plant()`` ends with the post-plant adversarial guard: the adversarial
  query must produce >= 1 BM25 hit against the planted corpus, else the case
  raises a clear authoring error. A zero-hit adversarial query would
  silently execute the query-blind composite fallback inside
  ``_pull_path_hybrid`` (a ``logger.warning`` only), so the "lexical
  adversarial" number would measure composite-fallback behavior, not lexical
  query-sensitivity.
- ``lint_case()`` enforces the two token authoring rules with the REAL BM25
  tokenizer (``src.popoto.fields._tokenizer.tokenize`` — the single source of
  truth BM25 uses for indexing and query-term extraction): the adversarial
  query is token-disjoint from every relevant memory AND intersects the
  union of all corpus content tokens.
"""

import uuid
from dataclasses import dataclass, field
from typing import Dict, Tuple

from src import popoto
from src.popoto.fields._tokenizer import tokenize
from src.popoto.fields.bm25_field import BM25Field
from src.popoto.redis_db import POPOTO_REDIS_DB

# Name of the BM25Field on every per-case model. BM25Field.search() takes the
# BM25 field's OWN name (it validates isinstance BM25Field), so the harness
# searches "content_index", never the source StringField "content".
BM25_FIELD_NAME = "content_index"


class CsrAuthoringError(ValueError):
    """A suite fixture violates an authoring rule (raised at load/plant)."""


@dataclass(frozen=True)
class PlantedMemory:
    """One memory planted into the corpus.

    ``timestamp`` is a planted fixture value (never wall clock) — it is what
    ``NoneOlderThan`` reads via ``planted_meta``.
    """

    id: str
    content: str
    timestamp: float
    topic: str
    importance: float = 0.5


@dataclass(frozen=True)
class CsrTestCase:
    """One CSR test case.

    ``relevant_ids`` is the authored ground truth for the token lint: the
    adversarial query must share no indexed token with any relevant memory's
    content (but at least one with a distractor, so BM25 still hits).

    ``retrieval_mode``/``score_weights`` are assembler overrides. Defaults
    (``"auto"``/``{}``) resolve to lexical (the model declares a BM25Field
    and no embedding field). The composite control case overrides both:
    ``retrieval_mode="composite"``, ``score_weights={"importance": 1.0}`` —
    non-empty weights over the model's SortedField, otherwise
    ``composite_score({})`` raises QueryException and ranks nothing.
    """

    case_id: str
    planted_corpus: Tuple[PlantedMemory, ...]
    standard_query: str
    adversarial_query: str
    primitive: str
    assertions: tuple
    relevant_ids: frozenset
    retrieval_mode: str = "auto"
    score_weights: dict = field(default_factory=dict)


def lint_case(case: CsrTestCase) -> None:
    """Pure suite-load lint. Raises :class:`CsrAuthoringError` on violation.

    Rules (all evaluated with the real BM25 tokenizer):

    1. The case has at least one assertion (an assertion-free case is an
       authoring mistake — CSR would be 0/0).
    2. ``relevant_ids`` is non-empty and a subset of the planted corpus ids.
    3. The adversarial query is token-disjoint from every relevant memory's
       content.
    4. The adversarial query shares at least one token with the union of all
       corpus content tokens (so BM25 returns hits and the lexical path
       actually executes instead of the composite fallback).
    """
    if not case.assertions:
        raise CsrAuthoringError(
            f"case {case.case_id!r}: empty assertion list (rejected at load; "
            f"an assertion-free case is almost always an authoring mistake)"
        )

    corpus_ids = [m.id for m in case.planted_corpus]
    if len(set(corpus_ids)) != len(corpus_ids):
        raise CsrAuthoringError(
            f"case {case.case_id!r}: duplicate planted ids in corpus"
        )
    if not case.relevant_ids:
        raise CsrAuthoringError(f"case {case.case_id!r}: empty relevant_ids")
    unknown = set(case.relevant_ids) - set(corpus_ids)
    if unknown:
        raise CsrAuthoringError(
            f"case {case.case_id!r}: relevant_ids not in corpus: {sorted(unknown)}"
        )

    adv_tokens = set(tokenize(case.adversarial_query))
    if not adv_tokens:
        raise CsrAuthoringError(
            f"case {case.case_id!r}: adversarial query produces no indexed tokens"
        )

    corpus_token_union = set()
    for memory in case.planted_corpus:
        content_tokens = set(tokenize(memory.content))
        corpus_token_union |= content_tokens
        if memory.id in case.relevant_ids:
            shared = adv_tokens & content_tokens
            if shared:
                raise CsrAuthoringError(
                    f"case {case.case_id!r}: adversarial query shares indexed "
                    f"token(s) {sorted(shared)} with relevant memory "
                    f"{memory.id!r} — a weak paraphrase behaves like the "
                    f"standard query"
                )

    if not (adv_tokens & corpus_token_union):
        raise CsrAuthoringError(
            f"case {case.case_id!r}: adversarial query shares no indexed token "
            f"with the entire corpus — BM25 would return zero hits and the "
            f"lexical path would silently fall back to the query-blind "
            f"composite path"
        )


def lint_suite(cases) -> None:
    """Lint every case and require unique case_ids."""
    seen = set()
    for case in cases:
        if case.case_id in seen:
            raise CsrAuthoringError(f"duplicate case_id {case.case_id!r}")
        seen.add(case.case_id)
        lint_case(case)


def _build_csr_model_class(safe_prefix: str):
    """Build a fresh per-case Popoto Model class for Redis key isolation.

    Unique class name per case (the ``external_base`` pattern) so keys from
    one case can never contaminate another. Fields:

    - ``content_index``: BM25Field(source="content") — the lexical signal;
      its presence makes ``retrieval_mode="auto"`` resolve to ``"lexical"``.
    - ``importance``: SortedField — gives every case's model a sorted index
      so the composite control case can rank via ``composite_score``
      (without it, ``composite_score({...})`` has nothing to resolve).
    - No embedding/vector field, no CyclicDecayField, no ExistenceFilter, no
      CoOccurrenceField — the push path and every nondeterministic surface
      stay inert.
    """

    # Built with type() so the metaclass sees the unique name from the start:
    # _meta.db_class_key (record keys, $SortedF index, $BM25 keys) is captured
    # at class-creation time, so a post-hoc __name__ rename (the external_base
    # style) would leave the SortedField index shared across cases —
    # cross-case contamination for the composite control.
    return type(
        f"CsrMem{safe_prefix}",
        (popoto.Model,),
        {
            "__module__": __name__,
            "__qualname__": f"CsrMem{safe_prefix}",
            "memory_key": popoto.AutoKeyField(),
            "agent_id": popoto.KeyField(),
            "content": popoto.StringField(default=""),
            "topic": popoto.StringField(default=""),
            "timestamp": popoto.FloatField(default=0.0),
            "importance": popoto.SortedField(type=float, default=0.5),
            "content_index": BM25Field(source="content"),
        },
    )


def plant(case: CsrTestCase):
    """Plant a case's corpus into the current Redis DB.

    Returns:
        Tuple ``(model_class, agent_id, id_to_record, reverse_map)`` where
        ``reverse_map`` maps ``record.db_key.redis_key -> planted_id`` (the
        translation the runner applies to ``AssemblyResult.records``).

    Raises:
        CsrAuthoringError: On duplicate planted ids, or when the post-plant
            adversarial guard finds zero BM25 hits for the adversarial query.
    """
    corpus_ids = [m.id for m in case.planted_corpus]
    duplicates = {i for i in corpus_ids if corpus_ids.count(i) > 1}
    if duplicates:
        raise CsrAuthoringError(
            f"case {case.case_id!r}: duplicate planted ids {sorted(duplicates)} "
            f"— ids are the assertion key space; collisions corrupt scoring"
        )

    safe_prefix = uuid.uuid4().hex[:8]
    model_class = _build_csr_model_class(safe_prefix)
    agent_id = f"csr{safe_prefix}"

    id_to_record: Dict[str, object] = {}
    reverse_map: Dict[str, str] = {}
    for memory in case.planted_corpus:
        record = model_class(
            agent_id=agent_id,
            content=memory.content,
            topic=memory.topic,
            timestamp=memory.timestamp,
            importance=memory.importance,
        )
        if record.save() is False:
            cleanup(model_class, agent_id)
            raise CsrAuthoringError(
                f"case {case.case_id!r}: save() returned False for "
                f"memory {memory.id!r}"
            )
        id_to_record[memory.id] = record
        reverse_map[record.db_key.redis_key] = memory.id

    # Post-plant adversarial guard: the adversarial query MUST hit the real
    # BM25 index, else the lexical run silently degrades to the query-blind
    # composite fallback and "lexical adversarial" measures the wrong thing.
    hits = BM25Field.search(
        model_class, BM25_FIELD_NAME, case.adversarial_query, limit=1
    )
    if not hits:
        cleanup(model_class, agent_id)
        raise CsrAuthoringError(
            f"case {case.case_id!r}: adversarial query {case.adversarial_query!r} "
            f"gets zero BM25 hits against the planted corpus — author a "
            f"paraphrase sharing >= 1 indexed token with a distractor memory"
        )

    return model_class, agent_id, id_to_record, reverse_map


def cleanup(model_class, agent_id: str) -> None:
    """Delete every Redis key created for one planted case.

    Scans ``*{ClassName}*`` — the unique per-case class name appears in the
    record keys (``CsrMemX:...``), the sorted index (``CsrMemX:_importance``)
    and the BM25 keys (``$BM25:CsrMemX:content_index...``) — then the agent
    prefix as a backstop (mirrors ``external_base.teardown``).
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
