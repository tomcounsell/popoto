---
status: Ready
type: feature
appetite: Large
owner: valorengels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/566
---

# M7 — Question queue: rationed clarifying-question channel with a value-of-information gate (#566)

## Problem

The memory layer can detect that it is uncertain and has no way to do anything
about it except refuse.

**Current behavior (verified on `origin/main` at `5955d141`):**

- The confidence gate (`src/popoto/recipes/context_assembler.py`, the
  `gate_meta` block) emits five scalar metadata keys — `gated`, `gate_score`,
  `threshold`, `mode`, and a count — and discards the surviving candidate list.
  That list is the exact raw material a clarifying question would need, and it
  is dropped a few lines after being computed.
- `RecallProposal` (`src/popoto/fields/observation.py:579`) is a payload-free,
  short-TTL ZSET tracking whether injected memories were adjudicated. It is not
  a question queue and recon confirmed it cannot be retrofitted into one: no
  payload, and TTL semantics that would silently expire a pending question.
- `VALID_OUTCOMES` (`observation.py:57`) is a closed five-member vocabulary —
  `{acted, dismissed, deferred, contradicted, used}` — with per-outcome
  appliers `_apply_acted` (`:265`) … `_apply_used` (`:539`).
- Zero hits for any ask-the-user mechanism across `src/` and `docs/`. No
  rate-limiting primitive for human attention exists: every "budget" in the
  codebase is a token count or a graph-expansion cap.

**Desired outcome:** one shared, strictly rationed channel. A persisted
`QuestionCandidate` queue every subsystem writes into instead of asking
directly; a deterministic boolean value-of-information gate; a token-bucket
budget of one ask per K turns; relevance-timed delivery; silent expiry after N
turns; and answers recorded as high-weight but defeasible evidence, never a
hard overwrite.

## Freshness Check

**#564 / PR #709 (M5) landed, and it is now the first real question source.**
`src/popoto/recipes/reconciliation.py` materializes explicit disjunctions:
`reconcile_entry()` can return the `disjoined` outcome, appending a
`kind="disjoin"` annotation to the merge log
(`MERGE_KINDS = ("merge", "disjoin")` at `:349`) carrying both sides of the
pair. The precedence table (`resolve_precedence`, `:828`) is deliberately total,
so an unresolvable contradiction becomes a disjunct pair rather than a
coin-flip winner. The issue's Definitions table entry "M5 disjunct pairs" is no
longer hypothetical.

Two planning details the upstream notice pins, both of which this plan adopts:

1. **There is no "pending disjunctions" query in M5's public surface.** Live
   disjunct pairs are recoverable only via `merge_log_entries(agent_id)`
   (`:959`) filtered to `kind="disjoin"`, which returns only
   `validity__current=True` annotations — so a *retracted* disjoin disappears
   from the producer's view. The producer must decode the annotation `payload`
   (`_decode_payload`, `:978`) for the pair's two sides. This plan's producer
   does exactly that and treats a vanished disjoin as "question no longer
   warranted", expiring the candidate rather than asking a stale question.
2. **M5 has its own abstention path, and it is a *different* ambiguity signal.**
   The sameness judge abstains rather than guesses and is bounded by
   `_JudgeBudget` (`:557`); `Sameness` (`:397`) carries the abstention. A judge
   abstention ("I could not tell") and a precedence tie ("both are equally
   entitled") are distinct, and the VOI gate must say which it keys on. See Q1.

**#565 / PR #708 (M6) also landed** —
`src/popoto/recipes/view_resolver.py` ships `BeliefSheetResolver` and
`resolve_entries`. The issue named M6 as a question source; the resolver's
per-entry staleness is now a real, readable signal rather than a planned one.

## Prior Art

- #463 / PR #482 — confidence-gated retrieval; the refusal this module gives a
  repair path.
- `AccessTracker` + `_apply_used` (`observation.py:539`) — the existing "this
  fact is live in reasoning but unvalidated" signal, which recon recommends as
  the *impact* half of the gate. Reusing it means no new counter.
- `docs/plans/confidence_gated_retrieval.md`,
  `docs/plans/confidence_field_capped_bayesian.md` — the write-back target.
- `docs/plans/m5_reconciliation.md`, `docs/plans/sdlc-565.md` — the two live
  producers.

## Data Flow

```
producers (each optional; queue works with any subset)
  M5 disjoin annotations ──┐
  M4 evidence_gap records ─┤
  M6 / confidence-gate  ───┤
      refusals             │
                           ▼
                  QuestionCandidate (persisted Model)
                  {question_text, kind, source_module, target_keys,
                   ambiguity_signal, impact_signal, ask_count,
                   status, created_turn, expires_turn}
                           │
                  dedup by embedding similarity vs pending+answered
                           │
                           ▼
              VOI gate (deterministic boolean):
                 ambiguity_high AND recently_used
                           │
                           ▼
              token bucket: at most 1 ask per K turns
                           │
                           ▼
              relevance timing: only when current cues are near the topic
                           │
                    ┌──────┴──────┐
                    ▼             ▼
              delivered      expired after N turns (silent)
                    │
              human answer (free text)
                    │
              answer recognition → mapped onto VALID_OUTCOMES
                    │
              ConfidenceField.update_confidence (capped-Bayesian)
                    │
              high weight, still defeasible: a later contradiction supersedes
```

## Architectural Impact

One new recipe module plus **two small additive extensions**, and nothing else.
That ratio is the design constraint, not an accident: the issue's recon
explicitly rejected building a large new subsystem.

- CREATE `src/popoto/recipes/question_queue.py` — the `QuestionCandidate`
  model, the gate, the bucket, the producers' write API, and the
  "what should I ask next, if anything" read API.
- EXTEND the confidence-gate metadata in `context_assembler.py` to carry the
  refused candidates and their cues. This is additive: existing keys keep their
  meaning and existing callers are unaffected.
- EXTEND the answer path to map human answers onto the **existing** closed
  outcome vocabulary (`acted` / `contradicted`) rather than growing
  `VALID_OUTCOMES`. The docstring at `observation.py:154-161` already directs
  application-specific outcomes to map onto the five; this follows it.

**The contract ends at "the next question to ask, if any."** How a rider
question attaches to an assistant reply is host-application territory. This
module never writes to a transport.

## Appetite

**Size:** Large.

**Team:** Solo dev, code reviewer, PM for Q1 and Q4 (the two decisions that
change the model shape).

**Interactions:** PM check-ins: 1, before task 3. Review rounds: 2. The model
and the bucket are easy; the VOI gate's *definition* and the answer-recognition
path are where this will take rounds.

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| M1 journal present | `python -c "import popoto.recipes.provenance_journal"` | Evidence write-back target. |
| M5 present (first producer) | `python -c "from popoto.recipes.reconciliation import merge_log_entries"` | The disjoin source. |
| M6 present (optional producer) | `python -c "from popoto.recipes.view_resolver import BeliefSheetResolver"` | Staleness source. |
| ConfidenceField available | `python -c "from popoto.fields.confidence_field import ConfidenceField"` | Answer-as-evidence write-back. |
| Redis/Valkey reachable | `redis-cli -n 15 PING` | Test suite. |

None of the producers is a hard blocker: the queue must function with any
subset, and a test asserts that with each producer disabled in turn.

## Solution

### Key Elements

1. **`QuestionCandidate` is a real Model**, not a ZSET. Recon established
   `RecallProposal` cannot be retrofitted; the question is persisted state with
   a payload and a status, which is what a Model is for.
2. **The VOI gate is a deterministic boolean, computed from stored metadata.**
   The LLM only *phrases* the question. This is the module's central discipline:
   probabilistic VOI scoring was dropped by recon as false precision on
   sub-35B models, and re-introducing a score is the most likely way this
   design degrades.
3. **The budget is structural, not advisory.** A token bucket in Redis, checked
   and decremented atomically, so "at most one ask per K turns" is enforced by
   the data structure rather than by every caller remembering to check.
4. **Answers are evidence, never overwrites.** `ConfidenceField
   .update_confidence` capped-Bayesian, so a human answer dominates but a later
   contradiction can still supersede it.
5. **Re-asking after cooldown is legal.** The protocol is idempotent in the
   sense that matters: answers may legitimately differ over time, and the newest
   wins as the latest strong evidence. `ask_count` records the history.

### Flow

```python
# producer side — never asks, only writes
question_queue.propose(
    agent_id=...,
    question_text=...,          # phrased later, or by the caller
    kind="disjunction",         # from a closed kind vocabulary
    source_module="reconciliation",
    target_keys=[...],          # the facts this would resolve
    ambiguity_signal="precedence_tie",   # see Q1
)

# consumer side — the whole delivery contract
q = question_queue.next_question(agent_id, query_cues=...)   # or None
question_queue.record_answer(q, answer_text)
```

### Technical Approach

**Valkey-safe.** Model hashes, ZSETs for the pending queue ordered by impact,
a STRING or HASH counter for the token bucket, and Lua for the
check-and-decrement. No modules — no `BF.*`, no `CMS.*`. Dedup by embedding
similarity reuses the existing embedding path (`cached_embedding` in M5 has the
same shape) and must degrade to an exact/normalized-text match when no
embedding provider is configured, because the `embeddings` extra is optional.

**Default ON with a deploy-level kill switch.** Per repo doctrine the channel
is not opt-in. But "default ON" for a module that talks to a human deserves a
precise reading: what defaults ON is the *queue* — producers write candidates
and the gate evaluates them. Whether a question is ever *delivered* is the host
application's call, because this module's contract ends at `next_question()`.
So the kill switch (`Defaults.QUESTION_QUEUE_ENABLED`, readable at deploy level
like `NEVER_RECORD_ENABLED`) disables proposal and gating; it is not the only
thing standing between a library default and a user being interrupted. See Q5.

**K and N are pinned magic numbers.** `Defaults.QUESTION_BUDGET_TURNS` (K) and
`Defaults.QUESTION_EXPIRY_TURNS` (N), never constructor kwargs — repo doctrine.
Both must be registered in `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS`
or `tests/benchmarks/test_defaults_sync.py` fails in CI *after* review has
approved (this has already cost a lane once).

**"Turns" needs a definition.** K and N are counted in turns, and nothing in
`src/` currently owns a turn counter that survives a process restart. M3's
`turn_id` is per-extraction, not a monotonic counter. This is a real gap and
task 2 resolves it before anything depends on it. See Q3.

**Answer recognition.** The design study leans toward LLM-classifying free text
against the offered options. The risk recon named is misclassified deflection:
"I'd rather not say" scored as an answer corrupts a fact's confidence with a
non-answer. This plan's position: the classifier emits a closed three-way
enum — `answered` / `deflected` / `unrecognized` — and only `answered` reaches
`update_confidence`; the other two mark the candidate cooled-down, not
resolved. Mirrors M5's abstaining judge. See Q4.

**New code binds Redis via `get_REDIS_DB()`.** No
`from popoto.redis_db import POPOTO_REDIS_DB` anywhere in the diff — a plain
import passes file-scoped spy tests and fails only in a full-suite run.

## Failure Path Test Strategy

- Budget exhausted: `next_question()` returns `None`, does not raise, does not
  silently reset the bucket.
- All producers absent: the module imports and `next_question()` returns
  `None`. Asserted per-producer, each disabled in turn.
- A disjoin annotation retracted between proposal and delivery: the candidate
  expires rather than asking a stale question (Freshness Check, point 1).
- No embedding provider: dedup degrades to normalized-text match; asserted, not
  assumed.
- Answer classified `deflected` or `unrecognized`: confidence is
  **bit-identical** before and after. This is the assertion that keeps the
  misclassification risk honest.
- Expiry: a candidate past N turns is not returned and is not deleted
  destructively where its history is still wanted (see Q2).
- Redis unavailable mid-flow: the module fails **closed** — no question — and
  never raises into the caller's retrieval path.

## Test Impact

New: `tests/test_question_queue.py`. Existing: `context_assembler` gate tests
must be extended to assert the new metadata keys are present *and* that the
five existing keys are unchanged — the additive-extension guarantee is worth an
explicit assertion, since a silently changed metadata contract breaks callers
outside this repo.

**Connection binding for any fixture that builds its own client.** Do not read
`REDIS_URL` and trust it. An operator-injected `REDIS_URL=redis://localhost:6379/0`
— the live agent store — was observed on a developer machine on 2026-09-16, and
`tests/benchmarks/run_external._resolve_bench_db()` is a second binder
(`POPOTO_BENCH_DB`, default 14) with different precedence. Every fixture here
binds through `get_REDIS_DB()` and, if it stands up its own connection, asserts
the resolved DB from the live `connection_pool` and refuses `0`, following
`examples/tests/conftest.py`. See `docs/plans/sdlc-568.md` for the full
statement of this hazard.

## Rabbit Holes

- **Probabilistic VOI scoring.** Dropped by recon: false precision on sub-35B
  models. The gate is boolean.
- **Adaptive budgets.** Dropped: no reliable engagement signal, and the failure
  mode (the system quietly decides it may ask more) is invisible.
- **Question batching / digests.** Dropped: front-loads attention cost and
  destroys relevance timing, which is the whole point of riding along.
- **Building the delivery transport.** Host-application territory. The contract
  ends at `next_question()`.
- **Growing `VALID_OUTCOMES`.** The vocabulary is closed on purpose and its own
  docstring says to map onto it.

## Risks

**R1 — The gate's ambiguity factor is under-specified.** M5 offers two distinct
ambiguity signals with different semantics (Freshness Check, point 2), and the
confidence gate offers a third. Mitigation: Q1 settles it before task 3; the
model stores `ambiguity_signal` as a closed enum so the answer is queryable
afterward rather than buried in gate logic.

**R2 — Human attention is unrecoverable if the budget leaks.** A bucket checked
non-atomically, or reset on restart, produces a burst of questions — the exact
harm the module exists to prevent. Mitigation: Lua check-and-decrement; a test
that hammers `next_question()` concurrently and asserts exactly one delivery
per K turns; bucket state persisted, never in-process.

**R3 — Answer-as-evidence becomes an overwrite in practice.** If the weight is
high enough, capped-Bayesian is indistinguishable from an overwrite.
Mitigation: a test that a subsequent contradiction *does* move confidence after
a human answer — the defeasibility assertion, not merely the update assertion.

**R4 — Dedup false-positives suppress a genuinely new question.** Embedding
similarity over short question text is noisy. Mitigation: dedup only against
*pending and recently answered* candidates with the same `target_keys`
intersection, never on text alone.

**R5 — "Turns" has no owner.** See Technical Approach. Task 2 resolves it; if
it cannot be resolved without an `src/` change beyond the two permitted
extensions, that is a scope escalation to report, not to absorb.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG] Delivery transport / how a rider question attaches to a reply.
- [SEPARATE-SLUG] Any change to `VALID_OUTCOMES`.
- [SEPARATE-SLUG] Retrofitting `RecallProposal`. Recon settled this; do not
  revisit.
- [SEPARATE-SLUG] A "pending disjunctions" query in M5's public surface. The
  producer reads `merge_log_entries` as it stands; adding a public query to M5
  is an M5 change.
- [ORDERED] M8 (#567) using a budgeted ask as an audit tie-breaker. Noted in the
  issue as possible; it depends on M8 existing.
- Probabilistic scoring, adaptive budgets, batching (Rabbit Holes).

## Update System

No update-system change. New module, no dependency, no migration. Two new
`Defaults` constants, registered per the doctrine above.

## Agent Integration

This is the one module in the wave with a genuine agent-facing surface, and it
is deliberately one function: `next_question(agent_id, query_cues) -> Question
| None`. Nothing is pushed; the host asks. Document that shape explicitly so an
integrator does not build a second, unrationed path around it.

## Documentation

- `docs/features/question-queue.md` — new page: the model, the two gate
  factors, the budget, expiry, answer-as-evidence, and the explicit statement
  that delivery is the host's job.
- `docs/features/confidence-gated-retrieval.md` — cross-reference: the refusal
  now has a repair path, and the gate metadata gained keys.
- Per `project_diff_scoped_review_misses_docstrings`: grep repo-wide for
  docstrings and docs teaching "the gate refuses and that is the end of it" and
  update them; pre-existing lines never enter the diff.
- Issue comments on #564 and #565 noting they are now question producers.

## Success Criteria

- [ ] All question sources write `QuestionCandidate` records; a test asserts no
      module in `src/` addresses the person directly (grep-shaped assertion
      over the diff's new surface, in the `test_type_checking_guard.py` idiom).
- [ ] At most one question per K turns is delivered, enforced structurally by a
      Lua check-and-decrement, verified by a concurrent hammer test — not by a
      sequential one, which cannot detect a racy bucket.
- [ ] A question is askable only when **both** gate factors hold; each delivered
      question is traceable to the stored ambiguity signal and the stored
      recent-use evidence.
- [ ] `ambiguity_signal` is a closed enum that distinguishes M5 judge
      abstention from M5 precedence tie from confidence-gate refusal (Q1).
- [ ] An answer updates target-fact confidence as high-weight defeasible
      evidence, and a later contradiction still moves it — both asserted.
- [ ] A `deflected` or `unrecognized` answer leaves stored confidence
      bit-identical.
- [ ] Unasked candidates expire after N turns and are not delivered afterward.
- [ ] K and N live in `Defaults` and are registered in
      `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS`.
- [ ] The queue functions with each producer absent, asserted per-producer.
- [ ] A retracted M5 disjoin expires its candidate instead of asking.
- [ ] Dedup degrades to normalized-text matching with no embedding provider.
- [ ] Redis unavailable → no question, no raise into the retrieval path.
- [ ] The five existing confidence-gate metadata keys are unchanged; new keys
      are additive — asserted.
- [ ] `VALID_OUTCOMES` is unchanged — asserted by test.
- [ ] `Defaults.QUESTION_QUEUE_ENABLED` disables proposal and gating at deploy
      level without a model-code edit.
- [ ] Valkey-safe: core types + Lua only. Passes the Valkey CI job.
- [ ] New code binds via `get_REDIS_DB()`; no plain `POPOTO_REDIS_DB` import.
- [ ] `tests/test_question_queue.py` present; `docs/features/question-queue.md`
      published; `mkdocs build --strict` passes.
- [ ] `ruff check src/`, `black --check src/ tests/`,
      `scripts/mypy_ratchet.py` pass.

## Step by Step Tasks

### 1. Read the three ambiguity signals against their sources
M5 `Sameness` abstention, M5 precedence tie, confidence-gate refusal. Record
what each actually means at the point it is produced. Input to Q1.

### 2. Resolve what a "turn" is — **blocking**
K and N are counted in turns and nothing owns a durable turn counter. Establish
whether one exists, can be derived, or must be caller-supplied. If it must be
caller-supplied, that changes the public API shape and belongs in the plan
before task 4.

### 3. Settle Q1 and Q4 — **PM gate**
The ambiguity enum and the answer-recognition contract. Both change the model.

### 4. `QuestionCandidate` model + producer API
`propose()`, dedup, the closed `kind` and `ambiguity_signal` enums.

### 5. VOI gate + token bucket
Boolean gate; Lua check-and-decrement; the concurrent hammer test alongside,
not after.

### 6. Confidence-gate metadata extension
Additive only. Extend the existing gate tests in the same commit.

### 7. Answer path
Three-way classification; `acted`/`contradicted` mapping; `update_confidence`;
the defeasibility test.

### 8. Expiry + relevance timing
Silent expiry; cue-proximity check for delivery.

### 9. Producers
M5 disjoin reader (with the retraction case), M4 evidence_gap, M6 / gate
refusal. Each independently disableable.

### 10. Docs + issue comments

### 11. Verification
Full gate run, including the Valkey job.

## Verification

| Claim | How it is verified |
|---|---|
| Nothing asks directly | Source-shape assertion over the new surface |
| Budget is structural | Concurrent hammer test, exactly one delivery per K |
| Both gate factors required | Four-case truth-table test |
| Answers are defeasible | Contradiction-after-answer moves confidence |
| Deflection is safe | Confidence bit-identical |
| Works with any producer subset | Per-producer disabled tests |
| Additive extension | Existing gate keys asserted unchanged |
| Valkey-safe | Valkey CI job green |

## Questions for the architect

1. **Which ambiguity signal does the VOI gate key on?** There are now three
   with genuinely different semantics: M5's judge *abstention* ("I could not
   tell whether these are the same claim"), M5's *precedence tie* ("both claims
   are equally entitled"), and the confidence gate's *refusal* ("nothing I hold
   is confident enough"). They warrant different questions and arguably
   different budgets. **The plan proceeds on:** all three qualify, stored as a
   closed `ambiguity_signal` enum so the decision stays queryable — but the gate
   treats them uniformly. If a precedence tie should outrank an abstention, that
   is a priority ordering you need to state.

2. **Does an expired or answered candidate persist as history, or is it
   deleted?** Keeping it enables "we already asked this in March" and the
   re-ask cooldown; deleting it keeps the queue small, which the issue asks for.
   **The plan proceeds on:** status-marked, retained, with a bounded retention
   pinned in `Defaults`. Confirm — retention is a privacy-adjacent call, since
   a question's text can echo sensitive content.

3. **What counts as a turn, and who owns the counter?** K and N are in turns and
   no durable turn counter exists in `src/`. Options: derive from
   `AssemblyEvent` volume; require the host to pass a monotonic turn number;
   add a counter. **The plan proceeds on:** host-supplied turn number, because
   the host is the only party that actually knows, and inventing a counter
   inside the library would be wrong under concurrency. This changes the public
   API shape, so it should be confirmed rather than assumed.

4. **Answer recognition: LLM classification, offered options, or both?** The
   design study leans LLM-classify-free-text; recon named misclassified
   deflection as the risk. **The plan proceeds on:** a closed three-way enum
   (`answered` / `deflected` / `unrecognized`) with only `answered` reaching
   `update_confidence`. Open sub-question: does classification require a live
   model in CI (it would), or does the default path use a deterministic
   option-matcher with LLM classification opt-in? The plan assumes the latter.

5. **What does "default ON" mean for a channel that interrupts a human?** Repo
   doctrine says capabilities default ON with a deploy-level kill switch. Here
   the honest reading is that the *queue* defaults ON while *delivery* is the
   host's call — the module never sends anything. **The plan proceeds on** that
   reading. If you intend "default ON" to reach delivery, this module needs a
   transport and the scope roughly doubles.

6. **Should this wait for M8 (#567)?** The issue notes M8 may use a budgeted ask
   as an audit tie-breaker, which would make M8 a fourth producer competing for
   the same K-turn budget. Building the bucket now without knowing M8's demand
   risks a budget that M8 immediately renders inadequate. **The plan proceeds
   on:** build now; M8's tie-breaker is an `[ORDERED]` No-Go and a follow-up.
