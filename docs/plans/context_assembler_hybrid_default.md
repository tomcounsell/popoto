---
status: Planning
type: feature
appetite: Medium
owner: Valor
created: 2026-05-18
tracking: https://github.com/tomcounsell/popoto/issues/395
last_comment_id:
---

# Default ContextAssembler to Hybrid Retrieval (BM25 + Vector + Graph via RRF)

## Problem

`ContextAssembler` is the headline retrieval recipe in Popoto Agent Memory.
PR #306 shipped the two primitives needed for hybrid retrieval —
`BM25Field` (lexical) and `CompositeScoreQuery.fuse()` (RRF) — but the
recipe itself still drives a single weighted-sum `composite_score()` over
`score_weights`. Users who want hybrid ranking must wire BM25 + vector +
graph and call `fuse()` by hand, and most won't.

The upstream reference (agentmemory, 95.2% R@5 on LongMemEval-S) shows
that RRF fusion across heterogeneous signals is the single biggest
contributor to SOTA retrieval. Our defaults shape what users get; if
hybrid retrieval isn't the default, it's effectively absent from the
product.

**Current behavior:**
- `ContextAssembler.__init__(model_class, score_weights, ...)` requires
  a `score_weights` dict and runs a single `query.composite_score(
  indexes=self.score_weights, ...)` in `_pull_path`
  (`src/popoto/recipes/context_assembler.py:691`, `:877`).
- Capability detection at `__init__` notices `ExistenceFilter`,
  `CoOccurrenceField`, `CyclicDecayField`, `ConfidenceField`,
  `DecayingSortedField` — but **not** `BM25Field` or `EmbeddingField`.
- `CompositeScoreQuery.fuse()` exists and is well-tested
  (`src/popoto/models/query.py:894`), but no recipe consumes it.

**Desired outcome:**
- A model with `BM25Field` + `EmbeddingField` (+ optional
  `CoOccurrenceField`) gets hybrid RRF retrieval **by default** when
  the user instantiates `ContextAssembler`.
- Models without those fields fall back transparently to today's
  composite path — no migration required.
- A user can pin behavior with `retrieval_mode="hybrid" | "composite"
  | "auto"` (default `"auto"`).
- The change ships with a measurable R@5 / MRR delta on LongMemEval-S
  and LoCoMo, produced by the harness from #394, committed alongside
  the PR. If hybrid does not beat composite, the **default is not
  flipped** — the mode is exposed but `auto` continues to pick
  composite.

## Freshness Check

**Baseline commit:** `10cf580e35102d19f568c72708930b96345cd10e`
**Issue filed at:** 2026-05-17T23:11:26Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:691` — `__init__` accepts
  `score_weights` — still holds.
- `src/popoto/recipes/context_assembler.py:877` — `_pull_path` calls
  `composite_score(indexes=self.score_weights, ...)` — still holds.
- `src/popoto/models/query.py:894` — `CompositeScoreQuery.fuse()`
  defined with RRF — still holds.
- `src/popoto/models/query.py:2220` — second `fuse()` entry point
  exists and matches issue description.
- `src/popoto/fields/bm25_field.py:487` — `BM25Field.search(
  cls, model_class, field_name, query_text, limit=10)` — still holds.

**Cited sibling issues/PRs re-checked:**
- #394 — Benchmark harness — **OPEN**. This plan declares a hard
  dependency on it landing first.
- #396 — Memory lifecycle — **OPEN**. Downstream of this plan; no
  current entanglement.
- #304 / PR #306 — Hybrid retrieval primitives — **MERGED** 2026-03-30.
  `BM25Field` and `fuse()` confirmed present.

**Commits on main since issue was filed (touching referenced files):**
- None. `git log --since="2026-05-17T23:11:26Z"` on the four target
  files returns nothing. The issue is fresh.

**Active plans in `docs/plans/` overlapping this area:**
- `docs/plans/hybrid_retrieval.md` — tracks #304 (primitives). Status
  In Progress, but PR #306 merged 2026-03-30. **No overlap** with this
  plan's scope; that plan ends where this one begins.
- `docs/plans/context_assembler.md` — original assembler recipe plan.
  No overlap; this plan additively extends the assembler, does not
  rewrite it.

## Prior Art

- **Issue #304 / PR #306 (merged 2026-03-30)** — "Hybrid retrieval:
  BM25 + RRF fusion." Shipped `BM25Field` and `CompositeScoreQuery
  .fuse()`. **Foundational dependency.** This plan consumes those
  primitives directly.
- **`docs/plans/hybrid_retrieval.md`** — plan for #304. Documents the
  RRF math (`score(d) = sum(1 / (k + rank_i))`), the choice of `k=60`
  (Cormack et al. default), and the tokenization rules reused from
  `ExistenceFilter`. This plan inherits all those decisions.
- **Issue #394 (open)** — Benchmark harness. **Hard prerequisite** —
  without it, the "measurable delta" acceptance criterion cannot be
  satisfied. See Prerequisites.
- **`CompositeScoreQuery.semantic_search()`** (`models/query.py:640`)
  — pre-existing path that embeds a query, computes cosine similarity,
  and injects via `similarity_boost`. We will reuse this as the
  "vector retriever" in hybrid mode rather than reinvent vector
  retrieval.

No prior failed attempts to default to hybrid retrieval — this is the
first.

## Research

External research is unnecessary for this plan. The technical
substrate (RRF math, BM25 implementation, vector path, capability
detection pattern) is all in-repo and was researched in depth for
#304 / `docs/plans/hybrid_retrieval.md`. The agentmemory reference
cited in the issue body is the only external input needed, and it has
already informed the design.

No relevant external findings — proceeding with codebase context.

## Data Flow

For a model defined as:

```python
class Memory(popoto.Model):
    key = popoto.AutoKeyField()
    content = popoto.ContentField()
    content_bm25 = popoto.BM25Field(source="content")
    embedding  = popoto.EmbeddingField(source="content")
    relevance  = popoto.DecayingSortedField(...)
    confidence = popoto.ConfidenceField()
    cooccur    = popoto.CoOccurrenceField(...)
```

calling `ContextAssembler(model_class=Memory, score_weights=...)` with
the new code path:

1. **Entry**: caller invokes `assembler.assemble(query_cues={"topic":
   "deploy"}, agent_id="a-1")`.
2. **`__init__` capability detection** (one-time, at construction):
   walks `model_class._meta.fields` and detects `BM25Field` (records
   `_bm25_field_name`) and `EmbeddingField` (records
   `_embedding_field_name`) **in addition to** the five fields it
   already detects.
3. **Mode resolution**: `_resolved_mode` is computed at `__init__`:
   - `retrieval_mode="hybrid"` → require BM25 + Embedding, raise on
     missing.
   - `retrieval_mode="composite"` → use today's path, ignore detected
     BM25 / Embedding.
   - `retrieval_mode="auto"` (default) → `"hybrid"` if both BM25 and
     Embedding present **and** the bench-gate flag (see Risks) is
     enabled; else `"composite"`.
4. **Pull path dispatch** (`_pull_path` → either `_pull_path_composite`
   [renamed from current body] or new `_pull_path_hybrid`).
5. **`_pull_path_hybrid`** runs three retrievers, each returning a
   `list[(redis_key, score)]`:
   - **Lexical**: `BM25Field.search(model_class, field_name,
     query_text, limit=hybrid_candidate_pool)` for each query cue.
     Cues are joined into a single query string per call (or one call
     per cue with results pre-merged — see Spike spike-2).
   - **Vector**: use the model's embedding provider via
     `EmbeddingField` directly. Build a `(redis_key, similarity)` list
     using the same matmul pattern as `semantic_search()` lines 711–736
     — but **without** invoking `composite_score`, since we want the
     raw ranked list for RRF, not pre-fused output.
   - **Graph (optional)**: if `CoOccurrenceField` is present, run a
     low-cost first-pass to get seeds (BM25 top-K), then
     `co_occurrence_field.propagate(seed_pks, depth=propagation_depth)`
     → cast `{pk: weight}` dict to a `(pk, weight)` list sorted desc.
6. **Fusion**: `model_class.query.fuse(k=60, limit=max_items*2,
   keyword=lexical_list, semantic=vector_list, graph=graph_list)`
   → returns hydrated model instances ranked by RRF score.
7. **Filters**: hybrid path applies `partition_filters` post-fusion by
   discarding non-matching instances (cheaper than pre-filtering each
   retriever; we can revisit if N becomes large — see Rabbit Holes).
8. **Output**: feed `pull_records` back into the existing
   `assemble()` flow at the same point the composite path returns
   them (line 778). Push path, dedup, budget, post-effects, format —
   all unchanged.

The change is **scoped to `_pull_path`**. Push path, merge,
deduplication, budgeting, post-effects, and formatting are
untouched, and `score_weights` semantics for `composite` mode are
unchanged.

## Spike Results

### spike-1: Verify `CompositeScoreQuery.fuse()` accepts heterogeneous ranked lists with arbitrary keyword names
- **Assumption**: "`fuse(**ranked_lists)` accepts any keyword (`keyword=`,
  `semantic=`, `graph=`) and ranks consistently."
- **Method**: code-read
- **Finding**: Confirmed at `query.py:894–968`. Signature is
  `fuse(self, k=60, limit=10, post_filter=None, **ranked_lists)`.
  Keywords are arbitrary; each value must be `list[(redis_key,
  score)]`. RRF is rank-based — the actual score in each tuple is
  discarded, only ordering matters. **Confidence: high.**
- **Impact**: No new wrapper around `fuse()` is needed; the assembler
  passes named ranked lists directly. The graph retriever's
  `{pk: weight}` dict must be converted to a sorted `(pk, weight)`
  list — straightforward.

### spike-2: Determine how to handle multi-cue query strings against BM25
- **Assumption**: "BM25 search on multiple cues should join cues into
  one query string rather than run per-cue."
- **Method**: code-read
- **Finding**: `BM25Field.search(model_class, field_name, query_text,
  limit=10)` takes a single string. Multi-cue handling has two options:
  - (a) join cue values with spaces → single search call → BM25
    naturally weights by term overlap. Simplest, matches how users
    think of "all these terms."
  - (b) one search per cue, merge ranked lists into one before
    passing to `fuse()`. More complex and **double-counts** when
    `fuse()` already does rank fusion.
  Option (a) is correct. **Confidence: high.**
- **Impact**: `_pull_path_hybrid` joins `query_cues.values()` with
  spaces to form the lexical query. Empty cues are skipped.

### spike-3: Confirm `EmbeddingField` has no public similarity API and we must reuse `semantic_search`'s internals or compute matmul directly
- **Assumption**: "There is no `EmbeddingField.search()` — vector
  retrieval lives inside `CompositeScoreQuery.semantic_search()` and we
  must extract the matmul logic for our use."
- **Method**: code-read
- **Finding**: Confirmed.
  `grep -n "def " embedding_field.py` shows no `search` / `similarity`
  / `find_similar` method on the field. The matmul lives in
  `models/query.py:710–736` inside `semantic_search`. Pulling out a
  small private helper `_vector_ranked_list(model_class, query_text,
  limit)` that returns `list[(redis_key, similarity)]` is the right
  shape. **Confidence: high.**
- **Impact**: Plan introduces `popoto.models.query._vector_ranked_list`
  (module-private) extracted from `semantic_search`'s lines 681–737.
  `semantic_search` continues to call it internally, so its public
  behavior is unchanged. `_pull_path_hybrid` calls it directly.

### spike-4: Validate that `auto` mode default is safe to ship without #394's measurable delta
- **Assumption**: "We can ship `retrieval_mode='auto'` with a
  conservative gate that **does not** enable hybrid until #394's
  benchmark proves a positive delta, so users see no behavior change
  until evidence justifies it."
- **Method**: code-read + plan-doc gating design
- **Finding**: Gating cleanly via an internal flag `_HYBRID_AUTO_DEFAULT
  = False` constant in the recipe module (or `Defaults.HYBRID_AUTO`).
  When `False`, `auto` resolves to `"composite"` regardless of
  detected capabilities; opt-in users pass `retrieval_mode="hybrid"`
  explicitly. The constant flips to `True` in a follow-up commit in
  the **same PR** if and only if the #394 benchmark shows R@5 / MRR
  improvement on LongMemEval-S **and** LoCoMo. **Confidence: high.**
- **Impact**: Ship is decoupled from benchmark outcome — the
  *feature* lands always; the *default flip* is conditional. Removes
  the dependency on #394 to merge anything; only the default-flip
  commit depends on it.

## Architectural Impact

- **New dependencies**: none. All primitives in-repo.
- **Interface changes**:
  - `ContextAssembler.__init__` gains `retrieval_mode: str = "auto"`
    kwarg. Existing positional/kwarg call sites unchanged.
  - `score_weights` becomes optional in `"hybrid"` mode (used for
    push-path `CompositeScoreQuery` if push path uses it; pull path
    doesn't read it in hybrid mode). It remains required in
    `"composite"` mode.
  - One new module-private helper `_vector_ranked_list` in
    `models/query.py`.
- **Coupling**: marginally increased — assembler now imports
  `BM25Field` and `EmbeddingField` for `isinstance` checks. Already
  imports five other fields, so this is incremental, not new.
- **Data ownership**: unchanged.
- **Reversibility**: high. The hybrid path is additive. Removing the
  feature is a revert of one file plus the helper extraction.

## Appetite

**Size:** Medium

**Team:** Solo dev (builder), code reviewer, test engineer.

**Interactions:**
- PM check-ins: 1 (after benchmark numbers from #394 are in, to
  decide whether to flip the `auto` default).
- Review rounds: 1.

Medium because: the change spans recipe + query module + tests +
docs, and gates on an external dependency (#394). Coding is small,
but coordination with #394 and the benchmark gate adds overhead.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| `BM25Field` available | `python -c "from popoto.fields.bm25_field import BM25Field"` | Lexical retrieval primitive shipped (PR #306) |
| `CompositeScoreQuery.fuse()` available | `python -c "from popoto.models.query import CompositeScoreQuery; assert hasattr(CompositeScoreQuery, 'fuse')"` | RRF fusion primitive shipped (PR #306) |
| `EmbeddingField` available | `python -c "from popoto.fields.embedding_field import EmbeddingField"` | Vector retrieval primitive |
| #394 benchmark harness available | `python -c "import tests.benchmarks.run_external" 2>/dev/null` | **Required to flip the `auto` default** — not required to ship the mode itself |
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test execution |

The default-flip commit explicitly depends on #394; the feature
itself does not block on #394.

## Solution

### Key Elements

- **`retrieval_mode` parameter**: new `__init__` kwarg (`"hybrid"` /
  `"composite"` / `"auto"`, default `"auto"`).
- **Extended capability detection**: detect `BM25Field` and
  `EmbeddingField` alongside existing fields.
- **Dispatch in `_pull_path`**: route to `_pull_path_composite`
  (today's logic, renamed) or new `_pull_path_hybrid`.
- **`_pull_path_hybrid`**: runs lexical + vector + (optional) graph
  retrievers, calls `query.fuse()` with RRF (`k=60`), applies
  partition filters, returns ranked records.
- **`_vector_ranked_list` helper**: small module-private extraction
  from `semantic_search`'s internals (in `models/query.py`).
- **`Defaults.HYBRID_AUTO`** (or equivalent): gate flag for whether
  `auto` resolves to hybrid. Ships `False`; flips to `True` only
  after #394 benchmarks show positive delta.
- **Backward compatibility**: any caller using
  `ContextAssembler(model_class, score_weights={...})` works
  unchanged. No `DeprecationWarning` — composite mode is a
  first-class, supported choice.

### Flow

Construct → assemble → pull-path dispatch → (hybrid: BM25 + vector
+ graph → RRF fuse → filter) OR (composite: composite_score) →
merge with push path → budget → format → return.

### Technical Approach

- `_pull_path_hybrid` mirrors the existing `_pull_path` *shape* —
  same return type (`(records, all_candidates)`), same
  `ExistenceFilter` pre-check at the top, same graceful empty-cue
  handling — so the rest of `assemble()` is untouched.
- RRF constant `k=60` (Cormack et al.; matches agentmemory). Exposed
  as `rrf_k` constructor kwarg for power users; default constant
  lives in `Defaults` for tunability via the sweep harness.
- Candidate pool: each retriever requests `max_items * 5` (matching
  agentmemory's "wide net + fuse + trim"). Exposed as
  `hybrid_candidate_pool` kwarg, default 50 for `max_items=10`.
- Graph signal in hybrid mode keeps the current two-pass behavior:
  first-pass BM25 seeds → propagate → graph ranked list → fuse with
  lexical + vector. No second `composite_score` call.
- Per-signal RRF weights are **not** exposed in v1 — equal weighting
  via standard RRF. Add as `rrf_weights` kwarg in a follow-up only
  if benchmarks demand it.
- The five-line "all retrievers returned empty → return [], []"
  short-circuit is preserved.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `_pull_path_hybrid` wraps each retriever (BM25, vector, graph)
  in a narrow `try/except Exception as e: logger.warning(...)` block.
  Each handler is covered by a test that monkeypatches the retriever
  to raise and asserts (a) the assembler returns degraded results,
  (b) a `logger.warning` is emitted.
- [ ] Existing `_pull_path` (renamed `_pull_path_composite`) — no
  change to its exception handling; existing tests still cover it.

### Empty/Invalid Input Handling
- [ ] `query_cues=None` → no pull path runs (existing assert).
- [ ] `query_cues={}` → no pull path runs.
- [ ] `query_cues={"topic": ""}` → BM25 query string is empty after
  join → retriever returns `[]` → fusion returns `[]` → assembler
  returns push-path-only result. Test added.
- [ ] Model with `BM25Field` but no `EmbeddingField` and
  `retrieval_mode="hybrid"` → raise `ValueError` at `__init__` with
  actionable message. Test added.
- [ ] Model with neither BM25 nor Embedding and
  `retrieval_mode="auto"` → falls back to composite. Test added.

### Error State Rendering
- [ ] When all hybrid retrievers fail, `AssemblyResult.records` is
  `[]`, `metadata["pull_count"] == 0`, push path still runs. Test
  added; verifies no exception leaks to caller.

## Test Impact

- [ ] `tests/recipes/test_context_assembler.py` (all existing tests)
  — UPDATE: nothing to change in behavior since `auto` defaults to
  composite while `HYBRID_AUTO=False`, but add an explicit assertion
  somewhere that `ContextAssembler(model_class, score_weights={...})`
  still works without passing `retrieval_mode`.
- [ ] `tests/recipes/test_context_assembler.py::test_assemble_*` —
  UPDATE: spot-check that `metadata` shape is unchanged when
  `retrieval_mode="composite"`.
- [ ] `tests/recipes/test_context_assembler_hybrid.py` (NEW) — full
  coverage of the new path: capability detection, mode resolution,
  hybrid pull path with all three retrievers, hybrid pull path
  missing graph, hybrid pull path retriever-failure paths,
  `ValueError` on missing required fields in `"hybrid"`, RRF
  `k`/pool overrides via constructor.
- [ ] `tests/test_query_fuse.py` (existing for #304) — no change;
  this plan does not modify `fuse()`.

No existing tests removed.

## Rabbit Holes

- **Don't refactor `_pull_path`'s composite branch.** Rename body to
  `_pull_path_composite` and stop. No "while we're here" cleanup.
- **Don't add per-signal RRF weights.** Equal weighting is the
  Cormack default and the agentmemory baseline. Tunable weighting is
  a separate optimization the benchmark may motivate later.
- **Don't add a new vector index or embedding provider.** Reuse the
  existing `EmbeddingField` matmul path.
- **Don't pre-filter retrievers by `partition_filters`.** Post-filter
  after fusion. Pre-filtering each retriever is 3x the work for
  marginal candidate-pool savings.
- **Don't auto-tune `k` or the candidate pool.** Constants for v1;
  sweep harness can tune in a separate issue.
- **Don't refactor `semantic_search`.** Extract `_vector_ranked_list`
  as a small private helper and have `semantic_search` call it. No
  changes to `semantic_search`'s public surface.

## Risks

### Risk 1: `auto` defaults to hybrid prematurely, regressing real users
**Impact:** Quietly worse retrieval for users with BM25 + vector models
who weren't expecting a mode flip.
**Mitigation:** `Defaults.HYBRID_AUTO=False` ships with this PR.
`auto` resolves to `"composite"` until a follow-up commit (same PR
or sibling PR) flips the flag based on #394 numbers. Opt-in users
get hybrid via explicit `retrieval_mode="hybrid"` from day one.

### Risk 2: Hybrid mode breaks on models with `BM25Field` but no `EmbeddingField` (or vice versa)
**Impact:** `ContextAssembler(..., retrieval_mode="hybrid")` blows up
in `_pull_path_hybrid` with a confusing error.
**Mitigation:** Validate at `__init__`. If `retrieval_mode="hybrid"`
and any required field is missing, raise `ValueError` with the
missing-field name and a hint to use `retrieval_mode="auto"` or
`"composite"`.

### Risk 3: #394 is not landed by the time this PR is ready
**Impact:** Cannot satisfy the "measurable R@5 / MRR delta"
acceptance criterion.
**Mitigation:** Decouple the *feature* (modes + dispatch) from the
*default flip*. Feature lands without benchmarks; default flip is a
~5-line follow-up commit gated on the bench numbers.

### Risk 4: Performance regression in hybrid mode (3 retrievers + matmul + fusion)
**Impact:** p95 latency exceeds composite by enough to hurt
interactive use.
**Mitigation:** Benchmark p50/p95 from #394 is part of the
default-flip gate, not just R@5/MRR. If p95 doubles, hybrid does not
auto-default even if quality improves. Latency budget is a sibling
acceptance criterion.

### Risk 5: Valkey compatibility break
**Impact:** Plan touches a path that accidentally introduces a
Redis-only command.
**Mitigation:** All work uses primitives already shipped in
Valkey-compatible form (PR #306). No new Redis commands introduced.
Reviewer checks the diff for `BF.*`, `CMS.*`, `FT.*` references.

## Race Conditions

No new race conditions introduced. The hybrid path is read-only:
issue queries, fuse ranked lists, return records. Push path and
post-effects are unchanged. `EmbeddingField.load_embeddings()` already
handles its own cache invalidation. **No race conditions identified.**

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #394] LongMemEval-S / LoCoMo benchmark harness —
  prerequisite, not in this plan.
- [SEPARATE-SLUG #396] Memory consolidation / decay lifecycle — next
  in the 3-issue sequence.
- [SEPARATE-SLUG #395] Per-signal RRF weighting and tuning — if and
  only if benchmarks motivate it, file a new issue under the same
  agent-memory label. Out of scope until evidence demands it.

## Update System

No update system changes required — this is a library-internal change.
Downstream installations re-pin Popoto on next upgrade and pick it up.

## Agent Integration

No agent integration required — this plan touches the Popoto library
itself. Any application using `ContextAssembler` gets the new
parameter automatically on upgrade with no code change required.

## Documentation

### Feature Documentation
- [ ] Update `docs/recipes.md` (or wherever `ContextAssembler` is
  documented today) with: the new `retrieval_mode` parameter, the
  capability-detection rules for `auto`, a worked example using a
  model with BM25 + Embedding + CoOccurrence fields, and a
  "when to override" guidance section.
- [ ] Add a "Hybrid retrieval" subsection covering the three-signal
  pipeline (lexical + vector + graph) and the RRF math link.

### External Documentation Site
- [ ] `mkdocs.yml` is unchanged; the recipes page already exists.
- [ ] `mkdocs build --strict` passes locally.

### Inline Documentation
- [ ] Docstrings on `ContextAssembler.__init__` updated to document
  `retrieval_mode`, capability detection, and the gating semantics
  of `auto`.
- [ ] Module docstring updated to mention hybrid mode as a supported
  pipeline.
- [ ] `CHANGELOG.md` entry: "ContextAssembler: hybrid (BM25 + vector
  + graph via RRF) retrieval mode."

## Success Criteria

- [ ] `ContextAssembler(model_class=Memory, retrieval_mode="hybrid",
  ...)` works on a model with `BM25Field` + `EmbeddingField` +
  `CoOccurrenceField`; tests pass.
- [ ] `ContextAssembler(model_class=Memory, retrieval_mode="hybrid",
  ...)` raises `ValueError` at `__init__` if BM25 or Embedding is
  missing, with the field name in the message.
- [ ] `retrieval_mode="auto"` resolves to `"composite"` while
  `Defaults.HYBRID_AUTO=False` (initial ship).
- [ ] Existing `ContextAssembler(model_class=Memory,
  score_weights={...})` constructions work unchanged — no
  `DeprecationWarning`, identical metadata shape.
- [ ] All hybrid retriever failures are caught and logged
  (`logger.warning`); the assembler returns degraded but valid
  results.
- [ ] **Benchmark gate (separate commit)**: R@5 and MRR on
  LongMemEval-S **and** LoCoMo using #394's harness, committed as
  `tests/benchmarks/results/external/...`. Hybrid vs. composite delta
  reported. If both metrics improve and p95 latency does not
  regress >2×, the default-flip commit lands; else the mode stays
  opt-in.
- [ ] No Redis-module references in the diff (`grep -E "BF\.|CMS\.
  |FT\." src/` returns nothing new).
- [ ] `pytest` passes (`/do-test`).
- [ ] `mypy src/` passes.
- [ ] `black src/ tests/` clean.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (assembler-hybrid)**
  - Name: `assembler-builder`
  - Role: Implement `retrieval_mode` plumbing in `ContextAssembler`,
    extract `_vector_ranked_list`, wire `_pull_path_hybrid`.
  - Agent Type: builder
  - Resume: true

- **Test engineer (hybrid path)**
  - Name: `hybrid-tester`
  - Role: Write `tests/recipes/test_context_assembler_hybrid.py`
    covering capability detection, mode resolution, fusion path,
    failure paths, and `ValueError` cases.
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `assembler-docs`
  - Role: Update `docs/recipes.md`, module docstring, `CHANGELOG.md`.
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `assembler-validator`
  - Role: Run full test suite, mypy, ruff/black, confirm Valkey
    compatibility (no module-only commands introduced).
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Extract `_vector_ranked_list` helper
- **Task ID**: build-vector-helper
- **Depends On**: none
- **Validates**: existing `tests/` covering `semantic_search` still pass
- **Informed By**: spike-3 (no public similarity API on EmbeddingField; helper lives in query.py)
- **Assigned To**: assembler-builder
- **Agent Type**: builder
- **Parallel**: true
- Add module-private `_vector_ranked_list(model_class, query_text,
  limit)` in `src/popoto/models/query.py`.
- Move the embedding + matmul logic from `semantic_search` lines
  ~681–737 into the helper, returning `list[(redis_key, similarity)]`
  sorted descending.
- Refactor `semantic_search` to call the helper and build its
  `similarity_boost` dict from the returned list.
- Existing `semantic_search` tests must pass unchanged.

### 2. Add `retrieval_mode` parameter and extended capability detection
- **Task ID**: build-mode-param
- **Depends On**: none
- **Validates**: new `tests/recipes/test_context_assembler_hybrid.py`
- **Informed By**: spike-1 (fuse accepts arbitrary kwargs), spike-4 (gate via `Defaults.HYBRID_AUTO=False`)
- **Assigned To**: assembler-builder
- **Agent Type**: builder
- **Parallel**: true
- Edit `src/popoto/recipes/context_assembler.py`:
  - Add `retrieval_mode: str = "auto"`, `rrf_k: int = 60`,
    `hybrid_candidate_pool: int = 50` kwargs to `__init__`.
  - Detect `BM25Field` and `EmbeddingField` in the field-walk loop;
    record `_bm25_field_name`, `_bm25_source_field_name`,
    `_embedding_field_name`.
  - Compute `self._resolved_mode` at `__init__`. Validate
    capabilities for `"hybrid"`; raise `ValueError` on missing field
    with the missing-field name in the message.
- Add `HYBRID_AUTO = False` constant to `src/popoto/fields/constants.py`
  (or `context_assembler.py` module-level, matching the existing
  `COMPETITIVE_SUPPRESSION_SIGNAL` pattern).
- Make `score_weights` optional when `retrieval_mode="hybrid"`.

### 3. Implement `_pull_path_hybrid` and dispatch
- **Task ID**: build-hybrid-pull
- **Depends On**: build-vector-helper, build-mode-param
- **Validates**: `tests/recipes/test_context_assembler_hybrid.py`
- **Informed By**: spike-1, spike-2 (join cues with spaces for BM25), spike-3
- **Assigned To**: assembler-builder
- **Agent Type**: builder
- **Parallel**: false
- Rename current `_pull_path` body to `_pull_path_composite`. Wrap
  with thin `_pull_path` dispatcher reading `self._resolved_mode`.
- Implement `_pull_path_hybrid(query_cues, filters)`:
  - ExistenceFilter pre-check (same as composite).
  - Build the lexical query string by joining
    `query_cues.values()` with spaces; skip if empty.
  - Lexical: `BM25Field.search(self.model_class, self._bm25_source_field_name,
    query_text, limit=self.hybrid_candidate_pool)` wrapped in
    try/except.
  - Vector: `_vector_ranked_list(self.model_class, query_text,
    limit=self.hybrid_candidate_pool)` wrapped in try/except.
  - Graph (only if `_co_occurrence_field` is set): seed from lexical
    top-K, `propagate(...)` → ranked list. Try/except.
  - Build `fuse_kwargs = {}`; include only non-empty lists.
  - If no signals returned anything, return `[], []`.
  - `candidates = self.model_class.query.fuse(k=self.rrf_k,
    limit=self.max_items * 2, **fuse_kwargs)`.
  - Post-filter on `partition_filters`: drop instances whose field
    values don't match.
  - Return `(candidates, list(candidates))`.

### 4. Tests for hybrid path
- **Task ID**: test-hybrid
- **Depends On**: build-hybrid-pull
- **Validates**: `pytest tests/recipes/test_context_assembler_hybrid.py -v`
- **Assigned To**: hybrid-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Create `tests/recipes/test_context_assembler_hybrid.py`. Test cases:
  - Capability detection: model with BM25 + Embedding gets
    `_resolved_mode="composite"` under `HYBRID_AUTO=False` and `auto`;
    same model with `retrieval_mode="hybrid"` gets `"hybrid"`.
  - Missing-capability error: `retrieval_mode="hybrid"` on a model
    without BM25 raises `ValueError`.
  - Full hybrid pipeline: insert N memories with content + embeddings
    + co-occurrence, query, assert results are ranked by RRF and
    differ from composite-mode results on the same data.
  - Retriever failure: monkeypatch BM25 to raise, assert
    `logger.warning` and degraded result.
  - Empty query cues: `assemble(query_cues={"topic": ""})` returns
    push-path-only result with no exception.
  - Backward compatibility: `ContextAssembler(model_class,
    score_weights={...})` returns composite results unchanged.
  - `rrf_k` and `hybrid_candidate_pool` overrides plumb through.

### 5. Update existing assembler tests
- **Task ID**: test-existing
- **Depends On**: build-hybrid-pull
- **Validates**: `pytest tests/recipes/test_context_assembler.py -v`
- **Assigned To**: hybrid-tester
- **Agent Type**: test-engineer
- **Parallel**: true
- Spot-check that all existing `ContextAssembler` tests still pass
  unchanged. Add one explicit assertion that `metadata` shape is
  identical under `retrieval_mode="composite"` vs. omitting the
  parameter entirely.

### 6. Documentation
- **Task ID**: document-feature
- **Depends On**: build-hybrid-pull
- **Assigned To**: assembler-docs
- **Agent Type**: documentarian
- **Parallel**: true
- Update `docs/recipes.md` with the new `retrieval_mode` parameter
  and a hybrid worked example.
- Update `ContextAssembler` module docstring.
- Add `CHANGELOG.md` entry.

### 7. Benchmark and default-flip (gated on #394)
- **Task ID**: bench-default-flip
- **Depends On**: build-hybrid-pull, #394 landed
- **Assigned To**: assembler-builder
- **Agent Type**: builder
- **Parallel**: false
- Run `python -m tests.benchmarks.run_external --dataset longmemeval-s`
  with `retrieval_mode="composite"` → baseline.
- Run same with `retrieval_mode="hybrid"` → hybrid result.
- Repeat for `--dataset locomo`.
- Commit both reports to `tests/benchmarks/results/external/`.
- If R@5 and MRR both improve on both datasets and p95 latency does
  not regress >2×, set `Defaults.HYBRID_AUTO = True` in the same
  commit. Otherwise document the negative result in the report and
  leave the gate `False`.

### 8. Final validation
- **Task ID**: validate-all
- **Depends On**: test-hybrid, test-existing, document-feature
- **Assigned To**: assembler-validator
- **Agent Type**: validator
- **Parallel**: false
- Run full `pytest`, `mypy src/`, `black --check src/ tests/`,
  `ruff check .`.
- `grep -rE "BF\.|CMS\.|FT\." src/popoto/` returns no new matches
  vs. main.
- Confirm `docs/recipes.md` builds cleanly.
- Verify the open-question answers from the user are reflected in
  the final code (RRF `k`, candidate pool, capability requirements).
- Generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| Hybrid-specific tests pass | `pytest tests/recipes/test_context_assembler_hybrid.py -v` | exit code 0 |
| Existing assembler tests pass | `pytest tests/recipes/test_context_assembler.py -v` | exit code 0 |
| Type-check clean | `mypy src/` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Lint clean | `ruff check src/ tests/` | exit code 0 |
| No new Redis-module commands | `git diff main -- src/ \| grep -E 'BF\.\|CMS\.\|FT\.'` | output empty |
| Backward-compat: composite default behavior unchanged | `pytest tests/recipes/test_context_assembler.py -k "not hybrid" -q` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique. Leave empty until critique is run. -->

---

## Open Questions

These are for human input before finalizing. Spikes resolved most of
the implementation-level questions in the issue body; what's left is
policy.

1. **Default RRF `k`**: spike-3 confirmed agentmemory uses 60 and
   that's the Cormack et al. default — proceeding with `k=60`. Any
   reason to deviate? (Most likely: no.)
2. **Backwards-compat warning policy**: the issue asks "should
   existing `score_weights`-only constructions warn?" Plan currently
   says **no `DeprecationWarning`** — composite is a first-class
   supported mode, not deprecated. Confirm this stance.
3. **Vector-field interface**: the issue asks "specific type or
   duck-type a `vector_search` method?" Plan currently uses
   `isinstance(EmbeddingField)`. If you want duck typing
   (`hasattr(field, "vector_search")`), say so — it's a one-line
   change but extends the API contract.
4. **Default-flip authority**: who signs off on flipping
   `Defaults.HYBRID_AUTO=True` after the benchmark? PM? Repo
   maintainer? Self-merge after a clean delta?
5. **`hybrid_candidate_pool=50` for `max_items=10`** — agentmemory
   uses ~5× the final K. Is 50 fine as the v1 default, or do you
   want it parametrized differently (e.g., always `max_items * 5`)?
