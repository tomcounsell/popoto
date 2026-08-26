"""
The write-side audit store for auditable extraction (M3).

Every candidate the generator produces gets a row here, and every row ends
in exactly one **terminal** state -- ``firewall_drop | accept | reject |
withhold`` (issue #562). Extraction precision and recall become computable
offline from these rows alone: "audit by construction".

Two properties carry the whole design, and both are structural rather than
conventional -- neither depends on a caller remembering to do the right
thing.

**1. The row is identified by the candidate, not by a mint-on-save id.**
``agent_id``, ``turn_id`` and ``candidate_id`` are *all* ``KeyField``s, so
the composite Redis key **is** the candidate's identity and re-saving the
same tuple transitions that row **in place**. Do not "fix" this by copying
the sibling :class:`~popoto.recipes.provenance_journal.JournalEntry`, which
uses ``entry_id = AutoKeyField()``: an ``AutoKeyField`` mints a brand-new
row on every save, which is right for an append-only journal and fatal
here. It would leave two rows behind every ``pending`` -> terminal
transition, make the terminal-write guard read an empty row and never
refuse, and make ``list_pending`` return already-reconciled rows forever.
``AutoKeyField`` is forbidden on :class:`DecisionRecord`.

Two consequences of composite keying worth knowing before you read a key
by eye or by position:

- KeyFields join **alphabetically**, not in declaration order
  (``models/base.py:284-301``), so the Redis key is
  ``DecisionRecord:<agent_id>:<candidate_id>:<turn_id>`` -- ``candidate_id``
  in the middle.
- ``DB_key`` escapes colons and glob characters inside values
  (``models/db_key.py:86-88``), so a ``candidate_id`` of ``t-41:sent:0``
  renders as ``t/-41{&#58;}sent{&#58;}0``. That is correct; do not "fix" it.

**2. The decision log is written before every irreversible side effect.**
A candidate never reaches a side effect that has no row already describing
it:

- ``firewall_drop`` / ``reject`` / ``withhold`` have no downstream side
  effect, so their first write is also their terminal write.
- ``accept`` is the only two-phase path: :meth:`DecisionLog.write_pending`
  commits a **non-terminal** ``pending`` row *before* assembly calls
  ``ProvenanceJournal.append()``, and :meth:`DecisionLog.write_terminal`
  transitions that same row afterwards. ``pending`` is not a fifth terminal
  state -- it is a *visible unfinished write*, which is the opposite of a
  silent drop. A surviving ``pending`` row means a process died
  mid-assembly.

Recovering stale ``pending`` rows is **manual in v1** -- nothing sweeps,
expires or alerts on them, and decision rows deliberately carry no TTL
(a TTL would delete the audit evidence this module exists to keep). The
operator recipe is:

    log = DecisionLog()
    for row in log.list_pending("agent-7"):        # oldest-first
        print(row.turn_id, row.candidate_id, row.written_at)
    # then re-invoke the auditable path for each stale (agent_id, turn_id);
    # assembly's identity probe reconciles it.

A periodic age-keyed sweep, an alert threshold and any dashboard over this
log are M9 (#568) follow-ons, not v1. Detail rows ship **unbounded** on
purpose: the right retention horizon cannot be known until M9 consumes the
log, so v1 declines to guess a number. There is no ``LTRIM`` here and no
``Defaults`` cap constant for this log.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Union,
    cast,
)
from uuid import uuid4

import msgpack

from ..exceptions import JournalBlockedError
from ..fields.constants import Defaults
from ..models.base import Model
from ..models.db_key import DB_key
from ..models.encoding import encode_popoto_model_obj
from ..models.query import Query
from ..redis_db import ENCODING, POPOTO_REDIS_DB
from ..fields.shortcuts import FloatField, IntField, KeyField, StringField
from .verdict import TERMINAL_VERDICTS, ReasonCode, Verdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .candidates import Candidate

logger = logging.getLogger("POPOTO.extraction")

CLAIM_KEY_PREFIX = "popoto:m3:claim"
"""Prefix for the ephemeral assembly-claim keys.

A separate, extraction-owned keyspace from the decision rows. Claim keys
carry a TTL and hold no audit content, so losing one costs at most a
reprobe -- never a record.
"""

CLAIM_RELEASE_LUA = """
-- KEYS[1] = the claim key, ARGV[1] = this runner's token
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
"""Token-checked release. A runner never deletes a claim it lost."""

_NOT_RECONCILED = object()
"""Sentinel: the identity probe found no prior entry; proceed to append."""

SUMMARY_KEY_PREFIX = "popoto:m3:summary"
"""Prefix for the per-turn compact summary hash.

The summary is a **convenience index, not a completeness fallback** --
detail rows are unbounded and remain the sole source of truth. It exists
so per-turn terminal-state counts are O(1) instead of a scan over every
candidate row for the turn. Any summary/detail disagreement is resolved in
favour of the detail rows.
"""


class DecisionRecord(Model):
    """One candidate's decision row, keyed by the candidate's identity.

    ``agent_id`` + ``turn_id`` + ``candidate_id`` form the composite key,
    so writing the same tuple twice transitions one row rather than
    creating two. See the module docstring for why ``AutoKeyField`` is
    forbidden here and for the alphabetical key-join order.

    Every field below is a plain (non-indexed) field on purpose. Popoto
    routes ``IndexedFieldMixin`` fields out of the base ``HSET`` and into
    ``INDEX_SWAP_LUA`` (``models/base.py:1470-1480``), so an indexed
    ``state`` would be invisible to the guard script's ``HGET`` and its
    index would be silently desynchronised by the guard's ``HSET``.
    ``list_pending`` filters in Python instead; it is an operator recovery
    reader, not a hot path.

    Attributes:
        agent_id: Owning agent. KeyField.
        turn_id: The turn the candidate was generated from. KeyField.
        candidate_id: ``{turn_id}:{generator_rule}:{ordinal}``. KeyField.
        state: A :class:`~popoto.extraction.verdict.Verdict` value -- one
            of the four terminal states, or the non-terminal ``pending``.
        reason_code: A :class:`~popoto.extraction.verdict.ReasonCode`
            value.
        generator_rule: Which deterministic rule produced the candidate,
            so offline metrics can break results down per rule.
        span_start: Candidate span start offset in the turn text.
        span_end: Candidate span end offset in the turn text.
        text_hash: SHA-256 of the candidate text. A digest is safe *here*
            because nothing scans a decision row; the ``cand:`` journal
            subject tag is the thing that must stay low-entropy -- a
            digest-shaped ``candidate_id`` would be blocked by the
            journal's own write-time firewall as ``high_entropy``
            (``popoto.privacy.never_record``), which is why
            :func:`~popoto.extraction.candidates.generate_candidates`
            mints ``candidate_id`` as ``{turn_id}:{generator_rule}:{ordinal}``
            rather than a hash.
        entry_id: The journal entry id, set on a terminal ``accept``.
            Empty on every other state.
        detail_code: Free-form diagnostic string, written only by trusted
            code. See :attr:`DecisionLog.CONFLICT_REFUSED`.
        written_at: Unix timestamp, stamped on **every** write -- both the
            ``pending`` write and the terminal transition. ``list_pending``
            has nothing to sort or threshold on without it.
    """

    agent_id = KeyField()
    turn_id = KeyField()
    candidate_id = KeyField()
    state = StringField(default="")
    reason_code = StringField(default="")
    generator_rule = StringField(default="")
    span_start = IntField(null=True)
    span_end = IntField(null=True)
    text_hash = StringField(default="")
    entry_id = StringField(default="")
    detail_code = StringField(default="")
    written_at = FloatField(null=True)

    @property
    def is_terminal(self) -> bool:
        """True when :attr:`state` is one of the four terminal states."""
        try:
            return Verdict(self.state) in TERMINAL_VERDICTS
        except ValueError:
            return False


# ---------------------------------------------------------------------------
# The guarded terminal write.
#
# One conditional Lua script, EVAL'd, is the ONLY way any terminal state is
# written. There is deliberately no bare HSET / .save() fast path -- not even
# for the pre-LLM firewall_drop, which cannot conflict in practice because no
# prior row exists for a fresh candidate.
#
# The rule it enforces: a terminal write must not overwrite a row that is
# already terminal ``accept`` carrying an ``entry_id``. That row has a journal
# entry physically behind it, so keeping it is the only outcome that leaves
# the decision log agreeing with the journal. Only the ``accept`` path is
# claim-protected, and the LLM verdict is non-deterministic, so a retried
# verdict resolving ``reject`` would otherwise permanently split the log (the
# source of truth for metrics) from the journal (the source of truth for
# stored memories).
#
# Why Lua and not the two mechanisms an earlier plan revision named:
#   - MULTI/EXEC queues commands blind. Nothing inside the transaction can
#     read ``state`` and branch on it; that needs WATCH (a dedicated
#     connection plus a retry loop, which does not compose with Popoto's
#     shared pool) or Lua.
#   - The ``SET NX`` assembly claim only buys mutual exclusion on a key and
#     never inspects ``state``. Non-``accept`` verdicts take no claim at all,
#     so a retried ``reject`` would trivially win ``SET NX`` on an unclaimed
#     key and write unguarded.
#
# EVAL plus HGET/HSET/HINCRBY/SADD are core commands -- Valkey-safe, no
# modules -- and EVAL is an established in-repo pattern
# (``models/query.py:445,463``, ``fields/existence_filter.py:430``).
#
# Values in a Popoto model hash are msgpack-packed, so the script compares
# and writes *packed* bytes supplied from Python. It never builds a value
# itself.
# ---------------------------------------------------------------------------

TERMINAL_WRITE_LUA = """
-- KEYS[1] = the DecisionRecord hash for (agent_id, turn_id, candidate_id)
-- KEYS[2] = the per-turn compact summary hash
-- KEYS[3] = the model's class set
-- KEYS[4..6] = the KeyField secondary index Sets for agent_id,
--           candidate_id and turn_id
--
-- KEYS[3..6] are the membership bookkeeping Model.save() would normally do
-- through KeyFieldMixin.on_save. This script writes the row hash directly,
-- so on_save never runs; without these SADDs a row whose FIRST write is
-- terminal would sit in Redis in full while Model.query.all() and
-- Model.query.filter() both failed to see it.
-- ARGV[1] = msgpack-packed 'accept'
-- ARGV[2] = msgpack-packed ''        (the empty-entry_id sentinel)
-- ARGV[3] = msgpack-packed 'terminal_conflict_refused'
-- ARGV[4] = msgpack-packed 'pending'
-- ARGV[5] = number of summary fields to increment (n)
-- ARGV[6 .. 5+n]  = those summary field names
-- ARGV[6+n ..]    = flat field/packed-value pairs for the row
local prev_state = redis.call('HGET', KEYS[1], 'state')
local prev_entry = redis.call('HGET', KEYS[1], 'entry_id')

if prev_state == ARGV[1] and prev_entry and prev_entry ~= ARGV[2] then
    -- Already assembled. The accept row and its entry_id stand unchanged;
    -- the refusal is recorded on that same row. No second row, no new
    -- state, no exception to the caller.
    redis.call('HSET', KEYS[1], 'detail_code', ARGV[3])
    return 0
end

-- The summary aggregates TERMINAL states only, and counts each candidate
-- once: bump only when the row is new or still non-terminal 'pending'.
if prev_state == false or prev_state == nil or prev_state == ARGV[4] then
    local n = tonumber(ARGV[5])
    for i = 1, n do
        redis.call('HINCRBY', KEYS[2], ARGV[5 + i], 1)
    end
end

redis.call('SADD', KEYS[3], KEYS[1])
redis.call('SADD', KEYS[4], KEYS[1])
redis.call('SADD', KEYS[5], KEYS[1])
redis.call('SADD', KEYS[6], KEYS[1])
redis.call('HSET', KEYS[1], unpack(ARGV, 6 + tonumber(ARGV[5])))
return 1
"""


def _packb(value: str) -> bytes:
    """Pack a str the way ``encode_popoto_model_obj`` packs field values."""
    return msgpack.packb(value)


def _enum_value(value: Union[Verdict, ReasonCode, str]) -> str:
    """Accept an enum member or its raw value; return the raw value."""
    return value.value if hasattr(value, "value") else value


def hash_candidate_text(text: str) -> str:
    """SHA-256 of the candidate text, for tamper-evidence on the row."""
    return hashlib.sha256((text or "").encode(ENCODING)).hexdigest()


class DecisionLog:
    """Writer/reader over :class:`DecisionRecord` rows.

    Stateless -- every method takes the identity it operates on -- so one
    instance can serve any agent. All writes go through Redis/Valkey core
    commands and Lua only.

    Example::

        log = DecisionLog()
        log.write_terminal(
            agent_id="agent-7",
            candidate=candidate,
            state=Verdict.FIREWALL_DROP,
            reason_code=ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
        )
    """

    CONFLICT_REFUSED = "terminal_conflict_refused"
    """``detail_code`` recorded when a terminal write is refused.

    ``detail_code`` is a free-form diagnostic **string**, not an enum: it
    carries three structurally different payloads by design -- this fixed
    literal, an exception class name on ``assembly_failed``, and
    ``",".join(entry_ids)`` on ``ambiguous_reconciliation``. That does not
    weaken the enums-only constraint on the model's output: ``state`` and
    ``reason_code`` are genuine single-value enums, and ``detail_code`` is
    written exclusively by trusted code, never by the LLM.
    """

    def __init__(self, redis_client: Any = None):
        """Args:
        redis_client: Redis/Valkey client. Defaults to Popoto's.
        """
        self._redis = redis_client if redis_client is not None else POPOTO_REDIS_DB

    # -- keys ------------------------------------------------------------

    @staticmethod
    def row_key(agent_id: str, turn_id: str, candidate_id: str) -> str:
        """The Redis key for one candidate's row.

        Built through ``DB_key`` rather than by string formatting, so the
        alphabetical join order and the colon/glob escaping stay in one
        place (the model) instead of being re-derived by every caller.
        """
        return DecisionRecord(
            agent_id=agent_id, turn_id=turn_id, candidate_id=candidate_id
        ).db_key.redis_key

    @staticmethod
    def summary_key(agent_id: str, turn_id: str) -> str:
        """The per-turn compact summary hash key."""
        return f"{SUMMARY_KEY_PREFIX}:{agent_id}:{turn_id}"

    @staticmethod
    def claim_key(agent_id: str, turn_id: str, candidate_id: str) -> str:
        """The ephemeral assembly-claim key for one candidate.

        Carries a TTL and holds no audit content -- losing it costs at most
        a re-probe, never a record.
        """
        return f"{CLAIM_KEY_PREFIX}:{agent_id}:{turn_id}:{candidate_id}"

    @staticmethod
    def key_field_index_keys(
        agent_id: str, turn_id: str, candidate_id: str
    ) -> List[str]:
        """The KeyField secondary index Sets for one row's key values.

        Built with ``KeyFieldMixin``'s own key builder rather than by
        formatting the ``$KeyF:`` pattern here, so the guard script SADDs
        into exactly the Sets ``on_save`` would have.
        """
        values = {
            "agent_id": agent_id,
            "turn_id": turn_id,
            "candidate_id": candidate_id,
        }
        return [
            DB_key(
                KeyField.get_special_use_field_db_key(DecisionRecord, field_name),
                value,
            ).redis_key
            for field_name, value in values.items()
        ]

    # -- writes ----------------------------------------------------------

    def write_pending(
        self,
        agent_id: str,
        candidate: "Candidate",
        reason_code: Union[ReasonCode, str] = ReasonCode.ACCEPTED,
    ) -> DecisionRecord:
        """Commit the non-terminal ``pending`` row for an accepted candidate.

        Phase 1 of the two-phase ``accept`` path. This write is committed
        **before** ``ProvenanceJournal.append()`` is called, never
        pipelined with it -- that ordering is what guarantees no candidate
        can reach an irreversible side effect with zero decision-log rows.

        No summary update accompanies it: the per-turn summary aggregates
        terminal states only, and ``pending`` is never counted into it.

        Args:
            agent_id: Owning agent. Never ``None``.
            candidate: The candidate being assembled.
            reason_code: The verdict's reason code, carried forward.

        Returns:
            The saved :class:`DecisionRecord`.
        """
        record = DecisionRecord(
            agent_id=agent_id,
            turn_id=candidate.turn_id,
            candidate_id=candidate.candidate_id,
            state=Verdict.PENDING.value,
            reason_code=_enum_value(reason_code),
            generator_rule=candidate.generator_rule,
            span_start=candidate.start,
            span_end=candidate.end,
            text_hash=hash_candidate_text(candidate.text),
            entry_id="",
            detail_code="",
            written_at=time.time(),
        )
        record.save()
        logger.debug(
            "decision log: pending row committed for %s/%s",
            agent_id,
            candidate.candidate_id,
        )
        return record

    def write_terminal(
        self,
        agent_id: str,
        candidate: "Candidate",
        state: Union[Verdict, str],
        reason_code: Union[ReasonCode, str],
        entry_id: str = "",
        detail_code: str = "",
    ) -> bool:
        """Write a terminal state through the guarded script.

        This is the **only** way any terminal state is written. Every
        terminal state routes through it, including the pre-LLM
        ``firewall_drop`` that cannot conflict in practice -- there is no
        fast path to bypass.

        The guard applies uniformly rather than only to non-``accept``
        writes. Applying it to ``accept`` too costs nothing and removes the
        last conditional branch: the legitimate ``pending`` -> ``accept``
        transition is never refused (a ``pending`` row is not ``accept``
        with an ``entry_id``), and the only ``accept`` write it does refuse
        is a duplicate assembly of an already-assembled candidate, where
        refusing is correct.

        Args:
            agent_id: Owning agent.
            candidate: The candidate being decided.
            state: A terminal :class:`~popoto.extraction.verdict.Verdict`.
            reason_code: A :class:`~popoto.extraction.verdict.ReasonCode`.
            entry_id: Journal entry id. Required on ``accept``, empty
                elsewhere.
            detail_code: Free-form trusted diagnostic string.

        Returns:
            ``True`` if the row was written, ``False`` if the write was
            refused because the row is already terminal ``accept`` with an
            ``entry_id``. A refusal is not an error and never raises: the
            existing row stands and its ``detail_code`` becomes
            :attr:`CONFLICT_REFUSED`.

        Raises:
            ValueError: If ``state`` is not one of the four terminal
                states -- writing ``pending`` here would defeat the
                two-phase ordering, so it is rejected loudly rather than
                silently accepted.
        """
        verdict = Verdict(_enum_value(state))
        if verdict not in TERMINAL_VERDICTS:
            raise ValueError(
                f"write_terminal requires a terminal state, got {verdict.value!r}. "
                "Use write_pending() for the non-terminal pending marker."
            )
        reason = _enum_value(reason_code)

        record = DecisionRecord(
            agent_id=agent_id,
            turn_id=candidate.turn_id,
            candidate_id=candidate.candidate_id,
            state=verdict.value,
            reason_code=reason,
            generator_rule=candidate.generator_rule,
            span_start=candidate.start,
            span_end=candidate.end,
            text_hash=hash_candidate_text(candidate.text),
            entry_id=entry_id,
            detail_code=detail_code,
            written_at=time.time(),
        )
        row_fields: List[bytes] = []
        for field_name, packed in encode_popoto_model_obj(record).items():
            row_fields.append(field_name)
            row_fields.append(packed)

        summary_fields = [
            f"state:{verdict.value}",
            f"reason:{reason}",
        ]

        written = self._redis.eval(
            TERMINAL_WRITE_LUA,
            # numkeys: row + summary + class set + one index Set per
            # KeyField. Undercounting here would shunt an index Set into
            # ARGV and silently stop maintaining it.
            6,
            self.row_key(agent_id, candidate.turn_id, candidate.candidate_id),
            self.summary_key(agent_id, candidate.turn_id),
            DecisionRecord._meta.db_class_set_key.redis_key,
            *self.key_field_index_keys(
                agent_id, candidate.turn_id, candidate.candidate_id
            ),
            _packb(Verdict.ACCEPT.value),
            _packb(""),
            _packb(self.CONFLICT_REFUSED),
            _packb(Verdict.PENDING.value),
            len(summary_fields),
            *summary_fields,
            *row_fields,
        )
        if not written:
            logger.warning(
                "decision log: terminal %s write refused for %s/%s -- row is "
                "already terminal accept with an entry_id",
                verdict.value,
                agent_id,
                candidate.candidate_id,
            )
        return bool(written)

    # -- the atomic assembly claim ---------------------------------------

    def acquire_claim(
        self, agent_id: str, turn_id: str, candidate_id: str
    ) -> Optional[str]:
        """Claim a candidate for assembly. One atomic op, not read-then-act.

        Without this, two runners over the same candidate -- a duplicated
        delivery racing a crash-retry, both seeing a surviving ``pending``
        -- can both probe, both find nothing (neither has committed
        ``append()`` yet), and both append. That produces **two permanent
        journal entries**, and the journal cannot catch it: ``append()``
        takes no idempotency key and ``AppendOnlyViolation`` fires only when
        a record's Redis key already exists, which is never true for a fresh
        ``AutoKeyField`` append.

        ``SET ... NX PX`` is one round trip and a core command on both Redis
        and Valkey. It is chosen over ``WATCH``/``MULTI`` compare-and-set
        deliberately: ``WATCH`` needs a dedicated connection held across the
        transaction plus a retry loop, which does not compose with Popoto's
        shared pool. Both are Valkey-safe; ``SET NX`` is smaller.

        Returns:
            This runner's token if the claim was won, else ``None``. A
            runner holding ``None`` must perform **no** journal write and
            **no** row transition -- it no-ops and leaves the candidate to
            the winner.
        """
        token = uuid4().hex
        won = self._redis.set(
            self.claim_key(agent_id, turn_id, candidate_id),
            token,
            nx=True,
            px=Defaults.M3_ASSEMBLY_CLAIM_TTL_MS,
        )
        return token if won else None

    def release_claim(
        self, agent_id: str, turn_id: str, candidate_id: str, token: str
    ) -> bool:
        """Release a claim this runner still owns.

        Token-checked so a runner can never delete a claim it no longer
        owns -- if the TTL expired and another runner re-claimed, the DEL
        would otherwise hand that runner's candidate to a third.
        """
        released = self._redis.eval(
            CLAIM_RELEASE_LUA,
            1,
            self.claim_key(agent_id, turn_id, candidate_id),
            token,
        )
        return bool(released)

    # -- assembly --------------------------------------------------------

    def assemble(
        self,
        agent_id: str,
        candidate: "Candidate",
        journal: Any,
        speaker: Optional[str] = None,
        topic_tags: Sequence[str] = (),
    ) -> Optional[str]:
        """Assemble one accepted candidate into the provenance journal.

        The full ordering, which is the reason this is one function rather
        than a few helpers a caller sequences by hand: **claim -> read row
        -> (probe) -> pending -> append -> terminal transition -> release**.

        Dedup is owned here and cannot be delegated to the journal:
        ``append()`` accepts no idempotency key, ``JournalEntry.entry_id``
        is an ``AutoKeyField``, and M1 is append-only with no delete path,
        so a duplicate entry would be permanent.

        The four-case probe on the existing row:

        - terminal ``accept`` with an ``entry_id`` -- already assembled,
          skip entirely.
        - any other terminal state -- already decided, skip.
        - ``pending`` -- a retry of an interrupted assembly. Reconcile by
          **candidate identity** (see below) before considering a
          re-append.
        - absent -- fresh candidate. Write ``pending``, then ``append()``.

        Reconciliation matches the ``cand:{candidate_id}`` subject tag, not
        ``verbatim`` text. Text matching is unsound here and the plan
        withdrew it: ``verbatim`` is not unique per candidate within a turn
        by construction -- a repeated sentence, or a sentence span whose
        text equals an entity-lifted span, produces two candidates with
        identical ``verbatim``, and a ``pending`` row could reconcile onto
        the *other* candidate's entry and record the wrong ``entry_id``.

        Args:
            agent_id: Owning agent. Required and never ``None`` -- a
                ``None`` renders the literal ``"None"`` into the journal
                record's Redis key.
            candidate: The accepted candidate.
            journal: The ``ProvenanceJournal`` class to append through.
            speaker: Attribution, passed through to the entry.
            topic_tags: Extra subject tags. The ``cand:`` identity tag is
                appended to these and is never optional.

        Returns:
            The journal ``entry_id`` when this call assembled or reconciled
            one, else ``None`` (claim lost, already decided, or the append
            failed -- in which case a terminal row records why).
        """
        turn_id = candidate.turn_id
        candidate_id = candidate.candidate_id

        token = self.acquire_claim(agent_id, turn_id, candidate_id)
        if token is None:
            # Lost the claim. The winner owns this candidate's terminal
            # state; writing anything here is how duplicates happen.
            logger.debug(
                "assembly: claim lost for %s/%s -- no-op", agent_id, candidate_id
            )
            return None

        try:
            existing = self.get(agent_id, turn_id, candidate_id)

            if existing is not None and existing.is_terminal:
                if existing.state == Verdict.ACCEPT.value and existing.entry_id:
                    return existing.entry_id
                return None

            if existing is not None and existing.state == Verdict.PENDING.value:
                reconciled = self._reconcile_pending(agent_id, candidate, journal)
                if reconciled is not _NOT_RECONCILED:
                    return cast(Optional[str], reconciled)
            else:
                self.write_pending(agent_id, candidate)

            return self._append_and_transition(
                agent_id, candidate, journal, speaker, topic_tags
            )
        finally:
            self.release_claim(agent_id, turn_id, candidate_id, token)

    def _reconcile_pending(
        self, agent_id: str, candidate: "Candidate", journal: Any
    ) -> Any:
        """Resolve a surviving ``pending`` row by candidate identity.

        Returns the entry id when the prior ``append()`` landed, ``None``
        when the row was closed out as ambiguous, or the
        :data:`_NOT_RECONCILED` sentinel when the prior append never landed
        and the caller should proceed to append.
        """
        entry_model = getattr(journal, "entry_model", None)
        if entry_model is None:
            return _NOT_RECONCILED

        matches = list(
            entry_model.query.filter(
                turn_id=candidate.turn_id,
                subjects__all=[f"cand:{candidate.candidate_id}"],
            )
        )

        if len(matches) == 1:
            entry_id = matches[0].entry_id
            self.write_terminal(
                agent_id,
                candidate,
                Verdict.ACCEPT,
                ReasonCode.ACCEPTED,
                entry_id=entry_id,
            )
            return entry_id

        if len(matches) > 1:
            # Unreachable with correct tagging and per-turn-unique candidate
            # ids (both guaranteed by the generator), so this is an
            # assertion in row form: a loud, queryable state instead of a
            # silent wrong-entry_id write. Take no entry, append nothing.
            entry_ids = sorted(str(match.entry_id) for match in matches)
            self.write_terminal(
                agent_id,
                candidate,
                Verdict.REJECT,
                ReasonCode.AMBIGUOUS_RECONCILIATION,
                detail_code=",".join(entry_ids),
            )
            logger.error(
                "assembly: %s journal entries carry cand:%s -- refusing to "
                "guess which one this row belongs to",
                len(matches),
                candidate.candidate_id,
            )
            return None

        return _NOT_RECONCILED

    def _append_and_transition(
        self,
        agent_id: str,
        candidate: "Candidate",
        journal: Any,
        speaker: Optional[str],
        topic_tags: Sequence[str],
    ) -> Optional[str]:
        """Append, then transition the same row to exactly one terminal state.

        ``statement`` and ``verbatim`` are the **same string object** as the
        candidate span: accepted content is byte-identical to the span, with
        no normalization, whitespace collapsing or casing change.
        Distillation is M4's job, and doing any of it here would violate
        #562's own acceptance criteria.
        """
        try:
            result = journal.append(
                agent_id=agent_id,
                kind="assert",
                verbatim=candidate.text,
                statement=candidate.text,
                speaker=speaker,
                turn_id=candidate.turn_id,
                subjects=[*topic_tags, f"cand:{candidate.candidate_id}"],
            )
        except JournalBlockedError:
            # The journal runs its own never-record scan at write time over
            # values M3's per-candidate scan never sees: agent_id, the
            # subject tags, and the entry's own scan values. So an
            # LLM-accepted candidate can still be refused at assembly. It
            # maps to firewall_drop rather than a fifth state because the
            # semantics are identical to the pre-LLM drop -- content refused
            # by the never-record firewall, nothing stored. Only the reason
            # code distinguishes them, which keeps firewall_drop meaning
            # exactly "privacy refusal" and never a bucket for write errors.
            self.write_terminal(
                agent_id,
                candidate,
                Verdict.FIREWALL_DROP,
                ReasonCode.POST_ACCEPT_JOURNAL_BLOCK,
            )
            return None
        except Exception as e:
            # Every *other* assembly failure is an infrastructure loss, not
            # a privacy one, and is charged to reject(assembly_failed) with
            # the exception class name in the free-form detail_code.
            self.write_terminal(
                agent_id,
                candidate,
                Verdict.REJECT,
                ReasonCode.ASSEMBLY_FAILED,
                detail_code=type(e).__name__,
            )
            logger.warning(
                "assembly: append failed for %s/%s: %s",
                agent_id,
                candidate.candidate_id,
                e,
            )
            return None

        entry_id = str(result.entry.entry_id)
        self.write_terminal(
            agent_id,
            candidate,
            Verdict.ACCEPT,
            ReasonCode.ACCEPTED,
            entry_id=entry_id,
        )
        return entry_id

    # -- reads -----------------------------------------------------------

    @staticmethod
    def _agent_key_pattern(agent_id: str) -> str:
        """Glob matching every row of one agent, agent_id in its own segment.

        Built from the model's own key metadata rather than by formatting a
        string, so the alphabetical position of ``agent_id`` and the value
        escaping are derived, not assumed. Mirrors
        ``KeyFieldMixin.filter_query``'s pattern construction.
        """
        meta = DecisionRecord._meta
        position = meta.get_db_key_index_position("agent_id")
        pattern = meta.db_class_key.redis_key + ":"
        pattern += "*:" * (position - 1)
        pattern += DB_key.clean(agent_id)
        pattern += ":*" * (meta.db_key_length - position - 1)
        return pattern

    def get(
        self, agent_id: str, turn_id: str, candidate_id: str
    ) -> Optional[DecisionRecord]:
        """Read one candidate's row, or ``None`` if it has none yet."""
        key = self.row_key(agent_id, turn_id, candidate_id)
        if not self._redis.exists(key):
            return None
        rows = Query.get_many_objects(DecisionRecord, {key})
        return rows[0] if rows else None

    def list_for_agent(self, agent_id: str) -> List[DecisionRecord]:
        """Every row for an agent, in no particular order.

        Reads by key pattern rather than through
        ``DecisionRecord.query.filter(agent_id=...)`` **on purpose**, and
        this is load-bearing rather than a style choice. Exact-match
        KeyField filtering resolves through secondary index Sets that
        ``KeyFieldMixin.on_save`` maintains
        (``fields/key_field_mixin.py:422+``), and the guarded terminal
        write is a deliberate ORM bypass -- it writes the row hash from
        Lua, so ``on_save`` never runs and those index Sets are never
        built. A candidate whose *first* write is terminal (every
        ``firewall_drop`` / ``reject`` / ``withhold``) would therefore be
        invisible to ``filter()`` while sitting in Redis in full. Scanning
        the keyspace has no such dependency.

        Uses ``SCAN`` rather than ``KEYS`` so an unbounded log cannot block
        the server. This is an operator/analysis reader, not a hot path.
        """
        keys = set(self._redis.scan_iter(match=self._agent_key_pattern(agent_id)))
        if not keys:
            return []
        return Query.get_many_objects(DecisionRecord, keys)

    def list_pending(
        self, agent_id: str, older_than: Optional[float] = None
    ) -> List[DecisionRecord]:
        """Stale ``pending`` rows for an agent, **oldest-first**.

        The operator recovery reader (see the module docstring). A thin
        reader over existing rows -- it adds no keyspace, and decision rows
        carry no TTL, so nothing here deletes audit evidence.

        Args:
            agent_id: Owning agent.
            older_than: If given, only rows whose ``written_at`` is
                strictly older than this Unix timestamp.

        Returns:
            Matching rows sorted ascending by ``written_at``.
        """
        rows = [
            row
            for row in self.list_for_agent(agent_id)
            if row.state == Verdict.PENDING.value
        ]
        if older_than is not None:
            rows = [
                row
                for row in rows
                if row.written_at is not None and row.written_at < older_than
            ]
        return sorted(rows, key=lambda row: row.written_at or 0.0)

    def turn_summary(self, agent_id: str, turn_id: str) -> Dict[str, int]:
        """The per-turn compact summary as ``{field: count}``.

        A convenience index over the detail rows, aggregating terminal
        states only. Correctness never depends on it -- if it ever
        disagrees with the detail rows, the detail rows are right.
        """
        # redis-py types every command Awaitable[T] | T for the sync
        # client too; the cast is the repo-wide shape for that.
        raw = cast(
            Dict[Any, Any], self._redis.hgetall(self.summary_key(agent_id, turn_id))
        )
        return {
            key.decode(ENCODING) if isinstance(key, bytes) else key: int(value)
            for key, value in raw.items()
        }

    # -- offline metrics -------------------------------------------------

    def compute_metrics(self, agent_id: str, gold_labels: Dict[str, bool]) -> "Metrics":
        """Precision/recall/F1 for one agent, **from the log alone**.

        Reads decision-log rows and a caller-supplied gold-label mapping and
        nothing else -- no journal read, no LLM, no other live Redis state.
        That isolation is the point: it is what makes "extraction quality is
        computable offline" true rather than aspirational, and a test
        re-runs this with the journal keyspace flushed to prove it.

        A row counts as a positive prediction when its state is ``accept``.
        Rows with no gold label are excluded from precision/recall (they are
        still counted in the breakdowns), so a partially-labelled corpus
        does not silently score as a pile of false positives.

        Args:
            agent_id: Owning agent.
            gold_labels: ``{candidate_id: should_accept}``.

        Returns:
            A :class:`Metrics`.
        """
        rows = [row for row in self.list_for_agent(agent_id) if row.is_terminal]

        true_positives = false_positives = false_negatives = 0
        per_reason_code: Dict[str, int] = {}
        per_generator_rule: Dict[str, Dict[str, int]] = {}

        for row in rows:
            per_reason_code[row.reason_code] = (
                per_reason_code.get(row.reason_code, 0) + 1
            )
            by_state = per_generator_rule.setdefault(row.generator_rule, {})
            by_state[row.state] = by_state.get(row.state, 0) + 1

            if row.candidate_id not in gold_labels:
                continue
            should_accept = gold_labels[row.candidate_id]
            accepted = row.state == Verdict.ACCEPT.value
            if accepted and should_accept:
                true_positives += 1
            elif accepted and not should_accept:
                false_positives += 1
            elif not accepted and should_accept:
                false_negatives += 1

        precision = _ratio(true_positives, true_positives + false_positives)
        recall = _ratio(true_positives, true_positives + false_negatives)
        f1 = _ratio(2 * precision * recall, precision + recall)

        return Metrics(
            precision=precision,
            recall=recall,
            f1=f1,
            true_positives=true_positives,
            false_positives=false_positives,
            false_negatives=false_negatives,
            per_reason_code=per_reason_code,
            per_generator_rule=per_generator_rule,
        )


def _ratio(numerator: float, denominator: float) -> float:
    """Safe division: an undefined ratio is 0.0, never a ZeroDivisionError."""
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class Metrics:
    """Offline extraction quality, computed from decision-log rows alone.

    Attributes:
        precision: ``tp / (tp + fp)``, 0.0 when undefined.
        recall: ``tp / (tp + fn)``, 0.0 when undefined.
        f1: Harmonic mean of the two, 0.0 when undefined.
        true_positives: Accepted and gold-labelled accept.
        false_positives: Accepted and gold-labelled reject.
        false_negatives: Not accepted but gold-labelled accept.
        per_reason_code: ``{reason_code: count}`` over terminal rows. This
            dimension has one specific consumer: separating a **privacy**
            drop (``pre_llm_candidate_block`` /
            ``post_accept_journal_block``) from a **model** reject
            (``not_a_fact`` / ``not_memorable``) from an **infrastructure**
            loss (``llm_unavailable`` / ``assembly_failed``) inside one
            metric run. A privacy drop is not an extraction-quality failure
            and must not be charged against precision; an infrastructure
            loss must not be charged against recall. Without the breakdown,
            a run whose recall fell cannot distinguish "the model got
            worse" from "the firewall got stricter" from "Redis was flaky".
        per_generator_rule: ``{generator_rule: {state: count}}``. Comparing
            candidate shapes becomes a query over rows grouped by rule,
            rather than another blind bake-off.
    """

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    per_reason_code: Dict[str, int]
    per_generator_rule: Dict[str, Dict[str, int]]


@dataclass(frozen=True)
class AuditableExtractionConfig:
    """Opt-in configuration for the auditable extraction path.

    Passing ``auditable_extraction=None`` (the default) is the only thing
    the existing path ever sees, and its behavior is byte-for-byte
    unchanged.

    Deliberately carries no numeric knobs:
    ``Defaults.M3_ASSEMBLY_CLAIM_TTL_MS`` is a pinned in-repo constant, not
    a config field, per the repo's magic-number rule.

    Attributes:
        verdict_provider: Anything callable as
            ``provider(candidate) -> VerdictResult``, or an object exposing
            ``llm_verdict(candidate)``. Defaults to
            :func:`popoto.extraction.verdict.llm_verdict`. This is the seam
            tests inject a stubbed provider through.
        journal: The :class:`~popoto.recipes.provenance_journal.\
ProvenanceJournal` class (or a subclass) accepted candidates are appended
            to.
    """

    verdict_provider: Any = None
    journal: Any = None


__all__ = [
    "DecisionRecord",
    "DecisionLog",
    "Metrics",
    "AuditableExtractionConfig",
    "TERMINAL_WRITE_LUA",
    "SUMMARY_KEY_PREFIX",
    "hash_candidate_text",
]
