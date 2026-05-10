---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-05-10
tracking: https://github.com/tomcounsell/popoto/issues/389
---

# TrajectoryMemory — Fingerprint-Keyed Procedural Pattern Recipe

## Problem

`popoto.recipes` ships `ContextAssembler` (retrieval-to-injection) and `SubconsciousMemory`
(auto inject/extract around LLM turns) — both compose existing primitives into named
patterns for **semantic** memory.

A third pattern keeps reappearing in agent codebases that Popoto does not yet name:
**store completed task trajectories, cluster them by structural fingerprint, and recall
the canonical "what worked last time" sequence by fingerprint.** This is procedural
memory in the cognitive sense — the unit of recall is *a sequence of actions plus an
outcome*, not a fact.

**Current behaviour.** A consumer who needs this composes ~4 collaborating pieces by
hand: an episode model, a pattern model, a crystallization step (cluster episodes by
fingerprint, observe success on the matching pattern), and a recall query
(`composite_score({confidence: ..., last_reinforced: ...})`). Each consumer reinvents
the same shape, frequently with subtle bugs in the confidence-vs-decay weighting and
non-idempotent crystallization.

**Desired outcome.** A `TrajectoryMemory` recipe that ships the four pieces as a
generic, model-agnostic class — same stewardship model as `SubconsciousMemory` (a thin
wrapper around `ContextAssembler` + an extraction helper). The recipe owns *only* the
generic primitive (fingerprint shape, episode schema, crystallization, recall);
domain-specific bits (what counts as a "topology", how trajectories are captured) stay
in the consumer.

## Freshness Check

- **Issue age**: filed 2026-05-09T14:02 UTC, plan being written 2026-05-10. <24h.
- **Baseline commit**: `82b607d` (Merge PR #387, v1.6.2). No commits have landed on
  `main` since the issue was filed that touch `src/popoto/recipes/`,
  `src/popoto/fields/confidence_field.py`, `src/popoto/models/query.py`, or
  `src/popoto/recipes/__init__.py`.
- **Cited references re-verified**:
  - `popoto.recipes.context_assembler.ContextAssembler` — exists at
    `src/popoto/recipes/context_assembler.py:1` (1189 lines). Field-introspection
    convention preserved.
  - `popoto.recipes.subconscious_memory.SubconsciousMemory` — exists at
    `src/popoto/recipes/subconscious_memory.py:81` (283 lines). Doc page at
    `docs/guides/subconscious-memory-recipe.md`.
  - `composite_score()` — exists at `src/popoto/models/query.py:446`.
  - `ConfidenceField` — exists at `src/popoto/fields/confidence_field.py:102`. **Note:**
    the issue's API sketch refers to `pattern.observe(success=...)`, but the actual
    public API is `ConfidenceField.update_confidence(instance, field_name, signal=...)`.
    The recipe MUST call `update_confidence`; the docstring/guide will note the
    cognitive-domain analogy.
- **Cited prior PR #391** ("rejected Mar 2026 with 'use Popoto primitives instead'") —
  no PR #391 exists in `tomcounsell/popoto`; the reference likely points to a downstream
  consumer repo (`tomcounsell/ai#1358` is also cited and is also a downstream consumer
  ticket). Treat as motivational context, not a constraint on this plan.
- **Active plan overlap**: `ls -lt docs/plans/` shows no in-flight plan touching
  `popoto/recipes/`. No overlap.

**Disposition: Unchanged.** Proceed.

## Research

Skipped — this is a pure-Popoto recipe with no external library, API, or ecosystem
dependency. All technical context comes from the existing codebase.

## Prior Art

Closed issues / merged PRs in this repo:

- **PR #239** — `PolicyCache` recipe (RL-style action selection by state fingerprint).
  Closest analogue: it also clusters by fingerprint, also uses `ConfidenceField`, also
  exposes a `compute_fingerprint(features, time_bucket=None)` helper at
  `src/popoto/recipes/policy_cache.py:224`. **TrajectoryMemory will reuse
  `compute_fingerprint()` directly** rather than introduce a second hashing helper.
- **Issue #233 / `docs/plans/context_assembler.md`** — established the recipe
  conventions (module docstring with synergy table, `__init__.py` export pattern,
  test-with-real-Redis discipline). TrajectoryMemory follows the same conventions.
- **`SubconsciousMemory` (PR predates archive scan)** — established the "thin wrapper
  + helper" recipe shape that the issue explicitly cites as the model. The proposed
  recipe will mirror its structure 1:1 (single class, ~280 LOC, generic over
  `model_class`, no extra dependencies).

No prior attempt at TrajectoryMemory was found in this repo. Greenfield.

## Data Flow

**Write path (`record_episode`)**

1. Caller hands the recipe a `fingerprint` dict (e.g. `{"problem_topology": "bug_fix",
   "affected_layer": "agent"}`), a `trajectory` list, an `outcome` dict, and a
   `partition` value.
2. Recipe constructs an `episode_model` instance with:
   - each `fingerprint_fields` value as a KeyField on the episode
   - `trajectory` and `outcome` written into the episode's payload fields
   - `partition` written into the episode's partition KeyField
3. `instance.save()` — episode is persisted as a normal Popoto record. No clustering
   yet (write path is intentionally cheap).

**Crystallization path (`crystallize`)**

1. `Episode.query.filter(partition=partition).all()` — pulls all episodes in the
   partition. (Crystallization is O(N) per partition; expected to run on a periodic
   cadence, not per-episode.)
2. Group episodes by the tuple of `fingerprint_fields` values.
3. For each group with `len(group) >= cluster_threshold`:
   a. Compute the canonical sequence (modal trajectory across the group; ties broken
      by most recent).
   b. Look up an existing `pattern_model` record keyed by the same fingerprint tuple
      (and `partition`).
   c. If it exists: call `ConfidenceField.update_confidence(pattern, "confidence",
      signal=success_rate)` to Bayesian-update; touch `last_reinforced` (the
      DecayingSortedField re-scores on save).
   d. If it does not exist: create a new pattern record with the canonical sequence
      and an initial confidence observation.
4. Return a list of crystallized/reinforced patterns.

**Read path (`recall`)**

1. Recipe builds a partition-filtered query on `pattern_model`.
2. If the query's filter values include each `fingerprint_field`, those are added as
   exact-match filters (recall is keyed lookup, not a fuzzy cluster query).
3. `composite_score({"confidence": w_conf, "last_reinforced": w_recency},
   limit=limit)` ranks the matching patterns. Default weights:
   `{"confidence": 0.6, "last_reinforced": 0.4}` — confidence dominates, but freshness
   breaks ties between equally-confident patterns.
4. Returns a `list[pattern_model]`, ranked.

The fingerprint tuple acts as both **partition key** (so patterns are looked up by
exact match) and **cluster key** (so episodes group naturally during crystallization).
This is the same trick `PolicyCache` uses with `state_fingerprint`.

## Why Previous Fixes Failed

N/A — no prior attempts in this repo. The motivation references downstream consumer
attempts that are out of scope for this plan.

## Appetite

**Size**: Medium

**Team**: Solo dev

**Interactions**: 1 PM check-in (scope alignment on recall API: keyed exact-match vs.
fuzzy fingerprint match) · 1 review round.

The recipe is intentionally small — Issue #389 explicitly says "shorter and more
opinionated than `ContextAssembler` — it has fewer knobs because procedural recall is a
narrower problem." Target ~250–350 LOC for the recipe module. If implementation drifts
beyond ~500 LOC, that is a signal we have absorbed concerns that belong in the
consumer.

## Prerequisites

None. All required primitives are shipped:

- `ConfidenceField.update_confidence()` — `src/popoto/fields/confidence_field.py:368`
- `DecayingSortedField` (with `base_score_field`, `partition_by`) —
  `src/popoto/fields/decaying_sorted_field.py:104`
- `composite_score()` query method — `src/popoto/models/query.py:446`
- `compute_fingerprint()` — `src/popoto/recipes/policy_cache.py:224`
- `KeyField`, `ListField`, `AutoKeyField` — all in `popoto.fields.shortcuts`

## Solution

### Key Elements

- **`TrajectoryMemory` class** in `src/popoto/recipes/trajectory_memory.py`. Generic
  over `episode_model` and `pattern_model` — the recipe introspects fields, it does not
  define them.
- **No new model classes** in this recipe. The consumer brings two models satisfying
  documented field requirements (mirrors how `ContextAssembler` is generic over
  `model_class`).
- **Three public methods**: `record_episode`, `crystallize`, `recall`. No push-path,
  no extraction helper, no formatter — by design (per issue comparison table:
  "Push path: out of scope").
- **Reuse `compute_fingerprint`** from `policy_cache.py` rather than duplicating
  hashing. Re-export it from the recipe module for ergonomics.

### Required `pattern_model` fields

The recipe introspects these by name (configurable via constructor kwargs, mirroring
`SubconsciousMemory`'s `content_field` / `importance_field` / `agent_id_field`
convention):

| Required field | Type | Default name | Purpose |
|---|---|---|---|
| Confidence | `ConfidenceField` | `confidence` | Bayesian success tracking |
| Last reinforced | `DecayingSortedField` | `last_reinforced` | Recency-weighted ranking |
| Canonical sequence | `ListField` | `canonical_sequence` | Modal trajectory |
| Each fingerprint field | `KeyField` | from `fingerprint_fields` ctor arg | Exact-match recall |
| Partition | `KeyField` | from `partition_field` ctor arg | Multi-tenant isolation |

Optional: `AccessTrackerMixin` for read-tracking. Recipe checks for it and calls
`on_access` if present (graceful degradation, same pattern as `ContextAssembler`).

### Required `episode_model` fields

| Required field | Type | Default name | Purpose |
|---|---|---|---|
| Each fingerprint field | `KeyField` | from `fingerprint_fields` | Cluster key |
| Trajectory | `ListField` | `trajectory` | Action sequence |
| Outcome | `JSONField` (or any dict-serialisable field) | `outcome` | Result payload |
| Partition | `KeyField` | from `partition_field` | Cluster scope |

### Flow

**Application → `tm.record_episode(fingerprint, trajectory, outcome, partition)`**
→ instantiate `episode_model` → `save()` → return episode pk.

**Cron / reflection → `tm.crystallize(partition)`** → load episodes → group by
fingerprint tuple → for each group ≥ threshold: upsert pattern via
`update_confidence` + canonical-sequence write → return list of patterns.

**Application → `tm.recall(fingerprint, partition, limit)`**
→ `pattern_model.query.filter(**fingerprint, partition=partition).composite_score(...)`
→ list of patterns, ranked.

### Technical Approach

- **Idempotent crystallization** is the load-bearing invariant. Achieve it by:
  1. Looking up existing patterns by `(partition, *fingerprint_field_values)` —
     guaranteed unique because fingerprint fields are KeyFields.
  2. Using `ConfidenceField.update_confidence()` (Bayesian update — running it twice
     with the same observations converges, same as PolicyCache crystallization).
  3. Computing the canonical sequence as a pure function of the current episode set
     (modal trajectory, ties broken by most-recent timestamp). Two runs on the same
     episode set produce the same canonical sequence.
  4. **Episode pruning is out of scope.** Episodes are append-only in this recipe; if
     a consumer wants TTL on episodes, they set it on their `episode_model`. Plan
     calls this out in Open Questions.
- **Recall ranking** uses `composite_score({confidence: 0.6, last_reinforced: 0.4})`.
  Both fields are sorted indexes; `composite_score` builds a ZUNIONSTORE under the
  hood — single round trip.
- **Partition filtering**: every public method requires a `partition` argument. The
  recipe passes it as a `filter()` kwarg before `composite_score` / `all()`. This is
  the same partition-isolation pattern `ContextAssembler` and `SubconsciousMemory`
  use.
- **Field introspection**: at `__init__`, walk `pattern_model._meta.fields` and
  validate the required fields are present and of the right Field subclass. Raise a
  descriptive `TypeError` immediately rather than failing deep inside `crystallize`.
  This is the contract pattern `ContextAssembler.__init__` uses.
- **Module structure** mirrors `subconscious_memory.py` exactly: module docstring with
  flow diagram, defaults block, single class. ~280 LOC target.
- **Re-export** `compute_fingerprint` so consumers don't have to import from
  `policy_cache`.

### Defaults

```python
DEFAULT_CLUSTER_THRESHOLD = 3            # episodes-per-cluster to crystallize
DEFAULT_RECALL_LIMIT = 5
DEFAULT_SCORE_WEIGHTS = {"confidence": 0.6, "last_reinforced": 0.4}
DEFAULT_CONFIDENCE_FIELD = "confidence"
DEFAULT_RECENCY_FIELD = "last_reinforced"
DEFAULT_TRAJECTORY_FIELD = "trajectory"
DEFAULT_OUTCOME_FIELD = "outcome"
DEFAULT_CANONICAL_SEQUENCE_FIELD = "canonical_sequence"
DEFAULT_PARTITION_FIELD = "partition"
```

## Failure Path Test Strategy

### Exception Handling Coverage

- [ ] `__init__` with `pattern_model` missing `ConfidenceField` → `TypeError` naming
      the missing field.
- [ ] `__init__` with `pattern_model` missing `DecayingSortedField` → `TypeError`.
- [ ] `__init__` with `episode_model` missing a fingerprint field → `TypeError`.
- [ ] `record_episode` with fingerprint dict missing one of `fingerprint_fields` →
      `ValueError`.
- [ ] `crystallize` when Redis is unreachable: propagates `ConnectionError` (no silent
      swallowing, matching `ContextAssembler` policy).
- [ ] `recall` with empty `fingerprint` dict: returns `[]` (cannot do exact-match recall
      without keys; do not silently return all patterns).

### Empty / Invalid Input Handling

- [ ] `crystallize` with zero episodes in partition: returns `[]`, no patterns created.
- [ ] `crystallize` with episodes but no group meeting threshold: returns `[]`.
- [ ] `recall` with `limit=0`: returns `[]`.
- [ ] `recall` for a partition with no patterns: returns `[]`.

## Test Impact

No existing tests affected — greenfield recipe. New file:
`tests/test_trajectory_memory.py` (real Redis, follows
`tests/test_subconscious_memory.py` patterns).

### Test Plan (Acceptance Criterion 2)

The issue's acceptance criteria explicitly require these three test areas. Each maps
1:1 to a test class:

**Idempotent crystallization**
- [ ] Record N episodes for one fingerprint cluster. Run `crystallize()` twice. Second
      run produces same set of patterns (same redis_keys, same canonical_sequence).
- [ ] Confidence after two runs is consistent with two observation rounds (Bayesian
      update is monotonic; assert `confidence_after_two_runs >= confidence_after_one_run`).
- [ ] No duplicate pattern records created (count of patterns matching fingerprint
      stays at 1).
- [ ] Episodes are not mutated by crystallization (their pks and payloads are
      untouched).

**Recall ordering**
- [ ] Two patterns with equal confidence: the more recently reinforced ranks higher
      (the "fresh > stale at equal confidence" criterion from the issue).
- [ ] Two patterns with same `last_reinforced`: the higher-confidence one ranks
      higher.
- [ ] `recall(fingerprint, limit=N)` returns at most N patterns, in descending
      composite-score order.
- [ ] `composite_score` weight changes propagate: passing
      `score_weights={"confidence": 1.0, "last_reinforced": 0.0}` produces a
      confidence-only ranking; passing `{"confidence": 0.0, "last_reinforced": 1.0}`
      produces a recency-only ranking.

**Partition isolation**
- [ ] Episodes recorded in `partition="A"` do NOT contribute to a pattern in
      `partition="B"`, even with identical fingerprints.
- [ ] `crystallize(partition="A")` returns only A's patterns; B's patterns are
      untouched (assert via redis_key count and confidence values).
- [ ] `recall(fingerprint, partition="A")` returns no patterns from B.

**Plus a baseline integration test**
- [ ] End-to-end: record 5 episodes → crystallize → recall returns the canonical
      sequence → record 5 more episodes for same fingerprint → crystallize again →
      `last_reinforced` advanced, `confidence` strengthened, recall ranking unchanged
      (still returns the same pattern as #1).

## Rabbit Holes

- **Trajectory similarity scoring.** Tempting to add edit-distance or n-gram overlap
  to find "near-canonical" sequences. The recipe uses modal trajectory only — exact
  agreement, ties broken by recency. Anything fancier is application-layer.
- **TTL on episodes.** Episodes accumulate forever in this recipe. Consumers who want
  bounded retention set TTL on their `episode_model`. The recipe does not own
  retention.
- **Async crystallization.** Crystallization is O(N) and could be slow for large
  partitions. Don't add an async variant — same reasoning as `ContextAssembler`. If a
  consumer needs background crystallization, they wrap the call in their own task
  queue.
- **Cluster-based fuzzy recall.** Tempting to recall by "nearest fingerprint" rather
  than exact match. Issue is explicit: recall is "by fingerprint" (exact). Fuzzy
  match is application-layer.
- **Crystallization-on-every-write.** Tempting to call `crystallize()` from
  `record_episode` for "live" patterns. Don't — issue calls out crystallization is for
  daily reflection; per-write crystallization makes hot paths O(N).
- **Push-path (proactive surfacing).** Comparison table in the issue explicitly says
  "Out of scope — trajectory recall is pull-only." Honour it.
- **Custom canonical-sequence aggregator.** Tempting to expose the canonical-sequence
  computation as a strategy callable. Ship modal-with-recency-tiebreak only; if
  consumers need something else, they subclass.

## Risks

### Risk 1: Modal-trajectory ambiguity when no clear mode exists

**Impact**: Crystallization picks an arbitrary canonical sequence; subsequent runs
might pick a different one if episode arrival order changes; idempotence breaks.

**Mitigation**: Tie-break by most-recent episode timestamp deterministically. Episode
ordering is stable (Redis sorts by score). Test case: cluster with three distinct
trajectories at frequency 1 each → assert canonical sequence is the most recent
trajectory and is stable across re-runs.

### Risk 2: Confidence drift under repeated crystallization

**Impact**: Calling `crystallize()` daily on the same episode set Bayesian-updates
confidence on every run; an unchanged episode set produces ever-strengthening
confidence (a "phantom reinforcement" bug).

**Mitigation**: Track which episodes have already been observed for a given pattern.
Two viable approaches:

  (a) Add an `observed_episode_pks: ListField` on the pattern. Skip episodes already
      in the list during crystallization. Simple, but adds a required field.

  (b) Filter episodes by `last_reinforced > pattern.last_reinforced` before
      observing — only count episodes newer than the last crystallization.

Approach (b) needs no extra fields and reuses an already-required field. Plan adopts
**approach (b)**. Test case: run `crystallize()` twice with no new episodes between
runs → confidence and `last_reinforced` are unchanged on the second run.

### Risk 3: Field-introspection brittleness across model class hierarchies

**Impact**: A consumer's `pattern_model` declares `ConfidenceField` on a parent class;
naive `vars(model_class)` introspection misses it; recipe rejects a valid model.

**Mitigation**: Use `model_class._meta.fields` (the same path `ContextAssembler` uses).
This is the model framework's canonical field registry and walks inheritance.

## Race Conditions

Identified hazards and how the recipe handles them:

- **Concurrent `record_episode` + `crystallize`.** A new episode landing during a
  crystallization run is either picked up (good) or missed (acceptable — the next
  crystallization picks it up). No data corruption: episodes are append-only and
  patterns are upserted by exact key, not by aggregate count.
- **Two concurrent `crystallize(partition=P)` calls** (e.g., two reflection processes
  racing). Both compute the same canonical sequence (deterministic) and both call
  `update_confidence` — Bayesian updates are commutative, so the final confidence is
  approximately right (slightly over-counted if both observe the same episode set).
  **Mitigation**: document that crystallization should be single-flighted per
  partition; do not add a Redis lock in the recipe (consumer concern).
- **`record_episode` and `recall` racing**: `recall` reads patterns, not episodes, so
  it is unaffected by in-flight episode writes. Patterns are only updated by
  `crystallize`, which is the only mutation path for the data `recall` reads.

## No-Gos (Out of Scope)

- Push-path / proactive surfacing of patterns (issue explicit).
- Async API variants.
- Fuzzy-fingerprint recall.
- Edit-distance / n-gram trajectory similarity.
- TTL or retention policy on episodes.
- Multi-model assembly (one episode_model + one pattern_model per recipe instance).
- Distributed-lock primitives for crystallization concurrency.
- Custom canonical-sequence aggregators.
- Cross-partition recall.

## Update System

No update-system changes — pure library feature.

## Agent Integration

No agent integration — Popoto is a library; consumers wire it in.

## Documentation

### Feature / Recipe Documentation

- [ ] New: `docs/guides/trajectory-memory-recipe.md`. Mirror
      `docs/guides/subconscious-memory-recipe.md` structure 1:1:
        - Architecture diagram (record → crystallize → recall)
        - Quick Start with full example (`CyclicEpisode`, `ProceduralPattern` model
          definitions matching the issue's API sketch)
        - "How It Works" section per public method
        - Tuning table for the defaults block
        - Extensibility section (subclassing `TrajectoryMemory` for custom canonical
          sequence)
        - "See Also" linking ContextAssembler, PolicyCache, SubconsciousMemory.
- [ ] Update `docs/recipes.md` (the recipe index) — add a row for TrajectoryMemory.
- [ ] Update `mkdocs.yml` nav to include the new guide page.

### Inline Documentation

- [ ] Module docstring on `trajectory_memory.py` following `subconscious_memory.py`
      conventions (purpose, flow diagram, dependencies, example).
- [ ] Method docstrings on `__init__`, `record_episode`, `crystallize`, `recall`.
- [ ] Note in the `__init__` docstring that the `pattern.observe(success=...)` API in
      the issue's sketch is realised via `ConfidenceField.update_confidence` under the
      hood — the consumer doesn't call it directly.

### External Documentation Site

- [ ] Verify `mkdocs build --strict` passes.

## Success Criteria

- [ ] `popoto.recipes.TrajectoryMemory` class importable from
      `popoto.recipes.trajectory_memory` and re-exported via `popoto.recipes.__init__`.
- [ ] Three public methods: `record_episode`, `crystallize`, `recall` — signatures
      matching issue's API sketch.
- [ ] Field introspection convention from `ContextAssembler` (named ctor kwargs for
      every field name; descriptive `TypeError` on missing required fields).
- [ ] Idempotent crystallization (re-running on unchanged episode set produces no
      drift in confidence or `last_reinforced`).
- [ ] Recall ranking respects composite_score: confidence-dominant by default, fresh
      > stale at equal confidence.
- [ ] Partition isolation enforced on all three methods.
- [ ] Valkey compatible — no Redis modules.
- [ ] Test file `tests/test_trajectory_memory.py` covers all three acceptance areas
      (idempotency, ordering, partition).
- [ ] Guide page `docs/guides/trajectory-memory-recipe.md` published, mirroring
      `subconscious-memory-recipe.md`.
- [ ] `pytest tests/test_trajectory_memory.py -x -q` exits 0.
- [ ] `pytest tests/ -x -q` exits 0 (no regression).
- [ ] `ruff check src/popoto/recipes/trajectory_memory.py` exits 0.

## Step by Step Tasks

### 1. Implement `TrajectoryMemory` class

- **Task ID**: build-trajectory-memory
- **Depends On**: none
- **Validates**: `tests/test_trajectory_memory.py` (created in step 2)
- **Agent Type**: builder
- Create `src/popoto/recipes/trajectory_memory.py`. Mirror
  `subconscious_memory.py` module structure (module docstring with flow diagram,
  defaults block, single class).
- Constructor signature: `__init__(self, episode_model, pattern_model,
  fingerprint_fields, cluster_threshold=3, score_weights=None,
  partition_field="partition", confidence_field="confidence",
  recency_field="last_reinforced", trajectory_field="trajectory",
  outcome_field="outcome", canonical_sequence_field="canonical_sequence")`.
- Field introspection in `__init__` via `pattern_model._meta.fields` and
  `episode_model._meta.fields` — raise `TypeError` listing missing fields.
- `record_episode(self, fingerprint, trajectory, outcome, partition)` — instantiate
  `episode_model` with the field name mapping, call `save()`, return the instance.
- `crystallize(self, partition)`:
    1. `episodes = list(episode_model.query.filter(partition=partition).all())`.
    2. Group by tuple of fingerprint values (Python-side `defaultdict`).
    3. For each group with `len >= cluster_threshold`:
        - Lookup pattern by `pattern_model.query.filter(partition=partition,
          **fingerprint).all()` — expect 0 or 1 result (fingerprint fields are
          KeyFields, so the combo is unique under partition).
        - If 0: instantiate a new pattern with canonical sequence + initial
          confidence observation, save.
        - If 1: filter episodes to those with timestamp newer than
          `pattern.last_reinforced`; observe each via
          `ConfidenceField.update_confidence(pattern, confidence_field,
          signal=success_rate_for_episode)`; recompute canonical sequence; save.
    4. Return list of (created or reinforced) patterns.
- `recall(self, fingerprint, partition, limit=5, score_weights=None)`:
    1. If `fingerprint` is empty → return `[]`.
    2. Build query: `pattern_model.query.filter(partition=partition,
       **fingerprint).composite_score(weights, limit=limit)`.
    3. Return list.
- Re-export `compute_fingerprint` from `popoto.recipes.policy_cache` at module top.
- Update `popoto/recipes/__init__.py` to export `TrajectoryMemory`. Append to
  `__all__` in alphabetical order.
- **No update to `popoto.__init__`** — recipes are namespaced under `popoto.recipes`,
  matching `SubconsciousMemory` (which is not in the top-level public API either).

### 2. Implement integration tests

- **Task ID**: build-tests
- **Depends On**: build-trajectory-memory
- **Validates**: `tests/test_trajectory_memory.py`
- **Agent Type**: builder
- Create `tests/test_trajectory_memory.py` following `test_subconscious_memory.py`
  patterns — real Redis (DB 15 via the popoto pytest plugin), no mocks.
- Define a `CyclicEpisode` model and a `ProceduralPattern` model in the test module
  matching the issue's API sketch (KeyFields for `problem_topology`,
  `affected_layer`, `partition`; ConfidenceField; DecayingSortedField for
  `last_reinforced`; ListField for `trajectory` / `canonical_sequence`).
- Test classes:
  - `TestRecordEpisode` (smoke + signature)
  - `TestCrystallizeIdempotent` (the four bullets under "Idempotent crystallization"
    above)
  - `TestRecallOrdering` (the four bullets under "Recall ordering")
  - `TestPartitionIsolation` (the three bullets under "Partition isolation")
  - `TestErrors` (the six bullets under "Exception Handling Coverage")
  - `TestEmptyInputs` (the four bullets under "Empty / Invalid Input Handling")
  - `TestEndToEnd` (the integration scenario)

### 3. Documentation

- **Task ID**: docs
- **Depends On**: build-tests
- **Agent Type**: documentarian
- Create `docs/guides/trajectory-memory-recipe.md` mirroring
  `docs/guides/subconscious-memory-recipe.md` structure exactly.
- Update `docs/recipes.md` index.
- Update `mkdocs.yml` nav.

### 4. Final validation

- **Task ID**: validate-all
- **Depends On**: docs
- **Agent Type**: validator
- Run `pytest tests/test_trajectory_memory.py -x -q`.
- Run `pytest tests/ -x -q`.
- Run `ruff check src/popoto/recipes/trajectory_memory.py`.
- Run `mkdocs build --strict`.
- Verify all success-criteria checkboxes met.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Recipe tests pass | `pytest tests/test_trajectory_memory.py -x -q` | exit 0 |
| Full suite passes | `pytest tests/ -x -q` | exit 0 |
| Lint clean | `ruff check src/popoto/recipes/trajectory_memory.py` | exit 0 |
| Format clean | `ruff format --check src/popoto/recipes/trajectory_memory.py` | exit 0 |
| Class importable | `python -c "from popoto.recipes import TrajectoryMemory"` | exit 0 |
| Docs build | `mkdocs build --strict` | exit 0 |

---

## Open Questions

1. **`pattern.observe` ergonomics.** Issue's API sketch shows
   `pattern.observe(success=True)` as the conceptual call shape, but Popoto's actual
   API is `ConfidenceField.update_confidence(pattern, "confidence", signal=...)`.
   Three options: (a) keep the recipe internal-only (consumers never see `observe`,
   they call `record_episode` / `crystallize`); (b) add a thin
   `TrajectoryMemory.observe_pattern(pattern, success=True)` convenience method;
   (c) add a project-wide `pattern.observe(...)` shortcut on `ConfidenceField`
   itself (out of scope here, but worth flagging). **Recommendation: (a) — the
   recipe owns the observation call site; consumers never need `observe`.**

2. **Episode timestamp source.** Crystallization uses an episode's timestamp to
   filter "newer than last_reinforced" (Risk 2 mitigation). Should the recipe
   require a `recorded_at: DecayingSortedField` (or similar) on `episode_model`, or
   should it use the model's `_auto_key` ordering as a proxy? Latter is simpler;
   former is more explicit. **Recommendation: require an explicit
   `episode_recency_field` (default `recorded_at`); document it in the field
   requirements table.** This question changes the field-requirements section if
   answered differently.

3. **Cluster threshold tuning.** Default `cluster_threshold=3` matches the issue's
   sketch. Should it be tunable via a `magic_numbers.py` constant (so it participates
   in the project-wide tuning sweeps catalogued in
   `docs/features/experimental_tuning_magic_numbers.md`)? **Recommendation: yes —
   add `Defaults.TRAJECTORY_CLUSTER_THRESHOLD` and reference it from the recipe.**
