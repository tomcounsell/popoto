"""M7 question queue: a rationed clarifying-question channel (#566).

The memory layer can detect that it is uncertain -- an M5 disjunct pair, a
confidence-gate refusal, an M4 evidence gap -- and until now could do nothing
about it except refuse. This module gives that uncertainty one shared, strictly
rationed outlet. Every subsystem that wants to ask the person something writes
a :class:`QuestionCandidate` with :func:`propose` instead of asking directly,
and the host application asks the queue, once per turn, whether there is
anything worth asking with :func:`next_question`.

**The contract ends at "the next question to ask, if any."** Nothing here
writes to a transport or addresses a person. How a rider question attaches to
an assistant reply is host-application territory.

Five rules shape every function below:

1. **Turns are host-supplied.** Every public call takes a monotonic
   ``turn: int``. The library owns no turn counter: the host is the only party
   that actually knows what a turn is, and a library-side counter would be
   wrong under concurrency.
2. **The value-of-information gate is a deterministic boolean** computed from
   stored metadata (:func:`passes_voi_gate`): the candidate carries a known
   ``ambiguity_signal`` *and* was recently used. There is no probabilistic VOI
   score, on purpose.
3. **The budget is structural.** At most one delivery per
   ``QUESTION_BUDGET_TURNS`` turns per agent, enforced by a Lua
   script (:data:`_DELIVER_LUA`) that grants the budget *and* claims a
   candidate in one step, so budget is spent only when a question is actually
   delivered -- never an in-process counter, never reset on restart, never
   rewound by a regressed turn.
4. **Answers are evidence, never overwrites.** An ``answered`` reply is mapped
   onto the existing closed outcome vocabulary (``acted`` / ``contradicted``)
   and applied through ``ObservationProtocol.on_context_used`` plus
   ``QUESTION_ANSWER_WEIGHT - 1`` extra capped-Bayesian observations. It never
   sets ``_superseded_by``, so it never closes a validity interval. A later
   contradiction still moves confidence.
5. **Fail closed.** ``propose*``, :func:`next_question` and
   :func:`record_answer` log and return ``None`` / a non-applied result on any
   Redis error; they never raise into the caller. Invalid *input* (an unknown
   kind, an option whose ``acted`` and ``contradicted`` lists overlap) is a
   producer bug and still raises ``ValueError``.

The free-text answer is **never stored or logged** -- only the matched option
index (``answer_option``) -- because a reply can echo sensitive content.

Extension seam for producers
----------------------------

A producer whose source of ambiguity can disappear before the question is
asked (an M5 disjoin annotation that gets retracted) registers a staleness
check with :func:`register_staleness_check`. :func:`expire_stale` -- which
:func:`next_question` runs before every delivery -- expires any open candidate
of that kind for which the check returns ``True``, so a stale question is
never asked. The M5 disjunction producer registers exactly such a check at
import time (:func:`_disjunction_is_stale`).

Producers
---------

Three thin adapters turn an existing ambiguity signal into a :func:`propose`
call. Each is a separate function the host calls when, and only when, it wants
that source -- so each is independently disableable by not calling it, and the
queue works with any subset (including none):

* :func:`propose_from_disjunctions` -- M5 ``disjoin`` annotations (#564).
  Imports ``reconciliation`` lazily, so the queue works without M5.
* :func:`propose_from_gate` -- a confidence-gate refusal in ``assemble()``
  metadata. **Dormant unless the host configures
  ``ContextAssembler(confidence_gate_threshold=...)``**: without a threshold
  there is no ``metadata["gate"]`` and the producer has no input.
* :func:`propose_from_resolution` -- M4 ``evidence_gap`` references on a
  ``ResolutionRecord`` (#563).

All three fail closed: on any error they log and return ``0`` / ``None`` and
never raise into the caller.

Example::

    from popoto.recipes import question_queue as qq

    qq.propose(
        agent_id="a1",
        question_text="Does Dana prefer morning or afternoon meetings?",
        kind="disjunction",
        source_module="reconciliation",
        target_keys=[key_a, key_b],
        options=[
            {"label": "morning", "acted": [key_a], "contradicted": [key_b]},
            {"label": "afternoon", "acted": [key_b], "contradicted": [key_a]},
        ],
        ambiguity_signal="disjunction",
        turn=12,
    )

    q = qq.next_question("a1", turn=13, query_cues="morning meetings with Dana")
    if q is not None:
        ...  # host phrases and delivers q.question_text
        qq.record_answer(q, "morning", turn=14)
"""

import contextvars
import importlib
import json
import logging
import re
import string
import time
import uuid
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

import msgpack

from ..fields import observation as _observation
from ..fields.constants import Defaults, question_queue_enabled
from ..fields.shortcuts import (
    IndexedField,
    IntField,
    KeyField,
    ListField,
    StringField,
)
from ..models.base import Model
from ..redis_db import get_REDIS_DB, run_lua

logger = logging.getLogger("POPOTO.QuestionQueue")

# ---------------------------------------------------------------------------
# Tuning constants -- module-level aliases of Defaults, registered in
# tests/benchmarks/overrides.py MODULE_CONSTANTS. Functions read these bare
# names at call time, so the benchmark harness can patch them.
# ---------------------------------------------------------------------------

QUESTION_BUDGET_TURNS = Defaults.QUESTION_BUDGET_TURNS
QUESTION_EXPIRY_TURNS = Defaults.QUESTION_EXPIRY_TURNS
QUESTION_RECENT_USE_TURNS = Defaults.QUESTION_RECENT_USE_TURNS
QUESTION_ANSWER_WEIGHT = Defaults.QUESTION_ANSWER_WEIGHT
QUESTION_RETENTION_TURNS = Defaults.QUESTION_RETENTION_TURNS
QUESTION_COOLDOWN_TURNS = Defaults.QUESTION_COOLDOWN_TURNS
QUESTION_BUCKET_TTL_SECONDS = Defaults.QUESTION_BUCKET_TTL_SECONDS

# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------

#: The shape of the question. ``disjunction``: "A or B?" (two mutually
#: exclusive options). ``confirmation``: "is k true?" (yes/no). ``referent``:
#: "which one did you mean?" (one option per candidate referent).
KINDS: Tuple[str, ...] = ("disjunction", "confirmation", "referent")

#: What produced the ambiguity -- the VOI gate's ambiguity factor. These are
#: the signals a producer can actually observe: an M5 disjoin annotation (ties
#: and probe splits are indistinguishable in its payload), a confidence-gate
#: refusal, and an M4 ``evidence_gap`` reference. There is deliberately no
#: judge-abstention value: a plain M5 abstention writes nothing.
AMBIGUITY_SIGNALS: Tuple[str, ...] = ("disjunction", "gate_refusal", "evidence_gap")

#: Candidate lifecycle. ``pending`` -> ``delivered`` -> ``answered``; a
#: deflected/unrecognized reply -> ``cooled`` (re-askable after cooldown); an
#: undelivered candidate past ``expires_turn`` -> ``expired`` (retained).
STATUSES: Tuple[str, ...] = ("pending", "delivered", "answered", "cooled", "expired")

#: Statuses a duplicate proposal is folded into (and touched) rather than
#: re-created.
_OPEN_STATUSES = frozenset({"pending", "delivered", "cooled"})

#: Statuses ``next_question`` may deliver from (``cooled`` only once its
#: cooldown has elapsed).
_DELIVERABLE_STATUSES = frozenset({"pending", "cooled"})

#: Statuses ``record_answer``'s claim accepts.
_ANSWERABLE_STATUSES = ("delivered", "pending")

#: Answer classifications. Only ``answered`` ever writes evidence.
ANSWERED = "answered"
DEFLECTED = "deflected"
UNRECOGNIZED = "unrecognized"


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    The single normalization used for dedup, option matching and the
    deflection set, so the three can never disagree about what "the same
    text" means.
    """
    lowered = str(text).lower()
    stripped = lowered.translate(str.maketrans("", "", string.punctuation))
    return " ".join(stripped.split())


#: Replies that decline to answer. Checked *before* option matching, so a
#: deflection can never be scored as an answer -- the misclassification that
#: would corrupt a fact's confidence with a non-answer. Stored normalized.
DEFLECTION_PHRASES = frozenset(
    normalize_text(p)
    for p in (
        "skip",
        "pass",
        "next",
        "not sure",
        "unsure",
        "no idea",
        "dunno",
        "i don't know",
        "don't know",
        "idk",
        "rather not say",
        "i'd rather not say",
        "i'd rather not",
        "prefer not to say",
        "no comment",
        "never mind",
        "nevermind",
        "ask me later",
        "later",
        "not now",
    )
)

#: Words dropped from ``cue_tokens`` (tokens shorter than 3 characters are
#: dropped regardless).
_CUE_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "are",
        "was",
        "were",
        "you",
        "your",
        "with",
        "that",
        "this",
        "which",
        "what",
        "who",
        "whom",
        "when",
        "where",
        "why",
        "how",
        "does",
        "did",
        "from",
        "have",
        "has",
        "had",
        "not",
        "but",
        "its",
        "our",
        "their",
        "they",
        "them",
        "there",
        "here",
        "about",
        "into",
        "than",
        "then",
        "should",
        "would",
        "could",
        "can",
        "will",
        "yes",
        "any",
        "all",
        "one",
        "still",
        "true",
        "prefer",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def cue_tokens_for(*texts: str) -> List[str]:
    """Relevance-timing tokens: lowercased word tokens of length >= 3 from
    ``texts``, minus :data:`_CUE_STOPWORDS`, sorted and de-duplicated."""
    tokens = set()
    for text in texts:
        for tok in _TOKEN_RE.findall(str(text).lower()):
            if len(tok) >= 3 and tok not in _CUE_STOPWORDS:
                tokens.add(tok)
    return sorted(tokens)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class QuestionCandidate(Model):
    """One persisted clarifying question awaiting the gate, the budget and a
    relevant moment.

    A real Model rather than a ZSET because the question is persisted state
    with a payload and a status (``RecallProposal`` is payload-free and
    TTL-expiring, and cannot be retrofitted).

    ``status``, ``ask_count``, ``delivered_turn``, ``answer_option``,
    ``cooldown_until`` and ``resolved_turn`` are deliberately **unindexed**
    plain fields: delivery and :func:`record_answer` write them with Lua
    directly on the model hash, which an index would not see. Reads go through the indexed ``agent_id``.

    Fields:
        question_text: The question, phrased by the producer (or by the host
            later). Echoes content, so retention is bounded by ``prune()``.
        kind: One of :data:`KINDS`.
        source_module: Free-form producer name (e.g. ``"reconciliation"``).
        target_keys: Redis keys of the facts the answer would resolve.
        options: ``[{"label": str, "acted": [keys], "contradicted": [keys]}]``.
            Answering applies exactly the chosen option's two lists.
        ambiguity_signal: One of :data:`AMBIGUITY_SIGNALS`.
        status: One of :data:`STATUSES`.
        ask_count: Times delivered. Re-asking after cooldown is legal.
        created_turn / expires_turn: Proposal turn and ``created + N``.
        last_seen_turn: Most recent turn the ambiguity was re-observed or the
            targets were used -- the VOI gate's impact factor.
        cooldown_until: Not deliverable before this turn (after a
            deflected/unrecognized reply).
        answer_option: 0-based index of the chosen option. The free-text reply
            itself is never stored.
        resolved_turn: Turn the candidate left the open set (answered, cooled,
            expired), used as the retention reference by ``prune()``.
        cue_tokens: See :func:`cue_tokens_for`.
        delivered_turn: Turn of the most recent delivery, written by the
            delivery Lua. A ``delivered`` candidate that gets no reply within
            ``QUESTION_COOLDOWN_TURNS`` of it is treated like a non-answer and
            moved to ``cooled`` (see :func:`expire_stale`).
        disjunction_id: Set by the M5 producer so a retracted disjoin can
            expire its candidate via a registered staleness check.
        agent_id: Owning agent.
    """

    candidate_id = KeyField()
    agent_id = IndexedField(type=str)
    question_text = StringField(default="")
    kind = StringField(default="")
    source_module = StringField(default="")
    target_keys = ListField(default=[])
    options = ListField(default=[])
    ambiguity_signal = StringField(default="")
    status = StringField(default="pending")
    ask_count = IntField(default=0)
    created_turn = IntField(default=0)
    expires_turn = IntField(default=0)
    last_seen_turn = IntField(default=0)
    cooldown_until = IntField(null=True)
    answer_option = IntField(null=True)
    resolved_turn = IntField(null=True)
    cue_tokens = ListField(default=[])
    delivered_turn = IntField(null=True)
    disjunction_id = StringField(null=True)


@dataclass
class AnswerResult:
    """Outcome of :func:`record_answer`.

    Attributes:
        applied: True only when an ``answered`` reply was claimed *and* its
            evidence was written.
        reason: ``"applied"``, ``"cooled"`` (deflected/unrecognized, no
            evidence), ``"not_open"`` (the claim failed: already answered,
            expired, or -- for a reply arriving after the question was
            ignored for ``QUESTION_COOLDOWN_TURNS`` -- already moved to
            ``cooled``), ``"apply_failed"`` (claimed, evidence write
            failed -- the answer is lost, never double-counted),
            ``"disabled"`` (kill switch) or ``"error"`` (Redis error before
            the claim).
        classification: ``"answered"`` / ``"deflected"`` / ``"unrecognized"``.
        option_index: 0-based chosen option for an ``answered`` reply.
    """

    applied: bool
    reason: str
    classification: Optional[str] = None
    option_index: Optional[int] = None


# ---------------------------------------------------------------------------
# Lua
# ---------------------------------------------------------------------------

#: Budget grant **and** delivery claim in one atomic step.
#:
#: The token bucket grants iff there is no prior ask or
#: ``turn >= last_ask_turn + K``; a regressed turn can never satisfy that, so
#: ``last_ask_turn`` is never rewound. Only when the bucket would grant are the
#: candidates tried, in the caller's impact order: the first whose ``status``
#: is still deliverable and whose ``cooldown_until`` has passed is flipped to
#: ``delivered`` (``ask_count`` incremented server-side, ``delivered_turn``
#: written). ``last_ask_turn`` is written -- with a fresh wall-clock TTL
#: backstop -- **only** when that claim succeeds, so a candidate expired or
#: claimed by another worker between the read and this script never burns the
#: budget.
#:
#: KEYS[1] bucket key, KEYS[2..] candidate hashes in delivery preference.
#: ARGV[1] turn, ARGV[2] K, ARGV[3] TTL seconds, ARGV[4] encoded
#: ``"delivered"``, ARGV[5] encoded turn, ARGV[6] n allowed statuses, then n
#: encoded statuses. Returns ``{index, new_ask_count}`` (1-based index into the
#: candidates) on a delivery, else 0.
_DELIVER_LUA = """
local turn = tonumber(ARGV[1])
local last = redis.call('GET', KEYS[1])
if last and turn < tonumber(last) + tonumber(ARGV[2]) then
    return 0
end
local n = tonumber(ARGV[6])
for c = 2, #KEYS do
    local current = redis.call('HGET', KEYS[c], 'status')
    local allowed = false
    if current then
        for i = 7, 6 + n do
            if current == ARGV[i] then
                allowed = true
                break
            end
        end
    end
    if allowed then
        local craw = redis.call('HGET', KEYS[c], 'cooldown_until')
        if craw then
            local ok, cooldown = pcall(cmsgpack.unpack, craw)
            if ok and tonumber(cooldown) and turn < tonumber(cooldown) then
                allowed = false
            end
        end
    end
    if allowed then
        local count = 0
        local raw = redis.call('HGET', KEYS[c], 'ask_count')
        if raw then
            local ok, v = pcall(cmsgpack.unpack, raw)
            if ok and tonumber(v) then
                count = tonumber(v)
            end
        end
        count = count + 1
        redis.call('HSET', KEYS[c], 'status', ARGV[4],
            'ask_count', cmsgpack.pack(count), 'delivered_turn', ARGV[5])
        redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[3]))
        return {c - 1, count}
    end
end
return 0
"""

#: Compare-and-set on the model hash's msgpack-encoded ``status`` field,
#: optionally guarded by further fields.
#: KEYS[1] candidate hash. ARGV[1] = n allowed statuses, ARGV[2] = g guard
#: pairs, then n encoded statuses, then g field/encoded-expected-value pairs
#: (an absent field compares as msgpack ``nil``), then field/encoded-value
#: pairs to write on a match. Returns 1 on a claim, 0 otherwise (including a
#: missing hash).
_CLAIM_LUA = """
local current = redis.call('HGET', KEYS[1], 'status')
if not current then
    return 0
end
local n = tonumber(ARGV[1])
local g = tonumber(ARGV[2])
local matched = false
for i = 3, n + 2 do
    if current == ARGV[i] then
        matched = true
        break
    end
end
if not matched then
    return 0
end
local first_guard = n + 3
for i = first_guard, first_guard + 2 * g - 1, 2 do
    local stored = redis.call('HGET', KEYS[1], ARGV[i]) or '\\192'
    if stored ~= ARGV[i + 1] then
        return 0
    end
end
for i = first_guard + 2 * g, #ARGV, 2 do
    redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
return 1
"""


def _bucket_key(agent_id: str) -> str:
    return f"$QuestionBucket:{agent_id}"


def _queue_backend() -> Any:
    """``QuestionCandidate``'s backend when it is not Redis (#759 M4), else
    ``None``. The bucket, the propose lock and the candidate CAS then go
    through its ``_qq`` ``field_call`` adapters (``FOR UPDATE SKIP LOCKED``
    delivery) instead of the Lua below."""
    from ..backends.routing import non_redis_backend

    return non_redis_backend(QuestionCandidate)


#: Propose lock: a correctness bound, not a tuning knob, so it is pinned here
#: rather than in ``Defaults``. The TTL only has to outlive one dedup scan
#: plus one save (it exists so a crashed holder cannot wedge the agent's
#: proposals); the wait is short because ``propose`` is called inline from
#: a host turn and fails closed rather than stall it.
_PROPOSE_LOCK_TTL_MS = 5000
_PROPOSE_LOCK_ATTEMPTS = 50
_PROPOSE_LOCK_RETRY_SECONDS = 0.01

#: Release the propose lock only if this caller still owns it.
#: KEYS[1] lock key; ARGV[1] owner token.
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


def _propose_lock_key(agent_id: str) -> str:
    return f"$QuestionProposeLock:{agent_id}"


def _acquire_propose_lock(agent_id: str) -> Optional[str]:
    """``SET NX PX`` the agent's propose lock, retrying briefly. Returns the
    owner token, or ``None`` if it could not be taken (callers fail closed).
    Raises on Redis errors."""
    token = uuid.uuid4().hex
    key = _propose_lock_key(agent_id)
    backend = _queue_backend()
    if backend is not None:
        spec = QuestionCandidate._meta.spec
        for attempt in range(_PROPOSE_LOCK_ATTEMPTS):
            if backend.field_call(
                spec, "_qq", "lock", key, token, _PROPOSE_LOCK_TTL_MS
            ):
                return token
            if attempt + 1 < _PROPOSE_LOCK_ATTEMPTS:
                time.sleep(_PROPOSE_LOCK_RETRY_SECONDS)
        return None
    client = get_REDIS_DB()
    for attempt in range(_PROPOSE_LOCK_ATTEMPTS):
        if client.set(key, token, nx=True, px=_PROPOSE_LOCK_TTL_MS):
            return token
        if attempt + 1 < _PROPOSE_LOCK_ATTEMPTS:
            time.sleep(_PROPOSE_LOCK_RETRY_SECONDS)
    return None


def _release_propose_lock(agent_id: str, token: str) -> None:
    try:
        backend = _queue_backend()
        if backend is not None:
            backend.field_call(
                QuestionCandidate._meta.spec,
                "_qq",
                "release",
                _propose_lock_key(agent_id),
                token,
            )
            return
        run_lua(get_REDIS_DB(), _RELEASE_LUA, 1, _propose_lock_key(agent_id), token)
    except Exception:
        # The TTL bounds the damage; never let a release error mask the result.
        logger.exception("question_queue: propose lock release failed")


def _try_deliver(
    agent_id: str, turn: int, ordered: Sequence[QuestionCandidate]
) -> Optional[QuestionCandidate]:
    """Atomically take the agent's one ask per K turns *and* claim the first
    still-deliverable candidate of ``ordered`` (:data:`_DELIVER_LUA`).

    Returns the delivered candidate (its in-memory ``status``, ``ask_count``
    and ``delivered_turn`` updated), or ``None`` when the bucket refused or
    no candidate could be claimed -- in both cases the bucket is untouched.
    Raises on Redis errors (callers fail closed).
    """
    if not ordered:
        return None
    allowed = tuple(sorted(_DELIVERABLE_STATUSES))
    backend = _queue_backend()
    if backend is not None:
        got = backend.field_call(
            QuestionCandidate._meta.spec,
            "_qq",
            "deliver",
            str(agent_id),
            [cand.db_key.redis_key for cand in ordered],
            int(turn),
            int(QUESTION_BUDGET_TURNS),
            int(QUESTION_BUCKET_TTL_SECONDS),
            allowed,
        )
        result: Any = None if got is None else list(got)
    else:
        result = run_lua(
            get_REDIS_DB(),
            _DELIVER_LUA,
            1 + len(ordered),
            _bucket_key(agent_id),
            *[cand.db_key.redis_key for cand in ordered],
            int(turn),
            int(QUESTION_BUDGET_TURNS),
            int(QUESTION_BUCKET_TTL_SECONDS),
            msgpack.packb("delivered"),
            msgpack.packb(int(turn)),
            len(allowed),
            *[msgpack.packb(s) for s in allowed],
        )
    if not isinstance(result, (list, tuple)) or len(result) != 2:
        return None
    cand = ordered[int(result[0]) - 1]
    setattr(cand, "status", "delivered")
    setattr(cand, "ask_count", int(result[1]))
    setattr(cand, "delivered_turn", int(turn))
    return cand


def _claim(
    candidate: QuestionCandidate,
    allowed: Sequence[str],
    updates: Dict[str, Any],
    guard: Optional[Dict[str, Any]] = None,
) -> bool:
    """CAS the candidate's status from one of ``allowed``; on a match write
    ``updates`` (field -> python value, msgpack-encoded like the model does).

    ``guard`` (field -> expected python value) additionally requires each
    stored field to still hold exactly that value, so a transition decided
    on an earlier read cannot land on a candidate that has since gone
    through the same status again (ABA).
    """
    guard = guard or {}
    backend = _queue_backend()
    if backend is not None:
        from ..backends.types import RecordId

        claimed_on_backend = backend.field_call(
            QuestionCandidate._meta.spec,
            "_qq",
            "claim",
            RecordId.from_key(
                QuestionCandidate._meta.model_name, candidate.db_key.redis_key
            ),
            tuple(allowed),
            dict(updates),
            dict(guard),
        )
        if claimed_on_backend:
            for field_name, value in updates.items():
                setattr(candidate, field_name, value)
            return True
        return False
    args: List[Any] = [len(allowed), len(guard)]
    args.extend(msgpack.packb(s) for s in allowed)
    for field_name, value in guard.items():
        args.append(field_name)
        args.append(msgpack.packb(value))
    for field_name, value in updates.items():
        args.append(field_name)
        args.append(msgpack.packb(value))
    claimed = run_lua(
        get_REDIS_DB(),
        _CLAIM_LUA,
        1,
        candidate.db_key.redis_key,
        *args,
    )
    if int(claimed or 0):
        for field_name, value in updates.items():
            setattr(candidate, field_name, value)
        return True
    return False


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _validate_options(
    options: Optional[Sequence[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Normalize and validate the option list; raise ``ValueError`` on a
    producer bug."""
    cleaned: List[Dict[str, Any]] = []
    for i, option in enumerate(options or []):
        if not isinstance(option, dict):
            raise ValueError(f"option {i} must be a dict, got {type(option)}")
        label = option.get("label")
        if not isinstance(label, str) or not normalize_text(label):
            raise ValueError(f"option {i} needs a non-empty string 'label'")
        acted = [str(k) for k in option.get("acted", []) or []]
        contradicted = [str(k) for k in option.get("contradicted", []) or []]
        overlap = set(acted) & set(contradicted)
        if overlap:
            raise ValueError(
                f"option {i} ({label!r}) lists {sorted(overlap)} as both "
                "acted and contradicted"
            )
        cleaned.append({"label": label, "acted": acted, "contradicted": contradicted})
    return cleaned


def classify_answer(
    options: Sequence[Dict[str, Any]], answer_text: str
) -> Tuple[str, Optional[int]]:
    """Deterministic three-way answer recognition.

    After :func:`normalize_text`, in order:

    1. In :data:`DEFLECTION_PHRASES` -> ``("deflected", None)``.
    2. Equal to exactly one option's normalized label, or its 1-based index
       -> ``("answered", i)`` with ``i`` 0-based.
    3. Anything else (including a reply matching more than one option)
       -> ``("unrecognized", None)``.
    """
    reply = normalize_text(answer_text)
    if not reply or reply in DEFLECTION_PHRASES:
        return (DEFLECTED if reply else UNRECOGNIZED), None
    matches = {
        i
        for i, option in enumerate(options)
        if normalize_text(option.get("label", "")) == reply
    }
    if reply.isdigit():
        idx = int(reply) - 1
        if 0 <= idx < len(options):
            matches.add(idx)
    if len(matches) == 1:
        return ANSWERED, matches.pop()
    return UNRECOGNIZED, None


def passes_voi_gate(candidate: QuestionCandidate, turn: int) -> bool:
    """The value-of-information gate: a deterministic boolean.

    ``ambiguity_signal in AMBIGUITY_SIGNALS and recently_used``, where
    recently used means ``turn - last_seen_turn <= QUESTION_RECENT_USE_TURNS``.
    Both factors are stored on the candidate, so every delivered question is
    traceable to the evidence that let it through.
    """
    if _str_attr(candidate, "ambiguity_signal") not in AMBIGUITY_SIGNALS:
        return False
    last_seen = _int_attr(candidate, "last_seen_turn")
    if last_seen is None:
        return False
    return 0 <= int(turn) - last_seen <= QUESTION_RECENT_USE_TURNS


def _query_tokens(query_cues: Union[str, Iterable[str]]) -> set[str]:
    if isinstance(query_cues, str):
        return set(cue_tokens_for(query_cues))
    return set(cue_tokens_for(*[str(c) for c in query_cues]))


def _retention_reference(candidate: QuestionCandidate) -> int:
    for name in ("resolved_turn", "last_seen_turn", "created_turn"):
        value = _int_attr(candidate, name)
        if value is not None:
            return value
    return 0


def _int_attr(candidate: QuestionCandidate, name: str) -> Optional[int]:
    """Typed read of an IntField value (mypy sees the Field descriptor)."""
    value = getattr(candidate, name, None)
    return None if value is None else int(value)


def _str_attr(candidate: QuestionCandidate, name: str) -> str:
    value = getattr(candidate, name, None)
    return "" if value is None else str(value)


def _list_attr(candidate: QuestionCandidate, name: str) -> List[Any]:
    return list(getattr(candidate, name, None) or [])


def _candidates(agent_id: str) -> List[QuestionCandidate]:
    return list(QuestionCandidate.query.filter(agent_id=agent_id))


# ---------------------------------------------------------------------------
# Staleness registry (extension seam for producers)
# ---------------------------------------------------------------------------

#: ``kind -> [check(candidate, turn) -> bool]``. A ``True`` return means "the
#: ambiguity behind this candidate no longer exists": it is expired instead of
#: asked.
_STALENESS_CHECKS: Dict[str, List[Callable[[QuestionCandidate, int], bool]]] = {}


def register_staleness_check(
    kind: str, check: Callable[[QuestionCandidate, int], bool]
) -> None:
    """Register ``check`` for candidates of ``kind`` (idempotent).

    Used by producers whose source can vanish between proposal and delivery
    -- e.g. the M5 disjunction producer expiring a candidate whose
    ``disjunction_id`` no longer appears among the live disjoins.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    checks = _STALENESS_CHECKS.setdefault(kind, [])
    if check not in checks:
        checks.append(check)


def unregister_staleness_check(
    kind: str, check: Callable[[QuestionCandidate, int], bool]
) -> None:
    """Remove a previously registered check (no-op if absent)."""
    checks = _STALENESS_CHECKS.get(kind, [])
    if check in checks:
        checks.remove(check)


def _is_stale(candidate: QuestionCandidate, turn: int) -> Optional[bool]:
    """True/False from the registered checks; None if a check raised (the
    candidate is then neither expired nor delivered this turn)."""
    for check in _STALENESS_CHECKS.get(_str_attr(candidate, "kind"), []):
        try:
            if check(candidate, turn):
                return True
        except Exception:
            logger.exception(
                "question_queue: staleness check failed for %s",
                candidate.candidate_id,
            )
            return None
    return False


def _expire(
    candidate: QuestionCandidate, turn: int, from_status: Optional[str] = None
) -> bool:
    """CAS the candidate to ``expired`` from the status the caller read.

    ``from_status`` defaults to the candidate's local ``status``. The CAS is
    deliberately *not* from any open status: an expiry decided on a
    ``pending``/``cooled`` read (past ``expires_turn``, or stale) must not land
    on a candidate another worker has since delivered -- that would flip the
    question the host is showing to ``expired`` and its reply would then be
    refused as ``not_open``. Only a pass that read ``delivered`` may expire a
    ``delivered`` candidate.
    """
    status = from_status if from_status is not None else _str_attr(candidate, "status")
    if status not in _OPEN_STATUSES:
        return False
    return _claim(
        candidate,
        (status,),
        {"status": "expired", "resolved_turn": int(turn)},
    )


def _ignored_since(candidate: QuestionCandidate) -> int:
    """The turn a ``delivered`` candidate was asked (``created_turn`` for a
    candidate written before ``delivered_turn`` existed -- never
    ``last_seen_turn``, which re-observation keeps bumping)."""
    delivered = _int_attr(candidate, "delivered_turn")
    if delivered is not None:
        return delivered
    return _int_attr(candidate, "created_turn") or 0


def _cool_ignored(candidate: QuestionCandidate, turn: int) -> bool:
    """CAS an ignored ``delivered`` candidate to ``cooled``.

    The cooldown is measured from the delivery, so ``cooldown_until`` is
    ``delivered_turn + QUESTION_COOLDOWN_TURNS`` -- the turn at which it is
    recognised as ignored -- and the result is the same whichever later turn
    the expiry pass happens to run on. ``ask_count`` keeps the history.

    Guarded on the ``delivered_turn`` this caller read: if another worker
    cooled and re-delivered the candidate in the meantime, its status is
    ``delivered`` again but with a newer ``delivered_turn``, and this stale
    decision must not cool the question the host is showing right now.
    """
    return _claim(
        candidate,
        ("delivered",),
        {
            "status": "cooled",
            "cooldown_until": _ignored_since(candidate) + QUESTION_COOLDOWN_TURNS,
            "resolved_turn": int(turn),
        },
        guard={"delivered_turn": _int_attr(candidate, "delivered_turn")},
    )


#: Per-pass memo for staleness checks (see :func:`_staleness_memo`). ``None``
#: outside an expiry pass.
_PASS_MEMO: contextvars.ContextVar[Optional[Dict[Any, Any]]] = contextvars.ContextVar(
    "question_queue_pass_memo", default=None
)


def _staleness_memo() -> Optional[Dict[Any, Any]]:
    """The memo dict of the expiry pass currently running in this context, or
    ``None``. A staleness check whose evidence is the same for every candidate
    of a pass (the live-disjoin set) caches it here, so it is computed once
    per :func:`next_question` / :func:`expire_stale` call rather than once per
    candidate, without changing the ``check(candidate, turn)`` seam."""
    return _PASS_MEMO.get()


def _expire_stale_in(
    candidates: Sequence[QuestionCandidate], turn: int
) -> Tuple[int, List[QuestionCandidate]]:
    """Expire what should expire; return ``(n_expired, survivors)`` where
    survivors are deliverable-status candidates whose staleness is known
    False.

    A ``delivered`` candidate is never asked again while it waits for its
    reply, but it does not wait forever: a registered staleness check can
    expire it, and once ``QUESTION_COOLDOWN_TURNS`` have passed since its
    delivery with no reply it is treated like a non-answer and moved to
    ``cooled`` (:func:`_cool_ignored`), after which it is handled like any
    other cooled candidate -- re-askable, and subject to ``expires_turn``.
    """
    expired = 0
    survivors: List[QuestionCandidate] = []
    token = _PASS_MEMO.set({})
    try:
        for cand in candidates:
            status = _str_attr(cand, "status")
            if status == "delivered":
                if int(turn) < _ignored_since(cand) + QUESTION_COOLDOWN_TURNS:
                    if _is_stale(cand, turn):
                        expired += int(_expire(cand, turn, "delivered"))
                    continue
                if not _cool_ignored(cand, turn):
                    continue  # answered or otherwise moved concurrently
                status = "cooled"
            if status not in _DELIVERABLE_STATUSES:
                continue
            expires = _int_attr(cand, "expires_turn")
            if expires is not None and int(turn) > expires:
                expired += int(_expire(cand, turn, status))
                continue
            stale = _is_stale(cand, turn)
            if stale:
                expired += int(_expire(cand, turn, status))
            elif stale is False:
                survivors.append(cand)
    finally:
        _PASS_MEMO.reset(token)
    return expired, survivors


def expire_stale(agent_id: str, turn: int) -> int:
    """Silently expire the agent's not-yet-answered candidates that are past
    ``expires_turn`` or that a registered staleness check reports stale, and
    move ``delivered`` candidates ignored for ``QUESTION_COOLDOWN_TURNS`` to
    ``cooled``.

    Expired candidates are status-marked and retained (``prune()`` deletes).
    Returns the number expired; ``0`` on a Redis error (fail closed).
    """
    try:
        expired, _ = _expire_stale_in(_candidates(agent_id), turn)
        return expired
    except Exception:
        logger.exception("question_queue: expire_stale failed")
        return 0


# ---------------------------------------------------------------------------
# Producer API
# ---------------------------------------------------------------------------


def propose(
    agent_id: str,
    question_text: str,
    kind: str,
    source_module: str,
    target_keys: Sequence[str],
    ambiguity_signal: str,
    turn: int,
    options: Optional[Sequence[Dict[str, Any]]] = None,
    disjunction_id: Optional[str] = None,
) -> Optional[QuestionCandidate]:
    """Write a question candidate -- producers never ask directly.

    Dedup: an incoming proposal is a duplicate of an existing candidate that
    is open (pending/delivered/cooled) or ``answered`` within
    ``QUESTION_RETENTION_TURNS`` when ``kind`` matches and the
    ``target_keys`` sets intersect, **or** when the normalized question text
    matches exactly. A duplicate is not re-created: an open duplicate is
    *touched* (``last_seen_turn`` bumped, feeding the gate's impact factor)
    and returned; an answered one is returned unchanged.

    The dedup check and the create run under a short per-agent ``SET NX``
    lock, so concurrent proposals of one question yield one candidate. If the
    lock cannot be taken within a brief retry window the proposal is dropped
    (fail closed): a producer re-observes its ambiguity on a later turn.

    Returns:
        The new or existing candidate; ``None`` when the kill switch is set,
        the propose lock stays busy, or on a Redis error.

    Raises:
        ValueError: unknown ``kind`` / ``ambiguity_signal``, an empty question,
            or an option listing a key as both acted and contradicted.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; expected one of {KINDS}")
    if ambiguity_signal not in AMBIGUITY_SIGNALS:
        raise ValueError(
            f"unknown ambiguity_signal {ambiguity_signal!r}; "
            f"expected one of {AMBIGUITY_SIGNALS}"
        )
    norm_text = normalize_text(question_text)
    if not norm_text:
        raise ValueError("question_text must not be empty")
    cleaned_options = _validate_options(options)
    keys = [str(k) for k in target_keys or []]
    turn = int(turn)

    if not question_queue_enabled():
        return None

    try:
        token = _acquire_propose_lock(agent_id)
    except Exception:
        logger.exception("question_queue: propose lock failed")
        return None
    if token is None:
        logger.warning(
            "question_queue: propose lock for %s busy; proposal dropped", agent_id
        )
        return None
    try:
        return _propose_locked(
            agent_id,
            question_text,
            norm_text,
            kind,
            source_module,
            keys,
            ambiguity_signal,
            turn,
            cleaned_options,
            disjunction_id,
        )
    except Exception:
        logger.exception("question_queue: propose failed")
        return None
    finally:
        _release_propose_lock(agent_id, token)


def _propose_locked(
    agent_id: str,
    question_text: str,
    norm_text: str,
    kind: str,
    source_module: str,
    keys: List[str],
    ambiguity_signal: str,
    turn: int,
    cleaned_options: List[Dict[str, Any]],
    disjunction_id: Optional[str],
) -> QuestionCandidate:
    """Dedup check-and-create; caller holds the agent's propose lock, so two
    concurrent proposals of the same question cannot both miss the dedup scan.
    Raises on Redis errors."""
    key_set = set(keys)
    for existing in _candidates(agent_id):
        status = existing.status
        if status == "answered":
            if turn - _retention_reference(existing) > QUESTION_RETENTION_TURNS:
                continue
        elif status not in _OPEN_STATUSES:
            continue
        same_keys = _str_attr(existing, "kind") == kind and bool(
            key_set & set(_list_attr(existing, "target_keys"))
        )
        same_text = normalize_text(_str_attr(existing, "question_text")) == norm_text
        if not (same_keys or same_text):
            continue
        if status in _OPEN_STATUSES and turn > (
            _int_attr(existing, "last_seen_turn") or 0
        ):
            setattr(existing, "last_seen_turn", turn)
            existing.save(update_fields=["last_seen_turn"])
        return existing

    labels = [o["label"] for o in cleaned_options]
    candidate = QuestionCandidate(
        candidate_id=uuid.uuid4().hex,
        agent_id=agent_id,
        question_text=question_text,
        kind=kind,
        source_module=source_module,
        target_keys=keys,
        options=cleaned_options,
        ambiguity_signal=ambiguity_signal,
        status="pending",
        ask_count=0,
        created_turn=turn,
        expires_turn=turn + QUESTION_EXPIRY_TURNS,
        last_seen_turn=turn,
        cue_tokens=cue_tokens_for(question_text, *labels),
        disjunction_id=disjunction_id,
    )
    candidate.save()
    return candidate


def note_use(agent_id: str, keys: Iterable[str], turn: int) -> int:
    """Record that the host just injected ``keys`` into context at ``turn``.

    Bumps ``last_seen_turn`` (never backwards) on every open candidate whose
    ``target_keys`` intersect ``keys`` -- the "recently used" half of the VOI
    gate. Returns the number of candidates touched; ``0`` when disabled or on
    a Redis error.
    """
    if not question_queue_enabled():
        return 0
    key_set = {str(k) for k in keys or []}
    if not key_set:
        return 0
    touched = 0
    try:
        for cand in _candidates(agent_id):
            if cand.status not in _OPEN_STATUSES:
                continue
            if not key_set & set(_list_attr(cand, "target_keys")):
                continue
            if int(turn) > (_int_attr(cand, "last_seen_turn") or 0):
                setattr(cand, "last_seen_turn", int(turn))
                cand.save(update_fields=["last_seen_turn"])
                touched += 1
    except Exception:
        logger.exception("question_queue: note_use failed")
    return touched


# ---------------------------------------------------------------------------
# Consumer API
# ---------------------------------------------------------------------------


def _impact_order(candidate: QuestionCandidate) -> Tuple[int, int, int, str]:
    """Most recently used first; then least asked; then oldest; then id."""
    return (
        -(_int_attr(candidate, "last_seen_turn") or 0),
        _int_attr(candidate, "ask_count") or 0,
        _int_attr(candidate, "created_turn") or 0,
        str(candidate.candidate_id),
    )


def next_question(
    agent_id: str,
    turn: int,
    query_cues: Optional[Union[str, Iterable[str]]] = None,
) -> Optional[QuestionCandidate]:
    """The whole delivery contract: the next question to ask, if any.

    Pipeline, in order: expiry and registered staleness checks
    (:func:`expire_stale`); cooldown; the VOI gate (:func:`passes_voi_gate`);
    relevance timing (``query_cues`` must share at least one cue token with
    the candidate; ``None`` disables timing); ordering by impact; and only
    then one atomic script that grants the token bucket *and* claims the
    first still-deliverable candidate (:data:`_DELIVER_LUA`), so the budget is
    never spent when nothing is actually delivered. The delivered candidate's
    ``ask_count`` is incremented and ``delivered_turn`` recorded.

    Returns ``None`` -- never raises -- when disabled, when nothing passes,
    when the budget is exhausted (the bucket is not reset), or on a Redis
    error.
    """
    if not question_queue_enabled():
        return None
    turn = int(turn)
    try:
        _, survivors = _expire_stale_in(_candidates(agent_id), turn)
        cue_set = None if query_cues is None else _query_tokens(query_cues)
        eligible = []
        for cand in survivors:
            cooldown = _int_attr(cand, "cooldown_until")
            if cooldown is not None and turn < cooldown:
                continue
            if not passes_voi_gate(cand, turn):
                continue
            if cue_set is not None and not cue_set & set(
                _list_attr(cand, "cue_tokens")
            ):
                continue
            eligible.append(cand)
        if not eligible:
            return None
        eligible.sort(key=_impact_order)
        return _try_deliver(agent_id, turn, eligible)
    except Exception:
        logger.exception("question_queue: next_question failed")
        return None


# ---------------------------------------------------------------------------
# Answer path
# ---------------------------------------------------------------------------


def _model_classes_named(name: str) -> List[type]:
    found: List[type] = []
    stack = list(Model.__subclasses__())
    seen = set()
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        meta = getattr(cls, "_meta", None)
        if meta is None or getattr(meta, "abstract", False):
            continue
        if getattr(meta, "model_name", None) == name:
            found.append(cls)
    return found


def _resolve_targets(
    keys: Iterable[str], instances: Optional[Sequence[Model]]
) -> List[Model]:
    """Load a *fresh* instance for every key that still exists.

    Fresh loads guarantee no caller-supplied ``_superseded_by`` attribute ever
    reaches ``_apply_contradicted`` -- an answer must not close a validity
    interval. ``instances`` (optional) only disambiguates which class a key
    belongs to; otherwise the class is found by the key's model-name prefix.
    """
    class_for: Dict[str, type] = {}
    for inst in instances or []:
        try:
            class_for[inst.db_key.redis_key] = type(inst)
        except Exception:
            continue
    resolved: List[Model] = []
    for key in dict.fromkeys(keys):
        classes = (
            [class_for[key]]
            if key in class_for
            else _model_classes_named(key.split(":", 1)[0])
        )
        for cls in classes:
            loaded = getattr(cls, "query").get(redis_key=key, _no_track=True)
            if loaded is not None:
                resolved.append(loaded)
                break
    return resolved


def _apply_option_effects(option: Dict[str, Any], targets: Sequence[Model]) -> None:
    """Apply one chosen option as ``QUESTION_ANSWER_WEIGHT`` observations.

    One observation comes from the acted/contradicted outcome applier (which
    also brings the decay/cycle effects); ``QUESTION_ANSWER_WEIGHT - 1``
    further ``update_confidence`` calls go to every target that declares a
    ConfidenceField. Targets without one get only the outcome effects they
    support.
    """
    from ..fields.confidence_field import ConfidenceField

    acted = set(option.get("acted", []) or [])
    contradicted = set(option.get("contradicted", []) or [])
    outcome_map: Dict[str, str] = {}
    signal_for: Dict[str, float] = {}
    for key in acted:
        outcome_map[key] = "acted"
        signal_for[key] = _observation.ACTED_CONFIDENCE_SIGNAL
    for key in contradicted:
        outcome_map[key] = "contradicted"
        signal_for[key] = _observation.CONTRADICTED_CONFIDENCE_SIGNAL

    instances = [t for t in targets if t.db_key.redis_key in outcome_map]
    if not instances:
        return
    _observation.ObservationProtocol.on_context_used(instances, outcome_map)

    extra = max(int(QUESTION_ANSWER_WEIGHT) - 1, 0)
    for inst in instances:
        signal = signal_for[inst.db_key.redis_key]
        for field_name, field in inst._meta.fields.items():
            if not isinstance(field, ConfidenceField):
                continue
            for _ in range(extra):
                ConfidenceField.update_confidence(inst, field_name, signal=signal)


def record_answer(
    candidate: QuestionCandidate,
    answer_text: str,
    turn: int,
    instances: Optional[Sequence[Model]] = None,
) -> AnswerResult:
    """Record the person's reply to a delivered question.

    The reply is classified by :func:`classify_answer` and then:

    * ``answered`` -- **claim, then apply.** A Lua compare-and-set flips
      ``status`` from ``delivered``/``pending`` to ``answered`` and writes
      ``answer_option``. Only on a successful claim are the chosen option's
      effects applied (:func:`_apply_option_effects`). A crash between the two
      loses this answer's evidence; it never double-counts, because a second
      call fails the claim (``reason="not_open"``). Only ``delivered`` and
      ``pending`` are claimable: a late reply to a question the expiry pass
      already cooled as ignored also gets ``not_open`` and writes nothing --
      the question is re-asked after its cooldown instead.
    * ``deflected`` / ``unrecognized`` -- the candidate becomes ``cooled``
      with ``cooldown_until = turn + QUESTION_COOLDOWN_TURNS``; no evidence
      is written, so stored confidence is bit-identical.

    ``answer_text`` is never stored or logged.

    Args:
        candidate: The candidate returned by :func:`next_question`.
        answer_text: The person's free-text reply.
        turn: Host turn of the reply.
        instances: Optional target instances, used only to tell which model
            class each target key belongs to; fresh copies are always loaded.

    Returns:
        :class:`AnswerResult`. Never raises.
    """
    if not question_queue_enabled():
        return AnswerResult(applied=False, reason="disabled")
    turn = int(turn)
    options: List[Dict[str, Any]] = _list_attr(candidate, "options")
    classification, option_index = classify_answer(options, answer_text)
    try:
        if classification != ANSWERED:
            claimed = _claim(
                candidate,
                _ANSWERABLE_STATUSES,
                {
                    "status": "cooled",
                    "cooldown_until": turn + QUESTION_COOLDOWN_TURNS,
                    "resolved_turn": turn,
                },
            )
            return AnswerResult(
                applied=False,
                reason="cooled" if claimed else "not_open",
                classification=classification,
            )
        claimed = _claim(
            candidate,
            _ANSWERABLE_STATUSES,
            {
                "status": "answered",
                "answer_option": option_index,
                "resolved_turn": turn,
            },
        )
    except Exception:
        logger.exception("question_queue: record_answer claim failed")
        return AnswerResult(
            applied=False, reason="error", classification=classification
        )
    if not claimed:
        return AnswerResult(
            applied=False,
            reason="not_open",
            classification=classification,
            option_index=option_index,
        )
    assert option_index is not None  # classify_answer guarantees it on ANSWERED
    try:
        option = options[option_index]
        keys = list(option.get("acted", []) or []) + list(
            option.get("contradicted", []) or []
        )
        _apply_option_effects(option, _resolve_targets(keys, instances))
    except Exception:
        logger.exception(
            "question_queue: answer evidence for %s lost after claim",
            candidate.candidate_id,
        )
        return AnswerResult(
            applied=False,
            reason="apply_failed",
            classification=classification,
            option_index=option_index,
        )
    return AnswerResult(
        applied=True,
        reason="applied",
        classification=classification,
        option_index=option_index,
    )


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def prune(agent_id: str, current_turn: int) -> int:
    """Delete the agent's non-pending candidates older than
    ``QUESTION_RETENTION_TURNS`` (measured from ``resolved_turn``, else
    ``last_seen_turn``). Pending candidates are never pruned here -- they
    expire first. Returns the number deleted; ``0`` on a Redis error."""
    deleted = 0
    try:
        for cand in _candidates(agent_id):
            if cand.status == "pending":
                continue
            age = int(current_turn) - _retention_reference(cand)
            if age > QUESTION_RETENTION_TURNS:
                cand.delete()
                deleted += 1
    except Exception:
        logger.exception("question_queue: prune failed")
    return deleted


# ---------------------------------------------------------------------------
# Producers -- thin adapters, each independently disableable (by not calling
# it) and each failing closed. The assembler never calls these: a host hands
# them the signal it already holds.
# ---------------------------------------------------------------------------


def _reconciliation() -> Any:
    """Lazily import M5. Raises ``ImportError`` when it is unavailable, which
    the callers below turn into "no input" rather than a crash.

    ``importlib.import_module`` rather than ``from . import reconciliation``:
    the latter short-circuits on the package attribute once M5 was ever
    imported, so it could not observe M5 being made unavailable.
    """
    return importlib.import_module(f"{__package__}.reconciliation")


def _live_disjoins(agent_id: str) -> List[Tuple[str, str, str]]:
    """``(disjunction_id, side_a_key, side_b_key)`` for every *live* M5
    ``disjoin`` annotation of ``agent_id``, oldest first.

    ``merge_log_entries`` returns only ``validity__current=True`` annotations,
    so a retracted disjoin is simply absent here. Side A is the annotation's
    ``target`` (the entry reconciled), side B is the payload's ``other``. The
    stable id is the payload's ``disjunction_id`` (shared with the two
    ``ClaimMembership`` rows), falling back to the annotation's own key.
    Raises on an unavailable M5 or a Redis error.
    """
    recon = _reconciliation()
    live: List[Tuple[str, str, str]] = []
    for annotation in recon.merge_log_entries(agent_id):
        if getattr(annotation, "kind", None) != "disjoin":
            continue
        payload = recon._decode_payload(annotation)
        if payload is None:
            continue
        side_a = str(getattr(annotation, "target", None) or "")
        side_b = str(payload.get("other") or "")
        if not side_a or not side_b or side_a == side_b:
            continue
        disjunction_id = str(payload.get("disjunction_id") or annotation.pk)
        live.append((disjunction_id, side_a, side_b))
    return live


def _disjunction_is_stale(candidate: QuestionCandidate, turn: int) -> bool:
    """Staleness check for M5-produced candidates (registered at import).

    A candidate with no ``disjunction_id`` was not produced from an M5
    annotation and is never stale here. One with an id is stale when that id
    no longer appears among the live disjoins -- the annotation was retracted,
    so the question is no longer warranted and is expired rather than asked.
    Raises if M5 cannot be read; ``_is_stale`` then neither expires nor
    delivers the candidate this turn (fail closed).
    """
    disjunction_id = _str_attr(candidate, "disjunction_id")
    if not disjunction_id:
        return False
    return disjunction_id not in _live_disjoin_ids(_str_attr(candidate, "agent_id"))


def _live_disjoin_ids(agent_id: str) -> FrozenSet[str]:
    """Live disjunction ids for ``agent_id``, read once per expiry pass.

    Inside a pass the result -- or the exception, so an unreadable M5 fails
    every candidate closed without re-reading it -- is cached in
    :func:`_staleness_memo`; outside a pass it is read directly.
    """
    memo = _staleness_memo()
    slot = ("live_disjoin_ids", agent_id)
    if memo is not None and slot in memo:
        cached = memo[slot]
        if isinstance(cached, BaseException):
            raise cached
        return cached
    try:
        ids = frozenset(d for d, _, _ in _live_disjoins(agent_id))
    except Exception as exc:
        if memo is not None:
            memo[slot] = exc
        raise
    if memo is not None:
        memo[slot] = ids
    return ids


register_staleness_check("disjunction", _disjunction_is_stale)


def _entry_statement(key: str) -> str:
    """The journal statement behind ``key``, or ``""`` if it cannot be read."""
    try:
        from .provenance_journal import JournalEntry

        entry = JournalEntry.query.get(redis_key=key, _no_track=True)
    except Exception:
        return ""
    return "" if entry is None else str(getattr(entry, "statement", "") or "")


def _distinct_labels(texts: Sequence[str]) -> List[str]:
    """Use ``texts`` as option labels unless any is blank, two normalize
    equal, or one is a :data:`DEFLECTION_PHRASES` entry, in which case fall
    back to ``option 1`` ... (the 1-based index always matches in
    :func:`classify_answer` anyway).

    A label equal to a deflection phrase (a referent literally named "Next")
    could never be chosen by name -- :func:`classify_answer` checks
    deflections first -- so the whole set falls back to index labels.
    """
    normalized = [normalize_text(t) for t in texts]
    if (
        all(normalized)
        and len(set(normalized)) == len(normalized)
        and not DEFLECTION_PHRASES.intersection(normalized)
    ):
        return list(texts)
    return [f"option {i + 1}" for i in range(len(texts))]


def propose_from_disjunctions(agent_id: str, turn: int) -> int:
    """Propose one ``disjunction`` question per live M5 disjunct pair.

    Reads ``reconciliation.merge_log_entries(agent_id)`` filtered to
    ``kind="disjoin"`` and decodes each payload for the pair's two sides.
    Options are exclusive by construction: A = ``acted:[a],
    contradicted:[b]``, B = the mirror. ``disjunction_id`` is set so the
    registered staleness check expires the candidate if the annotation is
    later retracted. Calling this again re-observes the same pairs, which
    dedup folds into a touch (bumping ``last_seen_turn``).

    The answer is recorded on the candidate (``answer_option``); the
    ``JournalEntry`` sides carry no ConfidenceField, so nothing about them
    changes. Feeding the answer back so the disjunction resolves is out of
    scope (an M1/M5 change).

    Returns:
        The number of pairs proposed or touched; ``0`` when M5 is unavailable,
        the kill switch is set, or on any error. Never raises.
    """
    if not question_queue_enabled():
        return 0
    try:
        disjoins = _live_disjoins(agent_id)
    except ImportError:
        logger.info("question_queue: M5 reconciliation unavailable; no disjoins")
        return 0
    except Exception:
        logger.exception("question_queue: reading M5 disjoins failed")
        return 0
    proposed = 0
    for disjunction_id, side_a, side_b in disjoins:
        try:
            label_a, label_b = _distinct_labels(
                [_entry_statement(side_a), _entry_statement(side_b)]
            )
            candidate = propose(
                agent_id=agent_id,
                question_text=(
                    f"Two things I hold conflict: {label_a!r} or {label_b!r}. "
                    "Which is right?"
                ),
                kind="disjunction",
                source_module="reconciliation",
                target_keys=[side_a, side_b],
                ambiguity_signal="disjunction",
                turn=turn,
                options=[
                    {"label": label_a, "acted": [side_a], "contradicted": [side_b]},
                    {"label": label_b, "acted": [side_b], "contradicted": [side_a]},
                ],
                disjunction_id=disjunction_id,
            )
        except Exception:
            logger.exception("question_queue: disjunction proposal failed")
            continue
        proposed += int(candidate is not None)
    return proposed


def propose_from_gate(
    agent_id: str,
    metadata: Any,
    turn: int,
    query_text: str,
) -> Optional[QuestionCandidate]:
    """Propose a ``confirmation`` question from a confidence-gate refusal.

    ``metadata`` is ``assemble()``'s metadata dict (an ``AssemblyResult`` is
    accepted too). Acts only when ``metadata["gate"]`` reports ``gated``,
    ``mode == "refuse"`` and a non-empty ``refused_keys``; ``k`` is the top
    (rank-0) refused key, the one whose confidence fell below threshold.
    Options: ``"yes"`` = ``acted:[k]``, ``"no"`` = ``contradicted:[k]``.

    **Only ``"refuse"`` mode produces a question.** The assembler reports
    ``refused_keys`` in ``"flag"`` mode too, but there the records were
    injected into context anyway: nothing was withheld, so there is no
    refusal to clarify, and asking would spend the shared budget on a fact
    the agent is already using.

    The question text names ``k`` as well as the query, so two refusals of
    different facts under the same query stay two candidates instead of
    being folded together by normalized-text dedup.

    **Dormant unless ``confidence_gate_threshold`` is configured** on the
    ``ContextAssembler``: without it ``assemble()`` emits no gate metadata
    and this producer has nothing to read.

    Returns:
        The new or existing candidate; ``None`` when there was no refusal,
        when disabled, or on any error. Never raises.
    """
    try:
        if not isinstance(metadata, dict):
            metadata = getattr(metadata, "metadata", None)
        gate = metadata.get("gate") if isinstance(metadata, dict) else None
        if not isinstance(gate, dict) or not gate.get("gated"):
            return None
        if gate.get("mode") != "refuse":
            return None
        refused = [str(k) for k in gate.get("refused_keys") or [] if k]
        if not refused:
            return None
        key = refused[0]
        topic = str(query_text or "").strip() or "this topic"
        # NOTE: the raw Redis key in the person-facing text (and the cue tokens
        # its fragments add) -- left as-is because propose() dedups on kind +
        # target_keys intersection OR exact normalized text. Without the key,
        # two refusals of different facts under the same query would match on
        # text alone and collapse into one candidate. Removing it needs that
        # text-match rule to also require compatible target_keys, which
        # changes dedup for every producer; the host phrases question_text
        # before delivery anyway. The topic still supplies the cue tokens.
        return propose(
            agent_id=agent_id,
            question_text=(
                f"I'm not confident in what I recall about {topic!r} "
                f"({key}). Is it still accurate?"
            ),
            kind="confirmation",
            source_module="context_assembler.confidence_gate",
            target_keys=[key],
            ambiguity_signal="gate_refusal",
            turn=turn,
            options=[
                {"label": "yes", "acted": [key]},
                {"label": "no", "contradicted": [key]},
            ],
        )
    except Exception:
        logger.exception("question_queue: gate proposal failed")
        return None


def propose_from_resolution(record: Any, turn: int) -> int:
    """Propose one ``referent`` question per M4 ``evidence_gap`` reference.

    Reads ``record.references_json`` (the shape ``_serialise_reference`` in
    ``extraction/resolution_log.py`` writes) and, for each entry whose
    ``status == "evidence_gap"``, proposes its ``question`` with one option
    per entry in ``candidates``. Options carry empty ``acted`` /
    ``contradicted`` lists: the answer is recorded (``answer_option``), never
    applied as evidence.

    Each reference gets its own target key,
    ``"{record_key}#ref:{start}:{end}"``, so two gaps in one sentence are two
    questions rather than one swallowed by key-intersection dedup.

    Returns:
        The number of references proposed or touched; ``0`` when there are
        none, when disabled, or on any error. Never raises.
    """
    if not question_queue_enabled():
        return 0
    try:
        agent_id = str(getattr(record, "agent_id", "") or "")
        record_key = str(record.db_key.redis_key)
        references = json.loads(str(getattr(record, "references_json", "") or "[]"))
    except Exception:
        logger.exception("question_queue: unreadable ResolutionRecord")
        return 0
    if not agent_id or not isinstance(references, list):
        return 0
    proposed = 0
    for ref in references:
        try:
            if not isinstance(ref, dict) or ref.get("status") != "evidence_gap":
                continue
            seen: Dict[str, str] = {}
            for cand in ref.get("candidates") or []:
                text = str(cand)
                if normalize_text(text) and normalize_text(text) not in seen:
                    seen[normalize_text(text)] = text
            if not seen:
                continue
            surface = str(ref.get("surface") or "")
            question = str(ref.get("question") or "").strip() or (
                f"Who or what does {surface!r} refer to?"
            )
            candidate = propose(
                agent_id=agent_id,
                question_text=question,
                kind="referent",
                source_module="extraction.resolution",
                target_keys=[f"{record_key}#ref:{ref.get('start')}:{ref.get('end')}"],
                ambiguity_signal="evidence_gap",
                turn=turn,
                options=[
                    {"label": label, "acted": [], "contradicted": []}
                    for label in _distinct_labels(list(seen.values()))
                ],
            )
        except Exception:
            logger.exception("question_queue: evidence_gap proposal failed")
            continue
        proposed += int(candidate is not None)
    return proposed
