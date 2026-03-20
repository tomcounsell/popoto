---
status: Draft
type: feature
appetite: Large
owner: Valor
created: 2026-03-20
tracking: https://github.com/tomcounsell/popoto/issues/232
last_comment_id:
---

# PolicyCache — Learned Action Selection from Crystallized Patterns

## Problem

AI agents approach recurring tasks from scratch every time. An agent that has successfully completed "deploy to staging" 10 times still treats the 11th encounter as novel — there is no mechanism to crystallize repeated successful patterns into reusable policies.

**Current behavior:**
The Popoto memory stack can track observations, confidence, predictions, and co-occurrences, but these remain individual record-level signals. There is no model pattern that composes them into state-action-outcome triples for reinforcement-learning-style action selection.

**Desired outcome:**
A `PolicyEntry` reference model pattern (not core ORM) that stores `state -> action -> expected_value` triples, built entirely from shipped Popoto primitives. When a StreamConsumer's compaction pipeline detects repeated successful patterns in the event stream, it crystallizes them into reusable PolicyEntry records that agents can query for action selection. A temporal pattern discovery handler detects cyclical patterns in event timestamps and adds discovered cycles to existing memories.

## Prior Art

- **PR #238** (StreamConsumer): Background processing framework. PolicyCache's crystallization and temporal discovery handlers run inside StreamConsumer.
- **PR #231** (PredictionLedgerMixin): Outcome tracking with auto-resolution. PolicyEntry uses predictions to track whether selected actions succeeded.
- **PR #222** (CompositeScoreQuery): Multi-factor retrieval. Agents query PolicyEntry via `composite_score()` for action selection.
- **PR #215** (ConfidenceField): Bayesian certainty. PolicyEntry confidence grows with successful outcomes.
- **PR #218** (CoOccurrenceField): Weighted associations. Related policies link together.
- **PR #199** (DecayingSortedField): Time-weighted scoring. PolicyEntry's expected_value decays without use.
- **PR #225** (ExistenceFilter): Bloom filter pre-check before expensive policy queries.
- All other shipped primitives (CyclicDecayField, AccessTrackerMixin, ObservationProtocol, WriteFilterMixin, EventStreamMixin).

No prior attempts at PolicyCache exist — this is greenfield.

## Data Flow

1. **Event production**: Application models with `EventStreamMixin` produce mutation events to Redis Streams on every save/delete
2. **Stream consumption**: A `StreamConsumer` reads events from the stream in batches
3. **Pattern detection (crystallization handler)**: The handler groups events by `(state_fingerprint, action_type)`, counts successes/failures, computes Wilson CI lower bound. When min_events threshold is met and Wilson CI lower bound > 0.6, creates a PolicyEntry
4. **Temporal pattern detection**: A separate handler buckets event timestamps by time-of-year/month/week, performs chi-squared test against uniform distribution. Significant clusters (p < 0.05) are added as `(period, amplitude, phase)` cycles to existing memories
5. **Policy query**: Agent retrieves top policies for current state via `CompositeScoreQuery` with decay, confidence, and co-occurrence weights
6. **Action execution**: Agent selects and executes an action based on policy
7. **Outcome observation**: `ObservationProtocol.on_context_used()` fires, updating Q-value, confidence, and prediction error
8. **Q-value update**: Temporal difference update via Lua script adjusts expected_value based on reward signal

## Architectural Impact

- **New files**: `src/popoto/recipes/__init__.py`, `src/popoto/recipes/policy_cache.py` — self-contained recipe module
- **No modified files**: This is a reference pattern that composes existing primitives. No changes to core ORM code.
- **No new dependencies**: Uses only existing Popoto primitives and core Redis commands + Lua scripts
- **Interface changes**: New recipe module importable from `popoto.recipes.policy_cache`. No changes to existing interfaces.
- **Coupling**: Loose — PolicyCache imports from existing Popoto fields/mixins. No reverse dependency.
- **Reversibility**: Fully reversible — remove the `recipes/` subpackage. No impact on existing functionality.
- **Deferred decision**: Final packaging (recipes subpackage vs examples dir vs docs-only) to be evaluated via follow-up issue after e2e testing reveals how real-life implementations and human teams actually use this pattern.

## Appetite

**Size:** Large

**Team:** Solo dev, PM

**Interactions:**
- PM check-ins: 2 (scope confirmation on crystallization logic, temporal discovery review)
- Review rounds: 1-2 (code review, integration test review)

This is the largest primitive — it composes all 11 prior shipped primitives and includes application-layer logic (crystallization handler, temporal discovery, Q-value updates). The reference pattern nature means less ORM rigor required, but the integration test matrix is substantial.

## Prerequisites

All 12 shipped primitives (Steps 1-10 of the roadmap) are prerequisites. All are shipped and merged.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| All primitives importable | `python -c "from popoto.fields import DecayingSortedField, CyclicDecayField, ConfidenceField, CoOccurrenceField; from popoto.fields.access_tracker import AccessTrackerMixin; from popoto.fields.observation_protocol import ObservationProtocol; from popoto.fields.write_filter import WriteFilterMixin; from popoto.fields.event_stream_mixin import EventStreamMixin; from popoto.fields.existence_filter import ExistenceFilter, FrequencySketch; from popoto.fields.prediction_ledger import PredictionLedgerMixin; from popoto.streams import StreamConsumer; print('OK')"` | All primitives available |
| Redis 5.0+ | `python -c "from popoto.redis_db import POPOTO_REDIS_DB; info = POPOTO_REDIS_DB.info('server'); print(info['redis_version'])"` | Redis Streams support |

## Solution

### Key Elements

- **PolicyEntry model**: Reference model composing DecayingSortedField, ConfidenceField, CoOccurrenceField, ExistenceFilter, and EventStreamMixin
- **Q-value TD update Lua script**: Atomic temporal difference update of expected_value
- **Crystallization handler**: StreamConsumer handler that detects repeated successful patterns and creates PolicyEntry records
- **Temporal pattern discovery handler**: StreamConsumer handler that clusters event timestamps and discovers cyclical patterns
- **State fingerprint utility**: Configurable fingerprint generation from state features
- **Pure-Python chi-squared approximation**: No scipy dependency for temporal discovery

### Flow

**Events accumulate in stream** -> **StreamConsumer reads batch** -> **Crystallization handler groups by (state, action)** -> **Wilson CI check** -> **PolicyEntry created** -> **Agent queries via CompositeScoreQuery** -> **Selects action** -> **Outcome observed** -> **Q-value updated** -> **Confidence adjusted**

**Parallel flow:** **Events accumulate** -> **Temporal discovery handler buckets timestamps** -> **Chi-squared test** -> **Significant cluster found** -> **Cycle added to existing memory** -> **Entrainment strengthens/weakens over time**

### Technical Approach

#### PolicyEntry Model

```python
class PolicyEntry(EventStreamMixin, AccessTrackerMixin, PredictionLedgerMixin, Model):
    entry_id = AutoKeyField()
    agent_id = KeyField()
    state_fingerprint = KeyField()
    state_features = Field()                     # JSON dict
    action_type = KeyField()
    action_spec = Field()                        # JSON dict
    expected_value = DecayingSortedField(
        partition_by="agent_id",
    )
    confidence = ConfidenceField(initial_confidence=0.5)
    related_policies = CoOccurrenceField(symmetric=True, max_edges=100)
    bloom = ExistenceFilter(
        error_rate=0.01,
        capacity=100_000,
        fingerprint_fn=lambda inst: inst.state_fingerprint,
    )

    _stream_name = "policy_mutations"
    _stream_partition_field = "agent_id"
    _pl_partition = "default"
```

Note: WriteFilterMixin is intentionally excluded — the crystallization handler IS the write gate (Wilson CI > 0.6 threshold). Having gating logic in two places makes debugging harder.

#### Q-value TD Update Lua Script

```lua
-- td_update.lua: Temporal difference Q-value update
-- KEYS[1] = PolicyEntry instance hash key
-- KEYS[2] = DecayingSortedField sorted set key
-- ARGV[1] = reward (float)
-- ARGV[2] = alpha (learning rate, default 0.1)
-- ARGV[3] = gamma (discount factor, default 0.95)
-- ARGV[4] = max_future_q (float)
-- ARGV[5] = member key for sorted set
--
-- Returns: td_error as string

local current_q = tonumber(redis.call('ZSCORE', KEYS[2], ARGV[5]) or '0')
local reward = tonumber(ARGV[1])
local alpha = tonumber(ARGV[2])
local gamma = tonumber(ARGV[3])
local max_future_q = tonumber(ARGV[4])

local td_error = reward + gamma * max_future_q - current_q
local new_q = current_q + alpha * td_error

redis.call('ZADD', KEYS[2], new_q, ARGV[5])
return tostring(td_error)
```

Stored as a string constant in the recipe module (`TD_UPDATE_LUA`), called via `POPOTO_REDIS_DB.eval()`. Same pattern as `RESOLVE_PREDICTION_LUA` in prediction_ledger.py.

#### State Fingerprint

```python
def compute_fingerprint(features: dict, include_fields: list = None,
                        include_timestamp: bool = False) -> str:
    """Generate a stable fingerprint from state features.

    Args:
        features: Dict of state features to fingerprint.
        include_fields: Optional list of field names to include.
            If None, all fields are included.
        include_timestamp: If True, includes current hour-bucket
            timestamp for time-unique fingerprints.

    Returns:
        str: SHA-256 truncated to 16 hex chars.
    """
```

Design notes:
- Usually all KeyFields are included in the fingerprint
- Timestamp fields are included when the fingerprint needs to be unique by time (e.g., "deploy to staging at 2pm" vs "deploy to staging at 3pm")
- `include_fields` allows per-model customization of which features matter
- Multiple fingerprints per object are supported by creating multiple PolicyEntry records with different `state_fingerprint` values derived from different `include_fields` configurations
- Applications can subclass or replace `compute_fingerprint` entirely for domain-specific hashing

#### Crystallization Handler

```python
async def crystallization_handler(entries):
    """StreamConsumer handler that detects repeated patterns and crystallizes PolicyEntry records.

    Groups events by (state_fingerprint, action_type), counts successes/failures,
    and creates PolicyEntry when evidence threshold is met.

    Magic numbers:
        MIN_EVENTS_FOR_CRYSTALLIZATION (default 3): Minimum events with same
            state+action before considering crystallization. Can be set as low
            as 1 for eager crystallization in high-confidence environments.
        WILSON_CI_THRESHOLD (default 0.6): Wilson confidence interval lower
            bound that must be exceeded for crystallization.
    """
```

Pattern counting state is derived from reading stream entries — groups events by `(state_fingerprint, action_type)`, counts how many had successful outcomes. Wilson CI lower bound is computed in Python (simple formula, no Lua needed for this):

```python
def wilson_ci_lower(successes, total, z=1.96):
    """Wilson score confidence interval lower bound.

    Args:
        successes: Number of successful outcomes.
        total: Total number of outcomes.
        z: Z-score for confidence level (1.96 = 95%).

    Returns:
        float: Lower bound of Wilson CI.
    """
    if total == 0:
        return 0.0
    p_hat = successes / total
    denominator = 1 + z**2 / total
    center = p_hat + z**2 / (2 * total)
    spread = z * (p_hat * (1 - p_hat) / total + z**2 / (4 * total**2)) ** 0.5
    return (center - spread) / denominator
```

#### Temporal Pattern Discovery Handler

```python
async def temporal_discovery_handler(entries):
    """StreamConsumer handler that discovers cyclical patterns from event timestamps.

    Buckets event timestamps by time-of-year, time-of-month, and day-of-week.
    Performs chi-squared test against uniform distribution. Significant clusters
    (p < 0.05) are added as (period, amplitude, phase) cycles to existing memories.
    """
```

Pure-Python chi-squared approximation (no scipy):

```python
def chi_squared_uniform(observed: list, expected_per_bucket: float) -> float:
    """Chi-squared statistic against uniform distribution.

    Returns the test statistic. Compare against critical values:
    df=6 (days): 12.59, df=11 (months): 19.68, df=3 (quarters): 7.81
    """
    return sum((o - expected_per_bucket)**2 / expected_per_bucket
               for o in observed if expected_per_bucket > 0)
```

Critical values for common degrees of freedom are stored as a lookup dict (hardcoded, not a dependency).

#### Magic Numbers

All magic numbers in this recipe are documented as experimental tuning constants:

| Constant | Default | Description | Category |
|----------|---------|-------------|----------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | `3` | Min events with same state+action before crystallization. Can be 1 for eager mode. | Behavioral |
| `WILSON_CI_THRESHOLD` | `0.6` | Wilson CI lower bound for crystallization trigger | Behavioral |
| `TD_ALPHA` | `0.1` | Q-value learning rate | Behavioral |
| `TD_GAMMA` | `0.95` | Q-value discount factor | Behavioral |
| `CHI_SQUARED_P_THRESHOLD` | `0.05` | p-value threshold for temporal discovery | Behavioral |
| `INITIAL_CYCLE_AMPLITUDE` | `0.5` | Initial amplitude for discovered cycles | Behavioral |

These should be added to issue #234 (experimental tuning) for post-ship tuning.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `compute_fingerprint()` with empty dict returns a valid fingerprint (not an error)
- [ ] `compute_fingerprint()` with `include_fields` referencing missing keys raises `KeyError`
- [ ] Crystallization handler with fewer than `MIN_EVENTS_FOR_CRYSTALLIZATION` events — no PolicyEntry created (silent)
- [ ] TD update Lua script on non-existent key — creates entry with `current_q=0`
- [ ] Wilson CI with zero total events returns 0.0 (no division by zero)
- [ ] Chi-squared with all-zero buckets returns 0.0

### Empty/Invalid Input Handling
- [ ] Empty entries batch to crystallization handler — no-op
- [ ] Events without state_fingerprint field — skipped with warning
- [ ] Events without action_type field — skipped with warning
- [ ] Temporal discovery with < 3 events per bucket — insufficient data, skip

### Error State Rendering
- Not applicable — handlers are background processing. Errors logged via `POPOTO.PolicyCache` logger.

## Test Impact

No existing tests affected — this is a greenfield recipe module. New test file:
- `tests/test_policy_cache.py` — recipe functionality and full integration matrix

## Rabbit Holes

- **Building a full RL framework** — PolicyCache is a recipe/reference pattern, not a general-purpose RL library. Ship the simplest useful version that demonstrates primitive composition.
- **Sophisticated temporal clustering** (DBSCAN, spectral clustering) — The pure-Python chi-squared against uniform distribution is sufficient for detecting obvious cyclical patterns. More sophisticated clustering is application-layer work.
- **Real-time crystallization** — The crystallization handler processes batches from the stream. It does not need to be real-time. Latency between pattern emergence and PolicyEntry creation is acceptable.
- **Packaging decisions** — Where does the recipe live long-term (recipes subpackage, examples dir, docs-only)? Defer to follow-up issue after e2e testing reveals real-world usage patterns. Ship in `src/popoto/recipes/` for now.
- **Multi-agent policy sharing** — Policies are per-agent (`agent_id` is a KeyField). Cross-agent policy transfer is a follow-up concern.

## Risks

### Risk 1: Stream entry format assumptions
**Impact:** Crystallization handler assumes specific field names in stream entries (state_fingerprint, action_type, outcome). If EventStreamMixin entry format changes, handler breaks.
**Mitigation:** Document the expected entry format. Handler validates required fields per entry and skips malformed entries with a warning.

### Risk 2: Integration test complexity
**Impact:** Full integration test exercising all 11 primitives is the most complex test in the Popoto test suite. Flaky failures from timing-dependent operations (decay, XCLAIM) could make CI unreliable.
**Mitigation:** Use deterministic timestamps where possible. Set generous timeouts. Isolate Redis keys per test with unique prefixes.

### Risk 3: Chi-squared approximation accuracy
**Impact:** Pure-Python chi-squared without proper p-value computation (no scipy) may produce false positives/negatives for borderline cases.
**Mitigation:** Use conservative critical values. The approximation is sufficient for obvious patterns (which is the use case). Edge cases are acceptable — the cycle will weaken via entrainment if it's wrong.

## Race Conditions

### Race 1: Concurrent crystallization of same pattern
**Location:** Crystallization handler creating PolicyEntry
**Trigger:** Two StreamConsumer workers process overlapping event batches with the same (state_fingerprint, action_type) pattern
**Data prerequisite:** Both workers see enough events to exceed MIN_EVENTS_FOR_CRYSTALLIZATION
**State prerequisite:** No existing PolicyEntry for this (state_fingerprint, action_type)
**Mitigation:** Duplicate PolicyEntry records are tolerable — CoOccurrenceField will link them, and the lower-confidence one will decay away naturally. For correctness, use ExistenceFilter pre-check before creating: `if not PolicyEntry.bloom.definitely_missing(PolicyEntry, fingerprint)` — won't prevent all duplicates (Bloom filter false positives) but catches most.

### Race 2: TD update during concurrent policy queries
**Location:** TD update Lua script modifying sorted set score
**Trigger:** One process updates Q-value while another reads via CompositeScoreQuery
**Data prerequisite:** PolicyEntry exists in sorted set
**State prerequisite:** Lua script is atomic but ZUNIONSTORE in CompositeScoreQuery reads a snapshot
**Mitigation:** Acceptable — CompositeScoreQuery reads a consistent snapshot at query time. The next query picks up the updated score.

## No-Gos (Out of Scope)

- **New field types or mixins** — PolicyCache composes existing primitives only
- **Changes to core ORM code** — no modifications to existing fields, models, or queries
- **Multi-agent policy transfer** — policies are per-agent; sharing is a follow-up
- **Packaging/distribution decisions** — deferred to follow-up issue after e2e testing
- **Sophisticated clustering algorithms** — chi-squared against uniform is sufficient
- **Real-time crystallization** — batch processing via StreamConsumer is sufficient

## Update System

No update system changes — Popoto is a library. Users import from `popoto.recipes.policy_cache`.

## Agent Integration

No direct agent integration — this is a Popoto ORM recipe. Agent applications compose PolicyEntry into their memory systems.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` PolicyCache section with full recipe reference
- [ ] Create `docs/guides/policy-cache-recipe.md` with step-by-step usage guide

### Inline Documentation
- [ ] Module docstring with overview, dependencies, and usage example
- [ ] Docstrings on all public functions and the PolicyEntry model
- [ ] Extensive documentation on fingerprint configuration (when to include timestamps, how to use multiple fingerprints, custom fingerprint functions)
- [ ] Document all magic numbers with rationale and tuning guidance

## Success Criteria

- [ ] `PolicyEntry` reference model composing all shipped primitives
- [ ] Q-value TD update Lua script (atomic, tested)
- [ ] Crystallization handler with configurable `MIN_EVENTS_FOR_CRYSTALLIZATION` (default 3, minimum 1) and Wilson CI threshold
- [ ] Temporal pattern discovery handler with pure-Python chi-squared test
- [ ] `compute_fingerprint()` utility with `include_fields` and `include_timestamp` support
- [ ] Full integration test exercising all 11 shipped primitives (test class with 5+ methods)
- [ ] Valkey compatible (no Redis modules)
- [ ] All magic numbers documented and added to issue #234
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)
- [ ] Follow-up issue created for packaging decision after e2e testing

## Team Orchestration

### Team Members

- **Builder (policy-cache)**
  - Name: policy-builder
  - Role: Implement PolicyEntry model, Lua script, fingerprint utility, and handler functions
  - Agent Type: builder
  - Resume: true

- **Builder (tests)**
  - Name: test-builder
  - Role: Write comprehensive integration tests covering all primitive compositions
  - Agent Type: test-writer
  - Resume: true

- **Validator (policy-cache)**
  - Name: policy-validator
  - Role: Verify recipe implementation, integration tests, and Valkey compatibility
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: docs-writer
  - Role: Update agent-memory.md, create recipe guide, document fingerprint configuration
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Create recipes subpackage and PolicyEntry model
- **Task ID**: build-policy-entry
- **Depends On**: none
- **Validates**: tests/test_policy_cache.py (create)
- **Assigned To**: policy-builder
- **Agent Type**: builder
- **Parallel**: true
- Create `src/popoto/recipes/__init__.py` with PolicyEntry export
- Create `src/popoto/recipes/policy_cache.py` with:
  - `PolicyEntry` model composing all shipped primitives
  - `TD_UPDATE_LUA` string constant for Q-value update
  - `update_q_value(instance, reward, max_future_q, alpha=0.1, gamma=0.95)` function wrapping the Lua script
  - `compute_fingerprint(features, include_fields=None, include_timestamp=False)` utility
  - All magic numbers as module-level constants with docstrings
  - `wilson_ci_lower(successes, total, z=1.96)` utility function
  - `chi_squared_uniform(observed, expected_per_bucket)` utility function
  - CHI_SQUARED_CRITICAL_VALUES lookup dict

### 2. Implement crystallization handler
- **Task ID**: build-crystallization
- **Depends On**: build-policy-entry
- **Validates**: tests/test_policy_cache.py
- **Assigned To**: policy-builder
- **Agent Type**: builder
- **Parallel**: false
- Implement `crystallization_handler(entries)` as async StreamConsumer handler
- Group events by (state_fingerprint, action_type)
- Count successes/failures per group
- Compute Wilson CI lower bound
- Create PolicyEntry when MIN_EVENTS_FOR_CRYSTALLIZATION met and Wilson CI > WILSON_CI_THRESHOLD
- ExistenceFilter pre-check to reduce duplicate crystallization
- Log crystallization events via logger

### 3. Implement temporal discovery handler
- **Task ID**: build-temporal-discovery
- **Depends On**: build-policy-entry
- **Validates**: tests/test_policy_cache.py
- **Assigned To**: policy-builder
- **Agent Type**: builder
- **Parallel**: true (parallel with step 2)
- Implement `temporal_discovery_handler(entries)` as async StreamConsumer handler
- Bucket timestamps by time-of-year, time-of-month, day-of-week
- Compute chi-squared statistic against uniform distribution
- Compare against critical values for appropriate df
- When significant cluster found (p < 0.05):
  - Compute period from TemporalPeriod constants
  - Compute phase from cluster centroid
  - Add cycle with INITIAL_CYCLE_AMPLITUDE to existing memory's CyclicDecayField

### 4. Write integration tests
- **Task ID**: build-tests
- **Depends On**: build-crystallization, build-temporal-discovery
- **Validates**: pytest tests/test_policy_cache.py -v
- **Assigned To**: test-builder
- **Agent Type**: test-writer
- **Parallel**: false
- Create `tests/test_policy_cache.py` test class with:
  - `test_policy_entry_creation` — create PolicyEntry with all field types, verify save/load
  - `test_q_value_update` — TD update Lua script, verify score changes correctly
  - `test_crystallization_from_events` — events -> stream -> handler -> PolicyEntry created
  - `test_crystallization_threshold` — verify MIN_EVENTS gating (test with 1, 2, 3 events)
  - `test_composite_score_query` — query PolicyEntry via composite_score with decay + confidence
  - `test_observation_updates_q_value` — act on policy -> observation -> Q-value updated
  - `test_prediction_error_feedback` — high prediction error reduces confidence
  - `test_temporal_discovery` — events cluster at similar times -> cycle discovered
  - `test_co_occurrence_linking` — related policies strengthen their association
  - `test_existence_filter_precheck` — bloom filter catches known fingerprints
  - `test_fingerprint_generation` — compute_fingerprint with various configurations
  - `test_fingerprint_with_timestamp` — time-bucketed fingerprints
  - `test_end_to_end` — full path: event -> crystallize -> query -> observe -> update -> re-query

### 5. Validate implementation
- **Task ID**: validate-policy-cache
- **Depends On**: build-tests
- **Assigned To**: policy-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_policy_cache.py -v`
- Verify all success criteria met
- Verify no Redis module commands (Valkey compatible)
- Verify all magic numbers documented with constants
- Verify fingerprint configuration is well-documented

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-policy-cache
- **Assigned To**: docs-writer
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` PolicyCache section with full recipe reference
- Create recipe guide with step-by-step usage
- Document fingerprint strategies extensively (when to include timestamps, multiple fingerprints, custom functions)
- Document all magic numbers with rationale

### 7. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: policy-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full test suite: `pytest tests/ -x -q`
- Verify lint: `python -m ruff check .`
- Verify format: `python -m ruff format --check .`
- Verify all success criteria met

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/test_policy_cache.py -v` | exit code 0 |
| Full suite passes | `pytest tests/ -x -q` | exit code 0 |
| Lint clean | `python -m ruff check .` | exit code 0 |
| Format clean | `python -m ruff format --check .` | exit code 0 |
| Recipe importable | `python -c "from popoto.recipes.policy_cache import PolicyEntry; print('OK')"` | output contains OK |
| No Redis modules | `grep -rn 'BF\.\|CF\.\|CMS\.\|TDIGEST\.\|TS\.' src/popoto/recipes/` | exit code 1 |

## Open Questions (Resolved)

1. **Code location**: Ship in `src/popoto/recipes/policy_cache.py` for now (importable, testable). Raise a follow-up issue after e2e testing to evaluate final packaging for real-life implementations and human teamwork scenarios. The right answer depends on how teams actually use this pattern.

2. **Q-value update mechanism**: String constant Lua script in the recipe module, called via `POPOTO_REDIS_DB.eval()`. Same pattern as `RESOLVE_PREDICTION_LUA` in prediction_ledger.py. No new core ORM helpers needed.

3. **Crystallization event counting**: `MIN_EVENTS_FOR_CRYSTALLIZATION` defaults to 3, documented as a magic number. Code explicitly supports being set to 1 for eager crystallization. Added to issue #234 magic numbers catalog.

4. **Chi-squared dependency**: Pure-Python approximation with hardcoded critical value lookup table. No scipy dependency.

5. **State fingerprint**: `compute_fingerprint()` utility with `include_fields` for field selection and `include_timestamp` for time-unique fingerprints. Multiple fingerprints per object supported by creating multiple PolicyEntry records. Extensively documented.

6. **WriteFilterMixin**: Excluded from PolicyEntry. Crystallization handler IS the write gate — decision lives in one place for clear debugging.

7. **Integration test scope**: Test class with 13 focused methods covering each primitive composition and the end-to-end critical path.
