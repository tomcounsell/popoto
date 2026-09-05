"""
Reference resolution stage for auditable extraction (M4, #563).

This module turns a candidate span plus its conversational window into a
:class:`Resolution`: a rewritten ``statement`` with pronouns, relative
dates and definite references either resolved, explicitly assumed,
flagged as an evidence gap, or left indeterminate. Exactly four statuses
make up the model's vocabulary, in :class:`ResolutionStatus`:

- ``resolved`` -- the reference was anchored with no ambiguity.
- ``assumed`` -- the stage anchored it, but only by stating an assumption
  (e.g. picking the most recent antecedent).
- ``evidence_gap`` -- multiple plausible antecedents exist and the stage
  cannot pick one; the candidates are recorded and a clarifying question
  is posed.
- ``indeterminate`` -- nothing in the window resolves the reference.

**Fail-open, per M3's precedent.** :func:`resolve_references` never
raises and never returns ``None``. A missing ``anthropic`` client, a
raising client, a malformed reply, or the ``M4_RESOLUTION_ENABLED`` kill
switch all fall back to a ``degraded`` :class:`Resolution` whose
``statement`` is byte-identical to the candidate's own verbatim text --
the entry is still captured. This is "quality loss, not corruption": M4
never causes M3's fail-open contract to fail closed.

**The model never emits an epoch number.** Every date the model returns
is ISO-8601 text; this module is the only thing that ever calls
``datetime.fromisoformat`` on it, and floats reaching :class:`Resolution`
are always Python-computed (see :func:`_to_epoch`), never model-supplied.
"""

import dataclasses
import enum
import json
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .candidates import Candidate

try:
    import anthropic as anthropic_module

    _anthropic_available = True
except ImportError:
    # Keep the name bound (to None) even when the optional dependency is
    # absent, so callers/tests can always monkeypatch it -- same contract
    # as popoto.extraction.claude and popoto.extraction.verdict.
    anthropic_module = None
    _anthropic_available = False

logger = logging.getLogger("POPOTO.extraction")


class ResolutionStatus(str, enum.Enum):
    """The four-member status vocabulary a resolved reference may carry.

    Ordered worst-last for :func:`ResolutionStatus.worst_of`:
    ``resolved`` is the best outcome, ``indeterminate`` the worst.
    """

    RESOLVED = "resolved"
    ASSUMED = "assumed"
    EVIDENCE_GAP = "evidence_gap"
    INDETERMINATE = "indeterminate"

    @property
    def _rank(self) -> int:
        return _STATUS_RANK[self]

    @staticmethod
    def worst_of(statuses: "List[ResolutionStatus]") -> "ResolutionStatus":
        """Return the worst (most-degraded) status among ``statuses``.

        Defaults to ``RESOLVED`` (the best status) for an empty input --
        this mirrors "nothing needed resolving" being a legitimate,
        non-degraded outcome (see :func:`resolve_references`).
        """
        if not statuses:
            return ResolutionStatus.RESOLVED
        return max(statuses, key=lambda s: s._rank)


_STATUS_RANK: Dict[ResolutionStatus, int] = {
    ResolutionStatus.RESOLVED: 0,
    ResolutionStatus.ASSUMED: 1,
    ResolutionStatus.EVIDENCE_GAP: 2,
    ResolutionStatus.INDETERMINATE: 3,
}


class ReferenceKind(str, enum.Enum):
    """What kind of reference a span is."""

    PRONOUN = "pronoun"
    RELATIVE_TIME = "relative_time"
    DEFINITE_REFERENCE = "definite_reference"


class TemporalRole(str, enum.Enum):
    """The role a ``relative_time`` reference plays in the statement.

    Only ``onset`` participates in the ``valid_from`` emission rule (see
    :func:`_compute_valid_from`); ``deadline`` and ``mention`` reference
    a date without asserting the claim became true then, and ``none`` is
    for non-temporal references classified with this vocabulary by
    mistake-proofing rather than by relevance.
    """

    ONSET = "onset"
    DEADLINE = "deadline"
    MENTION = "mention"
    NONE = "none"


@dataclass(frozen=True)
class Reference:
    """One resolved (or attempted) reference inside a candidate span.

    Attributes:
        surface: The exact substring of the candidate text this reference
            covers -- ``candidate.text[start:end] == surface`` is
            mechanically enforced by :func:`_parse_reference`.
        start: Start offset into the candidate text.
        end: End offset into the candidate text.
        kind: What kind of reference this is.
        status: This reference's own resolution status.
        temporal_role: Only meaningful when ``kind == RELATIVE_TIME``.
        resolved_text: The resolved human-readable text, when resolved or
            assumed.
        resolved_epoch: The resolved instant as a Python-computed epoch
            float (via :func:`_to_epoch`), or ``None``. Never taken
            directly from the model -- see the module docstring.
        assumption: A one-line stated assumption, required when
            ``status == ASSUMED``.
        candidates: Plausible antecedents, required (2-4) when
            ``status == EVIDENCE_GAP``.
        question: A clarifying question, required when
            ``status == EVIDENCE_GAP``.
    """

    surface: str
    start: int
    end: int
    kind: ReferenceKind
    status: ResolutionStatus
    temporal_role: TemporalRole = TemporalRole.NONE
    resolved_text: Optional[str] = None
    resolved_epoch: Optional[float] = None
    assumption: Optional[str] = None
    candidates: Tuple[str, ...] = ()
    question: Optional[str] = None


@dataclass(frozen=True)
class WindowTurn:
    """One prior turn carried in a :class:`TurnContext` window.

    Attributes:
        turn_id: Identity of the turn.
        speaker: Who spoke it, or ``None`` if unknown.
        text: The turn's text.
    """

    turn_id: Optional[str]
    speaker: Optional[str]
    text: str


@dataclass(frozen=True)
class TurnContext:
    """The conversational context a candidate is resolved against.

    Attributes:
        speaker: Who spoke the current turn, or ``None`` if unknown.
        captured_at: Epoch seconds the current turn was captured. A
            ``None``, NaN or infinite value is coerced to ``time.time()``
            in ``__post_init__`` (with a logged warning) -- this is a
            guard M4 adds, not M1 behaviour; see the module docstring's
            fail-open discussion and Technical Approach §3a of the plan.
        timezone: IANA timezone name naive resolved dates are anchored
            to.
        window: Prior turns, oldest-first, that make up the resolution
            window. Use :meth:`bounded_window` rather than reading this
            directly when a bound is required.
    """

    speaker: Optional[str] = None
    captured_at: float = field(default_factory=time.time)
    timezone: str = "UTC"
    window: Tuple[WindowTurn, ...] = ()

    def __post_init__(self) -> None:
        captured_at = self.captured_at
        if captured_at is None or not math.isfinite(captured_at):
            logger.warning(
                "resolution: TurnContext.captured_at %r is missing or "
                "non-finite; coercing to the current clock",
                captured_at,
            )
            # frozen dataclass -- object.__setattr__ is the sanctioned
            # escape hatch for __post_init__ coercion.
            object.__setattr__(self, "captured_at", time.time())

    @classmethod
    def now(cls) -> "TurnContext":
        """Build a context with no speaker, no window, UTC, now."""
        return cls(speaker=None, captured_at=time.time(), timezone="UTC", window=())

    def bounded_window(self) -> Tuple[Tuple[WindowTurn, ...], bool]:
        """Return the window truncated to the M4 bounds, oldest-first.

        Truncation drops the *oldest* turns first when either the turn
        count or the total character count exceeds its bound (Technical
        Approach §9). Returns ``(turns, truncated)`` so callers can
        record whether truncation happened, distinguishing "the window
        did not contain the antecedent" from "the model missed it".
        """
        from ..fields.constants import Defaults

        max_turns = Defaults.M4_WINDOW_MAX_TURNS
        max_chars = Defaults.M4_WINDOW_MAX_CHARS

        turns = list(self.window)
        original_len = len(turns)

        if len(turns) > max_turns:
            turns = turns[-max_turns:]

        total_chars = sum(len(t.text) for t in turns)
        while turns and total_chars > max_chars:
            dropped = turns.pop(0)
            total_chars -= len(dropped.text)

        truncated = len(turns) < original_len
        return tuple(turns), truncated


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving one candidate against its context.

    Attributes:
        statement: The rewritten statement. Byte-identical to
            ``verbatim`` when the stage is degraded, or when the model
            emitted an empty references array (nothing needed
            resolving).
        verbatim: The original candidate text, unmodified.
        references: The resolved (or dropped-then-retried) references,
            in offset order.
        status: The aggregate :class:`ResolutionStatus` -- the worst
            status among ``references``, or ``RESOLVED`` when
            ``references`` is empty.
        valid_from: The onset instant, as a Python-computed epoch float,
            or ``None``. See the module-level onset rule documented on
            :func:`_compute_valid_from`.
        degraded: ``True`` when this Resolution is a fail-open fallback
            (missing dependency, raising client, malformed reply, kill
            switch, or a non-finite float caught before construction).
            Distinct from ``status == INDETERMINATE``, which can also be
            a genuine model abstention -- see :attr:`subject_tag`.
        context: The :class:`TurnContext` this resolution ran against,
            or ``None``.
        window_truncated: Whether :meth:`TurnContext.bounded_window`
            reported truncation for this run.
    """

    statement: str
    verbatim: str
    references: Tuple[Reference, ...] = ()
    status: ResolutionStatus = ResolutionStatus.RESOLVED
    valid_from: Optional[float] = None
    degraded: bool = False
    context: Optional[TurnContext] = None
    window_truncated: bool = False

    @property
    def subject_tag(self) -> str:
        """The ``res:*`` journal subject tag for this resolution.

        ``degraded`` takes precedence: a degraded resolution is tagged
        ``res:degraded`` and nothing else, so "the model abstained" and
        "the resolution stage never ran" remain distinguishable on the
        one channel guaranteed to travel with the fact (Technical
        Approach §3b).
        """
        if self.degraded:
            return "res:degraded"
        return f"res:{self.status.value}"


# ---------------------------------------------------------------------------
# Pinned, non-user-configurable constants.
#
# Per this project's convention (popoto.fields.constants.Defaults' module
# docstring and CLAUDE.md's "numeric constants are magic numbers for
# experimental tuning, not dev/user config"), the model and prompt used
# for the resolution call are pinned in-repo, not exposed as kwargs.
# ---------------------------------------------------------------------------

RESOLUTION_MODEL = "claude-haiku-4-5-20251001"
"""Pinned model for the per-candidate resolution call. Not user-configurable.

Same tier as ``verdict.py``'s ``VERDICT_MODEL`` -- this call runs once per
*accepted* candidate, not once per turn.
"""

RESOLUTION_MAX_TOKENS = 1024
"""Pinned max_tokens. A rewritten statement plus a handful of references."""

RESOLUTION_PROMPT = """You are a reference-resolution engine for an AI agent's \
memory. You are given ONE candidate statement lifted verbatim from a \
conversation turn, plus recent conversational context, and you resolve its \
pronouns, relative dates and definite references.

For each reference you find in the candidate text, report:
- "surface": the EXACT substring of the candidate text it covers (character \
for character -- do not paraphrase it).
- "start"/"end": its character offsets into the candidate text.
- "kind": "pronoun", "relative_time", or "definite_reference".
- "status": "resolved" (unambiguous), "assumed" (you picked one candidate and \
must state the assumption), "evidence_gap" (multiple plausible antecedents, \
list 2-4 of them and ask a clarifying question), or "indeterminate" (nothing \
in the context resolves it).
- For a resolved or assumed reference: "resolved_text" (the resolved form) \
and, for "relative_time", "resolved_iso" (an ISO-8601 date/datetime string -- \
NEVER an epoch number).
- For "relative_time" references only, also report "temporal_role": "onset" \
(the claim becomes true at this date), "deadline" (something is due by this \
date, but the claim is already true now), "mention" (the date is mentioned \
but is neither), or "none".
- For "assumed": a one-line "assumption" explaining your choice.
- For "evidence_gap": "candidates" (2-4 plausible antecedents) and a \
"question" that would resolve the ambiguity.

Then report "statement": the candidate rewritten with resolved references \
substituted in place, changing nothing else. If nothing needed resolving, \
"references" is an empty array and "statement" equals the candidate text \
verbatim.

Reply with the candidate's id, the statement, and the references array only \
-- any other text is discarded."""
"""Pinned system prompt for the resolution call. Not user-configurable."""

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "string"},
        "statement": {"type": "string"},
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                    "kind": {
                        "type": "string",
                        "enum": [k.value for k in ReferenceKind],
                    },
                    "status": {
                        "type": "string",
                        "enum": [s.value for s in ResolutionStatus],
                    },
                    "temporal_role": {
                        "type": "string",
                        "enum": [r.value for r in TemporalRole],
                    },
                    "resolved_text": {"type": ["string", "null"]},
                    "resolved_iso": {"type": ["string", "null"]},
                    "assumption": {"type": ["string", "null"]},
                    "candidates": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "question": {"type": ["string", "null"]},
                },
                "required": [
                    "surface",
                    "start",
                    "end",
                    "kind",
                    "status",
                    "temporal_role",
                    "resolved_text",
                    "resolved_iso",
                    "assumption",
                    "candidates",
                    "question",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["candidate_id", "statement", "references"],
    "additionalProperties": False,
}
"""JSON schema confining the reply. Not user-configurable.

One level of nesting only -- an object holding a flat array of flat
objects -- because Anthropic's structured-output schema support is not
full JSON Schema. This is a first line of defence, not the enforcement
point: :func:`_parse_reply` re-validates every field.
"""


def _default_client() -> Any:
    """Build the default Anthropic client, or raise if unavailable."""
    if not _anthropic_available or anthropic_module is None:
        raise ImportError(
            "anthropic is required to use the reference resolution stage. "
            "Install it with: pip install popoto[anthropic]"
        )
    return anthropic_module.Anthropic()


def _request_resolution(
    client: Any,
    candidate: "Candidate",
    turn_text: str,
    context: TurnContext,
) -> Optional[str]:
    """Issue the one resolution call and return the raw reply text.

    Returns None if the response carried no text block. Raises whatever
    the client raises -- :func:`resolve_references` owns the failure
    mapping.
    """
    window, _truncated = context.bounded_window()
    window_payload = [
        {"turn_id": w.turn_id, "speaker": w.speaker, "text": w.text} for w in window
    ]
    response = client.messages.create(
        model=RESOLUTION_MODEL,
        max_tokens=RESOLUTION_MAX_TOKENS,
        system=RESOLUTION_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "candidate_id": candidate.candidate_id,
                        "text": candidate.text,
                        "turn_text": turn_text,
                        "speaker": context.speaker,
                        "timezone": context.timezone,
                        "window": window_payload,
                    }
                ),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": RESOLUTION_SCHEMA}},
    )
    return next(
        (block.text for block in response.content if block.type == "text"),
        None,
    )


def _to_epoch(iso: str, tz: str) -> Optional[float]:
    """Convert an ISO-8601 string to an epoch float, or None if invalid.

    Uses ``datetime.fromisoformat`` + ``zoneinfo.ZoneInfo`` exclusively --
    never a numeric epoch from the model, and no third-party date-parsing
    library. A naive value takes the context timezone; an unknown
    timezone falls back to UTC (the caller marks the result degraded).
    """
    try:
        dt = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return None

    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("resolution: unknown timezone %r; falling back to UTC", tz)
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))

    epoch = dt.timestamp()
    if not math.isfinite(epoch):
        return None
    return epoch


def _parse_reference(
    raw: Dict[str, Any], candidate_text: str, tz: str
) -> Optional[Reference]:
    """Parse and validate one reference dict, or None if malformed.

    A structurally invalid reference (bad enum, bad offsets, a surface
    that is not exactly ``candidate_text[start:end]``, a required field
    missing for its status) is dropped -- returns ``None`` -- rather than
    raising, so one bad reference cannot sink the whole reply. ``tz`` is
    the context timezone, used to anchor a naive ``resolved_iso``.
    """
    from ..fields.constants import Defaults

    if not isinstance(raw, dict):
        return None

    surface = raw.get("surface")
    start = raw.get("start")
    end = raw.get("end")
    if (
        not isinstance(surface, str)
        or not isinstance(start, int)
        or not isinstance(end, int)
    ):
        return None
    if start < 0 or end > len(candidate_text) or start >= end:
        return None
    if candidate_text[start:end] != surface:
        return None

    try:
        kind = ReferenceKind(raw.get("kind"))
        status = ResolutionStatus(raw.get("status"))
        temporal_role = TemporalRole(raw.get("temporal_role", TemporalRole.NONE))
    except ValueError:
        return None

    resolved_text = raw.get("resolved_text")
    resolved_iso = raw.get("resolved_iso")
    assumption = raw.get("assumption")
    candidates_raw = raw.get("candidates") or []
    question = raw.get("question")
    resolved_epoch: Optional[float] = None

    if status in (ResolutionStatus.RESOLVED, ResolutionStatus.ASSUMED):
        if not isinstance(resolved_text, str) or not resolved_text.strip():
            return None
        if kind == ReferenceKind.RELATIVE_TIME:
            if not isinstance(resolved_iso, str) or not resolved_iso.strip():
                return None
            resolved_epoch = _to_epoch(resolved_iso, tz)
            if resolved_epoch is None:
                return None
        if status == ResolutionStatus.ASSUMED:
            if (
                not isinstance(assumption, str)
                or not assumption.strip()
                or "\n" in assumption
                or len(assumption) > Defaults.M4_ASSUMPTION_MAX_CHARS
            ):
                return None
    elif status == ResolutionStatus.EVIDENCE_GAP:
        if not isinstance(candidates_raw, list) or not (
            Defaults.M4_EVIDENCE_GAP_MIN_CANDIDATES
            <= len(candidates_raw)
            <= Defaults.M4_EVIDENCE_GAP_MAX_CANDIDATES
        ):
            return None
        if not all(isinstance(c, str) for c in candidates_raw):
            return None
        if (
            not isinstance(question, str)
            or not question.strip()
            or len(question) > Defaults.M4_QUESTION_MAX_CHARS
        ):
            return None
    elif status == ResolutionStatus.INDETERMINATE:
        if resolved_text or resolved_iso or assumption or candidates_raw or question:
            return None
    else:  # pragma: no cover - enum already validated above
        return None

    return Reference(
        surface=surface,
        start=start,
        end=end,
        kind=kind,
        status=status,
        temporal_role=temporal_role,
        resolved_text=resolved_text if isinstance(resolved_text, str) else None,
        resolved_epoch=resolved_epoch,
        assumption=assumption if isinstance(assumption, str) else None,
        candidates=tuple(candidates_raw),
        question=question if isinstance(question, str) else None,
    )


def _compute_valid_from(
    references: Tuple[Reference, ...],
) -> Tuple[Optional[float], bool, Optional[str]]:
    """Apply the onset rule to a validated reference tuple.

    Emits ``valid_from`` **only** when exactly one reference has
    ``kind == RELATIVE_TIME``, ``status in {RESOLVED, ASSUMED}``, and a
    ``temporal_role`` whose value is a member of the pinned constant
    ``Defaults.M4_VALID_FROM_ROLES`` (read fresh here, never a literal, so
    a maintainer reversal is a one-constant edit -- Decision 4).

    Zero matching onsets -> ``(None, False, None)`` (M1's default, the
    capture instant). Exactly one -> its resolved epoch. Two or more ->
    ``(None, True, assumption)`` -- abstention, floors the aggregate
    status at ``ASSUMED`` with a stated assumption naming the competing
    onsets (Decision 5).
    """
    from ..fields.constants import Defaults

    onset_roles = set(Defaults.M4_VALID_FROM_ROLES)
    onsets = [
        r
        for r in references
        if r.kind == ReferenceKind.RELATIVE_TIME
        and r.status in (ResolutionStatus.RESOLVED, ResolutionStatus.ASSUMED)
        and r.temporal_role.value in onset_roles
    ]

    if not onsets:
        return None, False, None
    if len(onsets) == 1:
        return onsets[0].resolved_epoch, False, None

    names = ", ".join(r.surface for r in onsets)
    assumption = (
        f"Multiple onset references ({names}) compete for valid_from; "
        "abstaining rather than guessing which one the statement is about."
    )
    return None, True, assumption


def _parse_reply(
    raw_text: Optional[str], candidate: "Candidate", tz: str = "UTC"
) -> Optional[Resolution]:
    """Parse an untrusted reply into a Resolution, or None if malformed.

    A malformed *envelope* (bad JSON, wrong shape, wrong candidate id,
    unparseable statement) returns ``None`` -- the caller maps that to
    the degraded fallback. A malformed individual *reference* is instead
    dropped by :func:`_parse_reference`, and the aggregate status floors
    at ``INDETERMINATE`` for the drop, without discarding the whole
    reply.
    """
    from ..fields.constants import Defaults

    if not raw_text or not raw_text.strip():
        return None
    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    if parsed.get("candidate_id") != candidate.candidate_id:
        return None

    statement = parsed.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        return None
    max_statement_len = (
        Defaults.M4_STATEMENT_MAX_GROWTH_FACTOR * len(candidate.text)
        + Defaults.M4_STATEMENT_MAX_GROWTH_CHARS
    )
    if len(statement) > max_statement_len:
        return None

    raw_references = parsed.get("references")
    if not isinstance(raw_references, list):
        return None
    if len(raw_references) > Defaults.M4_MAX_REFERENCES_PER_CANDIDATE:
        return None

    references: List[Reference] = []
    any_dropped = False
    for raw_ref in raw_references:
        ref = _parse_reference(raw_ref, candidate.text, tz)
        if ref is None:
            any_dropped = True
            continue
        references.append(ref)

    statuses = [r.status for r in references]
    if any_dropped:
        statuses.append(ResolutionStatus.INDETERMINATE)
    status = ResolutionStatus.worst_of(statuses)

    valid_from, onset_conflict, onset_assumption = _compute_valid_from(
        tuple(references)
    )
    if onset_conflict:
        status = ResolutionStatus.worst_of([status, ResolutionStatus.ASSUMED])
        if onset_assumption:
            references.append(
                Reference(
                    surface="",
                    start=0,
                    end=0,
                    kind=ReferenceKind.RELATIVE_TIME,
                    status=ResolutionStatus.ASSUMED,
                    temporal_role=TemporalRole.NONE,
                    assumption=onset_assumption,
                )
            )

    if valid_from is not None and not math.isfinite(valid_from):
        logger.warning(
            "resolution: non-finite valid_from for candidate %s; dropping",
            candidate.candidate_id,
        )
        valid_from = None

    return Resolution(
        # Plan's Risk 2 mitigation (docs/plans/reference_resolution_m4.md):
        # a per-reference validation violation degrades to verbatim rather
        # than storing the rewrite. `statement` is model-authored freeform
        # text, not assembled from the surviving `references`, so a single
        # dropped reference (`any_dropped`) gives no way to know whether the
        # model's substitution for the dropped reference still lurks in
        # `statement` -- e.g. "She"->"Alice" rejected for a bad offset, but
        # `statement` still reads "Alice ...". The only sound response is
        # for the whole reply to fall back to verbatim, not just the one
        # reference.
        statement=statement if references and not any_dropped else candidate.text,
        verbatim=candidate.text,
        references=tuple(references),
        status=status,
        valid_from=valid_from,
        degraded=False,
    )


def _degraded_resolution(
    candidate: "Candidate", context: Optional[TurnContext]
) -> Resolution:
    """Build the fail-open fallback Resolution for ``candidate``."""
    return Resolution(
        statement=candidate.text,
        verbatim=candidate.text,
        references=(),
        status=ResolutionStatus.INDETERMINATE,
        valid_from=None,
        degraded=True,
        context=context,
        window_truncated=False,
    )


def resolve_references(
    candidate: "Candidate",
    turn_text: str,
    context: TurnContext,
    client: Any = None,
) -> Resolution:
    """Resolve one candidate's pronouns, dates and definite references.

    Never raises and never returns ``None``. Every failure mode --
    ``Defaults.M4_RESOLUTION_ENABLED`` is False, the optional
    ``anthropic`` package is unavailable, the client raises, or the reply
    is malformed -- maps to the degraded fallback:
    ``Resolution(statement=verbatim, status=INDETERMINATE, references=(),
    valid_from=None, degraded=True)``, matching M3's fail-open contract
    (quality loss, not corruption).

    An empty/whitespace candidate span is also degraded, checked here as
    defence in depth even though M3 rejects such candidates before they
    can reach this stage -- no client call is made in that case either.

    A reply whose ``references`` array is empty is a *legitimate,
    non-degraded* outcome ("nothing needed resolving"): ``status =
    RESOLVED``, ``statement == verbatim``, no ``valid_from``.

    Args:
        candidate: The candidate to resolve. Only ``candidate_id`` and
            ``text`` are read.
        turn_text: The full turn text the candidate was lifted from.
        context: The conversational context to resolve against.
        client: An Anthropic-style client (anything exposing
            ``messages.create``). ``None`` builds the default client,
            which requires the optional ``anthropic`` package.

    Returns:
        A :class:`Resolution`, always -- degraded or not.
    """
    from ..fields.constants import Defaults

    candidate_id = candidate.candidate_id

    if not Defaults.M4_RESOLUTION_ENABLED:
        logger.debug(
            "resolution: M4_RESOLUTION_ENABLED is False; degrading %s",
            candidate_id,
        )
        return _degraded_resolution(candidate, context)

    if not candidate.text or not candidate.text.strip():
        logger.debug("resolution: blank candidate %s; degrading", candidate_id)
        return _degraded_resolution(candidate, context)

    window, truncated = context.bounded_window()
    bounded_context = TurnContext(
        speaker=context.speaker,
        captured_at=context.captured_at,
        timezone=context.timezone,
        window=window,
    )

    try:
        if client is None:
            client = _default_client()
        raw_text = _request_resolution(client, candidate, turn_text, bounded_context)
    except Exception as e:
        logger.warning("resolution: call failed for candidate %s: %s", candidate_id, e)
        return dataclasses.replace(
            _degraded_resolution(candidate, context), window_truncated=truncated
        )

    result = _parse_reply(raw_text, candidate, bounded_context.timezone)
    if result is None:
        logger.warning(
            "resolution: malformed reply for candidate %s; degrading",
            candidate_id,
        )
        return dataclasses.replace(
            _degraded_resolution(candidate, context), window_truncated=truncated
        )

    return dataclasses.replace(result, context=context, window_truncated=truncated)


__all__ = [
    "ResolutionStatus",
    "ReferenceKind",
    "TemporalRole",
    "Reference",
    "WindowTurn",
    "TurnContext",
    "Resolution",
    "resolve_references",
]
