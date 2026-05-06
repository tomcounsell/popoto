---
status: Planning
type: bug
appetite: Small
owner: Valor
created: 2026-05-06
tracking: https://github.com/tomcounsell/popoto/issues/385
last_comment_id:
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

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Should partial-write hash deletion be opt-in or unconditional?** The plan currently treats it as unconditional (the issue's "Gap 2" says delete the orphan hash). An alternative would be a `delete_corrupt_hashes: bool = True` parameter for operators who want to inspect the hash contents before deletion. The issue text strongly implies "delete unconditionally" — proceeding with that unless told otherwise.
2. **Should `partial_writes` appear in the dict for non-AutoKeyField models?** Currently planned: always include the key, value is `0` for ineligible models. This keeps the dict shape stable for downstream consumers. Alternative: omit the key entirely. Stable shape is better for callers; flagging for confirmation.
3. **Single combined EXISTS+HGET pipeline vs. two sequential pipelines?** The plan describes two sequential round-trips for clarity. A single pipeline (EXISTS+HGET interleaved per key) halves the round-trip count. The implementation should prefer the single-pipeline form if it doesn't complicate the code substantially. Flagging for the builder's judgment.
