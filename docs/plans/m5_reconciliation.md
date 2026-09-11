---
status: Planning
type: feature
appetite: Large
owner: agent-a773efcbae003f9f3
created: 2026-09-11
tracking: https://github.com/tomcounsell/popoto/issues/564
last_comment_id: 5537009267
---

# M5 — Reconciliation: claim equivalence classes, typed contradiction rules, explicit disjunctions

Tracking issue: #564.

## Problem

Popoto's agent-memory layer stores each extracted fact as an independent record;
nothing recognizes restatements or resolves contradictions structurally. After a
few turns the store holds three copies of "user prefers morning meetings" plus one
stale "user prefers evenings", and the read path injects all of them into the
prompt — the agent sees a self-contradicting brief with no signal about which
claim won.

**Current behavior:** Read-path dedupe is by redis key only
(`src/popoto/recipes/context_assembler.py:1951-1969`, drifted from the
`:1763-1781` cited in the issue), so two records asserting the same fact are both
injected. Contradiction handling in `ObservationProtocol._apply_contradicted`
(`src/popoto/fields/observation.py:373`, drifted from `:327`) now writes a
supersession edge when the model carries a `ValidityField` and the correcting
instance is supplied — but nothing ever supplies it from the memory path, so on
the journal it remains a scalar confidence nudge with no pointer to the winner.
The closest prior art, `TrajectoryMemory.crystallize()`
(`src/popoto/recipes/trajectory_memory.py:390-469`, tie-break at `:617-642`,
drifted from `:450-531` / `:637-643`), does fingerprint grouping plus a canonical
representative — but it is hardwired to trajectory sequences, has no class ids, no
merge log, and its tie-break always forces a single winner.

**Desired outcome:** After each turn's capture, same-subject same-type claims are
judged pairwise ("same claim under the convention book?") by the LLM against each
candidate class's representative. Sameness joins the class (a restatement confirms
instead of duplicating); a fired type-incompatibility rule triggers per-type
provenance precedence via `SupersessionProtocol.save_and_supersede`; a genuine tie
is stored as an explicit disjunct pair surfaced as uncertainty, never silently
broken. Every merge appends to an append-only merge log with rationale, making any
merge reversible by replay.

## Freshness Check

**Baseline commit:** `ea7fc584` (2026-09-11)
**Issue filed at:** 2026-08-13T06:28:40Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:1763-1781` (key-only dedupe) — drifted
  to `:1951-1969`; claim still holds, dedupe is still by redis key only.
- `src/popoto/fields/observation.py:327` (`_apply_contradicted` scalar nudge) —
  drifted to `:373`; claim PARTIALLY STALE (see Notes). Function now writes a
  supersession edge when the model carries a `ValidityField` AND the correcting
  instance is supplied via `superseded_by` / `instance._superseded_by`.
- `src/popoto/recipes/trajectory_memory.py:450-531` (`crystallize`) — drifted to
  `:390-469`; tie-break drifted from `:637-643` to `:617-642`
  ("most recent trajectory wins"). Claim still holds: no class ids, no merge log,
  forced single winner.
- `src/popoto/fields/existence_filter.py:274` (probabilistic membership, no
  identity) — module restructured around `_compute_fingerprint_impl`; claim still
  holds, no identity resolution added.
- `grep -rn "class_id|merge_log"` over `src/popoto/` — zero hits. No equivalence
  classes, merge log, typed rules, or disjunction semantics exist anywhere.

**Cited sibling issues/PRs re-checked:**
- #560 (M1, dependency) — CLOSED 2026-08-19. `ProvenanceJournal` + `JournalEntry`
  landed with `stated` flag, `ValidityField`, `EventStreamMixin` ("journal"
  stream), and an explicit `register_kind` seam whose docstring anticipates "a
  merge/equivalence kind". Dependency now available; strictly strengthens plan.
- #580 (V0, dependency) — CLOSED 2026-08-17. `SupersessionProtocol` with
  `identity_key` / `save_and_supersede` / `SupersedeResult` landed. Available.
- #494 (adjacent dedupe signal) — CLOSED. Intentionally separate scope; unchanged.
- #563 (M4, sharpens sameness) — CLOSED. Reference resolution landed. Available.
- #588 / PR #601 (upstream notice, sole issue comment) — MERGED. Membership
  decision moved into `SUPERSEDE_LUA`; `supersede()` raises
  `ValidityMemberAbsentError` instead of returning `None`; new
  `save_and_supersede` combined entry point. Incorporated in Solution.
- Downstream #565 (M6), #566 (M7), #567 (M8) — all OPEN. No landscape change.

**Commits on main since issue was filed (touching referenced files):**
- `a4f7fbf4` feat(#580) V0 validity primitives (#582) — enables deterministic tier;
  does not implement classes.
- `90fc3d30` fix(#588) supersession membership in LUA (#601) — enables atomic
  winner-write; does not implement classes.
- `1d50bd83` assembler through field layer (#656), `c046e1bd` call-time client
  (#697), `f0c3d29f` unsaved-instance guards (#615), `16aa702e` production audit
  (#594) — all irrelevant to root cause.

**Active plans in `docs/plans/` overlapping this area:** none. No plan slug
mentions reconciliation, equivalence, disjunction, or contradiction.

**Notes:** The `_apply_contradicted` drift is the only substantive change: V0 gave
contradiction a provenance shape, but the memory write path never supplies the
corrector, so journal contradiction is still winner-less in practice. M5 remains
the module that detects the contradiction and names both sides. All corrected
line numbers are used inline in Technical Approach.

## Prior Art

- **#582 (V0 validity primitives, merged):** `SupersessionProtocol` in
  `src/popoto/fields/supersession.py` — `identity_key(subject, predicate)`,
  `supersede` / `invalidate`, combined `save_and_supersede` /
  `save_and_invalidate`, typed errors under `ValidityError`. This IS the
  deterministic tier's write path; M5 must call it, never reimplement closing.
- **#601 (supersession membership guard in LUA, merged):** membership decided
  atomically inside `SUPERSEDE_LUA`; absent member raises
  `ValidityMemberAbsentError`. M5's reconcile loop must catch this (loser deleted
  between shortlist and write) instead of relying on the old `None` contract.
- **`TrajectoryMemory.crystallize()`** (`src/popoto/recipes/trajectory_memory.py:390-469`):
  group → threshold → canonical representative → watermark idempotence. The shape
  M5 borrows; the specifics M5 rejects (fingerprint equality instead of judged
  sameness, no class ids, forced single winner at `:617-642`).
- **`ProvenanceJournal`** (`src/popoto/recipes/provenance_journal.py:535+`):
  `append` / `confirm` / `supersede` / `retract` façade over immutable
  `JournalEntry`; `register_kind` seam explicitly names "a merge/equivalence
  kind" as a future consumer. M5 registers merge kinds here rather than adding a
  parallel annotation store.
- **`src/popoto/extraction/verdict.py::llm_verdict`:** the in-repo LLM-judge
  pattern — firewall checks before the call, single call with JSON-schema
  `output_config`, re-validation of every reply field against fixed vocabularies,
  never raises / never returns `None` (every failure maps to a logged verdict).
  M5's sameness judge copies this contract shape.
- **`EmbeddingField`** (`src/popoto/fields/embedding_field.py`): provider-set
  embeddings cached as in-memory matrices for cosine shortlist; numpy optional.
  Note `JournalEntry` carries NO `EmbeddingField` today — the shortlist needs one
  (source `"statement"`) or a bounded fallback.
- **#558 (export/import round-trip, merged):** per-field round-trip fidelity
  precedent — any new field M5 adds (`class_id`, disjunction links) must
  round-trip through it.

No prior issues found attempting equivalence classes themselves — no
**Why Previous Fixes Failed** section (greenfield module, not a re-fix).

## Research

No relevant external findings — proceeding with codebase context and training
data. Skipped per Phase 0.7 (purely internal work): the LLM-judge contract
(`extraction/verdict.py`), the embedding shortlist (`fields/embedding_field.py`),
and the stream trigger (`streams/consumer.py`, `fields/event_stream.py`) are all
in-repo patterns; no external library docs, ecosystem patterns, or migration
guides bear on the design.

## Data Flow

1. **Entry point**: post-turn background window. A `StreamConsumer` on the
   `"journal"` stream (metadata: `agent_id`, `kind`, `target`) wakes on new
   `kind="assert"` entries, or the capture pipeline calls the reconciler
   directly off M1's event emission. Input is one fresh `JournalEntry`
   (`statement`, `subjects`, `stated`, `turn_id`).
2. **Deterministic tier (zero LLM calls)**: compute the entry's identity key
   `subject|predicate` per the V0 amendment. Singleton-slot type rules (one
   birthdate per subject) and same-target deadline supersession express as
   identity-key collisions and resolve immediately through
   `SupersessionProtocol.save_and_supersede` — one MULTI/EXEC writing the
   winner, closing the loser, linking the chain, repointing the pointer.
3. **Embedding shortlist**: entries that find no deterministic collision are
   shortlisted to same-subject + same-type candidate classes via cosine
   similarity over the entry embedding (bounded N; judge calls scale with
   shortlist size, never with class count).
4. **LLM sameness judge**: the new claim is compared ONLY against each candidate
   class's most-confirmed member ("same claim under the convention book?"),
   prompted with the versioned convention book. Verdicts: same (join class,
   confirmation count += 1 via `ProvenanceJournal.confirm`), incompatible-type
   (fire the per-type rule → precedence → `save_and_supersede`), or genuine tie
   (write disjunct pair with shared disjunction id).
5. **Merge log**: every join / supersede / disjoin appends
   `{class_a, class_b, rationale, ts, judge_version}` as an immutable journal
   annotation (registered merge kind). Replay = revoke entry + recompute class
   assignment. Classes are tracked by plain relabeled `class_id`, never
   union-find.
6. **Output**: M6 reads one representative per class (most-confirmed member);
   without M5 it falls back to per-record. M7 consumes disjunct pairs as question
   sources; M8 pools causal estimates by `class_id`, falling back to by-type.

## Architectural Impact

- **New dependencies**: none vendored. New recipe module
  (`src/popoto/recipes/reconciliation.py`, name TBD at build) depends on
  `ProvenanceJournal`, `SupersessionProtocol`, `EmbeddingField`, and the
  `extraction/verdict.py`-shaped LLM client (anthropic extra, already optional).
  No new Redis modules — Valkey-safe by construction (memory:
  `feedback_valkey_compatibility`).
- **Interface changes**: additive only. `JournalEntry` gains `class_id`
  (IndexedField) and disjunction-link fields; new merge/disjoin kinds via
  `register_kind`. `ProvenanceJournal` gains no signature changes (reconciler
  calls existing `confirm` / `supersede`). M6/M7/M8 read the new fields; all
  degrade to per-record behavior without them.
- **Coupling**: reconciliation owns sameness; V0 owns closing; M1 owns
  annotation vocabulary. The amendment's invariant holds structurally: the
  deterministic tier calls `save_and_supersede`, so there is exactly one
  supersession mechanism and `class_id` subsumes identity keys (a deterministic
  hit is a class of size 2 merged without a judge call).
- **Data ownership**: class membership lives on entries (`class_id`) + the
  append-only merge log; no sidecar store to keep in sync.
- **Reversibility**: merge-log replay reproduces pre-merge assignment
  (acceptance criterion 4); the module itself is removable — entries remain
  valid journal records with `class_id` NULL.

## Appetite

**Size:** Large

**Team:** Solo dev, PM (convention-book wording + precedence table sign-off)

**Interactions:**
- PM check-ins: 2-3 (convention-book contents, type slots, precedence tables are
  plan-level config decisions per the issue's Downstream note)
- Review rounds: 2+ (judge-prompt wording, atomicity of class writes)

Solo dev work is fast — the bottleneck is alignment and review. Appetite measures communication overhead, not coding time.

## Prerequisites

No prerequisites — this work has no external dependencies. The LLM judge reuses
the existing optional `anthropic` extra; without it the deterministic tier still
functions and judge calls degrade to logged abstentions (same contract as
`llm_verdict`'s `LLM_UNAVAILABLE`). No API keys, services, or infra changes.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | test isolation (DB 15 via pytest plugin) |
| embeddings extra for shortlist tests | `python -c "import numpy"` | cosine shortlist coverage |

## Solution

### Key Elements

- **Equivalence classes**: plain `class_id` (IndexedField) on `JournalEntry`,
  relabeled on merge. A restatement joins the class and increments its
  confirmation count via `ProvenanceJournal.confirm` — confirmation instead of
  duplication.
- **Convention book**: short versioned config standard for "same claim"
  (converse phrasings merge, restatements confirm). The only prompt the judge
  sees; version recorded on every merge-log entry so replays are reproducible.
- **Frozen type enum**: `preference | deadline | trait | relationship | goal |
  procedure | note`, where `note` is the rule-free catch-all that absorbs the
  tail (decidability of rules dies with an open enum — recon Dropped bucket).
- **Typed contradiction rules**: per-type decidable checks with zero LLM calls
  (singleton slots, same-target deadline supersession). Firing a rule resolves
  through the per-type precedence table and writes via
  `SupersessionProtocol.save_and_supersede` — exactly one supersession
  mechanism.
- **Provenance precedence**: per-type ordering. Self-stated beats inferred
  everywhere (consumes M1's `stated` flag); recency wins for supersession types
  (deadlines), confirmation count wins for stable types (traits).
- **Disjunct pairs**: precedence ties stored with a shared disjunction id,
  retrievable together, surfaced as uncertainty. This module's own contribution —
  V0 always produces a deterministic winner within one identity key, so ties
  arise only at the judgment layer and stay here.
- **Append-only merge log**: `{class_a, class_b, rationale, ts, judge_version}`
  as immutable journal annotations (registered merge kind). Replay reproduces
  pre-merge assignment.

### Flow

**Post-turn capture** → new assert entry → **Deterministic tier** (identity-key
collision? → `save_and_supersede`, done, zero LLM calls) → **Embedding
shortlist** (same subject + type, bounded N) → **Judge vs each class
representative** (most-confirmed member only) → same → **join + confirm** /
rule fires → **precedence + supersede** / tie → **disjunct pair** → **append
merge-log entry** → M6/M7/M8 read classes downstream.

The new/revision/restatement/contradiction classification falls out of pipeline
order and is never asked as its own question.

### Technical Approach

- **Deterministic tier is a special case of `class_id`, never a parallel
  mechanism** (V0 amendment): an identity-key hit assigns both entries the same
  `class_id` and writes the outcome through `save_and_supersede`. Singleton-slot
  rules (one birthdate per subject) express as identity keys. The judge is the
  escalation path for claims normalization cannot equate.
- **Judge verdicts are not transitive — budget one symmetry re-check.**
  Recon and the design study both flag compounding false-"same" verdicts
  (mega-classes) as the top threat. The plan decision: after a "same" verdict
  joins entry E to class C, re-ask the judge once with E against C's
  representative *as restated including E's phrasing* (symmetry probe). A "same"
  both ways commits; a split verdict routes to a disjunct pair instead of a
  join. Cost is bounded (one extra call per join, still within shortlist
  budget) and it converts the worst failure (silent mega-class) into the safe
  failure (explicit uncertainty). Full N-way transitivity closure is a No-Go —
  quadratic judge calls for no additional safety over the symmetry probe.
- **Representative discipline**: the judge always sees the class's
  most-confirmed member, never a random sample; ties in confirmation count break
  by recency (mirrors `crystallize`'s modal+recency shape without copying its
  forced-winner semantics).
- **`ValidityMemberAbsentError` handling** (per #601 notice): the reconcile loop
  catches it around `save_and_supersede` — a loser deleted between shortlist and
  write is re-read; if gone, the merge-log records `loser-absent` and the winner
  stands. Never a silent skip, never a crash.
- **Replay cost bounded by watermark**: merge-log replay reprocesses only
  entries newer than the last replay watermark (borrow `crystallize`'s watermark
  shape: strict-`>` filter on `captured_at`, stored per reconciler run). Full
  from-genesis replay stays available as an explicit repair operation, not the
  steady state.
- **Embedding fallback**: if numpy/provider is absent, the shortlist degrades
  to same-subject + same-type index scan (bounded by `Defaults` cap) with zero
  judge-call change — recall narrows, correctness properties hold.
- **Numeric tuning constants live in `Defaults`** (per repo magic-numbers
  doctrine) and every new one is registered in
  `tests/benchmarks/test_defaults_sync.py`: shortlist cap, symmetry-probe
  on/off, replay watermark field, judge model/token caps (mirroring
  `VERDICT_MODEL` / `VERDICT_MAX_TOKENS`).
- **Judge failure contract copies `llm_verdict`**: firewall before the call,
  JSON-schema output, re-validate every field, never raise — failures map to a
  logged abstention that leaves the entry unclassified (new singleton class),
  never to a guessed join.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] The reconcile loop's `except` around `save_and_supersede`
  (`ValidityMemberAbsentError` → re-read → `loser-absent` log) must have a test
  asserting the merge-log entry, not a silent pass. Same for the judge
  abstention path (logged, entry stays singleton).
- [ ] Audit `except Exception` blocks in touched journal/supersession call sites
  during build; each needs an observable-behavior assertion.

### Empty/Invalid Input Handling
- [ ] Empty/whitespace-only `statement` never reaches the judge (firewall, same
  as `llm_verdict`'s `reject`/`empty_turn`); test asserts zero judge calls.
- [ ] Entries with empty `subjects` skip the deterministic tier (no identity key
  computable) and enter the judge path with subject-unbounded shortlist capped
  by `Defaults`; test covers the cap.
- [ ] Malformed judge replies (wrong schema, verdict outside fixed vocabulary,
  reply about a different class) map to abstention — copy `verdict.py`'s
  `_parse_reply` adversarial tests.

### Error State Rendering
- [ ] Disjunct pairs and supersession pointers must render in the M6-facing
  representative read as explicit uncertainty markers; test asserts the marker
  survives formatting rather than being dropped (no silent winner at read time).

## Test Impact

New module + additive fields; no existing behavior changes, so no UPDATE/DELETE
— but three adjacent suites constrain the build:

- [ ] `tests/` journal suites (`test_provenance_journal.py` et al) — MUST PASS
  UNCHANGED: `class_id` defaults NULL, merge kinds are additive registrations.
  Any failure here is a regression, not an expected update.
- [ ] `tests/test_ci_workflow_redis_url.py`-style env contracts — new tests bind
  via the pytest plugin (`popoto_test_db`), never `REDIS_URL` + DB 0.
- [ ] `tests/benchmarks/test_defaults_sync.py` — MUST UPDATE: every new
  `Defaults` constant registered (shortlist cap, probe flag, watermark,
  judge caps). This gate fails only in CI under narrow test selection, so the
  build must run it explicitly.
- [ ] New `tests/test_reconciliation_m5.py` (name per issue's
  `tests/test_<name>.py` criterion): restatement-joins, rule-fires-supersedes,
  tie-disjoins, replay-reverses, zero-LLM-calls-for-rules, judge-call bound.

No existing tests affected otherwise — greenfield recipe with additive-only
journal changes.

## Rabbit Holes

- Full N-way transitivity closure over classes (quadratic judge calls; the
  symmetry probe buys the safety cheaper).
- General entailment checking ("does claim A entail claim B?") — unreliable on
  small judges and unneeded; narrow sameness + type rules cover all four
  outcomes (recon Dropped bucket).
- LLM-extensible type schema — decidability of rules dies with an open enum;
  `note` absorbs the tail (recon Dropped bucket).
- Persisted union-find for classes — plain `class_id` relabel + merge log at
  one-person scale (recon Revised bucket).
- Rewriting `crystallize()` to share code with the reconciler — trajectory
  fingerprints and claim sameness only rhyme; a shared abstraction couples both
  to neither's benefit. Borrow the shape (watermark, modal+recency), not the code.
- Backfilling classes over the entire historical journal at ship time — steady
  state is incremental; genesis replay is an explicit repair op.

## Risks

### Risk 1: Judge non-transitivity compounds into mega-classes
**Impact:** One class absorbs unrelated claims; downstream belief sheets present
merged falsehoods as confirmed fact.
**Mitigation:** Symmetry probe on every join (split verdict → disjunct pair);
representative discipline (most-confirmed member only); merge log makes any bad
join reversible by replay. Mega-class detector (class size velocity alert) as
telemetry, not a gate.

### Risk 2: Convention-book wording silently moves the sameness boundary
**Impact:** Same code, different merges after a wording tweak; irreproducible
history.
**Mitigation:** Convention book is versioned config; version recorded on every
merge-log entry; replay pins the entry's version. Wording changes need PM
sign-off (appetite check-ins).

### Risk 3: Post-turn background window overruns under burst capture
**Impact:** Reconciliation lags; read path serves unreconciled duplicates.
**Mitigation:** Judge calls bounded by shortlist cap; deterministic tier absorbs
the common collisions with zero calls; lag is safe (unreconciled reads are
today's status quo, and M6 falls back to per-record).

### Risk 4: Embedding provider absent in consumer environments
**Impact:** No cosine shortlist; judge path falls back to coarse index scan.
**Mitigation:** Bounded subject+type scan fallback (correctness holds, recall
narrows); deterministic tier unaffected. Documented in feature docs.

## Race Conditions

### Race 1: Two reconciler runs process the same fresh entry
**Location:** new `recipes/reconciliation.py` run loop
**Trigger:** Overlapping post-turn triggers (stream redelivery + direct call)
both shortlist and judge the same entry concurrently.
**Data prerequisite:** Entry persisted in journal before either run reads it.
**State prerequisite:** `class_id` NULL on the fresh entry at both runs' read time.
**Mitigation:** Claim-by-write: the join writes `class_id` conditionally
(WATCH/MULTI or a Lua compare-and-set on NULL→id); the loser of the race sees
non-NULL and re-runs that entry against its now-assigned class. Merge-log
append is idempotent per `(entry_id, run_watermark)`. At-most-one reconciler
per agent documented (mirrors `crystallize`'s one-crystallizer-per-partition).

### Race 2: Loser deleted between shortlist and `save_and_supersede`
**Location:** reconcile loop → `SupersessionProtocol.save_and_supersede`
**Trigger:** Retraction/expiry removes the loser after the shortlist read.
**Data prerequisite:** Shortlist holds a live key that is gone at write time.
**State prerequisite:** None beyond the delete landing first.
**Mitigation:** `ValidityMemberAbsentError` catch → re-read → `loser-absent`
merge-log entry; winner stands. Specified in Technical Approach; tested.

No other concurrency: merge-log appends are immutable inserts; representative
reads are last-write-wins on confirmation counts and tolerate staleness.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #565] Belief-sheet representative surfacing — M5 guarantees one
  representative per class is *selectable* (most-confirmed member) and that
  per-record fallback works without it; the reader-facing view is M6's module.
- [SEPARATE-SLUG #566] Disjunct-pair question generation — M5 stores ties as
  retrievable-together pairs with a shared id; turning them into clarifying
  questions is M7's module.
- [SEPARATE-SLUG #567] Causal pooling by class — M5 guarantees `class_id`
  stability for M8 to pool on; the pooling itself is M8's module.

## Update System

No update system changes required — this feature is purely additive library
code. No migration: new journal fields default NULL, merge kinds register
additively, and the reconciler runs only when the host invokes it. Downstream
adoption is a normal version bump; unreconciled journals read exactly as today.

## Agent Integration

No agent-tool surface. The reconciler is library code the host process invokes
post-turn (via `StreamConsumer` on the `"journal"` stream or a direct call off
M1's event emission). No MCP wrapper, no bridge import, no new tool. Integration
tests verify the consumer-driven trigger path end to end (stream append →
reconcile → class assigned), which is the closest equivalent to "the agent can
invoke it".

## Documentation

### Feature Documentation
- [ ] Create `docs/features/reconciliation.md`: equivalence classes, convention
  book (with version), frozen type enum + per-type rules + precedence tables,
  disjunct-pair semantics, merge-log replay procedure, embedding-fallback behavior.
- [ ] Add entry to `docs/features/` index (mkdocs nav if index-driven — follow
  the precedent of the validity/journal feature pages).

### External Documentation Site
- [ ] Verify `mkdocs serve` builds with the new page (docs gate runs at merge).

### Inline Documentation
- [ ] Convention-book rationale comments on each per-type rule (why this
  precedence, not just what).
- [ ] Docstrings on the reconciler entry points, judge contract, and replay
  procedure (watermark semantics mirror `crystallize`'s documented limits).

## Success Criteria

- [ ] A restatement of a stored claim increments its class's confirmation count
  and creates no duplicate class (issue AC1).
- [ ] A firing type rule resolves by the per-type precedence table, marking the
  loser superseded (never deleted) with a pointer to the winner, written through
  `save_and_supersede` (issue AC2).
- [ ] A precedence tie produces a disjunct pair retrievable together; no silent
  winner at write or read time (issue AC3).
- [ ] Any merge is reversible: revoking a merge-log entry and replaying
  reproduces the pre-merge class assignment (issue AC4).
- [ ] Type rules run with zero LLM calls; judge calls are bounded by the
  embedding shortlist size (issue AC5).
- [ ] Tests at `tests/test_reconciliation_m5.py`; docs page under
  `docs/features/` (issue AC6).
- [ ] Tests pass (`/do-test`); Documentation updated (`/do-docs`); every new
  `Defaults` constant registered in `tests/benchmarks/test_defaults_sync.py`.
- [ ] Anti-criterion: M6 surfacing stays out (see Verification).

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using Task tools. The lead NEVER builds directly - they deploy team members and coordinate.

### Team Members

- **Builder (journal-model)**
  - Name: model-builder
  - Role: `class_id` + disjunction fields, merge-kind registration, Defaults constants
  - Agent Type: builder
  - Resume: true

- **Builder (deterministic-tier)**
  - Name: rules-builder
  - Role: identity-key mapping, frozen type enum, per-type rules + precedence, `save_and_supersede` wiring + `ValidityMemberAbsentError` path
  - Agent Type: builder
  - Resume: true
  - Domain: Redis/Popoto data (single-writer atomicity via SUPERSEDE_LUA; Valkey-safe, no modules)

- **Builder (judge-loop)**
  - Name: judge-builder
  - Role: convention book config, sameness judge (`llm_verdict` contract), embedding shortlist + fallback, symmetry probe, reconcile loop + watermark + merge log + replay
  - Agent Type: builder
  - Resume: true
  - Domain: Redis/Popoto data (claim-by-write compare-and-set on `class_id`; one-reconciler-per-agent discipline)

- **Validator (reconciliation)**
  - Name: recon-validator
  - Role: Verifies all success criteria, failure-path coverage, Defaults sync registration
  - Agent Type: validator
  - Resume: true

- **Documentarian (reconciliation)**
  - Name: recon-docs
  - Role: Feature docs page, index entry, mkdocs build check
  - Agent Type: documentarian
  - Resume: true

### Available Agent Types

Tier 1 core (`builder`, `validator`, `code-reviewer`, `test-engineer`,
`documentarian`, `plan-maker`, `frontend-tester`) per the skill baseline. No
standing specialists — domain work is prompted Tier 1 with a `Domain:` line as
above.

## Step by Step Tasks

### 1. Journal model surface
- **Task ID**: build-model
- **Depends On**: none
- **Validates**: `tests/test_reconciliation_m5.py::test_class_id_defaults_null` (create), existing journal suites unchanged
- **Assigned To**: model-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `class_id` IndexedField (NULL default) + disjunction-link fields to `JournalEntry`
- Register merge/disjoin annotation kinds via `register_kind`
- Add numeric constants to `Defaults`; register each in `tests/benchmarks/test_defaults_sync.py`
- Add/export round-trip coverage for the new fields (precedent: #558)

### 2. Deterministic tier
- **Task ID**: build-rules
- **Depends On**: build-model
- **Validates**: `tests/test_reconciliation_m5.py::test_singleton_rule_zero_llm_calls`, `::test_deadline_supersession_uses_save_and_supersede` (create)
- **Assigned To**: rules-builder
- **Agent Type**: builder
- **Parallel**: false
- Frozen type enum (`preference | deadline | trait | relationship | goal | procedure | note`)
- Per-type incompatibility rules + precedence tables (self-stated > inferred everywhere; recency for supersession types; confirmation count for stable types)
- All outcomes through `SupersessionProtocol.save_and_supersede`; `ValidityMemberAbsentError` → re-read → `loser-absent` log
- Assert zero judge calls on this path (spy on judge client)

### 3. Judge + shortlist + symmetry probe
- **Task ID**: build-judge
- **Depends On**: build-model
- **Validates**: `tests/test_reconciliation_m5.py::test_malformed_reply_abstains`, `::test_split_verdict_disjoins` (create; malformed-reply cases mirror `verdict.py` adversarial tests)
- **Assigned To**: judge-builder
- **Agent Type**: builder
- **Parallel**: true
- Versioned convention-book config; sameness judge copying `llm_verdict`'s contract (firewall, JSON schema, re-validate, never raise → abstention leaves singleton class)
- Embedding shortlist (same subject + type, capped) with bounded subject+type index-scan fallback when numpy/provider absent
- Symmetry probe on join (split verdict → disjunct pair, never a join)
- Empty/whitespace statements short-circuit with zero judge calls

### 4. Reconcile loop + merge log + replay
- **Task ID**: build-loop
- **Depends On**: build-rules, build-judge
- **Validates**: `tests/test_reconciliation_m5.py::test_restatement_confirms`, `::test_replay_reverses_merge`, `::test_concurrent_runs_claim_by_write` (create)
- **Assigned To**: judge-builder
- **Agent Type**: builder
- **Parallel**: false
- `StreamConsumer`-on-`"journal"` trigger + direct-call entry; claim-by-write compare-and-set on `class_id`; merge-log append `{class_a, class_b, rationale, ts, judge_version}`; watermark-bounded replay + explicit genesis repair op
- Restatement path routes through `ProvenanceJournal.confirm` (confirmation count)

### 5. Validate reconciliation
- **Task ID**: validate-recon
- **Depends On**: build-loop
- **Assigned To**: recon-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full new suite + adjacent journal suites + `test_defaults_sync.py` explicitly (fails only in CI under narrow selection)
- Verify judge-call bound under burst capture; verify disjunct marker survives representative read formatting
- Report pass/fail status

### N-1. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-recon
- **Assigned To**: recon-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/reconciliation.md`; index entry; `mkdocs` build check

### N. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: recon-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all validation commands
- Verify all success criteria met (including documentation)
- Generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New suite passes | `pytest tests/test_reconciliation_m5.py -q` | exit code 0 |
| Journal suites unregressed | `pytest tests/ -q -k "journal or provenance or supersession or validity"` | exit code 0 |
| Defaults sync registered | `pytest tests/benchmarks/test_defaults_sync.py -q` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `scripts/mypy_ratchet.py` | exit code 0 |
| No M6 surfacing in M5 module | `grep -rn "belief_sheet\|belief-sheet\|representative_view" src/popoto/recipes/reconciliation.py \| wc -l` | output contains 0 |
| No stale union-find machinery | `grep -rn "union.find\|UnionFind\|union_find" src/popoto/recipes/reconciliation.py \| wc -l` | output contains 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Symmetry probe confirmed?** The issue leaves single-verdict vs re-check to
   the planner; this plan budgets one symmetry re-check per join (split verdict
   → disjunct pair). Veto = single-verdict joins with mega-class telemetry only.
2. **Convention-book v1 contents?** The "same claim" standard wording and the
   precedence rows for `relationship | goal | procedure` need PM sign-off
   (self-stated > inferred and deadline-recency / trait-confirmation rules are
   set by the issue; the middle three types have no specified ordering).
3. **Frozen type enum final?** The 7-type list (`preference | deadline | trait |
   relationship | goal | procedure | note`) is the plan's proposal — merge or
   split any slot now, since the enum is frozen at build and later changes break
   decidability.
4. **Embedding placement?** Add `EmbeddingField(source="statement")` to
   `JournalEntry` (M1 schema change, shortlist native) vs a reconciler-side
   sidecar index (no M1 touch, second store to keep in sync). Plan assumes the
   former; flag if M1's model is meant to stay embedding-free.
5. **Trigger shape?** Plan builds both `StreamConsumer`-on-`"journal"` and a
   direct-call entry off capture. If the host wants exactly one, which?
