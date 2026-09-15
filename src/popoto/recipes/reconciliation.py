"""M5 reconciliation: claim equivalence classes, typed contradiction rules,
and explicit disjunctions over the provenance journal (#564).

The journal (:mod:`popoto.recipes.provenance_journal`) records every claim an
agent captures, immutably and with full attribution. What it does *not* do is
notice that two entries say the same thing. This module adds that layer: it
groups entries that assert one claim into an **equivalence class**, resolves
typed contradictions inside a class through a per-type precedence table, and
stores a precedence tie as an explicit **disjunct pair** rather than picking an
arbitrary winner.

Three properties shape every design choice here:

**Nothing in this module ever mutates a persisted ``JournalEntry``.**
``JournalEntry`` composes ``AppendOnlyMixin``, whose ``save()`` refuses any
re-save of an existing key -- including a partial ``save(update_fields=[...])``.
So class membership cannot be a field on the entry. It lives in two ordinary
(non-append-only) models this module owns outright, :class:`ClaimMembership`
and :class:`ClaimClass`, where a relabel is an ordinary ``save()``. The only
writes M5 makes are (a) ``claim_type``, set by *capture* before an entry's
first and only ``save()``, (b) new appended annotation entries, and (c) rows in
its own two models.

**The merge log is the source of truth; the two tables are a rebuildable
index.** Every reconcile outcome is appended to the journal as an immutable
``merge`` (or ``disjoin``) annotation carrying the class ids, the rationale,
the timestamp and the convention-book version. :func:`replay` discards the
index and recomputes it from those annotations, which is what makes
reversibility structural rather than a property to be tested into existence:
retract a ``merge`` annotation, replay, and the pre-merge assignment is back. A
crash mid-relabel is a repair, not a corruption.

**One reconciler per agent, processing entries sequentially -- the
single-writer invariant.** This is a *deployment constraint*, not an
implementation detail, and it is what the two documented races take as their
mitigation. The :class:`~popoto.streams.StreamConsumer` on the ``"journal"``
stream is the only production trigger, and being the sole writer is what
*produces* the invariant. :func:`reconcile_entry` is a thin adapter over the
same reconcile function that exists for tests to drive; it is **not** a
production entry point, because a host calling it from concurrent turn handling
has nothing establishing the sequencing. Running a second reconciler per agent
re-opens both races and requires reintroducing an atomic membership claim plus
per-claim-slot serialization.

Example::

    from popoto.recipes.provenance_journal import ProvenanceJournal
    from popoto.recipes.reconciliation import (
        ClaimClass, representative_for, reconciliation_consumer,
    )
    from popoto.streams import StreamConsumer

    # Capture assigns claim_type before the entry's first and only save().
    ProvenanceJournal.append(
        agent_id="a1",
        statement="prefers morning meetings",
        subjects=["dana"],
        claim_type="preference",
    )

    # Production trigger: one consumer per agent, sequential.
    consumer = reconciliation_consumer(agent_id="a1", consumer_name="worker-1")

    # Downstream (M6/M7/M8) reads classes through the ORM.
    for claim_class in ClaimClass.query.filter(agent_id="a1"):
        entry, uncertain = representative_for(claim_class.class_id)
"""

import enum
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field as dataclass_field
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)

from ..fields.constants import Defaults
from ..fields.shortcuts import FloatField, IndexedField, IntField, KeyField
from ..fields.validity_field import ValidityField, ValidityMemberAbsentError
from ..models.base import Model
from ..privacy.never_record import scan_never_record
from ..redis_db import get_REDIS_DB
from .provenance_journal import (
    VALIDITY_FIELD_NAME,
    JournalEntry,
    ProvenanceJournal,
)

from ..extraction._anthropic_compat import assert_messages_create_supported

try:  # pragma: no cover - exercised by environment, not by a branch test
    import anthropic as anthropic_module

    _anthropic_available = True
except ImportError:  # pragma: no cover - same
    # Keep the name bound (to None) even when the optional dependency is
    # absent, so callers/tests can always monkeypatch this module's
    # ``anthropic_module`` regardless of whether `anthropic` is installed.
    # No ``type: ignore`` here, matching the three sibling guards in
    # ``extraction/``: the gate environment installs no `anthropic` extra,
    # so the import resolves to ``Any`` and an ignore would be unused --
    # and ``warn_unused_ignores`` makes an unused one an error.
    anthropic_module = None
    _anthropic_available = False

if TYPE_CHECKING:  # pragma: no cover - typing only
    # Imported for annotations only; the runtime import stays function-local
    # in ``reconciliation_consumer`` to keep module import cheap.
    from ..streams import StreamConsumer

#: One decoded stream batch: ``StreamConsumer`` hands the handler a list of
#: ``(entry_id, fields_dict)`` pairs (``streams/consumer.py:62``).
StreamBatch = Sequence[Tuple[str, Dict[str, Any]]]

logger = logging.getLogger("POPOTO.Reconciliation")


# ---------------------------------------------------------------------------
# Tuning constants
#
# Module-level aliases of ``Defaults`` entries, read by name at call time in
# the functions below. Each one is registered in
# ``tests/benchmarks/overrides.py::MODULE_CONSTANTS`` as
# ``name -> (module, attr)``; ``tests/benchmarks/test_defaults_sync.py`` is the
# gate that reads that registry and is never the place to add an entry.
# ---------------------------------------------------------------------------

M5_SHORTLIST_CAP = Defaults.M5_SHORTLIST_CAP
M5_SYMMETRY_PROBE_ENABLED = Defaults.M5_SYMMETRY_PROBE_ENABLED
M5_JUDGE_MODEL = Defaults.M5_JUDGE_MODEL
M5_JUDGE_MAX_TOKENS = Defaults.M5_JUDGE_MAX_TOKENS
M5_REPLAY_WATERMARK_FIELD = Defaults.M5_REPLAY_WATERMARK_FIELD
MEGA_CLASS_VELOCITY_ALERT = Defaults.MEGA_CLASS_VELOCITY_ALERT


# ---------------------------------------------------------------------------
# Convention book v1
# ---------------------------------------------------------------------------

#: Version string recorded on every merge-log annotation. Changing any line of
#: :data:`CONVENTION_BOOK_V1` is a **version bump, not an edit**: replay pins
#: the wording that produced a merge, so a silent reword would make history
#: irreproducible (Risk 2).
CONVENTION_BOOK_VERSION = "v1"

#: The literal v1 same-claim standard. This is the only thing the judge is
#: prompted with besides the two claims.
#:
#: Rule 8 is the load-bearing one: the judge's default under uncertainty is to
#: *not* merge, so an ambiguous pair costs one extra class (today's behavior)
#: rather than building a mega-class out of compounding false "same" verdicts
#: (Risk 1).
CONVENTION_BOOK_V1 = """Same-claim convention book, v1.

Two claims are the same claim when they assert the same thing about the same \
subject, such that a reader who believed one would consider the other a \
restatement rather than new information. Specifically:

1. Converse phrasings are the same claim. "A reports to B" and "B manages A" \
assert one fact from two directions.
2. Restatements and paraphrases are the same claim, including changes in \
wording, tense, politeness, verbosity, or the presence of hedging.
3. Differences in precision are the same claim when the less precise \
statement is entailed by the more precise one and the claims do not conflict \
("prefers mornings" / "prefers meetings before 10am").
4. Different subjects are never the same claim, even under identical \
predicates.
5. Different values in the same slot are NOT the same claim -- they are a \
conflict, and the type rule decides, not this judge ("prefers mornings" / \
"prefers evenings").
6. Different predicates about one subject are NOT the same claim, however \
related ("lives in Berlin" / "works in Berlin").
7. A claim about a moment and a claim about a pattern are NOT the same claim \
("was late today" / "is often late").
8. When the two claims are not clearly on one side of the rules above, \
answer "different". Abstaining costs one extra class; a wrong "same" merges \
two beliefs irreversibly from the reader's point of view."""


# ---------------------------------------------------------------------------
# Frozen type enum and the precedence table
# ---------------------------------------------------------------------------

#: The frozen v1 claim-type vocabulary. Frozen rather than open because the
#: decidability of the per-type rules dies with an open enum: every type except
#: ``note`` needs a decidable incompatibility rule and a precedence row.
#: ``note`` is the rule-free catch-all that absorbs the tail, which is what
#: lets the enum be frozen without an escape hatch.
CLAIM_TYPES = (
    "preference",
    "deadline",
    "trait",
    "relationship",
    "goal",
    "procedure",
    "note",
)

#: The rule-free catch-all, and the fallback for an entry whose ``claim_type``
#: is ``None`` -- every entry captured before #564 shipped, plus anything a
#: capture path left unlabelled. A ``note`` fires no incompatibility rule and
#: has no precedence row, so it can only ever join, disjoin, or stay a
#: singleton through the judge path.
DEFAULT_CLAIM_TYPE = "note"

#: Types whose deterministic rule is same-target supersession. Membership is
#: not a taste call: it is exactly the set of types for which "the newest
#: assertion is the truth" is correct, which is what makes recency the right
#: per-type order for them and the wrong one for everything else.
SUPERSESSION_FAMILY = frozenset({"deadline"})

#: Types describing standing facts that get restated, where a claim confirmed
#: many times should not lose to a single fresh mention.
STABLE_FAMILY = frozenset({"preference", "trait", "relationship", "goal", "procedure"})

#: ``claim_type -> family``. Complete over :data:`CLAIM_TYPES` by construction
#: (asserted at import below), so a lookup never needs a default.
CLAIM_TYPE_FAMILY = {
    **{name: "supersession" for name in SUPERSESSION_FAMILY},
    **{name: "stable" for name in STABLE_FAMILY},
    "note": "rule_free",
}

#: Per-type ordering applied **after** global Rule 0 (a self-stated claim beats
#: an inferred one -- M1's ``stated`` flag), and applied only once a type rule
#: has fired. This table never decides sameness and never runs on a ``note``.
#:
#: The table is **total**: when Rule 0 ties, the family order ties, and recency
#: ties, the outcome is a disjunct pair rather than a coin flip. That is what
#: makes "no silent winner" a property of the table instead of an assumption
#: about the data.
PRECEDENCE_ORDER = {
    "supersession": ("recency",),
    "stable": ("confirmations", "recency"),
    "rule_free": (),
}

assert set(CLAIM_TYPE_FAMILY) == set(CLAIM_TYPES), (
    "CLAIM_TYPE_FAMILY must cover exactly CLAIM_TYPES -- an uncovered type "
    "would reach precedence with no ordering and pick an arbitrary winner"
)


def normalize_claim_type(claim_type: Optional[str]) -> str:
    """Return a type from :data:`CLAIM_TYPES`, falling back to ``note``.

    Tolerating ``None`` is a requirement, not a convenience: ``claim_type`` is
    write-once-at-capture, so every entry captured before #564 shipped has
    ``None`` and no code path can back-fill it. The reconciler must classify
    those entries rather than skip them or raise. An unrecognized string is
    treated the same way, so a capture path emitting a type outside the frozen
    enum degrades to the rule-free catch-all instead of reaching a precedence
    lookup with no row.
    """
    if isinstance(claim_type, str) and claim_type in CLAIM_TYPES:
        return claim_type
    return DEFAULT_CLAIM_TYPE


# ---------------------------------------------------------------------------
# The two reconciliation-owned models
# ---------------------------------------------------------------------------


class ClaimMembership(Model):
    """One row per reconciled entry: which class it belongs to.

    Keyed by the entry's own Redis key, which gives exactly the identity
    semantics wanted -- one row per entry, no duplicate-membership state to
    reconcile. The colons inside that key are a non-issue: ``DB_key.clean()``
    escapes them before the value reaches the keyspace, so a composite key
    value cannot forge a key boundary.

    A plain ``Model`` deliberately -- **no** ``AppendOnlyMixin``. A relabel on
    merge must be an ordinary ``save()``.

    **Privacy invariant: this row holds a digest, never claim content.**
    ``claim_slot`` is a one-way ``sha256`` of ``agent_id|subject|claim_type``,
    and neither this model nor :class:`ClaimClass` stores the subject string or
    the plaintext ``claim_type``. The reason is the exact scope of
    ``JournalEntry.hard_delete()``: it erases a record and every trace of *its
    own* derived state, and explicitly not "every trace of the record anywhere
    in the keyspace". A plaintext subject on a mutable sibling model would be
    precisely such a field-value copy, sitting outside the reach of the only
    erasure primitive an append-only record has -- and a sharper regression
    than usual, because ``JournalEntry`` also composes ``NeverRecordMixin``,
    i.e. this data is already governed as never-record. Slot *equality* is all
    reconciliation needs for grouping sibling claims, so the digest costs
    nothing here. See :func:`erase_entry` for the cascade that keeps the
    remaining derived state erasable.
    """

    entry_redis_key = KeyField()
    class_id = IndexedField(type=str)
    claim_slot = IndexedField(type=str)
    disjunction_id = IndexedField(type=str, null=True)


class ClaimClass(Model):
    """One row per equivalence class -- the read surface for M6/M7/M8.

    Every read downstream needs is an ordinary indexed ORM query
    (``ClaimClass.query.filter(agent_id=...)``,
    ``ClaimMembership.query.filter(class_id=...)``) rather than an accessor
    wrapping ``HGET``/``SMEMBERS``, which is what makes this a real read
    surface instead of a key convention a reader has to trust.

    Holds no claim content, for the reason given on :class:`ClaimMembership`:
    ``representative_key`` is a Redis key, and the entry it names is where the
    claim text lives.
    """

    class_id = KeyField()
    agent_id = IndexedField(type=str)
    representative_key = IndexedField(type=str)
    member_count = IntField(default=1)
    updated_at = FloatField(null=True)


# ---------------------------------------------------------------------------
# Merge-kind registration -- at module import, deliberately
# ---------------------------------------------------------------------------

#: The annotation kinds this module appends. Registered at import below.
#:
#: ``closing=False`` for both, which is **not** the value
#: ``register_kind``'s own docstring uses in its example: a join or a disjoin
#: does not close anybody's validity interval. Only the
#: deterministic/precedence path closes, and it does so through the journal's
#: ``supersede``, never through a merge kind.
#:
#: ``targetless=False`` (the default) means every merge-log annotation **must**
#: name a target -- ``validate_kind_and_target`` raises ``ValueError`` on a
#: falsy target for a non-targetless kind, from ``pre_save``, so this fails at
#: write time rather than at review time. ``targetless=True`` is not an option:
#: a targetless kind must carry no target at all, which would strand the
#: annotation with nothing to hang off.
MERGE_KINDS = ("merge", "disjoin")

for _kind in MERGE_KINDS:
    JournalEntry.register_kind(_kind, closing=False)
del _kind


# ---------------------------------------------------------------------------
# Claim slots
# ---------------------------------------------------------------------------


def claim_slot(agent_id: str, subject: str, claim_type: Optional[str]) -> str:
    """Return the one-way slot digest for ``(agent_id, subject, claim_type)``.

    32 hex characters of ``sha256``. Computed at reconcile time and stored
    one-way, so the slot supports the equality test grouping needs while
    carrying none of the text -- see :class:`ClaimMembership`'s privacy
    invariant.
    """
    raw = f"{agent_id}|{subject}|{normalize_claim_type(claim_type)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def slot_for_entry(entry: Any) -> str:
    """Return ``entry``'s claim slot, using its first subject tag.

    An entry with no subjects gets the empty subject, which puts every
    untagged claim of one type into one slot. That is deliberate and matches
    ``TagField``'s own zero-tag semantics: such an entry has no identity key to
    compute, so it skips the deterministic tier and reaches the judge with a
    subject-unbounded shortlist bounded by :data:`M5_SHORTLIST_CAP`.
    """
    subjects = list(getattr(entry, "subjects", None) or [])
    subject = str(subjects[0]) if subjects else ""
    return claim_slot(str(entry.agent_id), subject, getattr(entry, "claim_type", None))


# ---------------------------------------------------------------------------
# The sameness judge
#
# The contract is copied from ``extraction/verdict.py``'s ``llm_verdict``:
# firewall before the call, JSON-schema output, re-validate every field, never
# raise. Every failure maps to a logged abstention that leaves the entry
# unclassified (a new singleton class), never to a guessed join.
# ---------------------------------------------------------------------------


class Sameness(str, enum.Enum):
    """The judge's fixed two-value reply vocabulary."""

    SAME = "same"
    DIFFERENT = "different"


#: JSON schema confining the reply to the enum. A first line of defence, not
#: the enforcement point: :func:`_parse_sameness` re-validates regardless,
#: because a provider that ignores or partially honours the schema must still
#: not be able to write free text into a merge decision.
SAMENESS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": sorted(v.value for v in Sameness)},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class SamenessResult:
    """One judge answer, or the reason there isn't one.

    Attributes:
        verdict: The parsed :class:`Sameness`, or ``None`` on an abstention.
        abstained: True when no usable verdict was obtained. An abstention is
            never read as ``different`` on the forward ask (it leaves the entry
            a singleton) and never as ``same`` anywhere.
        reason: A short machine token for the log and the merge-log rationale.
            Never model-authored text.
    """

    verdict: Optional[Sameness]
    abstained: bool
    reason: str

    @property
    def is_same(self) -> bool:
        return self.verdict is Sameness.SAME and not self.abstained


def _default_client() -> Any:
    """Build the default Anthropic client, or raise if unavailable.

    Also rejects an installed-but-too-old SDK, so a missing ``output_config``
    reads as a precise version error in the log rather than a bare
    ``TypeError`` from deep inside the request. :func:`judge_sameness`'s
    blanket handler converts either into the same abstention.
    """
    if not _anthropic_available or anthropic_module is None:
        raise ImportError(
            "anthropic is required to use the M5 sameness judge. "
            "Install it with: pip install popoto[anthropic]"
        )
    client = anthropic_module.Anthropic()
    assert_messages_create_supported(client)
    return client


def _parse_sameness(raw_text: Optional[str]) -> Optional[Sameness]:
    """Parse an untrusted reply into a :class:`Sameness`, or ``None``.

    Re-validates against the fixed vocabulary regardless of the request's JSON
    schema. A reply that is empty, not JSON, not an object, or carries a
    verdict outside the two-value enum is malformed rather than partially
    applied.
    """
    if not raw_text or not raw_text.strip():
        return None
    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return Sameness(parsed.get("verdict"))
    except ValueError:
        return None


def judge_sameness(
    left_statement: str,
    right_statement: str,
    client: Any = None,
) -> SamenessResult:
    """Ask once whether two claims are the same claim. Never raises.

    Order of operations, mirroring ``llm_verdict``:

    1. Either statement blank or whitespace-only -> abstain, **zero calls**.
       There is nothing to compare.
    2. ``scan_never_record`` runs on both statements **before** the call. A
       blocked statement abstains and its text is never transmitted.
    3. Otherwise one call is issued. A malformed, empty or out-of-vocabulary
       reply, an unreachable provider, and a raising client all abstain.

    Args:
        left_statement: The first claim. Order matters -- see
            :func:`_judge_pair`, which re-asks with the order swapped.
        right_statement: The second claim.
        client: An Anthropic-style client (anything exposing
            ``messages.create``). ``None`` builds the default client, which
            requires the optional ``anthropic`` package.

    Returns:
        A :class:`SamenessResult` carrying an enum or an abstention -- never
        any model-authored text.
    """
    if not (left_statement or "").strip() or not (right_statement or "").strip():
        logger.debug("sameness: blank statement -> abstain(empty_statement)")
        return SamenessResult(None, True, "empty_statement")

    for statement in (left_statement, right_statement):
        firewall = scan_never_record(statement)
        if firewall.blocked:
            # The reason code only -- never a fragment of the blocked text.
            logger.info(
                "sameness: statement blocked by never-record firewall (%s)",
                firewall.reason,
            )
            return SamenessResult(None, True, "firewall_drop")

    try:
        if client is None:
            client = _default_client()
        response = client.messages.create(
            model=M5_JUDGE_MODEL,
            max_tokens=M5_JUDGE_MAX_TOKENS,
            system=CONVENTION_BOOK_V1,
            messages=[
                {
                    "role": "user",
                    "content": json.dumps(
                        {"claim_a": left_statement, "claim_b": right_statement}
                    ),
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": SAMENESS_SCHEMA}
            },
        )
        raw_text = next(
            (block.text for block in response.content if block.type == "text"),
            None,
        )
    except Exception as e:
        logger.warning("sameness: judge call failed: %s", e)
        return SamenessResult(None, True, "llm_unavailable")

    verdict = _parse_sameness(raw_text)
    if verdict is None:
        logger.warning("sameness: malformed reply -> abstain(llm_unavailable)")
        return SamenessResult(None, True, "llm_unavailable")
    return SamenessResult(verdict, False, verdict.value)


@dataclass
class _JudgeBudget:
    """Call counter, so the AC5 bound is observable rather than asserted."""

    calls: int = 0


def _judge_pair(
    entry_statement: str,
    representative_statement: str,
    client: Any,
    budget: _JudgeBudget,
) -> SamenessResult:
    """Forward ask plus the swapped-order symmetry probe.

    Judge verdicts are not transitive, and compounding false "same" verdicts
    into a mega-class is the top threat to this design (Risk 1). So the forward
    ask compares the entry against the class representative in that order, and
    a forward "same" is re-asked **once with the claim order swapped**. The
    join commits only on same/same; **any** split -- forward-same then
    probe-different, or a probe abstention -- returns ``different`` so the
    caller routes to a disjunct pair instead of a join.

    Cost is bounded at two calls per candidate class, which with the shortlist
    cap gives AC5's "at most 2x the shortlist cap per entry". Full N-way
    transitivity closure is a No-Go: quadratic judge calls for no additional
    safety over this probe.
    """
    budget.calls += 1
    forward = judge_sameness(entry_statement, representative_statement, client)
    if not forward.is_same:
        return forward
    if not M5_SYMMETRY_PROBE_ENABLED:
        return forward

    budget.calls += 1
    probe = judge_sameness(representative_statement, entry_statement, client)
    if probe.is_same:
        return forward
    # A split verdict is not "different" in the ordinary sense -- it is
    # detected non-transitivity, and the caller turns it into explicit
    # uncertainty rather than a silent non-merge.
    reason = "probe_abstained" if probe.abstained else "probe_split"
    logger.info("sameness: symmetry probe split the verdict (%s)", reason)
    return SamenessResult(Sameness.DIFFERENT, False, reason)


# ---------------------------------------------------------------------------
# Reconciler-side embedding cache and shortlist
#
# ``JournalEntry`` gains no ``EmbeddingField`` (D4). This holds the ownership
# line the append-only remedy already draws -- M5 owns derived state, M1 owns
# the record -- and keeps ``claim_type`` the only new field on the entry. An
# ``EmbeddingField`` would have been *legal* under append-only, since it
# populates on the first and only ``save()``; its absence is a scope decision,
# recorded so nobody "fixes" it by adding the field.
# ---------------------------------------------------------------------------

#: Redis hash holding cached statement vectors, ``entry_redis_key -> JSON``.
#: Reconciler-owned derived state, not a source of truth: a cache loss costs a
#: re-embed, behind a correctness-preserving fallback.
EMBEDDING_CACHE_KEY = "POPOTO:M5:embedding_cache"


def _embedding_provider() -> Any:
    """Return the configured default embedding provider, or ``None``."""
    try:
        from ..fields.embedding_field import get_default_provider

        return get_default_provider()
    except Exception:  # pragma: no cover - import-time environment failure
        return None


def cached_embedding(entry: Any, provider: Any = None) -> Optional[List[float]]:
    """Return ``entry``'s statement vector, embedding and caching on a miss.

    ``None`` when no provider is available or the provider fails, which is the
    signal :func:`shortlist_candidates` uses to take its index-scan fallback.
    """
    redis_key = entry.pk
    client = get_REDIS_DB()
    raw = client.hget(EMBEDDING_CACHE_KEY, redis_key)
    if raw:
        try:
            return list(json.loads(raw))
        except (ValueError, TypeError):
            logger.warning("embedding cache: undecodable entry, re-embedding")

    if provider is None:
        provider = _embedding_provider()
    if provider is None:
        return None

    statement = str(getattr(entry, "statement", "") or "")
    if not statement.strip():
        return None
    try:
        vectors = provider.embed([statement], input_type="document")
    except Exception as e:
        logger.warning("embedding provider failed, falling back to scan: %s", e)
        return None
    if not vectors or not vectors[0]:
        return None
    vector = [float(v) for v in vectors[0]]
    client.hset(EMBEDDING_CACHE_KEY, redis_key, json.dumps(vector))
    return vector


def drop_cached_embedding(redis_key: str) -> None:
    """Delete one entry's cached vector.

    Part of :func:`erase_entry`'s cascade: an embedding is a lossy encoding of
    ``statement``, so the cache is content-derived state in a store
    ``hard_delete()`` does not reach.
    """
    get_REDIS_DB().hdel(EMBEDDING_CACHE_KEY, redis_key)


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity, 0.0 on a zero vector or a dimension mismatch."""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def shortlist_candidates(
    entry: Any,
    *,
    exclude_class_ids: Sequence[str] = (),
    provider: Any = None,
) -> List[str]:
    """Return at most :data:`M5_SHORTLIST_CAP` candidate class ids for ``entry``.

    Ranked by cosine similarity between the entry's statement vector and each
    class representative's, over the same-agent classes. When no embedding
    provider is available -- or the provider fails -- this degrades to a
    bounded same-subject + same-type index scan: recall narrows, every
    correctness property holds, and the judge-call bound is unchanged (Risk 4).
    """
    cap = int(M5_SHORTLIST_CAP)
    if cap <= 0:
        return []
    excluded = set(exclude_class_ids)
    classes = [
        row
        for row in ClaimClass.query.filter(agent_id=str(entry.agent_id))
        if row.class_id not in excluded
    ]
    if not classes:
        return []

    entry_vector = cached_embedding(entry, provider)
    if entry_vector is None:
        # Degraded path: same slot first (an exact index equality), then any
        # remaining same-agent class, both bounded by the cap.
        slot = slot_for_entry(entry)
        slot_classes = {
            row.class_id for row in ClaimMembership.query.filter(claim_slot=slot)
        }
        ordered = [row.class_id for row in classes if row.class_id in slot_classes]
        ordered += [row.class_id for row in classes if row.class_id not in slot_classes]
        return ordered[:cap]

    scored: List[Tuple[float, str]] = []
    for row in classes:
        representative = JournalEntry.query.get(redis_key=row.representative_key)
        if representative is None:
            continue
        other = cached_embedding(representative, provider)
        if other is None:
            continue
        scored.append((_cosine(entry_vector, other), row.class_id))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [class_id for _score, class_id in scored[:cap]]


# ---------------------------------------------------------------------------
# Representative discipline
# ---------------------------------------------------------------------------


def _live_members(class_id: str) -> List[Any]:
    """Return a class's validity-open member entries.

    Validity-closed losers stay in-class for audit but are excluded from
    representative selection **and** from confirmation counts: a superseded
    claim should not be what the judge compares against, nor should its
    corroboration keep counting for a claim that is no longer believed.

    The loop is inverted -- membership rows first, then a per-member validity
    probe -- rather than hydrating ``filter(validity__current=True)`` and
    intersecting. The intersecting shape read *every* validity-open
    ``JournalEntry`` in the keyspace, for every agent, and discarded all but
    one class's members; ``_reconcile`` reaches here once per candidate class
    plus once per :func:`_recompute_class`, so a single reconcile cost up to
    ~9 full-journal scans. ``is_valid_at`` is two ``ZSCORE``s against the
    interval ZSETs, so the cost is now proportional to the class, not to the
    journal.
    """
    live: List[Any] = []
    for row in ClaimMembership.query.filter(class_id=class_id):
        if not ValidityField.is_valid_at(
            JournalEntry, VALIDITY_FIELD_NAME, row.entry_redis_key
        ):
            continue
        entry = JournalEntry.query.get(redis_key=row.entry_redis_key)
        if entry is not None:
            live.append(entry)
    return live


def confirmation_count(entry: Any) -> int:
    """Return ``entry``'s corroboration count.

    Derived by counting ``confirm`` annotations, never stored on the record:
    ``ProvenanceJournal.confirm`` appends a new annotation and leaves the
    target untouched, which is what makes corroboration append-only-safe.
    """
    return sum(
        1
        for annotation in ProvenanceJournal.annotations_for(entry)
        if annotation.kind == "confirm"
    )


def representative_for(class_id: str) -> Tuple[Optional[Any], bool]:
    """Return ``(representative_entry, uncertainty_flag)`` for a class.

    The representative is the class's **most-confirmed member among
    validity-open entries only**, ties broken by recency. That is what the
    judge is always shown, and it is M5's guarantee to M6: one representative
    per class is *selectable*.

    The second element is the uncertainty marker, and it is returned **here**,
    from M5's own selection call, rather than left to a reader's formatting
    layer: it is True when any live member of the class carries an open
    ``disjunction_id``, i.e. the class holds a precedence tie no winner was
    picked for. A reader that ignores the flag degrades to showing a
    representative; it cannot be handed a silent winner, because there isn't
    one to hand.

    Returns:
        ``(None, False)`` for an empty or fully-closed class.
    """
    live = _live_members(class_id)
    if not live:
        return None, False
    live.sort(
        key=lambda e: (confirmation_count(e), float(e.captured_at or 0.0)),
        reverse=True,
    )
    uncertain = any(
        bool(row.disjunction_id)
        for row in ClaimMembership.query.filter(class_id=class_id)
    )
    return live[0], uncertain


# ---------------------------------------------------------------------------
# Precedence resolution
# ---------------------------------------------------------------------------

#: Returned by :func:`resolve_precedence` when every column ties.
PRECEDENCE_TIE = "tie"


def resolve_precedence(
    challenger: Any, incumbent: Any, claim_type: str
) -> Tuple[Optional[Any], Optional[Any], str]:
    """Resolve a fired type rule into ``(winner, loser, basis)``.

    Applied **only** after a type rule fires: this never decides sameness, and
    it never runs on a ``note``.

    Global **Rule 0** is evaluated first for every type -- a self-stated claim
    beats an inferred one, consuming M1's ``stated`` flag -- and the per-type
    row applies only when both claims agree on ``stated``. Then the family
    order from :data:`PRECEDENCE_ORDER`: recency for the supersession family
    (``deadline``), confirmation count then recency for the stable family.

    The table is total. When Rule 0 ties, the family order ties, and recency
    ties, this returns ``(None, None, PRECEDENCE_TIE)`` and the caller stores a
    disjunct pair -- never an arbitrary winner.
    """
    family = CLAIM_TYPE_FAMILY.get(claim_type, "rule_free")
    if family == "rule_free":
        return None, None, PRECEDENCE_TIE

    # Rule 0, global and first.
    challenger_stated = bool(challenger.stated)
    incumbent_stated = bool(incumbent.stated)
    if challenger_stated != incumbent_stated:
        if challenger_stated:
            return challenger, incumbent, "rule0_stated"
        return incumbent, challenger, "rule0_stated"

    # Both columns are compared as floats: ``confirmation_count`` returns a
    # small int that is exactly representable, and recency is a timestamp.
    # The comparison never crosses columns, so the widening loses nothing.
    left: float
    right: float
    for column in PRECEDENCE_ORDER[family]:
        if column == "confirmations":
            left = confirmation_count(challenger)
            right = confirmation_count(incumbent)
        else:  # "recency"
            left = float(challenger.captured_at or 0.0)
            right = float(incumbent.captured_at or 0.0)
        if left > right:
            return challenger, incumbent, column
        if right > left:
            return incumbent, challenger, column

    return None, None, PRECEDENCE_TIE


# ---------------------------------------------------------------------------
# Merge log
# ---------------------------------------------------------------------------


def _merge_payload(
    *,
    class_a: str,
    class_b: str,
    rationale: str,
    slot: str,
    disjunction_id: str = "",
    other: str = "",
) -> str:
    """Build a merge-log annotation's ``payload``.

    Carries class ids, a machine rationale token, the timestamp, the
    convention-book version, and the one-way slot digest -- no claim content,
    by the same invariant :class:`ClaimMembership` holds to. ``slot`` is here
    because :func:`replay` has to rebuild the membership row's ``claim_slot``
    from the log alone; the log being sufficient to rebuild the index is what
    makes reversibility structural.
    """
    return json.dumps(
        {
            "class_a": class_a,
            "class_b": class_b,
            "rationale": rationale,
            "ts": time.time(),
            "judge_version": CONVENTION_BOOK_VERSION,
            "slot": slot,
            "disjunction_id": disjunction_id,
            "other": other,
        },
        sort_keys=True,
    )


def _append_merge_log(
    entry: Any,
    *,
    class_a: str,
    class_b: str = "",
    rationale: str,
    slot: str,
    disjunction_id: str = "",
    other: str = "",
    kind: str = "merge",
) -> Any:
    """Append one immutable merge-log annotation targeting ``entry``.

    ``target`` is the entry the record is *about*: the joining entry for a
    ``merge``, one side of the pair for a ``disjoin`` (with the other side and
    the shared disjunction id in the payload). Both kinds are non-targetless,
    so a missing target would raise from ``pre_save``.

    The JSON goes to ``payload``, **not** ``statement``. Every value in it is
    Popoto-generated -- class ids, a disjunction id, entry keys -- and
    ``statement`` is scanned by the never-record firewall, whose Luhn rule
    matches any 13-19 digit run. A uuid4 hex trips it ~0.23% of the time and a
    merge-log write carries three or more, so routing this through ``statement``
    raised ``JournalBlockedError`` on ~0.66% of writes: red on CI's Valkey job,
    green on Redis, same commit. ``payload`` is exempt because Popoto generates
    all of it; see :meth:`JournalEntry._never_record_scan_values`, which
    carries the measurement and the #589 precedent.
    """
    return ProvenanceJournal.append(
        agent_id=str(entry.agent_id),
        kind=kind,
        target=entry,
        payload=_merge_payload(
            class_a=class_a,
            class_b=class_b,
            rationale=rationale,
            slot=slot,
            disjunction_id=disjunction_id,
            other=other,
        ),
    ).entry


def merge_log_entries(agent_id: str) -> List[Any]:
    """Return an agent's live merge-log annotations, oldest first.

    Only ``validity__current=True`` annotations are returned, which is what
    makes AC4 work: retracting a ``merge`` annotation closes its interval, so
    the next :func:`replay` no longer sees it and reproduces the pre-merge
    assignment.
    """
    live: List[Any] = []
    for kind in MERGE_KINDS:
        live.extend(
            JournalEntry.query.filter(
                agent_id=agent_id, kind=kind, validity__current=True
            )
        )
    live.sort(key=lambda e: float(e.captured_at or 0.0))
    return live


def _decode_payload(entry: Any) -> Optional[Dict[str, Any]]:
    """Decode a merge-log annotation's payload, or ``None`` if unreadable.

    Reads ``payload``, the firewall-exempt machine field ``_append_merge_log``
    writes. Do not fall back to ``statement``: no merge-log annotation has ever
    been persisted with the JSON there (M5 ships in one PR with the field), so
    a fallback would be dead code that also re-legitimises the scanned field as
    a payload home.
    """
    try:
        parsed = json.loads(str(entry.payload or ""))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# The reconcile loop
# ---------------------------------------------------------------------------


@dataclass
class ReconcileOutcome:
    """What one reconcile pass did to one entry.

    Attributes:
        entry_key: The reconciled entry's Redis key.
        class_id: The class it belongs to afterwards.
        action: One of ``created``, ``confirmed``, ``joined``, ``superseded``,
            ``loser-absent``, ``disjoined``, ``noop``.
        judge_calls: Calls the judge actually issued, so AC5's bound is
            observable rather than asserted.
        disjunction_id: The shared id when this pass stored a disjunct pair.
        superseded_key: The loser's key when a type rule resolved.
    """

    entry_key: str
    class_id: str
    action: str
    judge_calls: int = 0
    disjunction_id: str = ""
    superseded_key: str = ""
    merged_class_ids: List[str] = dataclass_field(default_factory=list)


def _new_class_id() -> str:
    return uuid.uuid4().hex


def _touch_class(
    class_id: str, agent_id: str, representative_key: str, member_count: int
) -> Any:
    row = ClaimClass(
        class_id=class_id,
        agent_id=agent_id,
        representative_key=representative_key,
        member_count=member_count,
        updated_at=time.time(),
    )
    row.save()
    return row


def _recompute_class(class_id: str) -> Optional[Any]:
    """Recompute a class's row from its membership, dropping it if empty.

    Reselects ``representative_key`` and ``member_count``. Returns the row, or
    ``None`` when the class is gone.
    """
    rows = list(ClaimMembership.query.filter(class_id=class_id))
    existing = ClaimClass.query.get(class_id=class_id)
    if not rows:
        if existing is not None:
            existing.delete()
        return None
    representative, _uncertain = representative_for(class_id)
    if representative is None:
        # Every member is validity-closed. The class still exists for audit;
        # name any member so the row never points at nothing.
        representative_key = rows[0].entry_redis_key
        agent_id = existing.agent_id if existing is not None else ""
    else:
        representative_key = representative.pk
        agent_id = str(representative.agent_id)
    return _touch_class(class_id, str(agent_id), representative_key, len(rows))


def _relabel_class(loser: str, winner: str) -> None:
    """Move every membership row from ``loser`` to ``winner``.

    Safe under the single-writer invariant and idempotent on re-run: a row
    already relabeled matches the winner's filter instead, so a crash
    mid-relabel is a repair (rerun replay for the class), not a corruption.
    """
    for row in ClaimMembership.query.filter(class_id=loser):
        row.class_id = winner
        row.save()
    stale = ClaimClass.query.get(class_id=loser)
    if stale is not None:
        stale.delete()


def _record_membership(
    entry: Any, class_id: str, slot: str, disjunction_id: str = ""
) -> Any:
    row = ClaimMembership(
        entry_redis_key=entry.pk,
        class_id=class_id,
        claim_slot=slot,
        disjunction_id=disjunction_id or None,
    )
    row.save()
    return row


def _store_disjunction(entry: Any, other: Any, slot: str, class_id: str) -> str:
    """Store a precedence tie or a split verdict as an explicit disjunct pair.

    The ``disjoin`` annotation is authoritative and is the complete history; the
    two ``ClaimMembership`` rows carry the shared id so a reader gets the pair
    with one indexed query. v1 simplification: a membership row holds the *most
    recent* open disjunction for its entry, so an entry disjoined a second time
    repoints it -- the full set of pairs stays recoverable from the ``disjoin``
    annotations, which is what replay reads.
    """
    disjunction_id = uuid.uuid4().hex
    _append_merge_log(
        entry,
        class_a=class_id,
        rationale="disjoined",
        slot=slot,
        disjunction_id=disjunction_id,
        other=other.pk,
        kind="disjoin",
    )
    for side in (entry, other):
        row = ClaimMembership.query.get(entry_redis_key=side.pk)
        if row is not None:
            row.disjunction_id = disjunction_id
            row.save()
    return disjunction_id


def _mega_class_telemetry(class_id: str, joins: int) -> None:
    """Emit the class-size-velocity signal. Telemetry only, never a gate.

    Risk 1's structural barrier is the symmetry probe; this is the second line
    that reports damage the probe let through. It deliberately does not refuse
    the join: a legitimately large class must not be blocked, so a threshold
    that gated would be a correctness bug of its own.
    """
    if joins > int(MEGA_CLASS_VELOCITY_ALERT):
        logger.warning(
            "mega-class velocity: class %s absorbed %d joins in one pass "
            "(alert threshold %s) -- telemetry only, the join stands",
            class_id,
            joins,
            MEGA_CLASS_VELOCITY_ALERT,
        )


def _supersede_loser(
    winner: Any, loser: Any, class_id: str, slot: str, basis: str
) -> Tuple[str, str]:
    """Close the loser's interval through the journal. Returns (action, key).

    Every supersession outcome goes through the journal's ``supersede``, which
    appends a ``supersede`` annotation and closes the target's interval in one
    MULTI/EXEC via ``SupersessionProtocol``. That is the *only* supersession
    mechanism M5 uses -- and it has to be this one rather than
    ``save_and_supersede`` applied to the winner, because the winner is already
    persisted and re-saving it would raise ``AppendOnlyViolation``, which is the
    same rule as "nothing M5 writes ever mutates a persisted entry".

    ``ValidityMemberAbsentError`` is caught, not allowed to crash and not
    silently swallowed: a loser whose live membership closed between the
    shortlist read and this write is re-read, and if it is no longer a live
    validity member the merge log records ``loser-absent`` and the winner
    stands. The condition is a **closed membership with the hash still
    present** -- nothing deletes here, since the journal is append-only -- so
    the detection is this caught exception and never an ``EXISTS`` check, which
    would pass and hide the case.
    """
    try:
        ProvenanceJournal.supersede(
            loser,
            agent_id=str(winner.agent_id),
            statement=str(winner.statement or ""),
        )
    except ValidityMemberAbsentError as e:
        logger.info(
            "reconcile: loser %s left live membership before the close (%s)",
            loser.pk,
            e,
        )
        _append_merge_log(
            winner,
            class_a=class_id,
            rationale="loser-absent",
            slot=slot,
            other=loser.pk,
        )
        return "loser-absent", loser.pk
    _append_merge_log(
        winner,
        class_a=class_id,
        rationale=f"superseded:{basis}",
        slot=slot,
        other=loser.pk,
    )
    return "superseded", loser.pk


def _reconcile(
    entry: Any,
    *,
    client: Any = None,
    provider: Any = None,
) -> ReconcileOutcome:
    """Reconcile one entry. The single reconcile function; see module docstring.

    Idempotent: an entry that already holds a ``ClaimMembership`` row returns a
    ``noop``, which is what makes a crash-and-rerun of the same writer safe.
    """
    entry_key = entry.pk
    existing = ClaimMembership.query.get(entry_redis_key=entry_key)
    if existing is not None:
        return ReconcileOutcome(entry_key, existing.class_id, "noop")

    claim_type = normalize_claim_type(getattr(entry, "claim_type", None))
    slot = slot_for_entry(entry)
    budget = _JudgeBudget()
    joins = 0

    # --- Deterministic tier. An exact slot-equality lookup, before any
    # embedding work: a sibling committed earlier in the same pass is found by
    # index rather than by similarity, which is what makes the single-writer
    # invariant *sufficient* against Race 3 rather than merely hopeful.
    slot_rows = list(ClaimMembership.query.filter(claim_slot=slot))
    slot_class_ids: List[str] = []
    for row in slot_rows:
        if row.class_id not in slot_class_ids:
            slot_class_ids.append(row.class_id)

    if slot_class_ids and claim_type in SUPERSESSION_FAMILY:
        # Singleton-slot type: a same-slot collision *is* a conflict by
        # definition, so the rule fires with **zero judge calls** and the
        # precedence table resolves it. The judge is the escalation path for
        # claims normalization cannot equate -- not for ones it can.
        class_id = slot_class_ids[0]
        incumbents = _live_members(class_id)
        _record_membership(entry, class_id, slot)
        # The row and the annotation that reconstructs it are written
        # together, unconditionally. :func:`replay` rebuilds the index from
        # the log alone, so a membership row with no annotation naming this
        # entry as a member is a row a from-genesis rebuild silently drops --
        # and the outcome-specific annotations appended below do not name it:
        # ``disjoin`` only repoints an existing row, and ``_supersede_loser``
        # targets the *winner*, which on an incumbent win is not this entry.
        _append_merge_log(entry, class_a=class_id, rationale="joined", slot=slot)
        if not incumbents:
            _recompute_class(class_id)
            return ReconcileOutcome(entry_key, class_id, "joined", budget.calls)
        incumbent = incumbents[0]
        winner, loser, basis = resolve_precedence(entry, incumbent, claim_type)
        if winner is None:
            disjunction_id = _store_disjunction(entry, incumbent, slot, class_id)
            _recompute_class(class_id)
            return ReconcileOutcome(
                entry_key,
                class_id,
                "disjoined",
                budget.calls,
                disjunction_id=disjunction_id,
            )
        action, loser_key = _supersede_loser(winner, loser, class_id, slot, basis)
        _recompute_class(class_id)
        return ReconcileOutcome(
            entry_key, class_id, action, budget.calls, superseded_key=loser_key
        )

    # --- Judge path. Same-slot classes first (they are the ones a type rule
    # can fire against), then the embedding shortlist for cross-slot sameness
    # such as a converse phrasing that names the subjects the other way round.
    candidate_class_ids = list(slot_class_ids)
    if len(candidate_class_ids) < int(M5_SHORTLIST_CAP):
        candidate_class_ids += shortlist_candidates(
            entry, exclude_class_ids=candidate_class_ids, provider=provider
        )
    candidate_class_ids = candidate_class_ids[: int(M5_SHORTLIST_CAP)]

    statement = str(getattr(entry, "statement", "") or "")
    joined_class_id: Optional[str] = None
    merged: List[str] = []

    for class_id in candidate_class_ids:
        representative, _uncertain = representative_for(class_id)
        if representative is None:
            continue
        verdict = _judge_pair(
            statement, str(representative.statement or ""), client, budget
        )

        if verdict.is_same:
            if joined_class_id is None:
                joined_class_id = class_id
                _record_membership(entry, class_id, slot)
                ProvenanceJournal.confirm(representative, agent_id=str(entry.agent_id))
                _append_merge_log(
                    entry, class_a=class_id, rationale="confirmed", slot=slot
                )
                joins += 1
                _mega_class_telemetry(class_id, joins)
            else:
                # A second "same" means these two classes are the same class.
                _relabel_class(class_id, joined_class_id)
                merged.append(class_id)
                _append_merge_log(
                    entry,
                    class_a=joined_class_id,
                    class_b=class_id,
                    rationale="joined",
                    slot=slot,
                )
                joins += 1
                _mega_class_telemetry(joined_class_id, joins)
            continue

        if verdict.abstained:
            # An abstention is never read as a verdict either way; the entry
            # simply stays unclassified against this candidate.
            continue

        if verdict.reason in ("probe_split", "probe_abstained"):
            # Detected non-transitivity -> explicit uncertainty, never a join.
            if joined_class_id is None:
                joined_class_id = _new_class_id()
                _record_membership(entry, joined_class_id, slot)
                _touch_class(joined_class_id, str(entry.agent_id), entry.pk, 1)
                # Same rebuildability rule as the deterministic tier above:
                # the ``disjoin`` annotation appended next repoints rows, it
                # does not create them.
                _append_merge_log(
                    entry, class_a=joined_class_id, rationale="joined", slot=slot
                )
            disjunction_id = _store_disjunction(
                entry, representative, slot, joined_class_id
            )
            _recompute_class(joined_class_id)
            return ReconcileOutcome(
                entry_key,
                joined_class_id,
                "disjoined",
                budget.calls,
                disjunction_id=disjunction_id,
            )

        # A clean "different" inside the same slot is a *conflict* by
        # convention-book rule 5, so the type rule fires and precedence
        # decides. Across slots it is simply an unrelated claim.
        if class_id in slot_class_ids and claim_type in STABLE_FAMILY:
            if joined_class_id is None:
                joined_class_id = class_id
                _record_membership(entry, class_id, slot)
                # Same rebuildability rule as the deterministic tier above.
                _append_merge_log(
                    entry, class_a=class_id, rationale="joined", slot=slot
                )
            winner, loser, basis = resolve_precedence(entry, representative, claim_type)
            if winner is None:
                disjunction_id = _store_disjunction(
                    entry, representative, slot, joined_class_id
                )
                _recompute_class(joined_class_id)
                return ReconcileOutcome(
                    entry_key,
                    joined_class_id,
                    "disjoined",
                    budget.calls,
                    disjunction_id=disjunction_id,
                )
            action, loser_key = _supersede_loser(
                winner, loser, joined_class_id, slot, basis
            )
            _recompute_class(joined_class_id)
            return ReconcileOutcome(
                entry_key,
                joined_class_id,
                action,
                budget.calls,
                superseded_key=loser_key,
            )

    if joined_class_id is not None:
        _recompute_class(joined_class_id)
        for stale in merged:
            _recompute_class(stale)
        return ReconcileOutcome(
            entry_key,
            joined_class_id,
            "joined",
            budget.calls,
            merged_class_ids=merged,
        )

    # Nothing matched: a new singleton class. This is also where every
    # abstention lands, which is the point -- a judge failure costs one extra
    # class (today's behavior) and never a guessed join.
    class_id = _new_class_id()
    _record_membership(entry, class_id, slot)
    _touch_class(class_id, str(entry.agent_id), entry_key, 1)
    _append_merge_log(entry, class_a=class_id, rationale="created", slot=slot)
    return ReconcileOutcome(entry_key, class_id, "created", budget.calls)


def reconcile_entry(
    entry: Any, *, client: Any = None, provider: Any = None
) -> ReconcileOutcome:
    """Reconcile one entry directly. **Test-only** -- not a production path.

    A thin adapter over the same reconcile function the stream consumer drives.
    One loop, one production trigger, never two pipelines.

    This is deliberately **not** documented as a host-facing API. The
    single-writer invariant that Races 1 and 3 take as their mitigation is not
    a property of the reconcile function; it is *produced by* the consumer
    being the sole writer, one reconciler per agent processing entries
    sequentially. A consumer-less host calling this from concurrent turn
    handling has nothing establishing that sequencing, which re-opens exactly
    the concurrent-join hazard the invariant covers. Use
    :func:`reconciliation_consumer` in production.
    """
    return _reconcile(entry, client=client, provider=provider)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay(
    agent_id: str,
    *,
    since: Optional[float] = None,
    rebuild: bool = False,
) -> int:
    """Rebuild the class index from the merge log. Returns entries replayed.

    The merge log is the source of truth and these two tables are a rebuildable
    index derived from it, so there is no sidecar to keep in sync: a divergent
    index is discarded and recomputed rather than reconciled against the log.
    That is what makes AC4's reversibility structural -- retract a ``merge``
    annotation, replay, and the pre-merge assignment is back, because
    :func:`merge_log_entries` only reads live annotations.

    Args:
        agent_id: The agent whose index to rebuild.
        since: Replay only annotations strictly newer than this instant
            (``captured_at``, the field named by
            :data:`M5_REPLAY_WATERMARK_FIELD`, filtered with a strict ``>``,
            borrowing ``crystallize``'s watermark shape). ``None`` replays from
            genesis, which is an explicit repair operation rather than the
            steady state.
        rebuild: Delete this agent's existing index rows first. Required for a
            true from-genesis rebuild; without it a replay is additive.
    """
    if rebuild:
        for row in ClaimClass.query.filter(agent_id=agent_id):
            for member in ClaimMembership.query.filter(class_id=row.class_id):
                member.delete()
            row.delete()

    replayed = 0
    touched: List[str] = []
    for annotation in merge_log_entries(agent_id):
        watermark = float(getattr(annotation, M5_REPLAY_WATERMARK_FIELD) or 0.0)
        if since is not None and not watermark > since:
            continue
        payload = _decode_payload(annotation)
        if payload is None:
            logger.warning(
                "replay: undecodable merge-log payload on %s, skipped",
                annotation.pk,
            )
            continue
        class_a = str(payload.get("class_a") or "")
        class_b = str(payload.get("class_b") or "")
        slot = str(payload.get("slot") or "")
        if not class_a:
            continue

        if annotation.kind == "disjoin":
            disjunction_id = str(payload.get("disjunction_id") or "")
            for key in (str(annotation.target or ""), str(payload.get("other") or "")):
                row = ClaimMembership.query.get(entry_redis_key=key)
                if row is not None:
                    row.disjunction_id = disjunction_id or None
                    row.save()
            replayed += 1
            continue

        target_key = str(annotation.target or "")
        if target_key:
            row = ClaimMembership.query.get(entry_redis_key=target_key)
            if row is None:
                ClaimMembership(
                    entry_redis_key=target_key,
                    class_id=class_a,
                    claim_slot=slot,
                ).save()
            else:
                row.class_id = class_a
                row.claim_slot = slot or row.claim_slot
                row.save()
        if class_b and class_b != class_a:
            _relabel_class(class_b, class_a)
            if class_b not in touched:
                touched.append(class_b)
        if class_a not in touched:
            touched.append(class_a)
        replayed += 1

    for class_id in touched:
        _recompute_class(class_id)
    return replayed


# ---------------------------------------------------------------------------
# Erasure cascade
# ---------------------------------------------------------------------------


def erase_entry(entry: Any) -> bool:
    """Erase a reconciled entry and every trace of M5's derived state.

    **Use this, not** ``JournalEntry.hard_delete()`` **directly**, for a
    reconciled entry. ``hard_delete()`` is the retention/erasure primitive and
    its documented scope is the record plus every trace of *its own* derived
    state -- explicitly **not** "every trace of the record anywhere in the
    keyspace". M5 adds derived state the primitive therefore does not reach, so
    a bare ``hard_delete()`` leaves a dangling membership row and, worse, a
    :class:`ClaimClass` whose ``representative_key`` points at an erased key.

    Four legs, in order:

    1. ``JournalEntry.hard_delete()`` on the entry.
    2. Delete its :class:`ClaimMembership` row.
    3. Delete its cached reconciler-side embedding -- an embedding is a lossy
       encoding of ``statement``, so the cache is content-derived state in a
       store ``hard_delete()`` does not reach.
    4. Recompute the affected :class:`ClaimClass`: reselect
       ``representative_key`` and decrement ``member_count``, or drop the row
       when the class is left empty.

    The membership row is survivable at all only because it carries the
    one-way ``claim_slot`` digest and no claim content -- which matters
    because ``JournalEntry`` also composes ``NeverRecordMixin``, so this data
    is governed as never-record.
    """
    redis_key = entry.pk
    row = ClaimMembership.query.get(entry_redis_key=redis_key)
    class_id = row.class_id if row is not None else ""

    erased = JournalEntry.hard_delete(entry)
    if row is not None:
        row.delete()
    drop_cached_embedding(redis_key)
    if class_id:
        _recompute_class(class_id)
    return bool(erased)


# ---------------------------------------------------------------------------
# Production trigger: the StreamConsumer on the "journal" stream
# ---------------------------------------------------------------------------

#: The journal's Redis stream key. ``JournalEntry`` is deliberately
#: unpartitioned -- ``StreamConsumer`` takes exactly one ``stream_key`` and has
#: no partition-discovery mechanism -- so ``agent_id`` rides in the stream
#: metadata and a consumer filters on it without hydrating the record.
JOURNAL_STREAM_KEY = f"stream:{JournalEntry._stream_name}"


def make_reconciliation_handler(
    agent_id: Optional[str] = None,
    *,
    client: Any = None,
    provider: Any = None,
) -> Callable[[StreamBatch], Awaitable[None]]:
    """Build the ``StreamConsumer`` handler that is M5's production trigger.

    Entries are processed **sequentially** inside one handler call, and the
    deployment contract is one consumer per agent. That sequencing is what
    produces the single-writer invariant: entry A's membership row is committed
    before entry B is shortlisted, so the interleaved shortlist-to-commit span
    Race 3 needs never occurs.

    Args:
        agent_id: When given, only this agent's entries are reconciled --
            filtered on the stream's ``agent_id`` metadata field, without
            hydrating the record.
        client: Judge client, forwarded to the reconcile function.
        provider: Embedding provider, forwarded to the shortlist.
    """

    async def reconciliation_handler(entries: StreamBatch) -> None:
        """Reconcile each ``assert`` capture the journal stream announces."""
        if not entries:
            return
        for stream_id, fields in entries:
            if fields.get("op") not in ("create", None, ""):
                continue
            if fields.get("kind") not in ("assert", None, ""):
                # Annotations -- including M5's own merge-log appends -- are
                # not claims to reconcile. Skipping them here is also what
                # keeps the consumer from reconciling its own writes.
                continue
            if agent_id is not None and fields.get("agent_id") != agent_id:
                continue
            redis_key = fields.get("pk") or ""
            if not redis_key:
                continue
            entry = JournalEntry.query.get(redis_key=redis_key)
            if entry is None:
                logger.debug(
                    "reconcile: stream entry %s names no readable record",
                    stream_id,
                )
                continue
            try:
                _reconcile(entry, client=client, provider=provider)
            except Exception:
                # Re-raised after logging: the consumer's retry and
                # dead-letter machinery is the right place to decide, and
                # swallowing here would drop a claim silently.
                logger.exception(
                    "reconcile: failed on %s from stream entry %s",
                    redis_key,
                    stream_id,
                )
                raise

    return reconciliation_handler


def reconciliation_consumer(
    *,
    agent_id: Optional[str] = None,
    consumer_name: str,
    group_name: str = "m5-reconciler",
    client: Any = None,
    provider: Any = None,
) -> "StreamConsumer":
    """Build the journal-stream consumer. **One per agent** -- see the docstring.

    Running a second reconciler for one agent is a deployment error, not a
    state this code arbitrates: it re-opens Races 1 and 3 and requires
    reintroducing an atomic membership claim plus per-claim-slot serialization
    at the same time. Whoever proposes the second writer owns that change.
    """
    from ..streams import StreamConsumer

    return StreamConsumer(
        stream_key=JOURNAL_STREAM_KEY,
        group_name=group_name,
        consumer_name=consumer_name,
        handler=make_reconciliation_handler(agent_id, client=client, provider=provider),
    )


__all__ = [
    "CLAIM_TYPES",
    "CLAIM_TYPE_FAMILY",
    "CONVENTION_BOOK_V1",
    "CONVENTION_BOOK_VERSION",
    "DEFAULT_CLAIM_TYPE",
    "MERGE_KINDS",
    "PRECEDENCE_ORDER",
    "PRECEDENCE_TIE",
    "STABLE_FAMILY",
    "SUPERSESSION_FAMILY",
    "ClaimClass",
    "ClaimMembership",
    "ReconcileOutcome",
    "Sameness",
    "SamenessResult",
    "claim_slot",
    "confirmation_count",
    "erase_entry",
    "judge_sameness",
    "make_reconciliation_handler",
    "merge_log_entries",
    "normalize_claim_type",
    "reconcile_entry",
    "reconciliation_consumer",
    "replay",
    "representative_for",
    "resolve_precedence",
    "shortlist_candidates",
    "slot_for_entry",
]
