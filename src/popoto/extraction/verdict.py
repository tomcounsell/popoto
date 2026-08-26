"""
Enum-confined per-candidate verdict stage for auditable extraction (M3).

This module owns the *vocabulary* the rest of the auditable-extraction
pipeline decides in, and the one LLM call that produces a verdict for a
single :class:`~popoto.extraction.candidates.Candidate`.

Two invariants make this stage auditable, and both are enforced here
rather than trusted to the model:

1. **The model contributes enums only.** A reply is parsed into exactly
   ``{candidate_id, verdict, reason_code}``; ``verdict`` and
   ``reason_code`` must be members of the fixed vocabularies below, and
   ``candidate_id`` is taken from the *trusted* candidate, never from the
   reply. Any free text the model emits is discarded at the parse
   boundary, so no model-authored prose can reach the store. Accepted
   content is the verbatim candidate span, assembled by trusted code (see
   ``decision_log.py``).
2. **No candidate is ever silently dropped.** Every path through
   :func:`llm_verdict` returns a :class:`VerdictResult` -- a blocked
   candidate, a malformed reply, an unreachable API and a raising client
   all map to a logged verdict. The function never raises and never
   returns ``None``.

The per-candidate never-record firewall (M2) runs *before* the LLM call,
so text the firewall blocks is never transmitted to the provider.

Opt-in only: importing this module never imports ``anthropic``. The
optional dependency is resolved the same way
``popoto.extraction.claude`` resolves it, so callers and tests can always
monkeypatch ``anthropic_module`` regardless of whether the package is
installed.

Example:
    from popoto.extraction.candidates import generate_candidates
    from popoto.extraction.verdict import llm_verdict

    for candidate in generate_candidates("turn-001", response_text):
        result = llm_verdict(candidate)
        # result.verdict / result.reason_code are enum members, always.
"""

import enum
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, Optional

from ..privacy.never_record import scan_never_record

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .candidates import Candidate

try:
    import anthropic as anthropic_module

    _anthropic_available = True
except ImportError:
    # Keep the name bound (to None) even when the optional dependency is
    # absent, so callers/tests can always monkeypatch it -- same contract
    # as popoto.extraction.claude.
    anthropic_module = None
    _anthropic_available = False

logger = logging.getLogger("POPOTO.extraction")


class Verdict(str, enum.Enum):
    """The state vocabulary a candidate can be logged in.

    Four of these are **terminal**: every candidate ends in exactly one of
    ``FIREWALL_DROP``, ``ACCEPT``, ``REJECT`` or ``WITHHOLD`` (issue
    #562's acceptance criteria).

    ``PENDING`` is **not terminal** and is not a fifth terminal state. It
    is an intent marker written on the decision row *before* assembly
    calls the provenance journal, so a candidate can never reach an
    irreversible side effect with zero decision-log rows. A surviving
    ``PENDING`` row means a process died mid-assembly -- a visible,
    queryable, recoverable incident. Any terminal-state aggregation
    (per-turn summaries, offline precision/recall) must exclude it; use
    :data:`TERMINAL_VERDICTS` or :attr:`Verdict.is_terminal` rather than
    iterating the enum.

    The model's own vocabulary is narrower still -- see
    :data:`LLM_VERDICTS`. The LLM never emits ``PENDING`` or
    ``FIREWALL_DROP``; both are written exclusively by trusted code.
    """

    FIREWALL_DROP = "firewall_drop"
    ACCEPT = "accept"
    REJECT = "reject"
    WITHHOLD = "withhold"
    PENDING = "pending"

    @property
    def is_terminal(self) -> bool:
        """True for the four terminal states, False for ``PENDING``."""
        return self is not Verdict.PENDING


TERMINAL_VERDICTS: FrozenSet[Verdict] = frozenset(
    {
        Verdict.FIREWALL_DROP,
        Verdict.ACCEPT,
        Verdict.REJECT,
        Verdict.WITHHOLD,
    }
)
"""The four terminal states. ``Verdict.PENDING`` is deliberately absent."""

LLM_VERDICTS: FrozenSet[Verdict] = frozenset(
    {Verdict.ACCEPT, Verdict.REJECT, Verdict.WITHHOLD}
)
"""The only verdicts a model reply may carry.

``FIREWALL_DROP`` is a trusted-code decision (the never-record firewall,
before or after the call) and ``PENDING`` is an internal write-ordering
marker; a reply claiming either is malformed.
"""


class ReasonCode(str, enum.Enum):
    """The fixed reason vocabulary paired with a :class:`Verdict`.

    Only the members listed in :data:`LLM_REASON_CODES` may come from a
    model reply. The rest are written exclusively by trusted code:

    - ``PRE_LLM_CANDIDATE_BLOCK`` / ``POST_ACCEPT_JOURNAL_BLOCK`` --
      never-record firewall refusals, before and after the LLM call
      respectively. Both pair with ``FIREWALL_DROP``; the reason code is
      what distinguishes them, so ``FIREWALL_DROP`` keeps meaning exactly
      "privacy refusal" and never becomes a bucket for write errors.
    - ``ASSEMBLY_FAILED`` / ``AMBIGUOUS_RECONCILIATION`` -- assembly-time
      failures, both pairing with ``REJECT``.
    - ``LLM_UNAVAILABLE`` -- the verdict call failed, returned nothing, or
      returned something that is not a well-formed enum reply. Pairs with
      ``REJECT``. Offline analysis reads this as an *infrastructure* loss
      and must not charge it against the model's recall.
    - ``EMPTY_TURN`` -- there was nothing to decide (blank turn, or a
      candidate whose span is whitespace). Pairs with ``REJECT``.
    - ``TURN_LEVEL_BLOCK`` -- the turn-level (M2) never-record scan voided
      the *whole* turn before any candidate was generated. Pairs with
      ``FIREWALL_DROP``, but is distinct from
      ``PRE_LLM_CANDIDATE_BLOCK``: that code means a per-candidate span was
      blocked by the M3 scan after candidates existed and the LLM never saw
      *that* one; this code means M2's turn-level scan fired first and no
      candidates were ever generated for this turn at all.
    """

    # --- trusted code only -------------------------------------------
    PRE_LLM_CANDIDATE_BLOCK = "pre_llm_candidate_block"
    POST_ACCEPT_JOURNAL_BLOCK = "post_accept_journal_block"
    ASSEMBLY_FAILED = "assembly_failed"
    AMBIGUOUS_RECONCILIATION = "ambiguous_reconciliation"
    LLM_UNAVAILABLE = "llm_unavailable"
    EMPTY_TURN = "empty_turn"
    TURN_LEVEL_BLOCK = "turn_level_block"

    # --- the model may emit these ------------------------------------
    ACCEPTED = "accepted"
    NOT_A_FACT = "not_a_fact"
    NOT_MEMORABLE = "not_memorable"
    LOW_CONFIDENCE = "low_confidence"
    NEEDS_CONFIRMATION = "needs_confirmation"


LLM_REASON_CODES: Dict[Verdict, FrozenSet[ReasonCode]] = {
    Verdict.ACCEPT: frozenset({ReasonCode.ACCEPTED}),
    Verdict.REJECT: frozenset({ReasonCode.NOT_A_FACT, ReasonCode.NOT_MEMORABLE}),
    Verdict.WITHHOLD: frozenset(
        {ReasonCode.LOW_CONFIDENCE, ReasonCode.NEEDS_CONFIRMATION}
    ),
}
"""Reason codes the model may pair with each verdict it may emit.

A reply whose ``reason_code`` is absent from its verdict's set is
malformed -- including a reason code that is a valid enum member but
reserved for trusted code (e.g. ``assembly_failed``). This is what keeps
the model from labelling its own rejection as an infrastructure failure,
which would corrupt the offline precision/recall breakdown.
"""


@dataclass(frozen=True)
class VerdictResult:
    """One candidate's verdict: enums plus the trusted candidate id.

    Deliberately has no free-text field. ``candidate_id`` is copied from
    the :class:`~popoto.extraction.candidates.Candidate` that trusted code
    generated -- it is never read out of the model's reply -- so nothing
    on this object originated as model prose.

    Attributes:
        candidate_id: The deciding candidate's id, from the candidate.
        verdict: A :class:`Verdict` member.
        reason_code: A :class:`ReasonCode` member.
    """

    candidate_id: str
    verdict: Verdict
    reason_code: ReasonCode


# ---------------------------------------------------------------------------
# Pinned, non-user-configurable constants.
#
# Per this project's convention (popoto.fields.constants.Defaults' module
# docstring and CLAUDE.md's "numeric constants are magic numbers for
# experimental tuning, not dev/user config"), the model and prompt used
# for the verdict call are pinned in-repo, not exposed as kwargs.
# ---------------------------------------------------------------------------

VERDICT_MODEL = "claude-haiku-4-5-20251001"
"""Pinned model for the per-candidate verdict call. Not user-configurable.

Deliberately a smaller model than ``claude.py``'s ``EXTRACTION_MODEL``:
the verdict stage issues roughly one call per sentence-plus-entity
candidate rather than one per turn (plan Risk 1, "LLM verdict-call cost
per turn"), and the task is a constrained enum classification rather than
open-ended extraction.
"""

VERDICT_MAX_TOKENS = 256
"""Pinned max_tokens. The reply is three short enum-valued keys."""

VERDICT_PROMPT = """You are a memory-verdict engine for an AI agent. You are \
given ONE candidate span of text lifted verbatim from a conversation turn, and \
you decide whether it is worth remembering.

Reply with the candidate's id and exactly one verdict and one reason code:

- "accept" (reason "accepted") -- a discrete, independently-useful fact worth \
remembering later.
- "reject" with reason "not_a_fact" (it asserts nothing -- a greeting, a \
question, filler, conversational scaffolding) or "not_memorable" (it asserts \
something, but it is trivial or has no value once the conversation ends).
- "withhold" with reason "low_confidence" (the span is hedged, speculative, or \
too ambiguous to store as stated) or "needs_confirmation" (it looks \
consequential but depends on context outside this span).

You are not asked to rewrite, summarize, or explain. Emit only the id, the \
verdict, and the reason code -- any other text is discarded."""
"""Pinned system prompt for the verdict call. Not user-configurable."""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "candidate_id": {"type": "string"},
        "verdict": {
            "type": "string",
            "enum": sorted(v.value for v in LLM_VERDICTS),
        },
        "reason_code": {
            "type": "string",
            "enum": sorted(
                r.value for codes in LLM_REASON_CODES.values() for r in codes
            ),
        },
    },
    "required": ["candidate_id", "verdict", "reason_code"],
    "additionalProperties": False,
}
"""JSON schema confining the reply to enums. Not user-configurable.

The schema is a first line of defence, not the enforcement point:
:func:`_parse_reply` re-validates every field, because a provider that
ignores or partially honours the schema must still not be able to write
free text or an out-of-vocabulary code into the decision log.
"""


def _default_client() -> Any:
    """Build the default Anthropic client, or raise if unavailable."""
    if not _anthropic_available or anthropic_module is None:
        raise ImportError(
            "anthropic is required to use the LLM verdict stage. "
            "Install it with: pip install popoto[anthropic]"
        )
    return anthropic_module.Anthropic()


def _request_verdict(client: Any, candidate: "Candidate") -> Optional[str]:
    """Issue the one verdict call and return the raw reply text.

    Returns None if the response carried no text block. Raises whatever
    the client raises -- :func:`llm_verdict` owns the failure mapping.
    """
    response = client.messages.create(
        model=VERDICT_MODEL,
        max_tokens=VERDICT_MAX_TOKENS,
        system=VERDICT_PROMPT,
        messages=[
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "candidate_id": candidate.candidate_id,
                        "text": candidate.text,
                    }
                ),
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    )
    return next(
        (block.text for block in response.content if block.type == "text"),
        None,
    )


def _parse_reply(raw_text: Optional[str], candidate_id: str) -> Optional[VerdictResult]:
    """Parse an untrusted reply into a VerdictResult, or None if malformed.

    Every field is re-validated against the fixed vocabularies regardless
    of the request's JSON schema, and the reply's own ``candidate_id`` is
    checked against the trusted one but never *used* as the id -- a reply
    about a different candidate is treated as malformed rather than
    silently applied to this one.
    """
    if not raw_text or not raw_text.strip():
        return None

    try:
        parsed = json.loads(raw_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    if parsed.get("candidate_id") not in (None, candidate_id):
        return None

    try:
        verdict = Verdict(parsed.get("verdict"))
        reason_code = ReasonCode(parsed.get("reason_code"))
    except ValueError:
        return None

    if verdict not in LLM_VERDICTS:
        return None
    if reason_code not in LLM_REASON_CODES[verdict]:
        return None

    return VerdictResult(
        candidate_id=candidate_id,
        verdict=verdict,
        reason_code=reason_code,
    )


def llm_verdict(candidate: "Candidate", client: Any = None) -> VerdictResult:
    """Decide one candidate: firewall first, then a single LLM call.

    Never raises and never returns ``None`` -- every failure mode maps to
    a logged verdict, because a candidate that vanishes is precisely the
    defect this module exists to eliminate.

    Order of operations:

    1. A whitespace-only span is ``reject`` / ``empty_turn``; there is
       nothing to decide and no call is made.
    2. ``scan_never_record(candidate.text)`` runs **before** the call. A
       blocked span is ``firewall_drop`` / ``pre_llm_candidate_block`` and
       its text is never transmitted to the provider.
    3. Otherwise one call is issued. A malformed, empty or
       out-of-vocabulary reply, an unreachable provider, and a raising
       client all map to ``reject`` / ``llm_unavailable``.

    Args:
        candidate: The candidate to decide. Only ``candidate_id`` and
            ``text`` are read.
        client: An Anthropic-style client (anything exposing
            ``messages.create``). ``None`` builds the default client,
            which requires the optional ``anthropic`` package.

    Returns:
        A :class:`VerdictResult` carrying enum fields and the candidate's
        own id -- never any model-authored text.
    """
    candidate_id = candidate.candidate_id

    if not candidate.text or not candidate.text.strip():
        logger.debug("verdict: blank candidate %s -> reject(empty_turn)", candidate_id)
        return VerdictResult(candidate_id, Verdict.REJECT, ReasonCode.EMPTY_TURN)

    firewall = scan_never_record(candidate.text)
    if firewall.blocked:
        # Log the reason code and the candidate id only -- never a
        # fragment of the blocked span.
        logger.info(
            "verdict: candidate %s blocked by never-record firewall (%s)",
            candidate_id,
            firewall.reason,
        )
        return VerdictResult(
            candidate_id,
            Verdict.FIREWALL_DROP,
            ReasonCode.PRE_LLM_CANDIDATE_BLOCK,
        )

    try:
        if client is None:
            client = _default_client()
        raw_text = _request_verdict(client, candidate)
    except Exception as e:
        logger.warning("verdict: call failed for candidate %s: %s", candidate_id, e)
        return VerdictResult(candidate_id, Verdict.REJECT, ReasonCode.LLM_UNAVAILABLE)

    result = _parse_reply(raw_text, candidate_id)
    if result is None:
        logger.warning(
            "verdict: malformed reply for candidate %s -> reject(llm_unavailable)",
            candidate_id,
        )
        return VerdictResult(candidate_id, Verdict.REJECT, ReasonCode.LLM_UNAVAILABLE)
    return result


__all__ = [
    "Verdict",
    "ReasonCode",
    "VerdictResult",
    "TERMINAL_VERDICTS",
    "LLM_VERDICTS",
    "LLM_REASON_CODES",
    "llm_verdict",
]
