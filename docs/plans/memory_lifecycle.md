---
status: Planning
type: feature
appetite: Large
owner: Valor
created: 2026-05-18
tracking: https://github.com/tomcounsell/popoto/issues/396
last_comment_id:
---

# Memory Lifecycle: Working / Episodic / Semantic Consolidation + Auto-Forget

## Problem

Popoto ships rich primitives — `DecayingSortedField`, `AccessTrackerMixin`,
`CyclicDecayField`, `ConfidenceField`, plus importance scoring — but no
**policy layer** that composes them into a multi-tier memory lifecycle.
Every memory record sits at a single tier for its entire lifetime;
importance/decay drive ranking but not transitions; nothing forgets.

**Current behavior:**
- A `Memory` record is created at time T, decays from then on, and lives
  forever unless an external caller deletes it.
- Low-value memories accumulate indefinitely, growing index sizes and
  diluting retrieval precision (especially over multi-session dialogues
  like LoCoMo).
- `SubconsciousMemory` (`src/popoto/recipes/subconscious_memory.py`)
  injects/extracts memories but does not consolidate or forget them.

**Desired outcome:**
1. A documented, opt-in `MemoryLifecycle` recipe that classifies new
   memories into a starting tier, promotes them upward when usage
   warrants, and forgets them when usage collapses.
2. All thresholds exposed as constants on `fields/constants.py::Defaults`
   and registered in `tests/benchmarks/overrides.py` so the existing
   sweep harness can tune them.
3. LoCoMo + LongMemEval-S R@5 / MRR / latency deltas committed alongside
   the recipe so the value is *measured*, not asserted.
4. Zero migration burden — `SubconsciousMemory` users keep working;
   lifecycle is purely additive.

## Freshness Check

**Baseline commit:** `10cf580` (Merge PR #391, v1.6.3)
**Issue filed at:** 2026-05-17T23:11:31Z (yesterday)
**Disposition:** Unchanged (with one Overlap noted)

**File:line references re-verified:**
- `src/popoto/fields/` (DecayingSortedField, AccessTracker,
  CyclicDecayField, ConfidenceField) — all present and current.
- `src/popoto/recipes/subconscious_memory.py` — present (284 lines).
- `tests/benchmarks/overrides.py` — present, registry pattern as
  described.
- `src/popoto/fields/constants.py:52` — `Defaults.DECAY_RATE = 0.1`
  (best from sweep 2026-04-20). `INITIAL_CONFIDENCE = 0.5`
  (empirically inert).

**Cited sibling issues/PRs re-checked:**
- #394 (Benchmark harness: LongMemEval-S + LoCoMo adapters) — **OPEN**.
  Hard prerequisite: lifecycle deltas cannot be measured without it.
- #395 (Default ContextAssembler to hybrid retrieval) — **OPEN**.
  Soft prerequisite: retrieval-over-consolidated-tiers benefits from
  hybrid, but lifecycle works without it.

**Commits on main since issue was filed:** None touching `recipes/`,
`fields/`, or `tests/benchmarks/`. Worktree HEAD is the same as main HEAD.

**Active plans in `docs/plans/` overlapping this area:**
- `hybrid_retrieval.md` (In Progress) — Overlap is read-side only; this
  plan adds a write-side lifecycle layer that's orthogonal to retrieval
  fusion. No coordination required beyond ordering (#395 lands first to
  set retrieval defaults the lifecycle benchmarks can rely on).

**Notes:** The issue body lacks an explicit `## Recon Summary` heading
but its Context / Problem / Definitions / Solution sketch / Open
questions sections collectively serve the same function. Treating these
as the recon evidence.

## Prior Art

- **PR #390 / plan `trajectory_memory_recipe.md`** — Most recent recipe
  added to Popoto (TrajectoryMemory, fingerprint-keyed procedural
  memory). Confirms the "recipe composes existing primitives" pattern
  and the plan→critique→revision→build cadence is healthy.
- **`recipes/subconscious_memory.py`** — Extant memory recipe; this plan
  composes alongside (not replaces) it.
- **`recipes/context_assembler.py`** — Read-side orchestrator the
  lifecycle implicitly relies on (consolidated tiers should be
  retrievable via the existing assembler with a `tier` filter cue).
- **`fields/access_tracker.py`** — Provides `access_count` and
  `last_accessed` via `confirm_access()`. Promotion criteria read these.
- **`fields/decaying_sorted_field.py`** — Provides time-decayed scoring.
  The lifecycle treats decay as a continuous force; promotion/forget
  are discrete events on top.
- **`fields/cyclic_decay_field.py`** — Adds cyclic resonance + homeostatic
  pressure. Useful for the semantic tier where periodic re-surfacing
  matters (e.g., quarterly facts).
- **`fields/confidence_field.py`** — Bayesian confidence with
  partition_by support. The semantic tier should require both high
  access reinforcement *and* high confidence — confidence is the
  natural lock against premature consolidation of noisy episodics.
- **`tests/benchmarks/overrides.py`** — Triple-patch override registry
  with `VALID_RANGES`. New lifecycle constants slot in here directly
  with no harness changes beyond entries.

No prior issues or PRs implemented a memory lifecycle layer.

## Research

**Queries used:**
- "agentmemory consolidation auto-forget tier policy"
- "memory consolidation working episodic semantic LLM agents"
- "LoCoMo benchmark long-context multi-session memory"

**Key findings:**

- **agentmemory's lifecycle** (cited in the issue) implements a 4-tier
  scheme (short / working / long / archival) with periodic
  consolidation passes and an importance + access-count gate for
  promotion. Their public ablation attributes ~10–15% of LoCoMo
  performance to this layer. Source: rohitg00/agentmemory README and
  benchmark tables.
  *Informs:* (a) start with a 3-tier scheme (working / episodic /
  semantic) — 4 tiers is more configuration than the project's typical
  workload justifies, and the "archival" tier in agentmemory is closer
  to a soft delete (which our auto-forget already does). (b) Promotion
  must require both reinforcement *and* importance, not just one.

- **LoCoMo benchmark** is multi-session by construction — turns
  reference earlier sessions, so consolidation effects show up where
  single-session benchmarks (LongMemEval-S) cannot see them. Source:
  LoCoMo paper / huggingface card.
  *Informs:* LoCoMo is the primary measurement gate. LongMemEval-S is
  the secondary "did we regress single-session retrieval?" gate.

- **Memory consolidation in cognitive psychology** (Wikipedia,
  Squire et al.) — Consolidation is *not* immediate; it requires
  repeated reactivation. Translates directly to "promotion requires
  multiple confirmed accesses over time," not "promotion on creation."
  *Informs:* default promotion threshold should be access_count ≥ 3
  AND age ≥ minimum_episodic_age, not a single-shot trigger.

- **Cognitive economy / forgetting curves** (Ebbinghaus) — Forgetting
  is exponential and unused items should drop fastest.
  *Informs:* the existing `DecayingSortedField` already implements this
  shape — auto-forget is just "decayed score below floor *and* not
  accessed within window." We do not introduce new decay math.

## Spike Results

### spike-1: Can `AccessTrackerMixin.access_count` + `last_accessed` be read cheaply during a tick scan?
- **Assumption:** "Per-record access metadata is reachable in O(1) per
  record from a tick-scan loop without N+1 amplification."
- **Method:** code-read (`src/popoto/fields/access_tracker.py`)
- **Finding:** Yes. Meta is stored as a single Redis hash per record
  (`$AT:{ClassName}:meta:{redis_key}`) with `access_count` and
  `last_accessed` fields. HMGET-per-record is fine for the scan sizes
  we'll start with; a pipelined batch HMGET is the obvious next step
  if a scan becomes hot. **No Lua needed for v1.**
- **Confidence:** high
- **Impact on plan:** Promotion check is a per-record dict read, not a
  full model rehydrate. Locks in the "scan in batches with pipeline"
  technical approach.

### spike-2: Is `tier` better as a partition key or as a sorted-set index?
- **Assumption:** "A `tier` `KeyField` partition cleanly scopes queries
  without needing a separate sorted set per tier."
- **Method:** code-read (`src/popoto/models/db_key.py`,
  `confidence_field.py:partition_by`)
- **Finding:** KeyField partitioning is the established pattern (e.g.
  `partition_by='project'` on ConfidenceField). Composite redis keys
  (`ClassName:agent_id:tier:key`) make per-tier queries
  `ClassName:agent_id:working:*`-cheap, and transitions become
  delete-old-key + write-new-key (atomic via a small Lua wrapper).
- **Confidence:** high
- **Impact on plan:** `tier` is implemented as a model `KeyField` with
  a fixed enum-like value set, not a separate sorted set. Transitions
  use a small `MOVE_TIER_LUA` to keep them atomic.

### spike-3: Does `Defaults.DECAY_RATE` already get patched by the override harness?
- **Assumption:** "New lifecycle constants follow the same triple-patch
  pattern and need only registry entries — no harness rework."
- **Method:** code-read (`tests/benchmarks/overrides.py`, lines 80–157)
- **Finding:** Yes. `MODULE_CONSTANTS` and `CLASS_ATTR_CONSTANTS` plus
  `VALID_RANGES` are the only touch points. New constants get one
  entry in each. The triple-patch (Defaults / module-level alias /
  class-cached attr) is already documented as the canonical pattern.
- **Confidence:** high
- **Impact on plan:** All lifecycle thresholds live on `Defaults.*` and
  register through the existing two dicts + VALID_RANGES. Zero harness
  changes.

### spike-4: Does the existing benchmark harness run LoCoMo today?
- **Assumption:** "We can measure LoCoMo deltas at build time."
- **Method:** code-read (`tests/benchmarks/scenarios/`,
  `tests/benchmarks/fixtures/`, grep for `locomo`)
- **Finding:** **No.** No LoCoMo scenario, fixture, or grep hit
  exists. The scenarios that exist (`coding_assistant`,
  `factual_recall`, `multi_step_reasoning`, `research_agent`,
  `support_agent`, `temporal_scheduling`) are the family-factory
  Tier-1/2 sweep set. LoCoMo + LongMemEval-S adapters are #394's job
  and #394 is still **OPEN**.
- **Confidence:** high
- **Impact on plan:** This plan **depends on #394** landing first.
  Build will gate the measurement step on #394; if #394 is not done at
  build time, the build still ships the recipe + unit tests +
  override-registry entries but **defers the benchmark commit step**
  to a follow-up issue. This is called out as a hard dependency in
  Risks.

## Data Flow

### Save path (new record)

1. Caller invokes `lifecycle.record(content=..., importance=...)`
   (or `SubconsciousMemory.extract_memories()` if the option
   `lifecycle=...` is wired through).
2. `MemoryLifecycle.classify_new(record)` returns a starting tier
   (default: `"episodic"`; `"working"` only if explicitly enabled).
3. Model instance is created with `tier=<classified>` and saved.
4. Existing `on_save` hooks fire: decay timestamp written,
   AccessTracker meta initialized, ConfidenceField initialized.

### Read path (unchanged)

1. `ContextAssembler.assemble()` runs as today; an optional
   `tier_filter` query cue lets callers scope to
   `{"semantic", "episodic"}` (defaults to all non-forgotten tiers).
2. Reads automatically stage in `AccessTracker`. Caller's existing
   `confirm_access()` promotes the staged reads (this is the existing
   contract — not changed).

### Tick path (new)

1. Caller invokes `MemoryLifecycle.tick(agent_id=...)` from their own
   scheduler (cron, Celery beat, asyncio task — orchestration is
   caller's problem; the issue explicitly says so).
2. Tick scans records in `tier="working"` and `tier="episodic"` for
   the given `agent_id` partition in batches (default batch = 100).
3. For each record:
   - Read `access_count`, `last_accessed`, `importance`, current decay
     score, and confidence (if present) via pipelined HMGET.
   - Evaluate `should_promote(record)`. If true → atomic tier-move
     via `MOVE_TIER_LUA` (delete old key, write new key, update
     indexes). Confidence and access metadata travel with the record.
   - Evaluate `should_forget(record)`. If true → hard delete.
     (Tombstone tier is a `[SEPARATE-SLUG]` candidate — see No-Gos.)
4. Tick returns a `LifecycleTickReport`: counts of promoted, demoted
   (rare), forgotten, scanned, errored.
5. Tick is **idempotent**: a second call within the same minute
   produces no transitions (a per-record `last_lifecycle_tick`
   timestamp suppresses repeated evaluation under a configurable
   re-check interval).

### Forget path

`should_forget` default rule:
`decayed_score < FORGET_SCORE_FLOOR AND (now - last_accessed) > FORGET_AGE_MIN AND access_count < FORGET_ACCESS_MAX AND importance < FORGET_IMPORTANCE_CEIL`

A record passing the rule has its model row deleted; the existing
`on_delete` hooks on each Field handle index cleanup (df decrement
for BM25, sorted-set ZREM for DecayingSortedField, etc.). This is
the established Popoto delete contract — no new cleanup code.

## Architectural Impact

- **New dependencies**: None. No new Python packages, no Redis modules.
- **New files**:
  - `src/popoto/recipes/memory_lifecycle.py` — the recipe.
  - `tests/recipes/test_memory_lifecycle.py` — unit tests.
  - `tests/benchmarks/scenarios/memory_lifecycle.py` — sweep scenario
    that varies the new constants (added in a follow-up if #394 not
    ready; see Risks).
- **Modified files**:
  - `src/popoto/fields/constants.py` — adds lifecycle constants to
    `Defaults` (see Tunable constants below).
  - `tests/benchmarks/overrides.py` — registers new constants in
    `MODULE_CONSTANTS` (or `CLASS_ATTR_CONSTANTS`) + `VALID_RANGES`.
  - `src/popoto/recipes/subconscious_memory.py` — *optional* opt-in
    `lifecycle: MemoryLifecycle | None = None` constructor arg that
    calls `lifecycle.classify_new()` in `extract_memories()`. Default
    `None` preserves current behavior.
  - `docs/recipes.md` — adds a "Memory lifecycle" section.
- **Interface changes**: Additive only. No existing method signature
  changes. Existing `SubconsciousMemory(...)` calls keep working.
- **Coupling**: Lifecycle reads AccessTracker meta and the model's
  importance/confidence fields by *name* (configurable). The recipe
  is loosely coupled — works on any Popoto model that has the named
  fields, mirroring `SubconsciousMemory`'s pattern.
- **Data ownership**: Lifecycle owns the `tier` field on the user's
  model (a `KeyField`) and a per-record `last_lifecycle_tick`
  timestamp (a regular `DateTimeField`). It does NOT own importance,
  decay, confidence, or access — those remain on the existing fields.
- **Reversibility**: Pure addition. Removing the recipe leaves user
  models with one extra `tier` partition key and a tick timestamp
  field. Both can be ignored or stripped trivially. No schema lock-in.

## Appetite

**Size:** Large

**Team:** Solo dev, code reviewer, plan-critique war room

**Interactions:**
- PM check-ins: 2-3 (default tuning, benchmark gate decision, scope of
  the optional `SubconsciousMemory` wiring)
- Review rounds: 2+ (recipe code review, benchmark numbers review)

Large because (a) the recipe is non-trivial (lifecycle policies +
atomic tier moves + tick scheduler + idempotency), (b) it must be
*measured* not just shipped, and (c) it depends on #394 which is not
yet done.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis or Valkey reachable | `python -c "import os; from src.popoto.redis_db import POPOTO_REDIS_DB; POPOTO_REDIS_DB.ping()"` | Lifecycle reads/writes Redis structures |
| `tests/benchmarks/` harness importable | `python -c "import tests.benchmarks.overrides; import tests.benchmarks.sweep"` | New constants must register |
| #394 LoCoMo + LongMemEval-S adapters merged | `python -c "from tests.benchmarks.scenarios import locomo, longmemeval_s"` | Required to measure deltas — see Risks for fallback |

## Solution

### Key Elements

- **`MemoryLifecycle` recipe**: composes existing primitives into a
  tiered lifecycle. Configurable policies as callables (with
  documented defaults). Lives in `src/popoto/recipes/memory_lifecycle.py`.
- **`tier` partition key**: a `KeyField` on the user's memory model
  with values `"working" | "episodic" | "semantic"`. Working is
  opt-in via a constructor flag; default scheme is two-tier
  (episodic + semantic) per the spike-1/cognitive-psych research.
- **Policies (callables, all defaults shipped)**:
  - `classify_new(record) -> tier` — starting tier on save.
  - `should_promote(record, meta) -> Optional[tier]` — drives
    episodic → semantic (and working → episodic if working is enabled).
  - `should_forget(record, meta) -> bool` — drives auto-forget.
- **`tick(agent_id=None) -> LifecycleTickReport`**: idempotent
  background pass. Caller schedules it. If `agent_id` is None, scans
  all partitions (caller's responsibility to size).
- **Atomic tier moves**: a small Lua script
  (`MOVE_TIER_LUA`) deletes the old composite key and writes the new
  one in one round trip, so partial moves can't leave a record in two
  tiers at once.
- **Tunable constants** (all on `Defaults`, all in `overrides.py`):
  - `LIFECYCLE_PROMOTE_ACCESS_MIN` (default 3)
  - `LIFECYCLE_PROMOTE_AGE_MIN_SECONDS` (default 3600 = 1 hour)
  - `LIFECYCLE_PROMOTE_IMPORTANCE_MIN` (default 0.5)
  - `LIFECYCLE_PROMOTE_CONFIDENCE_MIN` (default 0.6 — optional, only
    consulted when the user's model has a ConfidenceField)
  - `LIFECYCLE_FORGET_SCORE_FLOOR` (default 0.05)
  - `LIFECYCLE_FORGET_AGE_MIN_SECONDS` (default 604800 = 7 days)
  - `LIFECYCLE_FORGET_ACCESS_MAX` (default 1 — never accessed after
    creation)
  - `LIFECYCLE_FORGET_IMPORTANCE_CEIL` (default 0.3)
  - `LIFECYCLE_TICK_RECHECK_INTERVAL_SECONDS` (default 60)
  - `LIFECYCLE_TICK_BATCH_SIZE` (default 100)

  Every constant gets a `VALID_RANGES` entry so sweep boundary
  checking works.

### Flow

User wires `MemoryLifecycle` into their stack → user calls
`record()` on save (or wires `SubconsciousMemory(lifecycle=...)`) →
records start at `episodic` (default) → reads stage in
AccessTracker → user calls `confirm_access()` as they do today →
**user's own scheduler calls `lifecycle.tick()` periodically** →
records meeting promotion criteria move to `semantic`; records
meeting forget criteria are deleted → `ContextAssembler` retrieves
across tiers, optionally filtered.

### Technical Approach

- **Recipe class shape** mirrors `SubconsciousMemory`: framework-agnostic,
  configurable field names (`tier_field="tier"`,
  `importance_field="importance"`, `access_tracker_kind="default"`),
  callable policy hooks with shipped defaults.
- **Policies are pure functions** of `(record_meta_dict, now,
  Defaults.*)`. They take a dict (not a hydrated model) so the tick
  loop never round-trips through Popoto's full model reconstruction
  cost. Tested in isolation by passing meta dicts directly.
- **Atomic tier move** via `MOVE_TIER_LUA`. Inputs: old composite key,
  new composite key, model hash payload. Operation: `RENAME` is not
  safe because the surrounding index keys are tied to the composite —
  instead, the Lua script (a) reads the model hash, (b) writes the
  new composite, (c) deletes the old, all atomically. The recipe
  then re-runs the new tier's `on_save` hooks at the Python layer
  for any field that needs index re-registration. Confidence and
  AccessTracker meta are keyed off `db_key.redis_key` — moving the
  key requires the recipe to also move those companion hashes; the
  Lua script handles all three keys in one EVAL.
- **Idempotency** via `last_lifecycle_tick` timestamp on each record.
  Tick reads the timestamp; if `now - last_tick < RECHECK_INTERVAL`,
  the record is skipped. After processing (whether or not a
  transition happened), the timestamp is updated.
- **Tick scan strategy**: per-tier-per-agent SCAN with a MATCH
  pattern (`ClassName:{agent_id}:working:*`). SCAN, not KEYS — large
  partitions must not block the event loop.
- **Configuration knobs not in the constants list** (these stay on the
  recipe constructor, not on `Defaults`, because they're user-facing
  toggles, not numeric tuning):
  - `enable_working_tier: bool = False`
  - `forget_mode: Literal["delete"] = "delete"` (future-proofs for
    "tombstone" without enabling it now)
- **No Redis modules**: every Lua script uses base Redis commands +
  `cmsgpack` (which both Redis and Valkey support natively).
- **`SubconsciousMemory` integration**: optional `lifecycle=` kwarg.
  If provided, `extract_memories()` calls `lifecycle.classify_new()`
  before saving. If `None`, behavior is identical to today.

## Failure Path Test Strategy

### Exception Handling Coverage
- The tick loop wraps each record's evaluation in `try/except` and
  logs at WARNING with `record_key`, `tier`, `error`. Tests assert
  the warning is emitted with the right record key when policy
  callables raise.
- A test injects a broken `should_promote` (raises ValueError) and
  asserts the tick continues to the next record without aborting,
  and that the broken record is reported in `LifecycleTickReport.errors`.
- A test verifies that if the `MOVE_TIER_LUA` script fails mid-tick
  (e.g., Redis disconnect simulated via patched client), the record
  remains in its original tier (no half-move state).

### Empty/Invalid Input Handling
- `tick(agent_id="")` — must raise immediately, not silently scan all
  partitions (silent broad scans are a footgun).
- `tick()` with no records in any tier — returns a zero-count report,
  not None.
- `classify_new(record)` with `importance=None` — falls back to
  default tier; documented and tested.
- `should_forget` called on a record with no `access_count`
  (AccessTracker never confirmed) — treats access_count as 0, which
  triggers forget if other conditions hold. Tested explicitly.

### Error State Rendering
- `LifecycleTickReport` includes per-record error tuples so callers
  can log or alert; a test asserts errors appear in the report and
  are not silently swallowed.

## Test Impact

- `tests/recipes/test_memory_lifecycle.py` — CREATE: unit tests for
  classify_new, should_promote, should_forget, tick idempotency,
  atomic tier moves, error path coverage.
- `tests/recipes/test_subconscious_memory.py` (if it exists) — UPDATE:
  add a test confirming `SubconsciousMemory(lifecycle=...)` calls
  `classify_new`. If the file does not exist, no change.
- `tests/benchmarks/test_overrides_reach.py` — UPDATE: assert each
  new `LIFECYCLE_*` constant is reached by an override (the harness
  has an existing convention for this assertion).
- `tests/benchmarks/scenarios/memory_lifecycle.py` — CREATE:
  family-factory-style scenario that varies the new constants. Only
  added if #394 LoCoMo adapter is available; otherwise deferred.
- No existing test deletions or replacements — this is purely additive.

## Rabbit Holes

- **Building a generic scheduler / cron / task queue.** The issue is
  explicit: `tick()` is the API; orchestration is the caller's
  problem. Do not add Celery, RQ, asyncio loops, or background
  threads. Document the contract: "call tick() periodically; you
  decide how often."
- **Replacing existing decay primitives** with a unified
  "lifecycle decay engine." Tempting because the math could be more
  cohesive — but `DecayingSortedField` / `CyclicDecayField` are
  used outside the lifecycle context. Compose, don't replace.
- **A 4-tier scheme matching agentmemory exactly.** Their 4th tier
  ("archival") is essentially "tombstoned-not-deleted." Our default
  is `forget_mode="delete"`. Re-introducing archival to mimic
  agentmemory is a separate scope decision — keep this plan at three.
- **LLM-driven promotion criteria.** Tempting because LLMs could
  judge "is this fact worth consolidating?" — but adds an inference
  cost per record per tick. Defer. Defaults are deterministic.
- **Confidence-driven demotion.** "If confidence drops, demote
  semantic → episodic" sounds elegant but inverts the project's
  current confidence-as-strengthening-signal model and adds tier
  thrash. Demotion stays out for v1; only working → episodic via age
  ages.

## Risks

### Risk 1: #394 not merged at build time → cannot measure LoCoMo delta
**Impact:** The issue's "benchmark report committed showing LoCoMo
and LongMemEval-S deltas" acceptance criterion cannot be satisfied
at merge time.
**Mitigation:** Build in two phases. **Phase A** ships the recipe,
unit tests, override-registry entries, and `SubconsciousMemory`
integration — no benchmark dependency. **Phase B** (after #394
merges) adds `tests/benchmarks/scenarios/memory_lifecycle.py`, runs
the sweep, and commits the report. The acceptance criterion that
references benchmark numbers is explicitly conditioned on #394 in
the plan's Success Criteria. If we ship Phase A only and #394 still
lags, the missing measurement is filed as a follow-up
`[SEPARATE-SLUG]` (see No-Gos).

### Risk 2: Tick scan amplifies Redis load on large partitions
**Impact:** A naive scan of millions of records per agent on every
tick saturates Redis.
**Mitigation:** SCAN with `MATCH` + `COUNT=100` (configurable),
pipelined HMGET per batch, and per-record `last_lifecycle_tick`
suppression. Document the per-1k-records latency in the recipe doc
and surface a counter in `LifecycleTickReport.scanned`. Add a
benchmark scenario that stresses scan latency at 10k / 100k records.

### Risk 3: Atomic tier moves break index integrity if Lua script is wrong
**Impact:** A record could end up indexed under two tiers, or lose
its access/confidence metadata.
**Mitigation:** The `MOVE_TIER_LUA` script is the highest-risk new
code. It gets a dedicated test class with: (a) crash simulation
between sub-operations (impossible with EVAL atomicity, but tested
that pre/post state is consistent), (b) idempotent re-runs, (c)
verification that companion AccessTracker and ConfidenceField
metadata follow the move. Code review for Lua specifically requested
in the plan critique step.

### Risk 4: Default thresholds are wrong and harm retrieval quality
**Impact:** Lifecycle ships, gets enabled by users, and *degrades*
their R@5 vs no lifecycle.
**Mitigation:** All thresholds are sweepable via the existing
harness. The Phase-B sweep on LoCoMo + LongMemEval-S is exactly
this gate. Defaults are revised based on the sweep before merging
the recipe doc that recommends defaults. If no setting beats the
no-lifecycle baseline on LoCoMo R@5 by ≥ 3%, the recipe ships as
"experimental, off by default" rather than "recommended."

### Risk 5: Concurrent ticks racing each other
**Impact:** Two ticks running simultaneously could both decide to
promote the same record, double-applying transitions or worse.
**Mitigation:** See Race Conditions section. Per-record
`last_lifecycle_tick` + atomic Lua move + SETNX-based agent-level
tick lock with a TTL (default 5 minutes) so a crashed tick auto-
releases.

## Race Conditions

### Race 1: Concurrent ticks evaluating the same record
**Location:** `MemoryLifecycle.tick()` scan loop, per-record evaluate
+ move block.
**Trigger:** Two schedulers (cron + ad-hoc) both call `tick()` at
the same instant for the same `agent_id`.
**Data prerequisite:** Both ticks see the same record at the same
tier with the same `last_lifecycle_tick` timestamp.
**State prerequisite:** Promotion criteria evaluate true for both.
**Mitigation:** (a) An agent-level SETNX tick lock
(`$LC:lock:{ClassName}:{agent_id}` with TTL =
`LIFECYCLE_TICK_RECHECK_INTERVAL_SECONDS * 2`) — second tick
returns a "skipped, lock held" report and does not scan. (b) The
`MOVE_TIER_LUA` is atomic, so even if the lock somehow fails, the
worst case is one move (the second sees the record already in the
new tier and no-ops).

### Race 2: A read updates `last_accessed` mid-promotion-evaluation
**Location:** Between `HMGET access_count, last_accessed` and the
promotion decision.
**Trigger:** User reads the record (via `Model.query.get`) at the
exact moment tick is evaluating it.
**Data prerequisite:** AccessTracker staging list is non-empty for
the record.
**State prerequisite:** The read pushes the access into a state that
would change the promotion decision (e.g., access_count crosses the
threshold).
**Mitigation:** Acceptable. Tick fetches meta at decision time;
either it sees the new access_count and promotes, or it sees the
old and waits for next tick. Either way, the record ends up in the
right state within one tick interval. Documented as eventually
consistent.

### Race 3: Forget evaluation racing a fresh save
**Location:** Tick `should_forget` evaluation vs concurrent
`Model.save()` for the same record.
**Trigger:** Caller saves an updated version of a record at the
moment tick decides to forget it.
**Data prerequisite:** Record exists and meets forget criteria;
caller's save races the tick.
**State prerequisite:** A `DEL` and a `HSET` of the same composite
key arrive at Redis nearly simultaneously.
**Mitigation:** Acceptable failure mode. Either the save wins (the
record survives this tick; next tick may forget if criteria still
hold), or the delete wins (caller sees their save vanish). The
window is microseconds and the contract is documented: "do not save
records you also expect to be forgettable on the same agent." Most
real workflows are append-then-tick-much-later, so the race is
near-zero in practice.

## No-Gos (Out of Scope)

- `[SEPARATE-SLUG #394]` LoCoMo + LongMemEval-S adapters themselves
  — required for Phase B but owned by #394.
- `[SEPARATE-SLUG #395]` Default ContextAssembler to hybrid retrieval
  — affects how the consolidated tiers are *queried* but is owned by
  #395.
- `[SEPARATE-SLUG TBD]` Tombstone tier / soft-forget mode — left as a
  follow-up issue, to be filed after this plan lands if real demand
  emerges. (Per the validator: this entry will be promoted to a real
  issue number before the plan is finalized, OR removed.)
- `[SEPARATE-SLUG TBD]` Cron / Celery / asyncio scheduler wrapper
  around `tick()` — explicitly out per the issue body. Same
  promote-or-remove note applies.
- `[SEPARATE-SLUG TBD]` LLM-judged promotion criteria — file as
  follow-up if/when default heuristics under-deliver after sweep
  tuning.

## Update System

No update-system changes required. This is a library feature; users
pull a new Popoto version and opt in. The deployment script in this
repo (`do-deploy` skill) handles the version bump unchanged.

## Agent Integration

No agent/MCP integration required. `MemoryLifecycle` is a Popoto
library recipe; the agent only interacts with memory through
existing MCP tools that read/write the underlying model.

## Documentation

### Feature Documentation
- [ ] Create `docs/recipes/memory_lifecycle.md` (or add a section to
  `docs/recipes.md`) covering: when to use, how to wire,
  configuration, defaults table, tuning guidance, the tick contract.
- [ ] Add entry to `docs/recipes.md` index.
- [ ] Update `docs/agent_memory_dx_quickstart.md` (if it covers the
  recipes list) to mention lifecycle as an opt-in.

### External Documentation Site
- [ ] Update mkdocs nav to include the new recipe page.
- [ ] Verify `mkdocs build` passes.

### Inline Documentation
- [ ] Module docstring on `memory_lifecycle.py` explaining the model.
- [ ] Docstrings on every public method, with default-values table.
- [ ] Comments on the `MOVE_TIER_LUA` script explaining each step.

## Success Criteria

**Phase A (recipe ships, regardless of #394 status):**
- [ ] `MemoryLifecycle` recipe exists in `src/popoto/recipes/memory_lifecycle.py`.
- [ ] Promotion, decay (existing primitives), and auto-forget wired and unit-tested.
- [ ] `tick()` is idempotent under concurrent calls (lock test passes).
- [ ] All `LIFECYCLE_*` constants on `Defaults`; registered in
  `tests/benchmarks/overrides.py` `MODULE_CONSTANTS` / `VALID_RANGES`.
- [ ] `tests/benchmarks/test_overrides_reach.py` passes including new constants.
- [ ] Atomic tier-move Lua script has dedicated tests covering pre/post
  consistency and metadata travel.
- [ ] No Redis-module dependencies (Valkey-compat verified via
  `grep -rin "BF\.\|CMS\." src/popoto/recipes/memory_lifecycle.py` returns nothing).
- [ ] No required migration: existing `SubconsciousMemory(...)` calls
  still pass their tests unchanged.
- [ ] Documentation page committed.
- [ ] All standard checks: tests pass, lint clean, format clean.

**Phase B (gated on #394 merging):**
- [ ] LoCoMo R@5/MRR/latency benchmark report committed showing
  delta vs pre-lifecycle baseline.
- [ ] LongMemEval-S R@5/MRR/latency benchmark report committed
  (regression gate — must not get worse than baseline).
- [ ] Sweep run over `LIFECYCLE_*` constants; chosen defaults
  documented with the sweep findings (mirroring the `DECAY_RATE = 0.1
  # best from sweep 2026-04-20` style in `constants.py`).
- [ ] If no setting beats baseline by ≥ 3% on LoCoMo R@5, recipe is
  documented as "experimental" with a clear caveat in the recipe doc.

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using
Task tools. Build tasks can run in parallel where their deps allow;
validators wait for their builder.

### Team Members

- **Builder (recipe)**
  - Name: `lifecycle-recipe-builder`
  - Role: Implement `MemoryLifecycle` recipe and the `MOVE_TIER_LUA` script.
  - Agent Type: builder
  - Resume: true

- **Builder (constants + overrides)**
  - Name: `lifecycle-constants-builder`
  - Role: Add `LIFECYCLE_*` constants to `Defaults`, register them in
    `overrides.py`, add `VALID_RANGES` entries.
  - Agent Type: builder
  - Resume: true

- **Builder (subconscious integration)**
  - Name: `subconscious-lifecycle-integrator`
  - Role: Add optional `lifecycle=` kwarg to `SubconsciousMemory`,
    wire `classify_new()` through `extract_memories()`.
  - Agent Type: builder
  - Resume: true

- **Test engineer (recipe tests)**
  - Name: `lifecycle-test-engineer`
  - Role: Unit tests for policies, tick idempotency, atomic moves,
    error paths.
  - Agent Type: test-engineer
  - Resume: true

- **Code reviewer (Lua + atomic moves)**
  - Name: `lua-reviewer`
  - Role: Review `MOVE_TIER_LUA` specifically — Valkey compat, atomicity,
    companion-key handling, no module usage.
  - Agent Type: code-reviewer
  - Resume: true

- **Documentarian**
  - Name: `lifecycle-documentarian`
  - Role: `docs/recipes/memory_lifecycle.md`, index update, mkdocs nav,
    inline docstrings.
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `lifecycle-validator`
  - Role: Run full test suite, lint, format, override-reach test,
    Valkey-module grep, mkdocs build.
  - Agent Type: validator
  - Resume: true

- **(Phase B only) Benchmark runner**
  - Name: `lifecycle-bench-runner`
  - Role: Author `scenarios/memory_lifecycle.py`, run sweep on LoCoMo
    + LongMemEval-S (post-#394), commit report, finalize defaults.
  - Agent Type: builder
  - Resume: true

## Step by Step Tasks

### 1. Add lifecycle constants
- **Task ID**: build-constants
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_overrides_reach.py`, `tests/benchmarks/test_defaults_sync.py`
- **Informed By**: spike-3 (override harness needs only registry entries)
- **Assigned To**: `lifecycle-constants-builder`
- **Agent Type**: builder
- **Parallel**: true
- Add `LIFECYCLE_PROMOTE_ACCESS_MIN`, `LIFECYCLE_PROMOTE_AGE_MIN_SECONDS`,
  `LIFECYCLE_PROMOTE_IMPORTANCE_MIN`, `LIFECYCLE_PROMOTE_CONFIDENCE_MIN`,
  `LIFECYCLE_FORGET_SCORE_FLOOR`, `LIFECYCLE_FORGET_AGE_MIN_SECONDS`,
  `LIFECYCLE_FORGET_ACCESS_MAX`, `LIFECYCLE_FORGET_IMPORTANCE_CEIL`,
  `LIFECYCLE_TICK_RECHECK_INTERVAL_SECONDS`, `LIFECYCLE_TICK_BATCH_SIZE`
  to `Defaults` in `src/popoto/fields/constants.py` with documented defaults.
- Register each in `MODULE_CONSTANTS` (with a module-level alias in
  `memory_lifecycle` module) and add a `VALID_RANGES` entry.
- Confirm `test_overrides_reach.py` discovers the new entries.

### 2. Build the recipe
- **Task ID**: build-recipe
- **Depends On**: build-constants
- **Validates**: `tests/recipes/test_memory_lifecycle.py` (create)
- **Informed By**: spike-1 (HMGET-per-record is fine), spike-2 (tier as KeyField partition)
- **Assigned To**: `lifecycle-recipe-builder`
- **Agent Type**: builder
- **Parallel**: false
- Create `src/popoto/recipes/memory_lifecycle.py` with `MemoryLifecycle`
  class, default policy callables, `tick()`, `LifecycleTickReport`,
  `MOVE_TIER_LUA`, and an agent-level tick lock.
- Use `KeyField`-style composite keys for the tier partition. Use SCAN
  (not KEYS) for the tick scan.
- All tunable thresholds read from `Defaults.*` at runtime (not
  cached at class-body time — see overrides.py guidance).

### 3. Write recipe tests
- **Task ID**: test-recipe
- **Depends On**: build-recipe
- **Validates**: `tests/recipes/test_memory_lifecycle.py`
- **Assigned To**: `lifecycle-test-engineer`
- **Agent Type**: test-engineer
- **Parallel**: false
- Unit tests for `classify_new`, `should_promote`, `should_forget` with
  fixture meta dicts.
- Integration tests against fakeredis or live Redis (mirror existing
  recipe tests): saves, ticks, transitions, idempotency.
- Concurrent-tick test using the lock.
- Atomic move tests asserting AccessTracker and Confidence metadata
  travel correctly.
- Error path tests: broken policy callable, redis disconnect mid-Lua.

### 4. Lua review
- **Task ID**: review-lua
- **Depends On**: build-recipe
- **Assigned To**: `lua-reviewer`
- **Agent Type**: code-reviewer
- **Parallel**: true (with test-recipe)
- Review `MOVE_TIER_LUA` for Valkey compat (no modules, only base
  commands + cmsgpack), atomicity, and companion-key handling.
- Confirm the agent-level tick lock can't deadlock.

### 5. Wire SubconsciousMemory integration
- **Task ID**: build-subconscious-integration
- **Depends On**: build-recipe
- **Validates**: existing `tests/recipes/test_subconscious_memory.py`
  (if present) — must remain green; one new test for the
  `lifecycle=...` path.
- **Assigned To**: `subconscious-lifecycle-integrator`
- **Agent Type**: builder
- **Parallel**: true (with test-recipe)
- Add optional `lifecycle: MemoryLifecycle | None = None` kwarg to
  `SubconsciousMemory.__init__`.
- In `extract_memories()`, if `lifecycle` is set, call
  `lifecycle.classify_new(record)` to pick the starting tier before
  save. If `None`, behave exactly as today.
- Add one regression test confirming `None` path is unchanged.

### 6. Documentation
- **Task ID**: document-recipe
- **Depends On**: build-recipe, build-subconscious-integration
- **Assigned To**: `lifecycle-documentarian`
- **Agent Type**: documentarian
- **Parallel**: true (with test-recipe)
- Create `docs/recipes/memory_lifecycle.md`.
- Update `docs/recipes.md` index.
- Update mkdocs nav.
- Add inline docstrings on the new class and Lua script.
- Cross-reference `agent_memory_dx_quickstart.md` if applicable.

### 7. Phase-A validation
- **Task ID**: validate-phase-a
- **Depends On**: build-constants, build-recipe, test-recipe, review-lua,
  build-subconscious-integration, document-recipe
- **Assigned To**: `lifecycle-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest` (full suite).
- Run `ruff check` and `ruff format --check`.
- Run `mkdocs build` (warnings-as-errors if the project sets that).
- Run `grep -rin "BF\.\|CMS\." src/popoto/recipes/memory_lifecycle.py` —
  must return zero matches.
- Run `tests/benchmarks/test_overrides_reach.py` explicitly.
- Report pass/fail and surface any drift in the override sweep coverage.

### 8. (Phase B, gated on #394) Benchmark sweep + defaults finalization
- **Task ID**: bench-lifecycle
- **Depends On**: validate-phase-a, AND #394 merged
- **Assigned To**: `lifecycle-bench-runner`
- **Agent Type**: builder
- **Parallel**: false
- Author `tests/benchmarks/scenarios/memory_lifecycle.py`.
- Run sweep on LoCoMo and LongMemEval-S.
- Commit the results report alongside the baseline (no-lifecycle).
- Update `Defaults.LIFECYCLE_*` to the best-from-sweep values with
  the `# best from sweep YYYY-MM-DD` comment style used elsewhere
  in `constants.py`.
- If no setting beats baseline by ≥ 3% on LoCoMo R@5, update the
  recipe doc to mark the feature "experimental, off by default."

### 9. Final validation
- **Task ID**: validate-all
- **Depends On**: validate-phase-a (and bench-lifecycle if Phase B ran)
- **Assigned To**: `lifecycle-validator`
- **Agent Type**: validator
- **Parallel**: false
- Re-run full suite + lint + format.
- Confirm all Success Criteria checkboxes are tickable.
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| No Redis modules used | `grep -rEn "BF\\.\|CMS\\.\|TOPK\\.\|TDIGEST\\." src/popoto/recipes/memory_lifecycle.py` | exit code 1 (no match) |
| Override reach passes | `pytest tests/benchmarks/test_overrides_reach.py -x -q` | exit code 0 |
| Defaults sync passes | `pytest tests/benchmarks/test_defaults_sync.py -x -q` | exit code 0 |
| Recipe tests pass | `pytest tests/recipes/test_memory_lifecycle.py -x -q` | exit code 0 |
| mkdocs builds | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Two-tier vs three-tier default.** Default plan ships two tiers
   (episodic + semantic) with a constructor flag to enable the
   working tier. Cognitive-psych research says working memory's value
   is in *very* short-lived state; in a Redis-backed system that
   window may not be worth a tier of its own. **Confirm: ship
   two-tier default + working opt-in, or three-tier default?**
2. **Promotion as `on_access` hook vs only during `tick()`?** Plan
   currently routes promotion through `tick()` only — simpler,
   testable, no per-read latency. An `on_access` hook would react
   faster but adds Lua-on-every-read cost. **Confirm: tick-only for
   v1?**
3. **Importance source.** Recipe reads importance from a configurable
   field name on the user's model (default `"importance"`). It does
   NOT compute or update importance itself. **Confirm this division
   of responsibility?**
4. **`SubconsciousMemory` opt-in shape.** Plan adds an optional
   `lifecycle=` kwarg. Alternative: ship `MemoryLifecycle` as a
   completely separate recipe with no `SubconsciousMemory` touch
   at all. **Which is preferred — light integration or zero
   integration in v1?**
5. **Phase-B gating.** If #394 stalls, do we ship Phase A as a
   patch release (1.6.x) and Phase B later, or hold the whole plan
   until #394 lands?
6. **No-Gos with TBD slug numbers.** The validator may reject
   `[SEPARATE-SLUG TBD]` entries — should I file placeholder issues
   for tombstone-mode, scheduler-wrapper, and LLM-judged-promotion
   now, or remove those bullets entirely?
