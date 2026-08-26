# Tuning Magic Numbers: Agent-Memory Constants

> **New to Agent Memory?** Start with the [Quickstart Guide](agent-memory-quickstart.md) for a progressive adoption path.

This guide documents the ~25 behavioral constants that control Popoto's agent-memory primitives. Each constant has been validated through systematic parameter sweeps across hand-crafted benchmark scenarios and parametrically generated stress-test scenarios.

## Overview

Popoto's agent-memory stack uses constants that control scoring, decay, strengthening, weakening, filtering, and learning. These were initially set to reasonable guesses and have now been validated through a benchmark harness that measures retrieval quality (precision@k, nDCG) and calibration error across factual recall, multi-step reasoning, and temporal scheduling scenarios.

For Tiers 1-3, a `ScenarioFactory` can generate 50 diverse stress-test scenarios from parameterized seeds, with a 70/30 train/validation split to guard against overfitting. A complementary `FamilyScenarioFactory` (see `tests/benchmarks/scenarios/family_factory.py`) adds 7 family-aware scenarios (decay, confidence, write_filter, co_occurrence, prediction_ledger, context_assembler, policy_cache) that exercise each constant's actual code path — these are the scenarios that surface the sensitivity ratings in the tables below. A ratchet loop automates keep/discard decisions for proposed constant changes. See [Parametric Sweep](../features/parametric-sweep.md) for details.

**Key finding**: The initial defaults are all within their safe operating ranges. The only constant with a cliff effect is `ACTED_CYCLE_STRENGTHEN_FACTOR`, which must be >= 1.0. As of sweep 2026-04-20 (`tests/benchmarks/results/sweep_20260420_051055.json`), 6 of 26 swept constants show nDCG@5 variance > 0.05 (`initial_weight`, `WILSON_CI_THRESHOLD`, `decay_per_hop`, `_wf_min_threshold`, `decay_rate`, `COMPETITIVE_SUPPRESSION_SIGNAL`).

## Constant Catalog

### ObservationProtocol Constants

Source: `src/popoto/fields/observation.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `ACTED_CONFIDENCE_SIGNAL` | 0.9 | [0.5, 1.0] | Low |
| `CONTRADICTED_CONFIDENCE_SIGNAL` | 0.1 | [0.05, 0.3] | Low |
| `ACTED_CYCLE_STRENGTHEN_FACTOR` | 1.2 | [1.0, 2.0] | **HIGH** — cliff at <1.0 |
| `DISMISSED_CYCLE_WEAKEN_FACTOR` | 0.8 | [0.3, 1.0] | Low |
| `CONTRADICTED_CYCLE_WEAKEN_FACTOR` | 0.5 | [0.3, 0.8] | Low |
| `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` | 0.1 | [0.05, 0.3] | Low |

### ConfidenceField

Source: `src/popoto/fields/confidence_field.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `initial_confidence` | 0.5 | [0.1, 0.9] | Low |
| `evidence_cap` | 20 | — | Not swept — user-facing config, not a tuning constant |
| `CONFIDENCE_EPSILON` | 1e-9 | — | Not swept — internal float-boundary guard |

`evidence_cap` (default `Defaults.CONFIDENCE_EVIDENCE_CAP = 20`) is the capped-evidence Bayesian rule's memory-window length — an epistemics knob deliberately exposed as per-field user config per the [issue #407](https://github.com/tomcounsell/popoto/issues/407) decision, so it is excluded from experimental sweeps. `CONFIDENCE_EPSILON` is the float tolerance applied to the `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` comparison in `observation.py` (values within epsilon of the threshold do not discharge); it guards a boundary condition and is not user config. See [ConfidenceField](../features/confidence-field.md) for the update-rule semantics.

### WriteFilterMixin

Source: `src/popoto/fields/write_filter.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `_wf_min_threshold` | **0.1** (sweep 2026-04-17) | [0.05, 0.5] | **Medium** (variance 0.068) |
| `_wf_priority_threshold` | 0.7 | [0.5, 0.9] | Low |

### NeverRecordMixin

Source: `src/popoto/privacy/never_record.py`, constants in `src/popoto/fields/constants.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `NR_ENTROPY_MIN_TOKEN_LEN` | 20 | — | Not swept |
| `NR_ENTROPY_MIN_BITS` | 3.5 | — | Not swept |
| `NR_ASSIGNMENT_MIN_VALUE_LEN` | 6 | — | Not swept |
| `NR_TOMBSTONE_LOG_MAX` | 1000 | n/a (capacity bound) | n/a |
| `NEVER_RECORD_ENABLED` | `True` (auto-detected; env override) | n/a (boolean) | n/a |

None of the four `NR_*` numeric constants have been through this guide's benchmark
harness — there is no sweep file backing any of them, and no retrieval-quality
metric applies to a privacy gate's block/allow decision the way it applies to a
decay rate or a threshold. They are set by argument, not by measurement:

- `NR_ENTROPY_MIN_TOKEN_LEN` (20) is the shortest whitespace token the entropy
  backstop will score. Below 20 characters, ordinary base64-ish English words
  start to dominate the token population and the detector becomes noise rather
  than signal.
- `NR_ENTROPY_MIN_BITS` (3.5) is the Shannon bits-per-character threshold above
  which a token is treated as random rather than natural language. Random
  base64 runs roughly 5.5-6.0 bits/char, random hex roughly 4.0; English text
  rendered over the same character set sits well below 3.5. The value is
  deliberately conservative toward over-blocking, not corpus-tuned.
- `NR_ASSIGNMENT_MIN_VALUE_LEN` (6) is the shortest value after a
  `password=`/`token:`-style prefix that counts as a credential assignment; it
  is also the shortest accepted password in a `scheme://user:password@host`
  URL.
- `NR_TOMBSTONE_LOG_MAX` (1000) caps the `$NR:{Class}:drops` LIST to a recent
  window. The `$NR:{Class}:counts` HASH is unbounded and is the authoritative
  count; the list exists for recent-drop inspection, not auditing at scale.

`NEVER_RECORD_ENABLED` is not a tuning constant at all — like
`DATETIME_KEY_LEGACY`, it is a deploy-level kill switch. It is default `True`
(the firewall runs), backed by the `POPOTO_NEVER_RECORD_DISABLE` environment
variable read at import, and assignable directly at runtime to disable the
firewall without touching model code. See
[NeverRecordFirewall](../features/never-record-firewall.md) for the guarantee
this gate makes and the enumerated holes in it (canonical git SHAs and UUIDs
are excluded from entropy scoring, for example) — those are shape decisions,
not values a sweep would tune.

### DecayingSortedField / CyclicDecayField

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `decay_rate` | **0.1** (sweep 2026-04-17) | [0.1, 1.0] | **Medium** (variance 0.067) |
| `DECAY_CONFIDENCE_MODULATION_STRENGTH` | 0.5 (not yet swept) | [0.3, 0.7] | Unmeasured |
| `DECAY_CONFIDENCE_MODULATION_ENABLED` | `True` | n/a (boolean) | n/a |

`DECAY_CONFIDENCE_MODULATION_STRENGTH` is the `s` in the per-record effective rate
`decay_rate * 2^(s * 2 * (c0 - confidence))`, where `c0` is the confidence field's own
`initial_confidence` — so `s` reads as "doublings of the decay rate at zero confidence", and
`s = 0` is a bit-exact no-op. The default is the literature-grounded midpoint of the 0.3–0.7 band
(Pavlik & Anderson 2005; Duolingo half-life regression), pending tuning against real dismissal data
([issue #493](https://github.com/tomcounsell/popoto/issues/493)) rather than synthetic corpora.
`DECAY_CONFIDENCE_MODULATION_ENABLED` is a deploy-level kill switch, not a tuning knob: modulation
is default-on via auto-detection, and setting this `False` restores pre-modulation behavior
byte-for-byte without editing any model definition.

### MemoryLifecycle

Source: `src/popoto/recipes/memory_lifecycle.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `LIFECYCLE_FORGET_CONFIDENCE_CEILING` | 0.3 (not yet swept) | [0.1, 0.5] | Unmeasured |
| `LIFECYCLE_FORGET_MIN_EVIDENCE` | 5 (not yet swept) | [3, 20] | Unmeasured |
| `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` | 1000 (by design) | n/a (capacity bound) | n/a |

The ceiling sits below `INITIAL_CONFIDENCE` (0.5) so a record must have moved decisively negative
before confidence alone can bury it, and below `LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD` (0.6) so
the forget and promote bands cannot overlap. `LIFECYCLE_FORGET_MIN_EVIDENCE` is a safety floor, not
a performance knob — confidence moves on every signal, so without it one unlucky dismissal could
forget a memory. `LIFECYCLE_TOMBSTONE_RETENTION_LIMIT` bounds the tombstone corpus (oldest age out);
it is a capacity bound rather than a quality parameter.

### CoOccurrenceField

Source: `src/popoto/fields/co_occurrence_field.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `decay_factor` | 0.95 | [0.5, 0.99] | Low |
| `initial_weight` | 0.1 | [0.01, 0.5] | **HIGH** (variance 0.144, sweep 2026-04-20) |
| `delta` | 0.05 | [0.01, 0.2] | Low |
| `decay_per_hop` | 0.5 | [0.1, 0.9] | **HIGH** (variance 0.112, sweep 2026-04-20) |

### PredictionLedgerMixin

Source: `src/popoto/fields/prediction_ledger.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `_pl_confidence_error_threshold` | 0.7 | — | Not swept (Tier 2) |
| `_pl_confidence_low_signal` | 0.2 | — | Not swept (Tier 2) |
| `_pl_auto_resolve_errors` | {acted:0.1, dismissed:0.5, contradicted:0.9, used:0.3} | — | Not swept |

### PolicyCache

Source: `src/popoto/recipes/policy_cache.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | 3 | [1, 10] | Low |
| `WILSON_CI_THRESHOLD` | 0.6 | [0.3, 0.8] | **HIGH** (variance 0.130, sweep 2026-04-20 via `PolicyCacheFamilyScenario`) |
| `TD_ALPHA` | 0.1 | [0.01, 0.5] | Low |
| `TD_GAMMA` | 0.95 | [0.8, 0.99) | Low |
| `CHI_SQUARED_P_THRESHOLD` | 0.05 | — | Not swept |
| `INITIAL_CYCLE_AMPLITUDE` | 0.5 | — | Not swept |

### ContextAssembler

Source: `src/popoto/recipes/context_assembler.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `COMPETITIVE_SUPPRESSION_SIGNAL` | 0.3 | [0.1, 0.7] | **Medium** (variance 0.053, sweep 2026-04-20 via `ContextAssemblerFamilyScenario`) |
| `DEFAULT_SURFACING_THRESHOLD` | 0.5 | [0.1, 0.9] | Low |

### TrajectoryMemory

Source: `src/popoto/recipes/trajectory_memory.py` and `src/popoto/fields/constants.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `TRAJECTORY_CLUSTER_THRESHOLD` | 3 | [2, 10] | Not swept (structural) |

`TRAJECTORY_CLUSTER_THRESHOLD` is the minimum number of episodes in a fingerprint group before `crystallize()` will upsert a `pattern_model` record. Lower values produce patterns earlier but with less evidence; higher values require more data before a pattern is trusted. This constant participates in project-wide tuning sweeps via `tests/benchmarks/test_defaults_sync.py`.

### SubconsciousMemory (Tier 4)

Source: `src/popoto/recipes/subconscious_memory.py`

These recipe-layer constants control the SubconsciousMemory pipeline (extraction, injection, scoring). They are evaluated through Tier 4 experiments that run multi-turn simulations across three agent scenarios (support agent, coding assistant, research agent).

| Constant | Default | Location | Role |
|----------|---------|----------|------|
| `DEFAULT_EXTRACTION_MIN_LENGTH` | 10 | `subconscious_memory.py` | Minimum character length for a sentence to be saved as a memory |
| `max_items` | 10 | Constructor arg | Maximum memory records injected per turn |
| `max_tokens` | 4000 | Constructor arg | Token budget for injected context (enforced; see [Token Budget Semantics](../features/context-assembler.md#token-budget-semantics)) |
| `default importance` | 0.5 | `extract_memories()` arg | Importance score assigned to newly extracted memories on the **default path only**. Ignored entirely when `auditable_extraction=` is set — see [Auditable Extraction](../features/auditable-extraction.md) |
| `score_weights` | (user-provided) | Constructor arg | Weight dict for ContextAssembler composite scoring |

Tier 4 also re-evaluates `_wf_min_threshold`, `_wf_priority_threshold`, and `initial_confidence` at the recipe layer to detect emergent interaction effects that field-level sweeps (Tiers 1-3) cannot observe.

### Auditable Extraction (M3)

Source: `src/popoto/fields/constants.py`, consumed by `src/popoto/extraction/decision_log.py`

| Constant | Default | Optimal Range | Sensitivity |
|----------|---------|--------------|-------------|
| `M3_ASSEMBLY_CLAIM_TTL_MS` | 30,000 | Not swept (liveness bound, not a quality knob) | Not swept (structural) |

`M3_ASSEMBLY_CLAIM_TTL_MS` bounds the `SET NX PX` assembly claim
(`popoto:m3:claim:{agent_id}:{turn_id}:{candidate_id}`) that closes a TOCTOU
window between the candidate-identity probe and the journal append — see
[Auditable Extraction](../features/auditable-extraction.md#the-atomic-assembly-claim).
It has no effect on extraction precision/recall, so it does not participate
in the Tier 1-4 quality sweeps below; it only bounds how long a crashed
runner can hold a candidate before a retry is free to claim it instead.

The decision log's detail rows themselves are **unbounded** — there is no
retention/cap constant for them, deliberately (see
[Retention policy](../features/auditable-extraction.md#retention-policy)).
Do not add one here without a corresponding decision recorded in the M3
plan; the right horizon isn't knowable until the M9 follow-on
([#568](https://github.com/tomcounsell/popoto/issues/568)) consumes the log
at scale.

## Cliff Effects

Two constants showed cliff effects in the full sweep (648 evaluations across all tiers):

**`ACTED_CYCLE_STRENGTHEN_FACTOR`**: Values below 1.0 cause a 23% drop in nDCG@5 for the temporal scheduling scenario. When the strengthen factor is < 1.0, acted outcomes actually weaken cycle amplitude instead of strengthening it, causing the system to suppress recurring tasks that should be reinforced.

**Recommendation**: Keep this constant at >= 1.0. The default of 1.2 is well within the safe zone.

**`default_importance`**: Values at or below 0.1 cause a total nDCG collapse (drop of 1.0) when transitioning from 0.1 to 0.3. Memories saved with near-zero importance are effectively invisible to retrieval, starving the pipeline of usable context.

**Recommendation**: Keep this at >= 0.3. The default of 0.5 provides a safe margin.

## Interaction Effects

Five pairwise interactions were tested:

1. **`decay_rate` x `initial_confidence`**: No interaction. Both constants are insensitive independently and together.

2. **`_wf_min_threshold` x `initial_weight`**: No interaction. Write filter threshold and co-occurrence initial weight operate independently.

3. **`ACTED_CONFIDENCE_SIGNAL` x `ACTED_CYCLE_STRENGTHEN_FACTOR`**: **Strong interaction**. When strengthen factor < 1.0, nDCG drops to 0.31 regardless of the confidence signal value. Above 1.0, both constants are insensitive.

4. **`TD_ALPHA` x `TD_GAMMA`**: No interaction. These RL constants do not affect retrieval quality in the benchmark scenarios.

5. **`_wf_min_threshold` x `_wf_priority_threshold`**: No interaction. Both operate independently.

## Methodology

### Benchmark Harness

The benchmark harness (`tests/benchmarks/`) includes:

**Tiers 1-3 (Field-Level Scenarios):**

- **Factual Recall**: 13 facts with varying importance, queried via `composite_score`. Measures whether high-importance facts rank first.
- **Multi-Step Reasoning**: 4-item reasoning chain + 5 distractors, linked via `CoOccurrenceField`. Measures whether chain items are retrieved together.
- **Temporal Scheduling**: 8 recurring tasks with `CyclicDecayField`, some recently acted on. Measures whether un-acted tasks surface above recently-acted ones.

**Tier 4 (Recipe-Layer Scenarios):**

- **Support Agent**: 25-turn customer support conversation with high redundancy and temporal importance gradient. Tests extraction noise filtering and importance ranking.
- **Coding Assistant**: 30-turn design discussion with contradictions and cross-references. Tests whether observation feedback correctly demotes superseded decisions.
- **Research Agent**: 5 source documents with corroborated and contradicted facts. Stress-tests extraction and write filter behavior across varied source quality.

Tier 4 scenarios use fixture data (JSON files in `tests/benchmarks/fixtures/`) with pre-labeled sentences to provide deterministic, reproducible benchmarks without LLM calls.

### Metrics

**Retrieval metrics (Tiers 1-4):**

- **Precision@k**: Fraction of top-k results that are relevant
- **nDCG@k**: Normalized discounted cumulative gain (rank-sensitive)
- **Calibration Error**: ECE between predicted confidence and actual outcomes
- **MRR**: Mean reciprocal rank of first relevant result

**Recipe-layer metrics (Tier 4 only):**

- **Extraction F1**: Precision and recall of extracted sentences against ground-truth labels (meaningful vs noise)
- **Token Utilization Ratio**: Fraction of token budget spent on above-median relevance memories
- **Importance Distribution Health**: Standard deviation and distinct rank count of importance scores after multi-turn simulation

### Sweep Design

Each constant was swept independently while holding others at defaults. Grid sizes ranged from 4 to 7 values per constant. All scenarios were evaluated per grid point. A full sweep across all 4 tiers with interactions runs ~648 evaluations in ~5 seconds.

Tier 4 adds 8 experiments covering SubconsciousMemory-layer constants across 3 recipe-layer scenarios, plus pairwise interaction sweeps for 4 constant pairs.

### Parametric Scenarios (Tiers 1-3)

The `--parametric` flag replaces hand-crafted scenarios with 50 generated stress-test scenarios from `ScenarioFactory`. Each scenario is built from a `ScenarioSeed` with 7 axes: record count (5-100), importance distribution shape (uniform, clustered, bimodal, exponential, flat), access pattern (all_recent, half_stale, mostly_stale, interleaved), outcome frequency, noise ratio, link density, and age spread. Larger record counts and clustered distributions force constants to break ties, exposing sensitivity that the 3 hand-crafted scenarios (with 8-13 records each) cannot detect.

### Ratchet Loop

The `--ratchet` flag runs an automated pipeline that sweeps all Tier 1-3 constants on a 70% train split of generated scenarios, validates proposed optimal values on the held-out 30%, checks cliff safety margins (10% buffer), and produces a human-readable diff proposal. The ratchet never writes to `constants.py` directly -- it outputs accept/reject recommendations for human review.

## Running the Benchmarks

```bash
# Run all sweeps (Tiers 1-4) with hand-crafted scenarios
python -m tests.benchmarks.run_sweeps --tier all --interactions

# Run field-level sweeps only (Tiers 1-3)
python -m tests.benchmarks.run_sweeps --tier 1
python -m tests.benchmarks.run_sweeps --tier 2
python -m tests.benchmarks.run_sweeps --tier 3

# Run with parametrically generated scenarios (Tiers 1-3)
python -m tests.benchmarks.run_sweeps --parametric --tier all
python -m tests.benchmarks.run_sweeps --parametric --tier 1

# Run the ratchet pipeline (generate, sweep, validate, propose)
python -m tests.benchmarks.run_sweeps --ratchet

# Run recipe-layer sweeps (Tier 4 -- SubconsciousMemory experiments)
python -m tests.benchmarks.run_sweeps --tier 4

# Run Tier 4 with interaction effect analysis
python -m tests.benchmarks.run_sweeps --tier 4 --interactions

# Run just the harness tests
pytest tests/benchmarks/test_harness.py -x -q

# Run sweep engine tests
pytest tests/benchmarks/test_sweep.py -x -q

# Run Tier 4 scenario and metrics tests
pytest tests/benchmarks/test_tier4.py -x -q

# Run parametric scenario tests
pytest tests/benchmarks/test_factory.py tests/benchmarks/test_split.py tests/benchmarks/test_ratchet.py -x -q
```

Results are saved to `tests/benchmarks/results/sweep_YYYYMMDD_HHMMSS.json` with a `latest.json` symlink pointing to the most recent run. Each result file includes performance metadata (p50/p95/p99 query durations, wall-clock time, platform info).
