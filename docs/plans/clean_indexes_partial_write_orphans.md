---
status: Ready
type: bug
appetite: Small
owner: Valor
created: 2026-05-06
tracking: https://github.com/tomcounsell/popoto/issues/385
last_comment_id:
revision_applied: true
revision_applied_at: 2026-05-06T15:30:00Z
critique_verdict: READY TO BUILD (with concerns)
---

# clean_indexes() detects partial-write orphans (hash present, primary key absent)

## Problem

Popoto's `clean_indexes()` removes class-set entries that point to hashes which no longer exist in Redis (`EXISTS == 0`). It cannot detect a second class of corruption: **partial-write orphans** — hashes that exist in Redis but are missing the model's `AutoKeyField` primary key.

Without `id`, the ORM cannot compute `_redis_key`, so:
- `query.all()` returns instances with `id=None` and `_redis_key=None`
- `instance.delete()` silently no-ops (no key to target)
- The hash sits in Redis until its TTL expires, surfacing as a "ghost row" in any application that lists `query.all()` results

**Current behavior** (`src/popoto/models/base.py:3033-3045`):
```python
def _collect_orphans(keys_to_check: list) -> list:
    for i in range(0, len(keys_to_check), batch_size):
        ...
        for key in batch:
            pipe.exists(key)          # ← True for partial-write orphans
        results = pipe.execute()
        for key, exists in zip(batch, results):
            if not exists:
                orphans.append(key)   # ← partial-write orphans never get here
    return orphans
```

After the orphan is identified, only `srem` runs against the class set:
```python
pipe.srem(class_set_key, orphan)      # no pipeline.delete(orphan)
```

For absent hashes that is correct (nothing to delete). For partial-write orphans, the corrupt hash itself is left behind.

**Desired outcome:**

For models whose key is a single `AutoKeyField`, `clean_indexes()`:
1. Detects class-set members where the hash exists but is missing the auto-key field, treats them as orphans, and reports them in `check_indexes()` under a new `partial_writes` count.
2. When cleaning, removes them from the class set **and** deletes the orphan hash with `DEL`.

`check_indexes()` returns:
```python
{'class_set': 2, 'partial_writes': 1, 'key_fields': {...}, ..., 'total': N}
```

## Freshness Check

**Baseline commit:** `d2326052` (main, HEAD at plan time)
**Issue filed at:** `2026-05-06T05:48:18Z` (under 12 hours before plan time)
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/models/base.py:3033-3045` — `_collect_orphans` uses only `EXISTS` — confirmed unchanged.
- `src/popoto/models/base.py:3086-3092` — class-set cleanup runs `srem` only, no `delete` — confirmed unchanged.
- `src/popoto/models/base.py:2883-2893` — `_count_orphans` (in `check_indexes`) uses only `EXISTS` — confirmed unchanged.
- `src/popoto/models/base.py:2928-2999` — `check_indexes` return dict has no `partial_writes` key — confirmed unchanged.

**Cited sibling issues/PRs re-checked:**
- #320 / PR #334 — `clean_indexes()` introduction — merged, current code matches issue's claims.
- #322 / PR #332 — `check_indexes()` introduction — merged, current code matches issue's claims.
- The deprecated `keys(clean=True)` path — explicitly out of scope per the issue.

**Commits on main since issue was filed (touching `src/popoto/models/base.py`):** none.

**Active plans in `docs/plans/` overlapping this area:**
- `docs/plans/clean-indexes.md` — the original plan for `clean_indexes()` (merged 2026-03-31). This plan extends that work; no live work overlap.
- `docs/plans/check-indexes.md` — the original plan for `check_indexes()` (merged 2026-03-31). Same — extension, no overlap.

**Notes:** No drift. The issue's sketch code lines up with the current implementation.

## Prior Art

- **#320 / PR #334**: Added `Model.clean_indexes()` for production-safe orphan cleanup using SCAN. Established the `_collect_orphans` pipeline-batched `EXISTS` pattern and the SREM/ZREM/HDEL cleanup pattern that this fix extends. Did NOT consider partial-write hashes.
- **#322 / PR #332**: Added `Model.check_indexes()` read-only health check, returning structured dict with per-type orphan counts. This fix adds a new top-level `partial_writes` key to that dict.
- **#380 / PR #381 (recent fix)**: `fix(encoding): initialize defaults for fields absent from lazy-loaded hash`. Adjacent — the partial-load decoding path now tolerates missing fields by defaulting them. That makes ghost rows visible (`id=None` instead of crash) but does not clean them up — exactly the symptom this issue addresses.
- **#146 / PR #158**: `rebuild_indexes()` and field-index edge-case tests — established the iteration pattern reused by `check_indexes`/`clean_indexes`.

## Why Previous Fixes Failed

The original `check_indexes`/`clean_indexes` pair (#322, #320) modeled orphans as *missing keys*. That captures the most common failure mode (ORM-bypassing deletes, manual `DEL`, expired hashes whose index entries weren't removed). It does not capture *corrupt-but-present* keys.

| Prior Fix | What It Did | Why It Was Incomplete |
|-----------|-------------|----------------------|
| PR #334 (clean_indexes) | `EXISTS`-based orphan detection + SREM/ZREM/HDEL | Treated `EXISTS == 1` as proof of validity. Did not consider hashes that exist but are structurally unusable. |
| PR #332 (check_indexes) | Read-only counterpart with structured dict report | Same `EXISTS`-based detection. Reports omit a category the operator cannot otherwise discover. |
| PR #381 (lazy-load defaults) | Tolerated missing fields during decode | Made the symptom (ghost rows in `query.all()`) visible without a repair path. |

**Root cause pattern:** The orphan model conflates "key absent" with "key invalid." Partial-write orphans require an *additional* validity check (primary key field present in hash body) layered on top of `EXISTS`. The check is cheap (one extra `HGET` per existing key) but must be model-aware (only AutoKeyField models qualify — the auto field name varies, and composite-KeyField models use different primary-key semantics).

## Data Flow

This change touches the class-set processing in two methods. The data flow is:

1. **Entry point**: `Model.check_indexes()` or `Model.clean_indexes()` (sync or async wrapper)
2. **Class set scan**: `SSCAN $Class:ModelName` -> list of redis keys
3. **Existence batching** (current): pipeline `EXISTS` per key
4. **Validity batching** (new): for the keys that `EXISTS` reports as present, pipeline `HGET <key> <auto_key_field>` to detect missing primary key
5. **Categorize**:
   - `EXISTS == 0` -> "absent orphan" (existing behavior; SREM only)
   - `EXISTS == 1` AND `HGET == None or ""` -> "partial-write orphan" (new; SREM + DEL)
   - `EXISTS == 1` AND `HGET == <value>` -> healthy (no action)
6. **Apply**: pipeline SREM (and DEL for partial-write orphans)
7. **Report**: returned int (`clean_indexes`) increments by both categories; `check_indexes` returns separate `class_set` and `partial_writes` counts

The fix only applies to step 4 in **the class set scan** — the other index types (key fields, sorted fields, geo fields, composite indexes) reference instance keys but are not the primary index of record, and the partial-write hash is already detected once via the class set. No change to those scans is needed.

**Eligibility gate**: Only models whose primary key is a single `AutoKeyField` qualify for partial-write detection. For composite-`KeyField` models the field-presence check is more complex (multiple key fields, value semantics differ) and is explicitly out of scope per the issue.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**:
  - `check_indexes()` return dict gains a `partial_writes: int` key. The `total` calculation includes it. This is an additive change — existing callers keying off `class_set`, `key_fields`, etc. still work.
  - `clean_indexes()` return type stays `int` (now includes partial-write removals).
  - No public API additions or removals.
- **Coupling**: Slightly higher coupling between `check_indexes`/`clean_indexes` and the AutoKeyField introspection (`getattr(field, "auto", False)`). The pattern is already used in `clean_indexes` step 2 (`if getattr(field, "auto", False): continue`) so the precedent exists.
- **Data ownership**: unchanged.
- **Reversibility**: trivial — both methods continue to work for non-AutoKeyField models with the existing `EXISTS`-only behavior, and the new dict key is additive.

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0 (issue is self-contained, sketch code provided)
- Review rounds: 1 (single PR, single file change)

## Prerequisites

No prerequisites — this work depends only on Redis (already required by the existing test suite).

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on `localhost:6379` (test DB 15) | `redis-cli -n 15 ping` | Same as existing test suite. |

## Solution

### Key Elements

- **Auto-key field detection**: A small classmethod helper that returns the name of the model's single AutoKeyField, or `None` if the model has no AutoKeyField or has multiple key fields. Models without a single AutoKeyField bypass partial-write detection (existing behavior preserved).
- **Hash-validity probe**: Inside the class-set processing of both methods, after the `EXISTS` batch, run a second pipelined batch of `HGET <key> <auto_key_field>` against the keys that exist. Empty/missing values mark the key as a partial-write orphan.
- **Cleanup with hash deletion**: In `clean_indexes`, partial-write orphans receive `pipeline.srem(class_set, orphan)` AND `pipeline.delete(orphan)` so no Redis memory is left behind.
- **Reporting**: `check_indexes` adds a top-level `partial_writes` count; `total` includes it. `clean_indexes` rolls partial-write removals into its returned int.

### Flow

`Model.clean_indexes()` is invoked → SSCAN class set → batch EXISTS to find absent orphans → for present keys, batch HGET on auto-key field to find partial-write orphans → pipeline SREM (both categories) + pipeline DEL (partial-write only) → continue with field/sorted/geo/composite scans (unchanged) → return total removed.

`Model.check_indexes()` follows the same shape but reports counts only.

### Technical Approach

- Add a private helper (e.g., `_get_auto_key_field_name(cls) -> str | None`) on `Model` that returns the lone AutoKeyField name (`getattr(field, "auto", False)`) when there is exactly one such field. Returns `None` otherwise — this is the eligibility gate.
- Refactor `_collect_orphans` and `_count_orphans` so the class-set processing can take an optional `auto_key_field_name` argument:
  - When `auto_key_field_name is None`: behavior is identical to today (EXISTS only).
  - When set: keys that pass `EXISTS` get a second pipelined `HGET` round; those returning empty/None are added to a separate "partial-write" bucket.
- For `check_indexes`, keep `_count_orphans` returning a single int for non-class-set scans and have a class-set-specific counter that returns `(absent_count, partial_write_count)`. Update the result dict to include `partial_writes` and update the `total` summation.
- For `clean_indexes`, return a tuple-style result from the class-set helper `(absent_orphans, partial_write_orphans)`. After SREM, also pipeline `DEL` for the partial-write orphans.
- Async wrappers (`async_check_indexes`, `async_clean_indexes`) need no changes — they delegate via `to_thread`.
- Document the new `partial_writes` key in the docstring of `check_indexes` and add a note to `clean_indexes` describing that partial-write hashes are deleted (not merely de-indexed).

**Edge cases to handle in code:**
- Model has no AutoKeyField (composite KeyField only): skip partial-write detection. Old behavior.
- Model has an AutoKeyField under a custom name (e.g., `id = AutoKeyField()`): use that name (introspect — don't hardcode `_auto_key`).
- HGET returns `b""` (empty bytes) vs `None`: both must be treated as "missing." The `redis-py` client typically returns bytes; the test must cover this.
- Race condition: a healthy instance is being saved concurrently and the HGET fires between the SADD and the HSET completion. See Race Conditions section below.

## Failure Path Test Strategy

### Exception Handling Coverage
- The methods `_collect_orphans` and `_count_orphans` do not currently catch exceptions. The new HGET batching reuses the same pattern (pipelined commands raise on the `.execute()` call, propagating to the caller). No new `except Exception: pass` blocks will be introduced. State: "No exception handlers in scope."

### Empty/Invalid Input Handling
- A class set with zero members is already handled (the `if members:` guard). No change.
- An AutoKeyField that returns an empty string from HGET must be treated as a partial-write orphan — explicit test.
- A hash with the auto-key field set to a whitespace-only string is a degenerate case; for now we treat it as healthy (only None and empty bytes/str count as missing) and note this in the docstring. Whitespace stripping would be over-reach without a real reproducer.

### Error State Rendering
- These methods have no user-visible UI; the failure path is the operator running the command and seeing the orphan count. The new test will assert that the count reflects the partial-write orphan, so an absent or wrong count will fail the test loudly. No silent-failure surface.

## Test Impact

Existing tests in `tests/test_clean_indexes.py` and `tests/test_check_indexes.py` should continue to pass unchanged — the change is additive. The `total` computation in `check_indexes` now includes `partial_writes`, but existing tests assert `>= N` rather than `== N` for total, so they remain valid.

- [ ] `tests/test_check_indexes.py::TestCheckIndexesReturnStructure::test_return_keys_present` — UPDATE: assert that `partial_writes` is among the returned keys.
- [ ] `tests/test_check_indexes.py::TestCheckIndexesReturnStructure::test_return_types` — UPDATE: assert `partial_writes` is `int`.
- [ ] `tests/test_check_indexes.py` — ADD: new test class `TestCheckIndexesPartialWriteOrphans` covering the AutoKeyField partial-write scenario, the composite-KeyField negation (no detection), and round-trip with `clean_indexes`.
- [ ] `tests/test_clean_indexes.py` — ADD: new test class `TestCleanIndexesPartialWriteOrphans` covering removal from both class set and Redis hash, and round-trip with `check_indexes`.

No tests are deleted or replaced.

## Rabbit Holes

- **Partial-write detection for composite-KeyField models.** The issue explicitly defers this. Models with multiple `KeyField`s have richer key semantics — every key field would need to be present and consistent with the redis_key. This is a separate feature and a separate PR.
- **Auto-repair from raw hash data.** Tempting to think we could synthesize an `id` from the redis_key (which encodes the auto-key value). But the whole point of this fix is that the hash is *unrecoverable* from the application's perspective — re-injecting an id without the application's knowledge is a foot-gun. Delete is the right answer.
- **Whitespace/zero-value normalization.** Hashes whose `id` field is a whitespace-only string are pathological but not what was observed. Don't strip; document the boundary as None/empty only.
- **Adding a new `purge_orphans()` method.** Out of scope. The fix lives inside the existing methods. A new method would force operators to run two commands and break the documented `check_indexes()` -> `clean_indexes()` workflow.
- **Investigating the upstream cause of partial writes.** The issue mentions `pipeline.hdel(redis_key, "id")` migrations and process crashes mid-pipeline. Identifying every code path that can produce a partial-write is a separate audit (potentially issue-worthy) and not required for the cleanup-side fix.

## Risks

### Risk 1: HGET round-trip overhead
**Impact:** A second pipelined HGET batch per class-set scan adds one network round-trip per `batch_size` keys. For a 100k-instance class set with default batch_size=1000, that's an extra 100 round-trips on top of the existing 100 EXISTS round-trips. Doubles the Redis traffic for the class-set scan only (other scans untouched).
**Mitigation:** Document the cost in the docstring. The work is still SCAN-friendly and the operation is opt-in maintenance, not a hot path. We could batch EXISTS+HGET into a single pipeline (one round-trip per batch, not two) — implementation detail to keep in mind during build.

### Risk 2: Race with concurrent save
**Impact:** A save in another process runs `SADD class_set` then `HSET hash` non-atomically (Redis pipelines aren't transactions, and a process crash can interleave). If `clean_indexes` fires between SADD and HSET on a fresh instance, it could see "exists but missing primary key" and delete a record that's about to be valid.
**Mitigation:** Document the existing guidance: run during low-traffic windows (already in the `clean_indexes` docstring). The race window is narrower than the existing absent-key race window (HSET completes very fast after SADD for a single-pipeline save). The scenario this fix targets — a *crashed* save that never completed — is exactly when we *want* removal. We can also note in the docstring that operators worried about the race should snapshot first or run twice and only act on entries flagged in both runs.

### Risk 3: Migration HDEL timing
**Impact:** A migration that issues `pipeline.hdel(redis_key, "id")` (per the migration docs) for a brief window leaves the hash without its primary key. If `clean_indexes` runs during a migration, it would delete the in-flight migrated record.
**Mitigation:** Migrations are operator-initiated; the existing operational guidance is "don't run cleanup during migrations." Add an explicit note to the docstring of both methods.

## Race Conditions

### Race 1: Save in flight between SADD and HSET
**Location:** `src/popoto/models/base.py:1276-1286` (sync save path: hset → expire → sadd) and `src/popoto/models/base.py:1352-1363` (internal pipeline path).
**Trigger:** A separate process is saving a new instance. The pipeline is not transactional; if the process crashes after SADD but before HSET completes (or any pipeline command sequencing leaves the hash empty), the hash will exist briefly without a primary key.
**Data prerequisite:** The pipeline must execute SADD before HSET, OR the HSET command must be in flight when the cleanup HGET runs. In practice the save's `pipeline.hset(...)` line precedes `pipeline.sadd(...)` (see line 1277 vs line 1284), so a successful pipeline always writes the hash first. The race only opens if save crashes mid-pipeline.
**State prerequisite:** Concurrent traffic on the same model class.
**Mitigation:** Documented operator guidance — run cleanup during low-traffic windows. The race window is brief and the only false positive is a partially-completed save, which is exactly what we want to clean up. No code-level locking — Redis cleanup is a maintenance op, not a user-facing path.

### Race 2: Migration `pipeline.hdel(redis_key, "id")` window
**Location:** Migrations described in `src/popoto/models/migrations.py` (e.g., line 206-207) use HDEL to drop legacy fields. If a migration script's HDEL targets the AutoKeyField (or runs against models where the field name is reused), the hash temporarily lacks the key.
**Trigger:** Operator runs `clean_indexes` while a migration is running.
**Data prerequisite:** None — the cleanup just needs to fire during the HDEL window.
**State prerequisite:** Active migration touching the auto-key field.
**Mitigation:** Documented in docstring — do not run `clean_indexes` during migrations. This is consistent with existing operational guidance.

## No-Gos (Out of Scope)

- Partial-write detection for models with composite `KeyField` keys (per the issue).
- The deprecated `Query.keys(clean=True)` path — already superseded by `clean_indexes()`.
- A new public method (e.g., `purge_orphans`) — the fix lives inside the existing methods.
- Auto-repair (synthesize a primary key from the redis_key and rewrite the hash). The records are intentionally treated as unrecoverable.
- Whitespace-only primary key normalization — only None and empty values count as missing.
- Audit of upstream code paths that produce partial-writes (separate concern; would warrant its own issue).

## Update System

No update system changes required — this is a library-internal fix. Operators consume it on the next `popoto` upgrade.

## Agent Integration

No agent integration required — `popoto` is a library, not the bridge runtime. The Telegram bridge does not invoke this code path.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/clean_indexes.md` (if it exists; otherwise the existing `docs/plans/clean-indexes.md` notes are sufficient and the user-facing reference is the docstring).
- [ ] Update the docstrings on `Model.check_indexes` and `Model.clean_indexes` to:
  - Describe the new `partial_writes` field in the return dict.
  - Note that `clean_indexes` deletes partial-write hashes (not merely de-indexes them).
  - Reiterate "do not run during migrations or high-traffic windows."

### External Documentation Site
- [ ] Verify the popoto docs site (mkdocs) renders the new docstring text. If `docs/api.md` or similar references these methods, update the prose to mention the partial-write category. `mkdocs serve` smoke-test.

### Inline Documentation
- [ ] Code comments in `_collect_orphans` and `_count_orphans` (or their renamed/refactored versions) explaining the two-step EXISTS+HGET pattern and why composite-KeyField models are skipped.

## Success Criteria

- [ ] `check_indexes()` return dict includes `partial_writes: int` and `total` includes it.
- [ ] For an AutoKeyField model: a manually-constructed class-set member pointing at a hash with non-`id` fields only is reported under `partial_writes` and removed by `clean_indexes()` from BOTH the class set AND Redis.
- [ ] For a composite-KeyField model: partial-write detection is skipped (existing behavior preserved). No `partial_writes` count incremented even if such a hash exists.
- [ ] All existing tests in `tests/test_clean_indexes.py` and `tests/test_check_indexes.py` pass.
- [ ] New tests cover: AutoKeyField partial-write detect-and-clean, composite-KeyField negation, round-trip (clean -> check returns 0), async wrappers respect the new behavior.
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

When this plan is executed, the lead agent orchestrates work using Task tools. The lead NEVER builds directly - they deploy team members and coordinate.

### Team Members

- **Builder (clean-indexes-partial-write)**
  - Name: `clean-indexes-partial-write-builder`
  - Role: Implement partial-write detection in `check_indexes` and `clean_indexes`, including the auto-key-field helper, refactored orphan collectors, and docstring updates.
  - Agent Type: builder
  - Resume: true

- **Test Engineer (clean-indexes-partial-write)**
  - Name: `clean-indexes-partial-write-tests`
  - Role: Add `TestCheckIndexesPartialWriteOrphans` and `TestCleanIndexesPartialWriteOrphans` test classes covering detection, removal-with-hash-delete, composite-KeyField negation, round-trip, and async wrappers.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (clean-indexes-partial-write)**
  - Name: `clean-indexes-partial-write-validator`
  - Role: Run the full test suite, verify all success criteria, confirm `check_indexes()` return dict shape, and confirm composite-KeyField models are unaffected.
  - Agent Type: validator
  - Resume: true

### Available Agent Types

(Standard list — see template.)

## Step by Step Tasks

### 1. Implement partial-write detection
- **Task ID**: build-partial-write
- **Depends On**: none
- **Validates**: `tests/test_check_indexes.py`, `tests/test_clean_indexes.py`
- **Assigned To**: clean-indexes-partial-write-builder
- **Agent Type**: builder
- **Parallel**: true
- Add a private classmethod helper `_get_auto_key_field_name()` on `Model` that returns the single AutoKeyField name (via `getattr(field, "auto", False)`) or `None` when the model has zero or multiple AutoKeyFields.
- In `check_indexes`, refactor the class-set scan: after `EXISTS`, when an auto-key field is identified, run a second pipelined `HGET` batch on the present keys; classify into `class_set` (absent) and `partial_writes` (present-but-missing-key) buckets. Add `partial_writes` to the returned dict and include it in `total`.
- In `clean_indexes`, refactor the class-set scan: collect both absent and partial-write orphans; pipeline SREM both, plus pipeline DEL for partial-write orphans. Roll both into the return count.
- Update docstrings on `check_indexes` and `clean_indexes` to describe the new behavior and operational caveats.
- Make the implementation tolerate `b""`, `""`, and `None` as "missing primary key."

### 2. Add tests for partial-write detection and cleanup
- **Task ID**: build-partial-write-tests
- **Depends On**: none
- **Validates**: `tests/test_check_indexes.py`, `tests/test_clean_indexes.py`
- **Assigned To**: clean-indexes-partial-write-tests
- **Agent Type**: test-engineer
- **Parallel**: true
- Add `TestCheckIndexesPartialWriteOrphans` to `tests/test_check_indexes.py`:
  - Setup: manually `HSET` a hash with non-`id` fields and `SADD` it into the class set.
  - Assert `result["partial_writes"] == 1` and `result["total"]` increases accordingly.
  - Assert `class_set` (absent) count is unaffected when the hash is present.
  - Assert composite-KeyField model does NOT increment `partial_writes`.
  - Assert async wrapper returns the same dict shape.
- Add `TestCleanIndexesPartialWriteOrphans` to `tests/test_clean_indexes.py`:
  - Setup: same as above.
  - Assert `clean_indexes()` returns >= 1.
  - Assert the class-set member is gone (`SISMEMBER == 0`).
  - Assert the orphan hash is deleted (`EXISTS == 0`).
  - Round-trip: re-run `check_indexes()` and assert `partial_writes == 0`.
  - Async variant.
- Update `TestCheckIndexesReturnStructure::test_return_keys_present` and `test_return_types` to assert `partial_writes` key/type.

### 3. Validate
- **Task ID**: validate-partial-write
- **Depends On**: build-partial-write, build-partial-write-tests
- **Assigned To**: clean-indexes-partial-write-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_check_indexes.py tests/test_clean_indexes.py -v`.
- Run the full suite: `pytest`.
- Confirm new tests pass and no existing tests regress.
- Confirm `check_indexes` return dict includes `partial_writes` and `total` reflects it.
- Confirm composite-KeyField models behave identically to before.

### 4. Documentation
- **Task ID**: document-partial-write
- **Depends On**: validate-partial-write
- **Assigned To**: documentarian (use `/do-docs` skill)
- **Agent Type**: documentarian
- **Parallel**: false
- Update or create `docs/features/clean_indexes.md` to describe partial-write detection and the operational guidance.
- Verify `mkdocs serve` renders the updated docstrings cleanly.
- Update any reference doc for `check_indexes`/`clean_indexes` to mention the new `partial_writes` key.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-partial-write
- **Assigned To**: clean-indexes-partial-write-validator
- **Agent Type**: validator
- **Parallel**: false
- Run all verification commands below.
- Confirm all success criteria are met.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Targeted tests pass | `pytest tests/test_check_indexes.py tests/test_clean_indexes.py -v` | exit code 0 |
| Full test suite | `pytest` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| `partial_writes` key in dict | `python -c "import popoto; print('partial_writes' in popoto.Model.__subclasses__()[0].check_indexes())"` (run against a known subclass; smoke check during PR review) | output contains `True` |

## Critique Results

**Verdict:** READY TO BUILD (with concerns) — recorded 2026-05-06T10:24:43Z by `/do-plan-critique`.

The critique skill recorded the verdict headline but did not persist a per-finding breakdown. The notes below were derived from a self-critique re-read of the plan (Risks, Race Conditions, Edge Cases, Open Questions) during the revision pass. They capture every "with concerns" item as a load-bearing instruction that the builder MUST honor.

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| Medium | Operator (race) | A concurrent save's pipeline always orders `hset` before `sadd` (`base.py:1276-1286`). A *crashed* save mid-pipeline is the only false-positive trigger — and that is exactly what we want to clean up. | Code | Do **not** add cross-process locking. Reuse existing operator guidance (run during low-traffic windows) and surface it in the new docstring. See Race 1 in the plan. |
| Medium | Operator (migration) | Migrations that issue `pipeline.hdel(redis_key, "id")` (e.g. `migrations.py:206-207`) leave a brief HDEL window where a healthy hash legitimately lacks the auto-key. | Docs | Add a one-liner to the docstrings of both `check_indexes` and `clean_indexes`: "Do not run during active migrations or HDEL-based field migrations." See Race 2 in the plan. |
| Medium | Skeptic (perf) | The current sketch issues one EXISTS pipeline + one HGET pipeline per batch — two round-trips. Open Question #3 already resolved this to a **single interleaved pipeline**. | Code | Build MUST implement the interleaved variant from Open Question #3 (lines 437-454). Two-pipeline version is rejected. The 2:1 result-pairing requires an inline comment per the codebase clarity bar. |
| Medium | Adversary (eligibility) | The eligibility gate (single AutoKeyField only) is critical: composite-KeyField models must take the original EXISTS-only path. A regression here silently widens the deletion blast radius. | Code | The gate function `_get_auto_key_field_name()` MUST return `None` when there are zero OR multiple AutoKeyFields. Build MUST add a test where a composite-KeyField model with a deliberately-corrupt hash is **not** flagged or deleted. (Already in `Step by Step Tasks` step 2 — keep it.) |
| Medium | Skeptic (semantics) | Hash decoding empties: `redis-py` returns `b""` for empty bytes, `None` for missing fields. Either must count as "missing primary key." A whitespace-only string must NOT count as missing (out of scope per Rabbit Holes). | Code | Build MUST normalize the HGET return as: `if hget_value is None or hget_value == b"" or hget_value == "":` → orphan. Whitespace stripping is forbidden. Add an explicit unit test for both `b""` and `None` return values. |
| Low | Archaeologist (return shape) | `check_indexes()` initializes its return dict with all keys upfront (`base.py:2928-2935`). Open Question #2 already resolved this to **always include `partial_writes`**. | Code | Build MUST add `"partial_writes": 0` to the return-dict initializer alongside `"class_set"`. Do not gate it behind the AutoKeyField check. The structure tests in `tests/test_check_indexes.py:305-323` will catch a regression. |
| Low | Archaeologist (return total) | `total` summation logic in `check_indexes` must include `partial_writes`. Existing structure tests assert `>=` on total, so a missed addition is silent. | Code | Build MUST update the `total` summation to include `partial_writes`. Add an explicit assertion in the new `TestCheckIndexesPartialWriteOrphans` class: `assert result["total"] >= result["partial_writes"]`. |
| Low | Simplifier (DRY) | `_collect_orphans` and `_count_orphans` will both need the same EXISTS+HGET interleaving logic. | Code | Extract the class-set scanning logic into a single private helper that returns `(absent_orphans, partial_write_orphans)` and call it from both methods. Avoid copy-pasting the pipeline interleave. |
| Low | User (docstring) | The new behavior is operator-visible. Without docstring updates, operators will not know `clean_indexes` now also DELs hashes. | Docs | Build MUST update docstrings on **both** `check_indexes` and `clean_indexes` in the same PR. The `partial_writes` key, the DEL behavior, and the migration/low-traffic guidance must all be present. Plan task `document-partial-write` covers the broader docs/ pass. |
| Low | Adversary (async) | Async wrappers (`async_check_indexes`, `async_clean_indexes`) delegate via `to_thread`. A new code path that doesn't reuse the same internal helper would silently bypass the fix in async callers. | Code | Build MUST verify (and the test step MUST cover) that the async wrappers return the new dict shape with `partial_writes` populated. No separate async implementation. |

### Implementation Notes (from CRITIQUE) — load-bearing checklist for builder

Builder MUST treat these as acceptance gates in addition to the Success Criteria:

1. **Single-pipeline interleave** — implement the EXISTS+HGET pattern as shown in Open Question #3 (lines 437-454). One `pipe.execute()` per batch. Inline-comment the 2:1 results unzipping.
2. **Eligibility gate is strict** — `_get_auto_key_field_name()` returns `None` for zero OR multiple AutoKeyFields. Composite-KeyField models take the original EXISTS-only path with no behavioral change. Add a regression test.
3. **Normalize "missing"** — treat `None`, `b""`, and `""` as missing primary key. Do not strip whitespace; do not coerce types. Test both `b""` and `None`.
4. **Stable dict shape** — `partial_writes: 0` initialized in the result dict alongside `class_set`, regardless of model eligibility. `total` summation includes it.
5. **DRY the helper** — single private classmethod or inner function returns `(absent_orphans, partial_write_orphans)`; both `check_indexes` and `clean_indexes` consume it.
6. **DEL plus SREM for partial-writes** — pipeline both. Absent orphans get SREM only (existing behavior). Order in the pipeline does not matter, but both must commit in the same `.execute()`.
7. **Docstring discipline** — update `check_indexes` AND `clean_indexes` docstrings in the same diff. Mention: new `partial_writes` key, DEL behavior, "do not run during migrations or high-traffic windows."
8. **Async parity** — the async wrappers are not separately implemented; they must transparently inherit the new behavior. Test classes MUST include an async variant (already required by Step 2 of the task list — do not drop it).
9. **No new flags / no new public methods** — Open Question #1 resolved against opt-in. `clean_indexes()` signature is unchanged; deletion is unconditional. Do not introduce `dry_run`, `force`, or `purge_orphans`.
10. **No upstream-cause investigation** — this PR fixes the cleanup side only. Surface any newly-discovered upstream partial-write code paths as a follow-up issue, not a code change in this PR.

---

## Open Questions

_All three open questions were resolved by codebase research on 2026-05-06; no operator confirmation needed. Resolutions captured below._

### 1. Opt-in vs. unconditional hash deletion → **Unconditional** ✅
**Decision:** Delete the orphan hash unconditionally — no `delete_corrupt_hashes` flag.

**Evidence in repo:**
- `src/popoto/models/base.py:3004` — current `clean_indexes(cls, batch_size: int = 1000)` signature has no `dry_run` / `force` / `confirm` flag. The method's contract is already "clean all index corruption."
- `src/popoto/models/base.py:3088-3092` — existing absent-orphan removal runs `pipe.srem(...)` unconditionally; no guard.
- The only `dry_run` parameter in the project is on `migrate_to_partitioned()` in `confidence_field.py` (data migration, not cleanup). `delete_all()` in `base.py` is also unconditional. Adding a flag here would diverge from popoto convention.
- `tests/test_clean_indexes.py:72-90` — every test calls `clean_indexes()` with no parameters and expects deletion to happen. No flag-based suppression precedent.

If operators need to inspect first, the documented `check_indexes()` → `clean_indexes()` workflow already covers it.

### 2. `partial_writes` always present in dict → **Always include** ✅
**Decision:** Always include `partial_writes: int` in the `check_indexes()` return dict, value `0` for non-AutoKeyField models.

**Evidence in repo:**
- `src/popoto/models/base.py:2928-2935` — return dict is initialized with all keys upfront, regardless of model:
  ```python
  result = {
      "class_set": 0,
      "key_fields": {},
      "sorted_fields": {},
      "geo_fields": {},
      "composite_indexes": {},
      "total": 0,
  }
  ```
- `tests/test_check_indexes.py:305-323` — `TestCheckIndexesReturnStructure::test_return_keys_present` and `test_return_types` assert each key MUST be present and have the right type, using bare `in` checks.
- `tests/test_check_indexes.py:108-117` — `test_minimal_model_returns_zeros` confirms a model with no sorted/geo/composite indexes still gets `sorted_fields: {}`, `geo_fields: {}`, `composite_indexes: {}` (zero-shaped, not omitted).

Stable dict shape is the established convention. Downstream consumers (logging, dashboards) can rely on `result["partial_writes"]` always being a valid int.

### 3. Single combined EXISTS+HGET pipeline vs. two sequential → **Single combined pipeline** ✅
**Decision:** Use a single pipeline interleaving `EXISTS` and `HGET` per key (one round-trip per batch). Refactor results-unzipping with a clear inline comment.

**Evidence in repo:**
- `src/popoto/models/base.py:1276-1286` and `1352-1363` — the on-save path already pipelines `hset` + `expire`/`expireat` + `sadd` together. Three different command types in one round-trip is the established pattern.
- `src/popoto/models/base.py:1330-1334` — composite-index save pipelines `hdel` + `hset` together.
- The current `_collect_orphans` (`base.py:3033-3045`) is simple and amenable to refactor:
  ```python
  pipe = POPOTO_REDIS_DB.pipeline()
  for key in batch:
      pipe.exists(key)
      pipe.hget(key, auto_key_field_name)  # interleaved
  results = pipe.execute()
  # results[2*i]   == EXISTS for batch[i]
  # results[2*i+1] == HGET   for batch[i]
  for i, key in enumerate(batch):
      exists, hget_value = results[2*i], results[2*i + 1]
      if not exists:
          absent_orphans.append(key)
      elif not hget_value:
          partial_write_orphans.append(key)
  ```

Halves Redis round-trips for the class-set scan with no readability penalty. The 2:1 result-pairing must be inline-commented per the codebase's general clarity bar.

**Caveat:** When `auto_key_field_name is None` (composite-KeyField models, ineligible for partial-write detection), the loop must NOT issue the `hget` — fall back to the original EXISTS-only pipeline. Two code paths, gated on the eligibility flag.
