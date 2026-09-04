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
    journal is not this module's job.

Do not subclass :class:`JournalEntry`
-------------------------------------
Popoto's ``ModelBase`` metaclass does **not** inherit ``Field`` attributes from
a base model class: ``class SubEntry(JournalEntry): pass`` produces a model
with an *empty* field set, so every record it writes persists nothing. This is
an ORM-level limitation, not a journal one, and it is why this module ships no
subclass extension seam.

Two consequences, both load-bearing:

* To extend the annotation vocabulary, call
  :meth:`JournalEntry.register_kind` -- it extends the vocabulary *in place*
  and records whether the new kind carries a ``target`` and whether it closes
  its target's validity interval.
* To get a different field set (an ``EmbeddingField``, say) or a separate
  keyspace, declare your own ``Model`` with the same mixins and the same field
  set and point a :class:`ProvenanceJournal` subclass at it. Copying the field
  set is required, not stylistic. :meth:`ProvenanceJournal._write` refuses an
  ``entry_model`` that is missing them, so the failure is loud rather than a
  keyspace full of empty records.

All numeric behavior is left to the field defaults and to
``popoto.fields.constants.Defaults``; this module pins no constants of its own
beyond the stream bound noted on the class.
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, Optional, Sequence, Type, Union

import redis.client
import redis.exceptions

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
from ..fields.validity_field import ValidityField, map_lua_error
from ..models.base import Model
from ..privacy.never_record import NeverRecordMixin, scan_never_record
from ..redis_db import POPOTO_REDIS_DB

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from redis.client import Pipeline

    from ..models.query import QueryBuilder

logger = logging.getLogger("POPOTO.ProvenanceJournal")

#: Name of the ``ValidityField`` on :class:`JournalEntry`. Referenced by every
#: ``ValidityField`` key helper call in this module, so a rename is one edit.
VALIDITY_FIELD_NAME = "validity"

#: Core kinds that carry no ``target``. Everything else in the core vocabulary
#: annotates exactly one entry.
_CORE_TARGETLESS_KINDS = frozenset({"assert"})

#: Core kinds whose annotation closes the target's validity interval.
_CORE_CLOSING_KINDS = frozenset({"supersede", "retract"})

#: Kinds registered beyond the core four, ``name -> (targetless, closing)``.
#:
#: Process-global and additive, written only by
#: :meth:`JournalEntry.register_kind`. It is deliberately *not* a per-subclass
#: attribute: Popoto's ``ModelBase`` metaclass drops ``Field`` attributes on a
#: model subclass, so a subclass-based extension seam cannot work at all (see
#: the module docstring). Behavioural flags live here rather than in frozen
#: module-level sets so that a registered kind can actually be targetless or
#: membership-closing, instead of being permanently target-required and
#: permanently inert.
_REGISTERED_KINDS: "dict[str, tuple[bool, bool]]" = {}

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
        target_closed: Whether the target's validity interval was closed by
            this call.

            * **Journal-owned pipeline** (no ``pipeline=`` argument): a real
              ``bool``, read from the supersede script's own reply. ``False``
              on a capture or a ``confirm`` (neither changes membership), when
              the coupling switch is off, and when the target was *already*
              closed by a concurrent annotation -- which is the honest answer,
              since this call closed nothing.
            * **Caller-supplied pipeline**: ``None``, meaning *unknown until
              you execute*. Nothing has run, so no truthful ``bool`` exists.
              Reporting ``True`` for a queued close would reproduce exactly the
              #588 silent-no-op shape this type exists to prevent -- a re-close
              of an already-closed target is queued the same way and applies
              nothing. Read ``results[close_index]`` after your own
              ``execute()`` for the real outcome.
        coupling_enabled: The state of
            :attr:`Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED` at write time.
        pipeline: The caller-supplied pipeline, returned unexecuted, or
            ``None`` when the journal owned and executed its own.
        close_index: Index of the queued interval-close command in the
            caller-supplied pipeline, so ``pipeline.execute()[close_index]``
            gives the same answer ``target_closed`` carries on the
            journal-owned path (the closed member's key, or ``''`` when the
            target was already closed). ``None`` whenever no close was queued
            onto a caller pipeline -- always so on the journal-owned path,
            where the pipeline has already executed.
    """

    entry: "JournalEntry"
    target_closed: Optional[bool]
    coupling_enabled: bool
    pipeline: Optional[Any] = None
    close_index: Optional[int] = None


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

    @classmethod
    def register_kind(
        cls, name: str, *, targetless: bool = False, closing: bool = False
    ) -> None:
        """Extend the annotation vocabulary in place.

        The extension seam for downstream modules that need kinds the core
        four do not cover -- a merge/equivalence kind, a queue-able kind, an
        exposure kind. Registration is process-global and additive: it never
        removes or reclassifies a core kind, so the vocabulary can only grow.

        This is a registration call rather than a model subclass on purpose.
        Popoto's ``ModelBase`` metaclass does not inherit ``Field`` attributes,
        so a ``JournalEntry`` subclass has an empty field set and persists
        nothing -- see the module docstring.

        Registering also records the kind's *behaviour*, which a plain
        vocabulary list cannot: without ``targetless``/``closing`` a new kind
        would be permanently target-required and permanently inert for
        membership::

            JournalEntry.register_kind("merge", closing=True)
            ProvenanceJournal.append(
                agent_id="a1", kind="merge", target=old, statement="merged"
            )  # closes old's interval, exactly like a supersede

        The reader rule this seam is built around is unchanged: an entry whose
        ``kind`` a reader does not recognize is **inert for membership** --
        never silently treated as a ``supersede`` or a ``retract``.

        .. important::

           The registry is process-global and is **not persisted**. *Reading*
           an entry with an unregistered kind is fine -- it reads back with its
           stored ``kind`` and is inert for membership, per the reader rule.
           But *writing* one is not: ``transfer/import_.py`` restores records
           through ``instance.save()``, which runs ``pre_save`` validation, so
           importing entries that use a registered kind into a process that has
           not re-registered it fails with ``ValueError`` and classifies those
           records ``ERRORED``. **A restoring or importing process must call
           the same** ``register_kind`` **calls before importing.** (Found in
           PR #589 review.)

        Args:
            name: The new kind. Must be a non-empty string outside
                :attr:`Defaults.JOURNAL_KINDS`, which is frozen because
                changing it would reclassify already-stored entries.
            targetless: True for an original-capture kind that carries no
                ``target``, like ``assert``.
            closing: True if an entry of this kind closes its target's
                validity interval, like ``supersede`` and ``retract``.

        Raises:
            ValueError: On an empty name, a core kind, a ``targetless`` and
                ``closing`` combination (a kind with no target has nothing to
                close), or a re-registration under different flags. Registering
                the same name with the same flags again is a no-op.
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"kind name must be a non-empty string, got {name!r}")
        if name in tuple(Defaults.JOURNAL_KINDS):
            raise ValueError(
                f"{name!r} is a core kind; Defaults.JOURNAL_KINDS is frozen "
                f"because changing it reclassifies already-stored entries"
            )
        if targetless and closing:
            raise ValueError(
                f"{name!r}: a targetless kind carries no target, so it has "
                f"nothing to close -- targetless and closing are exclusive"
            )
        flags = (bool(targetless), bool(closing))
        existing = _REGISTERED_KINDS.get(name)
        if existing is not None and existing != flags:
            raise ValueError(
                f"{name!r} is already registered as "
                f"targetless={existing[0]}, closing={existing[1]}; "
                f"re-registering it as targetless={flags[0]}, "
                f"closing={flags[1]} would reclassify stored entries"
            )
        _REGISTERED_KINDS[name] = flags

    @classmethod
    def journal_kinds(cls) -> "tuple[str, ...]":
        """Return the annotation vocabulary: the core four plus registrations.

        :attr:`Defaults.JOURNAL_KINDS` is read at call time (so a runtime
        assignment to ``Defaults`` is honored rather than frozen at import),
        followed by every :meth:`register_kind` name in registration order.
        """
        return tuple(Defaults.JOURNAL_KINDS) + tuple(_REGISTERED_KINDS)

    @classmethod
    def kind_is_targetless(cls, kind: Any) -> bool:
        """True if ``kind`` is an original capture that carries no ``target``."""
        if kind in _CORE_TARGETLESS_KINDS:
            return True
        registered = _REGISTERED_KINDS.get(kind) if isinstance(kind, str) else None
        return bool(registered and registered[0])

    @classmethod
    def kind_is_closing(cls, kind: Any) -> bool:
        """True if an entry of ``kind`` closes its target's validity interval."""
        if kind in _CORE_CLOSING_KINDS:
            return True
        registered = _REGISTERED_KINDS.get(kind) if isinstance(kind, str) else None
        return bool(registered and registered[1])

    #: Non-key fields whose value Popoto itself generates, never a caller or a
    #: speaker. Excluded from the never-record scan surface -- see
    #: :meth:`_never_record_scan_values`.
    _MACHINE_GENERATED_FIELDS = frozenset({"target"})

    def _never_record_scan_values(self) -> Iterator[str]:
        """Yield the content values the firewall scans, minus machine pointers.

        ``target`` holds a Redis key **Popoto generated** -- ``JournalEntry:
        <agent_id>:<uuid4 hex>``. It is an internal pointer, not user content,
        and scanning it for payment cards is a category error with a measured
        cost: a uuid4 hex sometimes contains a 13-19 digit run that passes the
        Luhn checksum, so the firewall blocks the annotation as
        ``reason='payment_card', detector='luhn'``. Verified example::

            scan_never_record(
                "JournalEntry:agent/-under/-test:8ef1fc6db384458286216656bfb2cf04"
            )
            # -> NeverRecordVerdict(blocked=True, reason='payment_card',
            #                       detector='luhn')

        Measured at 5-8 blocked per 2000 random target keys (~0.25-0.4%), which
        is what made ``test_hard_delete_removes_the_record_and_every_derived_
        trace`` fail roughly 1 run in 150 on CI. The block landed at
        ``Model.save()`` step 0, which *returns* rather than raises, so the
        annotation was silently dropped while the invalidate EVAL still closed
        the target -- a membership change with zero provenance. (PR #589.)

        Every genuine content field is still scanned: ``verbatim``,
        ``statement``, ``speaker``, ``turn_id``, ``kind``, and -- via the
        façade's pre-flight, which also covers the ``subjects`` list the mixin
        cannot see -- ``agent_id`` and ``subjects``. This narrows the surface by
        exactly one machine-generated pointer; it does not weaken the firewall
        for anything a human or a model ever wrote.
        """
        skip = self._MACHINE_GENERATED_FIELDS
        for field_name in self._never_record_scan_field_names():
            if field_name in skip:
                continue
            value = getattr(self, field_name, None)
            if isinstance(value, str) and value.strip():
                yield value

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
    model: Type["JournalEntry"], kind: Any, target: Any
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
    if model.kind_is_targetless(kind):
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

    Every method is a classmethod; there is no instance state.

    To extend the annotation vocabulary, call
    :meth:`JournalEntry.register_kind` -- **not** a model subclass. Popoto's
    ``ModelBase`` metaclass does not inherit ``Field`` attributes, so a
    ``JournalEntry`` subclass has an empty field set and persists nothing.
    :meth:`_write` refuses an ``entry_model`` missing the journal field set for
    exactly that reason; a separate keyspace means declaring your own ``Model``
    with the same mixins and the same fields.

    Treating this as the sole read and write API is a stated contract, not a
    style preference: the single-record-type decision is cheaply reversible
    only while the keyspace is empty (validity and chain keys are namespaced
    per model, and every record's Redis key embeds its class name), so a future
    split has to stay behind this façade.
    """

    #: The entry model this façade reads and writes.
    entry_model: Type[JournalEntry] = JournalEntry

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
            kind: Normally left at ``"assert"``. A kind added with
                :meth:`JournalEntry.register_kind` is reachable here, with the
                same ``kind``/``target`` consistency rules -- and closes the
                target's interval if it was registered ``closing=True``.
            target: Only for a non-``assert`` kind. A
                :class:`JournalEntry` or its Redis key.
            pipeline: Optional caller pipeline. Must be transactional. When
                supplied it is returned unexecuted on
                :attr:`AnnotationResult.pipeline`.

        Returns:
            AnnotationResult: with ``target_closed=False`` -- a capture never
            changes another entry's membership. ``None`` instead when the
            caller supplied the pipeline (nothing has executed yet).

        Raises:
            JournalBlockedError: If any content is refused by the never-record
                firewall. Nothing is issued or queued.
            ValueError: On a missing ``agent_id``, empty content, a bad
                ``kind``/``target`` pairing, a nonexistent or cross-agent
                target, a backdated ``at``, or a non-transactional pipeline.
            TypeError: If ``entry_model`` does not declare the journal field
                set (a ``JournalEntry`` subclass, which persists nothing).
            AppendOnlyViolation: If the record's Redis key already exists.
        """
        if cls.entry_model.kind_is_targetless(kind) and not (statement or "").strip():
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
            AnnotationResult: with ``target_closed=False``, or ``None`` when
            the caller supplied the pipeline (nothing has executed yet).

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
            provenance, one close applies). It is ``None`` when the caller
            supplied the pipeline -- nothing has executed, so no truthful
            answer exists yet; read ``results[close_index]`` after your own
            ``execute()``.

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
    def annotations_for(
        cls, entry: Union[str, "JournalEntry"]
    ) -> Union["QueryBuilder", "list[JournalEntry]"]:
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
    def chain(cls, entry: Union[str, "JournalEntry"]) -> "list[Any]":
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
        instance: Any = entry
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
        _require_journal_shape(model)
        instant = _coerce_instant(at)
        subject_tags = list(subjects or [])

        # ---- D7 pre-flight. Every check below raises BEFORE a single
        # mutating command is issued or queued. The reads it performs
        # (target existence, the target's stored valid_from) are the point:
        # each one is a question that cannot be answered after the fact
        # without having already written something.

        # 1. Vocabulary and kind/target consistency. Pure, in-memory checks --
        #    they issue no Redis command, so they are safe to run ahead of the
        #    firewall.
        target_key = _resolve_member_key(target)
        kind, target_key = validate_kind_and_target(model, kind, target_key)

        if not agent_id:
            raise ValueError(
                f"{model.__name__}: agent_id is required and must be non-empty "
                f"-- a None renders the literal 'None' into the record key"
            )

        # 2. Build the entry. Construction is pure: ``AutoKeyField`` assigns a
        #    key in ``__init__`` but nothing is issued to Redis. Building it
        #    *here*, before the firewall, is what lets step 3 scan exactly the
        #    value set ``NeverRecordMixin`` will scan at save time.
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

        # 3. The firewall, scanned here rather than left to NeverRecordMixin.
        #    ``Model.save()`` *returns* rather than raises when the firewall
        #    fires (``return pipeline if pipeline else False``, base.py step 0),
        #    and in pipeline mode the pipeline it returns is indistinguishable
        #    from success -- so a naive annotate-and-close would queue the
        #    invalidate EVAL against an annotation that was never written and
        #    commit a membership change with zero provenance.
        #
        #    The scanned values are derived from
        #    ``entry._never_record_scan_values()`` -- the *same method* the
        #    mixin calls -- rather than re-listed here. That equality is
        #    load-bearing: PR #589 shipped a hand-written list that omitted
        #    ``target``, the mixin scanned it anyway, a Luhn-passing uuid4 hex
        #    blocked the save, and the drop was silent. Two value sets that can
        #    drift are two chances to reach that path; one set cannot drift.
        #
        #    Two additions the mixin structurally cannot make. ``agent_id`` is a
        #    KeyField, excluded from the mixin's surface, and it is the one
        #    value here that *must* be scanned: it is rendered into the record's
        #    Redis key NAME, the one place ``hard_delete`` cannot scrub
        #    retroactively -- the name of an annotation's ``$IndexedF:`` index
        #    key embeds it. And ``subject_tags`` is a list, so the mixin's
        #    str-only yield never sees it.
        _scan_or_block(agent_id, *subject_tags, *entry._never_record_scan_values())

        if target_key is not None:
            # 4. The target exists, and belongs to this agent. ``target`` is a
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

            # 5. The requested instant is not before the target's *stored*
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

        # 6. A non-transactional caller pipeline would silently void the
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
            # A WATCHing pipeline that has not yet been put into MULTI executes
            # each command immediately rather than queueing, so Model.save()
            # fails part-way through and leaves an entry hash with no indexes
            # and no validity interval. On an append-only model that orphan is
            # permanent -- only hard_delete can remove it -- so this is refused
            # here rather than discovered at write time. (Pre-existing ORM
            # behavior: a plain Model.save() against such a pipeline fails
            # identically. Found in PR #589 review.)
            #
            # ``watching and not explicit_transaction`` is the precise
            # condition, NOT ``watching`` alone. watch() + multi() is the
            # standard redis-py optimistic-locking pattern: multi() sets
            # ``explicit_transaction = True``, after which commands queue
            # normally and the whole annotate-and-close applies atomically at
            # execute(). Refusing that case made the journal strictly less
            # capable than a bare Popoto model, which accepts it. (Round-3
            # review of PR #589.)
            if getattr(pipeline, "watching", False) and not getattr(
                pipeline, "explicit_transaction", False
            ):
                raise ValueError(
                    f"{model.__name__}: a WATCHing pipeline that is not yet in "
                    f"MULTI executes commands immediately instead of queueing, "
                    f"which would leave a permanent orphan record on an "
                    f"append-only model. Call pipeline.multi() to open the "
                    f"transaction -- that keeps your optimistic lock and makes "
                    f"the annotate-and-close atomic -- or use a fresh pipeline. "
                    f"Do NOT call UNWATCH: it would discard the lock you took."
                )

        # ---- End of pre-flight. From here on, commands are issued.

        coupling_enabled = bool(Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED)
        should_close = model.kind_is_closing(kind) and target_key is not None
        if should_close and not coupling_enabled:
            _warn_uncoupled_once(model.__name__)

        owns_pipeline = pipeline is None
        pipe = POPOTO_REDIS_DB.pipeline() if pipeline is None else pipeline

        saved = entry.save(pipeline=pipe)

        # Defence in depth against the whole class of bug this module's worst
        # failure mode belongs to. ``Model.save()`` has several early-return
        # gates (the never-record firewall, the write filter) that *return*
        # instead of raising; each one is a way for the annotation to not be
        # written while the invalidate EVAL below is queued anyway, closing the
        # target's interval with zero provenance backing it -- and reporting
        # ``target_closed=True`` for an entry that does not exist.
        #
        # The pre-flight now scans the same value set the firewall does, so the
        # firewall gate is unreachable from here; this raise is what makes that
        # claim *checkable* rather than assumed, and it covers the gates that
        # have not been analysed yet. Raised, not asserted -- ``python -O``
        # strips asserts, and this must hold in production above all.
        blocked = getattr(entry, "_never_record_verdict", None)
        if not saved or blocked is not None:
            reason = (
                f"the never-record firewall ({blocked.reason})"
                if blocked is not None
                else "an early-return gate in Model.save()"
            )
            raise RuntimeError(
                f"{model.__name__}: the annotation was not written -- "
                f"{reason} stopped it, and Model.save() signals that by "
                f"returning rather than raising. Nothing further is issued: "
                f"queueing the interval close now would commit a membership "
                f"change with no provenance behind it. This is a bug in this "
                f"module (the pre-flight should have caught it), not a caller "
                f"error."
            )

        close_index: Optional[int] = None
        if should_close and coupling_enabled:
            # ``should_close`` is ``kind_is_closing(kind) and target_key is not
            # None``, so a close can never be reached without a target. Raised
            # rather than asserted (``python -O`` strips asserts) and rather
            # than annotated away, because a ``None`` here is exactly the #588
            # shape: ``execute_supersede`` would take its no-target branch,
            # return without closing anything, and the caller would still be
            # told the target was closed.
            if target_key is None:
                raise RuntimeError(
                    f"{model.__name__}: reached the interval close with no "
                    f"target key. execute_supersede would silently close "
                    f"nothing while the result reported a close (#588). This "
                    f"is a bug in this module, not a caller error."
                )
            close_index = len(pipe.command_stack)
            # execute_supersede, NOT SupersessionProtocol (#588 is fixed; this
            # is still the right call). The protocol's pipeline API works now --
            # membership is decided inside SUPERSEDE_LUA at the instant of the
            # write -- but M1 needs two things the protocol does not offer on
            # this path: an *explicit* ``old_member`` (the annotation target is
            # named by the caller and validated by the pre-flight, not resolved
            # through an identity pointer), and ``assert_valid_from=False``
            # because valid-time is already set at construction. Converting this
            # to ``save_and_supersede`` was offered to M4 and **declined**
            # (see docs/plans/reference_resolution_m4.md): M4 appends
            # targetless ``assert`` entries and never reaches this annotation
            # branch, so the conversion would add risk to a shipped write path
            # for no M4 benefit. It is tracked separately as #606. The
            # pre-flight above also carries firewall, cross-agent-ownership and
            # kind/target checks the protocol cannot express, so any conversion
            # has to reproduce those first.
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
            # The caller owns execution, so nothing has run yet and there is no
            # truthful answer to "was the target closed". Report ``None`` --
            # *unknown until you execute* -- and hand back the index of the
            # queued close so the caller can read the real outcome from their
            # own ``execute()`` results. Reporting ``True`` here would be
            # affirmatively wrong for a re-close of an already-closed target:
            # the command queues identically and applies nothing.
            return AnnotationResult(
                entry=entry,
                target_closed=None,
                coupling_enabled=coupling_enabled,
                pipeline=pipe,
                close_index=close_index,
            )

        try:
            results = pipe.execute()
        except redis.exceptions.ResponseError as e:
            # The entry HSET is above the EVAL in this MULTI and has already
            # applied; Redis does not roll back a transaction when one command
            # errors. The annotation is real provenance and stays -- suppressing
            # it to match a failed close would lose information an append-only
            # journal exists to keep. Only the close failed, so surface *which*,
            # typed, rather than a raw Lua token string (#588 D8).
            #
            # Reachable because M1 names ``old_member`` explicitly, which the
            # script reads as a caller assertion: a target hard-deleted between
            # the pre-flight above and EXEC now returns MEMBER_ABSENT where it
            # previously took the idempotent no-op branch.
            raise map_lua_error(e) from e
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


#: Every field a journal entry model must declare. Checked in full by
#: :func:`_require_journal_shape`.
#:
#: This was a single canary (``"statement"``) until PR #589's second review
#: round, which showed the canary had a false negative that reproduced the
#: original blocker in narrowed form: a subclass that re-declared only
#: ``entry_id``/``agent_id``/``statement`` passed the guard and then silently
#: persisted 3 of 12 fields -- no ``verbatim``, no ``kind``, no ``target``, and
#: no validity interval at all. A partial field set is exactly as lossy as an
#: empty one, so the guard checks the whole set.
#:
#: "The whole set" means every non-key field :class:`JournalEntry` declares.
#: Round 3 caught the set at 6 of 12 while the comment claimed completeness: a
#: model declaring exactly those 6 passed and then silently dropped
#: ``speaker``, ``turn_id``, ``subjects``, ``stated`` and ``captured_at`` --
#: attribution and provenance-of-the-provenance, which is the entire point of
#: this module. The five were added rather than the comment softened, because
#: "who said it, in which turn, and was it actually said" is not optional
#: metadata on a provenance record. ``entry_id`` is excluded: it is the
#: ``AutoKeyField``, and a model needs *a* key field, not that name.
_REQUIRED_ENTRY_FIELDS = frozenset(
    {
        "agent_id",
        "captured_at",
        "turn_id",
        "speaker",
        "verbatim",
        "statement",
        "subjects",
        "stated",
        "kind",
        "target",
        "validity",
    }
)


def _require_journal_shape(model: Any) -> None:
    """Refuse an ``entry_model`` that would silently persist an empty record.

    Popoto's ``ModelBase`` metaclass does **not** inherit ``Field`` attributes
    from a base model class, so ``class SubEntry(JournalEntry): pass`` yields a
    model whose ``_meta.fields`` is *empty*. Without this guard the journal
    would accept it and write records carrying no ``statement``, no
    ``verbatim``, no ``kind``, no ``target`` and no validity interval -- data
    loss with no error anywhere.

    :class:`JournalEntry` itself is exempt from the field check (it is the
    reference shape); anything else must declare the field set itself.

    Raises:
        TypeError: If ``model`` is not :class:`JournalEntry` and does not
            declare the journal field set.
    """
    if model is JournalEntry:
        return
    fields = getattr(getattr(model, "_meta", None), "fields", {}) or {}
    missing = sorted(_REQUIRED_ENTRY_FIELDS - set(fields))
    if not missing:
        return
    model_name = getattr(model, "__name__", repr(model))
    raise TypeError(
        f"{model_name} is not a usable journal entry "
        f"model: it declares no {', '.join(repr(m) for m in missing)} "
        f"field(s), so records it writes would silently lose them. If it "
        f"subclasses "
        f"JournalEntry, that is the cause: Popoto's ModelBase metaclass does "
        f"not inherit Field attributes from a base model class, so a "
        f"JournalEntry subclass has an EMPTY field set. Do not subclass "
        f"JournalEntry. To extend the annotation vocabulary call "
        f"JournalEntry.register_kind(...); for a separate keyspace or a "
        f"different field set, declare your own Model with the same mixins "
        f"and the same fields."
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
