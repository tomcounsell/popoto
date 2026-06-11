# TrajectoryMemory Recipe

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

Procedural memory: store completed task **trajectories**, cluster them by structural fingerprint, and recall the canonical "what worked last time" sequence by fingerprint.

`TrajectoryMemory` is the third named recipe in `popoto.recipes`, alongside [`ContextAssembler`](../features/context-assembler.md) (semantic retrieval) and [`SubconsciousMemory`](subconscious-memory-recipe.md) (auto inject/extract around LLM turns). Where those two model **facts**, `TrajectoryMemory` models **sequences of actions plus an outcome** — the cognitive analogue of procedural memory.

## Architecture

```
record_episode(fingerprint, trajectory, outcome, partition)
    |
    v
[episode_model.save() -- append-only]
    |
    v
crystallize(partition)   (periodic, e.g. daily reflection)
    |
    +-- group episodes by fingerprint tuple
    +-- for each group with len >= cluster_threshold:
    |       upsert pattern_model record
    |       update_confidence(signal=success_rate)
    |       write canonical (modal) trajectory
    v
recall(fingerprint, partition, limit)
    |
    +-- pattern_model.query.filter(**fingerprint, partition=partition)
    +-- composite_score({confidence, last_reinforced})
    v
list[pattern_model], ranked
```

## Quick Start

Define an `episode_model` (raw trajectories, append-only) and a `pattern_model` (crystallized canonical sequences keyed by fingerprint). The recipe introspects both — it does not define them.

```python
from popoto import (
    AutoKeyField, ConfidenceField, DecayingSortedField, DictField,
    KeyField, ListField, Model,
)
from popoto.recipes import TrajectoryMemory


class CyclicEpisode(Model):
    episode_id = AutoKeyField()
    partition = KeyField()                       # tenant / agent / team scope
    problem_topology = KeyField()                # part of the fingerprint
    affected_layer = KeyField()                  # part of the fingerprint
    trajectory = ListField(default=[])           # ordered actions taken
    outcome = DictField(default={})              # {"success": True, ...}
    recorded_at = DecayingSortedField(partition_by="partition")


class ProceduralPattern(Model):
    pattern_id = AutoKeyField()
    partition = KeyField()
    problem_topology = KeyField()
    affected_layer = KeyField()
    canonical_sequence = ListField(default=[])   # the "what worked" sequence
    confidence = ConfidenceField(initial_confidence=0.5)
    last_reinforced = DecayingSortedField(partition_by="partition")


tm = TrajectoryMemory(
    episode_model=CyclicEpisode,
    pattern_model=ProceduralPattern,
    fingerprint_fields=["problem_topology", "affected_layer"],
    cluster_threshold=3,
)
```

## How It Works

### `record_episode(fingerprint, trajectory, outcome, partition)`

Persist a single completed trajectory. The write path is intentionally cheap — no clustering happens here.

```python
tm.record_episode(
    fingerprint={"problem_topology": "bug_fix", "affected_layer": "agent"},
    trajectory=["read_logs", "edit_file", "run_tests"],
    outcome={"success": True},
    partition="team-a",
)
```

The recipe validates that `fingerprint` contains every name in `fingerprint_fields`. Missing fields raise `ValueError`.

### `crystallize(partition)`

Periodic (e.g. nightly reflection): cluster recent episodes by their fingerprint tuple, then upsert one `pattern_model` per cluster that exceeds `cluster_threshold`.

```python
# Triggered by a cron or reflection step, not on every write.
new_or_reinforced = tm.crystallize(partition="team-a")
```

Crystallization is **idempotent via watermark**: re-running on an unchanged episode set produces zero drift in confidence or `last_reinforced`. After each run, `last_reinforced` is set to the maximum `recorded_at` timestamp across all observed episodes (the watermark). Subsequent runs observe only episodes strictly newer than that watermark, so the same episodes are never double-counted.

**Operational constraints for idempotence:**

- **One crystallizer per partition.** Concurrent crystallization of the same partition is not coordinated — two processes reading the same episode set and both calling `_observe_episodes` will double-observe episodes, inflating confidence counts. Run a single crystallizer per partition.
- **NTP-synced writers.** The watermark guarantee assumes episode writers and the crystallizer are within ordinary NTP clock synchronization. A writer whose clock lags behind the current watermark will record episodes with `recorded_at` timestamps below the watermark; the strict `>` filter skips those episodes permanently on all subsequent runs.
- **`last_reinforced` stores episode time, not crystallize time.** The stored score for `last_reinforced` equals the maximum observed episode `recorded_at` — which may predate the wall-clock time the crystallize call ran. An operator running `ZSCORE` on the recency index reads "newest episode processed", not "when crystallize last ran". Under batched or nightly crystallization, patterns will rank correspondingly older in recency-weighted recall (`DEFAULT_SCORE_WEIGHTS` weights `last_reinforced` at 0.4) than they would under wall-clock semantics.

Note: within the evidence window, the capped-evidence update rule is order-invariant (to ~1e-12), which bounds — but does not eliminate — confidence drift if the single-crystallizer constraint is violated. Do not rely on commutativity as a substitute for the operational constraint.

The canonical sequence is the **modal trajectory** across the group, with ties broken by the most recent episode. Anything fancier (edit-distance, n-gram similarity, custom aggregators) is intentionally out of scope.

### `recall(fingerprint, partition, limit=5)`

Keyed exact-match retrieval. Returns at most `limit` patterns matching the supplied fingerprint dimensions, ranked by composite score.

```python
patterns = tm.recall(
    fingerprint={"problem_topology": "bug_fix", "affected_layer": "agent"},
    partition="team-a",
)
for p in patterns:
    print(p.canonical_sequence, "conf=", p.confidence)
```

A **partial fingerprint** is allowed (a subset of `fingerprint_fields`) — useful when an agent only knows part of the context. Empty `fingerprint` returns `[]` deliberately, so callers commit to at least one dimension rather than scanning the partition.

## Required Fields

The recipe introspects field names via `model_class._meta.fields`. Field names below are configurable via constructor kwargs (mirroring [`SubconsciousMemory`](subconscious-memory-recipe.md)'s convention).

### `pattern_model`

| Required field         | Type                  | Default name           | Purpose                       |
| ---------------------- | --------------------- | ---------------------- | ----------------------------- |
| Each fingerprint field | `KeyField`            | from `fingerprint_fields` ctor arg | Exact-match recall |
| Partition              | `KeyField`            | `partition`            | Multi-tenant isolation        |
| Confidence             | `ConfidenceField`     | `confidence`           | Capped-evidence success tracking |
| Last reinforced        | `DecayingSortedField` | `last_reinforced`      | Recency-weighted ranking      |
| Canonical sequence     | `ListField`           | `canonical_sequence`   | Modal trajectory              |

### `episode_model`

| Required field         | Type                  | Default name        | Purpose                       |
| ---------------------- | --------------------- | ------------------- | ----------------------------- |
| Each fingerprint field | `KeyField`            | from `fingerprint_fields` | Cluster key             |
| Partition              | `KeyField`            | `partition`         | Cluster scope                 |
| Trajectory             | `ListField`           | `trajectory`        | Action sequence               |
| Outcome                | dict-serialisable field | `outcome`         | Result payload                |
| Episode recency        | `DecayingSortedField` | `recorded_at`       | Crystallization filter (newer than `pattern.last_reinforced`) |

Missing or wrong-typed fields raise a single `TypeError` at `__init__` listing every problem — you do not discover them one at a time inside `crystallize()`.

## Defaults & Tuning

```python
DEFAULT_RECALL_LIMIT = 5
DEFAULT_SCORE_WEIGHTS = {"confidence": 0.6, "last_reinforced": 0.4}
```

Confidence dominates; freshness breaks ties between equally-confident patterns. Override per-call via `recall(..., score_weights={"confidence": 1.0, "last_reinforced": 0.0})`.

`cluster_threshold` defaults to `Defaults.TRAJECTORY_CLUSTER_THRESHOLD` (currently `3`) when omitted. The constant participates in the project-wide tuning sweeps described in [Magic Numbers Tuning](tuning-magic-numbers.md). Override before model definition or at runtime:

```python
from popoto.fields.constants import Defaults
Defaults.TRAJECTORY_CLUSTER_THRESHOLD = 5  # require more evidence
```

## The `pattern.observe(success=...)` Ergonomics

If you have read the [tracking issue](https://github.com/tomcounsell/popoto/issues/389) you may have noticed an API sketch with `pattern.observe(success=...)`. The recipe **owns** the observation call site — consumers never call `observe` directly. Inside `crystallize`, the recipe calls `ConfidenceField.update_confidence(pattern, confidence_field, signal=...)` on your behalf, deriving `signal` from each episode's `outcome` (default: `{"success": True}` → `0.9`, `{"success": False}` → `0.1`).

Subclass to plug a different outcome-to-signal mapping:

```python
class CustomTM(TrajectoryMemory):
    def _episode_signal(self, episode):
        # Map a richer outcome schema to a [0, 1] confidence signal
        outcome = getattr(episode, self.outcome_field, None) or {}
        score = outcome.get("user_rating", 3) / 5.0  # rating out of 5
        return max(0.0, min(1.0, score))
```

## Extensibility

Subclass `TrajectoryMemory` to override:

* `_canonical_sequence(episodes)` — replace modal-with-recency-tiebreak with your own aggregator (edit-distance, longest-common-subsequence, etc.).
* `_episode_signal(episode)` — map your outcome schema to a `[0, 1]` confidence signal.

The recipe deliberately keeps these as the only extension points. If you need fuzzy fingerprint recall, custom retention, push-path surfacing, or async crystallization — those belong in your application layer, not in the recipe (see the plan's **Rabbit Holes** section).

## Compute Fingerprint Helper

For consumers that need a stable fingerprint from a dict of features (rather than passing fingerprint fields directly), the recipe re-exports `compute_fingerprint` from `popoto.recipes.policy_cache`:

```python
from popoto.recipes.trajectory_memory import compute_fingerprint

fp = compute_fingerprint({"task": "deploy", "env": "staging"})  # SHA-256, 16 hex chars
```

This produces deterministic 16-hex-char fingerprints suitable for use as a `KeyField` value.

## See Also

* [ContextAssembler](../features/context-assembler.md) — retrieval-to-injection bridge for **semantic** memory
* [SubconsciousMemory](subconscious-memory-recipe.md) — automatic inject/extract around LLM turns
* [PolicyCache](policy-cache-recipe.md) — RL-style action selection by state fingerprint (the closest analogue: also clusters by fingerprint, also uses `ConfidenceField`)
* [`ConfidenceField`](../features/confidence-field.md) — capped-evidence confidence primitive used for pattern reinforcement
* [`DecayingSortedField`](../features/decaying-sorted-field.md) — time-decaying score used for `last_reinforced` ranking
