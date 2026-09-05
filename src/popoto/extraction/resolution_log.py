"""
The write-side sidecar store for reference resolution (M4, #563).

Every :func:`~popoto.extraction.resolution.resolve_references` outcome
that gets appended to the provenance journal gets one row here too, keyed
by the same ``(agent_id, turn_id, candidate_id)`` composite identity the
M3 decision log uses. :class:`ResolutionRecord` is a **sidecar**, not the
source of truth: the ``res:*`` status flag itself travels on the journal
entry's own subject tag (``Resolution.subject_tag``, set on
``JournalEntry.subjects`` by the pipeline), independent of whether this
write ever lands. This module exists so the full ``Reference`` detail --
surface offsets, resolved text, assumptions, candidate lists, clarifying
questions -- has somewhere durable to live without bloating the journal
entry itself. M7 (#566) is its intended consumer.

**The sidecar is never load-bearing for the ``res:`` flag.** A failed
:meth:`ResolutionLog.write` must never change whether the candidate was
accepted, nor what subject tags landed on its journal entry -- both are
already committed by the time ``write`` runs. See :meth:`ResolutionLog.write`.

**Composite ``KeyField`` identity, never ``AutoKeyField``.** Exactly like
:class:`~popoto.extraction.decision_log.DecisionRecord`
(``decision_log.py:13-23``): ``agent_id``, ``turn_id`` and
``candidate_id`` are all ``KeyField``s, so the Redis key **is** the
candidate's identity and a second ``write()`` for the same tuple
transitions that row in place rather than minting a duplicate.
``AutoKeyField`` is forbidden on this model for the same reason it is
forbidden on ``DecisionRecord``: it would leave stray rows behind on
every re-write.

**``references_json`` is JSON, not msgpack, on purpose.** Every other
Popoto model field is msgpack-packed by the base ``Model`` encoding, and
this one field is deliberately re-encoded as a JSON string on top of
that so the reference detail stays readable by M7 and by a human running
``redis-cli HGET`` -- msgpack bytes would not be.

**No TTL.** Matching M3's decision log, rows here are unbounded and never
expire. Retention and sweep policy is M9 (#568)'s job, not v1's -- this
module declines to guess a horizon.
"""

import json
import logging
import time
from typing import Any, cast, Dict, List, Optional, TYPE_CHECKING

from ..fields.shortcuts import BooleanField, FloatField, KeyField, StringField
from ..models.base import Model

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .resolution import Reference, Resolution

logger = logging.getLogger("POPOTO.extraction")


class ResolutionRecord(Model):
    """One candidate's reference-resolution row.

    ``agent_id`` + ``turn_id`` + ``candidate_id`` form the composite key,
    exactly mirroring :class:`~popoto.extraction.decision_log.DecisionRecord`.
    Writing the same tuple twice transitions one row rather than creating
    two. See the module docstring for why ``AutoKeyField`` is forbidden
    here.

    Every field below is a plain (non-indexed) field, matching
    ``DecisionRecord``'s convention -- this row is read by identity
    (``ResolutionLog.get``), never scanned or filtered by field value.

    Attributes:
        agent_id: Owning agent. KeyField.
        turn_id: The turn the candidate was generated from. KeyField.
        candidate_id: ``{turn_id}:{generator_rule}:{ordinal}``. KeyField.
        status: The aggregate :class:`~popoto.extraction.resolution.
            ResolutionStatus` value (``resolved | assumed | evidence_gap |
            indeterminate``).
        statement: The rewritten statement written to the journal.
        verbatim: The original candidate text, unmodified.
        references_json: ``json.dumps`` of the flattened reference list --
            see :func:`_serialise_references`. JSON, not msgpack; see the
            module docstring.
        valid_from: The onset instant as an epoch float, or ``None``.
        entry_id: The journal entry id this resolution was written for.
        speaker: Who spoke the turn, from the resolution's ``TurnContext``.
        captured_at: Epoch seconds the turn was captured.
        timezone: IANA timezone name the resolution ran against.
        window_truncated: Whether the conversational window was truncated.
        degraded: Whether this was a fail-open fallback resolution.
        written_at: Unix timestamp this row was last written.
    """

    agent_id = KeyField()
    turn_id = KeyField()
    candidate_id = KeyField()
    status = StringField(default="")
    statement = StringField(default="")
    verbatim = StringField(default="")
    references_json = StringField(default="[]")
    valid_from = FloatField(null=True)
    entry_id = StringField(default="")
    speaker = StringField(default="")
    captured_at = FloatField(null=True)
    timezone = StringField(default="")
    window_truncated = BooleanField(default=False)
    degraded = BooleanField(default=False)
    written_at = FloatField(null=True)


def _serialise_reference(ref: "Reference") -> Dict[str, Any]:
    """Flatten one ``Reference`` to a JSON-safe dict.

    Enum members (``kind``, ``status``, ``temporal_role``) are rendered
    as their ``.value`` strings; ``candidates`` (a tuple) becomes a list.
    """
    return {
        "surface": ref.surface,
        "start": ref.start,
        "end": ref.end,
        "kind": ref.kind.value,
        "status": ref.status.value,
        "temporal_role": ref.temporal_role.value,
        "resolved_text": ref.resolved_text,
        "resolved_epoch": ref.resolved_epoch,
        "assumption": ref.assumption,
        "candidates": list(ref.candidates),
        "question": ref.question,
    }


def _serialise_references(references: Any) -> List[Dict[str, Any]]:
    """Flatten a sequence of ``Reference`` objects to JSON-safe dicts."""
    return [_serialise_reference(ref) for ref in references]


class ResolutionLog:
    """Writer/reader over :class:`ResolutionRecord` rows.

    Stateless -- every method takes the identity it operates on -- so one
    instance can serve any agent.

    Example::

        log = ResolutionLog()
        log.write(
            agent_id="agent-7",
            turn_id="t-41",
            candidate_id="t-41:sentence:0",
            resolution=resolution,
            entry_id=entry.entry_id,
        )
        row = log.get("agent-7", "t-41", "t-41:sentence:0")
    """

    def write(
        self,
        agent_id: str,
        turn_id: str,
        candidate_id: str,
        resolution: "Resolution",
        entry_id: str = "",
    ) -> bool:
        """Serialise ``resolution`` into a :class:`ResolutionRecord` row.

        Idempotent by composite key: a second call with the same
        ``(agent_id, turn_id, candidate_id)`` overwrites the row in place
        rather than creating a duplicate, because ``agent_id``/``turn_id``/
        ``candidate_id`` are the model's ``KeyField``s.

        This method **never raises**. Any exception -- a bad
        ``resolution`` shape, a Redis error, anything -- is caught, logged
        as a ``POPOTO.extraction`` warning, and turned into a ``False``
        return. The sidecar is not load-bearing: by the time this runs,
        the candidate's accept/reject outcome and its journal subject tags
        are already committed, and a sidecar write failure must never flip
        that outcome (plan Race 2).

        Args:
            agent_id: Owning agent.
            turn_id: The turn the candidate was generated from.
            candidate_id: The candidate's identity.
            resolution: The :class:`~popoto.extraction.resolution.
                Resolution` to persist.
            entry_id: The journal entry id, if the candidate was accepted.

        Returns:
            ``True`` on a successful write, ``False`` on any failure.
        """
        try:
            context = resolution.context
            record = ResolutionRecord(
                agent_id=agent_id,
                turn_id=turn_id,
                candidate_id=candidate_id,
                status=resolution.status.value,
                statement=resolution.statement,
                verbatim=resolution.verbatim,
                references_json=json.dumps(
                    _serialise_references(resolution.references)
                ),
                valid_from=resolution.valid_from,
                entry_id=entry_id or "",
                speaker=(context.speaker if context else None) or "",
                captured_at=context.captured_at if context else None,
                timezone=(context.timezone if context else None) or "",
                window_truncated=bool(resolution.window_truncated),
                degraded=bool(resolution.degraded),
                written_at=time.time(),
            )
            record.save()
            return True
        except Exception:
            logger.warning(
                "ResolutionLog.write failed for agent_id=%r turn_id=%r "
                "candidate_id=%r; sidecar row not written",
                agent_id,
                turn_id,
                candidate_id,
                exc_info=True,
            )
            return False

    def get(
        self, agent_id: str, turn_id: str, candidate_id: str
    ) -> Optional[ResolutionRecord]:
        """Return the row for this candidate, or ``None`` if absent.

        Args:
            agent_id: Owning agent.
            turn_id: The turn the candidate was generated from.
            candidate_id: The candidate's identity.

        Returns:
            The :class:`ResolutionRecord`, or ``None`` if no row exists.
        """
        return cast(
            Optional[ResolutionRecord],
            ResolutionRecord.query.get(
                agent_id=agent_id,
                turn_id=turn_id,
                candidate_id=candidate_id,
            ),
        )
