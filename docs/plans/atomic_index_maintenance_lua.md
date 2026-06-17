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
- **Finding**: The value-Set key is `DB_key(prefix, value).redis_key` = `prefix:clean(str(python_value))` (`db_key.py:136-160, 186-206`). The stored hash field is msgpack-encoded. Two candidate strategies were examined:
  - **Reconstruct-in-Lua**: `HGET model_key field_name` → `cmsgpack.unpack` → rebuild the Set-key segment in Lua, then `SREM`. **Rejected — critical hazard**: Lua `tostring(1.0)` == `"1"` but Python `str(1.0)` == `"1.0"`; `None` vs `nil`; `True/False` vs `true/false`; and `DB_key.clean()`'s escaping (`/`-doubling, glob-char prefixing, `{&#58;}` colon-encoding) would have to be re-implemented byte-for-byte in Lua. Any drift targets the wrong Set. The script MUST NOT `str()`/`clean()` in Lua.
  - **Server-authoritative reverse pointer (Strategy 3 / reverse-lookup)**: store the *pre-cleaned old value-Set key* as a sibling hash field written **inside the same EVAL**. The next writer reads that field server-side and `SREM`s the member from exactly the Set it is actually in — unconditionally — then overwrites the pointer with the new Set key. This is type-safe (the key string is computed once in Python, where `clean(str(value))` is correct for every type, and is thereafter only ever read back verbatim — never reconstructed). **This is the strategy adopted** (see Technical Approach → Old-set discovery). It is precisely the reverse-lookup this spike found safest.
- **Confidence**: high (on the hazard, on cmsgpack availability, and on the reverse-pointer being the only convergent type-safe option).
- **Impact on plan**: Drove the decision to discover the old Set via a **server-authoritative sibling pointer hash field** (`{field_name}\x00idxset`) that stores the Python-pre-cleaned old value-Set key. The EVAL reads it, SREMs unconditionally, then rewrites it — closing the convergence gap (no membership can be stranded in a set no future writer names) while eliminating Lua-side `clean()`/`str()` parity risk entirely. See Technical Approach → Old-set discovery.

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
- **Impact on plan**: Closes the unproven-atomicity blocker. #147 atomicity (Risk 1) is preserved by the EVAL being inside MULTI/EXEC on both Redis and Valkey. Note the Lua-error-no-rollback hazard (spike-4 / Risk 2) is orthogonal and is handled by **EVAL hash-write ownership** (the EVAL is the sole writer of each indexed field; OQ2 resolved) plus **unique-check-first ordering inside the EVAL** (the conflict aborts before any mutation). The earlier "EVAL-first vs top-level-HSET ordering" framing is superseded: with write-ownership the relative queue order of the EVAL and the top-level HSET is irrelevant (Concern 1).

## Data Flow

1. **Entry point**: `Model.save()` (`base.py`), internal or external pipeline path. Two
   sub-paths share the hash-write surface: the **full-save path** (`:1257+`) and the
   **partial-save path** taken when `update_fields=[...]` is passed (`:1119-1255`); both build an
   `hset_mapping` and both must apply the indexed/unique exclusion. `async_save` delegates to
   `save()` and inherits both.
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

The fix replaces step 3 with a single atomic EVAL that owns the indexed field's hash write,
its Set move, and the advance of a server-authoritative `{field}\x00idxset` pointer — all
inside the same atomic unit as the rest of the save (step 4) so #147 holds. Indexed/unique
fields are dropped from the top-level full-mapping HSET (step 5's encode) so the EVAL is their
sole hash writer. The pointer field is excluded from the read path's decode (see Technical
Approach → Decode-path exclusion).

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
- **Data ownership**: each indexed/unique field gains **one additional hash field**,
  `{field_name}\x00idxset`, stored inside the existing model hash (no new top-level key). It
  holds the pre-cleaned old value-Set key and is the server-authoritative source for old-set
  removal; it is written and read only inside `INDEX_SWAP_LUA`. It is internal bookkeeping
  (NUL-suffixed, invisible to user field names and to `filter()`), excluded from decoding back
  into model attributes. See Technical Approach → Old-set discovery.
- **Reversibility**: moderate (revised down from "high" — Concern 4). The change is NOT
  localized to a single method. It spans: (1) `indexed_field_mixin.on_save` + the new
  `INDEX_SWAP_LUA`; (2) **four** hash-write exclusion edits in `base.py` — both full-save sites
  (`:1277`, `:1354`) AND both partial-save sites (`:1135-1140`, `:1198`) including the
  partial-save empty-mapping `DataError` guard and the `results[0]` index fix; and (3) **three**
  decode-skip edits in `encoding.py` (`:315-320`, `:327-332`, `:415-417`). Reverting cleanly
  requires undoing all three groups together — the exclusion and the EVAL-owns-the-write are a
  matched pair (excluding without the EVAL writing would drop the field value entirely). No data
  migration is required (existing Sets remain valid; `clean_indexes()` repairs any pre-existing
  corruption), and the on-disk format gains only the additive `\x00idxset` pointer field, but
  the code surface is wider than a one-method swap.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0-1 (all prior Open Questions resolved in this revision; remaining contact is optional confirmation)
- Review rounds: 1-2 (concurrency-sensitive change to the core save path)

**Scope note (Round-3 — Concern 4):** Still Medium, but the edit surface is wider than a
single-method swap implied. The hash-write exclusion must be applied at **four** sites (two
full-save + two partial-save `update_fields` HSETs), the partial-save path needs an
empty-mapping `DataError` guard and a `results[0]` index fix, and the decode skip touches three
sites in `encoding.py`. `rebuild_indexes`/`check_indexes`/`clean_indexes` were audited and need
no change. This is bounded, additive work — it does not change the appetite tier, but the
"localized to one method" framing from earlier drafts was inaccurate (reflected in Reversibility
above).

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

**Old-set discovery (the core decision — server-authoritative reverse pointer).** The
removal target must NOT be a client-computed hint. A client-computed `ARGV` SREM target only
ever names the *current writer's* old value; a membership stranded in an **older** value-Set
(left behind by an earlier interleaved writer) is never named by any future writer's hint and
persists forever. That is the exact stale-client-computed-SREM-target bug (Race 1) #412
exists to eliminate, and it would fail the "0 dual-membership over ≥200 rounds" criterion.

**The script therefore removes the member from a SERVER-AUTHORITATIVE pointer it itself
wrote on the previous save.** Each indexed field gets a sibling pointer hash field,
`{field_name}\x00idxset` (NUL-suffixed so it can never collide with a real user field name —
field names must start with a lowercase letter per the model rules, and NUL is not a legal
field-name byte). The pointer stores the **pre-cleaned old value-Set key** as a plain string.
On each save the EVAL: (1) reads the pointer, (2) if the pointer is non-empty and differs
from the new Set key, `SREM`s the member from exactly the Set the pointer names —
unconditionally, because the pointer is *the set this member is actually in* per the last
committed save — (3) `SADD`s the member to the new Set, (4) overwrites the pointer with the
new Set key, and (5) writes the field's value bytes into the hash. All five are one atomic
EVAL. Because removal always targets the set named by the record's own last-committed
pointer, **no membership can ever be stranded in a set no future writer names** — every
writer cleans up exactly the predecessor it observes, and the pointer always reflects the one
set the member belongs to. Convergence is *per-EVAL deterministic*, not statistical (see the
invariant below and Race 3).

The pointer value is computed **in Python** (`DB_key(prefix, value).redis_key`, where
`clean(str(value))` is correct for every type) and passed in pre-cleaned; Lua only ever reads
it back verbatim and writes it back verbatim — never reconstructs a key from a value. This
eliminates the Lua `str()`/`clean()` type-parity hazard from spike-1 entirely.

**Decode-path exclusion (mandatory).** `decode_popoto_model_hashmap` (`encoding.py:271`)
builds `model_attrs` from **every** field returned by `HGETALL` and passes them to
`model_class(**model_attrs)`; it also `msgpack.unpackb`s each value. The `\x00idxset` pointer
fields are plain (non-msgpack) strings and are not model attributes, so both decode branches
(`fields_only` at `:315-320` and the default at `:327-332`) MUST skip any hash field whose
name contains the `\x00` NUL byte (or, equivalently, endswith `\x00idxset`). The same skip is
required wherever else `HGETALL`/`hgetall` results are turned into attributes (e.g.
`_create_lazy_model`'s `_lazy_fields` build at `:415-417`, reached via the lazy branch at
`:323-325`; `_saved_field_values` reconstruction at `:343-346` iterates `_meta.fields` only
and is therefore already safe). This skip is the single
behavioral coupling the pointer introduces and is covered by the single-process read-back
parity test (Concern 2).

**Hash-write / hash-read site audit (Concern 3 — broader than the two full-save HSETs).** The
`\x00idxset` pointer is a schema addition, so EVERY place that writes the model hash, reads it
back into attributes, or rebuilds indexes from it was audited (against main @ 61f36aa):

| Site | File:line | Disposition |
|---|---|---|
| Full-save HSET (external) | `base.py:1277` | Exclude indexed/unique from `hset_mapping` (Hash-write ownership). |
| Full-save HSET (internal) | `base.py:1354` | Same exclusion. |
| **Partial-save HSET (external)** | `base.py:1135-1140` | **Apply exclusion to partial `hset_mapping`; guard empty-mapping DataError (Round-3 BLOCKER).** |
| **Partial-save HSET (internal)** | `base.py:1135-1136, :1198` | **Same exclusion + empty-mapping guard; fix `results[0]` indexing when HSET skipped.** |
| `async_save` | `base.py:2290` | Delegates to `save()`; no separate fix. |
| Decode → attrs (full) | `encoding.py:327-332` | Skip `\x00`-containing fields. |
| Decode → attrs (fields_only) | `encoding.py:315-320` | Skip `\x00`-containing fields. |
| Decode → lazy | `encoding.py:415-417` | Skip `\x00`-containing fields. |
| `rebuild_indexes` | `base.py:2707` | DELs all index Sets, then re-runs `on_save` per field. Decodes via `decode_popoto_model_hashmap` (`:2805`, already skips the pointer per the decode fix). The EVAL still HGETs the (now-stale) live pointer, SREMs from the just-deleted set (harmless no-op), SADDs, and rewrites the pointer — so **rebuild is convergent even with a stale pointer** (NIT 5b). No change needed beyond the decode skip. |
| `check_indexes` | `base.py:2964` | Read-only; scans the five index *Set* types and EXISTS-checks referenced instance keys. The `\x00idxset` pointer lives **inside the model hash**, not as a separate index Set, so it is never scanned and never counted as an orphan. No change needed. |
| `clean_indexes` | `base.py:3147` | Repairs orphaned Set entries / partial-write hashes; same structure as `check_indexes` (scans Sets, not hash fields). The pointer is not a Set member, so it is untouched. No change needed; existing remediation stays correct. |

Net: the only code edits the pointer forces are (1) the four hash-write exclusions (two
full-save + two partial-save) with the partial-save empty-mapping guard, and (2) the three
decode skips. `rebuild_indexes` / `check_indexes` / `clean_indexes` need no logic change — they
are already correct (rebuild is convergent against a stale pointer; the two read-only checkers
never observe the pointer because it is not a Set).

Concretely the script receives:

- `KEYS[1]` = model hash key
- `KEYS[2]` = new value Set key (Python-computed, pre-cleaned)
- `ARGV[1]` = field name (hash field)
- `ARGV[2]` = member key (the record's redis_key)
- `ARGV[3]` = new value, msgpack-packed by Python (the bytes the EVAL writes into the hash
  field; the EVAL never decodes it — it is stored verbatim, matching today's HSET payload)
- `ARGV[4]` = `unique` flag ("1"/"0")
- `ARGV[5]` = the pointer hash field name, `{field_name}\x00idxset` (passed explicitly so the
  Python side owns the naming convention; the EVAL treats it as an opaque field name)

Script logic (pseudocode):
```lua
local model_key, new_set = KEYS[1], KEYS[2]
local field, member, new_bytes, is_unique, ptr_field = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[5]
local old_set = redis.call('HGET', model_key, ptr_field)   -- server-authoritative; may be false/nil

-- idempotent re-save: same set already recorded AND member already present -> still rewrite
-- the hash field bytes (single-process read-back parity) but skip SREM/SADD churn.
if old_set == new_set and redis.call('SISMEMBER', new_set, member) == 1 then
  redis.call('HSET', model_key, field, new_bytes)          -- ALWAYS write the field value
  return 1
end

if is_unique == '1' then
  -- occupancy check excluding self, atomic with the SADD below
  local members = redis.call('SMEMBERS', new_set)
  for _, m in ipairs(members) do
    if m ~= member then return redis.error_reply('POPOTO_UNIQUE_CONFLICT') end
  end
end

if old_set and old_set ~= false and old_set ~= '' and old_set ~= new_set then
  redis.call('SREM', old_set, member)        -- remove from the EXACT set we last put it in
end
redis.call('SADD', new_set, member)
redis.call('HSET', model_key, ptr_field, new_set)   -- advance the server-authoritative pointer
redis.call('HSET', model_key, field, new_bytes)     -- value + index move together, ALWAYS written
return 1
```

The `unique` occupancy scan excludes self, so re-saving the same record is never a
self-collision (mirrors the existing `:156-161` self-exclusion). The unique check runs
**before** any SREM/SADD/HSET, so a conflict aborts via `error_reply` with nothing written
(see Risk 2). On a genuine first save the pointer is absent (`HGET` → `false`), so no SREM is
attempted — this is the type-safe replacement for the old `old_value is not None` Python
guard (Concern 5), needing no sentinel string.

**Single-process read-back parity (Concern 2 — resolved).** Because indexed/unique fields are
excluded from the top-level full-mapping HSET (see Hash-write ownership), read-back depends on
every EVAL writing the field's value bytes. The script therefore writes `HSET model_key field
new_bytes` on **every** non-error path — including the idempotent/no-op branch above — so a
fresh save always persists the field value even when the index does not move. A new
single-process test asserts hash-content parity: after `save()`, `HGET model_key field` equals
the value the legacy (full-mapping HSET) path would have written, for indexed, unique, and
no-op-re-save cases.

**Convergence invariant (NIT 5 — stated explicitly).** *After every individual `INDEX_SWAP_LUA`
EVAL commits, the record is a member of exactly the one value-Set named by its
`{field}\x00idxset` pointer, and of no other value-Set for that field; and the pointer equals
the value-Set key derived from the bytes just written to the hash field.* This holds per-EVAL
(not merely in aggregate over many rounds) because the EVAL is serialized by Redis and, within
one EVAL, the SREM removes from the previously-recorded set, the SADD adds to the new set, and
the pointer+value advance together. Under any interleaving of N concurrent writers, the
last-to-EXEC writer's post-state satisfies the invariant; every earlier writer's post-state
also satisfied it at its own EXEC. The ≥200-round test is the *regression gate* for this
invariant, not its proof.

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
  - **Why exclusion is necessary (Concern 1 — re-examined).** The prior draft justified
    excluding indexed fields with "the full-mapping HSET is structurally first and immovable,"
    which is unsound — the internal path already adopts EVAL-first ordering, so command order
    is *not* fixed. The real, ordering-independent reason exclusion is required: once the EVAL
    **owns the field's hash write** (mandatory here, because the value bytes and the index
    move must be one atomic step for the convergence invariant — the pointer must equal the
    value just written), leaving the same field in the top-level full-mapping HSET would mean
    **two commands write the same hash field in one transaction.** That is redundant at best
    and, on the no-rollback path (Risk 2), actively harmful: a top-level HSET of the indexed
    field would persist a value the EVAL's unique check then rejects, since the top-level HSET
    is not gated by the EVAL's `error_reply`. Excluding indexed/unique fields from the
    top-level mapping makes the EVAL the *single* writer of those fields, so the unique check
    gates the only write that exists. This holds regardless of whether the EVAL is queued
    before or after the top-level HSET — it is a write-ownership argument, not an ordering one.
  - Rejected alternative (a) — keep the top-level HSET writing *all* fields and have the EVAL
    move *only* the Sets (not write the field value) — was rejected because it severs the
    value-write from the index-move: the field bytes (top-level HSET) and the Set membership
    (EVAL) would then be two separate writes that a unique `error_reply` can split (HSET
    commits, EVAL aborts ⇒ hash holds a value with no/ wrong index entry), reopening exactly
    the partial-write window this fix closes. Co-locating the value write and the index move
    inside one EVAL (option (b)) is what makes the convergence invariant hold and makes the
    unique check gate the value write.

**Partial-save path `update_fields` (Round-3 BLOCKER — resolved).** The two full-save sites
(`base.py:1277` external, `:1354` internal) are NOT the only places the hash is written. The
**partial-save path** taken when `save(update_fields=[...])` is passed (`base.py:1119-1255`)
builds its **own** `hset_mapping` from the encoded full mapping filtered to the listed fields
(`:1135-1136`), HSETs it (`:1140` external / `:1198` internal), and *then* runs each listed
field's `on_save` (`:1164-1173` external / `:1224-1233` internal) — which, under this plan,
queues the field's `INDEX_SWAP_LUA`. If an Indexed/Unique field appears in `update_fields`,
that field is therefore written **twice** in one transaction (once by the partial HSET, once
by the EVAL), and on a unique conflict the partial HSET is **not** gated by the EVAL's
`error_reply`, so the rejected value stays persisted in the hash — the *exact* double-write /
no-rollback corruption the full-save fix closes, reopened on the partial path.
`async_save(update_fields=...)` (`base.py:2290`) delegates straight to `save()` via
`to_thread`, so it inherits the gap and needs no separate fix beyond fixing `save()`.

  - **Decision: apply the identical IndexedFieldMixin exclusion to the partial `hset_mapping`.**
    The dict comprehension at `:1135-1136` MUST drop entries for fields whose class is an
    `IndexedFieldMixin` subclass (Indexed + Unique), exactly as the full-save mapping does — so
    those fields are written **only** by their EVAL. The listed-field `on_save` loop
    (`:1164` / `:1224`) is unchanged: it still queues the EVAL for every listed field, including
    the now-excluded indexed/unique ones, so they are still indexed AND value-written (by the
    EVAL). Non-indexed listed fields continue to be written by the partial HSET.
  - **Empty-mapping guard (mandatory — DataError).** If `update_fields` contains **only**
    indexed/unique field names, the filtered `hset_mapping` becomes `{}`, and redis-py raises
    `redis.DataError("HSET requires at least one field/value pair")` (equivalently the
    server rejects an empty `HSET`). The partial HSET call (`:1140` / `:1198`) MUST therefore be
    **guarded**: skip the `pipeline.hset(..., mapping=hset_mapping)` entirely when
    `hset_mapping` is empty (the EVALs queued by the subsequent `on_save` loop perform the only
    necessary hash writes). The `HSET` result is currently captured as `db_response = results[0]`
    on the internal path (`:1242`); when the HSET is skipped, the result-index bookkeeping must
    be adjusted (or a benign return value substituted) so the internal path still returns a
    truthy db_response and `results[0]` indexing does not misalign. Add an explicit test for
    `save(update_fields=[<only an indexed field>])` (and the unique variant) to lock this in.
  - **`obsolete_key` cleanup (`:1142-1162` / `:1199-1222`) is unaffected**: it runs `on_delete`
    for *all* fields against the obsolete key and DELs the old hash, which does not write the new
    hash's indexed fields and so does not collide with the EVAL.

**Old-set discovery mechanism (was OQ1 — resolved).** Decision: **a server-authoritative
sibling pointer field** `{field_name}\x00idxset`, written by the EVAL on every save, holding
the pre-cleaned old value-Set key. The next save's EVAL reads it server-side and SREMs the
member from exactly that Set — unconditionally — then advances the pointer. See Technical
Approach → Old-set discovery for the full rationale and script. This *is* the reverse-lookup
spike-1 found safest; it is stored inside the model hash (same key, one extra field), so it
adds no separate top-level structure to keep consistent, and it is read+written inside the
same atomic EVAL as the value and Set move. A purely client-computed SREM-target hint
(`ARGV[5]`-as-target) was **rejected** as the core blocker of the prior draft: it can only
name the current writer's old value, so a membership stranded in an *older* value-Set by an
earlier interleaved writer is never named by any future hint and persists forever (re-opening
Race 1). The server-authoritative pointer closes that gap because removal always targets the
set named by the record's own last-committed pointer. The pointer string is computed in
Python (correct `clean(str(value))` for all types) and only ever read/written verbatim in
Lua — no Lua-side `clean()`/`str()` (type-parity guarantee preserved).

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
  cleaned) — the pointer is set to that Set's key like any other value, and the value bytes
  are msgpack `None` (spike-3). On a genuine first save the `\x00idxset` pointer is absent
  (`HGET`→`false`), so no SREM is attempted (the type-safe replacement for the old
  `old_value is not None` guard). Add a test: first save (no pointer) → value `None` → back to
  a value, asserting correct Set membership and pointer at each step.
- Re-saving the same instance with the same value must be a no-op (idempotent), not a
  self-collision for unique fields (existing self-exclusion logic at `:156-161`), and must
  still write the field's value bytes to the hash (read-back parity).

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
  can skip if it cannot run spawn-multiprocess against the server) PLUS single-process
  partial-save (`update_fields`) parity/empty-mapping/conflict cases (Round-3 BLOCKER).
- `tests/test_*` exercising `save(update_fields=...)` (partial save) — VERIFY unchanged: the
  exclusion + empty-mapping guard must not regress existing partial-save behavior for
  non-indexed fields.

No existing test asserts the *buggy* behavior, so nothing needs DELETE/REPLACE.

## Rabbit Holes

- **Reconstructing `DB_key.clean(str(value))` inside Lua.** Type-parity (float `1.0`,
  `None`, bool) makes this brittle (spike-1). Avoid — compute Set keys in Python, pass in.
- **A general reverse-index/secondary-structure overhaul.** Out of scope. The OQ1 decision
  adds exactly one bookkeeping field per indexed field (`{field}\x00idxset`) inside the
  existing model hash for old-set discovery — not a new top-level index structure or a
  general reverse-index subsystem.
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
**Impact:** Any Lua `error_reply` (the intended unique-conflict signal) or any *unexpected*
Lua runtime error (script bug, wrong-type, OOM) does NOT roll back commands already applied
earlier in the same EXEC. Two distinct sub-hazards:
  - **(2a) unique-conflict (expected error):** if the rejected value had already been written
    to the hash by a separate command, the hash would hold a value the index rejected.
  - **(2b) mid-EXEC non-unique error (unexpected):** a script that performed SREM/SADD/HSET in
    a partial order and then errored could commit a hash value whose indexed Set entry was
    never written (or vice-versa). Because `filter()` reads Sets directly and never
    re-verifies against the hash (Data Flow step 6), such a record could become invisible to
    queries or appear under a stale value.
**Mitigation (OQ2 resolved + ordering inside the EVAL):**
  1. Exclude Indexed/Unique fields from the `hset_mapping` at **all four** HSET sites — both
     full-save (`base.py:1277`/`:1354`) AND both partial-save `update_fields` branches
     (`:1140`/`:1198`) — so the EVAL is the **single** writer of each indexed field's hash value
     (Technical Approach → Hash-write ownership + Partial-save path). There is no separate HSET
     (full or partial) that a later EVAL error could leave stranded. The partial-save path also
     guards the empty-mapping `DataError`.
  2. **The unique occupancy check is the FIRST mutating-relevant step in the EVAL — before any
     SREM, SADD, or HSET.** On conflict the EVAL `error_reply`s having written nothing, so
     neither the value, the pointer, nor any Set changes (closes 2a deterministically).
  3. For 2b: the EVAL is short, branch-simple, and performs no operation that can fail
     mid-sequence under normal data (HGET/SISMEMBER/SMEMBERS/SREM/SADD/HSET on known key
     types). The only deliberate error path is the unique `error_reply` at step 2. A script
     *bug* is a code-correctness matter, not a runtime race; it is guarded by tests, not by
     rollback. To bound the blast radius of a hypothetical 2b, the SADD+pointer-HSET+value-HSET
     are ordered so the member is added to the new Set and the pointer advanced **immediately
     before** the value HSET, minimizing any window; and a regression test deliberately injects
     a forced non-unique Lua error after a partial mutation (via a test-only script variant) to
     assert that no half-state is queryable through `filter()` — i.e. either the whole swap
     applied or, on the production script, the unique-conflict path left nothing.
On caught `ResponseError`, the save raises `ModelException` and the per-field state stays
consistent. Tests: (i) after a rejected unique save, neither the hash field, the pointer, nor
the Set reflects the rejected value; (ii) the forced-mid-error variant leaves no
`filter()`-visible inconsistency.

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
**Data prerequisite:** The SREM target must be the set the member is *actually* in, recorded
server-side — not a client snapshot and not a client-computed hint that only names the current
writer's old value.
**State prerequisite:** The read-of-pointer and the SREM/SADD/pointer-advance must be one
atomic unit.
**Mitigation:** EVAL reads the server-authoritative `{field}\x00idxset` pointer it wrote on
the previous save, SREMs from exactly that set, then advances the pointer — atomically
(Technical Approach → Old-set discovery). No membership can be stranded in a set no future
writer names.

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
**Trigger:** Three+ processes saving the same record to different values in rapid succession,
including a membership left in an *older* value-Set by an earlier interleaved writer.
**Data prerequisite:** Each EVAL must remove from the set named by the record's own
last-committed server-authoritative pointer — guaranteeing every stale membership is named
and removed by the next writer, with none stranded.
**State prerequisite:** The pointer is read, used for SREM, and re-advanced inside one
serialized EVAL.
**Mitigation:** Per the Convergence invariant (Technical Approach), after every EVAL the
record is in exactly the one set its pointer names and no other; therefore after the
last-to-EXEC writer the record has exactly one membership matching its stored value. This is
*per-EVAL deterministic*, not statistical. The ≥200-round multiprocess test is the regression
gate, not the proof.

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
- [ ] Docstring for the new `INDEX_SWAP_LUA` explaining the KEYS/ARGV contract, the
  server-authoritative `{field}\x00idxset` pointer, and the type-parity rationale for computing
  Set keys in Python.
- [ ] Comment the decode-path exclusion in `encoding.py` explaining why `\x00`-containing hash
  fields are internal index bookkeeping and must be skipped during attribute reconstruction.

## Success Criteria

- [ ] 2-process barrier-synchronized same-record reproduction: **0 dual-membership over
  ≥200 rounds** (baseline 58–59/200).
- [ ] 2-process UniqueField race: **0 rounds where both saves succeed** and **0 value Sets
  with >1 member over ≥100 rounds** (baseline 67–68/100); exactly one save per round raises
  `ModelException`. Test asserts against the correct `$UniquF:` prefix (derived from
  `field_class_key`, not hard-coded).
- [ ] After any concurrent round, `filter(field=v)` returns only records whose stored hash
  value is actually `v`.
- [ ] **Single-process hash-content parity (Concern 2):** after `save()`, `HGET model_key
  field` equals the bytes the legacy full-mapping-HSET path would have written, for indexed,
  unique, no-op-re-save, and `None`-valued cases — proving the EVAL always writes the field
  value even on a no-op index move.
- [ ] **Convergence invariant holds (NIT 5):** after every round, for each record the
  `{field}\x00idxset` pointer names exactly the one value-Set the record is a member of, and
  that set matches the record's stored value; 0 records in more than one value-Set per field.
- [ ] **Rejected-unique leaves nothing (Risk 2a):** after a unique conflict, neither the hash
  field, the pointer, nor the target Set reflects the rejected value.
- [ ] **No `filter()`-visible half-state under a forced mid-EVAL error (Risk 2b):** the
  forced-error test variant leaves either a complete swap or no change.
- [ ] **Pointer field is invisible to the model API:** `\x00idxset` fields never decode into
  model attributes and never appear in `filter()` results.
- [ ] **Partial-save (`update_fields`) parity & safety (Round-3 BLOCKER):** an indexed/unique
  field passed in `update_fields` is written exactly once (no double-write), `save(update_fields=
  [<only indexed/unique>])` raises no `DataError`, and a partial-save unique conflict raises
  `ModelException` leaving neither the hash field, the pointer, nor the Set holding the rejected
  value. `async_save(update_fields=...)` shares the same guarantees (delegates to `save()`).
- [ ] New multiprocess regression test added (marked/skippable if CI cannot run spawn-mp).
- [ ] Existing suite passes unchanged — no API, exception-type, or key-schema differences
  for single-process users (`test_atomic_save.py` green = #147 preserved). The only new hash
  field is the internal `\x00idxset` pointer, excluded from decode.
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
- **Validates**: tests/test_atomic_save.py, tests/test_indexed_fields.py (if present), tests/test_queries.py, tests/test_concurrent_index_integrity.py (parity + invisibility cases)
- **Informed By**: spike-1 (server-authoritative reverse pointer; never `str()`/`clean()` in Lua), spike-3 (cmsgpack/error_reply facts), spike-4 (Lua errors don't roll back), spike-5 (EVAL queues inside MULTI/EXEC on Redis AND Valkey — atomicity proven), round-2 convergence blocker (pointer design), Concern 1 (write-ownership), Concern 2 (always-write field value)
- **Assigned To**: lua-swap-builder
- **Agent Type**: async-specialist
- **Parallel**: true
- Add `INDEX_SWAP_LUA` (KEYS/ARGV contract per Technical Approach → Old-set discovery). The
  script: reads the server-authoritative `{field}\x00idxset` pointer (`ARGV[5]` = pointer
  field name); for unique, runs the self-excluding occupancy check **first** (abort before any
  mutation); SREMs the member from the pointer-named set when non-empty and changed; SADDs the
  new set; HSETs the pointer to the new set key; HSETs the field value bytes (`ARGV[3]`) on
  **every** non-error path (including the idempotent no-op branch — Concern 2). No
  `str()`/`clean()` in Lua.
- **Exclude Indexed/Unique fields from the top-level `hset_mapping`** (`base.py:1273-1277` external, `:1354` internal) so the EVAL is the SOLE writer of each indexed field's hash value (OQ2 decision (b); Concern 1 — write-ownership, not ordering). Keep `self._db_content` reflecting the full intended mapping.
- **Apply the SAME exclusion to the partial-save path** (`save(update_fields=...)`): filter the partial `hset_mapping` dict comprehension (`base.py:1135-1136`) to drop IndexedFieldMixin-subclass fields, on BOTH the external (`:1140`) and internal (`:1198`) branches (Round-3 BLOCKER). The listed-field `on_save` loop (`:1164` / `:1224`) is unchanged and queues the EVAL for those fields. `async_save` (`:2290`) delegates to `save()` and needs no separate change.
- **Guard the empty `hset_mapping` (DataError)** in the partial-save path: when `update_fields` contains only indexed/unique fields the filtered mapping is `{}`; skip the `pipeline.hset(..., mapping={})` call (`:1140` / `:1198`) entirely, and on the internal path adjust the `db_response = results[0]` bookkeeping (`:1242`) so a skipped HSET does not misalign the result index or return a falsy db_response.
- **Add decode-path exclusion** for `\x00idxset` pointer fields in `decode_popoto_model_hashmap` (`encoding.py:315-320` and `:327-332`) and `_create_lazy_model` (`:415-417`): skip any hash field whose name contains `\x00`. (Pointer values are plain strings, not msgpack — they must never reach `msgpack.unpackb` or `model_class(**attrs)`.)
- Rewrite `IndexedFieldMixin.on_save`: internal path queues the EVAL and drops the stale-snapshot SREM and pre-check SMEMBERS entirely; **external path keeps the eager `SMEMBERS` pre-check** and queues the EVAL into the caller's pipeline (OQ3 decision (a)). Compute the pre-cleaned new-set key and the pointer field name in Python; pass them in.
- Map unique-conflict `ResponseError` to `ModelException` with the existing message wording; match only the `POPOTO_UNIQUE_CONFLICT` sentinel (other `ResponseError`s propagate).
- Fix all four `$IdxF:`→`$IndexF:` docstring occurrences (`:18, 25, 28, 110`); reword the internal-path best-effort caveat and KEEP an accurate external-path caveat (OQ3).
- Ensure the field's value HSET, pointer HSET, and Set move occur in one atomic unit (Risk 1/2; spike-5 confirms in-pipeline EVAL atomicity).

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
- **Assert the convergence invariant after each round (NIT 5):** for each record, the `{field}\x00idxset` pointer names exactly the one set the record is in, and that set matches its stored value.
- Add single-process tests: nullable field through `None` (first-save = no pointer = no SREM), idempotent re-save, rejected-unique leaves neither hash, pointer, nor Set changed (Risk 2a).
- **Hash-content parity test (Concern 2):** after `save()`, `HGET model_key field` equals the legacy full-mapping bytes for indexed/unique/no-op/`None` cases.
- **Pointer-invisibility test:** `\x00idxset` fields never decode into attributes and never appear in `filter()` results.
- **Forced mid-EVAL error test (Risk 2b):** a test-only script variant that errors after a partial mutation leaves no `filter()`-visible half-state.
- **Partial-save (`update_fields`) parity + conflict tests (Round-3 BLOCKER):**
  (a) `save(update_fields=[<indexed field>])` writes the field value exactly once and moves the index correctly — `HGET` parity with the legacy path, no double-write;
  (b) `save(update_fields=[<only an indexed field>])` and the unique-only variant do NOT raise `redis.DataError` (empty-mapping guard) and still index/value-write via the EVAL;
  (c) `save(update_fields=[<unique field>])` that conflicts raises `ModelException` and leaves **neither** the hash field, the pointer, nor the target Set holding the rejected value (no un-gated partial HSET persisted);
  (d) the internal-path partial save still returns a truthy db_response when the HSET is skipped.

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
| Pointer field excluded from decode | `.venv/bin/pytest tests/test_concurrent_index_integrity.py -q -k "parity or invisible"` | exit code 0 |
| Partial-save (`update_fields`) safety | `.venv/bin/pytest tests/test_concurrent_index_integrity.py -q -k "update_fields or partial"` | exit code 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | concurrency | Unproven atomicity — plan assumed `Pipeline.eval()` queues inside MULTI/EXEC, but the only in-repo precedent runs eval standalone. | spike-5 (Spike Results) | Live `MONITOR` capture on **Redis 8.6.2** and **Valkey 9.1.0** shows byte-identical `MULTI → HSET → EVAL → EXEC`; `pipeline.transaction == True`; `base.py:1352` builds `pipeline()` with default (transactional) args, not `transaction=False`. Atomicity confirmed. |
| BLOCKER | data-integrity | Undecided hash-write ownership (OQ2) — top-level HSET at step 1 + on_save at step 5 ⇒ a step-5 unique conflict leaves the step-1 HSET persisted (new partial-write corruption). | Technical Approach → Hash-write ownership; Risk 2; Task 1 | Decision (b): exclude Indexed/Unique fields from the top-level `hset_mapping`; each field's EVAL owns its hash write as the last step, after the unique check, so a conflict aborts before any hash write. |
| BLOCKER | accuracy | Stale precedent citation — cited `BAYESIAN_UPDATE_LUA`/`confidence_field.py:51` with a retracted "4000/4000 audit-verified" claim. | Problem; Freshness Check; Prior Art; Research | Live symbol is `CAPPED_BAYESIAN_UPDATE_LUA` (`:62`, PR #417), invoked standalone at `:467`. Stale claim dropped everywhere. |
| CONCERN | semantics | External-pipeline uniqueness now surfaces at caller's `execute()`; docstring removal was unconditional. | Technical Approach → External path (OQ3); Documentation | Decision (a): keep eager pre-check for external path only; docstring caveat reworded for internal path, KEPT (accurate) for external path — conditional, not unconditional removal. |
| CONCERN | correctness | None→value transition: SREM target unspecified; unconditional Lua SREM touches an unintended key on first save. | Technical Approach → Old-set discovery / Empty-Input handling; Task 1 | Superseded by the server-authoritative pointer (round 2): on first save the `\x00idxset` pointer is absent (`HGET`→`false`), so no SREM is attempted — no sentinel needed. |
| NIT | accuracy | `$IdxF:` occurrence list incomplete (said 18, 25). | Freshness Check; Documentation; Task 1; Verification | Re-grepped: four occurrences at `:18, 25, 28, 110`. All listed and gated by the `grep` verification check. |

### Round 2 (second revision pass)

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | concurrency | Old-set discovery does not converge: a client-computed `ARGV[5]` SREM target only names the current writer's old value, so a membership stranded in an OLDER value-Set is never named by any future writer and persists forever — re-introducing Race 1. | Technical Approach → Old-set discovery; spike-1; Race 1; Race 3; Architectural Impact | Replaced the client-computed hint with a **server-authoritative `{field}\x00idxset` pointer** written inside the EVAL, holding the pre-cleaned old-set key. Each writer SREMs from exactly the set its own last-committed pointer names — unconditionally — closing the convergence gap. Per-EVAL deterministic invariant stated. |
| CONCERN | correctness | OQ2 rejection of alt (a) was unsound ("full-mapping HSET is structurally first and immovable" contradicts the adopted EVAL-first ordering). | Technical Approach → Hash-write ownership; spike-4 impact | Re-argued on **write-ownership**, not ordering: the EVAL must be the sole writer of each indexed field (value+index in one atomic step), so leaving the field in the top-level HSET means two writers to one hash field; the unique check then gates only one of them. Holds regardless of queue order. |
| CONCERN | correctness | Read-back parity: excluding indexed fields from the wire HSET makes read-back depend on every EVAL writing them; the no-op branch could leave a field unwritten. | Technical Approach → Single-process read-back parity; Lua sketch; Success Criteria; Task 2 | EVAL writes `HSET model_key field new_bytes` on **every** non-error path, including the idempotent no-op branch. New hash-content parity success criterion + test. |
| CONCERN | correctness | Mid-EXEC non-unique Lua error can commit a hash missing indexed values; `filter()` never re-verifies ⇒ invisible records. | Risk 2 (2a/2b split); Success Criteria; Task 2 | Unique check is first (aborts before any mutation); EVAL is the sole writer with no separate top-level HSET to strand; forced-mid-error test asserts no `filter()`-visible half-state. |
| CONCERN | consistency | spike-1 self-contradictory: named reverse-lookup safest but Resolution rejected it. | spike-1; Old-set discovery | Reconciled: the server-authoritative pointer IS the reverse-lookup spike-1 found safest; spike-1 Finding/Resolution/Impact now all adopt it. |
| NIT | clarity | Convergence invariant only stated as "≥200-round test passes". | Technical Approach → Convergence invariant; Race 3; Success Criteria | Deterministic per-EVAL invariant stated explicitly; the round-test is named as the regression gate, not the proof. |

### Round 3 (third revision pass)

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | data-integrity | `update_fields` partial-save path reopens the double-write / unique-no-rollback corruption: it builds its own `hset_mapping` (`base.py:1135-1136`), HSETs it (`:1140`/`:1198`), then runs the EVAL for the same fields — an indexed/unique field in `update_fields` is written twice, and a unique conflict leaves the un-gated partial HSET persisted. `async_save` (`:2290`) inherits it. | Technical Approach → Partial-save path `update_fields`; Hash-write site audit; Task 1; Task 2; Success Criteria | Apply the IndexedFieldMixin exclusion to the partial `hset_mapping` on both branches; guard the empty-mapping `DataError`; fix internal `results[0]` indexing; add `save(update_fields=[indexed/unique])` parity + conflict tests. `async_save` delegates to `save()` and is covered. |
| CONCERN | correctness | Apply/audit the exclusion at EVERY hash-write and hash-read site, not just the two full-save sites (filter at the HSET call site, not the encoder). | Technical Approach → Hash-write site audit table; Hash-write ownership | Audited all hash-write (4), decode-read (3), and index-rebuild/check (3) sites. Exclusion applied at the four HSET call sites; decode skip at three sites. |
| CONCERN | correctness | Decode skip must be a pre-filter before `msgpack.unpackb`. | Technical Approach → Decode-path exclusion; Task 1 | The skip drops `\x00`-containing fields **before** they reach `msgpack.unpackb`/`model_class(**attrs)` (pointer values are plain strings, not msgpack). |
| CONCERN | completeness | The `\x00idxset` field is a schema addition needing a broader exclusion audit (`clean_indexes`/`check_indexes`/`rebuild_indexes`). | Technical Approach → Hash-write site audit table | Audited: `check_indexes`/`clean_indexes` scan index Sets (not hash fields) so never observe the pointer; `rebuild_indexes` is convergent against a stale pointer. No logic change needed in any of the three. |
| CONCERN | accuracy | Appetite/reversibility claims are now inaccurate. | Appetite → Scope note; Architectural Impact → Reversibility | Reversibility revised down to "moderate" (edit spans 4 HSET sites + 3 decode sites + the EVAL, not one method); Appetite kept Medium with an explicit scope note. |
| NIT | accuracy | Note that `rebuild_indexes` is already convergent with a stale pointer. | Technical Approach → Hash-write site audit table (`rebuild_indexes` row); NIT 5b | Documented: rebuild DELs all Sets then re-runs `on_save`; the EVAL SREMs from the just-deleted (stale-pointer-named) set harmlessly and rewrites the pointer. |

All round-1, round-2, and round-3 blockers, concerns, and nits are resolved in this revision.
All prior Open Questions (OQ1–OQ4) are decided below.

---

## Resolved Questions

All Open Questions from the prior draft are now decided — none remain blocking build.

1. **Old-set discovery mechanism (OQ1) — RESOLVED (revised in round 2).** Use a
   **server-authoritative `{field}\x00idxset` pointer** stored inside the model hash, written
   by the EVAL on each save and holding the pre-cleaned old value-Set key. The next save's
   EVAL reads it and SREMs from exactly that set unconditionally, then advances it. This is the
   reverse-lookup spike-1 found safest and is the only convergent, type-safe option (a
   client-computed SREM hint cannot converge — round-2 blocker). See Technical Approach →
   Old-set discovery.
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
