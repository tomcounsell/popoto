---
status: Ready
type: feature
appetite: Large
owner: valorengels
created: 2026-09-16
tracking: https://github.com/tomcounsell/popoto/issues/567
---

# M8 — Honest feedback loop: epsilon-holdout exposure records, monotone confidence recalibration, performativity quarantine (#567)

## Problem

The existing memory feedback loop measures only what it injected, so it cannot
distinguish "this memory helps" from "we keep injecting this memory."

**Current behavior (verified on `origin/main` at `5955d141`):**

- `AssemblyEvent` (`src/popoto/recipes/memory_telemetry.py:103`) logs
  `injected` — a list of `{key, rank, score, source}` for the winners only.
  There is no field for a memory that was eligible and not selected, and none
  for a memory that was deliberately withheld.
- `emit_trace` (`context_assembler.py:1798`, written at `:2164`) attaches
  `metadata["trace"]` describing the **selected** records in final rank order.
  Eligible-but-not-selected candidates are dropped before the trace is built.
- `TelemetryAnalyzer.confidence_calibration` (`memory_telemetry.py:549`)
  *measures* calibration. Nothing fits a correction and nothing applies one;
  read paths use raw stored confidence.
- `_apply_acted` (`src/popoto/fields/observation.py:265`) corroborates
  confidence for any injected memory, with no notion of whether the system's
  own injection caused the behavior it is now counting as evidence. The
  performativity loop — inject "prefers X" → assistant does X → user
  accommodates → signal confirms X — is entirely unguarded.

**Desired outcome:** four things, in dependency order. (1) an epsilon-holdout
gate producing exposure records with an arm label, yielding a with-vs-without
signal; (2) a post-turn judge labeling exposed memories `confirmed` /
`contradicted` / `unobserved` — three labels only; (3) a periodic monotone
(PAVA) fit from stated confidence to observed reliability, persisted and
applied at **read time** with stored values untouched; (4) performativity
quarantine, where preference-like memories carry a distinct status,
system-injected corroboration is recorded but not applied, and suppression
audits are the graduation path.

## Freshness Check

**The stated prerequisite is now half-shipped, and the half that shipped
changes the causal interpretation.** The issue says "`assemble()` has no way to
withhold a specific eligible memory — no exclusion hook exists." That is no
longer true: `assemble()` accepts a keyword-only `exclude_keys`
(`context_assembler.py:1739`), documented at `:1791`, applied at `:1852`.

But read `EXCLUDE_HEADROOM_CAP` (`context_assembler.py:140`) before designing
on top of it. Exclusion does **not** shrink the injected set: `_fetch_limit`
adds the exclusion count back, up to a cap of 200, precisely so that
"suppression without headroom silently starves retrieval." The consequence for
this module is load-bearing and the issue does not anticipate it:

> **Withholding one eligible memory replaces it with the next-best candidate.
> It does not create a "without" condition; it creates a "this memory vs. its
> substitute" condition.**

Every causal claim this module makes must be phrased that way, or it will
overstate what the data supports. A memory whose withheld arm performs just as
well may be genuinely useless, or it may be perfectly substitutable by a
near-duplicate — and the exposure record must carry enough to tell those apart
(the substitute's key, at minimum). This is Q1.

**What is still genuinely missing** (the real remaining prerequisite): the
eligible-but-not-selected candidate list in the `emit_trace` payload, and a
`withheld` arm label on `AssemblyEvent`. Both are small additive changes.

**#564 / PR #709 (M5) landed and supplies the pooling axis.**
`src/popoto/recipes/reconciliation.py` exports `CLAIM_TYPES` (`:200`),
`normalize_claim_type` (`:255`), `ClaimMembership` (`:276`) and `ClaimClass`
(`:310`). Two properties from the upstream notice bind this plan's pooling
design:

1. `claim_type` is assigned by *capture*, before an entry's single `save()`.
   Every entry written before M5 shipped, and anything an unlabelled capture
   path produced, normalizes to `DEFAULT_CLAIM_TYPE = "note"`. **The
   rule-free catch-all bucket will be large at first**, which matters because
   a pooled estimate over a heterogeneous "note" bucket is close to
   meaningless. Pooling must report per-bucket n and refuse to publish a
   bucket below a pinned minimum.
2. **Class membership is not immutable.** A later `merge` or `disjoin`
   annotation repoints memberships and `replay()` can rebuild them, so a class
   id is a moving grouping key, not a fixed label. Pooling that assumes stable
   buckets is wrong. See Q4.

**#565 / PR #708 (M6) landed** — `src/popoto/recipes/view_resolver.py`. The
issue offers "ContextAssembler (or M6 #565 when present)" as the injection
point; M6 now exists, so the choice is live rather than hypothetical. This plan
proceeds on `ContextAssembler`, because that is where `exclude_keys` and the
trace already are.

**Two-tier lifecycle, not an open tier axis.** `memory_lifecycle.py` documents a
deliberate **two**-tier design: `episodic` and `semantic`, with working memory
approximated by rapid decay rather than a third tier. The issue proposes
hosting quarantine "on the existing `tier` axis." Adding a third tier
contradicts that documented design. See Q3.

## Prior Art

- #464 / PR #473 — telemetry. `_should_sample` (`memory_telemetry.py:266`) and
  its injectable `rng` are the exact seam the epsilon gate reuses.
- #491 / PR #495 — confidence-modulated decay; the loop this makes causal.
- `docs/plans/confidence_field_capped_bayesian.md` — the confidence mechanism
  recalibration must **not** touch.
- Recon rejected `PredictionLedgerMixin` as the exposure store:
  one-prediction-per-instance overwrite semantics, no turn correlation id, and
  it mutates confidence on resolve.

## Data Flow

```
assemble(...)
   │
   ├─ eligible candidates  ─────────────┐
   │                                    │  (NEW: surfaced in trace)
   ├─ epsilon gate (seeded rng) ────────┤
   │     with prob ε: pick one eligible │
   │     memory → exclude_keys          │
   │                                    │
   ├─ selection (+ headroom substitute) │
   ▼                                    ▼
injected[]                        withheld[]  (NEW field on AssemblyEvent)
   └────────────┬───────────────────────┘
                ▼
         exposure records: (turn × memory × arm)
                │
      post-turn judge: confirmed | contradicted | unobserved
                │
   ┌────────────┴─────────────┐
   ▼                          ▼
pooled with-vs-without    PAVA fit: stated confidence → observed reliability
by type / class                │
                        persisted calibration table
                               │
                     read-time wrapper over get_confidence()
                     (stored values bit-identical)

quarantine: preference-like memories
   └─ _apply_acted / _apply_used guard: system-injected corroboration
      is RECORDED but NOT APPLIED, until a suppression audit passes
```

## Architectural Impact

Additive, in four separable layers — and the sequencing is the plan's most
important structural decision, because layer 0 is the only thing that touches
the hot retrieval path.

- **Layer 0 (separately mergeable, first):** eligible-but-not-selected
  candidates in `emit_trace`; `withheld` field on `AssemblyEvent`. Smallest
  possible diff to `ContextAssembler`. Nothing else here is buildable without
  it, and it is the only part a reviewer needs to scrutinize for latency.
- **Layer 1:** the epsilon gate + exposure records, in
  `memory_telemetry.py` beside `_should_sample`, plus withheld-arm labels in
  `report_outcomes` (`:340`).
- **Layer 2:** recalibration as a `TelemetryAnalyzer` sibling — PAVA over the
  existing calibration buckets, a persisted table, and a thin read-time wrapper
  over `ConfidenceField.get_confidence`. **Never** touching stored values or
  the confidence Lua.
- **Layer 3:** quarantine — a status axis plus a guard in
  `_apply_acted`/`_apply_used`.

**Ownership split:** telemetry stays documented as fail-open pure observation.
The holdout gate *changes what the agent sees*, so it cannot live under that
documentation. A thin `src/popoto/recipes/feedback_loop.py` owns the active
behavior, and telemetry keeps its "observes, never intervenes" contract intact.
This separation is not cosmetic — it is what keeps the existing telemetry
guarantee true.

## Appetite

**Size:** Large. This is the largest module in the wave and the most likely to
need splitting across PRs.

**Team:** Solo dev, code reviewer, PM for Q1–Q4.

**Interactions:** PM check-ins: 2 — one after Layer 0 lands (confirming the
substitution semantics from the Freshness Check are acceptable), one on the
epsilon value and pooling granularity. Review rounds: 2–3.

## Prerequisites

| Requirement | Check Command | Purpose |
|---|---|---|
| Telemetry present | `python -c "from popoto.recipes.memory_telemetry import AssemblyEvent, TelemetryAnalyzer"` | The host for exposure records. |
| `exclude_keys` present | `python -c "import inspect,popoto.recipes.context_assembler as c; assert 'exclude_keys' in inspect.signature(c.ContextAssembler.assemble).parameters"` | Half the stated prerequisite, already shipped. |
| M5 present (optional) | `python -c "from popoto.recipes.reconciliation import CLAIM_TYPES, ClaimClass"` | Class-level pooling. |
| ConfidenceField present | `python -c "from popoto.fields.confidence_field import ConfidenceField"` | The read-time wrapper's target. |
| Redis/Valkey reachable | `redis-cli -n 15 PING` | Test suite. |

## Solution

### Key Elements

1. **Epsilon-holdout with a seeded, injectable rng**, reusing the
   `_should_sample` seam so tests are deterministic and the acceptance
   criterion "demonstrated in a test with a seeded rng" is met exactly.
2. **Exposure records hosted on `AssemblyEvent`**, which is already per-turn,
   TTL'd and outcome-joined — three properties a new model would have to
   re-earn. Recon chose this over `PredictionLedgerMixin` for good reasons that
   still hold.
3. **The substitute is recorded.** Per the Freshness Check, a withheld memory
   is replaced, not omitted; the exposure record carries the substitute's key
   so "useless" and "substitutable" are distinguishable downstream.
4. **Three judge labels, no more.** `confirmed` / `contradicted` /
   `unobserved`. Recon dropped a finer taxonomy because three is what a
   sub-35B judge labels reliably.
5. **Recalibration is read-time only.** Stored confidence is bit-identical
   before and after a fit — an assertion, not a convention.
6. **Quarantine records but does not apply.** A quarantined memory's `acted`
   outcomes are logged; they simply do not corroborate confidence until a
   suppression audit passes.

### Flow

```python
# Layer 0 — the only hot-path change
result = assembler.assemble(..., emit_trace=True)
result.metadata["trace"]        # selected, as today
result.metadata["eligible"]     # NEW: eligible-but-not-selected

# Layer 1 — the active wrapper, separate from telemetry
loop = FeedbackLoop(assembler, rng=seeded)
result = loop.assemble(...)     # may silently withhold one eligible memory
loop.report_outcomes(result, judge=...)

# Layer 2 — offline, periodic
table = Recalibrator(analyzer).fit()     # PAVA, persisted
calibrated_confidence(record)            # read-time wrapper; stored untouched
```

### Technical Approach

**Valkey-safe.** Model hashes, ZSETs, and the existing confidence Lua. PAVA is
computed in Python over the analyzer's existing buckets and persisted as a
small table — no server-side statistics, no modules.

**Default ON with a deploy-level kill switch.** Repo doctrine. Here the
capability that defaults ON is exposure *recording*; the epsilon gate defaults
ON at a small pinned epsilon. The kill switch is
`Defaults.FEEDBACK_HOLDOUT_ENABLED`, readable at deploy level like
`NEVER_RECORD_ENABLED`, because a PyPI adopter cannot always edit model code.
Note the asymmetry deliberately: a deployment that wants telemetry but not
withholding must be able to get it, since withholding changes agent behavior.

**Epsilon and every threshold are pinned magic numbers.**
`Defaults.FEEDBACK_EPSILON`, `Defaults.FEEDBACK_MIN_BUCKET_N`,
`Defaults.SUPPRESSION_AUDIT_TURNS` — in `Defaults`, never constructor kwargs,
and each registered in `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS` or
`tests/benchmarks/test_defaults_sync.py` fails in CI after review approves.

**Fail-open, in both senses.** Telemetry remains fail-open pure observation.
The feedback wrapper degrades to **no-holdout** on any error — the safe
direction is "the agent sees everything it would have seen anyway", never "the
agent silently loses a memory because a table lookup raised."

**Metric-family doctrine.** The with-vs-without comparison is one metric family
(judged outcome labels). It is never cross-compared against recall numbers from
SIQ / LoCoMo / LongMemEval. Any write-up must state the family.

**New code binds Redis via `get_REDIS_DB()`.** No plain `POPOTO_REDIS_DB`
import anywhere in the diff.

## Failure Path Test Strategy

- Recalibration table missing or corrupt: the read-time wrapper returns raw
  confidence, logs, and does not raise.
- Judge unavailable or raising: exposures are labeled `unobserved`, not
  dropped, and never `confirmed` by default — a confirm-biased fallback is the
  exact failure this module exists to prevent.
- `exclude_keys` produces an empty result set (pathological corpus): the
  withheld turn must be **behaviorally silent** — no user-visible error — which
  the headroom machinery already mostly guarantees; asserted, not assumed.
- Epsilon fires on a single-candidate turn: withholding the only eligible
  memory is a real "without" condition and should be either skipped or labeled
  distinctly. Asserted either way.
- Quarantined memory receives `acted`: outcome row written, confidence
  bit-identical.
- Class id reassigned between exposure and pooling (M5 replay): the pooling
  path must not silently attribute an exposure to the wrong bucket. Asserted
  with a forced reassignment.
- PAVA over an empty or single-bucket corpus: returns identity, not a crash and
  not a degenerate table.

## Test Impact

New: `tests/test_feedback_loop.py`. Extended: telemetry tests gain the
`withheld` field and arm-label assertions; `context_assembler` trace tests gain
the `eligible` key with an explicit assertion that `trace` is unchanged.

## Rabbit Holes

- **Matched-pair counterfactual statistics.** Dropped by recon: single-user
  data volume does not support it (recon estimates weeks to converge).
- **Bandit / adaptive epsilon.** Dropped: scheduler failure modes, and an
  adaptive epsilon that drifts upward silently withholds more and more.
- **A fine-grained outcome taxonomy.** Three labels.
- **Making the quarantine graduation automatic and aggressive.** A suppression
  audit that runs too eagerly is itself an intervention on the user.
- **Touching the confidence Lua or stored values.** Explicitly forbidden below.

## Risks

**R1 — Overstating causality.** The Freshness Check's substitution semantics
mean the naive reading ("with vs without") is wrong. Mitigation: record the
substitute; phrase every reported claim as "this memory vs. its substitute";
make the write-up wording a Success Criterion, as the LongMemEval plan does.

**R2 — A confirm-biased judge silently reintroduces self-confirmation.** The
issue names this and it is the subtlest failure here: a judge that leans
`confirmed` recreates the very loop the quarantine guards against, one layer
up. Mitigation: the judge's label distribution is itself reported; a test
asserts the fallback is `unobserved`; and Q2 asks whether the judge should be
evaluated against a held-out labeled set before its output is trusted.

**R3 — Convergence takes weeks, so the module ships unvalidated.** Mitigation:
ship with seeded-rng synthetic demonstrations proving the *machinery* is
correct, and state plainly that real pooled estimates need accumulation. Do not
publish a pooled number from a small corpus; `FEEDBACK_MIN_BUCKET_N` enforces
this structurally rather than by discipline.

**R4 — The `"note"` bucket swamps pooling.** Per M5's upstream notice. Mitigated
by per-bucket n reporting and the minimum-n refusal.

**R5 — Moving class ids corrupt pooling.** Per M5's upstream notice. Mitigated
by recording the class id *at exposure time* on the exposure record, so pooling
groups by what was true then, with reassignment handled explicitly rather than
by silent re-grouping. This is Q4.

**R6 — Layer 0 adds hot-path cost.** Surfacing eligible-but-not-selected
candidates means retaining a list that is currently discarded. Mitigation: gate
it entirely on `emit_trace`, which is already opt-in and already documented as
costing extra; assert the `emit_trace=False` path is byte-identical to today.

## No-Gos (Out of Scope)

- Any change to stored confidence values or the confidence Lua. Recalibration
  is read-time only — this is the module's hardest boundary.
- Any change to `VALID_OUTCOMES`.
- [SEPARATE-SLUG] Using a budgeted M7 (#566) ask as an audit tie-breaker.
- [SEPARATE-SLUG] Causal pruning in the forgetting/lifecycle layer. This module
  produces the signal; consuming it is separate.
- [ORDERED] Publishing any pooled real-data estimate. Machinery ships first;
  numbers need accumulation and a separate decision to publish.
- Bandit epsilon, matched-pair statistics, finer taxonomies (Rabbit Holes).

## Update System

No update-system change. New module + additive fields. Three new `Defaults`
constants, registered per doctrine. `AssemblyEvent` gains a field — note it is
TTL-bounded, so old records simply age out and no migration is needed; assert
that a pre-existing record without the field still reads.

## Agent Integration

The agent-facing effect is *invisible by design*: on a withheld turn the agent
sees a different context and nothing else changes. That invisibility is the
acceptance criterion ("behaviorally silent") and also the risk — document it
prominently so an integrator debugging a surprising answer knows holdout
exists and knows the kill switch.

## Documentation

- `docs/features/feedback-loop.md` — new page: the four layers, the
  substitution semantics (prominently — it is the most misreadable part), the
  three judge labels, read-time-only recalibration, quarantine and suppression
  audits, the kill switch, and the metric-family statement.
- `docs/features/memory-telemetry.md` — cross-reference clarifying that
  telemetry remains pure observation and the *feedback loop* owns intervention.
- `docs/features/confidence-field.md` — a note that a read-time calibration
  wrapper may exist and that stored values are untouched.
- Per `project_diff_scoped_review_misses_docstrings`: repo-wide grep for
  docstrings teaching "confidence you read is confidence as stored"; update
  them. Pre-existing lines never enter the diff.

## Success Criteria

- [ ] Every retrieval decision (injected **and** withheld-eligible) produces an
      exposure record carrying an arm label.
- [ ] A withheld exposure record also carries the **substitute** key that took
      its place, so "useless" is distinguishable from "substitutable".
- [ ] Withholding rate is bounded by `Defaults.FEEDBACK_EPSILON`; withheld
      turns are behaviorally silent — no user-visible error — asserted.
- [ ] A pooled with-vs-without comparison is computable from exposure records
      alone, demonstrated in a test with a seeded rng.
- [ ] Pooling reports per-bucket n and refuses to emit an estimate below
      `Defaults.FEEDBACK_MIN_BUCKET_N`.
- [ ] Pooling records the M5 class id **at exposure time** and handles a later
      reassignment explicitly — asserted with a forced reassignment.
- [ ] Recalibration is read-time only: a before/after test asserts stored
      confidence bytes are **identical** across a fit, and that the confidence
      Lua is unmodified.
- [ ] PAVA over an empty or single-bucket corpus returns identity.
- [ ] A quarantined memory's `acted` outcomes are recorded but do not
      corroborate its confidence until a suppression audit passes — both halves
      asserted.
- [ ] A judge that is unavailable or raising yields `unobserved`, never
      `confirmed`; the judge's own label distribution is reported.
- [ ] Telemetry remains fail-open pure observation; the feedback wrapper
      degrades to **no-holdout** on any error, asserted by fault injection.
- [ ] `emit_trace=False` output is byte-identical to today; the new `eligible`
      key appears only under `emit_trace=True`, and `trace` itself is unchanged.
- [ ] A pre-existing `AssemblyEvent` without the `withheld` field still reads.
- [ ] `Defaults.FEEDBACK_HOLDOUT_ENABLED` disables withholding at deploy level
      without a model-code edit.
- [ ] All three new `Defaults` constants registered in
      `tests/benchmarks/overrides.py`'s `MODULE_CONSTANTS`.
- [ ] Docs state the substitution semantics and the metric family; no
      cross-family comparison appears anywhere.
- [ ] Valkey-safe: core types + existing Lua only. Passes the Valkey CI job.
- [ ] New code binds via `get_REDIS_DB()`; no plain `POPOTO_REDIS_DB` import.
- [ ] `tests/test_feedback_loop.py` present; `docs/features/feedback-loop.md`
      published; `mkdocs build --strict` passes.
- [ ] `ruff check src/`, `black --check src/ tests/`,
      `scripts/mypy_ratchet.py` pass.

## Step by Step Tasks

### 1. Layer 0 — **separately mergeable, first**
Eligible-but-not-selected in the `emit_trace` payload; `withheld` on
`AssemblyEvent`. Smallest possible diff to `ContextAssembler`. Assert the
`emit_trace=False` path is unchanged. Merge this before anything below.

### 2. Confirm the substitution semantics — **PM gate**
Q1. The headroom behavior means the causal claim is narrower than the issue
assumes. Settle the framing before building the analysis.

### 3. Layer 1 — epsilon gate + exposure records
`feedback_loop.py`; gate beside `_should_sample` with the injectable rng;
withheld-arm labels in `report_outcomes`; substitute recording.

### 4. Judge
Three labels; `unobserved` fallback; label-distribution reporting.

### 5. Layer 2 — PAVA recalibration
`TelemetryAnalyzer` sibling; persisted table; read-time wrapper; the
bit-identical assertion written **before** the fit code.

### 6. Layer 3 — quarantine
Status axis (Q3); `_apply_acted`/`_apply_used` guard; suppression audit as the
graduation path.

### 7. Pooling
Per-bucket n, minimum-n refusal, exposure-time class id, reassignment handling.

### 8. Docs + fault-injection tests

### 9. Verification
Full gate run including the Valkey job; confirm the telemetry contract
docstrings still hold.

## Verification

| Claim | How it is verified |
|---|---|
| Every decision produces an exposure record | Seeded-rng test enumerating both arms |
| Withheld turns are silent | Behavioral assertion + pathological-corpus case |
| Recalibration never mutates | Byte-comparison of stored confidence across a fit |
| Quarantine records but does not apply | Outcome row present, confidence identical |
| Judge is not confirm-biased | Fallback test + reported label distribution |
| Fail-open in the safe direction | Fault injection → no-holdout, not no-memory |
| Hot path unchanged when off | `emit_trace=False` byte-identical |
| Valkey-safe | Valkey CI job green |

## Questions for the architect

1. **Given that withholding substitutes rather than omits, is the weaker causal
   claim acceptable?** `EXCLUDE_HEADROOM_CAP` exists specifically so exclusion
   does not starve retrieval, so the withheld arm is "this memory replaced by
   the next-best candidate," not "no memory." **The plan proceeds on:** record
   the substitute and phrase all findings as this-vs-substitute. The
   alternative — a true omission arm — means bypassing the headroom machinery,
   which contradicts the reason it was built. This is the decision most likely
   to be misread later, which is why it is Q1.

2. **What epsilon, and does the judge need validating before its labels are
   trusted?** Recon estimates weeks to converge for a single user, so epsilon
   trades convergence speed against how often the agent is deliberately
   degraded. **The plan proceeds on:** a small pinned epsilon in `Defaults`,
   value left for you to set, and shipping the machinery with seeded-rng
   demonstrations rather than real estimates. Sub-question: should the judge be
   scored against a held-out hand-labeled set before any pooled number is
   produced? The plan assumes yes but does not schedule it, because it needs a
   labeled set that does not exist.

3. **Where does quarantine status live, given the documented two-tier design?**
   The issue proposes the `tier` axis, but `memory_lifecycle.py` documents
   exactly two tiers with working memory deliberately excluded — a third tier
   contradicts that. Options: a separate `status` field; a boolean
   `quarantined` flag; genuinely extend `tier`. **The plan proceeds on:** a
   separate status axis, leaving `tier` alone. Confirm, because the issue says
   otherwise.

4. **How should pooling handle M5 class reassignment?** A `merge` or `disjoin`
   repoints memberships and `replay()` rebuilds them, so a class id is a moving
   grouping key. **The plan proceeds on:** record the class id at exposure time
   and group by that; a reassignment splits the history rather than rewriting
   it. The alternative — re-group on read — makes every past estimate mutable.
   Both are defensible; the first is more honest, the second more responsive.

5. **Is `ContextAssembler` or M6's resolver the injection point?** M6 now
   exists, so the issue's "or M6 when present" is live. **The plan proceeds
   on:** `ContextAssembler`, because `exclude_keys` and the trace already live
   there and Layer 0 is minimal against it. If M6 is meant to become the
   canonical read path, Layer 0 should target it instead and this changes
   early.

6. **Should this be one PR or four?** Layer 0 is separately mergeable and the
   issue explicitly asks for it to be sequenced first. The remaining three are
   coupled but large. **The plan proceeds on:** Layer 0 as its own PR, then
   Layers 1+2 together, then Layer 3. Say so if you want a single PR — it would
   be a very large review.
