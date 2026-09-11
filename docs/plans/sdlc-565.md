---
status: Planning
type: feature
appetite: Medium
owner: Valor Engels
created: 2026-09-11
tracking: https://github.com/tomcounsell/popoto/issues/565
last_comment_id: none
---

# #565 — M6 Belief-sheet view resolver

## Problem

The assistant's read path can rank and truncate memory candidates but cannot remove one
because another record retracts it, has no reader-scoped visibility, and emits no
claim-level output. Concretely: `ContextAssembler.assemble()` is additive-then-truncate —
a retracted claim keeps its rank and gets injected into the prompt as if it were live;
supersession has no read-path collapse; tag scoping is cooperative and degrades to
unscoped retrieval on Redis error; formatters emit all fields or bare content, never a
claim with a stable provenance handle ("confirmed by N / superseded by X").

**Current behavior (recon-verified, re-verified 2026-09-11):**
no supersession/retraction semantics anywhere on the read path; staleness computed only
as a retrieval-level aggregate (`_staleness_ratio`, `context_assembler.py:953` — per-record
decayed scores computed then discarded, one ratio kept); scoping is an `agent_id`
partition filter plus cooperative tag scoping documented as "not a security boundary"
(`context_assembler.py:1605`, `1775`) that degrades to unscoped retrieval on Redis error;
a privacy filter bolted onto the existing `post_filter` seams would run after top-K
truncation, silently shrinking results.

**Desired outcome:** A `BeliefSheet`: surviving claims (retracted dropped, superseded
collapsed to winners, disjunctions shown as explicit uncertainty), each with a provenance
handle, a per-entry staleness annotation, and deterministic, replayable resolution —
computed by a pure function over the journal parameterized by a plain policy dict.

## Freshness Check

**Baseline commit:** `ea7fc584` (worktree HEAD, 2026-09-11)
**Issue filed at:** 2026-08-13T06:28:43Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:953` (`_staleness_ratio`) — issue cited `:951`;
  drifted 2 lines, claim still holds: per-record decayed scores computed via
  `_partition_scores_for_field`, then reduced to one ratio (`stale_count / len(records)`).
- `src/popoto/recipes/context_assembler.py:1605` ("cooperative, not a security boundary ...
  degrades to unscoped retrieval") and `:1775` (same in `assemble()` docstring) — issue
  cited `:1592`/`:1661`; drifted under refactors, claims still hold.
- `src/popoto/recipes/adaptive_assembler.py` (`AdaptiveAssembler.inner: ContextAssembler`
  wrapper) — composition precedent confirmed; class docstring matches issue description.
- `AssemblyResult` bit-for-bit stability surface — confirmed at `:1753`, `:1763`
  (`assess_quality=False` / `emit_trace=False` leave shape identical).

**Cited sibling issues/PRs re-checked:**
- #560 (M1 provenance journal) — CLOSED 2026-08-19. Substrate exists in tree:
  `src/popoto/recipes/provenance_journal.py` with `confirm` (`:646`), `supersede` (`:706`),
  `retract` (`:773`), `annotations_for` (`:836`), `chain` (`:854`).
- #580 (V0 validity primitives) — CLOSED 2026-08-17. Substrate exists:
  `src/popoto/fields/validity_field.py` (`ValidityField`, `SUPERSEDE_LUA`,
  chain fwd/rev hashes) plus assembler gating (`_resolve_excluded_keys`, `as_of`,
  `exclude_keys` params on `assemble()`).
- #564 (M5 reconciliation) — still OPEN. M5-optional degradation path (per-record collapse)
  remains the plan default.
- #463/PR #482 (confidence gating), #464/PR #473 (`emit_trace`) — reused as-is; `emit_trace`
  (`:1734`, `:1757`) provides the per-record `{key, rank, score, source}` handles M6 renders.

**Commits on main since issue was filed (touching referenced files):**
- `a4f7fbf4` feat(#580) V0 validity primitives (#582) — directly enables the amendment's
  pushdown-membership path; premise-supporting, not invalidating.
- `07b7268c` fix(#576) BM25/graph scoping (#593) — adjacent partition-leak fix; irrelevant
  to M6 semantics.
- `3a793d68` exclude_keys suppression + tail-position injection (#592) — gives M6 a
  suppression seam; premise-supporting.
- `16aa702e` agent memory production audit (#594) — contracts/P0 fixes incl. supersede
  atomicity; premise-supporting.
- `1d50bd83` route context_assembler through field layer (#648/#656) — refactor drift
  source for the line numbers above; claims re-verified against new locations.

**Active plans in `docs/plans/` overlapping this area:** `provenance_journal_m1.md`,
`validity_primitives_v0.md`, `context_assembler.md`, `context_assembler_hybrid_default.md`
— all shipped-area plans, no active competing build. No `belief`/`view-resolver`/`m6`
plan exists.

**Notes:** The amendment (V0 membership pushdown) is now buildable as written — both
prerequisite substrates are closed and present. The `_staleness_ratio` per-record exposure
(extension 2) has a concrete anchor: `_partition_scores_for_field` already returns the
per-record dict the ratio discards.

## Prior Art

Searched `gh issue list --state closed --search "belief sheet"` and
`gh pr list --state merged --search "belief sheet view resolver"` — both empty.
No prior attempt at a view resolver exists. Related shipped work this builds on:

- **#560 / `provenance_journal.py`**: M1 journal — `confirm`/`supersede`/`retract`
  annotations, `annotations_for`, `chain`, closing-kind validity intervals. M6's data source.
- **#580 / `validity_field.py` + assembler gating**: V0 validity axis — `SUPERSEDE_LUA`
  membership guards, chain fwd/rev hashes, `assemble(as_of=)`, `_resolve_excluded_keys`.
  M6's hot-path membership mechanism per the amendment.
- **#492 tag scoping** (`_resolve_tag_keys`, `_scope_by_tags`): the seam the reader gate
  extends — and the fail-open behavior (`:1605`) it must invert for privacy purposes.
- **`AdaptiveAssembler.inner`**: the wrap-don't-extend composition precedent M6 follows.
- **#494 tombstones-as-negative-prior**: adjacent write-time dedup signal, intentionally
  separate scope (per #564 recon).

No `Why Previous Fixes Failed` section — greenfield work, no prior fixes.

## Research

No relevant external findings — proceeding with codebase context and training data.
The work is purely internal: a new recipe over popoto's own journal/assembler substrates,
no external libraries, APIs, or ecosystem patterns involved.

## Spike Results

No subagent spikes dispatched (single-agent lane). Two verifiable assumptions were
resolved by direct code reads during planning; findings below are load-bearing for
the Technical Approach.

### spike-1: per-record staleness without a second Redis pass
- **Assumption**: "Exposing per-record staleness costs no second Redis round-trip."
- **Method**: code-read
- **Finding**: `_staleness_ratio` (`context_assembler.py:953-990`) already performs exactly
  one `_partition_scores_for_field` call returning a per-record `{key: score}` dict, then
  discards it into a ratio. A per-record variant returning the dict (or the
  `{key: (score, stale_bool)}` mapping) reuses the same single pass. Zero additional
  Redis work vs `assess_quality=True` confirmed structurally.
- **Confidence**: high
- **Impact on plan**: extension (2) is a small refactor (expose internals), not new I/O.

### spike-2: pre-truncation gating composes with existing fetch headroom
- **Assumption**: "A pre-truncation reader gate with over-fetch/back-fill fits the
  existing fetch budget."
- **Method**: code-read
- **Finding**: every arm already fetches bounded headroom (`max_items * 2`, or
  `max_items * HYBRID_CANDIDATE_MULTIPLIER` at `:1694`, `:2219`, `:2315`, `:2466`,
  `:2496`), and `selected = merged[: self.max_items]` truncates once at `:1973`.
  A gate applied to the merged candidate list before `:1973`, back-filling from the
  already-fetched headroom, needs no new fetch in the common case. Only pathological
  gate-rejection rates need a second bounded pull — cap it.
- **Confidence**: high
- **Impact on plan**: reader gate lives between merge and `:1973` truncation; over-fetch
  multiplier becomes a `Defaults` constant with a capped single back-fill.

## Data Flow

1. **Entry point**: caller invokes `BeliefSheetResolver.resolve(query, reader, policy)` —
   `reader` is a concrete `{agent_id, purpose, tags}` triple, `policy` a plain dict.
2. **Retrieval (unchanged)**: the wrapped `ContextAssembler` runs its normal pipeline
   (composite/hybrid/lexical pull with `agent_id` partition filters, V0 validity gating
   via `as_of`/`_resolve_excluded_keys`, tag scoping), returning ranked candidates with
   `emit_trace` handles. `assemble()` itself is untouched.
3. **Reader gate (pre-truncation)**: each candidate is checked against the reader/purpose
   before the `max_items` cut; rejected records are dropped and the sheet back-fills from
   over-fetched headroom up to `max_items`. Gate errors fail CLOSED (empty result, logged).
4. **Membership (V0 pushdown)**: retracted/superseded entries are already excluded inside
   the range read; this stage trusts that exclusion for membership and spends no chain walk.
5. **Chain resolution (pure function)**: for surviving records, `annotations_for`/`chain`
   are folded under the policy dict — drop retracted stragglers, collapse superseded to
   winners (with loser→winner handle links), count confirmations, pair M5 disjuncts as
   explicit uncertainty, flag unresolved contradictions for LLM escalation.
6. **Staleness annotation**: per-record decayed score attached to each claim from the
   exposed `_staleness_ratio` internals — no second Redis pass.
7. **Output**: `BeliefSheet` — ordered surviving claims, each
   `{handle, content, staleness, provenance: {confirmations, superseded_by, disjunct_with}}`,
   deterministic in (journal, policy): byte-identical replay.

## Architectural Impact

- **New dependencies**: none. Pure stdlib + existing `ProvenanceJournal`,
  `ValidityField`, `ContextAssembler` substrates.
- **Interface changes**: none to existing APIs. New module `recipes/view_resolver.py`
  (`BeliefSheetResolver` wrapping `inner: ContextAssembler`, AdaptiveAssembler-style);
  two narrow extensions to `context_assembler.py` (reader-gate hook point pre-truncation;
  per-record staleness accessor). `assemble()` behavior without the resolver is unchanged
  (bit-for-bit stability surface preserved).
- **Coupling**: resolver depends on M1 chain APIs and V0 validity gating; degrades to
  per-record collapse without M5 (M5 types consumed structurally — `class_id` /
  disjunction id — never imported, so the parallel M5 lane cannot break this build).
- **Data ownership**: unchanged. Journal owns chains, validity axis owns membership,
  resolver owns only the view (no writes).
- **Reversibility**: trivially reversible — additive module; removing it restores today's
  record-level output.

## Appetite

**Size:** Medium

**Team:** Solo dev, PM (scope alignment on policy-dict defaults), code reviewer

**Interactions:**
- PM check-ins: 1-2 (policy-dict shape; reader/purpose vocabulary)
- Review rounds: 1 (code review + critique war room)

New recipe plus two surgical extensions to a 2700-line stability-surfaced file, pure-function
resolution with determinism tests, fault-injection test, docs page. Not Large: no new Redis
data structures, no Lua, no LLM calls on the hot path.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test isolation (DB 7 in this lane) |
| M1 journal in tree | `python -c "from popoto.recipes.provenance_journal import ProvenanceJournal; print(hasattr(ProvenanceJournal,'chain'))"` | Chain data source |
| V0 validity in tree | `python -c "from popoto.fields.validity_field import ValidityField; print(True)"` | Membership pushdown |

No prerequisites — no external dependencies beyond the above (all present, verified above).

## Solution

### Key Elements

- **`BeliefSheetResolver` (new, `recipes/view_resolver.py`)**: wraps `inner: ContextAssembler`
  (AdaptiveAssembler precedent), never extends `assemble()`. Owns gate → resolve → annotate →
  emit.
- **Pure resolution function**: `(entries, chains, policy_dict) -> BeliefSheet`. Drop
  retracted → collapse superseded to winners (loser keeps a `superseded_by` handle) →
  count confirmations → pair disjuncts as uncertainty → flag unresolved contradictions for
  LLM escalation only. Deterministic: same journal + same policy → byte-identical sheet.
- **Reader gate (extension 1)**: visibility check per concrete `{agent_id, purpose, tags}`
  at the tag-scoping seam but pre-truncation (between merge and the `max_items` cut),
  with over-fetch/back-fill to `max_items`. Fails CLOSED on error (today's silent-degrade
  to unscoped is a leak for privacy purposes and is inverted here without changing the
  cooperative default of bare `assemble()`).
- **Per-record staleness (extension 2)**: expose `_staleness_ratio`'s internals as a
  per-record accessor over the already-fetched `_partition_scores_for_field` dict —
  no second Redis pass.
- **M5-optional collapse**: when entries carry M5 `class_id`/disjunction ids, collapse to
  one representative per class and surface disjunct pairs together; without M5, degrade to
  per-record resolution. M5 types are consumed structurally, never imported.

### Flow

Caller with reader/purpose → `BeliefSheetResolver.resolve()` → wrapped `ContextAssembler`
retrieves (validity-gated, trace-attached) → reader gate filters pre-truncation with
back-fill → chain resolution folds annotations under policy → staleness annotated →
`BeliefSheet` (claims with handles + "confirmed by N / superseded by X") → unresolved
contradictions flagged for LLM escalation.

### Technical Approach

- New file `src/popoto/recipes/view_resolver.py`. All Redis access via `get_REDIS_DB()`
  (never a `POPOTO_REDIS_DB` import — standing rule). New code paths only; no edits to
  retrieval arms, fusion, or formatters.
- Extension 1 anchor: gate sits between candidate merge and `selected = merged[: max_items]`
  (`context_assembler.py:1973`); reuses the arms' existing `max_items * 2` /
  `HYBRID_CANDIDATE_MULTIPLIER` headroom, plus one capped back-fill pull. Gate predicate
  takes `(record_key, reader)` and returns allow/deny; errors → deny + log (fail closed).
  Corrected line refs: stability surface `:1753`/`:1763`, tag seam `:1605`, cut `:1973`.
- Extension 2 anchor: module-level `_staleness_ratio` (`:953`) gains a sibling returning
  the per-record `{key: (decayed_score, stale_bool)}` mapping from the same single
  `_partition_scores_for_field` call; the ratio function delegates to it (behavior
  unchanged, covered by existing tests).
- Policy dict: plain `dict` (`prefer: recent | self-stated | confirmed`,
  `staleness_threshold`, `gate_overfetch_multiplier`, `max_backfill_pulls`). Numeric
  defaults pinned in `Defaults` (magic-numbers rule) and registered in
  `tests/benchmarks/test_defaults_sync.py` (standing rule).
- Provenance handles reuse `emit_trace` `{key, rank, score, source}` records (#464) —
  the sheet renders those handles, it does not invent new ones.
- Determinism: resolution sorts chains by `(redis_key, kind, ts)` before folding; no
  wall-clock reads, no dict-iteration-order dependence, no RNG. Replay test serializes
  journal + policy and asserts byte-identical output.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] Reader-gate Redis error path: fault-injection test asserts CLOSED (empty sheet +
  logged warning), never silent unscoped fallback. This is the load-bearing inversion of
  `_resolve_tag_keys`' cooperative degrade (`:1605`).
- [ ] Chain-walk fault tolerance: corrupt/missing annotation target resolves to
  "unresolved contradiction" flag, never a crash — test with a dangling `target`.
- [ ] If no other `except` blocks are added in scope, state so in the build PR.

### Empty/Invalid Input Handling
- [ ] Empty candidate set → empty `BeliefSheet` (not an error).
- [ ] `policy=None` → library defaults (documented `Defaults` values); unknown policy keys
  → ignored with warning, never crash.
- [ ] `reader=None` → gate denies all with a clear error (fail closed), tested.

### Error State Rendering
- [ ] `BeliefSheet` carries a `warnings` list (gate failures, unresolved chains,
  staleness-unavailable when no DecayingSortedField) — test asserts warnings render
  alongside surviving claims rather than being swallowed.

## Test Impact

New module plus two narrowly-scoped extensions; existing behavior contracts pin the rest:

- [ ] `tests/test_context_assembler*.py` — UPDATE only if the per-record refactor changes
  the ratio: the sibling delegates so `_staleness_ratio` output must be bit-identical;
  any failure here is a regression, not an expected update.
- [ ] Tag-scoping / validity-gating tests — no changes expected: bare `assemble()`
  keeps cooperative degrade; fail-closed lives only in the resolver path.
- [ ] New `tests/test_view_resolver.py` (create): retraction drop, supersession collapse +
  handle traceability, disjunct-pair surfacing, determinism replay, pre-truncation
  back-fill to `max_items`, fault-injection fail-closed, per-entry staleness with no
  extra Redis round-trip (assert via call-count spy), `assemble()`-unchanged guard.

No existing tests affected beyond the above — changes are additive (new recipe) plus a
behavior-preserving refactor (staleness sibling).

## Rabbit Holes

- Expressing annotation resolution as another `fuse()` arm or `composite_score` boost —
  already dropped in recon (RRF ranks; it has no removal semantics). Do not revisit.
- Extending `assemble()` with more opt-in flags instead of wrapping — the flag matrix
  (`assess_quality`, `emit_trace`, `as_of`, `exclude_keys`, ...) is exactly why the issue
  mandates a wrapper; a fifth flag re-creates the combinatorics problem.
- LLM-based contradiction resolution on the hot path — escalation only for flagged
  unresolved chains; the deterministic fold handles everything else.
- Waiting on M5 (#564) for class collapse — the per-record degradation path is the plan,
  M5 input is structural. Do not block.
- General entailment / "same claim" judging in the resolver — that is M5's job (LLM judge
  + convention book). M6 consumes its output ids.

## Risks

### Risk 1: Touching the stability-surfaced `context_assembler.py`
**Impact:** Any behavioral drift in `assemble()` breaks the bit-for-bit contracts other
recipes and tests rely on.
**Mitigation:** Both extensions are additive (new hook point + new sibling function);
existing tests pin the ratio output and bare-`assemble()` shape. Build PR must show those
suites green with no updates.

### Risk 2: M5 lands mid-build with a different disjunction shape
**Impact:** The M5-optional path assumes structural `class_id`/disjunction ids that may
not match M5's actual schema.
**Mitigation:** Consume structurally via duck-typing (`getattr(entry, "class_id", None)`)
with per-record fallback; no import of M5 types. If M5's schema differs, only the
collapse branch stays dormant — nothing breaks.

### Risk 3: Gate-rejection starvation (back-fill never reaches `max_items`)
**Impact:** A restrictive reader with a small candidate pool yields thin sheets.
**Mitigation:** Capped back-fill (`max_backfill_pulls` in policy, default 1 extra bounded
pull); residual shortfall is reported in `BeliefSheet.warnings`, never hidden.

### Risk 4: Chain-walk cost on large annotation histories
**Impact:** `annotations_for` per candidate could add round-trips linear in sheet size.
**Mitigation:** Membership stays on the V0 pushdown path (no chain walk for it); chains
are fetched only for display/replay handles on the already-truncated top-K. Bound and
document the per-resolve call budget in the build.

## Race Conditions

### Race 1: Annotation lands between retrieval and chain resolution
**Location:** `view_resolver.py` resolve path (retrieval → chain fold)
**Trigger:** a concurrent `supersede`/`retract` closes a candidate's validity interval
after the V0-gated pull but before `annotations_for` runs
**Data prerequisite:** chain read must postdate the membership decision for consistency
**State prerequisite:** none beyond the journal's own atomicity (M1/V0 own write safety)
**Mitigation:** resolution re-checks V0 membership (`validity__current`) for the top-K
after folding chains; entries closed in the window are dropped with a warning. Replay
remains deterministic because replay pins a journal snapshot.

No other concurrency in scope — resolution is synchronous and single-threaded; the
resolver performs no writes, so it cannot race itself.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #564] M5 equivalence classes, typed contradiction rules, convention
  book, LLM judge — consumed structurally, built in the M5 lane.
- [SEPARATE-SLUG #560] Journal write path (append/confirm/supersede/retract mechanics,
  vocabulary extension) — M1 shipped; M6 is read-only over it.

What this plan does not touch is not deferred work: bare `assemble()` keeps its
cooperative tag-scoping default (the fail-closed inversion lives only in the resolver
path — see Solution), and flagged unresolved chains are surfaced with handles for a
downstream escalation caller (M8 / assistant loop) that this module does not implement.
Both boundaries are named here as scope clarification, not promises.

## Update System

No update system changes required — pure library addition, no new dependencies, no
config files, no migrations. Downstream adopters opt in by wrapping their assembler.

## Update System

placeholder

## Agent Integration

No agent integration required — this is a library-level recipe. The resolver is consumed
in-process by the assistant loop / M8 holdout gate (the issue names M8's injection point
as the host), not via a tool/MCP surface. No `tools/` wrapping, no bridge changes.

## Documentation

### Feature Documentation
- [ ] Create `docs/features/belief-sheet-view.md` describing the resolver, policy dict,
  reader gate semantics (fail-closed), and replay procedure
- [ ] Add entry to `docs/features/README.md` index table

### External Documentation Site
- [ ] Verify docs build passes (`mkdocs build --strict` or the repo's docs gate)

### Inline Documentation
- [ ] Docstrings on `BeliefSheetResolver`, the pure resolution function, and both
  `context_assembler.py` extensions (including corrected line refs)
- [ ] Code comments on the pre-truncation gate placement (why not post-filter) and the
  determinism sort key

## Success Criteria

Mapped 1:1 to the issue's Acceptance Criteria:

- [ ] A retracted entry never appears in the belief sheet; a superseded entry is replaced
  by its winner with the chain traceable via provenance handle
- [ ] Disjunct pairs (M5) surface together as explicit uncertainty (structural-id path;
  per-record fallback verified without M5 present)
- [ ] Resolution is deterministic: same journal + same policy dict → byte-identical
  belief sheet (replay test)
- [ ] Reader gate runs pre-truncation, back-fills to `max_items`, and fails closed on
  error (fault-injection test)
- [ ] Each claim carries per-entry staleness; no second Redis round-trip vs current
  `assess_quality=True` cost (call-count spy test)
- [ ] Existing `assemble()` behavior unchanged when the resolver is not used (existing
  suites green, no updates)
- [ ] Tests at `tests/test_view_resolver.py`; docs page under `docs/features/`
- [ ] New `Defaults` constants registered in `tests/benchmarks/test_defaults_sync.py`
- [ ] Tests pass; documentation updated

## Team Orchestration

Single-builder lane (Medium appetite, one new module). The lead orchestrates; the lead
does not build directly.

### Team Members

- **Builder (view-resolver)**
  - Name: view-resolver-builder
  - Role: implement `recipes/view_resolver.py`, both `context_assembler.py` extensions,
    `Defaults` constants, full test coverage, docs page
  - Agent Type: builder
  - Resume: true

- **Validator (view-resolver)**
  - Name: view-resolver-validator
  - Role: verify all Success Criteria incl. determinism replay, fail-closed
    fault-injection, no-extra-round-trip spy, and `assemble()`-unchanged guard
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Per-record staleness sibling + Defaults constants
- **Task ID**: build-staleness
- **Depends On**: none
- **Validates**: existing staleness/quality tests green unmodified; new unit test for the
  per-record mapping; `tests/benchmarks/test_defaults_sync.py` passes
- **Informed By**: spike-1 (single-pass exposure, zero new I/O)
- **Assigned To**: view-resolver-builder
- **Agent Type**: builder
- **Parallel**: true
- Add per-record staleness sibling next to `_staleness_ratio`
  (`src/popoto/recipes/context_assembler.py:953`); reimplement the ratio as a delegate
  so output is bit-identical
- Pin policy numerics in `Defaults` (`popoto.fields.constants`): staleness threshold,
  gate over-fetch multiplier, max back-fill pulls; register each in
  `tests/benchmarks/test_defaults_sync.py`
- Use `get_REDIS_DB()` for any new Redis access (never a `POPOTO_REDIS_DB` import)

### 2. Reader-gate hook point (pre-truncation, fail-closed)
- **Task ID**: build-gate
- **Depends On**: none
- **Validates**: tag-scoping and validity-gating suites green unmodified;
  `tests/test_view_resolver.py::test_gate_*` (create)
- **Informed By**: spike-2 (headroom reuse, capped single back-fill)
- **Assigned To**: view-resolver-builder
- **Agent Type**: builder
- **Parallel**: true
- Add the gate hook between candidate merge and the `max_items` cut (`:1973`):
  `gate(record_key, reader) -> allow/deny`, errors → deny + log
- Implement over-fetch/back-fill to `max_items` with the capped extra pull
- Bare `assemble()` path keeps cooperative degrade — fail-closed only on the resolver path

### 3. BeliefSheetResolver + pure resolution function
- **Task ID**: build-resolver
- **Depends On**: build-staleness, build-gate
- **Validates**: `tests/test_view_resolver.py` full suite (create)
- **Assigned To**: view-resolver-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/recipes/view_resolver.py`: `BeliefSheetResolver(inner)` wrapping
  `ContextAssembler`; pure `resolve_entries(entries, chains, policy)` — drop retracted,
  collapse superseded to winners with handles, count confirmations, pair structural
  disjuncts, flag unresolved contradictions, attach per-record staleness, emit
  `BeliefSheet` with `warnings`
- M5-optional: duck-typed `class_id`/disjunction ids, per-record fallback without M5
- Deterministic fold: sort chains by `(redis_key, kind, ts)`; no clock reads, no RNG
- Cover: retraction drop, supersession collapse + traceability, disjunct surfacing,
  determinism replay (byte-identical), fault-injection fail-closed, no-extra-round-trip
  spy, `reader=None` deny, `policy=None` defaults, empty-set empty-sheet

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: build-resolver
- **Assigned To**: view-resolver-builder
- **Agent Type**: documentarian
- **Parallel**: false
- Create `docs/features/belief-sheet-view.md`; index-table entry; docs build passes

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: view-resolver-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all Verification commands; verify every Success Criterion; confirm
  `assemble()`-unchanged guard and standing rules (`get_REDIS_DB()`, Defaults sync);
  generate final report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New suite passes | `env -u REDIS_URL POPOTO_TEST_DB=7 PYTHONPATH=<worktree>/src python /Users/valorengels/src/popoto/.venv/bin/python -m pytest tests/test_view_resolver.py -q` | exit code 0 |
| Assembler suites unchanged | `env -u REDIS_URL POPOTO_TEST_DB=7 PYTHONPATH=<worktree>/src python /Users/valorengels/src/popoto/.venv/bin/python -m pytest tests/ -q -k "context_assembl or validity or provenance or tag_scop or defaults_sync"` | exit code 0 |
| Lint clean | `ruff check src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| No stale-snapshot import | `grep -rn "from popoto.redis_db import POPOTO_REDIS_DB\|from .redis_db import POPOTO_REDIS_DB\|from ..redis_db import POPOTO_REDIS_DB" src/popoto/recipes/view_resolver.py src/popoto/recipes/context_assembler.py \| grep -v "function-local" ; test $? -eq 1` | exit code 0 |
| Bare assemble unchanged | `grep -c "cooperative, not a security boundary" src/popoto/recipes/context_assembler.py` | output > 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| | | | | |

---

## Open Questions

1. **Reader/purpose vocabulary**: is the reader triple `{agent_id, purpose, tags}` the
   right shape, or does the supervisor want purpose as a free string vs a frozen enum?
   (Default assumed: small frozen purpose enum in `Defaults`, extensible like
   `JOURNAL_KINDS`.)
2. **Policy-dict precedence default**: "prefer recent / self-stated / multiply-confirmed"
   — which wins when they disagree (e.g. recent inference vs older self-stated claim)?
   (Default assumed: self-stated > multiply-confirmed > recent, per M5's provenance
   precedence, with per-type overrides left to M5.)
3. **Unresolved-contradiction flag shape**: bare handle list for downstream LLM
   escalation, or a richer struct (competing handles + staleness + confirmation counts)?
   (Default assumed: richer struct — costs nothing and the escalator needs the context.)
