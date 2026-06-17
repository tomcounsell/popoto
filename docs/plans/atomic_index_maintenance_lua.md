---
status: Planning
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-17
tracking: https://github.com/tomcounsell/popoto/issues/412
last_comment_id:
---

# Atomic Secondary-Index Maintenance via Lua (cross-process save() race)

## Problem

Popoto maintains secondary indexes as plain Redis Sets (`$IndexF:Model:field:value`
for `IndexedField`, `$UniquF:Model:field:value` for `UniqueField`). `Model.save()`
maintains these via `IndexedFieldMixin.on_save()` using a **read-then-write** pattern
with **client-side state**:

1. The old Set to SREM from is derived from `_saved_field_values` — a Python snapshot
   taken at hydration (`indexed_field_mixin.py:136-147`). Each `save()` is internally
   atomic (one MULTI/EXEC), but the snapshot can be stale by EXEC time.
2. The `unique=True` membership check is an immediate `SMEMBERS` at queue time,
   outside the transaction (`indexed_field_mixin.py:150-166`), with the SADD deferred
   to EXEC — classic check-then-act.

**Current behavior:** When two OS processes concurrently `save()`, indexes corrupt.
The June 2026 audit PoC measured (reconfirmed against main @ 61f36aa on 2026-06-17):

- **58–59/200 rounds (~29%)**: the same record present in *both* the old and new
  value Sets at once (dual membership). Serial control: 0.
- `filter(status="A")` returns a record whose stored `status` is actually `"B"` —
  silently wrong query results, no error, stale entry persists.
- **67–68/100 UniqueField rounds**: two processes saving different records with the
  same unique value both succeed, leaving the value Set with 2 members.

**Desired outcome:** Concurrent `save()` from any number of processes never leaves a
record in more than one value Set per indexed field, and never lets two records occupy
the same `UniqueField` value. Index maintenance becomes an atomic server-side
check-and-swap (Lua) — the in-repo pattern already proven flawless under 8-process
contention (`BAYESIAN_UPDATE_LUA`, `confidence_field.py:51`). Single-process behavior,
API, exception types, and key schema stay identical.

## Freshness Check

**Baseline commit:** 61f36aa5325aa4bb5f992193749d23df719e25c5 (`main`)
**Issue filed at:** 2026-06-11T05:20:32Z
**Disposition:** Minor drift (issue's key-prefix claims for UniqueField were incorrect; corrected below)

**File:line references re-verified:**
- `src/popoto/fields/indexed_field_mixin.py:136-147` — stale SREM from `_saved_field_values` — **still holds**.
- `src/popoto/fields/indexed_field_mixin.py:150-166` — uniqueness check is immediate `SMEMBERS` (line 155) even with a pipeline; SADD deferred — **still holds**.
- `src/popoto/fields/indexed_field_mixin.py:73-74, 115-116` — "best-effort under concurrent writes" docstrings — **still holds**.
- `src/popoto/models/base.py:1276-1348` (external pipeline) and `:1350-1418` (internal pipeline) — both run all `on_save` hooks inside one atomic unit — **still holds**.
- `src/popoto/fields/confidence_field.py:51-99` (`BAYESIAN_UPDATE_LUA`) and `:410-417` (invocation) — Lua precedent — **still holds**.
- `src/popoto/models/base.py:3147` (`clean_indexes`) — manual repair API — **still holds** (remediation, not prevention).

**Key-prefix correction (drift from issue body):** The issue states both `IndexedField`
and `UniqueField` write to `$IndexF:`. **They do not.** `field_class_key` is generated
per concrete class via `field.py:124` `f"${name.strip('Field')}F"`, and `str.strip("Field")`
strips the *character set* `{F,i,e,l,d}` from both ends:

| Field class | `name.strip("Field")` | Prefix |
|---|---|---|
| `IndexedField` | `Index` | `$IndexF` |
| `UniqueField` | `Uniqu` | `$UniquF` |
| `SortedField` | `Sort` | `$SortF` |

So `UniqueField` indexes live under `$UniquF:`, not `$IndexF:`. The audit PoC's
`index_sets_with_2_members` checker queried `$IndexF:UniqDoc:email:...` and silently
reported **0** — a false negative. The spike re-counted against `$UniquF:` and found 68
sets with 2 members, matching `both_saves_succeeded`. **The regression test must derive
each field's prefix from `field_class_key` / `get_special_use_field_db_key`, never
hard-code `$IndexF:`.** The module docstring also still says `$IdxF:` (`indexed_field_mixin.py:18,25`) — wrong; fix in passing.

**Cited sibling issues/PRs re-checked:**
- CONC-2, CONC-3 (audit findings) — explicitly out of scope per issue; different mechanisms.
- PR #417 (ConfidenceField capped-evidence) — merged 2026-06-11; established/extended the audit-verified Lua atomicity pattern this fix reuses.

**Commits on main since issue filed (touching referenced files):** none
(`git log --since=2026-06-11T05:20:32Z` on `indexed_field_mixin.py`, `base.py`,
`confidence_field.py` returned empty).

**Active plans in `docs/plans/` overlapping this area:** `immutable_keys_indexed_fields.md`
mentions IndexedField but addresses key immutability, not the save-path index race — no overlap.

**Notes:** Issue claims remain valid except the UniqueField prefix correction above.

## Prior Art

- **PR #190** — `atomic_increment()` for numeric fields (merged 2026-03-12): established
  the pattern of moving a read-modify-write into an atomic server-side op. Direct
  precedent for the approach, different field.
- **PR #417** — ConfidenceField Bayesian update (merged 2026-06-11): the
  `BAYESIAN_UPDATE_LUA` script + `POPOTO_REDIS_DB.eval(SCRIPT, numkeys, *args)`
  invocation this fix reuses. Audit-verified zero lost updates under 8 processes.
- **Issue #147** (atomic save) — `tests/test_atomic_save.py`: the reason `save()` bundles
  hash-write + index ops into one MULTI/EXEC. The fix MUST preserve this (see Risk 1).
- No closed issue previously attempted this cross-process index fix — greenfield for #412.
- `Model.clean_indexes()` (`base.py:3147`) repairs already-corrupted state; it is not prevention.

## Research

No relevant external findings beyond ecosystem facts validated empirically in the spikes
below (cmsgpack in Lua, redis-py Pipeline.eval, Lua error surfacing). Proceeding with
codebase context. Lua check-and-swap for index maintenance is a standard Redis pattern;
the in-repo `BAYESIAN_UPDATE_LUA` is the authoritative reference.

## Spike Results

### spike-1: How does Lua determine the *authoritative* old value-Set to SREM from?
- **Assumption**: Lua can avoid the stale `_saved_field_values` snapshot by reading the current state server-side.
- **Method**: code-read.
- **Finding**: The value-Set key is `DB_key(prefix, value).redis_key` = `prefix:clean(str(python_value))`. The stored hash field is msgpack-encoded. The robust approach is **Strategy 3**: inside the script, `HGET model_key field_name` → `cmsgpack.unpack` → reconstruct the segment, then `SREM` from that exact Set and `SADD` the new Set — all atomic. **Critical hazard**: Lua `tostring(1.0)` == `"1"` but Python `str(1.0)` == `"1.0"`; `None` vs `nil`; `True/False` vs `true/false`. Naive segment reconstruction in Lua will target the wrong Set for float/None/bool. **Resolution adopted** (see Technical Approach): do NOT reconstruct the segment in Lua. Instead Python passes the **new** Set key plus the **member key**, and the script removes the member from any stale Set via a different mechanism — see "old-set discovery" decision below. No new reverse-lookup structure exists in the repo today; one is the safest way to make old-set discovery type-safe.
- **Confidence**: high (on the hazard and on cmsgpack availability); the chosen old-set-discovery mechanism is decided in Technical Approach.
- **Impact on plan**: Drove the decision to discover the old Set via a **per-member reverse pointer in the model hash itself** (the already-stored, already-msgpack value) but compute the SREM target key **in Python and pass it pre-cleaned to Lua**, with the script re-reading the hash to detect concurrent moves — see Technical Approach. Eliminates Lua-side `clean()`/`str()` parity risk entirely.

### spike-2: Reproduce the baseline corruption on current main.
- **Assumption**: The bug still reproduces at the rates the issue claims.
- **Method**: prototype (issue's PoC, isolated /tmp, DB 12).
- **Finding**: Reproduces. 58–59/200 dual-membership rounds; 67–68/100 UniqueField double-saves. PoC's UniqueField counter used wrong prefix (`$IndexF:` vs real `$UniquF:`) and under-reported — corrected count = 68. redis-py 7.1.1, msgpack 1.1.2, spawn on macOS, no env issues.
- **Confidence**: high.
- **Impact on plan**: Baseline to beat = 0 for both. Acceptance test must use `$UniquF:` for UniqueField.

### spike-3: Lua/cmsgpack/redis-py facts (empirical, live server).
- **Assumption**: cmsgpack works on the running server; popoto hashes decode in Lua; Pipeline.eval exists; Lua errors surface cleanly.
- **Method**: prototype (live EVAL on DB 13).
- **Finding**: Server is **Redis 8.6.2** (standalone). cmsgpack PASS (str/int/float/None round-trip; `None`→`0xc0`→Lua `nil`, byte-identical to python `msgpack`). Popoto hash values decode via cmsgpack PASS. `Pipeline.eval`/`.evalsha` exist PASS. `redis.error_reply('msg')` → `redis.exceptions.ResponseError('msg')` PASS.
- **Confidence**: high.
- **Impact on plan**: Confirms the Lua-via-cmsgpack approach is viable; conflict signalling via `redis.error_reply` mapped to `ModelException` is sound. **Note**: must still smoke-test on Valkey (the running dev server is Redis; project rule requires both — see Risks).

### spike-4: How does EVAL compose with both save() pipeline paths?
- **Assumption**: A single-command EVAL can integrate with both the internal and external MULTI/EXEC paths without breaking #147 atomicity.
- **Method**: code-read + redis-py API check.
- **Finding**: `Pipeline.eval()` is supported and runs inside the surrounding MULTI/EXEC, so queuing the swap EVAL alongside the hash HSET preserves #147 atomicity. **Hazard**: a Lua error inside MULTI/EXEC does NOT roll back earlier queued commands — so a uniqueness `error_reply` would still leave the hash written. Conflict detection must therefore happen **before** the hash is committed, or the check must be ordered so the EVAL runs first / the script is self-contained. For the **external pipeline** path the conflict can only surface at the caller's `execute()`, deferring the `ModelException` — a documented semantics change to weigh.
- **Confidence**: high on the API facts; the ordering resolution is specified in Technical Approach + Open Questions.
- **Impact on plan**: Drove the two-path treatment and the "EVAL-first, then HSET" ordering decision, plus Open Question on external-pipeline error timing.

## Data Flow

1. **Entry point**: `Model.save()` (`base.py`), internal or external pipeline path.
2. **Per-field hooks**: for each field, `field.on_save(...)` is invoked
   (`base.py:1310-1319` external, `:1389-1398` internal).
3. **IndexedFieldMixin.on_save** (today): SREM old Set (from stale snapshot) → optional
   non-transactional SMEMBERS uniqueness check → SADD new Set; commands queued into the
   active pipeline.
4. **EXEC**: internal path executes immediately; external path executes when the caller chooses.
5. **Storage**: value Sets at `$IndexF:Model:field:value` / `$UniquF:Model:field:value`;
   model hash at `Model:key` (msgpack values).
6. **Read path**: `filter()` (`indexed_field_mixin.py:241-333`) reads Sets directly — no
   re-verification against the hash, so a wrong Set entry = a wrong result.

The fix replaces step 3 with a single atomic EVAL (the check-and-swap), keeping it inside
the same atomic unit as the hash HSET (step 4) so #147 holds.

## Why Previous Fixes Failed

No prior fix attempted this specific cross-process index race. The closest precedent
(PR #190 atomic_increment, PR #417 ConfidenceField Lua) succeeded by doing exactly what
this plan does — moving a client-side RMW into an atomic server-side op. The lesson
applied here: the existing per-field MULTI/EXEC is *intra-process* atomic but the
client-computed inputs (the snapshot SREM target, the pre-check uniqueness read) are
stale across processes. Atomicity must move server-side, where Redis serializes the EVAL.

## Architectural Impact

- **New dependencies**: none. Uses existing `POPOTO_REDIS_DB.eval` / `cmsgpack` (already in use).
- **Interface changes**: none public. `save()` signature, `ModelException` type, and the
  `$IndexF:`/`$UniquF:` key schema are unchanged. Internal `on_save` implementation changes.
- **Coupling**: `on_save` gains a dependency on the model hash being written in the same
  transaction (ordering constraint), tightening the existing #147 coupling — acceptable and intended.
- **Data ownership**: unchanged. (If a reverse-pointer companion is adopted for old-set
  discovery, it is an additive internal structure owned by the mixin — see Open Question 1.)
- **Reversibility**: high. The change is localized to `indexed_field_mixin.on_save` + a
  registered Lua script; revertible by restoring the old method. No data migration required
  (existing Sets remain valid; `clean_indexes()` repairs any pre-existing corruption).

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1-2 (one Open Question on external-pipeline error timing + old-set-discovery mechanism)
- Review rounds: 1-2 (concurrency-sensitive change to the core save path)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `.venv/bin/python -c "import redis,os; redis.Redis.from_url(os.environ.get('REDIS_URL','redis://localhost:6379/15')).ping()"` | Tests + multiprocess reproduction need a live server |
| cmsgpack in Lua runtime | `.venv/bin/python -c "from popoto.redis_db import POPOTO_REDIS_DB as r; print(r.eval('return cmsgpack.unpack(cmsgpack.pack(1))',0))"` | Lua script depends on cmsgpack |

## Solution

### Key Elements

- **`INDEX_SWAP_LUA`**: a registered Lua script performing an atomic check-and-swap for
  one indexed field per save: remove the member from the Set it is *actually* in, add it
  to the new value Set, and (for `unique=True`) abort if the new Set is occupied by a
  different member. One atomic server-side unit, O(1) per field.
- **`IndexedFieldMixin.on_save` rewrite**: replace the stale-snapshot SREM + pre-check
  SMEMBERS + SADD with a single `EVAL` (queued into the active pipeline so it runs inside
  the surrounding MULTI/EXEC).
- **Conflict surfacing**: `unique` violation → Lua `redis.error_reply(...)` →
  `redis.exceptions.ResponseError` → caught and re-raised as the same `ModelException`
  message users see today.
- **Multiprocess regression test**: `tests/test_concurrent_index_integrity.py`
  reproducing both scenarios (barrier-synchronized, spawn), asserting 0 dual-membership
  and 0 unique double-occupancy, prefix-derived from `field_class_key`.

### Flow

`save()` → for each indexed field, queue `INDEX_SWAP_LUA` (member key, new value Set key,
authoritative-old-value read) into the active pipeline → pipeline EXEC runs the swap
atomically alongside the hash HSET → on unique conflict the EVAL returns an error →
`save()` maps it to `ModelException`; otherwise the member is in exactly one value Set.

### Technical Approach

**Old-set discovery (the core decision).** To avoid the stale snapshot AND avoid the
Lua `str()`/`clean()` type-parity hazard from spike-1, the script reads the
**authoritative current value from the model hash** (`HGET model_key field_name` →
`cmsgpack.unpack`) and uses it only to decide *whether* a move is needed. The actual SREM
target Set key is computed **in Python** (where `DB_key.clean(str(value))` is correct for
all types) and passed in pre-cleaned, for both the previously-stored value (read fresh, not
from the stale snapshot) and the new value. Concretely the script receives:

- `KEYS[1]` = model hash key
- `KEYS[2]` = new value Set key (Python-computed, pre-cleaned)
- `ARGV[1]` = field name (hash field)
- `ARGV[2]` = member key (the record's redis_key)
- `ARGV[3]` = new value, msgpack-packed by Python (so Lua compares the authoritative hash
  bytes against the intended new value to detect "already moved by a concurrent writer")
- `ARGV[4]` = `unique` flag ("1"/"0")
- `ARGV[5]` = old value Set key candidate (Python-computed from a *fresh* re-read, used as
  the SREM target)

Script logic: read the current hash bytes; if they already equal `ARGV[3]` and the member
is already in `KEYS[2]`, no-op (idempotent re-save). Otherwise: for unique, check `KEYS[2]`
occupancy excluding self → `error_reply` if taken; `SREM ARGV[5] member`; `SADD KEYS[2] member`;
then write the hash field so the hash and index move together. **The hash HSET for the
indexed field happens inside the same EVAL**, making the authoritative value and its Set
membership update a single atomic step — this is what closes both the dual-membership and
the check-then-act windows.

> Open Question 1 records the alternative (a per-member reverse-pointer companion hash) if
> re-reading the old Set key in Python proves insufficient for a third-writer interleaving.
> The spike's "third writer" analysis showed the EVAL-reads-hash approach converges
> correctly because each EVAL removes from whatever Set the member is currently in (via the
> hash-derived current value), not a cached view; the Python-passed `ARGV[5]` is a fast-path
> hint the script validates against the live hash before trusting.

**Pipeline integration (spike-4).**
- *Internal path* (`base.py:1350-1418`): queue the EVAL into `internal_pipeline` so it
  executes inside the existing MULTI/EXEC — preserves #147. Wrap `internal_pipeline.execute()`
  to catch `ResponseError` from a unique conflict and raise `ModelException`. Order the
  per-field EVAL so a conflict aborts before the top-level hash HSET is relied upon
  (the EVAL writes the field's hash value itself; the outer HSET of the full mapping must
  be reconciled — see Open Question 2).
- *External path* (`base.py:1276-1348`): queue the EVAL into the caller's pipeline. The
  unique conflict can only surface at the caller's `execute()`. Either (a) keep the eager
  pre-check for the external path only (documented best-effort, unchanged from today), or
  (b) document that conflicts now raise at `execute()`. **This is Open Question 3.**

**No key-schema change.** Sets remain `$IndexF:`/`$UniquF:...`. The `$IdxF:` docstring is
corrected in passing.

## Failure Path Test Strategy

### Exception Handling Coverage
- The uniqueness path raises `ModelException` (not swallowed). Test asserts the exception
  is raised with the same message format (`indexed_field_mixin.py:163-166`) under both
  single-process and concurrent conditions.
- The EVAL's `ResponseError`→`ModelException` mapping must be tested: a malformed/non-unique
  ResponseError must NOT be silently turned into a uniqueness ModelException — assert the
  mapping only triggers on the unique-conflict sentinel, and other ResponseErrors propagate.
- No `except Exception: pass` blocks are introduced.

### Empty/Invalid Input Handling
- `field_value=None`: must map to the `None` value Set (Python `str(None)`→`"None"`,
  cleaned), and msgpack `None`→Lua `nil` (spike-3). Add a test saving then changing a
  nullable IndexedField through `None` and asserting correct Set membership.
- Re-saving the same instance with the same value must be a no-op (idempotent), not a
  self-collision for unique fields (existing self-exclusion logic at `:156-161`).

### Error State Rendering
- The `ModelException` message must remain user-visible and identical in wording for
  single-process unique violations (regression assertion).

## Test Impact

- `tests/test_atomic_save.py` — UPDATE/EXTEND: keep all existing assertions green (proves
  #147 still holds); the index-set-exists-immediately tests must still pass with the EVAL path.
- `tests/test_queries.py`, `tests/test_indexed_fields.py` (if present) — VERIFY unchanged:
  single-process filter/uniqueness behavior must be identical. No edits expected; run to confirm.
- `tests/test_stress.py::test_concurrent_creates_from_threads` — VERIFY: should still pass
  and ideally show improved index consistency.
- New: `tests/test_concurrent_index_integrity.py` — multiprocess reproduction (marked so CI
  can skip if it cannot run spawn-multiprocess against the server).

No existing test asserts the *buggy* behavior, so nothing needs DELETE/REPLACE.

## Rabbit Holes

- **Reconstructing `DB_key.clean(str(value))` inside Lua.** Type-parity (float `1.0`,
  `None`, bool) makes this brittle (spike-1). Avoid — compute Set keys in Python, pass in.
- **A general reverse-index/secondary-structure overhaul.** Out of scope; only add a
  companion structure if Open Question 1 forces it, and keep it additive/internal.
- **Fixing CONC-2 / CONC-3** (ObservationProtocol, CyclicDecayField RMW). Different
  mechanisms; separate issues (see No-Gos).
- **`filter()` re-verification against the hash.** Tempting defense-in-depth, but it
  changes read semantics/perf and is not required once writes are atomic. Out of scope.
- **Converting `clean_indexes()` into an online repair.** It is remediation, not prevention.
- **EVALSHA caching / script-load optimization.** `eval()` (used by ConfidenceField) is
  fine at this scale; don't gold-plate.

## Risks

### Risk 1: Breaking #147 (hash/index atomicity)
**Impact:** If the index EVAL is pulled out of the save MULTI/EXEC, a window reopens where
the record exists but its index entry doesn't (the exact bug #147 fixed; `test_atomic_save.py`).
**Mitigation:** Queue the EVAL inside the existing internal pipeline so it runs in the same
MULTI/EXEC. Keep `test_atomic_save.py` green as a gate. Verify the field's hash write and Set
membership move within one atomic unit.

### Risk 2: Lua error inside MULTI/EXEC does not roll back (spike-4)
**Impact:** A unique-conflict `error_reply` after other commands queued in the same EXEC
leaves partial writes (e.g., the top-level hash HSET already applied).
**Mitigation:** Order so the conflict-detecting EVAL runs first / make the EVAL own the
field's hash write so a conflict aborts before any visible state changes for that field;
on caught `ResponseError`, the save raises and the (now-uncommitted-for-that-field) state
stays consistent. Add a test asserting that after a rejected unique save, neither the hash
field nor the Set reflects the rejected value. Resolve via Open Question 2.

### Risk 3: Valkey parity unverified
**Impact:** Dev server is Redis 8.6.2 (spike-3); project rule requires Redis AND Valkey,
no Redis modules.
**Mitigation:** The script uses only plain `EVAL` + `cmsgpack` (both supported on Valkey).
Run the new regression test against a Valkey instance before merge (acceptance criterion).

### Risk 4: Performance regression at ~20k records/model
**Impact:** Extra EVAL round-trip per indexed field per save.
**Mitigation:** Swap is O(1) per field (HGET + SREM + SADD), comparable to today's
SREM+SADD plus the eliminated SMEMBERS round-trip; for unique fields it may even reduce
round-trips. Include rough before/after `save()` timing at 20k in the PR.

## Race Conditions

### Race 1: Dual membership from stale SREM target
**Location:** `src/popoto/fields/indexed_field_mixin.py:136-147`
**Trigger:** Two processes hydrate the same record at value `X`; one saves `A`, the other
`B`. Each SREMs the snapshot Set (`X`) and SADDs its own; the loser's SADD survives → record
in both `A` and `B`.
**Data prerequisite:** The authoritative current value must be read at swap time, not from
the hydration snapshot.
**State prerequisite:** The read-of-current and the SREM/SADD must be one atomic unit.
**Mitigation:** EVAL reads the live hash value and moves the member from the Set it is
actually in, atomically (Technical Approach).

### Race 2: Check-then-act uniqueness
**Location:** `src/popoto/fields/indexed_field_mixin.py:150-166`
**Trigger:** Two processes save different records with the same unique value; both SMEMBERS
see an empty Set; both SADD.
**Data prerequisite:** Occupancy check and SADD must be indivisible.
**State prerequisite:** Server-side serialization of check+set.
**Mitigation:** EVAL performs occupancy-check-then-SADD atomically; second writer gets
`error_reply` → `ModelException`.

### Race 3: Third-writer interleaving (convergence)
**Location:** new `INDEX_SWAP_LUA`
**Trigger:** Three+ processes saving the same record to different values in rapid succession.
**Data prerequisite:** Each EVAL must remove from the Set implied by the *current* hash, not
a passed-in hint, when the hint is stale.
**State prerequisite:** Script validates the Python-passed old-set hint against the live hash
before trusting it; falls back to hash-derived removal.
**Mitigation:** Script reads the hash inside the EVAL; the final committed state always
matches the last writer's hash value with exactly one Set membership. Covered by the
multiprocess regression test running ≥200 rounds.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #412] CONC-2 (ObservationProtocol torn outcome application) — different
  mechanism (immediate-EVAL vs deferred-pipeline), should be its own issue. *(Note: no
  separate issue filed yet; if the maintainer wants it tracked, file before relying on this
  tag — see Open Question 4.)*
- [SEPARATE-SLUG #412] CONC-3 (CyclicDecayField companion-hash RMW) — same RMW family,
  different keys/fix surface (per-field Lua like `BAYESIAN_UPDATE_LUA`). Separate issue.
- [DESTRUCTIVE] Bulk repair of already-corrupted production indexes — `clean_indexes()`
  exists for this and should be run deliberately by an operator, not bundled into the fix.
- `filter()` client-side re-verification — deliberately not done; unnecessary once writes
  are atomic and would change read semantics/perf.

## Update System

No update-system changes required — this is an internal ORM behavior fix; no new deps,
config files, or migration steps. Existing index Sets remain valid.

## Agent Integration

No agent integration required — this is a library-internal save-path change with no MCP
surface.

## Documentation

### Feature Documentation
- [ ] Update `docs/indexed_fields.md` and `docs/fields.md`: state that index maintenance
  and `UniqueField` enforcement are now atomic under concurrent cross-process writes
  (remove/soften any "best-effort" implication).

### External Documentation Site
- [ ] Verify mkdocs build passes after edits.

### Inline Documentation
- [ ] Correct `indexed_field_mixin.py:18,25` docstring (`$IdxF:` → `$IndexF:`).
- [ ] Update the `on_save` docstrings (`:73-74`, `:115-116`) to remove the "best-effort
  under concurrent writes" / "race condition" caveats once the EVAL path lands (for the
  internal-pipeline path at minimum; keep an accurate caveat for the external path if
  Open Question 3 leaves it best-effort).
- [ ] Docstring for the new `INDEX_SWAP_LUA` explaining KEYS/ARGV contract and the
  type-parity rationale for computing Set keys in Python.

## Success Criteria

- [ ] 2-process barrier-synchronized same-record reproduction: **0 dual-membership over
  ≥200 rounds** (baseline 58–59/200).
- [ ] 2-process UniqueField race: **0 rounds where both saves succeed** and **0 value Sets
  with >1 member over ≥100 rounds** (baseline 67–68/100); exactly one save per round raises
  `ModelException`. Test asserts against the correct `$UniquF:` prefix (derived from
  `field_class_key`, not hard-coded).
- [ ] After any concurrent round, `filter(field=v)` returns only records whose stored hash
  value is actually `v`.
- [ ] New multiprocess regression test added (marked/skippable if CI cannot run spawn-mp).
- [ ] Existing suite passes unchanged — no API, exception-type, or key-schema differences
  for single-process users (`test_atomic_save.py` green = #147 preserved).
- [ ] Verified green against both Redis and Valkey (no Redis modules used).
- [ ] No measurable `save()` regression at ~20k records/model (rough before/after timing in PR).
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (lua-swap)**
  - Name: lua-swap-builder
  - Role: Implement `INDEX_SWAP_LUA` + rewrite `IndexedFieldMixin.on_save` for both pipeline paths; map conflicts to `ModelException`; fix docstrings.
  - Agent Type: async-specialist
  - Resume: true

- **Builder (regression-test)**
  - Name: concurrency-test-builder
  - Role: Author `tests/test_concurrent_index_integrity.py` (spawn multiprocess, barrier-synced, prefix-derived assertions) + extend `test_atomic_save.py` coverage.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (concurrency)**
  - Name: concurrency-validator
  - Role: Run the reproduction at ≥200/≥100 rounds, confirm 0/0; run full suite on Redis and Valkey; confirm #147 tests green; capture timing.
  - Agent Type: validator
  - Resume: true

- **Documentarian**
  - Name: index-docs
  - Role: Update `docs/indexed_fields.md`, `docs/fields.md`, docstrings.
  - Agent Type: documentarian
  - Resume: true

## Step by Step Tasks

### 1. Implement the Lua check-and-swap + on_save rewrite
- **Task ID**: build-lua-swap
- **Depends On**: none
- **Validates**: tests/test_atomic_save.py, tests/test_indexed_fields.py (if present), tests/test_queries.py
- **Informed By**: spike-1 (compute Set keys in Python, not Lua), spike-3 (cmsgpack/error_reply facts), spike-4 (queue EVAL inside MULTI/EXEC; Lua errors don't roll back)
- **Assigned To**: lua-swap-builder
- **Agent Type**: async-specialist
- **Parallel**: true
- Add `INDEX_SWAP_LUA` (KEYS/ARGV contract per Technical Approach) near the mixin.
- Rewrite `IndexedFieldMixin.on_save` to queue the EVAL for both internal and external paths; remove stale-snapshot SREM and pre-check SMEMBERS from the internal path.
- Map unique-conflict `ResponseError` to `ModelException` with the existing message wording.
- Fix `$IdxF:`→`$IndexF:` docstring; update best-effort caveats per resolution of Open Question 3.
- Ensure the field's hash write and Set move occur in one atomic unit (Risk 1/2).

### 2. Author multiprocess regression + extend atomic-save tests
- **Task ID**: build-tests
- **Depends On**: none
- **Validates**: tests/test_concurrent_index_integrity.py (create)
- **Informed By**: spike-2 (baseline numbers; PoC prefix bug), Freshness Check (use `field_class_key` to derive `$IndexF:`/`$UniquF:`)
- **Assigned To**: concurrency-test-builder
- **Agent Type**: test-engineer
- **Parallel**: true
- Port the issue PoC into a marked multiprocess test (spawn, Redis barrier), correcting the UniqueField prefix to `$UniquF:` via `field_class_key`.
- Assert 0 dual-membership over ≥200 rounds and 0 unique double-occupancy over ≥100 rounds.
- Add single-process tests: nullable field through `None`, idempotent re-save, rejected-unique leaves neither hash nor Set changed (Risk 2).

### 3. Validate concurrency + cross-server + #147
- **Task ID**: validate-concurrency
- **Depends On**: build-lua-swap, build-tests
- **Assigned To**: concurrency-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the reproduction; confirm 0/0 at full round counts.
- Run full suite against Redis AND Valkey; confirm `test_atomic_save.py` green.
- Capture rough save() timing at ~20k records; record in report.

### 4. Documentation
- **Task ID**: document-feature
- **Depends On**: validate-concurrency
- **Assigned To**: index-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/indexed_fields.md`, `docs/fields.md`; finalize docstrings; verify mkdocs build.

### 5. Final Validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: concurrency-validator
- **Agent Type**: validator
- **Parallel**: false
- Re-run full suite + reproduction; confirm all Success Criteria; generate final report.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Full suite | `.venv/bin/pytest -q` | exit code 0 |
| Atomic-save (#147) preserved | `.venv/bin/pytest tests/test_atomic_save.py -q` | exit code 0 |
| Concurrency regression | `.venv/bin/pytest tests/test_concurrent_index_integrity.py -q` | exit code 0 |
| cmsgpack available | `.venv/bin/python -c "from popoto.redis_db import POPOTO_REDIS_DB as r; print(r.eval('return cmsgpack.unpack(cmsgpack.pack(7))',0))"` | output contains 7 |
| No $IdxF leftover in code/docstrings | `grep -rn 'IdxF' src/popoto/fields/indexed_field_mixin.py` | exit code 1 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Old-set discovery mechanism.** The plan computes Set keys in Python and has the EVAL
   read the live hash to validate/derive the SREM target (no Lua `clean()`). Is the
   "EVAL re-reads the hash field, owns the field's hash write, and removes from the
   hash-derived current Set" design acceptable, or do you prefer an additive per-member
   reverse-pointer companion hash (`member → current_value_segment`) for O(1) deterministic
   old-set removal? The former adds no structure; the latter is more explicit but adds an
   internal companion key.

2. **Hash-write ownership.** `save()` currently HSETs the full field mapping at the top of
   the transaction (`base.py:1273-1277/1354`). If the EVAL also writes the indexed field's
   hash value (to keep value+Set atomic and to make a unique conflict abort before any
   visible write), we have two writers of the same hash field in one transaction. Preferred:
   (a) let the top-level HSET write the value and the EVAL only move Sets + check
   uniqueness (simpler, but a unique `error_reply` then leaves the HSET applied — needs the
   EVAL ordered first and a guard), or (b) exclude indexed fields from the top-level mapping
   and let each field's EVAL own its hash write? This affects Risk 2's mitigation shape.

3. **External-pipeline uniqueness timing.** For `save(pipeline=...)`, a unique conflict can
   only surface at the caller's `execute()` (spike-4). Acceptable options: (a) keep the
   eager pre-check for the external path only (preserves immediate `ModelException`, remains
   documented best-effort across processes), or (b) document that conflicts now raise at
   `execute()` time. Which matches the documented batching contract you want to keep?

4. **CONC-2 / CONC-3 tracking.** The No-Gos tag them `[SEPARATE-SLUG #412]` but no separate
   issues exist yet. Should I file #-issues for CONC-2 and CONC-3 now (so the tags resolve),
   or leave them noted in this plan only?
