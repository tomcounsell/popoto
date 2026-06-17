---
status: Ready
type: bug
appetite: Medium
owner: valorengels
created: 2026-06-17
tracking: https://github.com/tomcounsell/popoto/issues/412
last_comment_id:
revision_applied: true
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
check-and-swap (Lua) — reusing the in-repo Lua-via-cmsgpack pattern established by
`CAPPED_BAYESIAN_UPDATE_LUA` (`confidence_field.py:62`, rewritten in PR #417;
invoked standalone via `POPOTO_REDIS_DB.eval(...)` at `:467`). Single-process behavior,
API, exception types, and key schema stay identical. (Note: ConfidenceField runs its
EVAL standalone, not inside a MULTI/EXEC — this plan instead queues the EVAL inside the
save pipeline, an integration that spike-5 verified independently; see Spike Results.)

## Freshness Check

**Baseline commit:** 61f36aa5325aa4bb5f992193749d23df719e25c5 (`main`)
**Issue filed at:** 2026-06-11T05:20:32Z
**Disposition:** Minor drift (issue's key-prefix claims for UniqueField were incorrect; corrected below)

**File:line references re-verified:**
- `src/popoto/fields/indexed_field_mixin.py:136-147` — stale SREM from `_saved_field_values` — **still holds**.
- `src/popoto/fields/indexed_field_mixin.py:150-166` — uniqueness check is immediate `SMEMBERS` (line 155) even with a pipeline; SADD deferred — **still holds**.
- `src/popoto/fields/indexed_field_mixin.py:73-74, 115-116` — "best-effort under concurrent writes" docstrings — **still holds**.
- `src/popoto/models/base.py:1276-1348` (external pipeline) and `:1350-1418` (internal pipeline) — both run all `on_save` hooks inside one atomic unit — **still holds**.
- `src/popoto/fields/confidence_field.py:62-115` (`CAPPED_BAYESIAN_UPDATE_LUA`) and `:467-475` (invocation) — Lua precedent — **still holds**. (Corrected from the prior draft, which cited a stale `BAYESIAN_UPDATE_LUA`/`:51` symbol with a since-retracted "4000/4000 audit-verified" claim; the live symbol is `CAPPED_BAYESIAN_UPDATE_LUA`, rewritten in PR #417, and it is invoked **standalone** — `POPOTO_REDIS_DB.eval(...)` — not inside a MULTI/EXEC. The pipeline-queued-EVAL integration this plan relies on is therefore NOT pre-existing precedent and was proven independently in spike-5.)
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
hard-code `$IndexF:`.** The module/method docstrings also still say `$IdxF:`
(`indexed_field_mixin.py:18, 25, 28, 110` — re-grepped 2026-06-17, four occurrences,
not two as the prior draft stated) — wrong; fix in passing.

**Cited sibling issues/PRs re-checked:**
- CONC-2, CONC-3 (audit findings) — explicitly out of scope per issue; different mechanisms.
- PR #417 (ConfidenceField capped-evidence) — merged 2026-06-11; rewrote the Lua script to
  `CAPPED_BAYESIAN_UPDATE_LUA`. It establishes the Lua-via-cmsgpack *coding* pattern this fix
  reuses, but it runs its EVAL standalone (no pipeline), so it is not precedent for the
  pipeline-queued-EVAL atomicity this plan needs (proven separately in spike-5).

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
- **PR #417** — ConfidenceField capped-evidence update (merged 2026-06-11): the
  `CAPPED_BAYESIAN_UPDATE_LUA` script (`confidence_field.py:62`) + standalone
  `POPOTO_REDIS_DB.eval(SCRIPT, numkeys, *args)` invocation (`:467`) whose Lua/cmsgpack
  coding pattern this fix reuses. (The earlier "4000/4000 audit-verified" / 8-process
  figures attached to a prior `BAYESIAN_UPDATE_LUA` symbol were retracted by the
  maintainer and are not relied on here; ConfidenceField also runs its EVAL standalone,
  so it is not precedent for in-pipeline EVAL atomicity — that was proven in spike-5.)
- **Issue #147** (atomic save) — `tests/test_atomic_save.py`: the reason `save()` bundles
  hash-write + index ops into one MULTI/EXEC. The fix MUST preserve this (see Risk 1).
- No closed issue previously attempted this cross-process index fix — greenfield for #412.
- `Model.clean_indexes()` (`base.py:3147`) repairs already-corrupted state; it is not prevention.

## Research

No relevant external findings beyond ecosystem facts validated empirically in the spikes
below (cmsgpack in Lua, redis-py Pipeline.eval, Lua error surfacing). Proceeding with
codebase context. Lua check-and-swap for index maintenance is a standard Redis pattern;
the in-repo `CAPPED_BAYESIAN_UPDATE_LUA` (`confidence_field.py:62`) is the authoritative
reference for the Lua/cmsgpack coding style (though it runs standalone, not in a pipeline).

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
- **Impact on plan**: Confirms the Lua-via-cmsgpack approach is viable; conflict signalling via `redis.error_reply` mapped to `ModelException` is sound. **Valkey now smoke-tested in spike-5** (Valkey 9.1.0 on port 6400): identical EVAL-in-pipeline wire order; full regression run on Valkey still required at validation time (acceptance criterion / Risk 3).

### spike-4: How does EVAL compose with both save() pipeline paths?
- **Assumption**: A single-command EVAL can integrate with both the internal and external MULTI/EXEC paths without breaking #147 atomicity.
- **Method**: code-read + redis-py API check.
- **Finding**: `Pipeline.eval()` is supported and queues into the surrounding MULTI/EXEC. **Hazard**: a Lua error inside MULTI/EXEC does NOT roll back earlier queued commands — so a uniqueness `error_reply` would still leave the hash written. Conflict detection must therefore happen **before** the hash is committed, or the check must be ordered so the EVAL runs first / the script is self-contained. For the **external pipeline** path the conflict can only surface at the caller's `execute()`, deferring the `ModelException` — a documented semantics change (resolved in OQ3 below). **The bare API-support claim was not sufficient to assert atomicity — spike-5 was added to prove the actual wire order.**
- **Confidence**: high on the API facts; the ordering resolution is specified in Technical Approach.
- **Impact on plan**: Drove the two-path treatment and the "EVAL-first, then HSET" ordering decision, plus the external-pipeline error-timing decision (OQ3).

### spike-5: Prove the MULTI -> EVAL -> EXEC wire order on BOTH Redis and Valkey (Blocker 1).
- **Assumption**: When `Pipeline.eval()` is queued into a transactional `POPOTO_REDIS_DB.pipeline()` (the exact object save() builds), the EVAL is actually wrapped inside the MULTI/EXEC on the wire — not silently promoted out of the transaction — so its index ops execute atomically with the hash HSET. The prior plan rested on this purely from API docs; the only in-repo precedent (`confidence_field.py` `update_confidence()`) deliberately runs eval **standalone**, so it does not corroborate the in-pipeline claim.
- **Method**: prototype (live `MONITOR` capture on DB 13), run identically against **Redis 8.6.2** (localhost:6379) and **Valkey 9.1.0** (localhost:6400, `valkey_version:9.1.0`, `server_name:valkey`). Built `r.pipeline()` with **default args** (mirroring `base.py:1352` / `:1276`), queued `HSET` → `EVAL` (whose body does `SADD`) → `SADD`, then `execute()`, while a second connection ran `MONITOR`.
- **Finding**: **CONFIRMED on both servers — byte-identical wire order:**
  ```
  MULTI
  HSET Model:k1 f \x01
  EVAL redis.call('SADD', KEYS[1], ARGV[1]); return 1  1  $IndexF:Model:f:v  Model:k1
  SADD $IndexF:Model:f:v Model:k1          <- the EVAL's internal SADD, executed at EXEC time
  SADD ClassSet Model:k1
  EXEC
  ```
  The EVAL sits between `MULTI` and `EXEC`; its internal `SADD` runs at EXEC time inside the transaction. `execute()` returned `[1, 1, 1]`. The pipeline object reported `pipeline.transaction == True` (it is NOT built with `transaction=False`) — and code inspection confirms `base.py:1352` builds `POPOTO_REDIS_DB.pipeline()` with default args (transactional) and `base.py:1276` reuses the caller's pipeline. So queuing `INDEX_SWAP_LUA` into the save pipeline genuinely runs the swap inside the same MULTI/EXEC as the hash write.
- **Confidence**: high (direct MONITOR evidence on both engines; matches the exact pipeline construction the save path uses).
- **Impact on plan**: Closes the unproven-atomicity blocker. #147 atomicity (Risk 1) is preserved by the EVAL being inside MULTI/EXEC on both Redis and Valkey. Note the Lua-error-no-rollback hazard (spike-4 / Risk 2) is orthogonal and is handled by EVAL-first ordering + hash-write ownership (OQ2, now resolved).

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
- **Data ownership**: unchanged. (A reverse-pointer companion for old-set discovery was
  considered and rejected — the live model hash, read inside the EVAL, is the single source
  of truth; see Technical Approach → Old-set discovery.)
- **Reversibility**: high. The change is localized to `indexed_field_mixin.on_save` + a
  registered Lua script; revertible by restoring the old method. No data migration required
  (existing Sets remain valid; `clean_indexes()` repairs any pre-existing corruption).

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0-1 (all prior Open Questions resolved in this revision; remaining contact is optional confirmation)
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
  the SREM target) **OR the sentinel `"\x00__NONE__"`** when there is no prior set to remove
  from (first save, or the previous value was `None`). See "None→value transition" below.

Script logic: read the current hash bytes; if they already equal `ARGV[3]` and the member
is already in `KEYS[2]`, no-op (idempotent re-save). Otherwise: for unique, check `KEYS[2]`
occupancy excluding self → `error_reply` if taken; **if `ARGV[5] ~= sentinel`** then
`SREM ARGV[5] member`; `SADD KEYS[2] member`; then write the hash field so the hash and
index move together. **The hash HSET for the indexed field happens inside the same EVAL**,
making the authoritative value and its Set membership update a single atomic step — this is
what closes both the dual-membership and the check-then-act windows.

**None→value transition (Concern 5 — resolved).** Today the SREM is guarded by
`if old_value is not None and old_value != field_value` (`indexed_field_mixin.py:139`).
The Lua script MUST mirror this guard, or an unconditional `SREM ARGV[5] member` would touch
an unintended key on every *first* save (where there is no old set). **Resolution:** Python
passes the sentinel `"\x00__NONE__"` as `ARGV[5]` whenever `old_value is None` (or the field
is being saved for the first time / has no `_saved_field_values` entry); the script does
`if ARGV[5] ~= '\x00__NONE__' then redis.call('SREM', ARGV[5], member) end`. The sentinel is
a NUL-prefixed string that cannot collide with any real `DB_key.clean(...)` Set key (clean
never emits a leading NUL). This is the direct Lua analogue of the existing
`old_value is not None` Python check.

**Hash-write ownership (OQ2 — resolved; was Blocker 2).** Today `save()` writes the FULL
field mapping via a single top-level `HSET ... mapping=hset_mapping` at step 1
(`base.py:1273-1277` external, `:1354` internal), and `on_save` runs later at step 5
(`:1310-1319` / `:1389-1398`). If the EVAL *also* wrote the indexed field's hash value, two
writers would target the same hash field in one transaction; worse, a unique-conflict
`error_reply` at step 5 does NOT roll back the step-1 HSET (spike-4 / Risk 2), so the
rejected value would remain persisted in the hash — a brand-new partial-write corruption
mode. **Decision: option (b) — exclude indexed AND unique fields from the top-level
`hset_mapping`, and let each such field's `INDEX_SWAP_LUA` own its own hash write inside the
EVAL.** Concretely:
  - `encode_popoto_model_obj(self)` (or the call site at `base.py:1273`/`:1354`) is filtered
    to drop entries for fields whose class is an `IndexedFieldMixin` subclass (Indexed +
    Unique). Non-indexed fields continue to be written by the top-level HSET unchanged.
  - For each excluded field, `INDEX_SWAP_LUA` performs `HSET model_key field_name new_bytes`
    as the LAST step of the swap, AFTER the unique occupancy check. Because the EVAL is a
    single atomic unit, a unique conflict aborts via `error_reply` **before** the hash field
    is written — so the hash never reflects a rejected value (closes Risk 2 at the field
    level), and the value+Set always move together (closes dual-membership).
  - `self._db_content` (`base.py:1274`) must still reflect the complete intended mapping for
    backward-compat/read-back; it is built from the full encode and is unaffected by what the
    top-level HSET omits, since the EVAL writes the omitted fields within the same EXEC.
  - Rejected alternative (a) — keep the top-level HSET writing everything and only move Sets
    in the EVAL — was rejected precisely because it cannot prevent the step-1 HSET from
    persisting a value the step-5 unique check then rejects, even with EVAL-first ordering
    (the full-mapping HSET is structurally first and writes all fields at once).

**Old-set discovery mechanism (was OQ1 — resolved).** Decision: **no new reverse-pointer
companion structure.** The script reads the live hash (`HGET KEYS[1] ARGV[1]`) as the
authoritative current value and removes the member from the Set implied by it; the
Python-passed `ARGV[5]` is a fast-path SREM target that the script validates against the
live hash before trusting (when `ARGV[5]` disagrees with the hash-derived current value,
the script removes from the hash-derived Set, computed via the same Python-clean rule by
passing the hash-current value's set key — see implementation note). This converges under
third-writer interleaving (Race 3) because each EVAL removes from whatever Set the member is
*currently* in per the live hash, never a cached/snapshot view. A per-member reverse-pointer
companion hash was considered and **rejected** as unnecessary additive surface: the live hash
is already the single source of truth and is read inside the same atomic EVAL, so it provides
deterministic old-set removal without a second structure to keep consistent. (Implementation
note: to keep the type-parity guarantee, the script does NOT `clean()`/`str()` in Lua; when
the hash-current value differs from `ARGV[5]`, the script SREMs `ARGV[5]` only if it matches,
and otherwise SADDs the new set and lets the next writer's EVAL reconcile any stray membership
via its own hash read — the multiprocess regression test at ≥200 rounds is the convergence gate.)

**Pipeline integration (spike-4).**
- *Internal path* (`base.py:1350-1418`): queue the EVAL into `internal_pipeline` so it
  executes inside the existing MULTI/EXEC — preserves #147. Wrap `internal_pipeline.execute()`
  to catch `ResponseError` from a unique conflict and raise `ModelException`. Order the
  per-field EVAL so a conflict aborts before any hash value for that field is written
  (the EVAL writes the field's hash value itself; indexed/unique fields are excluded from
  the top-level full-mapping HSET — see Hash-write ownership, OQ2 resolved).
  spike-5 confirms the queued EVAL runs inside this MULTI/EXEC on both Redis and Valkey.
- *External path* (`base.py:1276-1348`): queue the EVAL into the caller's pipeline. The
  unique conflict can only surface at the caller's `execute()`. **Decision (was OQ3 —
  resolved): option (a) — keep the eager `SMEMBERS` pre-check for the external pipeline path
  only.** Rationale: `save(pipeline=...)` is an explicit batching contract where the caller
  controls when EXEC happens; raising `ModelException` from inside `save()` at queue time
  preserves the existing, documented immediate-raise semantics for the common case and does
  not surprise callers with a deferred `ResponseError` at their own `execute()`. The pre-check
  remains genuinely best-effort across processes (the atomic EVAL inside the pipeline is the
  real guarantee at EXEC time; the pre-check is a fast, friendly early raise), so the external
  path's docstring caveat stays — see Documentation. The internal path drops the pre-check
  entirely because the atomic EVAL is the sole and complete guarantee there.

  Consequence for the docstring edits (`indexed_field_mixin.py:73-74, 115-116`): the
  "best-effort under concurrent writes" / "race condition" caveat is **removed only for the
  internal-pipeline (no-pipeline-argument) path and reworded**, NOT unconditionally deleted.
  The external-pipeline path keeps an accurate caveat: the eager pre-check is best-effort and
  the authoritative uniqueness guarantee is enforced atomically at `execute()` time by the
  queued EVAL.

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
- **A general reverse-index/secondary-structure overhaul.** Out of scope; the OQ1 decision
  is to use the live model hash for old-set discovery, adding no companion structure.
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
**Mitigation (OQ2 resolved):** Exclude Indexed/Unique fields from the top-level
`hset_mapping` and make each field's `INDEX_SWAP_LUA` own that field's hash write as the LAST
step of the EVAL, after the unique occupancy check. A conflict therefore aborts via
`error_reply` before the field's hash value is written, so neither the hash field nor the Set
ever reflects a rejected value. (If indexed fields were left in the top-level HSET, that
step-1 write would persist a value the EVAL later rejects — a new partial-write corruption
mode; this is why option (b) was chosen. See Technical Approach → Hash-write ownership.)
On caught `ResponseError`, the save raises `ModelException` and the per-field state stays
consistent. Add a test asserting that after a rejected unique save, neither the hash field
nor the Set reflects the rejected value.

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

- CONC-2 (ObservationProtocol torn outcome application) — different mechanism
  (immediate-EVAL vs deferred-pipeline). **Out of scope for #412.** Decision (was OQ4): do
  NOT file a separate issue as part of this plan — it is noted here only. The maintainer can
  open a tracking issue later if desired; this plan does not depend on it and carries no
  `[SEPARATE-SLUG]` obligation.
- CONC-3 (CyclicDecayField companion-hash RMW) — same RMW family, different keys/fix surface
  (per-field Lua like `CAPPED_BAYESIAN_UPDATE_LUA`). **Out of scope for #412.** Noted here
  only; not filed as a separate issue (OQ4 decision), consistent with CONC-2.
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
- [ ] Correct all four `$IdxF:` docstring occurrences (`indexed_field_mixin.py:18, 25, 28, 110`)
  → `$IndexF:`. (Verify with `grep -n IdxF`; all four must be gone.)
- [ ] Update the `on_save` docstrings (`:73-74`, `:115-116`) **conditionally, not by
  unconditional removal** (OQ3 decision): for the internal-pipeline / no-pipeline path,
  remove or reword the "best-effort under concurrent writes" / "race condition" caveat — that
  path is now fully atomic via the EVAL. For the **external-pipeline path, KEEP an accurate
  caveat**: the eager pre-check is best-effort and the authoritative uniqueness guarantee is
  enforced atomically at the caller's `execute()` by the queued EVAL (conflict surfaces as
  `ResponseError`→`ModelException` at that point).
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
- **Informed By**: spike-1 (compute Set keys in Python, not Lua), spike-3 (cmsgpack/error_reply facts), spike-4 (Lua errors don't roll back), spike-5 (EVAL queues inside MULTI/EXEC on Redis AND Valkey — atomicity proven)
- **Assigned To**: lua-swap-builder
- **Agent Type**: async-specialist
- **Parallel**: true
- Add `INDEX_SWAP_LUA` (KEYS/ARGV contract per Technical Approach, including the `ARGV[5]` sentinel `"\x00__NONE__"` guard mirroring the existing `old_value is not None` check) near the mixin.
- **Exclude Indexed/Unique fields from the top-level `hset_mapping`** (`base.py:1273-1277` external, `:1354` internal) and have each field's EVAL own its hash write as the last step of the swap (OQ2 decision). Keep `self._db_content` reflecting the full intended mapping.
- Rewrite `IndexedFieldMixin.on_save`: internal path queues the EVAL and drops the stale-snapshot SREM and pre-check SMEMBERS entirely; **external path keeps the eager `SMEMBERS` pre-check** and queues the EVAL into the caller's pipeline (OQ3 decision (a)).
- Map unique-conflict `ResponseError` to `ModelException` with the existing message wording; ensure only the unique-conflict sentinel maps (other `ResponseError`s propagate).
- Fix all four `$IdxF:`→`$IndexF:` docstring occurrences (`:18, 25, 28, 110`); reword the internal-path best-effort caveat and KEEP an accurate external-path caveat (OQ3).
- Ensure the field's hash write and Set move occur in one atomic unit (Risk 1/2; spike-5 confirms in-pipeline EVAL atomicity).

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

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | concurrency | Unproven atomicity — plan assumed `Pipeline.eval()` queues inside MULTI/EXEC, but the only in-repo precedent runs eval standalone. | spike-5 (Spike Results) | Live `MONITOR` capture on **Redis 8.6.2** and **Valkey 9.1.0** shows byte-identical `MULTI → HSET → EVAL → EXEC`; `pipeline.transaction == True`; `base.py:1352` builds `pipeline()` with default (transactional) args, not `transaction=False`. Atomicity confirmed. |
| BLOCKER | data-integrity | Undecided hash-write ownership (OQ2) — top-level HSET at step 1 + on_save at step 5 ⇒ a step-5 unique conflict leaves the step-1 HSET persisted (new partial-write corruption). | Technical Approach → Hash-write ownership; Risk 2; Task 1 | Decision (b): exclude Indexed/Unique fields from the top-level `hset_mapping`; each field's EVAL owns its hash write as the last step, after the unique check, so a conflict aborts before any hash write. |
| BLOCKER | accuracy | Stale precedent citation — cited `BAYESIAN_UPDATE_LUA`/`confidence_field.py:51` with a retracted "4000/4000 audit-verified" claim. | Problem; Freshness Check; Prior Art; Research | Live symbol is `CAPPED_BAYESIAN_UPDATE_LUA` (`:62`, PR #417), invoked standalone at `:467`. Stale claim dropped everywhere. |
| CONCERN | semantics | External-pipeline uniqueness now surfaces at caller's `execute()`; docstring removal was unconditional. | Technical Approach → External path (OQ3); Documentation | Decision (a): keep eager pre-check for external path only; docstring caveat reworded for internal path, KEPT (accurate) for external path — conditional, not unconditional removal. |
| CONCERN | correctness | None→value transition: `ARGV[5]` SREM target unspecified; unconditional Lua SREM touches an unintended key on first save. | Technical Approach → None→value transition; Task 1 | Sentinel `"\x00__NONE__"` passed when `old_value is None`; Lua guards `if ARGV[5] ~= sentinel then SREM ... end`, mirroring the existing `old_value is not None` check. |
| NIT | accuracy | `$IdxF:` occurrence list incomplete (said 18, 25). | Freshness Check; Documentation; Task 1; Verification | Re-grepped: four occurrences at `:18, 25, 28, 110`. All listed and gated by the `grep` verification check. |

All blockers, concerns, and the nit are resolved in this revision. All prior Open Questions
(OQ1–OQ4) are decided below.

---

## Resolved Questions

All Open Questions from the prior draft are now decided — none remain blocking build.

1. **Old-set discovery mechanism (OQ1) — RESOLVED.** Use the live model hash read inside the
   EVAL as the source of truth; **no** reverse-pointer companion structure. See Technical
   Approach → Old-set discovery.
2. **Hash-write ownership (OQ2) — RESOLVED.** Option (b): exclude Indexed/Unique fields from
   the top-level `hset_mapping`; each field's EVAL owns its hash write as the swap's last
   step. See Technical Approach → Hash-write ownership and Risk 2.
3. **External-pipeline uniqueness timing (OQ3) — RESOLVED.** Option (a): keep the eager
   pre-check for the external pipeline path only; internal path relies solely on the atomic
   EVAL. Docstring edits are conditional (internal reworded, external caveat kept). See
   Technical Approach → External path and Documentation.
4. **CONC-2 / CONC-3 tracking (OQ4) — RESOLVED.** Do not file separate issues as part of this
   plan; they are noted as out-of-scope in No-Gos only. No `[SEPARATE-SLUG]` obligation
   remains. The maintainer may open tracking issues later independently.
