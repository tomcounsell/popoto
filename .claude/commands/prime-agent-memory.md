Load full context for building Popoto Agent Memory primitives. Run this before planning or building any agent-memory labeled issue.

## What is Popoto Agent Memory?

A set of 12 ORM primitives that give AI agents programmable memory — records that decay over time, strengthen through use, track confidence, form associations, and surface the right context at the right moment. Each primitive is an independently useful Redis-backed field type, mixin, or query method.

## Step 1: Read the feature doc

Read `docs/features/agent-memory.md` for the full primitives overview, API sketches, and design principles.

## Step 2: Read the implementation roadmap

Read `docs/references/popoto-memory-roadmap.md` for the 12-step implementation plan with:
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

## Step 5: Review completed prerequisites

These PRs established patterns this work builds on:
- PR #190: `atomic_increment()` — Lua scripting pattern with `cmsgpack`, pipeline support
- PR #191: `ListField(max_length=N)` — new field type with separate Redis data structure
- PR #189: `computed_sort()` — QueryBuilder extension pattern

Check `tests/test_lua_decay_scoring.py` for the validated Lua decay formula and test patterns.

## Step 6: Check current progress

```bash
gh issue list --label agent-memory --state all --repo tomcounsell/popoto --json number,title,state,url
```

Review open issues and merged PRs to understand what's already shipped vs. in progress.

## Key design decisions (already settled)

- **DecayingSortedField** subclasses `SortedFieldMixin` — score is always a timestamp, inherits partition_by
- **Lua scripts compute decay at query time** (Option C) — store raw data, compute on read. Leave review notes about future background recomputation when a worker exists (Steps 6/10).
- **`base_score_field`** parameter with default 1.0, overridable at query time via `top_by_decay()`
- **`decay_rate`** parameter with default 0.5, overridable at query time
- **`touch()` method** for timestamp refresh without full save
- **`on_read()` hook** deferred to Step 2 (AccessTracker)
- **CS terminology throughout** — not neuroscience. See naming conventions table in the roadmap.

## Constraints

- Must not break existing `SortedField` behavior — new subclasses, not modifications
- Every operation must accept optional `pipeline` parameter for atomic execution
- Every primitive must be independently testable and useful
- Redis-native everything — no external brokers, no Celery
- Follow existing Popoto code style: black formatting, 88 char lines

## Downstream consumer

The [Behavioral Episode Memory System](https://github.com/tomcounsell/ai/issues/376) in the AI project builds `CyclicEpisode` and `ProceduralPattern` models on top of these primitives.
