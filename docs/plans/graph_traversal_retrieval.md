---
status: Ready
type: feature
appetite: Medium
owner: Dev (agent)
created: 2026-07-21
tracking: https://github.com/tomcounsell/popoto/issues/462
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-07-21T00:00:00Z
---

# Graph-traversal retrieval over association primitives (close the multi-hop gap)

## Problem

Popoto's multi-hop retrieval is structurally weak: `ContextAssembler` already
seeds candidates from BM25/vector/composite arms and expands them via
`CoOccurrenceField.propagate()` (a BFS over co-occurrence edge weights), but
that is only *one* of the two association primitives the issue calls out.
`RelationshipField` — the explicit foreign-key-style edge type — is never
walked during retrieval, so a query that hits record A never surfaces record
B that A points to (or that points to A) via a `Relationship` field, even
though the edge is sitting right there as a Redis Set. Separately, hop
admission during graph expansion is purely a function of static edge
weight/decay-per-hop; nothing about a candidate's own `ConfidenceField` or
decay state (`DecayingSortedField`/`CyclicDecayField`) influences whether it
survives into the merged candidate set. Both gaps are exactly what
competitors' entity-graph traversal (Zep) and logical-network approaches
(Hindsight) exploit to win multi-hop categories.

**Current behavior:** Retrieval only ever discovers records connected by
`CoOccurrenceField` edges. `RelationshipField`-only associations are
invisible to `ContextAssembler`, and a decayed/low-confidence node is
weighted the same as a fresh/high-confidence one during hop admission.

**Desired outcome:** A separable seed→expand traversal stage that, when
enabled, additionally walks `RelationshipField` edges (1–2 hop) and
modulates hop-survival by the candidate node's confidence/decay state —
wired into `ContextAssembler` as an opt-in extension of the existing graph
arm, with zero behavior change for callers who don't opt in.

## Freshness Check

**Baseline commit:** `856bf32` (`git rev-parse HEAD` at plan time, `origin/main`)
**Issue filed at:** 2026-07-10T10:03:15Z
**Disposition:** Minor drift — the issue text describes the multi-hop gap as
if no graph traversal exists yet at all ("Popoto has the association
primitives... but no traversal-based retrieval"). That premise has partially
shifted: `CoOccurrenceField.propagate()` traversal was already wired into
`ContextAssembler` on main (both `_pull_path_composite()` and
`_pull_path_hybrid()`, see below) as part of prior work, independent of the
two PRs currently in flight. The remaining, still-true portion of the issue
is the `RelationshipField` edge gap and the missing decay/confidence
modulation — this plan scopes to exactly that remainder.

**File:line references re-verified:**
- `src/popoto/fields/co_occurrence_field.py:587-669` (`propagate()`) — BFS
  graph propagation with exponential per-hop decay and a weight-cap
  contraction invariant. Confirmed present and unchanged.
- `src/popoto/recipes/context_assembler.py:1432-1454` (`_pull_path_composite`)
  — calls `self._co_occurrence_field.propagate(...)` to re-score composite
  candidates via `co_occurrence_boost`. Confirmed present.
- `src/popoto/recipes/context_assembler.py:1540-1552` (`_pull_path_hybrid`)
  — calls `propagate()` seeded from top-5 BM25 hits, feeds result into the
  `graph` arm of RRF fusion (`query.fuse(keyword=..., vector=..., graph=...)`).
  Confirmed present.
- `src/popoto/fields/relationship.py:319-353`/`430-517` — `Relationship`
  field maintains bidirectional index Sets at
  `$RelationshipF:{ModelClass}:{field_name}:{related_db_key}` and exposes
  `filter_query()` for direct/chained lookups. Confirmed present, unused by
  `ContextAssembler`.

**Cited sibling issues/PRs re-checked:**
- #479 (`feature/457-hybrid-fusion-v2`, weighted/query-adaptive RRF) — OPEN,
  not merged. Touches `_pull_path_hybrid`'s `fuse_kwargs`/weighting logic,
  not the `propagate()` call sites this plan touches.
- #482 (`session/confidence_gated_retrieval`, confidence-gated refusal,
  #463) — OPEN, not merged. Touches post-retrieval confidence-threshold
  gating, a different code region from the propagation call sites.
- Both are expected to land before this PR merges; this plan's diff is
  scoped to avoid the lines either touches (see Technical Approach) so the
  eventual rebase is textual.

**Commits on main since issue was filed (2026-07-10) touching referenced
files:** several (RetrievalQuality partition-aware scoring #480, index
pointer fix #476, etc.) — none touch `propagate()` call sites or
`relationship.py`; irrelevant to this plan's premise.

**Active plans in `docs/plans/` overlapping this area:** none found
specific to graph traversal; `confidence_gated_retrieval` (#463) and
`hybrid_fusion` work are adjacent but address different mechanisms
(refusal threshold, arm weighting) than traversal expansion.

## Prior Art

- **CoOccurrenceField (PR #218)** and its `propagate()` BFS — the pattern
  this plan extends rather than replaces.
- **ContextAssembler hybrid fusion (RRF `graph` arm)** — already-shipped
  precedent for injecting a graph-derived candidate list into the same
  `query.fuse()` call used by BM25/vector arms. This plan's new traversal
  output is designed to be a drop-in replacement for that arm's input list.
- No prior issue/PR attempted `RelationshipField`-based traversal or
  confidence/decay-modulated hop admission; this is genuinely new surface.

## Data Flow

1. **Entry point:** `ContextAssembler.assemble(query_cues=...)` →
   `_pull_path()` dispatches to `_pull_path_composite()` or
   `_pull_path_hybrid()` based on effective retrieval mode.
2. **Seed generation (unchanged):** BM25/vector/composite arms produce an
   initial candidate list; the existing code takes the top-K PKs as BFS
   seeds.
3. **Expansion (new):** When `graph_traversal_relationship_fields` is
   configured on the assembler, `graph_traversal.traverse()` is called
   instead of the bare `CoOccurrenceField.propagate()` call. It internally
   (a) calls `propagate()` for co-occurrence edges as before, (b) walks the
   configured `Relationship` field(s) forward/reverse Sets for 1–2 hops,
   (c) merges both candidate-weight maps (max-weight-wins per PK), (d) caps
   the merged set at `max_candidates` by weight before any instance loads,
   (e) loads that bounded instance set and multiplies each candidate's
   weight by a confidence/decay modulation factor, (f) re-thresholds and
   returns a `list[(pk, weight)]`.
4. **Merge into existing flow:** The returned list is fed into
   `co_occurrence_boost` (composite path) or the `graph` fuse arm (hybrid
   path) exactly as `propagate()`'s output is today — no downstream changes
   needed.
5. **Output:** `ContextAssembler.assemble()`'s existing budget selection
   (`max_items`/`max_tokens`) applies unchanged to the enlarged candidate
   pool.

## Architectural Impact

- **New dependencies:** none (pure Python + existing Redis primitives).
- **Interface changes:** one new optional `ContextAssembler.__init__` kwarg,
  `graph_traversal_relationship_fields: list[str] | None = None`. Default
  `None` preserves current behavior bit-for-bit (existing tests unaffected).
- **Coupling:** `graph_traversal.py` depends on `CoOccurrenceField`,
  `Relationship`, `ConfidenceField`/`DecayingSortedField`/`CyclicDecayField`
  read paths — all read-only, no new write paths. `context_assembler.py`
  gains an import and a two-line conditional branch at each of the two
  existing `propagate()` call sites; no existing lines are modified, only
  wrapped in an `if/else`.
- **Data ownership:** unchanged — no new persisted state.
- **Reversibility:** trivially reversible (delete the new module, revert
  the two conditional branches and the constructor kwarg).

## Appetite

**Size:** Medium

**Team:** Solo dev (agent), PM supervises

**Interactions:**
- PM check-ins: 0-1
- Review rounds: 1 (do-pr-review gate)

## Prerequisites

No prerequisites — this work has no external dependencies. Requires local
Redis on `localhost:6379` (DB 15 for tests, per repo convention).

## Solution

### Key Elements

- **`src/popoto/recipes/graph_traversal.py` (new module):** a separable
  seed→expand stage. Exposes `traverse(model_class, seed_pks, *,
  co_occurrence_field=None, relationship_field_names=None, depth=2,
  max_candidates=200, confidence_field_name=None, decay_field_name=None) ->
  list[tuple[str, float]]`.
- **`expand_relationships()`:** walks configured `Relationship` field(s) via
  their existing `$RelationshipF:...` index Sets — both the forward
  direction (an instance's own relationship value → related PK) and the
  reverse direction (`SMEMBERS` on the related object's index Set → PKs
  pointing at it). 1 hop by default; a second hop re-applies the same walk
  from newly discovered PKs when `depth >= 2`, with a fixed per-hop weight
  decay constant so relationship-derived edges are comparable in magnitude
  to co-occurrence edges.
- **`_modulate_admission()`:** given a candidate PK→weight map already
  capped at `max_candidates`, loads the (bounded) instance set, multiplies
  each candidate's weight by `get_confidence(instance, confidence_field_name)`
  (when a `ConfidenceField` is configured) and by a decay factor derived
  from the model's `DecayingSortedField`/`CyclicDecayField` state (when
  configured) — reusing the existing partition-aware decay-scoring helpers
  already in `context_assembler.py` (`_decayed_partition_scores` /
  `_score_proxy_for_records`) rather than re-deriving decay math. Candidates
  falling below a fixed admission threshold after modulation are dropped.
- **`ContextAssembler` wiring:** new optional constructor kwarg
  `graph_traversal_relationship_fields`. When set (and a `Relationship`
  field with that name exists on the model), both `_pull_path_composite()`
  and `_pull_path_hybrid()` route their existing `propagate()` call through
  `graph_traversal.traverse()` instead, passing the same seeds/depth. When
  unset (default), behavior is byte-for-byte identical to today.

### Flow

Query cues → BM25/vector/composite seed arms → top-K seed PKs →
`graph_traversal.traverse()` (CoOccurrence BFS + Relationship walk, merged,
budget-capped, confidence/decay-modulated) → candidate weight list → fed
into existing `co_occurrence_boost` / RRF `graph` arm → existing budget
selection (`max_items`/`max_tokens`) → `AssemblyResult`.

### Technical Approach

- Keep `graph_traversal.py` fully independent of `context_assembler.py`'s
  internals — it takes primitives (model class, field objects/names, seed
  PKs) and returns a plain `list[(pk, weight)]`, the same shape
  `propagate()` already returns as a dict. This is what makes the two call
  sites a pure `if/else` swap rather than a rewrite.
- Relationship walk uses only `SMEMBERS`/`SADD`/`SREM` (already used by
  `Relationship.on_save`/`filter_query`) — no new Redis command types, fully
  Valkey-safe.
- Cap candidate expansion **before** any instance loads: BFS/relationship
  walk produce a weight map first (cheap, sorted-set/set ops only), which is
  truncated to `max_candidates` (constant, e.g. 200 — experimental,
  in-code) before the confidence/decay modulation pass, which is the only
  part that pays per-candidate Redis round-trips (via `GET`/pipeline). This
  bounds worst-case traversal cost independent of graph fan-out, keeping it
  compatible with the RLT latency budget conceptually (bounded instance
  loads, not unbounded BFS).
- Hop-count and decay constants (`RELATIONSHIP_HOP_DECAY`,
  `GRAPH_TRAVERSAL_MAX_CANDIDATES`, `ADMISSION_THRESHOLD`) are in-code
  magic-number constants documented with a comment, matching the existing
  `COMPETITIVE_SUPPRESSION_SIGNAL`/`RRF_K` convention in
  `context_assembler.py` — not exposed as user config.
- Default-off: the feature only activates when
  `graph_traversal_relationship_fields` is explicitly passed, so no
  existing test or caller behavior changes. This also means the diff at the
  two existing call sites is additive (wrapped in a new `if` branch), not a
  modification of the lines #479/#482 are mid-flight on.

## Failure Path Test Strategy

### Exception Handling Coverage
- [ ] `graph_traversal.traverse()` wraps the relationship-walk and
  modulation passes in the same `try/except log-and-continue` pattern
  already used around `propagate()` calls in `context_assembler.py`
  (`logger.warning(...)`, fall back to co-occurrence-only results). Test:
  assert a broken/missing Relationship field name degrades to
  co-occurrence-only output with a logged warning, not a raised exception.

### Empty/Invalid Input Handling
- [ ] `traverse()` with empty `seed_pks` returns `[]` immediately (no Redis
  calls) — test this.
- [ ] `traverse()` with `relationship_field_names=None` and no
  `co_occurrence_field` returns `[]` — test this (degenerate no-op case).
- [ ] Model with a `ConfidenceField`/`DecayingSortedField` name that doesn't
  exist on the model — modulation is skipped (factor 1.0), not an error —
  test this.

### Error State Rendering
- [ ] N/A — this is a library-internal retrieval path with no direct
  user-facing rendering; covered by the existing `ContextAssembler`
  `assemble()` failure-path tests (falls back to composite/co-occurrence-only
  on any exception in the new branch).

## Test Impact

- [ ] `tests/test_context_assembler.py` — no changes expected (feature is
  opt-in; default-path tests must still pass unmodified).
- [ ] `tests/test_context_assembler_hybrid.py` — no changes expected for
  the same reason; may ADD new opt-in tests for the
  `graph_traversal_relationship_fields` kwarg.
- [ ] New file `tests/test_graph_traversal.py` — ADD unit tests for
  `graph_traversal.traverse()`, `expand_relationships()`, and
  `_modulate_admission()` in isolation (constructed `Relationship`,
  `CoOccurrenceField`, `ConfidenceField`, `DecayingSortedField` fixtures),
  plus one or two integration tests wiring it through
  `ContextAssembler.assemble()`.

## Step by Step Tasks

1. Create `src/popoto/recipes/graph_traversal.py` with `traverse()`,
   `expand_relationships()`, `_modulate_admission()`, and the module-level
   tuning constants, following the existing docstring/style conventions in
   `context_assembler.py` and `co_occurrence_field.py`.
2. Write `tests/test_graph_traversal.py` covering: empty seeds, no-config
   no-op, relationship-only expansion (1 hop, 2 hop), co-occurrence +
   relationship merge (max-weight-wins), confidence modulation lowering a
   candidate's weight, decay modulation lowering a stale candidate's
   weight, `max_candidates` cap enforcement, and the exception-fallback
   path.
3. Add `graph_traversal_relationship_fields` kwarg to
   `ContextAssembler.__init__`, store it, and add a small resolution step
   (validate the named field(s) are actually `Relationship` fields on
   `model_class`, warn-and-ignore if not, matching the existing
   capability-detection pattern in `__init__`).
4. Wire the two call sites (`_pull_path_composite`, `_pull_path_hybrid`) to
   branch: if `self._relationship_traversal_fields` is set, call
   `graph_traversal.traverse(...)`; else keep the existing
   `self._co_occurrence_field.propagate(...)` call exactly as-is.
5. Add opt-in integration tests to `tests/test_context_assembler_hybrid.py`
   (or a new focused test module) exercising `assemble()` with
   `graph_traversal_relationship_fields` set on a small fixture model graph
   (e.g. `Memory` with both a `CoOccurrenceField` and a `Relationship` to a
   related model), asserting relationship-only-connected records are now
   surfaced.
6. Run the narrow-scope test suite (`pytest tests/test_graph_traversal.py
   tests/test_context_assembler.py tests/test_context_assembler_hybrid.py
   tests/test_relationship.py tests/test_co_occurrence_field.py -q`) against
   local Redis DB 15; fix any failures.
7. Run `black src/ tests/` and `mypy src/` per repo convention.
8. Update docs: `docs/features/agent-memory.md` (mention traversal as an
   opt-in extension of the graph arm) and
   `docs/plans/benchmarking_strategy_2026-07.md` §3.2 (note the
   RelationshipField/confidence-decay-modulation slice shipped; leave the
   LoCoMo multi-hop + association-recall evaluation as an explicit tracked
   follow-up, not fabricated numbers) via `/do-docs`.
9. Rebase onto latest `origin/main` immediately before opening/merging the
   PR (expect #479/#482 to have landed) and re-run the narrow test scope
   post-rebase.

## Verification

- `pytest tests/test_graph_traversal.py tests/test_context_assembler.py tests/test_context_assembler_hybrid.py tests/test_relationship.py tests/test_co_occurrence_field.py -q` passes against local Redis DB 15.
- Full suite passes on DB 15 before merge (`pytest -q`).
- Manual check: construct two `Memory`-like records connected ONLY via a
  `Relationship` field (no `CoOccurrenceField` edge) and confirm
  `ContextAssembler.assemble()` with `graph_traversal_relationship_fields`
  set surfaces the related record from a query that only lexically matches
  the seed record.
- Default-path regression: existing `ContextAssembler` tests pass unchanged
  with no kwarg passed (bit-for-bit behavior parity).

## Risks

- **File contention with #479/#482:** mitigated by scoping the diff to
  additive branches at two call sites plus a new constructor kwarg; final
  rebase before merge is mandatory (task 9).
- **Confidence/decay modulation adds per-candidate Redis reads:** bounded
  by capping the candidate set to `max_candidates` *before* the modulation
  pass (see Technical Approach) — cost is O(max_candidates), not O(graph
  fan-out).
- **Relationship walk fan-out on high-degree nodes:** the reverse-lookup
  Set (`SMEMBERS` on `$RelationshipF:...`) has no built-in size cap unlike
  `CoOccurrenceField.max_edges`; a very popular related object could return
  a large Set. Mitigate by capping how many members are consumed per node
  per hop with a fixed constant (e.g. top N via `SRANDMEMBER` count or a
  slice of `SMEMBERS`), documented as a known constant in the new module.
- **Eval scope:** LoCoMo multi-hop slice + association-recall scenarios
  called for in the issue are compute-heavy; this plan implements and
  unit-tests the mechanism only and files a tracked follow-up issue for the
  evaluation run rather than fabricating numbers (see Open Questions).

## Documentation

- `docs/features/agent-memory.md` — add a short subsection on
  `ContextAssembler`'s optional `graph_traversal_relationship_fields` and
  what it does (RelationshipField-based expansion + confidence/decay hop
  modulation), cross-referencing the existing CoOccurrence graph-arm
  description.
- `docs/plans/benchmarking_strategy_2026-07.md` §3.2 — append a note (in
  the same style as the existing §3.3 LLM-extraction status note) that the
  RelationshipField-expansion + confidence/decay-modulation slice has
  shipped, with evaluation tracked as a follow-up issue.

## Open Questions

1. **Evaluation follow-up:** should the LoCoMo multi-hop slice +
   association-recall eval run be filed as a new tracked GitHub issue now
   (e.g. under epic #456 Track B), or left as a line item in the
   benchmarking strategy doc only? Plan assumes: file a small follow-up
   issue referencing this PR, per the project's general practice of
   tracking deferred evaluation work (matches how #461's extraction-eval
   gap is tracked in §3.3).
2. **Relationship-walk fan-out cap:** is a fixed per-node member cap (e.g.
   50) acceptable as an in-code constant, or should it scale with
   `max_candidates`? Plan assumes a fixed constant, consistent with the
   project's "hop-admission thresholds are experimental magic-number
   constants, not user config" rule.
3. **Second-hop relationship semantics:** for `depth=2` relationship
   traversal, should the second hop re-use the *same* configured
   relationship field name(s), or should it also consider the reverse of a
   different declared relationship on the newly-discovered model class
   (true entity-graph style)? Plan assumes same-field-name-only for v1
   (simpler, avoids needing to introspect arbitrary related model classes'
   relationship fields) — flagged for follow-up if reviewers want the
   fuller entity-graph behavior.
