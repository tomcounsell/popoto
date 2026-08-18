"""Provenance journal — append-only entries with annotations (#560).

Runs on the pytest plugin's isolated database (DB 15 by default). The plugin's
autouse ``_popoto_flush_db`` flushes before every test, so the only teardown
this file owns is restoring ``Defaults`` after a kill-switch flip.

Tests cover:
- The append-only contract in all six re-save shapes from the plan's spike-1
  matrix: same-instance re-save, colliding fresh object, ``query.get()``-then-
  save, ``delete()``, ``delete_all()``, and ``save(migrate_key=True)`` — plus a
  control model without the mixin proving the refusal is the mixin's doing
- Race 2's deterministically testable shape: two saves of one key queued onto
  one pipeline, which the ``EXISTS`` guard structurally cannot see (documented
  boundary, asserted as a known state rather than claimed closed)
- Full field round trip including ``statement`` and ``captured_at``, the
  ``subjects`` tag list, the open validity interval, and the empty-content and
  empty-``subjects`` input cases
- ``annotations_for`` resolving in one query — asserted by counting index reads
  with the ``_CallCounter`` pattern and showing the count does not grow with the
  number of annotations, not by reading the implementation
- Membership: after ``supersede``/``retract`` the target is absent from
  ``filter(validity__current=True)`` and still returned by
  ``filter(validity__as_of=<before the close>)``, with **zero** reads against
  either chain HASH
- ``chain()`` over a 3-deep supersession chain, oldest -> newest
- ``confirm`` as evidence only: the target keeps its open interval
- Never-record composition: a blocked ``append()`` raises and persists nothing;
  a blocked ``supersede()`` raises, closes nothing, writes no chain link and
  leaves ``annotations_for(target)`` empty (the war room's top blocker)
- The ``subjects`` firewall scan the mixin itself does not do (D8), and the
  regression that ``target`` survives the entropy detector
- Every D7 pre-flight rejection — bad kind, inconsistent kind/target, missing
  target, cross-agent target, backdated ``at``, non-numeric ``at``, and a
  non-transactional pipeline — asserted to issue **zero** commands
- Atomicity by shape, not by count: ``pipeline.transaction is True``, exactly
  one queued EVAL with ``ARGV[5] == "invalidate"`` at ``numkeys == 6``, exactly
  one with ``ARGV[5] == "open"``, zero mutating calls outside the pipeline
- The documented atomicity boundary: a command-level error inside ``EXEC``
  leaves the annotation appended with the target open. Tested as a known state;
  rollback is not claimed
- Both #588 regressions: ``SupersessionProtocol.invalidate`` silently no-ops for
  a successor saved in the same pipeline, and a ``valid_from`` passed only to
  ``execute_supersede`` is silently replaced by the save clock — pinned against
  the raw V0 behavior and against M1's write path
- The #588 pre-flight pin: bypassing D7 and closing with a backdated instant
  raises ``redis.exceptions.ResponseError`` from ``pipe.execute()`` with the
  annotation already written
- The validity-coupling kill switch: an uncoupled ``supersede`` issues the
  command set of a bare append and nothing more, and the degraded mode is
  detectable from ``AnnotationResult`` without reading Redis
- ``hard_delete``'s derived-state sweep: afterwards ``filter()`` returns nothing
  and no ``$*`` key retains the record's Redis key
- Race 1: an orphan index member whose hash is missing is skipped by the query
  layer rather than raising
- Race 3: two annotations closing one target — both entries are real
  provenance, exactly one close applies
"""

import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
import redis.exceptions
from src import popoto
from src.popoto import ValidityField
from src.popoto.exceptions import AppendOnlyViolation, JournalBlockedError
from src.popoto.fields.constants import Defaults, _read_journal_coupling_switch
from src.popoto.fields.supersession import SupersessionProtocol
from src.popoto.recipes import provenance_journal as journal_module
from src.popoto.recipes.provenance_journal import (
    VALIDITY_FIELD_NAME,
    JournalEntry,
    ProvenanceJournal,
)
from src.popoto.redis_db import POPOTO_REDIS_DB, scan_keys

AGENT = "agent-under-test"
OTHER_AGENT = "agent-next-door"

#: A representative credential for the firewall cases. Distinctive enough that
#: a keyspace sweep for it cannot collide with unrelated fixture data.
SECRET = "sk-ant-api03-ZZQQWWEERRTTYYUUIIOOPPAASSDDFFGG"


# --- Test Models ---


class MutableNote(popoto.Model):
    """Control model without ``AppendOnlyMixin``.

    Same shape as an entry, no write-once guard — so a passing re-save here is
    the proof that the refusals in :class:`TestAppendOnlyContract` come from the
    mixin and not from something ambient in the model layer.
    """

    note_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    statement = popoto.StringField(default="")


# --- Fixtures and helpers ---


@pytest.fixture(autouse=True)
def restore_defaults():
    """Undo kill-switch flips. The plugin's autouse flush owns the keyspace.

    ``popoto.pytest_plugin`` installs a function-scoped autouse ``flushdb()``
    (``pytest_plugin.py:234-250``), so no derived-key wipe belongs here — see
    the plan's D6.
    """
    coupling = Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED
    yield
    Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = coupling
    journal_module._UNCOUPLED_WARNED.clear()


def _append(**kwargs):
    """Append one capture and return the :class:`JournalEntry`."""
    kwargs.setdefault("agent_id", AGENT)
    kwargs.setdefault("statement", "the launch slipped to the 30th")
    return ProvenanceJournal.append(**kwargs).entry


def _keys():
    return ValidityField.get_all_keys(JournalEntry, VALIDITY_FIELD_NAME)


def _interval(instance):
    """Return ``(valid_from, invalid_at, ingested_at)`` for one entry."""
    keys = _keys()
    member = instance.db_key.redis_key
    return (
        POPOTO_REDIS_DB.zscore(keys["valid_from"], member),
        POPOTO_REDIS_DB.zscore(keys["invalid_at"], member),
        POPOTO_REDIS_DB.zscore(keys["ingested_at"], member),
    )


def _redis_keys(records):
    return sorted(r.db_key.redis_key for r in records)


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else str(value)


MUTATING_CLIENT_METHODS = [
    "zadd",
    "zrem",
    "zincrby",
    "hset",
    "hdel",
    "set",
    "delete",
    "sadd",
    "srem",
    "xadd",
    "lpush",
    "rpush",
]


class _CallCounter:
    """Count client calls by name, delegating to the real implementation.

    Copied from ``tests/test_validity_field.py`` so command-level assertions in
    this repo all read the same way.
    """

    def __init__(self, monkeypatch, names):
        self.counts = {name: 0 for name in names}
        for name in names:
            monkeypatch.setattr(POPOTO_REDIS_DB, name, self._wrap(name), raising=True)

    def _wrap(self, name):
        original = getattr(POPOTO_REDIS_DB, name)

        def wrapper(*args, **kwargs):
            self.counts[name] += 1
            return original(*args, **kwargs)

        return wrapper

    @property
    def nonzero(self):
        return {name: count for name, count in self.counts.items() if count}


class _PipelineSpy:
    """Capture every pipeline the code under test builds, and its command stack.

    ``command_stack`` is emptied by ``execute()``, so the stack is snapshotted
    on the way in. The spy records unexecuted pipelines too, which is what the
    caller-supplied-pipeline cases assert against.
    """

    def __init__(self, monkeypatch):
        self.pipelines = []
        self.stacks = []
        real_pipeline = POPOTO_REDIS_DB.pipeline
        spy = self

        def make_pipeline(*args, **kwargs):
            pipe = real_pipeline(*args, **kwargs)
            spy.pipelines.append(pipe)
            real_execute = pipe.execute

            def execute(*eargs, **ekwargs):
                spy.stacks.append(list(pipe.command_stack))
                return real_execute(*eargs, **ekwargs)

            pipe.execute = execute
            return pipe

        monkeypatch.setattr(POPOTO_REDIS_DB, "pipeline", make_pipeline)

    def snapshot(self, pipe):
        """Record an unexecuted pipeline's stack explicitly."""
        self.stacks.append(list(pipe.command_stack))

    def evals(self, mode=None):
        """Return every queued EVAL command, optionally filtered by ``ARGV[5]``."""
        found = []
        for stack in self.stacks:
            for entry in stack:
                args = entry[0] if isinstance(entry, tuple) else entry
                if not args or _as_str(args[0]).upper() != "EVAL":
                    continue
                if mode is not None and _supersede_mode(args) != mode:
                    continue
                found.append(args)
        return found

    def pipeline_of(self, mode):
        """Return the pipeline whose stack carries a supersede EVAL in ``mode``."""
        for pipe, stack in zip(self.pipelines, self.stacks):
            for entry in stack:
                args = entry[0] if isinstance(entry, tuple) else entry
                if args and _as_str(args[0]).upper() == "EVAL":
                    if _supersede_mode(args) == mode:
                        return pipe
        return None


class _CommandRecorder:
    """Count every command the code under test issues **or queues**.

    Client-level mutating calls come from :class:`_CallCounter`; queued
    commands come from :class:`_PipelineSpy`, counting both the stacks captured
    at ``execute()`` and whatever is still sitting on an unexecuted pipeline.
    Counting only the first would make "zero commands" unfalsifiable, because
    the journal routes its writes through a pipeline it owns.
    """

    def __init__(self, monkeypatch):
        self.spy = _PipelineSpy(monkeypatch)
        self.counter = _CallCounter(monkeypatch, ["eval"] + MUTATING_CLIENT_METHODS)

    @property
    def queued(self):
        executed = sum(len(stack) for stack in self.spy.stacks)
        pending = sum(len(pipe.command_stack) for pipe in self.spy.pipelines)
        return executed + pending

    @property
    def detail(self):
        detail = dict(self.counter.nonzero)
        if self.queued:
            detail["queued"] = self.queued
        return detail

    @property
    def total(self):
        return sum(self.counter.counts.values()) + self.queued

    @property
    def nonzero(self):
        """Empty exactly when nothing was issued and nothing was queued."""
        return self.detail


def _supersede_mode(args):
    """Return ``ARGV[5]`` of a queued ``SUPERSEDE_LUA`` EVAL, or ``None``.

    Layout: ``EVAL script numkeys KEYS[1..6] ARGV[1..7]`` -- so ``ARGV[5]`` is
    positional index 13, and anything shorter is a different script.
    """
    if len(args) < 16:
        return None
    if str(args[2]) != "6":
        return None
    return _as_str(args[13])


def _numkeys(args):
    return int(args[2])


# ---------------------------------------------------------------------------
# A. The append-only contract (plan spike-1's matrix, extended)
# ---------------------------------------------------------------------------


class TestAppendOnlyContract:
    def test_a_fresh_append_is_allowed_and_persists(self):
        entry = _append()
        assert POPOTO_REDIS_DB.exists(entry.db_key.redis_key)
        assert JournalEntry.query.get(redis_key=entry.db_key.redis_key) is not None

    def test_re_saving_the_same_instance_raises_append_only_violation(self):
        entry = _append()
        entry.statement = "a different claim"
        with pytest.raises(AppendOnlyViolation):
            entry.save()

    def test_a_fresh_object_with_a_colliding_key_raises_append_only_violation(self):
        """The shape a retry or a duplicate ingest takes.

        Both ``_db_content`` and ``_saved_field_values`` are empty here, which
        is exactly why the guard reads ``EXISTS`` instead.
        """
        entry = _append()
        collider = JournalEntry(
            entry_id=entry.entry_id,
            agent_id=AGENT,
            statement="an overwrite attempt",
            kind="assert",
        )
        assert collider.db_key.redis_key == entry.db_key.redis_key
        with pytest.raises(AppendOnlyViolation):
            collider.save()

    def test_saving_a_query_loaded_instance_raises_append_only_violation(self):
        """``_db_content`` is empty on a query-loaded instance; ``EXISTS`` is not."""
        entry = _append()
        loaded = JournalEntry.query.get(redis_key=entry.db_key.redis_key)
        assert loaded is not None
        with pytest.raises(AppendOnlyViolation):
            loaded.save()

    def test_deleting_an_entry_raises_append_only_violation(self):
        entry = _append()
        with pytest.raises(AppendOnlyViolation):
            entry.delete()
        assert POPOTO_REDIS_DB.exists(entry.db_key.redis_key)

    def test_delete_all_raises_append_only_violation_and_keeps_the_records(self):
        """``delete_all()`` routes through ``instance.delete()``, so the guard fires."""
        entry = _append()
        with pytest.raises(AppendOnlyViolation):
            JournalEntry.delete_all()
        assert POPOTO_REDIS_DB.exists(entry.db_key.redis_key)

    def test_save_with_migrate_key_raises_even_though_the_new_key_is_free(self):
        """The destroy-through-a-public-kwarg shape ``EXISTS`` cannot express."""
        entry = _append()
        with pytest.raises(AppendOnlyViolation, match="migrate_key"):
            entry.save(migrate_key=True)
        assert POPOTO_REDIS_DB.exists(entry.db_key.redis_key)

    def test_mutating_a_key_field_closes_both_routes_to_a_key_migration(self):
        """The rename route is shut whichever way it is taken.

        Without ``migrate_key=True`` the ORM's own ``KeyMutationError`` fires
        first (the mixin's ``EXISTS`` looks at the *new* key, which is free);
        with it, ``AppendOnlyViolation`` fires before anything is written.
        Either way the stored entry is untouched.
        """
        entry = _append()
        original_key = entry.db_key.redis_key
        entry.agent_id = "some-other-agent"

        with pytest.raises(popoto.exceptions.KeyMutationError):
            entry.save()
        with pytest.raises(AppendOnlyViolation, match="migrate_key"):
            entry.save(migrate_key=True)

        assert POPOTO_REDIS_DB.exists(original_key)

    def test_two_saves_of_one_key_on_one_pipeline_are_not_caught(self):
        """Race 2's deterministic shape — a documented boundary, not a guarantee.

        The guard's ``EXISTS`` executes immediately against ``POPOTO_REDIS_DB``
        and cannot see a command that is queued but not yet executed, so both
        saves pass. Asserted as a known state so the boundary cannot drift into
        an unnoticed regression in either direction.
        """
        pipe = POPOTO_REDIS_DB.pipeline()
        first = JournalEntry(agent_id=AGENT, statement="first", kind="assert")
        first.save(pipeline=pipe)
        second = JournalEntry(
            entry_id=first.entry_id,
            agent_id=AGENT,
            statement="second",
            kind="assert",
        )
        # No AppendOnlyViolation: this is the intra-pipeline TOCTOU window.
        second.save(pipeline=pipe)
        pipe.execute()

        stored = JournalEntry.query.get(redis_key=first.db_key.redis_key)
        assert stored is not None
        assert stored.statement == "second"

    def test_the_control_model_without_the_mixin_allows_re_saving(self):
        note = MutableNote(agent_id=AGENT, statement="v1")
        note.save()
        note.statement = "v2"
        note.save()
        reloaded = MutableNote.query.get(redis_key=note.db_key.redis_key)
        assert reloaded.statement == "v2"
        assert note.delete()


# ---------------------------------------------------------------------------
# B. Round trip and input handling
# ---------------------------------------------------------------------------


class TestEntryRoundTrip:
    def test_every_field_round_trips_including_statement_and_captured_at(self):
        captured = time.time() - 90.0
        instant = time.time() - 30.0
        entry = ProvenanceJournal.append(
            agent_id=AGENT,
            speaker="tom",
            turn_id="t-41",
            verbatim="the launch slipped to the 30th",
            statement="Launch date is the 30th",
            subjects=["launch", "tom"],
            stated=False,
            captured_at=captured,
            at=instant,
        ).entry

        loaded = JournalEntry.query.get(redis_key=entry.db_key.redis_key)
        assert loaded.agent_id == AGENT
        assert loaded.speaker == "tom"
        assert loaded.turn_id == "t-41"
        assert loaded.verbatim == "the launch slipped to the 30th"
        assert loaded.statement == "Launch date is the 30th"
        assert sorted(loaded.subjects) == ["launch", "tom"]
        assert loaded.stated is False
        assert loaded.captured_at == pytest.approx(captured)
        assert loaded.kind == "assert"
        assert loaded.target is None

        valid_from, invalid_at, ingested_at = _interval(entry)
        assert valid_from == pytest.approx(instant)
        assert invalid_at == float("inf")
        assert ingested_at is not None

    def test_indexed_fields_are_queryable_in_one_filter_each(self):
        entry = _append(speaker="tom", turn_id="t-41", subjects=["launch"])
        _append(speaker="ada", turn_id="t-42")

        assert _redis_keys(JournalEntry.query.filter(speaker="tom")) == [
            entry.db_key.redis_key
        ]
        assert _redis_keys(JournalEntry.query.filter(turn_id="t-41")) == [
            entry.db_key.redis_key
        ]
        assert _redis_keys(JournalEntry.query.filter(subjects__contains="launch")) == [
            entry.db_key.redis_key
        ]

    def test_an_entry_with_no_subjects_is_accepted(self):
        assert _append(subjects=None) is not None
        assert _append(subjects=[]) is not None

    def test_an_entry_with_neither_statement_nor_verbatim_is_refused(self):
        with pytest.raises(ValueError, match="not a provenance record"):
            ProvenanceJournal.append(agent_id=AGENT, statement="   ", verbatim="")

    def test_a_verbatim_only_entry_is_accepted(self):
        entry = ProvenanceJournal.append(
            agent_id=AGENT, statement="", verbatim="the 30th"
        ).entry
        assert entry.verbatim == "the 30th"

    def test_a_missing_agent_id_is_refused(self):
        with pytest.raises(ValueError, match="agent_id"):
            ProvenanceJournal.append(agent_id="", statement="anything")


# ---------------------------------------------------------------------------
# C. Annotations, membership, and chains
# ---------------------------------------------------------------------------


class TestAnnotationsAndMembership:
    def test_annotations_for_returns_every_annotation_targeting_an_entry(self):
        target = _append(statement="Launch date is the 30th")
        confirmation = ProvenanceJournal.confirm(
            target, agent_id=AGENT, statement="Ada agrees"
        ).entry
        unrelated = _append(statement="unrelated claim")

        found = _redis_keys(ProvenanceJournal.annotations_for(target))
        assert found == [confirmation.db_key.redis_key]
        assert unrelated.db_key.redis_key not in found
        assert ProvenanceJournal.annotations_for(unrelated) == []

    def test_annotations_for_costs_the_same_index_reads_at_any_result_size(
        self, monkeypatch
    ):
        """One query, asserted by cost rather than by reading the source.

        A per-annotation lookup would make the index-read count grow with the
        number of annotations. It does not, and it stays small.
        """
        target = _append(statement="Launch date is the 30th")
        ProvenanceJournal.confirm(target, agent_id=AGENT, statement="first")

        counter = _CallCounter(monkeypatch, ["smembers", "sinter", "sunion"])
        ProvenanceJournal.annotations_for(target)
        one_annotation = dict(counter.counts)
        monkeypatch.undo()

        for i in range(4):
            ProvenanceJournal.confirm(target, agent_id=AGENT, statement=f"more {i}")
        assert len(ProvenanceJournal.annotations_for(target)) == 5

        counter = _CallCounter(monkeypatch, ["smembers", "sinter", "sunion"])
        ProvenanceJournal.annotations_for(target)
        five_annotations = dict(counter.counts)

        assert five_annotations == one_annotation, (
            "annotations_for must not scale its index reads with the result "
            f"size: {one_annotation} -> {five_annotations}"
        )
        assert sum(five_annotations.values()) <= 3, five_annotations

    def test_confirm_leaves_the_target_in_live_membership(self):
        target = _append()
        result = ProvenanceJournal.confirm(target, agent_id=AGENT, statement="agreed")

        assert result.target_closed is False
        assert _interval(target)[1] == float("inf")
        assert target.db_key.redis_key in _redis_keys(
            JournalEntry.query.filter(validity__current=True)
        )

    def test_supersede_removes_the_target_from_live_membership(self):
        t0 = time.time() - 100.0
        target = _append(at=t0, statement="Launch date is the 30th")
        result = ProvenanceJournal.supersede(
            target,
            agent_id=AGENT,
            statement="Launch date is the 27th",
            at=t0 + 50.0,
        )

        assert result.target_closed is True
        assert result.coupling_enabled is True
        current = _redis_keys(JournalEntry.query.filter(validity__current=True))
        assert target.db_key.redis_key not in current
        assert result.entry.db_key.redis_key in current
        assert _interval(target)[1] == pytest.approx(t0 + 50.0)

    def test_retract_removes_the_target_from_live_membership(self):
        t0 = time.time() - 100.0
        target = _append(at=t0)
        result = ProvenanceJournal.retract(
            target, agent_id=AGENT, statement="withdrawn", at=t0 + 50.0
        )

        assert result.target_closed is True
        assert target.db_key.redis_key not in _redis_keys(
            JournalEntry.query.filter(validity__current=True)
        )

    def test_a_superseded_entry_is_still_returned_as_of_before_the_close(self):
        t0 = time.time() - 100.0
        target = _append(at=t0)
        ProvenanceJournal.supersede(target, agent_id=AGENT, at=t0 + 50.0)

        as_of = _redis_keys(JournalEntry.query.filter(validity__as_of=t0 + 10.0))
        assert target.db_key.redis_key in as_of
        assert POPOTO_REDIS_DB.exists(target.db_key.redis_key)

    def test_membership_queries_never_read_the_chain_hashes(self, monkeypatch):
        """Membership comes from the interval ZSETs, with no chain walk."""
        t0 = time.time() - 100.0
        target = _append(at=t0)
        ProvenanceJournal.supersede(target, agent_id=AGENT, at=t0 + 50.0)

        keys = _keys()
        chain_keys = {keys["chain_fwd"], keys["chain_rev"]}
        reads = []

        def record(name, original):
            def wrapper(key, *args, **kwargs):
                if _as_str(key) in chain_keys:
                    reads.append((name, _as_str(key)))
                return original(key, *args, **kwargs)

            return wrapper

        for name in ("hget", "hgetall", "hkeys", "hvals", "hmget"):
            monkeypatch.setattr(
                POPOTO_REDIS_DB, name, record(name, getattr(POPOTO_REDIS_DB, name))
            )

        current = _redis_keys(JournalEntry.query.filter(validity__current=True))
        as_of = _redis_keys(JournalEntry.query.filter(validity__as_of=t0 + 10.0))

        assert target.db_key.redis_key not in current
        assert target.db_key.redis_key in as_of
        assert reads == [], f"membership read the chain hashes: {reads}"

    def test_chain_returns_a_three_deep_supersession_chain_oldest_first(self):
        t0 = time.time() - 300.0
        first = _append(at=t0, statement="the 30th")
        second = ProvenanceJournal.supersede(
            first, agent_id=AGENT, statement="the 27th", at=t0 + 50.0
        ).entry
        third = ProvenanceJournal.supersede(
            second, agent_id=AGENT, statement="the 28th", at=t0 + 100.0
        ).entry

        expected = [
            first.db_key.redis_key,
            second.db_key.redis_key,
            third.db_key.redis_key,
        ]
        for anchor in (first, second, third):
            walked = [r.db_key.redis_key for r in ProvenanceJournal.chain(anchor)]
            assert walked == expected, f"from {anchor.db_key.redis_key}: {walked}"

        current = _redis_keys(JournalEntry.query.filter(validity__current=True))
        assert current == [third.db_key.redis_key]

    def test_chain_of_an_unannotated_entry_is_just_that_entry(self):
        entry = _append()
        assert [r.db_key.redis_key for r in ProvenanceJournal.chain(entry)] == [
            entry.db_key.redis_key
        ]


# ---------------------------------------------------------------------------
# D. Never-record composition (plan D7/D8 — the war room's top blocker)
# ---------------------------------------------------------------------------


def _lossy(value):
    """Decode a Redis reply for substring searching, tolerating msgpack bytes.

    The record hash holds msgpack, which is not valid UTF-8, so the sweep
    decodes with replacement rather than skipping those keys — a secret stored
    inside a msgpack blob must still be found.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _keyspace_contains(needle):
    """True if ``needle`` appears in any key name or any value in the DB."""
    for key in scan_keys("*"):
        key = _lossy(key)
        if needle in key:
            return True
        key_type = _lossy(POPOTO_REDIS_DB.type(key))
        if key_type == "hash":
            blob = POPOTO_REDIS_DB.hgetall(key)
            for field, value in blob.items():
                if needle in _lossy(field) or needle in _lossy(value):
                    return True
        elif key_type == "set":
            if any(needle in _lossy(m) for m in POPOTO_REDIS_DB.smembers(key)):
                return True
        elif key_type == "zset":
            members = POPOTO_REDIS_DB.zrange(key, 0, -1)
            if any(needle in _lossy(m) for m in members):
                return True
        elif key_type == "string":
            if needle in _lossy(POPOTO_REDIS_DB.get(key)):
                return True
        elif key_type == "stream":
            entries = POPOTO_REDIS_DB.xrange(key)
            if needle in _lossy(repr(entries)):
                return True
    return False


class TestNeverRecordComposition:
    def test_a_blocked_append_raises_and_persists_nothing(self):
        with pytest.raises(JournalBlockedError) as excinfo:
            ProvenanceJournal.append(
                agent_id=AGENT, statement=f"the key is {SECRET}", verbatim=""
            )
        assert SECRET not in str(excinfo.value)
        assert JournalEntry.query.filter(agent_id=AGENT) == []
        assert not _keyspace_contains(SECRET)

    def test_a_blocked_verbatim_span_raises_and_persists_nothing(self):
        with pytest.raises(JournalBlockedError):
            ProvenanceJournal.append(
                agent_id=AGENT, statement="a claim", verbatim=f"he said {SECRET}"
            )
        assert JournalEntry.query.filter(agent_id=AGENT) == []
        assert not _keyspace_contains(SECRET)

    def test_a_blocked_subject_tag_raises_even_though_the_mixin_never_scans_lists(self):
        """D8: ``_never_record_scan_values`` yields only ``str``, so a
        ``TagField`` list is outside the mixin's scan surface. ``append()``
        closes that gap itself."""
        with pytest.raises(JournalBlockedError):
            ProvenanceJournal.append(
                agent_id=AGENT, statement="a claim", subjects=["launch", SECRET]
            )
        assert JournalEntry.query.filter(agent_id=AGENT) == []
        assert not _keyspace_contains(SECRET)

    def test_the_mixin_alone_still_does_not_scan_tag_lists(self):
        """Pin the residual mixin hole so it cannot drift silently.

        Constructing the entry directly bypasses the façade's explicit tag
        scan, and the mixin does not catch it. Documented behavior, asserted
        so a future change to ``_never_record_scan_values`` is a deliberate
        one rather than a surprise.
        """
        entry = JournalEntry(
            agent_id=AGENT, statement="a claim", subjects=[SECRET], kind="assert"
        )
        entry.save()
        stored = JournalEntry.query.get(redis_key=entry.db_key.redis_key)
        assert stored is not None
        assert SECRET in stored.subjects

    def test_a_blocked_supersede_closes_nothing_and_leaves_no_annotation(self):
        """The plan's highest-severity failure path.

        ``Model.save()`` returns the *pipeline* when the firewall fires in
        pipeline mode, which is indistinguishable from success — so a naive
        implementation would commit the invalidate EVAL against an annotation
        that was never written: a membership change with zero provenance.
        """
        t0 = time.time() - 100.0
        target = _append(at=t0)

        with pytest.raises(JournalBlockedError):
            ProvenanceJournal.supersede(
                target,
                agent_id=AGENT,
                statement=f"the real value is {SECRET}",
                at=t0 + 50.0,
            )

        assert _interval(target)[1] == float("inf")
        assert target.db_key.redis_key in _redis_keys(
            JournalEntry.query.filter(validity__current=True)
        )
        assert ProvenanceJournal.annotations_for(target) == []
        keys = _keys()
        assert POPOTO_REDIS_DB.hget(keys["chain_fwd"], target.db_key.redis_key) is None
        assert POPOTO_REDIS_DB.hlen(keys["chain_rev"]) == 0
        assert not _keyspace_contains(SECRET)

    def test_a_blocked_retract_closes_nothing(self):
        target = _append()
        with pytest.raises(JournalBlockedError):
            ProvenanceJournal.retract(
                target, agent_id=AGENT, statement=f"because {SECRET}"
            )
        assert _interval(target)[1] == float("inf")
        assert ProvenanceJournal.annotations_for(target) == []

    def test_a_blocked_annotation_issues_or_queues_no_command(self, monkeypatch):
        target = _append()
        recorder = _CommandRecorder(monkeypatch)
        with pytest.raises(JournalBlockedError):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement=f"leak {SECRET}"
            )
        assert recorder.total == 0, recorder.detail

    def test_a_target_carrying_annotation_survives_the_entropy_detector(self):
        """Regression: ``target`` is a full Redis key.

        It clears the entropy detector only because it contains ``:``, which is
        outside the detector's charset. If a future single-segment key rendering
        changed that, every annotation would start being firewall-blocked.
        """
        target = _append()
        target_key = target.db_key.redis_key
        assert ":" in target_key

        result = ProvenanceJournal.confirm(target_key, agent_id=AGENT, statement="yes")
        assert result.entry.target == target_key
        assert _redis_keys(JournalEntry.query.filter(target=target_key)) == [
            result.entry.db_key.redis_key
        ]


# ---------------------------------------------------------------------------
# E. D7 pre-flight — every rejection issues zero commands
# ---------------------------------------------------------------------------


class TestPreFlightValidation:
    """Each case asserts by call counter, not just by exception type.

    "Raises" is not the property that matters here; "raises before anything was
    written" is. A rejection that has already queued or issued a command is the
    exact failure shape D7 exists to prevent.

    ``_counted`` deliberately counts *queued* commands as well as issued ones.
    The journal owns its pipeline, so a client-level counter alone would read
    zero even for a fully successful write — an assertion that could never
    fail. ``test_the_zero_command_harness_is_live`` is the positive control
    that keeps this honest.
    """

    def _counted(self, monkeypatch):
        return _CommandRecorder(monkeypatch)

    def test_the_zero_command_harness_is_live(self, monkeypatch):
        """Positive control: the recorder counts a write that does happen."""
        recorder = self._counted(monkeypatch)
        _append(statement="a claim that is written")
        assert recorder.total > 0, recorder.detail

    def test_an_out_of_vocabulary_kind_is_refused_before_any_command(self, monkeypatch):
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="kind must be one of"):
            ProvenanceJournal.append(
                agent_id=AGENT, statement="a claim", kind="annihilate"
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_an_assert_entry_carrying_a_target_is_refused_before_any_command(
        self, monkeypatch
    ):
        target = _append()
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="must not carry a target"):
            ProvenanceJournal.append(
                agent_id=AGENT,
                statement="a claim",
                kind="assert",
                target=target,
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_an_annotation_without_a_target_is_refused_before_any_command(
        self, monkeypatch
    ):
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="must name a target"):
            ProvenanceJournal.append(
                agent_id=AGENT, statement="a claim", kind="supersede", target=None
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_a_nonexistent_target_key_is_refused_before_any_command(self, monkeypatch):
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="does not exist"):
            ProvenanceJournal.supersede(
                "JournalEntry:no-such-entry", agent_id=AGENT, statement="a claim"
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_an_unsaved_target_instance_is_refused_before_any_command(
        self, monkeypatch
    ):
        unsaved = JournalEntry(agent_id=AGENT, statement="never saved", kind="assert")
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="does not exist"):
            ProvenanceJournal.supersede(
                unsaved, agent_id=AGENT, statement="a correction"
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_a_cross_agent_target_is_refused_before_any_command(self, monkeypatch):
        """``target`` is a full Redis key that can name another agent's
        partition, so a cross-agent close is refused rather than performed."""
        theirs = _append(agent_id=OTHER_AGENT, statement="their claim")
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="cross-agent"):
            ProvenanceJournal.supersede(
                theirs, agent_id=AGENT, statement="my correction"
            )
        assert counter.nonzero == {}, counter.nonzero
        assert _interval(theirs)[1] == float("inf")

    def test_a_backdated_instant_is_refused_before_any_command(self, monkeypatch):
        t0 = time.time()
        target = _append(at=t0)
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="precedes"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", at=t0 - 60.0
            )
        assert counter.nonzero == {}, counter.nonzero
        assert _interval(target)[1] == float("inf")
        assert ProvenanceJournal.annotations_for(target) == []

    def test_a_non_numeric_instant_is_refused_before_any_command(self, monkeypatch):
        target = _append()
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="epoch seconds"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", at="yesterday"
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_a_non_finite_instant_is_refused_before_any_command(self, monkeypatch):
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="finite"):
            ProvenanceJournal.append(
                agent_id=AGENT, statement="a claim", at=float("inf")
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_a_non_transactional_pipeline_is_refused_before_any_command(
        self, monkeypatch
    ):
        """``pipeline(transaction=False)`` is legal and would silently void the
        annotate-and-close atomicity guarantee."""
        target = _append()
        pipe = POPOTO_REDIS_DB.pipeline(transaction=False)
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="transaction=False"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", pipeline=pipe
            )
        assert counter.nonzero == {}, counter.nonzero
        assert list(pipe.command_stack) == []

    def test_a_non_pipeline_object_is_refused_before_any_command(self, monkeypatch):
        target = _append()
        counter = self._counted(monkeypatch)
        with pytest.raises(ValueError, match="must be a redis Pipeline"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", pipeline=object()
            )
        assert counter.nonzero == {}, counter.nonzero

    def test_pre_save_validates_kind_for_a_directly_constructed_entry(self):
        """Defence in depth behind the façade's pre-flight."""
        entry = JournalEntry(agent_id=AGENT, statement="a claim", kind="annihilate")
        with pytest.raises(ValueError, match="kind must be one of"):
            entry.save()


# ---------------------------------------------------------------------------
# F. Atomicity, fault injection, and the #588 regressions
# ---------------------------------------------------------------------------


class TestAnnotationAtomicity:
    """Shape assertions, never a literal total command count.

    A real ``JournalEntry`` queues the hash write, the class SADD, four indexed
    field EVALs, the tag commands, the validity ``open`` EVAL, the XADD, and the
    ``invalidate`` EVAL. That total is brittle to any field-set change, so the
    assertions below pin the properties that actually matter.
    """

    def test_the_annotate_and_close_sequence_is_one_transactional_pipeline(
        self, monkeypatch
    ):
        t0 = time.time() - 100.0
        target = _append(at=t0)

        spy = _PipelineSpy(monkeypatch)
        counter = _CallCounter(monkeypatch, ["eval"] + MUTATING_CLIENT_METHODS)
        result = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction", at=t0 + 50.0
        )

        invalidates = spy.evals(mode="invalidate")
        opens = spy.evals(mode="open")
        assert len(invalidates) == 1, f"expected one invalidate EVAL, got {invalidates}"
        assert len(opens) == 1, f"expected one open EVAL, got {len(opens)}"
        assert _numkeys(invalidates[0]) == 6

        # ARGV[1] is the new member, ARGV[7] the old — both must be non-empty
        # or the close is aimed at nothing.
        assert _as_str(invalidates[0][9]) == result.entry.db_key.redis_key
        assert _as_str(invalidates[0][15]) == target.db_key.redis_key

        pipe = spy.pipeline_of("invalidate")
        assert pipe is not None
        assert pipe.transaction is True

        assert (
            counter.nonzero == {}
        ), f"mutating calls were issued outside the pipeline: {counter.nonzero}"
        assert result.target_closed is True

    def test_a_caller_supplied_pipeline_is_returned_unexecuted(self, monkeypatch):
        """A fault before ``execute()`` applies nothing."""
        t0 = time.time() - 100.0
        target = _append(at=t0)

        pipe = POPOTO_REDIS_DB.pipeline()
        counter = _CallCounter(monkeypatch, ["eval"] + MUTATING_CLIENT_METHODS)
        result = ProvenanceJournal.supersede(
            target,
            agent_id=AGENT,
            statement="a correction",
            at=t0 + 50.0,
            pipeline=pipe,
        )

        assert result.pipeline is pipe
        assert len(pipe.command_stack) > 0
        assert counter.nonzero == {}, counter.nonzero

        # Nothing applied: the annotation is absent and the target is open.
        assert not POPOTO_REDIS_DB.exists(result.entry.db_key.redis_key)
        assert _interval(target)[1] == float("inf")

        spy_stack = list(pipe.command_stack)
        modes = [_supersede_mode(entry[0]) for entry in spy_stack]
        assert modes.count("invalidate") == 1
        assert modes.count("open") == 1

    def test_a_command_error_inside_exec_leaves_the_annotation_with_the_target_open(
        self,
    ):
        """The documented atomicity BOUNDARY, asserted as a known state.

        Redis ``MULTI``/``EXEC`` does not roll back sibling commands when one
        command errors at execute time. The property M1 claims is "no
        interleaving reader observes the annotation without the close" — not
        rollback. Bypassing D7's pre-flight to force a script error inside EXEC
        produces exactly the residual state the plan documents.
        """
        t0 = time.time()
        target = _append(at=t0)

        pipe = POPOTO_REDIS_DB.pipeline()
        annotation = JournalEntry(
            agent_id=AGENT,
            statement="a backdated correction",
            kind="supersede",
            target=target.db_key.redis_key,
            validity=t0 - 60.0,
        )
        annotation.save(pipeline=pipe)
        ValidityField.execute_supersede(
            JournalEntry,
            VALIDITY_FIELD_NAME,
            new_member=annotation.db_key.redis_key,
            mode="invalidate",
            now=t0 - 60.0,
            close_at=t0 - 60.0,
            old_member=target.db_key.redis_key,
            pipeline=pipe,
        )
        with pytest.raises(redis.exceptions.ResponseError):
            pipe.execute()

        # The annotation landed; the target did not close. Not a rollback.
        assert POPOTO_REDIS_DB.exists(annotation.db_key.redis_key)
        assert _interval(target)[1] == float("inf")

    def test_bypassing_the_pre_flight_surfaces_a_raw_response_error(self):
        """#588 pin: the reason D7 step 4 exists cannot be refactored away.

        ``execute_supersede`` remaps ``CLOSE_BEFORE_START`` to ``ValueError``
        only on the non-pipeline branch, and its client-side pre-check compares
        ``close_at`` against the *caller-supplied* ``valid_from`` — which M1
        sets to the same instant, so it never fires. Without M1's pre-read of
        the target's *stored* ``valid_from``, a genuine backdate surfaces here:
        a raw ``ResponseError`` out of ``pipe.execute()``, with the annotation
        already written.
        """
        t0 = time.time()
        target = _append(at=t0)
        backdated = t0 - 60.0

        pipe = POPOTO_REDIS_DB.pipeline()
        annotation = JournalEntry(
            agent_id=AGENT,
            statement="a backdated correction",
            kind="supersede",
            target=target.db_key.redis_key,
            validity=backdated,
        )
        annotation.save(pipeline=pipe)
        # The same instant for both, exactly as M1's write path passes them --
        # which is why the client-side pre-check does not fire.
        ValidityField.execute_supersede(
            JournalEntry,
            VALIDITY_FIELD_NAME,
            new_member=annotation.db_key.redis_key,
            mode="invalidate",
            now=backdated,
            valid_from=backdated,
            ingested_at=backdated,
            close_at=backdated,
            old_member=target.db_key.redis_key,
            pipeline=pipe,
        )
        with pytest.raises(redis.exceptions.ResponseError):
            pipe.execute()
        assert POPOTO_REDIS_DB.exists(annotation.db_key.redis_key)

        # And M1's own path refuses the same call before writing anything.
        with pytest.raises(ValueError, match="precedes"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", at=backdated
            )

    def test_supersession_protocol_silently_no_ops_for_a_pipelined_successor(self):
        """#588 finding 1, pinned against raw V0 and against M1's write path.

        ``SupersessionProtocol`` resolves member keys through
        ``POPOTO_REDIS_DB.exists(...)``. The successor's HSET is only *queued*,
        so ``EXISTS`` returns 0, the call takes its "unsaved successor -> no-op"
        branch, and returns ``None`` — indistinguishable from its normal
        pipeline-mode return. That is why M1 calls ``execute_supersede``
        directly.
        """
        t0 = time.time() - 100.0
        target = _append(at=t0)

        pipe = POPOTO_REDIS_DB.pipeline()
        successor = JournalEntry(
            agent_id=AGENT, statement="a correction", kind="assert", validity=t0 + 50.0
        )
        successor.save(pipeline=pipe)
        SupersessionProtocol.invalidate(target, superseded_by=successor, pipeline=pipe)
        stack = list(pipe.command_stack)
        pipe.execute()

        modes = [_supersede_mode(entry[0]) for entry in stack]
        assert "invalidate" not in modes, "V0 silently queued nothing (#588)"
        assert _interval(target)[1] == float("inf")
        keys = _keys()
        assert POPOTO_REDIS_DB.hlen(keys["chain_fwd"]) == 0

        # M1's write path closes the same target, in one transaction.
        second_target = _append(at=t0)
        ProvenanceJournal.supersede(second_target, agent_id=AGENT, at=t0 + 50.0)
        assert _interval(second_target)[1] == pytest.approx(t0 + 50.0)
        assert (
            _as_str(
                POPOTO_REDIS_DB.hget(keys["chain_fwd"], second_target.db_key.redis_key)
            )
            != "None"
        )

    def test_valid_time_is_taken_from_construction_not_from_the_supersede_argv(self):
        """#588 finding 2: ``ZADD NX`` makes the script's ``valid_from`` a no-op.

        The successor's own ``open`` EVAL runs earlier in the pipeline, so a
        ``valid_from`` passed only to ``execute_supersede`` is silently replaced
        by the save clock. M1 sets the instant at construction instead, and the
        stored value must be exact — not merely close.
        """
        t0 = time.time() - 100.0
        target = _append(at=t0)
        requested = t0 + 50.0

        result = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction", at=requested
        )
        stored_valid_from, _, _ = _interval(result.entry)
        assert stored_valid_from == requested

        # The raw shape M1 routes around: pass the instant ONLY to the script.
        raw_target = _append(at=t0)
        pipe = POPOTO_REDIS_DB.pipeline()
        raw_successor = JournalEntry(
            agent_id=AGENT, statement="raw correction", kind="assert"
        )
        raw_successor.save(pipeline=pipe)
        ValidityField.execute_supersede(
            JournalEntry,
            VALIDITY_FIELD_NAME,
            new_member=raw_successor.db_key.redis_key,
            mode="invalidate",
            now=requested,
            valid_from=requested,
            ingested_at=requested,
            close_at=requested,
            old_member=raw_target.db_key.redis_key,
            pipeline=pipe,
        )
        pipe.execute()
        raw_valid_from, _, _ = _interval(raw_successor)
        assert raw_valid_from != requested, (
            "V0's ZADD NX skew is gone -- #588's second finding may be fixed; "
            "re-check M1's construction-time valid_from workaround"
        )

    def test_an_xadd_failure_inside_the_pipeline_aborts_the_whole_annotation(
        self, monkeypatch
    ):
        """``EventStreamMixin`` re-raises in pipeline mode, so a stream failure
        must take the annotation down with it rather than committing half."""
        t0 = time.time() - 100.0
        target = _append(at=t0)

        real_pipeline = POPOTO_REDIS_DB.pipeline

        def make_pipeline(*args, **kwargs):
            pipe = real_pipeline(*args, **kwargs)

            def exploding_xadd(*xargs, **xkwargs):
                raise RuntimeError("injected XADD failure")

            pipe.xadd = exploding_xadd
            return pipe

        monkeypatch.setattr(POPOTO_REDIS_DB, "pipeline", make_pipeline)
        with pytest.raises(RuntimeError, match="injected XADD failure"):
            ProvenanceJournal.supersede(
                target, agent_id=AGENT, statement="a correction", at=t0 + 50.0
            )
        monkeypatch.undo()

        assert _interval(target)[1] == float("inf")
        assert ProvenanceJournal.annotations_for(target) == []


# ---------------------------------------------------------------------------
# G. The validity-coupling kill switch
# ---------------------------------------------------------------------------


class TestCouplingKillSwitch:
    def test_the_env_var_is_read_as_a_disable_so_default_on_holds(self, monkeypatch):
        monkeypatch.delenv("POPOTO_JOURNAL_COUPLING_DISABLE", raising=False)
        assert _read_journal_coupling_switch() is True
        for value in ("1", "true", "TRUE", "yes", "on"):
            monkeypatch.setenv("POPOTO_JOURNAL_COUPLING_DISABLE", value)
            assert _read_journal_coupling_switch() is False, value

    def test_an_uncoupled_supersede_issues_the_command_set_of_a_bare_append(
        self, monkeypatch
    ):
        """Not "byte-identical" — a clock value enters ARGV, so two runs differ
        by construction. The assertable property is the command *set*: the
        invalidate EVAL is absent and the EVAL count matches a plain append."""
        t0 = time.time() - 100.0
        target = _append(at=t0)

        baseline_spy = _PipelineSpy(monkeypatch)
        _append(at=t0 + 10.0, statement="a plain append")
        baseline_evals = len(baseline_spy.evals())
        monkeypatch.undo()
        assert baseline_evals > 0, "the EVAL comparison would be vacuous"

        Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False
        spy = _PipelineSpy(monkeypatch)
        result = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction", at=t0 + 50.0
        )

        assert spy.evals(mode="invalidate") == []
        assert len(spy.evals()) == baseline_evals
        assert len(spy.evals(mode="open")) == 1
        assert result.target_closed is False
        assert _interval(target)[1] == float("inf")

    def test_the_degraded_mode_is_detectable_without_reading_redis(self):
        target = _append()
        Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False
        result = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction"
        )
        assert result.target_closed is False
        assert result.coupling_enabled is False
        assert result.entry.target == target.db_key.redis_key

    def test_an_uncoupled_supersede_still_appends_and_still_records_the_target(self):
        target = _append()
        Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False
        result = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction"
        )
        assert _redis_keys(ProvenanceJournal.annotations_for(target)) == [
            result.entry.db_key.redis_key
        ]
        # Membership degrades to "everything ever appended" -- pre-M1 behavior.
        assert target.db_key.redis_key in _redis_keys(
            JournalEntry.query.filter(validity__current=True)
        )

    def test_the_uncoupled_warning_is_emitted_once_per_process(self, caplog):
        Defaults.JOURNAL_VALIDITY_COUPLING_ENABLED = False
        first = _append()
        second = _append()
        with caplog.at_level("WARNING", logger="POPOTO.ProvenanceJournal"):
            ProvenanceJournal.supersede(first, agent_id=AGENT, statement="one")
            ProvenanceJournal.supersede(second, agent_id=AGENT, statement="two")
        warnings = [r for r in caplog.records if "COUPLING_DISABLE" in r.getMessage()]
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# H. Retention escape hatch, and the two remaining race shapes
# ---------------------------------------------------------------------------


def _derived_keys_naming(member):
    """Return every ``$*`` key still holding ``member`` anywhere."""
    holders = []
    for key in scan_keys("$*"):
        key = _as_str(key)
        key_type = _as_str(POPOTO_REDIS_DB.type(key))
        if key_type == "set":
            values = [_as_str(m) for m in POPOTO_REDIS_DB.smembers(key)]
        elif key_type == "zset":
            values = [_as_str(m) for m in POPOTO_REDIS_DB.zrange(key, 0, -1)]
        elif key_type == "hash":
            blob = POPOTO_REDIS_DB.hgetall(key)
            values = [_as_str(f) for f in blob] + [_as_str(v) for v in blob.values()]
        elif key_type == "string":
            values = [_as_str(POPOTO_REDIS_DB.get(key))]
        else:
            values = []
        if any(member in value for value in values) or member in key:
            holders.append(key)
    return holders


class TestHardDelete:
    def test_hard_delete_removes_the_record_and_every_derived_trace(self):
        t0 = time.time() - 100.0
        target = _append(
            at=t0, speaker="tom", turn_id="t-41", subjects=["launch", "tom"]
        )
        annotation = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="a correction", at=t0 + 50.0
        ).entry
        member = target.db_key.redis_key

        assert JournalEntry.hard_delete(target) is True

        assert not POPOTO_REDIS_DB.exists(member)
        assert JournalEntry.query.filter(agent_id=AGENT, speaker="tom") == []
        assert JournalEntry.query.filter(turn_id="t-41") == []
        assert JournalEntry.query.filter(subjects__contains="launch") == []
        assert _derived_keys_naming(member) == []
        # The annotation itself survives -- only the erased record is swept.
        assert POPOTO_REDIS_DB.exists(annotation.db_key.redis_key)

    def test_hard_delete_clears_the_value_side_of_the_chain_hashes(self):
        t0 = time.time() - 100.0
        first = _append(at=t0)
        second = ProvenanceJournal.supersede(
            first, agent_id=AGENT, statement="a correction", at=t0 + 50.0
        ).entry
        member = second.db_key.redis_key

        JournalEntry.hard_delete(second)
        keys = _keys()
        fwd = POPOTO_REDIS_DB.hgetall(keys["chain_fwd"])
        assert all(_as_str(v) != member for v in fwd.values())
        assert _derived_keys_naming(member) == []


class TestOrphanIndexRead:
    def test_an_index_member_with_no_hash_is_skipped_rather_than_raising(self):
        """Race 1: indexed-field EVALs run eagerly, ahead of the internal
        pipeline, so a crash between the two leaves an index entry pointing at
        a nonexistent hash. The query layer must skip it, not raise."""
        target = _append()
        target_key = target.db_key.redis_key
        annotation = ProvenanceJournal.confirm(
            target, agent_id=AGENT, statement="agreed"
        ).entry

        index_field = JournalEntry._meta.fields["target"]
        prefix = index_field.get_special_use_field_db_key(JournalEntry, "target")
        index_key = popoto.models.db_key.DB_key(prefix, target_key).redis_key
        POPOTO_REDIS_DB.sadd(index_key, "JournalEntry:ghost-entry-that-never-existed")

        found = _redis_keys(JournalEntry.query.filter(target=target_key))
        assert found == [annotation.db_key.redis_key]

    def test_a_fully_orphaned_index_returns_empty_rather_than_raising(self):
        target = _append()
        ghost_target = "JournalEntry:no-such-target"
        index_field = JournalEntry._meta.fields["target"]
        prefix = index_field.get_special_use_field_db_key(JournalEntry, "target")
        index_key = popoto.models.db_key.DB_key(prefix, ghost_target).redis_key
        POPOTO_REDIS_DB.sadd(index_key, "JournalEntry:ghost-annotation")

        assert JournalEntry.query.filter(target=ghost_target) == []
        assert POPOTO_REDIS_DB.exists(target.db_key.redis_key)


class TestConcurrentDoubleClose:
    def test_two_annotations_closing_one_target_apply_exactly_one_close(self):
        """Race 3. ``SUPERSEDE_LUA``'s idempotency guard requires
        ``invalid_at == +inf`` before closing, so the second close is a
        server-side no-op while its entry still appends. Both annotations are
        real provenance; exactly one close applies."""
        t0 = time.time() - 200.0
        target = _append(at=t0)

        first = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="correction one", at=t0 + 50.0
        )
        second = ProvenanceJournal.supersede(
            target, agent_id=AGENT, statement="correction two", at=t0 + 100.0
        )

        assert first.target_closed is True
        assert second.target_closed is False

        annotations = _redis_keys(ProvenanceJournal.annotations_for(target))
        assert annotations == sorted(
            [first.entry.db_key.redis_key, second.entry.db_key.redis_key]
        )
        assert _interval(target)[1] == pytest.approx(t0 + 50.0)
        assert target.db_key.redis_key not in _redis_keys(
            JournalEntry.query.filter(validity__current=True)
        )
