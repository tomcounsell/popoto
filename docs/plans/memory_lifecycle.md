---
status: Ready
type: feature
appetite: Large
owner: Valor
created: 2026-05-19
tracking: https://github.com/tomcounsell/popoto/issues/396
last_comment_id:
---

# Memory Lifecycle: Working / Episodic / Semantic Consolidation + Decay + Auto-Forget

## Freshness Check

Verified 2026-05-22 against commit `273b491` (current `main`).

| Item | Status | Detail |
|------|--------|--------|
| `AccessTrackerMixin` | Unchanged | Imports and interface confirmed. `access_count` / `last_accessed` properties present. |
| `DecayingSortedField` | Unchanged | Power-law decay Lua script intact. `base_score_field` param confirmed. |
| `ConfidenceField` | Unchanged | `get_confidence()` / `update_confidence()` interface confirmed. |
| `SubconsciousMemory` | Unchanged | Composition pattern confirmed; recipe composes alongside, not replaces. |
| Benchmark harness (#394) | **Merged** `273b491` | LongMemEval-S + LoCoMo adapters shipped. Measurement gate satisfied. |
| Issue #395 (hybrid retrieval) | Open | Not yet implemented; plan exists. No impact on this plan. |
| No commits since issue filed | Confirmed | No changes to `src/popoto/fields/` or `src/popoto/recipes/` since 2026-05-17. |

**Disposition: Unchanged.** All premises hold. Proceed with plan as written.

**Critical correction identified during freshness check:** The Tiering Design example shows `tier = UniqueKeyField(default="episodic")` — this is wrong. `UniqueKeyField` enforces per-value uniqueness, meaning only one record could have `tier="episodic"` at a time. The correct field type is `KeyField`, which indexes by value and enables `Memory.query.filter(tier="episodic")` with many records sharing a tier. Fixed in the Tiering Design section below.

## Research

**Search queries used:**
1. "memory consolidation episodic semantic working tier agent LLM 2025 2026 implementation patterns"
2. "agentmemory LoCoMo benchmark memory lifecycle decay auto-forget consolidation implementation"

**Key findings:**

- **agentmemory (rohitg00)** implements a 4-tier consolidation + decay + auto-forget pipeline achieving 95.2% R@5 on LoCoMo vs. 68.5% for mem0. Their benchmark advantage is attributed to keeping long-term index growth sub-linear by compressing raw observations into semantic memories and pruning. Popoto's approach is comparable but cleaner — composing existing field primitives rather than new storage layers. Source: [github.com/rohitg00/agentmemory](https://github.com/rohitg00/agentmemory)

- **FadeMem (arxiv 2601.18642)** implements differential decay across a dual-layer hierarchy with adaptive exponential decay modulated by semantic relevance, access frequency, and temporal patterns. Confirms our approach: access frequency + importance scoring as the primary promotion/forget signal is the well-validated pattern.

- **SuperLocalMemory V3.3** shows lifecycle-aware quantization (Active→32-bit → Archive→2-bit) for embedding compression. Out of scope for v1 but confirms the tier-as-partition pattern is the right abstraction level for Popoto.

- **Position paper (arxiv 2502.06975)** argues that episodic→semantic consolidation — converting past events into compact, reusable representations — is the key mechanism for multi-session agent improvement. Validates the two-tier (episodic + semantic) starting point over a working-memory third tier.

**How findings inform the plan:** The agentmemory R@5 benchmark result on LoCoMo gives us a concrete target (95.2%) for the measurement gate in success criteria. The FadeMem decay-rate approach confirms the `DecayingSortedField` power-law decay is the appropriate mechanism. The position paper validates the two-tier decision.

No relevant findings changed the technical approach. Proceeding with the plan as scoped.

## Problem

Every Popoto memory record sits at a single tier for its entire lifetime. The primitives for
decay, importance, and access tracking exist — `DecayingSortedField`, `CyclicDecayField`,
`ConfidenceField`, `AccessTrackerMixin` — but no policy layer orchestrates them into a
working → episodic → semantic → forgotten lifecycle.

Without consolidation, every memory competes equally in retrieval regardless of age or
usefulness. Without auto-forget, low-value items accumulate indefinitely, growing index
sizes and degrading retrieval precision over multi-session workloads. The agentmemory repo's
benchmark advantage on LoCoMo (long multi-session dialogues) is partly attributable to
exactly this layer.

**Desired outcome:** A `MemoryLifecycle` recipe that:
1. Classifies new memories into a starting tier (`working` or `episodic`).
2. **Consolidates** items up tiers: episodic → semantic on promotion criteria.
3. **Decays** items continuously via existing primitives (not replaced — composed).
4. **Auto-forgets** items below a configurable importance floor with low recent access.
5. Ships as an **opt-in recipe** — does not force existing `SubconsciousMemory` users to
   migrate. No breaking changes to existing field types.

## Definitions

| Term | Definition |
|------|-----------|
| Working memory | Very recent items (seconds to minutes); high access likelihood; narrow capacity |
| Episodic memory | Specific events with temporal context; medium-term retention |
| Semantic memory | Consolidated facts, decontextualized from original event; long-term |
| Consolidation | Moving an item from a lower tier to a higher one when promotion criteria met |
| Auto-forget | Policy-driven hard deletion of items below importance floor + recency floor |
| Tier | A partition value on the model (`"working"`, `"episodic"`, `"semantic"`) |
| `tick()` | Idempotent method that runs one lifecycle pass: promote, decay, forget |

## Prior Art

| Primitive | Role in lifecycle |
|-----------|------------------|
| `DecayingSortedField` | Continuous score decay — drives importance-over-time signal |
| `CyclicDecayField` | Push-path proactive surfacing — feeds into consolidation trigger |
| `ConfidenceField` | Confidence score — part of promotion and forget thresholds |
| `AccessTrackerMixin` | Access count + last_accessed — primary reinforcement signal |
| `SubconsciousMemory` | End-to-end memory extraction; lifecycle composes alongside it |
| `ContextAssembler` | Retrieval layer — must support tier-scoped queries |

No existing policy layer orchestrates these primitives. This is greenfield composition work.

## Tiering Design

### Tier as a partition field

Each memory record carries a `tier` value. Implementation: a plain string field on the
model that Popoto indexes as an exact-match partition:

```python
class Memory(Model):
    key = AutoKeyField()
    tier = KeyField(default="episodic")  # KeyField enables filter(tier="episodic") across many records
    content = ContentField()
    relevance = DecayingSortedField(...)
    confidence = ConfidenceField()
    # ... other fields
```

Tier-scoped queries use Popoto's existing filter mechanism:
```python
Memory.query.filter(tier="semantic").composite_score(...)
```

**Why `KeyField` not `UniqueKeyField`:** `UniqueKeyField` enforces that no two records share the same value for that field — semantically wrong for a tier. `KeyField` maintains a secondary Redis Set index per-value, enabling `filter(tier=...)` to return all records with that tier in O(1) via set intersection. This is the same mechanism used for any multi-valued partition in Popoto (e.g., `agent_id = KeyField()`).

**Why not `StringField`:** Plain `StringField` has no secondary index and cannot be used in `filter()`. `KeyField` is required for tier-scoped queries.

This is purely a data field — no schema changes to Redis key structure needed.

### Two-tier vs. three-tier

**Decision: start with two tiers (episodic + semantic).** Working memory as a distinct tier
adds complexity for uncertain benefit at typical Popoto workload sizes. New memories enter
at `"episodic"` by default. The `"working"` tier can be added later if benchmarks show it
earns its keep.

Working-memory behavior is approximated by the existing `CyclicDecayField` rapid-decay
mechanism — items created recently have high cyclic decay scores; they surface proactively
via the push path. No separate tier needed.

## Promotion Criteria

Promotion from `episodic` → `semantic` requires ALL conditions:

1. `access_count >= PROMOTION_ACCESS_COUNT` (default: 3)
2. `confidence >= PROMOTION_CONFIDENCE_THRESHOLD` (default: 0.6)
3. `age_seconds >= PROMOTION_MIN_AGE_SECONDS` (default: 300 — 5 minutes)

These are tuning constants (per `feedback_magic_numbers.md`), not user config. They live in
`tests/benchmarks/` as sweep parameters tunable against #394 harness on LoCoMo.

Promotion is **non-reversible** in v1. Once semantic, always semantic (items decay and are
eventually forgotten, but do not demote).

## Decay (Existing Primitives)

`MemoryLifecycle` does not re-implement decay — it **composes** existing primitives:
- `DecayingSortedField` handles continuous score decay via its existing mechanisms.
- `CyclicDecayField` handles time-to-surface decay for push path.
- `ConfidenceField` scores are updated by existing hooks on access.

`tick()` does not touch decay mechanics — they are self-maintaining via field hooks.

## Auto-Forget Criteria

Forget (hard delete) when ALL conditions met:

1. `importance_score < FORGET_IMPORTANCE_FLOOR` (default: 0.1)
2. `last_accessed_seconds_ago > FORGET_IDLE_SECONDS` (default: 86400 — 24 hours)
3. `tier != "semantic"` (semantic memories are protected from auto-forget by default)

Forget is a **hard delete** (not tombstone) for index hygiene. Tombstoning is documented
as an alternative but not implemented — callers needing audit trails should disable
auto-forget and handle retention themselves.

Semantic memories are protected by default because they represent consolidated, reinforced
knowledge. A future option `forget_semantic=True` can override.

## `MemoryLifecycle` Recipe

### Class interface

```python
class MemoryLifecycle:
    """Policy layer orchestrating memory tier transitions and auto-forget.

    Composes existing Popoto decay primitives — does not replace them.

    Usage:
        lifecycle = MemoryLifecycle(
            model_class=Memory,
            importance_field="relevance",   # DecayingSortedField name
        )
        lifecycle.tick()          # Run one lifecycle pass
        lifecycle.tag_new(record) # Set tier on newly created memory
    """

    def __init__(
        self,
        model_class,
        importance_field,
        tier_field="tier",
        should_promote=None,   # Optional[Callable[[record], Optional[str]]]
        should_forget=None,    # Optional[Callable[[record], bool]]
        partition_filters=None,
    ):
```

### `tag_new(record, tier="episodic")`

Sets the tier field on a newly created memory. Called by the application after `record.save()`.
Does not auto-detect; the caller determines the starting tier (defaults to `"episodic"`).

### `tick()`

Single idempotent lifecycle pass. Sequence:

1. **Scan episodic tier** — fetch all episodic records (paginated, `TICK_BATCH_SIZE` at a time).
2. **Promote candidates** — for each record passing promotion criteria, update `tier = "semantic"` and save.
3. **Forget candidates** — for each record (across all non-semantic tiers) passing forget criteria, call `record.delete()`.
4. **Log summary** — number promoted, number forgotten, duration.

`tick()` is safe to run concurrently: promotion and deletion are idempotent at the record
level. Worst case: two concurrent ticks both promote the same record (second write is a
no-op) or both try to delete the same record (second delete is a no-op).

### `assess(record)` → `LifecycleState`

Returns current lifecycle state of a record:
```python
@dataclass
class LifecycleState:
    tier: str
    access_count: int
    last_accessed: Optional[datetime]
    importance_score: float
    promotion_eligible: bool
    forget_eligible: bool
```

### Default policy callables

```python
def _default_should_promote(record, lifecycle) -> Optional[str]:
    """Return new tier string or None."""
    if record.tier != "episodic":
        return None
    access_count = _get_access_count(record)
    confidence = _get_confidence(record)
    age = _get_age_seconds(record)
    if (access_count >= lifecycle.PROMOTION_ACCESS_COUNT
            and confidence >= lifecycle.PROMOTION_CONFIDENCE_THRESHOLD
            and age >= lifecycle.PROMOTION_MIN_AGE_SECONDS):
        return "semantic"
    return None

def _default_should_forget(record, lifecycle) -> bool:
    if record.tier == "semantic":
        return False
    importance = _get_importance(record, lifecycle.importance_field)
    idle = _get_idle_seconds(record)
    return (importance < lifecycle.FORGET_IMPORTANCE_FLOOR
            and idle > lifecycle.FORGET_IDLE_SECONDS)
```

Custom callables can be injected at construction time for application-specific policies.

## Magic Number Constants

All in `src/popoto/recipes/memory_lifecycle.py` as class-level constants:

```python
PROMOTION_ACCESS_COUNT = 3         # accesses before episodic→semantic eligible
PROMOTION_CONFIDENCE_THRESHOLD = 0.6
PROMOTION_MIN_AGE_SECONDS = 300    # 5 min — prevent promoting on burst access
FORGET_IMPORTANCE_FLOOR = 0.1
FORGET_IDLE_SECONDS = 86400        # 24 hours
TICK_BATCH_SIZE = 100              # records per tick scan page
```

These feed into the `tests/benchmarks/` parametric sweep grid (per `feedback_magic_numbers.md`).
They are experimental tuning constants, not user-configurable init params.

## Architectural Impact

- **New file:** `src/popoto/recipes/memory_lifecycle.py`
- **New test file:** `tests/test_memory_lifecycle.py`
- **No changes** to any existing field type, recipe, or model
- **No new Redis-module dependencies** — all state lives in Popoto fields
- **`tier` field** is a plain Popoto field added by the application developer on their model;
  `MemoryLifecycle` reads it by name (configurable `tier_field` param)
- **Relationship with `SubconsciousMemory`:** `MemoryLifecycle` is composable alongside
  `SubconsciousMemory`, not a replacement. Application calls both:
  ```python
  # After SubconsciousMemory extracts a memory:
  record = subconscious.extract(...)
  lifecycle.tag_new(record)

  # Periodically:
  lifecycle.tick()
  ```

## Appetite

**Size:** Large (new recipe, test suite, benchmark tuning, docs)

**Team:** Solo dev

**Interactions:**
- PM check-in: 1 — validate two-tier vs. three-tier decision before build
- Review rounds: 1-2

## Prerequisites

| Requirement | Check | Purpose |
|-------------|-------|---------|
| `AccessTrackerMixin` ships | `python -c "from popoto.fields.access_tracker import AccessTrackerMixin"` | Access count signal |
| `DecayingSortedField` ships | `python -c "from popoto.fields.decaying_sorted_field import DecayingSortedField"` | Importance signal |
| `ConfidenceField` ships | `python -c "from popoto.fields.confidence_field import ConfidenceField"` | Confidence signal |
| Benchmark harness (#394) | `pytest tests/benchmarks/ -v` | Measurement gate |

This plan is third in the sequence: #394 → default hybrid retrieval (#395) → this.
It depends on #394 so lifecycle impact can be measured on LoCoMo.

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] `tick()` on empty corpus → no-op, returns summary with 0 promoted, 0 forgotten
- [ ] `tick()` when `importance_field` not on model → raises `ConfigurationError` at init
- [ ] `tick()` when `tier_field` not on model → raises `ConfigurationError` at init
- [ ] Record delete fails during forget → logs warning, continues to next record
- [ ] Record save fails during promotion → logs warning, continues
- [ ] `tag_new()` on already-tiered record → overwrites (idempotent)
- [ ] Custom `should_forget` raises → logs warning, skips that record

### Concurrency Safety

- [ ] Two concurrent `tick()` calls: promotion idempotent (second write no-op)
- [ ] Two concurrent `tick()` calls: forget idempotent (second delete no-op — record absent)
- [ ] `tick()` while `tag_new()` is called → no conflict (different record sets)

### Edge Cases

- [ ] Model with no episodic records → tick promotes 0
- [ ] Model where every record is semantic → tick forgets 0 (protected)
- [ ] `TICK_BATCH_SIZE` boundary — 101 records paginate correctly

## Test Impact

**New tests (`tests/test_memory_lifecycle.py`):**

- `test_tag_new_sets_tier` — new record gets `"episodic"` tier
- `test_tick_promotes_eligible_episodic` — record with sufficient access/confidence/age gets `tier = "semantic"`
- `test_tick_does_not_promote_ineligible` — record below threshold stays episodic
- `test_tick_forgets_low_importance_idle` — record below floor + idle gets deleted
- `test_tick_does_not_forget_semantic` — semantic records never deleted by default tick
- `test_tick_is_idempotent` — running tick twice has same result as once
- `test_custom_should_promote` — custom callable overrides default logic
- `test_custom_should_forget` — custom callable overrides default logic
- `test_assess_returns_correct_state` — `assess()` returns correct `LifecycleState`
- `test_empty_corpus_tick` — tick on empty corpus is a no-op
- `test_tick_batch_pagination` — 200 records paginate correctly across two batches

**Existing tests:** No changes expected. Run `pytest tests/ -x -q` as regression suite.

## Rabbit Holes

- **Working memory as a third tier:** Deferred. Approximate working-memory behavior via
  `CyclicDecayField` rapid decay; add the tier explicitly only if benchmarks show benefit.
- **Automatic tier detection from creation time:** Do not automatically infer tier from
  timestamp. The caller (`SubconsciousMemory`, application) sets it. Explicit is better.
- **Demotion (semantic → episodic):** Rarely useful and complex to define correctly. Out of
  scope for v1.
- **Tombstoning / soft delete:** Hard delete only in v1. Document the pattern for callers
  who need audit trails (disable auto-forget, implement their own retention).
- **Background scheduler / cron:** `tick()` is exposed; orchestration is the caller's
  problem. A cron wrapper is a follow-up (not part of this plan).
- **Replacing decay primitives:** This plan composes existing primitives — do not replace or
  modify `DecayingSortedField`, `CyclicDecayField`, or `ConfidenceField`.

## Risks

### Risk 1: Importance signal incompatibility
**Impact:** `MemoryLifecycle` must read an importance score from the model. If the
`importance_field` is not a `DecayingSortedField` (or doesn't expose a comparable interface),
the forget policy has nothing to read from.
**Mitigation:** Require `importance_field` to be a `SortedFieldMixin` subclass (same
protocol as `DecayingSortedField`). Validate at `__init__` time.

### Risk 2: `AccessTrackerMixin` dependency
**Impact:** `access_count` and `last_accessed` are on `AccessTrackerMixin`, not all models.
If the model doesn't use `AccessTrackerMixin`, the promotion and forget policies degrade.
**Mitigation:** Detect `AccessTrackerMixin` at init (like `ContextAssembler` detects
fields). If absent, `access_count` defaults to 0 and `last_accessed` to creation time.
Log a warning recommending `AccessTrackerMixin` for best lifecycle results.

### Risk 3: Tick latency on large corpora
**Impact:** Scanning all episodic records is O(N). At 100K records, a single tick may
take seconds and block retrieval.
**Mitigation:** `TICK_BATCH_SIZE = 100` with paginated iteration. `tick()` processes one
page per call; callers can loop if needed. Document ceiling (~50K episodic records) before
performance becomes a concern.

## No-Gos (Out of Scope)

- Replacing any existing field type (lifecycle composes, not replaces)
- New embedding models or retrieval algorithms (handled by #395)
- A generic cron/task scheduler — `tick()` is exposed; orchestration is the caller's problem
- Working memory as a third tier (deferred)
- Demotion (semantic → episodic)
- Tombstoning / soft-delete semantics

## Step by Step Tasks

### 1. Audit existing primitives
- **Task ID**: audit-primitives
- **Depends On**: none
- **Parallel**: true
- Read `access_tracker.py` — confirm `access_count` and `last_accessed` interface
- Read `decaying_sorted_field.py` — confirm how to read current score
- Read `confidence_field.py` — confirm `get_confidence()` interface
- Read `subconscious_memory.py` — confirm composition pattern

### 2. Define `tier` field integration
- **Task ID**: define-tier-field
- **Depends On**: audit-primitives
- **Parallel**: false
- Decide field type for `tier` partition (confirm SortedField or plain string field supports filter query)
- Write usage example in docstring showing model with `tier` field

### 3. Implement `MemoryLifecycle` recipe
- **Task ID**: build-lifecycle-recipe
- **Depends On**: define-tier-field
- **Parallel**: false
- Create `src/popoto/recipes/memory_lifecycle.py`
- Implement `__init__` with capability detection and `ConfigurationError` guards
- Implement `tag_new()`, `tick()`, `assess()`
- Implement `_default_should_promote()` and `_default_should_forget()` as module functions
- Implement `LifecycleState` dataclass
- Add all magic number constants as class attributes
- Export from `recipes/__init__.py`

### 4. Write tests
- **Task ID**: write-tests
- **Depends On**: build-lifecycle-recipe
- **Parallel**: false
- Create `tests/test_memory_lifecycle.py`
- Implement all test cases listed above
- Run `pytest tests/test_memory_lifecycle.py -v`

### 5. Add constants to benchmark sweep grid
- **Task ID**: benchmark-constants
- **Depends On**: write-tests
- **Parallel**: false
- Add `PROMOTION_ACCESS_COUNT`, `PROMOTION_CONFIDENCE_THRESHOLD`, `PROMOTION_MIN_AGE_SECONDS`,
  `FORGET_IMPORTANCE_FLOOR`, `FORGET_IDLE_SECONDS` to `tests/benchmarks/` sweep config
- Run sweep on LoCoMo from #394 harness; commit benchmark report

### 6. Documentation
- **Task ID**: docs
- **Depends On**: benchmark-constants
- **Parallel**: false
- Add `MemoryLifecycle` to `docs/recipes.md` with full usage example
- Update `docs/features/agent-memory.md` referencing lifecycle layer
- Add `CHANGELOG` entry
- Verify `mkdocs build` passes

### 7. Final validation
- **Task ID**: validate-all
- **Depends On**: docs
- **Parallel**: false
- Run `pytest tests/ -x -q`
- Run `black --check src/ tests/`
- Confirm all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Lifecycle tests | `pytest tests/test_memory_lifecycle.py -v` | exit 0 |
| Full suite | `pytest tests/ -x -q` | exit 0 |
| Import | `python -c "from popoto.recipes.memory_lifecycle import MemoryLifecycle"` | exit 0 |
| Format | `black --check src/ tests/` | exit 0 |
| Docs build | `mkdocs build` | exit 0 |
| Benchmark report | `ls docs/benchmarks/` | report present |

## Success Criteria

- [ ] `MemoryLifecycle` recipe in `src/popoto/recipes/memory_lifecycle.py`
- [ ] `tick()` is idempotent and safe under concurrent runs
- [ ] Promotion, decay composition, and auto-forget covered by tests
- [ ] Magic-number thresholds added to `tests/benchmarks/` override grid and tuned with sweep
- [ ] Benchmark report showing LoCoMo and LongMemEval-S deltas vs. pre-lifecycle baseline
      (reference target: agentmemory achieves 95.2% R@5 on LoCoMo with lifecycle — our
       baseline without lifecycle is what #394 will establish)
- [ ] No Redis-module dependencies (Valkey-compat)
- [ ] No required migration for existing `SubconsciousMemory` users
- [ ] `docs/recipes.md` updated with full example
- [ ] `CHANGELOG` entry added

## Open Questions

All major design questions resolved during plan finalization:

1. **Two tiers vs. three:** ✅ Two tiers (episodic + semantic). Working memory approximated
   by `CyclicDecayField` rapid decay. Revisit in v2 only if LoCoMo benchmarks show benefit.

2. **Promotion trigger: on-access vs. tick only:** ✅ Tick-only. Simpler, more predictable,
   no per-read overhead. Application can call `tick()` as frequently as needed.

3. **Forget = hard delete vs. tombstone:** ✅ Hard delete for index hygiene. Tombstone
   pattern documented in code comments for callers needing audit trails.

4. **Importance scoring ownership:** ✅ Read from designated `importance_field`
   (`DecayingSortedField`). `MemoryLifecycle` does not re-compute importance.

5. **`tier` field type:** ✅ `KeyField` (not `UniqueKeyField`, not `StringField`). See
   Tiering Design section for the rationale.

**No open questions requiring human input before build.**
