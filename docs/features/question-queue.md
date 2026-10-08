# Question Queue

A rationed channel for clarifying questions. When the memory layer knows it is uncertain, it writes a `QuestionCandidate` instead of asking directly, and the host application asks the queue, once per turn, whether anything is worth asking. Shipped as M7 ([#566](https://github.com/tomcounsell/popoto/issues/566)).

**The contract ends at "the next question to ask, if any."** The queue never writes to a transport and never addresses a person. Phrasing a question and attaching it to a reply is the host's job.

## Why it exists

Three subsystems can detect uncertainty and, until now, could only refuse or abstain:

- [Reconciliation](reconciliation.md) stores a tie as a `disjoin` pair.
- The [confidence gate](context-assembler.md#confidence-gate) drops a low-confidence pull-path answer.
- [Reference Resolution](reference-resolution.md) marks a reference `evidence_gap`.

Each now has a repair path: propose a question, let the queue ration it, and feed the answer back as evidence. One shared budget covers all three, so no subsystem can start its own unrationed way of nagging the person.

## Data model

`popoto.recipes.question_queue.QuestionCandidate` is a regular `Model`, keyed by `candidate_id` and indexed on `agent_id`.

| Field | Meaning |
|-------|---------|
| `question_text` | The question, phrased by the producer |
| `kind` | One of `disjunction`, `confirmation`, `referent` |
| `ambiguity_signal` | One of `disjunction`, `gate_refusal`, `evidence_gap` |
| `target_keys` | Record keys of the facts the answer would resolve (the same key strings on either backend) |
| `options` | `[{"label": str, "acted": [keys], "contradicted": [keys]}]` |
| `status` | `pending`, `delivered`, `answered`, `cooled`, `expired` |
| `ask_count`, `delivered_turn` | Delivery history, written by the delivery script |
| `created_turn`, `expires_turn`, `last_seen_turn` | Turn bookkeeping (host-supplied turns) |
| `cooldown_until` | Not deliverable before this turn |
| `answer_option` | 0-based index of the chosen option; the free-text reply is never stored |
| `cue_tokens` | Lowercased word tokens (3+ characters, stopwords removed) from the question and option labels |

`kind` and `ambiguity_signal` are closed enums (`KINDS`, `AMBIGUITY_SIGNALS`). An unknown value raises `ValueError`. There is no "judge abstention" signal: a plain Reconciliation abstention writes nothing, and ties and probe splits both land as the same `disjoined` payload, so the queue cannot tell them apart.

### Statuses

```
pending -> delivered -> answered
              |
              +-> cooled   (reply was a deflection or unrecognized, or no reply at all)
pending -> expired         (not delivered within QUESTION_EXPIRY_TURNS)
cooled  -> delivered       (re-ask once cooldown_until has passed)
```

A `delivered` candidate that gets no reply within `QUESTION_COOLDOWN_TURNS` of its delivery is treated like a non-answer and moves to `cooled`. Expired and answered candidates are retained, not deleted, until `prune()` removes them.

### Options carry their own effects

Marking every unchosen option `contradicted` is only right when options are mutually exclusive, so each option lists its own effects. Answering applies exactly the chosen option's lists.

| Producer | Options |
|----------|---------|
| Disjunction | A = `acted:[a], contradicted:[b]`; B is the mirror |
| Gate refusal | "yes" = `acted:[k]`; "no" = `contradicted:[k]`, where `k` is the top refused key |
| Evidence gap | One option per candidate referent, with empty lists (recorded only) |

A key listed as both `acted` and `contradicted` in one option is a producer bug, and `propose()` raises `ValueError`.

## The gate

`passes_voi_gate(candidate, turn)` is a deterministic boolean with two factors, both read from stored metadata:

1. `ambiguity_signal` is in `AMBIGUITY_SIGNALS`.
2. The candidate was recently used: `turn - last_seen_turn <= QUESTION_RECENT_USE_TURNS`.

`last_seen_turn` is set when the candidate is proposed. It is bumped when a producer re-observes the same ambiguity, or when the host calls `note_use(agent_id, keys, turn)` with the keys it just injected. There is no probabilistic score, so every delivered question traces back to the evidence that let it through.

## The budget

At most one delivery per `QUESTION_BUDGET_TURNS` (K) turns, per agent. A Lua script (on Postgres, one statement behind the agent's bucket advisory lock) grants the token **and** claims a candidate in one step:

- It grants only when there is no prior ask or `turn >= last_ask_turn + K`.
- A regressed turn (`turn < last_ask_turn`) never grants, and `last_ask_turn` is never rewound.
- `last_ask_turn` is written only when a candidate is actually claimed, so budget is never spent when nothing is delivered.
- The bucket key carries a wall-clock TTL backstop (`QUESTION_BUCKET_TTL_SECONDS`, 7 days). A host whose turn counter reset after a restart is locked out for at most that long, never forever.

The counter lives in the store (Redis, or a Postgres table), not in process memory, so a restart does not refill the budget.

## Relevance timing

Pass `query_cues` (a string or an iterable of tokens) to `next_question()`. A candidate is deliverable only if at least one cue token overlaps its `cue_tokens`, so a question about meeting times is not asked in the middle of a database migration. `query_cues=None` turns timing off.

## Expiry and dedup

A pending candidate not delivered within `QUESTION_EXPIRY_TURNS` (N) becomes `expired` silently. Nothing is surfaced to the person.

A producer may also register a staleness check with `register_staleness_check()`. `next_question()` runs `expire_stale()` before delivering, and the disjunction producer registers one so that a retracted `disjoin` annotation never gets asked about.

Dedup rules, with no embeddings: an incoming proposal duplicates an existing candidate that is open (`pending`, `delivered`, `cooled`) or `answered` within `QUESTION_RETENTION_TURNS` when `kind` matches and the `target_keys` sets intersect, or when the normalized question text matches exactly. A duplicate is not re-created. An open duplicate is touched (`last_seen_turn` bumped) and returned. The dedup check and create run under a short per-agent lock, and a proposal that cannot take the lock is dropped.

**Known limitation.** The `answered` branch of that rule uses the same key intersection as the open branch, so for `QUESTION_RETENTION_TURNS` (500 by default) after a question is answered, any new proposal of the same `kind` that shares even one target key with it is folded into the answered candidate and never delivered. Answer "A or B?" at turn 1 and propose "A or C?" at turn 7: the producer reports the second as proposed or touched, but it is a duplicate of an answered record and nothing asks it. This fails in the safe direction (fewer questions, never a repeat). The intended fix is for the answered branch to require an equal `target_keys` set, or the same `disjunction_id`, rather than an intersection.

## Producers

Three thin adapters, each a separate function. Each is disabled by not calling it, and the queue works with any subset. The assembler never calls them. A host passes each the signal it already holds.

| Function | Source | Notes |
|----------|--------|-------|
| `propose_from_disjunctions(agent_id, turn)` | [Reconciliation](reconciliation.md) `disjoin` annotations | Imports `reconciliation` lazily |
| `propose_from_gate(agent_id, metadata, turn, query_text)` | `assemble()` metadata | See below |
| `propose_from_resolution(record, turn)` | `evidence_gap` references on a [Reference Resolution](reference-resolution.md) `ResolutionRecord` | Uses the reference's own clarifying question and candidates |

`propose_from_gate` is **dormant unless `confidence_gate_threshold` is set** on the `ContextAssembler`. Without a threshold, `assemble()` emits no gate metadata and the producer has nothing to read. It also acts **only in `"refuse"` mode**. In `"flag"` mode the records were injected anyway, nothing was withheld, and asking would spend the shared budget on a fact the agent is already using. The question text names the refused key as well as the query, so two refusals of different facts under one query stay two candidates.

## Answer recognition

`record_answer(candidate, answer_text, turn)` classifies the reply with a deterministic matcher. After normalization (lowercase, punctuation stripped, whitespace collapsed), in order:

1. In `DEFLECTION_PHRASES` ("skip", "pass", "not sure", "no idea", "i don't know", "rather not say", and similar): `deflected`.
2. Equal to exactly one option's normalized label, or to its 1-based index: `answered`.
3. Anything else, including a reply matching more than one option: `unrecognized`.

Deflections are checked first so a non-answer is never scored as an answer. An LLM classifier is deliberately out of scope.

`deflected` and `unrecognized` set the candidate to `cooled` with `cooldown_until = turn + QUESTION_COOLDOWN_TURNS` and write no evidence. The free-text reply is **never stored or logged**. Only the matched option index is kept, because a reply can echo sensitive content.

## Answers are evidence

An `answered` reply never overwrites anything. It is applied as defeasible evidence through the existing observation machinery:

1. **Claim.** A compare-and-set (a Lua script on Redis, a conditional `UPDATE` on Postgres) flips `status` from `delivered` (or `pending`) to `answered` and writes `answer_option`. If the status was anything else, the call returns `AnswerResult(applied=False, reason="not_open")` and changes nothing. That includes a reply that arrives after an ignored question was already moved to `cooled`: the late reply is not applied, and the question can be asked again after its cooldown.
2. **Apply.** The chosen option's `acted` keys map to the `acted` outcome and its `contradicted` keys to `contradicted`, applied through `ObservationProtocol.on_context_used`. Each target that declares a `ConfidenceField` then gets `QUESTION_ANSWER_WEIGHT - 1` further `update_confidence` observations at the same signal.

The total is `QUESTION_ANSWER_WEIGHT` observations. `update_confidence` has no weight argument, so this stands in for one. Past `evidence_cap` each observation has gain `1/(cap+1)`, so the evidence count stays capped and a later contradiction keeps its full gain. The claim is "W strong observations", never "dominates".

Other properties:

- `_superseded_by` is **never** set. A human answer must not close a validity interval.
- Claiming first means a crash between the steps loses that answer's evidence but never double-counts it. A second `record_answer` on an answered candidate is a no-op.
- Targets without a `ConfidenceField`, notably the `JournalEntry` sides of a disjunction, receive only the outcome effects they support. The answer is recorded on the candidate as `answer_option`. **Disjunction answers are recorded only.** Writing them back into the journal or Reconciliation so the disjunction resolves is out of scope for this feature.

See [ConfidenceField](confidence-field.md) for the update rule and [ObservationProtocol](observation-protocol.md) for the outcome vocabulary.

## Fail-closed behavior

`propose*`, `next_question`, `record_answer` and `note_use` log and return `None`, `0` or a non-applied `AnswerResult` on any storage error, Redis or Postgres. They never raise into the caller. Invalid input is the exception: an unknown `kind` or `ambiguity_signal`, an empty question, or an option whose `acted` and `contradicted` lists overlap raises `ValueError`, because that is a producer bug. `assemble()`'s own outage contract is untouched.

`AnswerResult.reason` is one of `applied`, `cooled`, `not_open`, `apply_failed`, `disabled`, `error`.

## Kill switch

Set `POPOTO_QUESTION_QUEUE_DISABLE=1` (any of the usual truthy strings) to turn the queue off. It is read on every call, not at import, so it can be flipped on a running deploy without editing model code. Disabled, producers write nothing, `next_question()` returns `None`, and `record_answer()` returns `reason="disabled"`.

The queue is default-on. What defaults on is the queue, not delivery: whether a question ever reaches a person is the host's call.

## Tuning constants

Pinned in `popoto.fields.constants.Defaults`. These are experimental tuning values, not user configuration. All counts are in host-supplied turns.

| Constant | Default | Meaning |
|----------|---------|---------|
| `QUESTION_BUDGET_TURNS` | 5 | K: one delivery per this many turns, per agent |
| `QUESTION_EXPIRY_TURNS` | 20 | N: undelivered candidates expire after this many turns |
| `QUESTION_RECENT_USE_TURNS` | 5 | Window for the "recently used" gate factor |
| `QUESTION_ANSWER_WEIGHT` | 3 | Total observations an answered reply writes per target |
| `QUESTION_RETENTION_TURNS` | 500 | `prune()` deletes non-pending candidates older than this |
| `QUESTION_COOLDOWN_TURNS` | 10 | Re-ask cooldown after a non-answer |
| `QUESTION_BUCKET_TTL_SECONDS` | 604800 | Wall-clock TTL backstop on the token bucket |

## Agent integration

The host drives the queue. The library supplies a candidate and records the outcome.

1. Pass a monotonic `turn` integer on every call. The library owns no counter.
2. Call `next_question(agent_id, turn, query_cues=...)` once per turn. A non-`None` result is the one question you may ask.
3. Phrase it and deliver it however your application delivers anything. Transport is not the library's concern.
4. When the person replies, call `record_answer(candidate, reply_text, turn)`.
5. Optionally call `prune(agent_id, turn)` periodically to bound retention.

Do not build a second path that asks questions around the queue, such as a producer that messages the person directly. The budget only protects the person if every question goes through `next_question()`.

## Usage

```python
from popoto.recipes import question_queue as qq

# A producer proposes; it never asks directly.
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

# Or let a producer adapter read an existing signal.
qq.propose_from_disjunctions("a1", turn=12)
result = assembler.assemble(query_cues={"topic": "deployment"}, agent_id="a1")
qq.propose_from_gate("a1", result.metadata, turn=12, query_text="deployment")

# The host asks the queue once per turn.
q = qq.next_question("a1", turn=13, query_cues="morning meetings with Dana")
if q is not None:
    ...  # host phrases and delivers q.question_text

    outcome = qq.record_answer(q, "morning", turn=14)
    print(outcome.applied, outcome.reason, outcome.option_index)
    # True applied 0
```

## On Postgres

A Postgres-bound queue keeps candidates as `question_candidate` rows, the
token bucket in `popoto_question_bucket (agent, last_turn, expires_at)` and
the propose lock in `popoto_lease`, where an expired row reads as absent, as
the Redis TTL makes the key vanish. Delivery is one statement behind the
agent's advisory lock, so the budget is exact under concurrency as on Redis.
Two behaviors differ (both rows of
[Records and other behaviour](postgres-backend.md#records-and-other-behaviour)):
a candidate whose row another transaction holds is skipped for the next
(`FOR UPDATE SKIP LOCKED`), where Redis's script waits and sees the write;
and a proposal that duplicates two or more open candidates folds into the
first in `_pk` order, where Redis uses set order. See
[Recipes, mixins and the queue](postgres-backend.md#recipes-mixins-and-the-queue-m4).

## See Also

- [ContextAssembler](context-assembler.md#confidence-gate) - the gate whose refusals feed `propose_from_gate`
- [Reconciliation](reconciliation.md) - source of `disjoin` pairs
- [Reference Resolution](reference-resolution.md) - source of `evidence_gap` references
- [ConfidenceField](confidence-field.md) - answers feed confidence as defeasible evidence
- [ObservationProtocol](observation-protocol.md) - the outcome vocabulary answers map onto
