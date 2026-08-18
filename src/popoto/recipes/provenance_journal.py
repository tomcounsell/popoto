"""Provenance journal -- append-only entries with confirm/supersede/retract (#560).

An agent is told, in a thread, "Tom said the launch slipped to the 30th." Two
turns later Tom says "the 30th is wrong, it's the 27th." Stored as ordinary
memory rows, the second statement overwrites the first: the original words are
gone, nobody recorded *who* said either one, and there is no way to ask what
the agent believed last Tuesday.

This module is the substrate that fixes that. Every capture is an immutable
:class:`JournalEntry`; every correction is a *new* entry pointing at the one it
corrects. Membership in the live belief set comes from the validity indexes
(#580), not from a chain walk, so ``JournalEntry.query.filter(
validity__current=True)`` is the working set and superseded entries stay fully
readable by key and by ``validity__as_of=<earlier>``.

::

    from popoto.recipes import ProvenanceJournal, JournalEntry

    first = ProvenanceJournal.append(
        agent_id="agent-1",
        speaker="tom",
        turn_id="t-41",
        verbatim="the launch slipped to the 30th",
        statement="Launch date is the 30th",
        subjects=["launch"],
    ).entry

    ProvenanceJournal.supersede(
        first,
        agent_id="agent-1",
        speaker="tom",
        turn_id="t-43",
        verbatim="actually the 30th is wrong, it's the 27th",
        statement="Launch date is the 27th",
    )

    JournalEntry.query.filter(validity__current=True)  # the correction only
    ProvenanceJournal.annotations_for(first)           # the correction, again
    ProvenanceJournal.chain(first)                     # [first, correction]

Field choices and why
---------------------

``entry_id`` (AutoKeyField)
    Immutable UUID identity, assigned at ``__init__`` -- so the record's Redis
    key is concrete *before* save, which is what lets the append-only guard
    check the right key, and what makes two independent appends unable to
    collide.
``agent_id`` (KeyField)
    Partition key, mirroring ``DefaultMemory``. Required non-null by
    :meth:`ProvenanceJournal.append`: a ``None`` renders the literal string
    ``"None"`` into the record key.
``captured_at`` (FloatField)
    Wall-clock capture time of the source turn. **Deliberately not named
    ``ingested_at``.** ``ValidityField.on_save`` hardcodes the *save clock*
    into its own ``ingested_at`` ZSET and ignores any model field, so two
    fields under one name would silently disagree and downstream readers would
    get different answers depending on which they read. The validity ingest
    axis is always the save clock; this field is the source turn's clock.
``turn_id`` (IndexedField)
    "Everything from turn T" in one query.
``speaker`` (IndexedField)
    "Everything attributed to S" in one query. Attribution, not authentication.
``verbatim`` (StringField)
    The exact source span. Privacy-sensitive -- the reason ``NeverRecordMixin``
    is mandatory on this model rather than optional.
``statement`` (StringField)
    The atomic natural-language claim distilled from the span.
``subjects`` (TagField)
    Multi-value: one entry can concern several people or topics. Convention
    over schema and explicitly **not** a security boundary, inheriting
    ``TagField``'s framing verbatim.
``stated`` (BooleanField)
    Stated (True) vs inferred (False). Downstream conflict resolution uses it
    for precedence; this module only stores it.
``kind`` (IndexedField)
    ``assert`` / ``confirm`` / ``supersede`` / ``retract``, validated against
    :attr:`Defaults.JOURNAL_KINDS`. Indexed so "every retraction" is one query.
``target`` (IndexedField)
    The annotated entry's Redis key. A plain indexed scalar rather than a
    ``Relationship``: the target is already addressed by Redis key, and
    ``Relationship``'s lazy-load machinery plus its heavier save buys nothing
    here. Annotations are 1:1 with a target by design -- a correction spanning
    several claims is N annotation entries.
``validity`` (ValidityField)
    The ``valid_from`` / ``invalid_at`` / ``ingested_at`` axes. This module
    owns the valid-time axis; the interval and chain state itself stays owned
    by ``ValidityField`` as derived index state.

One record type, not two
------------------------
Annotations are ``JournalEntry`` rows distinguished by ``kind`` + ``target``,
not a separate model. Annotations must themselves be annotatable (a retraction
of a mistaken retraction; a confirmation of a supersession), the validity chain
hashes are keyed by Redis key and are type-agnostic, and a single keyspace
keeps the live-membership query one ``filter()`` instead of a union of two.
The cost is honest and paid for explicitly: ``kind``/``target`` consistency is
runtime validation (in :meth:`JournalEntry.pre_save` and again in the
pre-flight), not a type-system guarantee.

Querying, with one sharp edge
-----------------------------
``filter(validity__current=True)`` and ``filter(validity__as_of=t)`` are the
supported validity lookups. ``filter(validity=t)`` -- a bare exact-value filter
on the field -- silently returns **nothing**: ``ValidityField.filter_query``
handles only the two suffixed params. ``target``, ``kind``, ``speaker`` and
``turn_id`` collide with nothing (the reserved field names are only ``limit``,
``order_by`` and ``values``).

Atomicity, stated precisely
---------------------------
``supersede``/``retract`` queue the annotation's save and the target's interval
close into **one** ``MULTI``/``EXEC``. The property that holds is: *no
interleaving reader observes the annotation without the close.* The property
that does **not** hold, and is not claimed, is rollback -- Redis ``MULTI/EXEC``
does not roll back sibling commands when one command errors at execute time. A
command-level error inside ``EXEC`` can therefore leave the annotation appended
with the target still open. The pre-flight below exists to make that window
unreachable in practice; the residual is a documented boundary, not an
impossibility claim.

Immutability, stated precisely
------------------------------
It is an **ORM-layer contract**, enforced by ``AppendOnlyMixin`` against every
Python write path in ``models/base.py``. It does not hold against a raw Redis
client. Two TOCTOU shapes are known and documented rather than closed: two
processes saving the same key concurrently, and two saves of the same key
queued onto one pipeline (the guard's ``EXISTS`` cannot see a queued-but-
unexecuted command). See :mod:`popoto.fields.append_only`.

What blocked content leaves behind
----------------------------------
"Nothing is destroyed" is scoped to what gets *stored*. A capture blocked by
the never-record firewall is dropped before storage and leaves only a
content-free tombstone in the ``$NR:`` keyspace, which is not part of the
journal and is returned by no journal query. The journal-side signal is a gap
in ``turn_id`` coverage, nothing more; no placeholder entry is written.

Deliberately **not** included
-----------------------------
``EmbeddingField``
    It requires an embedding provider (an API key or a local Ollama), so it
    cannot be a zero-configuration default, and similarity search over the
    journal is not this module's job. The extension point is explicit rather
    than a gap -- subclass and add one::

        class SearchableEntry(JournalEntry):
            statement_vec = EmbeddingField(source="statement")

    Subclassing also gives the subclass its own keyspace
    (``SearchableEntry:*``) and its own validity/chain keys, and is the same
    seam ``_journal_kinds`` uses to extend the annotation vocabulary.

All numeric behavior is left to the field defaults and to
``popoto.fields.constants.Defaults``; this module pins no constants of its own
beyond the stream bound noted on the class.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional, Sequence, Union

import redis.client

from ..exceptions import JournalBlockedError
from ..fields.constants import Defaults
from ..fields.append_only import AppendOnlyMixin
from ..fields.event_stream import EventStreamMixin
from ..fields.shortcuts import (
    AutoKeyField,
    BooleanField,
    FloatField,
    IndexedField,
    KeyField,
    StringField,
    TagField,
)
from ..fields.supersession import SupersessionProtocol
from ..fields.validity_field import ValidityField
from ..models.base import Model
from ..privacy.never_record import NeverRecordMixin, scan_never_record
from ..redis_db import POPOTO_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from redis.client import Pipeline

logger = logging.getLogger("POPOTO.ProvenanceJournal")

#: Name of the ``ValidityField`` on :class:`JournalEntry`. Referenced by every
#: ``ValidityField`` key helper call in this module, so a rename is one edit.
VALIDITY_FIELD_NAME = "validity"

#: Kinds that carry no ``target``. Everything else in the vocabulary annotates
#: exactly one entry.
_TARGETLESS_KINDS = frozenset({"assert"})

#: Kinds whose annotation closes the target's validity interval.
_CLOSING_KINDS = frozenset({"supersede", "retract"})

#: Set once the first uncoupled ``supersede``/``retract`` has warned, so the
#: degraded mode is announced exactly once per process rather than per call.
_UNCOUPLED_WARNED: "set[str]" = set()


@dataclass(frozen=True)
class AnnotationResult:
    """Outcome of one journal write, readable without touching Redis.

    This type exists specifically so the validity-coupling kill switch cannot
    reproduce the silent-no-op shape recorded in #588: a caller must be able to
    tell "the target was closed" from "the target was not closed" without
    issuing a read.

    Attributes:
        entry: The appended :class:`JournalEntry`.
        target_closed: Whether the target's validity interval was closed. On a
            capture (``append``) and on ``confirm`` this is always False by
            design -- neither changes membership. On ``supersede``/``retract``
            it is False when the coupling switch is off. When the journal owned
            the pipeline it reflects the script's actual reply (so a
            concurrently-closed target correctly reports False); when the
            *caller* supplied the pipeline nothing has executed yet, and it
            reports that the close was queued.
        coupling_enabled: The state of
            :attr:`Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED` at write time.
        pipeline: The caller-supplied pipeline, returned unexecuted, or
            ``None`` when the journal owned and executed its own.
    """

    entry: "JournalEntry"
    target_closed: bool
    coupling_enabled: bool
    pipeline: Optional[Any] = None


class JournalEntry(AppendOnlyMixin, NeverRecordMixin, EventStreamMixin, Model):
    """One immutable, fully attributed provenance record.

    Both a capture and an annotation: ``kind="assert"`` with no ``target`` is
    an original claim; ``confirm``/``supersede``/``retract`` with a ``target``
    annotate another entry. See the module docstring for the field rationale.

    Write through :class:`ProvenanceJournal` rather than constructing entries
    directly -- the façade owns the pre-flight validation and the
    one-transaction annotate-and-close sequence, and is the documented seam a
    future refactor stays behind.

    Example::

        JournalEntry.query.filter(turn_id="t-41")
        JournalEntry.query.filter(kind="retract", validity__current=True)
    """

    entry_id = AutoKeyField()
    agent_id = KeyField()
    captured_at = FloatField(null=True)
    turn_id = IndexedField(type=str, null=True)
    speaker = IndexedField(type=str, null=True)
    verbatim = StringField(default="")
    statement = StringField(default="")
    subjects = TagField(null=True)
    stated = BooleanField(default=True)
    kind = IndexedField(type=str, null=True)
    target = IndexedField(type=str, null=True)
    validity = ValidityField()

    _stream_name = "journal"
    # Deliberately unpartitioned. ``StreamConsumer`` takes exactly one
    # ``stream_key`` and has no partition-discovery mechanism, so a stream
    # partitioned by ``agent_id`` would be a channel its own named consumer
    # cannot read. ``agent_id`` rides in the metadata instead, so a consumer
    # can filter without hydrating the record.
    _stream_partition_field = None
    _stream_metadata_fields = ("agent_id", "kind", "target")
    # Pinned, not configurable. 10k approximate entries is the mixin's own
    # default and is a notification channel bound, not a retention policy --
    # the journal itself is the durable record, and a consumer that falls
    # more than 10k mutations behind must re-read the journal rather than
    # replay the stream. Routing this through ``Defaults`` would add a
    # sync-test exemption and buy nothing, since ``EventStreamMixin`` already
    # exposes it as a per-model class attribute.
    _stream_max_length = 10000

    #: Annotation vocabulary override for subclasses. Empty means "use
    #: :attr:`Defaults.JOURNAL_KINDS`". A subclass that sets it must name a
    #: **superset** of the core four (validated in ``__init_subclass__``), so
    #: extending the vocabulary can never quietly drop a core kind.
    _journal_kinds: tuple = ()

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Validate a subclass's ``_journal_kinds`` override at definition time."""
        super().__init_subclass__(**kwargs)
        declared = cls.__dict__.get("_journal_kinds")
        if not declared:
            return
        missing = set(Defaults.JOURNAL_KINDS) - set(declared)
        if missing:
            raise ValueError(
                f"{cls.__name__}._journal_kinds must be a superset of the core "
                f"vocabulary {tuple(Defaults.JOURNAL_KINDS)}; missing "
                f"{sorted(missing)}. Extend the vocabulary, never narrow it -- "
                f"a reader that meets an unknown kind treats it as inert for "
                f"membership, but a missing core kind changes what supersedes."
            )

    @classmethod
    def journal_kinds(cls) -> "tuple[str, ...]":
        """Return this model's annotation vocabulary.

        The subclass override when one is declared, else
        :attr:`Defaults.JOURNAL_KINDS` read at call time (so a runtime
        assignment to ``Defaults`` is honored rather than frozen at import).
        """
        return tuple(cls._journal_kinds) or tuple(Defaults.JOURNAL_KINDS)

    def pre_save(self, *args: Any, **kwargs: Any) -> Any:
        """Validate ``kind`` and the ``kind``/``target`` combination, then save.

        Defence in depth behind :class:`ProvenanceJournal`'s pre-flight: the
        façade validates before it issues or queues anything, and this catches
        an entry constructed directly.

        Raises:
            ValueError: If ``kind`` is outside the model's vocabulary, if an
                ``assert`` entry carries a ``target``, or if an annotation
                entry carries none.
        """
        validate_kind_and_target(type(self), self.kind, self.target)
        return super().pre_save(*args, **kwargs)


def validate_kind_and_target(
    model: type, kind: Any, target: Any
) -> "tuple[str, Optional[str]]":
    """Check one ``kind``/``target`` pair against ``model``'s vocabulary.

    Shared by :meth:`JournalEntry.pre_save` and :class:`ProvenanceJournal`'s
    pre-flight so the two cannot drift apart.

    Args:
        model: The :class:`JournalEntry` class (or subclass) being written.
        kind: The candidate kind.
        target: The candidate target Redis key, or ``None``.

    Returns:
        The validated ``(kind, target)`` pair.

    Raises:
        ValueError: On an out-of-vocabulary kind or an inconsistent pairing.
    """
    vocabulary = model.journal_kinds()
    if kind not in vocabulary:
        raise ValueError(
            f"{model.__name__}.kind must be one of {vocabulary}, got {kind!r}"
        )
    if kind in _TARGETLESS_KINDS:
        if target:
            raise ValueError(
                f"{model.__name__}: a {kind!r} entry is an original capture and "
                f"must not carry a target (got {target!r})"
            )
        return kind, None
    if not target:
        raise ValueError(
            f"{model.__name__}: a {kind!r} entry annotates another entry and "
            f"must name a target"
        )
    return kind, target


class ProvenanceJournal:
    """Stateless façade over :class:`JournalEntry` -- the only supported API.

    Every method is a classmethod; there is no instance state. Point a
    subclass at a :class:`JournalEntry` subclass to get your own keyspace or
    an extended kind vocabulary::

        class ProjectEntry(JournalEntry):
            pass

        class ProjectJournal(ProvenanceJournal):
            entry_model = ProjectEntry

    Treating this as the sole read and write API is a stated contract, not a
    style preference: the single-record-type decision is cheaply reversible
    only while the keyspace is empty (validity and chain keys are namespaced
    per model, and every record's Redis key embeds its class name), so a future
    split has to stay behind this façade.
    """

    #: The entry model this façade reads and writes.
    entry_model: type = JournalEntry

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    @classmethod
    def append(
        cls,
        *,
        agent_id: str,
        statement: str = "",
        verbatim: str = "",
        speaker: Optional[str] = None,
        turn_id: Optional[str] = None,
        subjects: Optional[Sequence[str]] = None,
        stated: bool = True,
        captured_at: Optional[float] = None,
        at: Optional[float] = None,
        kind: str = "assert",
        target: Optional[Union[str, "JournalEntry"]] = None,
        pipeline: Optional["Pipeline"] = None,
    ) -> AnnotationResult:
        """Append one capture to the journal.

        Args:
            agent_id: Partition key. Required and non-null -- a ``None`` would
                render the literal ``"None"`` into the record's Redis key.
            statement: The atomic claim. Required unless ``verbatim`` is given.
            verbatim: The exact source span.
            speaker: Who said it. Attribution, not authentication.
            turn_id: Conversation turn the capture came from.
            subjects: Subject tags. ``None`` or ``[]`` puts the entry in the
                untagged pool, mirroring ``TagField``'s zero-tag semantics.
            stated: True when stated outright, False when inferred.
            captured_at: Wall-clock time of the source turn. Defaults to now.
            at: Valid-from instant for the entry. Defaults to now. Set at
                construction rather than passed to the supersede script -- see
                the ``supersede`` implementation note on #588.
            kind: Normally left at ``"assert"``. An extended vocabulary
                (``_journal_kinds`` on a subclass) is reachable here, with the
                same ``kind``/``target`` consistency rules.
            target: Only for a non-``assert`` kind. A
                :class:`JournalEntry` or its Redis key.
            pipeline: Optional caller pipeline. Must be transactional. When
                supplied it is returned unexecuted on
                :attr:`AnnotationResult.pipeline`.

        Returns:
            AnnotationResult: with ``target_closed=False`` -- a capture never
            changes another entry's membership.

        Raises:
            JournalBlockedError: If any content is refused by the never-record
                firewall. Nothing is issued or queued.
            ValueError: On a missing ``agent_id``, empty content, a bad
                ``kind``/``target`` pairing, a nonexistent or cross-agent
                target, a backdated ``at``, or a non-transactional pipeline.
            AppendOnlyViolation: If the record's Redis key already exists.
        """
        if kind in _TARGETLESS_KINDS and not (statement or "").strip():
            if not (verbatim or "").strip():
                raise ValueError(
                    f"{cls.entry_model.__name__}: an entry with neither a "
                    f"statement nor a verbatim span is not a provenance record"
                )
        return cls._write(
            agent_id=agent_id,
            kind=kind,
            target=target,
            statement=statement,
            verbatim=verbatim,
            speaker=speaker,
            turn_id=turn_id,
            subjects=subjects,
            stated=stated,
            captured_at=captured_at,
            at=at,
            pipeline=pipeline,
        )

    @classmethod
    def confirm(
        cls,
        target: Union[str, "JournalEntry"],
        *,
        agent_id: str,
        statement: str = "",
        verbatim: str = "",
        speaker: Optional[str] = None,
        turn_id: Optional[str] = None,
        subjects: Optional[Sequence[str]] = None,
        stated: bool = True,
        captured_at: Optional[float] = None,
        at: Optional[float] = None,
        pipeline: Optional["Pipeline"] = None,
    ) -> AnnotationResult:
        """Append a ``confirm`` annotation. Membership is unaffected.

        A confirmation is evidence, not a membership change: the target keeps
        its open interval, and :attr:`AnnotationResult.target_closed` is always
        False. Downstream readers use the annotation count as corroboration.

        Args:
            target: The entry being confirmed, or its Redis key.
            agent_id: See :meth:`append`. Must match the target's agent.
            statement: See :meth:`append`. May be empty for an annotation.
            verbatim: See :meth:`append`.
            speaker: See :meth:`append`.
            turn_id: See :meth:`append`.
            subjects: See :meth:`append`.
            stated: See :meth:`append`.
            captured_at: See :meth:`append`.
            at: Valid-from instant. Must not precede the target's stored
                ``valid_from``.
            pipeline: See :meth:`append`.

        Returns:
            AnnotationResult: with ``target_closed=False``.

        Raises:
            JournalBlockedError: If content is refused by the firewall.
            ValueError: On a nonexistent, unsaved or cross-agent target, a
                backdated ``at``, or a non-transactional pipeline.
        """
        return cls._write(
            agent_id=agent_id,
            kind="confirm",
            target=target,
            statement=statement,
            verbatim=verbatim,
            speaker=speaker,
            turn_id=turn_id,
            subjects=subjects,
            stated=stated,
            captured_at=captured_at,
            at=at,
            pipeline=pipeline,
        )

    @classmethod
    def supersede(
        cls,
        target: Union[str, "JournalEntry"],
        *,
        agent_id: str,
        statement: str = "",
        verbatim: str = "",
        speaker: Optional[str] = None,
        turn_id: Optional[str] = None,
        subjects: Optional[Sequence[str]] = None,
        stated: bool = True,
        captured_at: Optional[float] = None,
        at: Optional[float] = None,
        pipeline: Optional["Pipeline"] = None,
    ) -> AnnotationResult:
        """Append a ``supersede`` annotation and close the target's interval.

        The annotation entry and the target's interval close are queued into
        one ``MULTI``/``EXEC``, so no interleaving reader sees the annotation
        without the close. Rollback is not claimed -- see the module docstring.

        Args:
            target: The entry being corrected, or its Redis key.
            agent_id: See :meth:`append`. Must match the target's agent.
            statement: The corrected claim.
            verbatim: The exact words of the correction.
            speaker: See :meth:`append`.
            turn_id: See :meth:`append`.
            subjects: See :meth:`append`.
            stated: See :meth:`append`.
            captured_at: See :meth:`append`.
            at: The instant the correction becomes true, and the instant the
                target's interval closes. Defaults to now. Must not precede
                the target's stored ``valid_from``.
            pipeline: See :meth:`append`.

        Returns:
            AnnotationResult: ``target_closed`` is False when the coupling
            switch is off, or when the target was already closed by a
            concurrent annotation (which is correct: both annotations are real
            provenance, one close applies).

        Raises:
            JournalBlockedError: If content is refused by the firewall. The
                target is **not** closed and no chain link is written.
            ValueError: On a nonexistent, unsaved or cross-agent target, a
                backdated ``at``, or a non-transactional pipeline.
        """
        return cls._write(
            agent_id=agent_id,
            kind="supersede",
            target=target,
            statement=statement,
            verbatim=verbatim,
            speaker=speaker,
            turn_id=turn_id,
            subjects=subjects,
            stated=stated,
            captured_at=captured_at,
            at=at,
            pipeline=pipeline,
        )

    @classmethod
    def retract(
        cls,
        target: Union[str, "JournalEntry"],
        *,
        agent_id: str,
        statement: str = "",
        verbatim: str = "",
        speaker: Optional[str] = None,
        turn_id: Optional[str] = None,
        subjects: Optional[Sequence[str]] = None,
        stated: bool = True,
        captured_at: Optional[float] = None,
        at: Optional[float] = None,
        pipeline: Optional["Pipeline"] = None,
    ) -> AnnotationResult:
        """Append a ``retract`` annotation and close the target's interval.

        Mechanically identical to :meth:`supersede`; the difference is
        semantic. A supersession replaces a claim with a better one, a
        retraction withdraws it with no replacement, and both remove the target
        from live membership while leaving it fully readable historically.

        Args:
            target: The entry being withdrawn, or its Redis key.
            agent_id: See :meth:`append`. Must match the target's agent.
            statement: Optional -- a retraction may be wordless.
            verbatim: See :meth:`append`.
            speaker: See :meth:`append`.
            turn_id: See :meth:`append`.
            subjects: See :meth:`append`.
            stated: See :meth:`append`.
            captured_at: See :meth:`append`.
            at: The instant the target stops being true. Defaults to now.
            pipeline: See :meth:`append`.

        Returns:
            AnnotationResult: see :meth:`supersede`.

        Raises:
            JournalBlockedError: If content is refused by the firewall.
            ValueError: On a nonexistent, unsaved or cross-agent target, a
                backdated ``at``, or a non-transactional pipeline.
        """
        return cls._write(
            agent_id=agent_id,
            kind="retract",
            target=target,
            statement=statement,
            verbatim=verbatim,
            speaker=speaker,
            turn_id=turn_id,
            subjects=subjects,
            stated=stated,
            captured_at=captured_at,
            at=at,
            pipeline=pipeline,
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    @classmethod
    def annotations_for(cls, entry: Union[str, "JournalEntry"]) -> list:
        """Return every entry annotating ``entry``, in one ``filter()`` call.

        Args:
            entry: The annotated entry, or its Redis key.

        Returns:
            list: Matching :class:`JournalEntry` instances, in index order.
            Empty for an unsaved or unannotated entry.
        """
        target_key = _resolve_member_key(entry)
        if not target_key:
            return []
        return cls.entry_model.query.filter(target=target_key)

    @classmethod
    def chain(cls, entry: Union[str, "JournalEntry"]) -> list:
        """Return the supersession chain through ``entry``, oldest first.

        A display and replay-verification read, **not** the membership query:
        live membership comes from
        ``filter(validity__current=True)`` with no chain walk at all. This
        walks the validity chain hashes from any member outward, so the chain
        is recoverable from its head, its tail, or anywhere in between.

        Args:
            entry: Any member of the chain, or its Redis key.

        Returns:
            list: :class:`JournalEntry` instances ordered oldest -> newest.
            ``[entry]`` when it has no chain links; ``[]`` for an unsaved entry
            or an unresolvable key.
        """
        instance = entry
        if isinstance(entry, str):
            instance = cls.entry_model.query.get(redis_key=entry)
            if instance is None:
                return []
        return SupersessionProtocol.chain(instance, VALIDITY_FIELD_NAME)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @classmethod
    def _write(
        cls,
        *,
        agent_id: str,
        kind: str,
        target: Optional[Union[str, "JournalEntry"]],
        statement: str,
        verbatim: str,
        speaker: Optional[str],
        turn_id: Optional[str],
        subjects: Optional[Sequence[str]],
        stated: bool,
        captured_at: Optional[float],
        at: Optional[float],
        pipeline: Optional["Pipeline"],
    ) -> AnnotationResult:
        """Run the pre-flight, then append (and optionally close) in one go.

        The single write path behind every public mutating method, so the
        pre-flight cannot be bypassed by adding a method.
        """
        model = cls.entry_model
        instant = _coerce_instant(at)
        subject_tags = list(subjects or [])

        # ---- D7 pre-flight. Every check below raises BEFORE a single
        # mutating command is issued or queued. The reads it performs
        # (target existence, the target's stored valid_from) are the point:
        # each one is a question that cannot be answered after the fact
        # without having already written something.

        # 1. The firewall, scanned here rather than left to NeverRecordMixin.
        #    Two reasons. ``Model.save()`` returns the *pipeline* when the
        #    firewall fires in pipeline mode -- indistinguishable from success
        #    -- so a naive annotate-and-close would queue the invalidate EVAL
        #    against an annotation that was never written and commit a
        #    membership change with zero provenance. And the mixin's scan
        #    surface yields only ``str`` values, so ``subjects`` (a list) is
        #    never scanned by it at all.
        _scan_or_block(statement, verbatim, speaker, turn_id, *subject_tags)

        # 2. Vocabulary and kind/target consistency.
        target_key = _resolve_member_key(target)
        kind, target_key = validate_kind_and_target(model, kind, target_key)

        if not agent_id:
            raise ValueError(
                f"{model.__name__}: agent_id is required and must be non-empty "
                f"-- a None renders the literal 'None' into the record key"
            )

        if target_key is not None:
            # 3. The target exists, and belongs to this agent. ``target`` is a
            #    full Redis key that could name another agent's partition, so a
            #    cross-agent annotation is rejected rather than silently
            #    closing a neighbour's record.
            if not POPOTO_REDIS_DB.exists(target_key):
                raise ValueError(
                    f"{model.__name__}: annotation target {target_key!r} does "
                    f"not exist. Save the target before annotating it."
                )
            stored_target = model.query.get(redis_key=target_key)
            if stored_target is None:
                raise ValueError(
                    f"{model.__name__}: annotation target {target_key!r} is not "
                    f"a readable {model.__name__} record"
                )
            if str(stored_target.agent_id) != str(agent_id):
                raise ValueError(
                    f"{model.__name__}: cross-agent annotation refused -- "
                    f"target belongs to agent {stored_target.agent_id!r}, "
                    f"annotation to {agent_id!r}"
                )

            # 4. The requested instant is not before the target's *stored*
            #    valid_from. This cannot be delegated to
            #    ``execute_supersede``: its CLOSE_BEFORE_START -> ValueError
            #    remap applies only on the non-pipeline branch, and its
            #    client-side pre-check compares close_at against the
            #    caller-supplied valid_from, which is the same instant here --
            #    so it never fires. Without this pre-read, a genuine backdate
            #    surfaces as a raw ResponseError from execute() with the
            #    annotation already written.
            valid_from_key, _ = ValidityField.get_interval_keys(
                model, VALIDITY_FIELD_NAME
            )
            stored_valid_from = POPOTO_REDIS_DB.zscore(valid_from_key, target_key)
            if stored_valid_from is not None and instant < float(stored_valid_from):
                raise ValueError(
                    f"{model.__name__}: annotation instant {instant} precedes "
                    f"the target's valid_from ({float(stored_valid_from)}). A "
                    f"zero-or-negative-length interval is a caller bug, not a "
                    f"state to store."
                )

        # 5. A non-transactional caller pipeline would silently void the
        #    atomicity guarantee, so it is refused rather than honored.
        if pipeline is not None:
            if not isinstance(pipeline, redis.client.Pipeline):
                raise ValueError(
                    f"{model.__name__}: pipeline must be a redis Pipeline, got "
                    f"{type(pipeline).__name__}"
                )
            if pipeline.transaction is not True:
                raise ValueError(
                    f"{model.__name__}: pipeline(transaction=False) voids the "
                    f"annotate-and-close atomicity guarantee. Use "
                    f"popoto.get_redis().pipeline() (transactional by default)."
                )

        # ---- End of pre-flight. From here on, commands are issued.

        coupling_enabled = bool(Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED)
        should_close = kind in _CLOSING_KINDS and target_key is not None
        if should_close and not coupling_enabled:
            _warn_uncoupled_once(model.__name__)

        entry = model(
            agent_id=agent_id,
            captured_at=time.time() if captured_at is None else float(captured_at),
            turn_id=turn_id,
            speaker=speaker,
            verbatim=verbatim,
            statement=statement,
            subjects=subject_tags,
            stated=stated,
            kind=kind,
            target=target_key,
            # Valid-time is set at CONSTRUCTION, not passed to the supersede
            # script. ValidityField.on_save uses the field value as valid_from
            # and its ZADD NX runs earlier in the pipeline, which makes the
            # supersede script's own valid_from write a silent no-op -- so a
            # backdated instant passed only to execute_supersede is silently
            # replaced by the save clock (#588).
            validity=instant,
        )

        owns_pipeline = pipeline is None
        pipe = POPOTO_REDIS_DB.pipeline() if owns_pipeline else pipeline

        entry.save(pipeline=pipe)

        close_index: Optional[int] = None
        if should_close and coupling_enabled:
            close_index = len(pipe.command_stack)
            # execute_supersede, NOT SupersessionProtocol.invalidate (#588):
            # SupersessionProtocol resolves its member keys through
            # ``POPOTO_REDIS_DB.exists(...)``, and the successor's HSET is only
            # *queued* at this point, so EXISTS returns 0, the call takes its
            # "unsaved successor -> no-op" branch, and returns None -- which is
            # indistinguishable from its normal pipeline-mode return. The
            # target would stay open with no error anywhere. Do not "simplify"
            # this back to the protocol; it is a read-side API here.
            ValidityField.execute_supersede(
                model,
                VALIDITY_FIELD_NAME,
                new_member=entry.db_key.redis_key,
                mode="invalidate",
                now=instant,
                valid_from=instant,
                ingested_at=instant,
                close_at=instant,
                old_member=target_key,
                pipeline=pipe,
            )

        if not owns_pipeline:
            # The caller owns execution, so nothing has run yet. Report the
            # close as queued rather than confirmed, and hand the pipeline back
            # unexecuted.
            return AnnotationResult(
                entry=entry,
                target_closed=bool(should_close and coupling_enabled),
                coupling_enabled=coupling_enabled,
                pipeline=pipe,
            )

        results = pipe.execute()
        target_closed = False
        if close_index is not None and close_index < len(results):
            # SUPERSEDE_LUA returns the closed member key, or '' when its
            # idempotency guard found the target already closed (Race 3: two
            # annotations closing one target -- both entries are real
            # provenance, exactly one close applies).
            target_closed = bool(results[close_index])
        return AnnotationResult(
            entry=entry,
            target_closed=target_closed,
            coupling_enabled=coupling_enabled,
            pipeline=None,
        )


def _coerce_instant(at: Optional[float]) -> float:
    """Return ``at`` as epoch seconds, or now.

    Coerced here rather than left to the field: ``ValidityField.on_save``
    swallows a non-numeric value and silently falls back to the save clock,
    which would store an interval the caller never asked for.

    Raises:
        ValueError: If ``at`` is not a finite number of epoch seconds.
    """
    if at is None:
        return time.time()
    try:
        instant = float(at)
    except (TypeError, ValueError) as e:
        raise ValueError(f"at must be a number of epoch seconds, got {at!r}") from e
    if instant != instant or instant in (float("inf"), float("-inf")):
        raise ValueError(f"at must be a finite number of epoch seconds, got {at!r}")
    return instant


def _resolve_member_key(entry: Any) -> Optional[str]:
    """Return the Redis key for an entry, a key string, or ``None``.

    An unsaved instance still yields a concrete key (``AutoKeyField`` assigns
    at ``__init__``); "unsaved" is caught by the existence check, not here.
    """
    if entry is None:
        return None
    if isinstance(entry, str):
        return entry or None
    db_key = getattr(entry, "db_key", None)
    if db_key is None:
        raise ValueError(
            f"expected a JournalEntry or a redis key string, got "
            f"{type(entry).__name__}"
        )
    return str(db_key.redis_key)


def _scan_or_block(*values: Any) -> None:
    """Refuse the write if any value trips the never-record firewall.

    Args:
        *values: Candidate strings. Non-strings are ignored by the scanner.

    Raises:
        JournalBlockedError: On the first blocking verdict. The message carries
            only the reason code and detector name -- never the matched text,
            an offset, or a length, because exception text reaches plaintext
            log files.
    """
    for value in values:
        verdict = scan_never_record(value)
        if verdict.blocked:
            raise JournalBlockedError(
                f"never-record: {verdict.reason} ({verdict.detector}); "
                f"nothing was written",
                verdict=verdict,
            )


def _warn_uncoupled_once(model_name: str) -> None:
    """Announce the degraded (uncoupled) annotation mode once per process."""
    if model_name in _UNCOUPLED_WARNED:
        return
    _UNCOUPLED_WARNED.add(model_name)
    logger.warning(
        "%s: POPOTO_JOURNAL_COUPLING_DISABLE is set, so supersede/retract "
        "annotations are appended but their targets' validity intervals are "
        "NOT closed. Live membership degrades to 'everything ever appended'. "
        "Callers can detect this per call via AnnotationResult.target_closed.",
        model_name,
    )
