---
status: Ready
type: bug
appetite: Medium
owner: dev
created: 2026-07-17
revision_applied: true
revision_applied_at: 2026-07-17T04:56:57Z
tracking: https://github.com/tomcounsell/popoto/issues/474
last_comment_id:
---

# RetrievalQuality score proxy ignores partition_by (0.0 scores for partitioned models)

## Problem

The shared metacognitive score-proxy helper
`_score_proxy_for_records` in `src/popoto/recipes/context_assembler.py`
reads each record's composite score from the **non-partitioned** sorted-set
index key. Agent-memory models almost always declare `partition_by` on their
sorted fields (e.g. `DecayingSortedField(partition_by="agent_id")`), so the
real per-record data lives in a **partition-specific** ZSET
(`$SortF:Model:field:<agent>`), not the base key (`$SortF:Model:field`). The
base key has zero members, every `ZSCORE` returns `None`, and the helper
reports `0.0` for every record.

This silently degrades every metacognitive signal that reads the proxy:
`RetrievalQuality.score_spread`, the FOK subthreshold-activation component, and
`staleness_ratio` — all collapse toward their degenerate value for partitioned
models. `AdaptiveContextAssembler`'s default quality metric consumes
`score_spread`, so retrieval-mode selection is fed a flatlined signal.

**Current behavior:** For a model with `partition_by` on its scored sorted
field, `_score_proxy_for_records(...)` returns `{key: 0.0, ...}` regardless of
the real per-record scores, and `RetrievalQuality.score_spread` is `0.0`.

**Desired outcome:** The shared helper reads each record's real relevance from
the partition-specific index, so partitioned models get correct per-record
proxy scores and `score_spread` reflects real score dispersion.

## Critique Revision (2026-07-17) — BLOCKER resolution

The first-draft plan proposed a **naive one-line key swap** at both call sites:
`get_special_use_field_db_key` → `get_partitioned_sortedset_db_key`, mirroring
the "reference" scorer `ContextAssembler._injection_scores` (PR #473). The
war-room critique returned **NEEDS REVISION** on a BLOCKER that this revision
resolves:

**The BLOCKER (confirmed empirically during this revision).** For a
`SortedField`, the ZSET member score *is* the relevance value, so reading it
back via `ZSCORE` is correct. But **`DecayingSortedField` stores each member's
`last_updated` epoch timestamp as the ZSET score** (`auto_now=True`;
`decaying_sorted_field.py:43,52-117`) — *not* a relevance value. The real
relevance is computed on read by `DECAY_SCORE_LUA`:
`decayed = base_score * elapsed_days^(-decay_rate)`. Every agent-memory fixture
in `tests/test_context_assembler.py` (and the issue's own motivating example)
uses `DecayingSortedField`, so:

- The **naive key swap makes the proxy read ~1.7e9 epoch timestamps as
  "scores."** `score_spread` (a coefficient of variation) over three
  near-identical timestamps is ~1e-8 — still meaningless, just non-zero. It
  does *not* deliver "real score dispersion."
- It **inverts `staleness_ratio`.** `_staleness_ratio` counts records whose
  score is `< surfacing_threshold` (0.5). A timestamp `1.7e9 < 0.5` is always
  false, so the `tests/.../test_...:804` fixture flips from asserted `1.0` to
  `0.0` — for the wrong reason (see below for the *correct* value).
- The reference `_injection_scores` (`context_assembler.py:1174`) has the **same
  latent defect**: it, too, reads the raw partition ZSCORE and is therefore only
  *partition*-correct, not *decay*-correct. It is not a valid reference for
  decaying fields — the original plan's "mirror `_injection_scores`" premise was
  wrong for the motivating case.

The plain-`SortedField` TDD test in the original plan would have **masked** all
of this (a plain field's raw ZSCORE is genuinely its value), so the bug would
have shipped green.

**Resolution.** The partition-aware proxy must read each record's *relevance*,
which is field-type dependent:

| Field type | ZSET member score is… | Correct proxy read |
|---|---|---|
| plain `SortedField` (`SortedFieldMixin`, non-decaying) | the relevance value | raw `ZSCORE` on the **partition** key (the key swap) |
| `DecayingSortedField` (and `CyclicDecayField` subclass) | a `last_updated` timestamp | the **decayed** score via the field's own decay computation (`DECAY_SCORE_LUA` / `CYCLIC_DECAY_LUA`), keyed by the **partition** key |

This is why appetite moves from **Small → Medium**: the fix is not a key swap,
it is a field-type-aware relevance read plus the test-contract corrections it
forces (including a **staleness inversion** in the `:804` fixture and new
FOK/staleness regression coverage the critique required before build).

The three CONCERNS + NIT the critique raised are folded in below: (C1) the
`:802`/`:804`/`:815` fixture must be recomputed against decayed scores, not left
as bug-masked zeros; (C2) `_injection_scores` shares the decay defect and must
be reconciled (delegate or document); (C3) new dedicated FOK-subthreshold and
staleness regression tests are required, not just a `score_spread` test; (NIT)
the misleading `:815-817` comment ("decayed to 0 because DecayingSortedField
starts at 0 score on a freshly saved record") is factually wrong — a freshly
saved record decays to its **maximum** score (elapsed clamped to 0.01 days), not
0 — and must be corrected.

## Freshness Check

**Baseline commit:** `774d274be8a2be3bb9d242b2fc6f6fd7d4438843`
**Issue filed at:** 2026-07-14T06:22:15Z
**Disposition:** Unchanged (one unrelated commit landed since filing)

**File:line references re-verified (against baseline HEAD):**
- `src/popoto/recipes/context_assembler.py:390` `_score_proxy_for_records` — still present; line 419 uses `get_special_use_field_db_key` (the non-partitioned key). Confirmed defect (partition dimension).
- `src/popoto/recipes/context_assembler.py:603` `_staleness_ratio` inline ZSCORE — **also** uses `get_special_use_field_db_key`; same partition defect, second call site. Additionally reads the raw ZSET score (a timestamp for its `DecayingSortedField` target) rather than the decayed score — the deeper defect this revision addresses.
- `src/popoto/recipes/context_assembler.py:1174` `ContextAssembler._injection_scores` — the partition-aware scorer from PR #473; uses `get_partitioned_sortedset_db_key` (line 1216) but reads raw ZSCORE, so it is partition-correct only, **not** decay-correct. Not a valid reference for decaying fields.
- `src/popoto/fields/decaying_sorted_field.py:43,52-117` — `DecayingSortedField` stores `last_updated` timestamps as ZSET scores; `DECAY_SCORE_LUA` computes `base_score * elapsed_days^(-decay_rate)` at read time. Confirmed.
- `src/popoto/models/query.py:292` `top_by_decay` — the canonical partition-aware decayed-score read (branches to `CYCLIC_DECAY_LUA` for `CyclicDecayField`, line 386-407). The reuse target for the proxy's decay path.
- `src/popoto/fields/cyclic_decay_field.py:156` — `class CyclicDecayField(DecayingSortedField)`; a subclass, so any `isinstance(f, DecayingSortedField)` branch also catches it (must use `CYCLIC_DECAY_LUA` + companion hash keys, per `top_by_decay`).

**Cited sibling issues/PRs re-checked:**
- #464 / PR #473 — merged (`7247a41 feat(#464): live-agent memory telemetry`). Introduced `_injection_scores` and deliberately left the shared helper untouched. Landscape unchanged.

**Commits on main since issue was filed (touching referenced files):**
- `774d274 feat(#458): judged-answer harness (Tier 5)` — unrelated; does not touch `context_assembler.py`, the scoring path, or the decay fields.

**Active plans in `docs/plans/` overlapping this area:** none.

**Reproduction (spike, DB 15):**
- Plain `SortedField(partition_by="agent_id")` at scores `0.2/0.6/0.9`:
  `_score_proxy_for_records(...)` → all `0.0` (buggy); base key empty; partition
  key holds `0.2/0.6/0.9` (correct raw-ZSCORE target).
- `DecayingSortedField(partition_by="agent_id")`, three fresh records: partition
  key holds three ~`1.7e9` **timestamps**, not relevances. Naive key swap →
  `score_spread ≈ 1e-8` (meaningless) and `staleness_ratio` flips `1.0 → 0.0`.
  Correct decayed score for a fresh record (elapsed clamped to 0.01 days,
  `base_score=1.0`, `Defaults.DECAY_RATE`): `1.0 * 0.01^(-rate)` — deterministic
  because elapsed is clamped, so the fixture stays reproducible.

## Prior Art

- **PR #473 (issue #464)** — Live-agent memory telemetry. Added the self-contained partition-aware scorer `ContextAssembler._injection_scores`, used **only** for the telemetry trace, and explicitly scoped around fixing the shared helper because doing so changes `score_spread` behavior and existing `== 0.0` test contracts. This plan is the deferred broad fix. **Important correction:** `_injection_scores` fixes only the *partition* dimension; it inherits the timestamp-vs-relevance defect for decaying fields, so this plan's fix must go beyond mirroring it.
- `src/popoto/models/query.py::top_by_decay` (+ `_materialize_decay_field`) — the existing, correct, partition-aware decayed-score computation. This plan **reuses** its Lua (`DECAY_SCORE_LUA` / `CYCLIC_DECAY_LUA`) rather than reimplementing decay math, to avoid Python/Lua drift.
- No closed issue/PR attempts to fix `_score_proxy_for_records`. First fix, not a repeat — no "Why Previous Fixes Failed" section.

## Research

No relevant external findings — this is an internal Popoto/Redis ZSET-keying and
decay-scoring bug. Proceeding with codebase context. Valkey-safety constraint
applies: the fix uses read-only pipelined `ZSCORE`/`HGET` and `EVAL` of the
existing decay Lua only (no Redis modules, no `ZUNIONSTORE`) — identical
primitives to the existing `top_by_decay` path, which already runs on both
Redis and Valkey.

## Data Flow

1. **Entry point:** `ContextAssembler.assess(...)` (or `AdaptiveContextAssembler` via its quality metric) builds a `RetrievalQuality`.
2. **`_compute_score_spread(records, model_class, score_weights)`** → **`_score_proxy_for_records`**. Currently reads `get_special_use_field_db_key(record, field).redis_key` (non-partitioned) → `ZSCORE` `None` for partitioned models → `0.0` scores → `score_spread == 0.0`.
3. Two sibling consumers of the same helper / pattern:
   - **`_compute_fok(...)`** (`:537`) → `_score_proxy_for_records` for the subthreshold-activation fraction (`0 < s < surfacing_threshold`).
   - **`_staleness_ratio(...)`** (`:603`) → its own inline `ZSCORE` loop over `get_special_use_field_db_key`; compares the score to `surfacing_threshold`.
4. **Output:** `RetrievalQuality.score_spread` / `.fok_score` / `.staleness_ratio` / `.score_distribution` feed the metacognitive layer and `AdaptiveContextAssembler`'s mode selection.

The fix replaces the *read* at these sites with a **field-type-aware relevance
read** on the **partition** key:
- plain `SortedField` → raw partition `ZSCORE` (the key swap; correct because the
  score is the value),
- `DecayingSortedField` / `CyclicDecayField` → the **decayed** score via the
  field's own decay Lua, keyed by the partition key.

## Appetite

**Size:** Medium (raised from Small during critique revision — see the BLOCKER
resolution; the fix is a field-type-aware relevance read, not a key swap, and it
forces a staleness inversion plus new regression coverage).

**Team:** Solo dev + validator

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

The judgment calls are: (a) how decaying fields are scored (resolved: reuse the
field's decay Lua), (b) which `== 0.0`/`== 1.0` assertions are legitimate vs
bug-masked (resolved below), and (c) whether to reconcile `_injection_scores`
(Open Question 1).

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli -n 15 ping` | Test suite needs a live server on DB 15 |

## Solution

### Key Elements

- **Partition-aware, field-type-aware proxy.** `_score_proxy_for_records`
  reads each record's relevance from the **partition-specific** index. For each
  sorted field in `score_weights`:
  - **Plain `SortedField`** (isinstance `SortedFieldMixin` but **not**
    `DecayingSortedField`): pipelined `ZSCORE` on
    `get_partitioned_sortedset_db_key(record, field_name)` — the ZSET score *is*
    the value. (For non-partitioned models `partition_by` is empty, so this
    returns the same key as today — behavior identical.)
  - **`DecayingSortedField` / `CyclicDecayField`**: read the **decayed** score,
    not the raw timestamp. Reuse the field's existing decay computation
    (`DECAY_SCORE_LUA`, or `CYCLIC_DECAY_LUA` + companion cycles/pressure hash
    keys for `CyclicDecayField`), exactly as `top_by_decay` does, keyed by the
    **partition** key. Group the records by their partition key, run the Lua once
    per distinct partition (returns `[member, decayed_score, …]`), and map each
    record's decayed score back. This is the single source of truth for decay —
    **no Python re-implementation of the decay formula** (avoids Lua/Python
    drift).
- **Partition- and decay-aware staleness.** `_staleness_ratio` targets a named
  `DecayingSortedField`, so it must compare the **decayed** score (not the raw
  timestamp) to `surfacing_threshold`, keyed by the partition key. A freshly
  saved record decays to its **maximum** score (elapsed clamped to 0.01 days),
  which is well above the 0.5 threshold → **not stale**. The current `== 1.0`
  contract for fresh records was a bug-masked artifact of reading the empty
  non-partition key (all `None` → counted stale); the correct value is `0.0`.
- **Reconcile `_injection_scores` (C2, preferred).** Make it delegate to the now
  correct module helper so the telemetry trace and the metacognitive proxy share
  one decay-and-partition-aware implementation, eliminating the drift this bug
  was born from. This changes telemetry-trace scores for decaying fields
  (previously raw timestamps) — acceptable per issue (beta substrate), call it
  out in the PR. If a clean delegation is not achievable in this appetite, apply
  the identical field-type-aware read inline in `_injection_scores` and note the
  duplication for a follow-up (do **not** leave it reading raw timestamps).
- **Test-contract updates.** Recompute the `:802`/`:804`/`:815-820` fixture
  against decayed scoring (staleness `1.0 → 0.0`; `score_distribution` from
  `[0,0,0]` to three equal decayed maxima; `score_spread` stays `0.0` because
  the three fresh scores are equal — now for the *right* reason); fix the
  misleading comment. Preserve the assertions where `0.0` is legitimately
  correct (empty retrieval, no-sorted-field-in-weights models).

### Flow

`assess()` → `_compute_score_spread` / `_compute_fok` / `_staleness_ratio` →
`_score_proxy_for_records` (partition + decay aware) → real per-record relevance
for partitioned models → `score_spread` reflects real dispersion, `staleness`
reflects real decay, FOK subthreshold reflects real subthreshold activation.

### Technical Approach

- **Field-type branch** in `_score_proxy_for_records` and `_staleness_ratio`
  keyed on `isinstance(f, DecayingSortedField)` (catches `CyclicDecayField`
  too). Non-decaying `SortedFieldMixin` fields keep the pipelined-`ZSCORE`
  path with the key builder swapped to `get_partitioned_sortedset_db_key`.
- **Decay path reuse:** import `DECAY_SCORE_LUA` (and, for `CyclicDecayField`,
  `CYCLIC_DECAY_LUA` + `get_cycles_hash_key_from_parts` /
  `get_pressure_hash_key_from_parts`) and the partition-value extraction pattern
  from `top_by_decay` (`query.py:365-417`). Group the passed records by their
  partition key; `EVAL` the appropriate Lua per distinct partition with
  `n = number of members` (or a large cap) so every passed record is covered;
  build `{member_redis_key: decayed_score}`; look up each record. Records whose
  partition value is missing (raises `QueryException` in the key builder) fall
  back to `0.0` (proxy) / counted stale (`_staleness_ratio`) via the existing
  `try/except` guards — keep them.
- **Non-partitioned safety:** `get_partitioned_sortedset_db_key` appends nothing
  when `partition_by` is empty, so plain non-partitioned models produce
  identical results to today; a non-partitioned decaying field still routes
  through the decay Lua on its base key (correct, and unchanged in value from a
  correct pre-existing single-partition read).
- **Numeric constants:** none added. Weights flow through `score_weights`; decay
  rate/base come from the field's own config (or its documented defaults), read
  the same way `top_by_decay` reads them. No new config surface.

### Distinguishing legitimate values from bug-masked values (critical)

The builder classifies each affected assertion by asking: *what does the
partition-specific index actually hold for this record, and what relevance does
it map to?*

- **`tests/test_context_assembler.py:802` (`score_spread`)** — three freshly
  saved `DecayingSortedField` records under one partition, equal base scores →
  equal decayed maxima → `pstdev/mean = 0`. `score_spread == 0.0` **stays**, but
  now for the correct reason (equal non-zero scores, not all-zero). Keep the
  assertion; update the rationale comment.
- **`:804` (`staleness_ratio`)** — **CHANGE `1.0 → 0.0`.** Fresh records decay to
  their maximum (≥ threshold) → none stale. The old `1.0` was bug-masked (empty
  non-partition key → all `None` → all counted stale).
- **`:815-820` (`score_distribution`)** — **CHANGE.** No longer `[0.0, 0.0, 0.0]`.
  The three decayed maxima are equal and non-zero; assert them via the field's
  decay formula (derive, don't hard-code a fragile literal that drifts if
  `Defaults.DECAY_RATE` changes — compute expected `= base * clamp_elapsed^(-rate)`
  from the field's effective rate, or assert `all(s > 0)` and `all equal`).
  Replace the false "decayed to 0 … starts at 0 score" comment with the correct
  "fresh records decay to their maximum; equal base scores → equal scores → zero
  spread."
- **`:812` / `:813` (`subthreshold_activation` = 0.0, `component_score` = 0.64,
  `fok_score` 0.64 at `:803`)** — **UNCHANGED.** Fresh decayed maxima are above
  `surfacing_threshold`, and the guard is `0 < s < threshold`, so
  subthreshold_frac stays `0.0`; FOK scalars are unaffected. Re-verify, keep.
- **`:655`, `:936`, `:954`** — models with **no sorted field in `score_weights`**
  → proxy legitimately `0.0`; keep.
- **`:721`** — empty retrieval → `0.0`; keep.
- **`tests/test_adaptive_assembler.py:87`** — hand-constructed
  `RetrievalQuality(score_spread=0.0)` literal, not computed; keep.

## Failure Path Test Strategy

### Exception Handling Coverage
- The touched code has `except Exception:` guards that fall back to a `0.0`
  proxy / count-as-stale when `get_partitioned_sortedset_db_key` raises
  `QueryException` for a missing partition value. Add/confirm a test that a
  record missing its partition field value does not crash the proxy or the decay
  path and does not corrupt other records' scores.
- Decay Lua `EVAL` failures (e.g. empty partition ZSET) return `[]` → all mapped
  records fall back to `0.0`; confirm no crash.

### Empty/Invalid Input Handling
- `_score_proxy_for_records([], ...)` returns `{}` (existing early return) — keep a test.
- Model with no sorted-field-backed weight returns all-`0.0` — keep existing tests (`:655`, `:936`, `:954`).

### Error State Rendering
- Not user-facing UI. `score_spread` / `staleness` degradation is the "error
  state"; the new regression tests are the guard that they are no longer
  silently zeroed/inverted.

## Test Impact

- [ ] `tests/test_context_assembler.py::<partitioned fixture at :790-820>` — UPDATE: `staleness_ratio` `1.0 → 0.0`; `score_distribution` `[0,0,0] → three equal decayed maxima`; keep `score_spread == 0.0` (equal scores) with corrected rationale; fix the misleading `:815-817` comment. Re-verify `avg_confidence`/`fok_score`/`per_cue_fok` scalars stay unchanged.
- [ ] `tests/test_context_assembler.py` — ADD (TDD red first): **decaying** partitioned model whose records have **distinct** decayed scores (distinct `base_score_field` values, or artificially aged timestamps) → `_score_proxy_for_records` returns the true per-record decayed scores and `assess(...).score_spread > 0`. A plain-`SortedField` test would mask the decay defect, so the primary TDD test MUST use `DecayingSortedField`.
- [ ] `tests/test_context_assembler.py` — ADD: **plain `SortedField(partition_by=...)`** control with distinct raw scores `0.2/0.6/0.9` → `score_spread > 0` (guards the plain-field key-swap path independently of decay).
- [ ] `tests/test_context_assembler.py` — ADD (staleness regression, C3): records aged past the surfacing threshold (old `last_updated` so decayed score `< surfacing_threshold`) → `staleness_ratio > 0`; fresh records → `staleness_ratio == 0.0`.
- [ ] `tests/test_context_assembler.py` — ADD (FOK subthreshold regression, C3): a partitioned decaying model with records whose decayed score falls in `(0, surfacing_threshold)` → `subthreshold_activation > 0` in `per_cue_fok`.
- [ ] `tests/test_context_assembler.py` — ADD: non-partitioned control (plain and decaying) → results identical to a correct single-partition read (no regression for non-partitioned models).
- [ ] `tests/test_context_assembler.py:655,:721,:936,:954` — KEEP: legitimate zeros (empty retrieval / no scored sorted field); do not weaken.
- [ ] `tests/test_adaptive_assembler.py:87` — KEEP: literal `RetrievalQuality`, not affected.
- [ ] `tests/test_adaptive_assembler.py` — REVIEW: confirm `_default_quality_metric` tests still hold; partitioned fixtures (if any) now produce non-zero `score_spread`. If `_injection_scores` is reconciled, check any telemetry-trace assertions that pinned raw-timestamp scores.

## Rabbit Holes

- **Reimplementing decay math in Python.** Do NOT re-derive `elapsed_days^(-rate)` in the proxy — reuse the field's `DECAY_SCORE_LUA` / `CYCLIC_DECAY_LUA` (single source of truth) to avoid drift.
- **Rewriting `_injection_scores` scoring semantics.** Only make it partition- **and** decay-aware (delegate to the shared helper if clean); do not change what fields contribute or how RRF/hybrid fused scores are (not) persisted. Per-arm score decomposition remains the documented fast-follow, out of scope.
- **Introducing a new config knob.** `partition_by`, `decay_rate`, `base_score_field` are already field metadata; do not add config surface.
- **Chasing every `== 0.0` in the test suite.** Only the proxy-fed assertions on partitioned/decaying models are in scope; the legitimate zeros stay intact.
- **`ZUNIONSTORE` / Redis-module aggregation for multi-field weights.** Stay with pipelined `ZSCORE`/`HGET` and `EVAL` of the existing decay Lua (Valkey-safe).
- **CyclicDecayField deep semantics.** Route it through its existing `CYCLIC_DECAY_LUA` (as `top_by_decay` does); do not re-derive its cycle/pressure model.

## Risks

### Risk 1: Over-correcting a legitimately-zero assertion
**Impact:** Weakening `:655/:721/:936/:954` (true zeros) or `:802` (equal-score zero spread) would delete real coverage.
**Mitigation:** The classification table above; the builder justifies each changed assertion in the PR. New distinct-score TDD tests isolate the non-zero cases so the legitimate-zero tests remain the guard for the zero case.

### Risk 2: Silent behavior change for downstream `AdaptiveContextAssembler`
**Impact:** `score_spread` becomes non-zero and `staleness_ratio` flips for partitioned decaying models, changing `_default_quality_metric` output and possibly mode selection — acceptable per issue (beta substrate), but must not surprise callers.
**Mitigation:** Grep all `score_spread` / `staleness_ratio` / `_score_proxy_for_records` / `RetrievalQuality` consumers (`adaptive_assembler.py`, both test files, `memory_telemetry.py`) and update expectations. Call out the behavior change (especially the staleness `1.0 → 0.0` inversion) in the PR body. No non-test production caller asserts `score_spread == 0.0` or `staleness_ratio == 1.0`.

### Risk 3: `QueryException` on records missing a partition value
**Impact:** A record without its partition field set would raise inside the key builder / partition-value extraction.
**Mitigation:** Existing `try/except` → `0.0` proxy / count-as-stale fallback already handles it; keep it and add a test.

### Risk 4: Python/Lua decay drift
**Impact:** A hand-rolled Python decay would diverge from `top_by_decay`, producing proxy scores that disagree with actual ranking.
**Mitigation:** Reuse the field's own Lua; do not reimplement. A cross-check test can assert the proxy's decayed score for a record matches `top_by_decay`'s score for the same record.

### Risk 5: Decay Lua cost inside the proxy
**Impact:** Running the decay Lua per partition scans the whole partition ZSET, heavier than a single `ZSCORE`.
**Mitigation:** One `EVAL` per distinct partition (records are usually single-partition in the assembler path); matches the existing `top_by_decay` cost profile. Acceptable for the metacognitive path. Noted, not blocking.

## Race Conditions

No race conditions identified — the proxy is a synchronous, read-only batch
(`ZSCORE`/`HGET`/`EVAL`); no shared mutable state, no cross-process writes. The
decay Lua reads a consistent snapshot server-side.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #464] Per-arm score decomposition for hybrid/lexical fused RRF scores (the trace already documents this as a fast-follow; unchanged here).
- Changing decay semantics, adding config knobs, or altering `top_by_decay`.
- Everything else — the shared-helper fix (partition + decay), the `_staleness_ratio` fix, `_injection_scores` reconciliation, the test-contract updates, and consumer coordination — is in scope.

## Update System

No update-system changes required — purely internal library fix; no deploy/propagation surface.

## Agent Integration

No agent integration required — internal to the `recipes` scoring path; no new tool/MCP surface.

## Documentation

### Inline Documentation
- [ ] Update `_score_proxy_for_records` docstring: it reads the **partition-specific** index and, for decaying fields, the **decayed** score (drop the "non-partitioned" framing; state the field-type branch).
- [ ] Update `_staleness_ratio` docstring: compares the **decayed** score to the surfacing threshold on the partition key; note that fresh records are not stale.
- [ ] Update `_injection_scores` docstring (lines 1177-1189): remove the claim that the metacognitive proxy "would return 0.0" for partitioned models (no longer true), and reflect the decay reconciliation.
- [ ] Fix the misleading fixture comment at `tests/test_context_assembler.py:815-817`.

### Feature / External Docs
- [ ] No `docs/features/` or MkDocs page documents `_score_proxy_for_records` behavior specifically; a scan confirms no `score_spread == 0.0`-for-partitioned claim to correct. If `/do-docs` finds a metacognitive-signals page, add a one-line note that proxy scores are partition- and decay-aware.

## Success Criteria

- [ ] TDD test (decaying, distinct scores) written first and observed **failing** on baseline HEAD, then green.
- [ ] `_score_proxy_for_records` and `_staleness_ratio` read the **partition-specific** index and the **decayed** score for `DecayingSortedField`/`CyclicDecayField`, and raw partition `ZSCORE` for plain `SortedField`.
- [ ] `staleness_ratio` for fresh partitioned decaying records is `0.0` (not the bug-masked `1.0`); a genuinely-aged-record test yields `staleness_ratio > 0`.
- [ ] FOK subthreshold regression test yields `subthreshold_activation > 0` for records with decayed score in `(0, surfacing_threshold)`.
- [ ] Non-partitioned models (plain and decaying) produce results identical to a correct single-partition read.
- [ ] `_injection_scores` no longer reads raw timestamps for decaying fields (delegated or fixed inline).
- [ ] All masked `score_spread`/`staleness`/`score_distribution` assertions updated with justification; all legitimate-zero assertions preserved.
- [ ] No new config surface or numeric-constant knobs; decay math reused, not reimplemented.
- [ ] Read-only `ZSCORE`/`HGET`/`EVAL` of existing decay Lua only — no `ZUNIONSTORE`, no Redis modules (Valkey-safe).
- [ ] Fix on a feature branch off `main` (e.g. `fix/474-partition-aware-score-proxy`); no direct push to `main`.
- [ ] Tests pass (`/do-test`). Documentation updated (`/do-docs`).

## Team Orchestration

### Team Members

- **Builder (score-proxy)**
  - Name: `proxy-builder`
  - Role: Write failing TDD tests (decaying distinct-score + staleness + FOK-subthreshold), then make `_score_proxy_for_records` + `_staleness_ratio` partition- and decay-aware; reconcile `_injection_scores`; update masked assertions.
  - Agent Type: builder
  - Domain: redis-popoto
  - Resume: true

- **Validator (score-proxy)**
  - Name: `proxy-validator`
  - Role: Verify decaying + plain partitioned scoring correct, staleness inversion fixed, legitimate zeros preserved, non-partitioned unchanged, decay reused (not reimplemented), Valkey-safe.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Write failing reproduction tests (TDD red)
- **Task ID**: build-tdd-test
- **Depends On**: none
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Parallel**: false
- Primary: `DecayingSortedField(partition_by="agent_id")` with a `base_score_field` giving three records **distinct** base scores under one partition → assert `_score_proxy_for_records(...)` returns the true per-record decayed scores and `assess(...).score_spread > 0`. Confirm it **fails** on HEAD (proxy returns all `0.0`). Capture output.
- Secondary (red where applicable): plain `SortedField(partition_by=...)` distinct-score `score_spread > 0`; staleness regression (aged records → `> 0`, fresh → `0.0`); FOK subthreshold regression (decayed score in `(0, threshold)` → `> 0`).

### 2. Make the proxy partition- and decay-aware (TDD green)
- **Task ID**: build-fix
- **Depends On**: build-tdd-test
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Domain**: redis-popoto
- **Parallel**: false
- In `_score_proxy_for_records` (~line 419): branch on `isinstance(f, DecayingSortedField)`. Plain `SortedFieldMixin` → pipelined `ZSCORE` on `get_partitioned_sortedset_db_key`. Decaying → group records by partition key, `EVAL` `DECAY_SCORE_LUA` (or `CYCLIC_DECAY_LUA` + companion hash keys for `CyclicDecayField`) per partition, map decayed scores back. Reuse the partition-value extraction and Lua-dispatch pattern from `top_by_decay` (`query.py:365-417`). Keep the `try/except` `0.0` fallback.
- In `_staleness_ratio` (~line 603): read the **decayed** score for the named `DecayingSortedField` on the partition key; compare to `surfacing_threshold`. Keep the count-as-stale fallback.
- Reconcile `_injection_scores`: delegate to the shared helper if clean, else apply the same field-type-aware read inline (Open Question 1).
- Update the three docstrings + the fixture comment (Documentation section).
- Run the new tests → green.

### 3. Reconcile existing test contracts
- **Task ID**: build-test-contracts
- **Depends On**: build-fix
- **Assigned To**: proxy-builder
- **Agent Type**: builder
- **Parallel**: false
- Run full `tests/test_context_assembler.py` and `tests/test_adaptive_assembler.py`.
- Apply the classification table: `:804` staleness `1.0 → 0.0`; `:815-820` `score_distribution` to the equal decayed maxima (derive from the decay formula, don't hard-code a drift-prone literal); `:802` `score_spread` stays `0.0` with corrected rationale; keep legitimate zeros. Re-verify `avg_confidence`/`fok_score`/`per_cue_fok` scalars unchanged.
- Grep for any other `score_spread`/`staleness_ratio`/`_score_proxy_for_records`/telemetry-trace consumers and update expectations.

### 4. Validation
- **Task ID**: validate-all
- **Depends On**: build-test-contracts
- **Assigned To**: proxy-validator
- **Agent Type**: validator
- **Parallel**: false
- Confirm: decaying + plain partitioned scoring correct; staleness inversion fixed; FOK subthreshold correct; non-partitioned identical to before; decay reused not reimplemented; no `ZUNIONSTORE`/Redis modules; no new config surface. Optional cross-check: proxy decayed score matches `top_by_decay` for the same record.
- Run the full suite (`scripts/ci-local.sh --fast` or `pytest`) and report pass/fail.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Shared helper no longer uses the non-partitioned key builder in the scoring path | `grep -n "get_special_use_field_db_key" src/popoto/recipes/context_assembler.py` | reviewer confirms none remain in `_score_proxy_for_records`/`_staleness_ratio` |
| Partition-aware key builder present in scoring path | `grep -c "get_partitioned_sortedset_db_key" src/popoto/recipes/context_assembler.py` | `output > 1` |
| Decay Lua reused (not reimplemented) | `grep -n "DECAY_SCORE_LUA\|CYCLIC_DECAY_LUA" src/popoto/recipes/context_assembler.py` | at least one import/use in the scoring path |
| No Redis-module / ZUNIONSTORE aggregation | `grep -rn "ZUNIONSTORE\|zunionstore\|BF\.\|CMS\." src/popoto/recipes/context_assembler.py` | `match count == 0` |
| context_assembler tests pass | `pytest tests/test_context_assembler.py -q` | `exit code 0` |
| adaptive_assembler tests pass | `pytest tests/test_adaptive_assembler.py -q` | `exit code 0` |

## Open Questions

1. **`_injection_scores` reconciliation:** Delegate it to the now decay-and-partition-aware shared helper (single implementation, but changes telemetry-trace scores for decaying fields from raw timestamps to decayed scores), or apply the same field-type-aware read inline and defer de-duplication? Plan recommends **delegate** (kills the drift that caused this bug). Confirm the telemetry-trace behavior change is acceptable.
2. **Decayed-score assertion style for the `:815-820` fixture:** derive the exact expected decayed value from the field's effective `decay_rate`/`base_score` (precise but re-derives the formula in the test), or assert the weaker invariant (`all > 0` and `all equal`, `score_spread == 0.0`)? Plan leans toward the weaker invariant to avoid brittleness if `Defaults.DECAY_RATE` is retuned. Preference?
