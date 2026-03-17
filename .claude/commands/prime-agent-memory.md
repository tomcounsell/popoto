Load full context for building Popoto Agent Memory primitives. Run this before planning or building any agent-memory labeled issue.

## What is Popoto Agent Memory?

A set of 14 ORM primitives that give AI agents programmable memory — records that decay over time, strengthen through use, track confidence, form associations, and surface the right context at the right moment. Each primitive is an independently useful Redis-backed field type, mixin, or query method.

## Step 1: Read the feature doc

Read `docs/features/agent-memory.md` for the full primitives overview, API sketches, and design principles.

## Step 2: Read the implementation roadmap

Read `docs/references/popoto-memory-roadmap.md` for the 14-step implementation plan with:
- Exact data structures and Lua scripts per primitive
- Synergy tests between primitives (combinatorial test matrix)
- Measurable agent improvement benchmarks per step
- Naming conventions (CS terminology, not neuroscience)

## Step 3: Read the research foundations

Skim these for design rationale — don't memorize, but understand the "why" behind each primitive:
- `docs/references/epistemic-flow-cognitive-agent-architectures.md` — how cognitive primitives compose into agent architectures
- `docs/references/programmable-memory-systems-neuroscience-design-spec.md` — neuroscience grounding for decay, confidence, and association algorithms

## Step 4: Understand the extension points

Read these files to understand how Popoto fields and queries work — every new primitive must follow these patterns:
- `src/popoto/fields/sorted_field_mixin.py` — the pattern DecayingSortedField subclasses. Study `on_save()`, `on_delete()`, `filter_query()`, `get_filter_query_params()`, and `partition_by`.
- `src/popoto/fields/field.py` — base Field class, hook signatures, mixin composition pattern
- `src/popoto/models/base.py` — Model.save() and Model.delete() call field hooks. Search for `atomic_increment` to see the established Lua scripting pattern.
- `src/popoto/models/query.py` — QueryBuilder and how `computed_sort()` works (the pattern `top_by_decay()` extends)
- `src/popoto/fields/existence_filter.py` — ExistenceFilter and FrequencySketch. Example of implementing probabilistic data structures (Bloom filter, Count-Min Sketch) with pure Lua + core Redis commands.

## Step 5: Review shipped primitives

10 of 14 primitives have shipped. Key implementation files:
- `src/popoto/fields/decaying_sorted_field.py` — DecayingSortedField (PR #199)
- `src/popoto/fields/cyclic_decay_field.py` — CyclicDecayField (PR #201, not yet confirmed)
- `src/popoto/fields/access_tracker.py` — AccessTrackerMixin (PR #203)
- `src/popoto/fields/observation_protocol.py` — ObservationProtocol (PR #206, not yet confirmed)
- `src/popoto/fields/write_filter.py` — WriteFilterMixin (PR #214)
- `src/popoto/fields/confidence_field.py` — ConfidenceField (PR #215)
- `src/popoto/fields/co_occurrence_field.py` — CoOccurrenceField (PR #218)
- `src/popoto/fields/event_stream_mixin.py` — EventStreamMixin (shipped, not yet confirmed)
- `src/popoto/models/query.py` — CompositeScoreQuery via `composite_score()` (PR #222)
- `src/popoto/fields/existence_filter.py` — ExistenceFilter + FrequencySketch (PR #225)

Check `tests/test_lua_decay_scoring.py` for the validated Lua decay formula and test patterns.

## Step 6: Check current progress

```bash
gh issue list --label agent-memory --state all --repo tomcounsell/popoto --json number,title,state,url
```

Review open issues and merged PRs to understand what's already shipped vs. in progress.

## Key design decisions (already settled)

- **DecayingSortedField** subclasses `SortedFieldMixin` — score is always a timestamp, inherits partition_by
- **Lua scripts compute decay at query time** (Option C) — store raw data, compute on read
- **`base_score_field`** parameter with default 1.0, overridable at query time via `top_by_decay()`
- **`decay_rate`** parameter with default 0.5, overridable at query time
- **`touch()` method** for timestamp refresh without full save
- **CS terminology throughout** — not neuroscience. See naming conventions table in the roadmap.
- **No Redis module dependencies** — all features use core Redis commands + Lua scripts for Valkey compatibility. ExistenceFilter uses SETBIT/GETBIT + Lua (not RedisBloom BF.*). FrequencySketch uses HINCRBY/HGET + Lua (not CMS.*).

## Constraints

- Must not break existing `SortedField` behavior — new subclasses, not modifications
- Every operation must accept optional `pipeline` parameter for atomic execution
- Every primitive must be independently testable and useful
- Redis-native everything — no external brokers, no Celery, no Redis modules
- **Valkey compatible** — only core Redis commands + Lua scripts. No BF.*, CMS.*, FT.*, JSON.* or any module commands
- Follow existing Popoto code style: black formatting, 88 char lines

## Remaining work (4 primitives)

- **PredictionLedger** — outcome tracking, prediction-outcome pairs, auto-resolution
- **StreamConsumer** — background processing framework for Redis Streams consumer groups
- **PolicyCache** — learned action selection from crystallized state-action-outcome patterns
- **ContextAssembler** — retrieval-to-injection bridge, assembles LLM-ready context within token budgets

## Downstream consumer

The [Behavioral Episode Memory System](https://github.com/tomcounsell/ai/issues/376) in the AI project builds `CyclicEpisode` and `ProceduralPattern` models on top of these primitives.
