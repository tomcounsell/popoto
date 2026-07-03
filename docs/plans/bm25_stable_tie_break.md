---
status: Ready
type: bug
appetite: Small
owner: valorengels
created: 2026-07-03
tracking: https://github.com/tomcounsell/popoto/issues/446
revision_applied: true
---

# BM25Field: deterministic tie-break for equal-scored search results

## Problem

`BM25Field`'s ranking Lua script (`BM25_SEARCH_LUA` in
`src/popoto/fields/bm25_field.py`) sorts scored candidates with Lua's
`table.sort`, which is **not stable**: the Lua 5.1 reference manual explicitly
states that elements considered equal by the given order may have their
relative positions changed by the sort. Worse, candidates are collected via
`pairs(scores)` (hash-order iteration), so the pre-sort input order is itself
arbitrary. The comparator is score-only (`a[2] > b[2]`), so any two documents
with exactly equal BM25 scores have no defined relative order.

**Current behavior:** two documents with identical token statistics (identical
content is the simplest case) get identical BM25 scores and can come back in
either order — across runs, across server restarts, and across the top-K
truncation boundary. The CSR eval harness (#418) papers over this with a
corpus-authoring rule ("no BM25 score ties between assertion-referenced ids"),
which is a mitigation, not a fix. Production callers of `BM25Field.search` see
nondeterministic ordering of equal-scored results.

**Desired outcome:** identical searches always return identical orderings.
Ties are broken deterministically by member key (ascending, byte-wise), inside
the Lua script, so Redis and Valkey behave identically and truncation at
`limit` is also deterministic.

## Freshness Check

**Baseline commit:** `2d7f31f` (origin/main HEAD at plan time)
**Issue filed at:** 2026-07-03T09:56:52Z (today)
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/fields/bm25_field.py:292` — `table.sort(results, function(a, b) return a[2] > b[2] end)` — still holds, exactly at line 292.

**Cited sibling issues/PRs re-checked:**
- #418 — closed; shipped via PR #444 (`8b781df`, merged today). The issue was filed at #418 ship time per that plan's No-Gos, so #444 landing is expected context, not drift.

**Commits on main since issue was filed (touching referenced files):**
- `8b781df` feat(#418): deterministic CSR eval harness (PR #444) — irrelevant to the Lua comparator; it *added* the mitigation this plan relaxes (`tests/benchmarks/README.md` rule 3, `tests/benchmarks/test_csr.py` double-run gate).

**Active plans in `docs/plans/` overlapping this area:** none (deterministic_eval_harness.md is Complete; a parallel pipeline is running for #445, which is test-only and does not touch `bm25_field.py`).

**Notes:** Recon also found the **same unstable pattern** in
`src/popoto/fields/decaying_sorted_field.py:96` and
`src/popoto/fields/cyclic_decay_field.py:132`. Scoped out — filed as #448.

## Prior Art

- **PR #444 / issue #418**: deterministic CSR eval harness — introduced the double-run determinism gate (`tests/benchmarks/test_csr.py::test_double_run_identical`) and the corpus-authoring rule that avoids ties. That gate is the standing tripwire this fix makes robust.
- **PR #426 / issue #409**: made BM25 a first-class retrieval mode — increased the production blast radius of tie nondeterminism (hybrid retrieval consumes `BM25Field.search` output ordering via RRF).
- **PR #441/#443**: hybrid benchmark work — rank-sensitive metrics (MRR, Recall@k) consume BM25 ordering; ties silently jitter those metrics today.

No prior attempt to fix the comparator itself — the README rule was the only mitigation.

## Research

No WebSearch needed — the defect is a documented language semantic, not an
ecosystem question. Lua 5.1 reference manual (§`table.sort`): "The sort
algorithm is not stable; that is, elements considered equal by the given order
may have their relative positions changed by the sort." Redis and Valkey both
embed Lua 5.1-compatible interpreters and execute `EVAL` scripts identically;
a pure-Lua comparator change requires no modules and is portable by
construction. String comparison (`<`) in Lua is byte-wise via `strcmp`
semantics — deterministic and locale-independent for the binary-safe member
keys Popoto generates.

## Data Flow

1. **Entry point**: `BM25Field.search(model_class, field_name, query_text, limit)` (`bm25_field.py:487`) tokenizes the query and calls `POPOTO_REDIS_DB.eval(BM25_SEARCH_LUA, ...)`.
2. **Lua script** (`bm25_field.py:222-303`): accumulates per-doc scores in a hash table, collects `{dkey, score}` pairs via `pairs()` (arbitrary order), `table.sort`s them (unstable, score-only comparator — **the defect**), truncates to `limit`, returns a flat `[key, score, ...]` array.
3. **Python side**: parses the flat array into `list[tuple[str, float]]`, preserving Lua's order verbatim — no re-sort, so whatever order Lua emits is what callers (and RRF fusion in hybrid retrieval) consume.
4. **Output**: ranked `(redis_key, score)` list to callers; CSR harness assertions (`RanksAbove`, `InTopK`) and hybrid RRF read positions from it.

The fix belongs at step 2 and only step 2: a total-order comparator makes the output independent of both `pairs()` iteration order and sort instability. No Python-side change to `search()` is needed (re-sorting in Python would leave the *truncation* at `limit` nondeterministic — ties straddling the top-K cutoff must be resolved inside Lua, before truncation).

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none — `search()` signature and return shape unchanged. Ordering among equal scores becomes defined (member key ascending) where it was previously undefined; no caller can regress since no caller could rely on undefined order.
- **Coupling**: none added. The Lua script's `SHA` changes (redis-py `eval` sends the script; no cached-SHA bookkeeping exists in popoto).
- **Reversibility**: trivial — single comparator expression.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1 (PR review)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | Test suite backend |
| Test DB 14 (parallel pipeline on other DBs) | `POPOTO_TEST_DB=14 pytest tests/test_version.py -q` | Isolation from the #445 pipeline sharing this Redis |

## Solution

### Key Elements

- **Total-order Lua comparator**: primary key = score descending; secondary key = member key ascending (byte-wise `<`). Every pair of distinct members now has a defined order, so `table.sort` instability and `pairs()` hash order become unobservable.
- **Regression test**: plant multiple documents with identical token statistics; assert exact, repeatable, key-ascending tie order — including under adversarial insertion orders and across the `limit` truncation boundary.
- **Constraint relaxation**: the CSR harness corpus-authoring rule 3 ("no BM25 score ties between assertion-referenced ids") downgrades from *required* to *best practice* (ties no longer flake; distinct scores still make assertions more meaningful).

### Technical Approach

In `BM25_SEARCH_LUA` (`src/popoto/fields/bm25_field.py:292`), replace:

```lua
table.sort(results, function(a, b) return a[2] > b[2] end)
```

with a comparator of the shape:

```lua
table.sort(results, function(a, b)
    if a[2] ~= b[2] then
        return a[2] > b[2]
    end
    return a[1] < b[1]
end)
```

- The comparator is a **strict weak ordering** (required by `table.sort`): for distinct members it never returns true both ways, and member keys are unique within `results` (they are hash-table keys of `scores`), so equality of both fields cannot occur.
- **Why the key-uniqueness guarantee actually holds** (critique-mandated evidence, to be embedded in the Lua comment above the comparator): `dkey` is the inverted-index member `inv_results[j]`, which is the document's full `redis_key` — unique per model instance by ORM construction (`db_key.py`), stored untruncated, and each search runs against a single field's `inv_prefix` namespace. Lua table keys are unique, so two entries in `results` always carry unequal key strings, making `a[1] < b[1]` a total order on any tie set. NaN scores are unreachable: each score is a finite sum of `idf * tf_norm` terms with `df > 0` guarded, `tf > 0` (ZSCORE of an existing member), and `avgdl` guarded by `or 1`, so the `a[2] ~= b[2]` branch never sees NaN.
- Tie-break on `a[1] < b[1]` (member key ascending) is byte-wise string comparison — identical semantics on Redis and Valkey, no modules, no locale sensitivity.
- Update the `search()` docstring (`bm25_field.py:487`) to document the defined tie order.
- No config knob for tie-break direction — per repo rule, constants/behaviors like this are fixed, not configurable.

Documentation/comment cascade (kept in-scope because they assert the now-false "unstable" premise):
- `tests/benchmarks/README.md:115-118` — rule 3 becomes a best practice with updated rationale.
- `tests/benchmarks/csr/suites/default.py:43-45` — same rule mirrored in the suite-authoring comment.
- `tests/benchmarks/test_csr.py:98-99` — `test_double_run_identical` docstring calls itself "the standing tripwire for BM25 score ties"; reword (the gate remains valuable as a regression tripwire, but ties no longer reorder).

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope — the change is a pure Lua expression inside an existing script; error propagation from `EVAL` is unchanged and already covered by existing tests.

### Empty/Invalid Input Handling
Unchanged and already covered: `test_empty_corpus_returns_empty`, `test_empty_query_returns_empty`, `test_none_query_returns_empty`, `test_query_all_stop_words_returns_empty` in `tests/test_bm25_field.py`. The new comparator is only reached when `results` is non-empty.

### Error State Rendering
No user-visible rendering surface — library API only.

## Test Impact

- [ ] `tests/test_bm25_field.py` — UPDATE: add a new test class (e.g. `TestBM25TieOrdering`) with the regression tests below. No existing test asserts anything about tie order (verified: existing ranking tests use distinct scores), so none break.
- [ ] `tests/benchmarks/test_csr.py::test_double_run_identical` — UPDATE (docstring only): reword the "standing tripwire" note; assertions unchanged. Must still pass — it double-runs the whole suite and is the CI determinism gate.
- [ ] All other tests — unaffected: the script change only alters relative order of *equal-scored* members, which no existing test depends on.

**New regression tests** (all with `POPOTO_TEST_DB=14` during this pipeline):
1. *Tie order is key-ascending and insertion-order-independent*: plant ~5 documents with identical content under known keys, inserted in non-ascending key order (e.g. reversed/shuffled); assert `search()` returns them in exact key-ascending order.
2. *Repeated searches are identical*: run the same search ~10 times with no corpus writes between iterations (single-threaded pytest body on DB 14); assert all runs return the identical ordered list.
3. *Deterministic truncation at the limit boundary*: with 5 tied documents and `limit=3`, assert exactly the 3 lowest keys are returned, in order — proves the tie-break happens before top-K truncation.
4. *Mixed scores*: one clearly higher-scoring doc plus tied lower-scoring docs; assert the higher doc is first and the tied tail is key-ascending (tie-break must not disturb primary score ordering).

## Rabbit Holes

- **Re-sorting in Python instead of Lua** — looks simpler, but leaves top-K truncation nondeterministic when ties straddle the `limit` cutoff. The fix must be inside the script. (Also an explicit acceptance criterion.)
- **Fixing the sibling fields in the same PR** — `decaying_sorted_field.py:96` and `cyclic_decay_field.py:132` have the identical pattern, but each needs its own regression tests; scope-creep for a Small bug fix. Filed as #448.
- **Trying to write a test that reliably *fails* on the old code via repeated runs** — nondeterminism may not manifest within a single server process/seed. The insertion-order-adversarial test (plant in reverse key order) is the reliable red test; don't burn time on probabilistic flakiness reproduction.
- **Removing the CSR corpus rule entirely** — distinct scores still make `RanksAbove` assertions *meaningful* (a tie broken by key is not evidence of ranking quality). Downgrade to best practice; don't delete.

## Risks

### Risk 1: Comparator violates strict-weak-ordering and crashes `table.sort` ("invalid order function for sorting")
**Impact:** search raises a Redis script error.
**Mitigation:** member keys in `results` are unique by construction (hash-table keys), so the comparator is a total order on distinct elements; the two-level `if a[2] ~= b[2]` shape is the canonical safe pattern. Full BM25 test file exercises it against real Redis.

### Risk 2: Some consumer implicitly depended on the old (undefined) tie order
**Impact:** downstream ordering-sensitive tests (hybrid retrieval, CSR suite, external benchmarks fixture baselines) shift.
**Mitigation:** undefined order cannot be depended on *reliably*, but run the full suite plus `tests/benchmarks/test_csr.py` locally before the PR; the CSR corpus was designed tie-free for assertion-referenced ids, so its results must not change.

### Risk 3: Float score equality in Lua vs. "identical statistics" in the test
**Impact:** if planted docs don't produce bit-identical scores, the regression test wouldn't exercise the tie path.
**Mitigation:** plant *literally identical content* → identical tf, dl, and df contributions → identical float arithmetic → bit-identical scores. Assert equality of returned scores inside the test to prove the tie path was exercised.

## Race Conditions

No race conditions identified — the entire scoring/sorting change executes inside a single Lua `EVAL`, which Redis/Valkey run atomically on one thread. Test isolation from the parallel #445 pipeline is handled by `POPOTO_TEST_DB=14`.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #448] Same unstable `table.sort` pattern in `DecayingSortedField` (`decaying_sorted_field.py:96`) and `CyclicDecayField` (`cyclic_decay_field.py:132`) — identical fix shape but needs its own regression tests per field; filed as #448.
- [ORDERED] Merging this PR — merge coordination happens outside this pipeline run (parallel #445 pipeline is active on the same repo); pipeline stops after the docs gate.

## Update System

No update system changes required — pure library-internal Lua script change; no new dependencies, config, or migration (the inverted-index data model is untouched).

## Agent Integration

No agent integration required — `BM25Field.search` is an existing library API; its callers (hybrid retrieval, CSR harness) consume the fix transparently.

## Documentation

### Feature Documentation
- [ ] `tests/benchmarks/README.md` — rule 3 downgraded from required to best practice (acceptance criterion).
- [ ] Check `docs/fields.md` / `docs/benchmarks.md` for BM25 ordering claims; update if they assert or imply anything about tie behavior (surgical, via /do-docs gate).

### Inline Documentation
- [ ] `search()` docstring documents the deterministic tie order (score desc, then member key asc).
- [ ] Lua comment above the comparator explaining why the tie-break exists (unstable sort + hash-order `pairs`).
- [ ] Comment updates in `tests/benchmarks/csr/suites/default.py` and `tests/benchmarks/test_csr.py` per Technical Approach.

## Success Criteria

- [ ] Equal-scored members return in deterministic, key-ascending order across repeated identical searches (regression tests 1–4 above pass).
- [ ] The tie-break is entirely inside `BM25_SEARCH_LUA` — no Python-side re-sort, no Redis modules.
- [ ] `tests/benchmarks/README.md` rule 3 relaxed to best practice; mirrored comments in `test_csr.py` and `csr/suites/default.py` updated.
- [ ] Full local suite green: `POPOTO_TEST_DB=14 scripts/ci-local.sh --fast`.
- [ ] CSR determinism gate green: `POPOTO_TEST_DB=14 pytest tests/benchmarks/test_csr.py`.
- [ ] Documentation updated (`/do-docs`).

## Team Orchestration

Solo builder execution — the change is a single comparator plus tests and doc text; no parallel components to split. `/do-build` implements directly on branch `fix/446-bm25-stable-tie-break`, with PR review as the validation stage.

## Step by Step Tasks

### 1. Fix the Lua comparator
- **Task ID**: build-comparator
- **Depends On**: none
- **Validates**: tests/test_bm25_field.py (existing suite must stay green)
- Replace the score-only comparator at `bm25_field.py:292` with the two-level total-order comparator (score desc, member key asc), plus an explanatory Lua comment.
- Update the `search()` docstring to document the tie order.

### 2. Add regression tests
- **Task ID**: build-tests
- **Depends On**: build-comparator
- **Validates**: tests/test_bm25_field.py::TestBM25TieOrdering (create)
- Implement regression tests 1–4 from Test Impact in a new test class in `tests/test_bm25_field.py`, following the file's existing model/fixture conventions.
- Assert returned scores are equal among tied docs (proves the tie path is exercised).

### 3. Relax the CSR corpus-design constraint
- **Task ID**: build-docs-relax
- **Depends On**: build-comparator
- **Validates**: tests/benchmarks/test_csr.py
- `tests/benchmarks/README.md`: move rule 3 from the enforced/required list to a best-practice note with corrected rationale (ties are now deterministic; distinct scores still recommended so ranking assertions stay meaningful).
- Update mirrored comments in `tests/benchmarks/csr/suites/default.py` and the `test_double_run_identical` docstring in `tests/benchmarks/test_csr.py`.

### 4. Full verification
- **Task ID**: validate-all
- **Depends On**: build-comparator, build-tests, build-docs-relax
- `POPOTO_TEST_DB=14 pytest tests/test_bm25_field.py tests/test_hybrid_retrieval.py tests/test_context_assembler_hybrid.py tests/test_retrieval_quality_regression.py -q`
- `POPOTO_TEST_DB=14 pytest tests/benchmarks/test_csr.py -q`
- `POPOTO_TEST_DB=14 scripts/ci-local.sh --fast`
- `black --check src/ tests/` and `mypy src/`
- Open PR referencing #446 (implementation PR body uses `Closes #446`).

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| BM25 suite incl. new regression tests | `POPOTO_TEST_DB=14 pytest tests/test_bm25_field.py -q` | exit code 0 |
| CSR determinism gate | `POPOTO_TEST_DB=14 pytest tests/benchmarks/test_csr.py -q` | exit code 0 |
| Full fast CI | `POPOTO_TEST_DB=14 scripts/ci-local.sh --fast` | exit code 0 |
| Tie-break is inside Lua (anti-criterion: no Python re-sort added) | `grep -c "sorted(scored" src/popoto/fields/bm25_field.py` | match count == 0 |
| Comparator has tie-break | `grep -c "a\[1\] < b\[1\]" src/popoto/fields/bm25_field.py` | output > 0 |
| README rule relaxed | `grep -ci "best practice" tests/benchmarks/README.md` | output > 0 |
| No config knob added | `grep -c "TIE_BREAK" src/popoto/fields/bm25_field.py` | match count == 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Consolidated Critic (LITE) | Key-uniqueness guarantee for the tie-break asserted, not evidenced | Technical Approach now cites the concrete guarantee (dkey = full redis_key, unique per instance, per-field namespace; NaN unreachable) | Embed this rationale as the Lua comment above the comparator; verify `scores[dkey]` keys on `inv_results[j]` |
| NIT | Consolidated Critic (LITE) | Regression test 2 assumes static corpus during repeat loop | Test Impact updated: no writes between iterations | Trivially true in single-threaded pytest body on DB 14 |

**Verdict: READY TO BUILD (with concerns)** — revision applied 2026-07-03.

---

## Open Questions

None — scope, approach, and acceptance criteria are fully specified by issue #446; the one discovered ambiguity (sibling fields with the same defect) is resolved by scoping to #448.
