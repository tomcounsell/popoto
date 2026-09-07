---
status: Planning
type: chore
appetite: Medium
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/649
---

# Recipes through the field layer, PR 5: `memory_lifecycle`

## Problem

`src/popoto/recipes/memory_lifecycle.py` is the last and largest recipe in the
#630 series. At `b7863500` it holds **15 `POPOTO_REDIS_DB` call sites** plus the
import. (Issue #649 says 17; that count includes the two `record.delete()`
calls at L882 and L1016, which already go through the model layer and need no
change.)

| # | Line | Command | Key it touches | What it is asking |
|---|---|---|---|---|
| 1 | 307 | `object("idletime", k)` | model hash | "how long since anything touched this record?" |
| 2 | 334 | `zscore(sortedkey, k)` | `$SortedF:...` | "what score does this record hold in the importance index?" |
| 3 | 837 | `hgetall(live_key)` | model hash | "give me the record's stored hash verbatim, for archival" |
| 4 | 871–874 | `pipeline()` + `hset` + `zadd` | `$TOMB:{M}:data`, `:index` | "archive this tombstone atomically" |
| 5 | 906 | `zcard(index_key)` | `$TOMB:{M}:index` | "how many tombstones are retained?" |
| 6 | 909 | `zrange(index_key, 0, n)` | `$TOMB:{M}:index` | "which are the n oldest?" |
| 7 | 913–916 | `pipeline()` + `hdel` + `zrem` | both `$TOMB` keys | "evict these tombstones atomically" |
| 8 | 927 | `zcard(index_key)` | `$TOMB:{M}:index` | "how many tombstones are retained?" |
| 9 | 936 | `zrevrange(index_key, 0, stop)` | `$TOMB:{M}:index` | "newest deaths first" |
| 10 | 943 | `hmget(data_key, keys)` | `$TOMB:{M}:data` | "fetch these entries" |
| 11 | 954 | `hget(data_key, k)` | `$TOMB:{M}:data` | "fetch one entry" |
| 12 | 970 | `hget(data_key, k)` | `$TOMB:{M}:data` | "fetch one entry" |
| 13 | 996–999 | `pipeline()` + `hdel` + `zrem` | both `$TOMB` keys | "purge one tombstone atomically" |
| 14 | 1005 | `delete(data_key, index_key)` | both `$TOMB` keys | "purge the whole store" |
| 15 | 1118 | `hget(live_key, tier_field)` | model hash | "re-read the authoritative tier before deleting" |

Two distinct layer violations, not one:

- **Model-hash and index reads done by hand** (sites 1, 2, 3, 15). The recipe
  builds the model's `redis_key`, builds the sorted-set key via
  `get_special_use_field_db_key`, and decodes raw hash bytes with
  `decode_lazy_field`. All four are things the model/field layer knows and the
  recipe should not.
- **A whole hand-built keyspace** (sites 4–14). `$TOMB:{Model}:data` (hash) and
  `$TOMB:{Model}:index` (zset) are managed entirely with raw commands, with the
  retention policy, the msgpack entry format, and the two-key atomicity
  contract all living in the recipe.

## Freshness Check

Baseline: `b7863500` (branch base). Issue filed `2026-09-06T11:15:19Z`.

- **Disposition: Minor drift — in the plan's favour.** Since the issue was
  filed, `3a5d00ca` (PR #644, `provenance_journal`) landed and added the two
  prerequisites this plan consumes: `Model.exists()` and `popoto.batch()`. Both
  verified present at `src/popoto/models/base.py:2019` and
  `src/popoto/batch.py:40`.
- Every file:line cited in #649 re-read and confirmed at `b7863500`. The one
  correction: the issue says `_tombstone_keys` is "around L789" — it is exactly
  L789. `OBJECT IDLETIME` is at L307 as stated.
- `src/popoto/recipes/memory_lifecycle.py` and `tests/test_memory_lifecycle.py`
  have **no** commits since the issue was filed.
- **Overlap check:** four sibling plans exist in `docs/plans/`
  (`recipes_field_layer_{default_memory,provenance_journal,policy_cache,graph_traversal}.md`).
  None touches `memory_lifecycle.py`. #648 is running in parallel on a
  different recipe. No conflict.
- **One premise in the issue is false and is corrected below** (see Spike 2):
  `OBJECT IDLETIME` is *not* Redis-only.

## Prior Art

| Ref | Relevance |
|---|---|
| #630 | Umbrella. Six recipes bypass the field layer. This is the last one. **Do not close it** — #648 is in flight; the umbrella is closed by hand. |
| #634 (`default_memory`) | First in series. Established "relocate the knowledge, keep the wire". |
| #644 (`provenance_journal`) | Added `Model.exists()` and `popoto.batch()`. Established the command-sequence parity capture as the acceptance oracle. |
| #653 (`policy_cache`) | Established that a *relocation* is the shape, even for a Lua script: byte-identical text, byte-identical wire, re-export for compat. |
| #654 (`graph_traversal`) | Established that a new field-layer entry point is written to the exact read the recipe was doing, not to the nearest existing API, when the nearest API would change the command. |
| #642 | Why per-recipe issues exist, and why patches + docs must precede `verdict finalize`. |
| #491 | The tombstone design being relocated here. Its rationale (removal, not a hidden flag; bounded retention; reversibility) is preserved verbatim. |
| #494 (open) | Will consume `Tombstone.fingerprint` as a negative prior. The fingerprint contract must survive this PR unchanged. |

### Why previous fixes did not cover this

None attempted. `memory_lifecycle` was deliberately sequenced last in #630
because it is the only recipe that owns a *keyspace* rather than a handful of
calls.

## Research

Queries run against `valkey.io` command documentation, plus the repo's own CI
workflow definitions.

- **`OBJECT IDLETIME` on Valkey** — https://valkey.io/commands/object-idletime/
  — supported since Valkey 2.2.3, with the same single caveat Redis carries:
  unavailable when `maxmemory-policy` is set to an LFU policy. This
  **contradicts the premise in #649** that the command is "Redis-only with no
  portable equivalent". It is a core command on both servers, not a module
  command, so it does not violate the no-Redis-modules constraint. Informs
  Decision 2 below.
- **Local server probe** — `redis-cli -n 15 OBJECT IDLETIME <k>` returns `0`;
  `CONFIG GET maxmemory-policy` returns `noeviction`. **This is Redis, not
  Valkey** (`redis_version:8.6.2`), so it is evidence that the command works
  and that the LFU caveat is inert on the dev box — and evidence of *nothing*
  about Valkey. Do not cite it as Valkey evidence.
- `.github/workflows/tests.yml:106-163` — the Valkey job runs
  `valkey/valkey:8-alpine` and asserts the server really is Valkey. **Its
  passing is also not evidence here**: `grep -rn "idletime" tests/` returns no
  test that exercises the fallback (the only test-side mention is a comment at
  `test_memory_lifecycle.py:290`), so the Valkey job has never executed L307.

## Spike Results

### spike-1: Would a real `Tombstone` Model preserve the wire and the tests?
- **Assumption**: "#630 proposes making `Tombstone` a real model with a
  `SortedField` recency index" is a viable design under this PR's parity
  contract.
- **Method**: code-read (`tests/test_memory_lifecycle.py`, the store's five
  entry points).
- **Result**: **No — refuted, decisively.**
  1. `tests/test_memory_lifecycle.py:1145` (`test_partial_tombstone_entry_is_dropped_not_inflated`)
     calls `lifecycle._tombstone_keys()` and writes a raw msgpack payload with
     `redis.hset(data_key, partial_key, ...)` + `redis.zadd(index_key, ...)`,
     then asserts `tombstone_count() == 2`. A per-record model layout has no
     such hash to inject into. **This test would have to change**, which the
     issue's contract forbids ("no test expectation may change").
  2. The wire diff would be enormous, not empty: `purge_all_tombstones()` is
     one `DEL` over two keys today and becomes N deletes each firing
     `on_delete` hooks; retention eviction is one `ZRANGE` + one pipelined
     `HDEL`/`ZREM` and becomes a query plus N model deletes.
  3. The archived payload is the *raw* hash of a foreign model — bytes keys,
     already-msgpack values. Storing that as a model field means re-encoding
     someone else's encoded hash, and `restore()`'s round trip through
     `decode_popoto_model_hashmap` depends on it coming back byte-for-byte.
  4. Existing `$TOMB:*` data in any deployment would be orphaned with **no
     migration path**, and #649 supplies none.
- **Confidence**: high.
- **Impact if false**: n/a — this is the load-bearing finding for Decision 1.

### spike-2: Is `OBJECT IDLETIME` actually unavailable on Valkey?
- **Assumption**: the issue's claim that it is Redis-only.
- **Method**: web-research + local probe (see Research).
- **Result**: **Refuted.** Core command on Valkey since 2.2.3. The real
  portability hazard is `maxmemory-policy *lfu`, which is a *configuration*
  condition identical on both servers, and which the existing bare
  `except Exception: pass` at L310 already absorbs.
- **Confidence**: high.
- **Impact if false**: if a future Valkey drops it, `Model.idle_seconds()`
  returns `None` on the same code path — the contract is already `Optional`.

### spike-3: Do `count()`/`members()` resolve the same sorted-set key the recipe uses?
- **Assumption**: a new `SortedFieldMixin.score()` can mirror its siblings and
  still be wire-identical at site 2.
- **Method**: code-read (`sorted_field_mixin.py:420-548`, `field.py:581`).
- **Result**: **Conditionally.** `count()`/`members()` use
  `get_partitioned_sortedset_db_key`, which is
  `get_special_use_field_db_key` **plus** one segment per `partition_by` field.
  The recipe (L330) uses the bare `get_special_use_field_db_key`. When
  `partition_by` is empty — every test in the repo, and the documented
  `MemoryLifecycle` usage — the two are **byte-identical**. When it is
  non-empty they diverge, and the recipe's current key is the unpartitioned
  base, which by construction can never contain the member, so its `ZSCORE`
  always returns `None` and the code falls through to the attribute read.
- **Confidence**: high.
- **Impact if false**: none; this is a measured property, and Risk 3 records
  the divergence explicitly.

### spike-4: Does `popoto.batch()` produce the same wire as `POPOTO_REDIS_DB.pipeline()`?
- **Assumption**: the three `pipeline()` sites can move to `batch()` with no
  wire change.
- **Method**: code-read (`src/popoto/batch.py:40-54`).
- **Result**: **Yes.** `batch(transaction=True)` is literally
  `POPOTO_REDIS_DB.pipeline(transaction=transaction)`, and
  `Pipeline.__init__` defaults `transaction=True`. Same object, same
  `MULTI`/`EXEC` framing, same shared pool.
- **Confidence**: high.

### spike-5: Is there already a field/model helper for a partial hash read?
- **Assumption**: `Model.load_fields` may already exist under another name.
- **Method**: code-read / grep across `src/popoto/models/` and `src/popoto/fields/`.
- **Result**: **No.** `Model.load()` and `Query.get()` both do a full `HGETALL`
  and full deserialization. `Model.exists()` (#644) is the only narrow-read
  primitive and it answers a different question. Nothing reads a named subset.
- **Confidence**: high.

## Data Flow

Two independent flows, which is why the site table splits cleanly in two.

**A. Assessment reads** (sites 1, 2, 15) — `tick()` → `_tick_pass()` →
`_should_forget(record)` → `_get_idle_seconds` → `_get_age_seconds`
(**site 1**) and `_get_importance_score` (**site 2**); then, immediately before
tombstoning, the re-check-tier guard re-reads the live hash (**site 15**) and
decodes it with `decode_lazy_field`. Each is a single command against a key the
model or field layer already knows how to name.

**B. The tombstone store** (sites 3–14) — `tombstone(record)` reads the live
hash verbatim (**site 3**), packs a `Tombstone` plus a `payload` into one
msgpack blob, writes it to `$TOMB:{M}:data` and indexes it by death time in
`$TOMB:{M}:index` in one transaction (**site 4**), then calls
`record.delete()`, then sweeps retention (**sites 5–7**). Reads
(`tombstone_count`, `list_tombstones`, `get_tombstone`, `restore`) hit sites
8–12; removals (`purge_tombstone`, `purge_all_tombstones`) hit 13–14.
`restore()` re-encodes the archived payload back to bytes keys and hands it to
`decode_popoto_model_hashmap`, so the payload must survive the round trip
**verbatim** — this is why site 3 cannot become a decoded read.

## Appetite

**Medium.** One new model-layer method pair, one new field-mixin classmethod,
one new field-layer store class that is a relocation rather than a redesign,
15 call-site swaps, and a 6-path parity matrix.

**One PR, not two.** The split contemplated by #649 was conditioned on "if the
tombstone model grows large". Decision 1 rejects the model, so the tombstone
half is a *move* of ~120 existing lines into a class, not new design. Splitting
would mean running the parity matrix twice over the same file and eating a
rebase between two PRs that touch the same 200 lines, for no reviewability
gain. The issue's own contract also says "One PR, this recipe only."

## Prerequisites

Both landed in #644 and are verified present at the branch base:

- `Model.exists()` — `src/popoto/models/base.py:2019`
- `popoto.batch()` — `src/popoto/batch.py:40`

## Solution

### Decision 1 — the tombstone store is relocated, not modelled

**Reject** the `Tombstone`-as-Model conversion #649 floats. Argument, in order
of decisiveness:

1. **It cannot satisfy this PR's own acceptance contract.**
   `test_partial_tombstone_entry_is_dropped_not_inflated` writes directly into
   `$TOMB:{M}:data` through `_tombstone_keys()`. A model layout removes the
   hash it injects into, so the test must change — and "no existing test
   expectation may change" is the contract.
2. **The wire diff would be the opposite of empty**, at every one of sites
   4–14, in both command names and cardinality (see spike-1).
3. **There is no migration story**, and #649 does not supply one. Existing
   `$TOMB:*` data in any deployment would silently become unreachable —
   including the archived payloads that are the entire point of #491's
   reversibility guarantee.
4. **The archived payload is a foreign model's raw hash.** A popoto model field
   is the wrong container for another model's already-encoded bytes, and
   `restore()`'s correctness depends on byte-for-byte survival.
5. **The `$TOMB` namespace is load-bearing.** #491 put it outside the model
   keyspace precisely so "no query, index scan, or key-set walk can ever
   surface a tombstoned record" (module docstring, L83-85). A real model gets a
   class key-set and participates in `Model.query.all()`. That is a *weakening*
   of the property `test_tombstoned_record_excluded_from_all_retrieval_modes`
   exists to protect.

Instead, follow the shape #653 set for `TD_UPDATE_LUA`: move the knowledge into
the field layer and leave a compatibility shim.

**New: `src/popoto/fields/tombstone_store.py`**, holding

- the `Tombstone` dataclass (moved verbatim),
- `_TOMBSTONE_FIELDS` / `_TOMBSTONE_REQUIRED_FIELDS`,
- `_unpack_tombstone_entry` / `_tombstone_from_entry` (moved verbatim),
- `TOMBSTONE_KEY_PREFIX`,
- `class TombstoneStore:` constructed with a `model_class`, owning both keys
  and every command against them.

`TombstoneStore` API (each method is the exact read/write the recipe was
doing, per the #654 precedent — not the nearest generic API):

| Method | Wire, unchanged |
|---|---|
| `keys() -> tuple[str, str]` | none |
| `archive(redis_key, entry, at) -> None` | `MULTI` / `HSET` / `ZADD` / `EXEC` |
| `count() -> int` | `ZCARD` |
| `oldest_keys(n) -> list[str]` | `ZRANGE key 0 n-1` |
| `newest_keys(stop) -> list[str]` | `ZREVRANGE key 0 stop` |
| `evict(keys) -> None` | `MULTI` / `HDEL` / `ZREM` / `EXEC` |
| `get_entry(redis_key) -> dict \| None` | `HGET` |
| `get_entries(keys) -> list` | `HMGET` |
| `purge(redis_key) -> bool` | `MULTI` / `HDEL` / `ZREM` / `EXEC` |
| `purge_all() -> None` | `DEL data index` |

All three transactions open with `popoto.batch()` (spike-4: same object, same
framing).

**Compatibility, so no test expectation moves:**

- `MemoryLifecycle._tombstone_keys()` stays, delegating to `store.keys()`.
  `test_partial_tombstone_entry_is_dropped_not_inflated` keeps passing
  unmodified.
- `Tombstone`, `TOMBSTONE_KEY_PREFIX`, `_unpack_tombstone_entry`, and
  `_tombstone_from_entry` are re-exported from `memory_lifecycle` (real
  re-exports — the same objects, asserted by identity in a test, per #653's
  Risk 5).
- The `logger` used for the "missing required keys" warning stays
  `POPOTO.MemoryLifecycle`, because
  `test_partial_tombstone_entry_is_dropped_not_inflated` asserts on
  `caplog.at_level(..., logger="POPOTO.MemoryLifecycle")`. The moved
  `_unpack_tombstone_entry` therefore keeps logging to that named logger, not
  to a new module logger. **This is the single easiest way to break that test
  and must be explicit in the code.**
- Every `try`/`except` and its warning text stay in `MemoryLifecycle`, not in
  the store — failure *policy* is the lifecycle's ("a failed sweep evicts
  nothing and the tick continues"), exactly as #654 kept the traversal's
  `except` in the traversal.

### Decision 2 — `OBJECT IDLETIME` stays, relocated to `Model.idle_seconds()`

**The argument that carries the weight is structural, not documentary.** L305-311
is `try: ... except Exception: pass`, falling through to `return 0.0`. On a
server that refused the command — for any reason: LFU policy, an unsupported
fork, a future removal — the site already degrades silently to `0.0`. This PR
relocates that structure **unchanged**, so *Valkey risk on this branch is
identical to Valkey risk on main*. That conclusion does not depend on any
external citation being right.

The citation is good supporting detail and belongs in the PR body, but must be
stated for what it is: `OBJECT IDLETIME` is a core Valkey command since 2.2.3,
not a module command (spike-2), so there is no constraint to satisfy and
nothing to drop. Neither the local probe (a Redis server) nor the green Valkey
CI job (which never executes L307) is evidence about Valkey — see Research.

**The flip side is worth stating too.** That same bare `except Exception: pass`
means that if the command *were* unsupported, the fallback would be silently
dead and every affected record would report `age = 0.0` with nobody the wiser.
`Optional[float]` is a genuine improvement at the boundary, because `None` and
`0.0` stop being the same answer. But the improvement stops at the boundary:
**the recipe's call site must still collapse `None` to `0.0`**, because
`_get_age_seconds` returns `float` and feeds `age >= PROMOTION_MIN_AGE_SECONDS`.
Propagating `None` outward would be a behavior change. The distinction is now
available to a future caller that wants it; this caller deliberately discards
it.

Neither option #649 offers is right:

- **Dropping the fallback** is a behavior change, not a relocation. Models with
  no `created_at`/`created` field would get `age = 0.0` instead of an idle
  time, which flips `_default_should_promote`'s `age >= PROMOTION_MIN_AGE_SECONDS`
  from possibly-true to always-false. That is precisely the kind of change the
  parity contract forbids.
- **`AccessTrackerMixin.last_access(record)`** is the wrong home. This call
  site is the fallback for models that *do not* have `AccessTrackerMixin` —
  a model that has it already exposes `last_accessed` and never reaches L307.
  Hanging the fallback off the mixin makes it unreachable from the only place
  that needs it.

So: **`Model.idle_seconds(self) -> Optional[float]`**, an instance method on
`Model`. It resolves the key exactly as L306 does
(`self._redis_key or self.db_key.redis_key`), issues the same
`OBJECT IDLETIME`, and returns `None` when the server declines — LFU policy, a
missing key, or a server without the command. The recipe keeps its own
`try`/`except → 0.0`.

The name is `idle_seconds`, not `age_seconds`: the command measures time since
last *access*, and the recipe's use of it as an age proxy is a pre-existing
approximation (documented at L289-292). Relocating it must not launder that
into a stronger claim, so the method's docstring says what it measures and the
recipe's docstring keeps saying it is a proxy. **Fixing that approximation is
out of scope** (No-Go 3).

### Decision 3 — two partial-load methods, split by decode

The partial-load sites are legitimate (#649 is right) and must not become full
loads. But they are not one method: site 3 needs the hash **verbatim** for
archival, site 15 needs it **decoded**.

- **`Model.load_fields(redis_key, *names) -> dict[str, Any]`** — decoded.
  Requires at least one name. **Issues `HGET` for exactly one name and `HMGET`
  for more than one.** This is a **parity requirement, not an optimization**:
  site 15 is an `HGET` today, and emitting `HMGET` there would break the wire
  diff. It must carry an inline comment saying exactly that, or a later cleanup
  pass will "simplify" it to always-`HMGET` and silently break parity. Values
  go through `decode_lazy_field`, which removes that
  import from the recipe. Missing fields are absent from the returned dict
  (so `"tier" not in result` distinguishes "key gone" from "tier is None"),
  preserving the L1122 `if raw_tier is None` guard's meaning.
- **`Model.load_raw_hash(redis_key) -> dict[bytes, bytes]`** — one `HGETALL`,
  no decode, keys and values exactly as Redis returned them. Site 3 only.
  `restore()`'s round trip through `decode_popoto_model_hashmap` requires
  byte-for-byte survival, so a decoded variant here would be a correctness bug,
  not a style choice.

### Decision 4 — `SortedFieldMixin.score()`

New classmethod, mirroring the `count()`/`members()` signature, plus one
parameter:

```python
SortedFieldMixin.score(model_instance, field_name, partitioned=True) -> Optional[float]
```

One `ZSCORE`, `None` for a missing member. `partitioned=True` (the default,
matching `count()`/`members()`) resolves via `get_partitioned_sortedset_db_key`;
`partitioned=False` resolves via the bare `get_special_use_field_db_key`.

**The recipe passes `partitioned=False`**, reproducing L330's current key
exactly. This PR is therefore byte-identical at site 2 for *every* field,
partitioned or not — there is no deliberate divergence anywhere in this PR.

`partitioned` is a real parameter rather than a hardcoded old key so the
follow-up fix is a one-line call-site change.

**Why not fix it here** (this reverses the author's initial recommendation;
decided by the supervisor):

- **It is the third instance of a defect class the repo has already decided
  how to handle.** #474 (*"RetrievalQuality score proxy ignores partition_by →
  0.0 scores for partitioned models"*, closed) is the same defect from the same
  cause, and it was handled as its own issue with its own tests, not as a rider
  on unrelated work. `context_assembler.py:589-596` records the story in prose:
  reading the base key *"returned `None` for every partitioned record and
  silently collapsed every metacognitive signal to its degenerate value (issue
  #474)"*.
- **It would hole the parity oracle.** An empty six-path diff is only worth
  something if it means *nothing changed*. One agreed exception turns every
  future reading into "empty except the bit we ignore" — and because no test
  exercises the partitioned path, the diff would not show the change anyway, so
  the matrix could not even confirm the fix works.
- **It is a silent behavior change for users who cannot see it coming.** The
  same `context_assembler` docstring notes that agent-memory sorted fields
  *"almost always declare `partition_by`"*, so the affected population is not
  niche. Those users would move from the attribute read to a real `ZSCORE`,
  changing retention and forgetting decisions — inside a PR whose stated
  contract is that nothing changes.
- **"Revert in one line if the reviewer objects" inverts the burden.** In a
  no-behavior-change PR the default is no change; the fix must argue its way
  in, which is what its own issue is for.

The follow-up issue is **#658**, filed before this PR opens (Task 0), and the
`partitioned=False` call site carries a comment naming both #474 and that
issue number. A fix described only in a PR body evaporates when the PR merges.

### Flow

```
_get_age_seconds(record)      -> record.idle_seconds()          [OBJECT IDLETIME]
_get_importance_score(record) -> SortedFieldMixin.score(...)    [ZSCORE]
_tick_pass() guard            -> type(record).load_fields(k, tier_field)  [HGET]
tombstone()                   -> type(record).load_raw_hash(k)  [HGETALL]
                              -> store.archive(...)             [MULTI HSET ZADD EXEC]
_enforce_tombstone_retention  -> store.count/oldest_keys/evict  [ZCARD ZRANGE MULTI HDEL ZREM EXEC]
tombstone_count               -> store.count()                  [ZCARD]
list_tombstones               -> store.newest_keys/get_entries   [ZREVRANGE HMGET]
get_tombstone / restore       -> store.get_entry()               [HGET]
purge_tombstone               -> store.purge()                   [MULTI HDEL ZREM EXEC]
purge_all_tombstones          -> store.purge_all()               [DEL]
```

After the swap, `memory_lifecycle.py` imports neither `POPOTO_REDIS_DB` nor
`decode_lazy_field`.

### Technical Approach

1. `src/popoto/models/base.py` — add `idle_seconds()`, `load_fields()`,
   `load_raw_hash()`.
2. `src/popoto/fields/sorted_field_mixin.py` — add `score()` beside
   `count()`/`members()`, resolving the client attribute at call time (the same
   comment `members()` carries at L541-542, so test spies keep intercepting).
3. `src/popoto/fields/tombstone_store.py` — new; receives the moved dataclass,
   helpers, prefix, and the new `TombstoneStore` class.
4. `src/popoto/recipes/memory_lifecycle.py` — construct
   `self._tombstone_store = TombstoneStore(model_class)` in `__init__`; swap
   all 15 sites; re-export the moved names; keep `_tombstone_keys()` as a shim;
   drop the `POPOTO_REDIS_DB` and `decode_lazy_field` imports.
5. `_sync()` and `_decoded_members()` move with the commands they narrow — the
   store owns the zset decode, so `_decoded_members` moves to
   `tombstone_store.py`. `_sync` is needed by both halves; keep one copy in
   `tombstone_store.py` and re-export it, rather than duplicating it.
6. `src/popoto/__init__.py` — export `Tombstone` and `TombstoneStore` if and
   only if the sibling field modules are exported there; otherwise leave the
   import path as `popoto.fields.tombstone_store` (check `td_value_field`'s
   precedent from #653 and match it).

## Test Impact

**No existing test expectation may change.** `git diff main...HEAD -- tests/`
must be additions only.

The 30 existing tests in `tests/test_memory_lifecycle.py` are the oracle for
behavior. They are *not* an oracle for the wire, so the wire gets its own
oracle (below).

New tests:

- `tests/test_model_partial_load.py` — `load_fields` with one name emits
  `HGET`; with several emits `HMGET`; a missing field is absent from the dict;
  values are decoded; `load_raw_hash` returns bytes keys and undecoded values
  and round-trips through `decode_popoto_model_hashmap`; `idle_seconds()`
  returns a float on a live key and `None` for a missing key.
- `tests/test_sorted_field_score.py` — `score()` returns the member's score,
  `None` for a member not in the index, and resolves the partitioned key for a
  `partition_by` field.
- `tests/test_tombstone_store.py` — each store method against a real Redis:
  key naming, the `MULTI`/`EXEC` framing of the three transactions, eviction
  order (oldest first), `get_entries` preserving argument order including
  `None` holes, `purge` returning `False` for an absent key, and the re-export
  identity assertions (`memory_lifecycle.Tombstone is tombstone_store.Tombstone`).
- `tests/test_memory_lifecycle.py` — additions only: an assertion that the
  module no longer imports `POPOTO_REDIS_DB`, and that
  `_tombstone_keys()` still returns the `$TOMB:{Model}:{data,index}` pair.

**Parity matrix — the main deliverable.** A spy wrapping
`POPOTO_REDIS_DB.execute_command`, `Pipeline.execute_command` and
`Pipeline.immediate_execute_command` records `(name, args)` for every call,
run against a detached `main` worktree and against this branch, over six fixed
paths. AutoKey UUIDs are normalized before diffing. Every diff must be empty.

| Path | Sites exercised |
|---|---|
| P1 `tick()` with a promotion | 1, 2 |
| P2 `tick()` with a forget → tombstone | 1, 2, 15, 3, 4, 5 |
| P3 `tick()` forcing retention eviction (limit exceeded) | 5, 6, 7 |
| P4 `tombstone_count()` + `list_tombstones()` + `get_tombstone()` | 8, 9, 10, 11 |
| P5 `restore()` | 12 (+ `save()` traffic) |
| P6 `purge_tombstone()` + `purge_all_tombstones()` | 13, 14 |

Capture script and both outputs land in
`scratchpad/sdlc-649-pr{N}-parity-*` (untracked), with the diff quoted in the
PR body.

## Rabbit Holes

- **Redesigning the tombstone entry format.** The msgpack blob mixes the
  `Tombstone` dataclass fields with a `payload` key. It is not elegant. It is
  also the on-disk format #494 will read. Out of scope.
- **Fixing `_get_age_seconds` to actually measure age.** Using idle time as an
  age proxy is wrong and pre-existing. Relocating it is in scope; correcting it
  is a behavior change.
- **Making `Model.load_fields` accept `DB_key` / KeyField kwargs** the way
  `exists()` does. Tempting for symmetry; nothing in this PR needs it, and the
  string-vs-`DB_key` short-circuit trap #644 documented is a real cost to
  re-litigate. Ship `redis_key`-only.
- **Generalizing `TombstoneStore` into a reusable "auxiliary keyspace" base
  class.** There is exactly one instance of the pattern. One is not a pattern.
- **Touching `context_assembler.py`.** It has its own direct-Redis sites and is
  a different issue in the #630 series.

## Risks

### Risk 1 — the logger name breaks `test_partial_tombstone_entry_is_dropped_not_inflated`
Moving `_unpack_tombstone_entry` to a new module and letting it take that
module's logger changes the logger name, and the test asserts on
`caplog.at_level(logging.WARNING, logger="POPOTO.MemoryLifecycle")`. Mitigation:
the store module explicitly binds `logging.getLogger("POPOTO.MemoryLifecycle")`
with a comment saying why. Verified by running that test specifically.

### Risk 2 — `HMGET`-for-one breaks parity at site 15
Covered by design (Decision 3) and by an explicit test asserting the command
name for a single-name call. Caught by P2 in the parity matrix regardless.

### Risk 3 — the partitioned-sorted-set defect is preserved, deliberately
For an importance field declared with `partition_by`, site 2 reads the
unpartitioned base key, which by construction cannot contain the member, so
`ZSCORE` always returns `None` and `_get_importance_score` silently falls
through to the attribute read. This PR **preserves that defect exactly** by
passing `partitioned=False` (Decision 4), so there is no behavior change and no
divergence in the parity matrix.

The residual risk is that the defect is now *also* reachable through a new
public API's non-default branch, which could read as endorsement. Mitigation:
the call-site comment names #474 and #658, `score()`'s docstring
states that `partitioned=False` exists only for byte-parity with a known
defect, and #658 is filed before this PR opens.

### Risk 4 — `ruff` F401 on the now-unused imports
Dropping `POPOTO_REDIS_DB` and `decode_lazy_field` from the recipe while
re-exporting `Tombstone` et al. can trip F401 on the re-exports. Mitigation:
re-export through `__all__` (the shape #653 used), and run `ruff check src/`
before the PR opens.

### Risk 5 — a re-export that is not a real re-export
If `memory_lifecycle.Tombstone` ends up as a copy rather than the same object,
`isinstance` checks in downstream code and in `restore()` silently diverge.
Mitigation: identity assertions in `tests/test_tombstone_store.py`.

### Risk 6 — import cycle
`fields/tombstone_store.py` importing from `models/` while `models/base.py`
imports fields. Mitigation: the store imports only `msgpack`, `logging`, and
`popoto.batch`; it takes `model_class` as a parameter and never imports a model
module. `batch.py` imports only `redis_db`.

### Risk 7 — mypy ratchet
New code in `models/base.py` touching `POPOTO_REDIS_DB` inherits the
`Awaitable[T] | T` union problem the recipe's `_sync()` exists to narrow. New
errors would raise the count above baseline. Mitigation: measure
`scripts/mypy_ratchet.py` before opening the PR; baseline is **1042** measured
under mypy 2.3.1 / redis-py 8.1.0 / Python 3.12, and this worktree's venv
reproduces that environment exactly (verified).

### Risk 8 — worktree phantom failures
DB 15 contention across concurrent lanes. Mitigation: `POPOTO_TEST_DB=11` for
this lane, and `REDIS_URL=redis://localhost:6379/11` set before `import popoto`
in any ad-hoc script. This lane's recipe performs `delete()` and eviction, so a
misbound script destroys live data rather than merely dirtying it — the parity
capture script is derived from `scripts/scratch_repro.py`.

## Race Conditions

None introduced. The three transactions keep `MULTI`/`EXEC` framing (spike-4),
so the two-key `$TOMB` atomicity contract is unchanged. The archive-before-delete
ordering in `tombstone()` (L880-881: "archive first so a crash between the two
steps loses nothing") and the re-check-tier guard's read-immediately-before-delete
placement (L1111-1119) are both preserved exactly; neither gains nor loses a
window.

## No-Gos (Out of Scope)

1. **Do not close #630.** #648 is in flight. The PR body carries `Closes #649`
   and no closing keyword for the umbrella.
1b. **Do not propagate `idle_seconds()`'s `None` outward.** The recipe's call
   site collapses `None` to `0.0`, preserving `_get_age_seconds`'s `float`
   return. The richer signature is for future callers, not this one.
2. **Do not convert `Tombstone` to a Model** (Decision 1).
3. **Do not fix `_get_age_seconds`'s idle-time-as-age approximation.**
4. **Do not change any existing test expectation.** Additions only.
5. **Do not touch `context_assembler.py`** or any other recipe.
6. **No Redis module commands.** Core commands only; `OBJECT IDLETIME` is core
   on both servers (spike-2).
7. **Do not widen `Model.load_fields` to accept `DB_key`/kwargs.**
8. **Do not fix the partitioned-sorted-set key at site 2.** Preserved via
   `partitioned=False`; tracked as #658 (Decision 4).

## Documentation

### Feature Documentation
- `docs/features/memory-lifecycle.md` (if present) — note that the tombstone
  store is now `popoto.fields.tombstone_store.TombstoneStore` and that the
  `$TOMB:{Model}:{data,index}` layout is unchanged.
- `docs/query.md` — add "Read Named Fields Without Loading" next to the
  "Check Existence Without Loading" section #644 added, covering `load_fields`,
  `load_raw_hash`, and `idle_seconds` with the LFU caveat.
- `CHANGELOG.md` — `[Unreleased]` entries for `Model.load_fields`,
  `Model.load_raw_hash`, `Model.idle_seconds`, `SortedFieldMixin.score`, and
  `TombstoneStore`.
- `mkdocs.yml` — only if a new page is added.

### Inline Documentation
- The `logging.getLogger("POPOTO.MemoryLifecycle")` binding in
  `tombstone_store.py` carries a comment naming the test it protects.
- `load_fields`'s docstring states the `HGET`-for-one rule and why.
- `idle_seconds`'s docstring states that it measures time since last access,
  not age, and names the LFU caveat.
- `score`'s docstring notes it resolves the partitioned key, as `count`/`members` do.

## Success Criteria

1. `grep -n "POPOTO_REDIS_DB" src/popoto/recipes/memory_lifecycle.py` → no output.
2. All six parity paths produce an **empty diff** base-vs-branch after UUID
   normalization.
3. `git diff main...HEAD -- tests/` shows **zero deletions** in
   `tests/test_memory_lifecycle.py`.
4. `pytest tests/test_memory_lifecycle.py tests/test_tombstone_store.py tests/test_model_partial_load.py tests/test_sorted_field_score.py tests/test_batch.py tests/test_model_exists.py` — all pass.
5. `ruff check src/` exits 0; `black --check src/ tests/` clean.
6. `scripts/mypy_ratchet.py` at or below **1042** (mypy 2.3.1 / redis-py 8.1.0 /
   Python 3.12).
7. Both CI jobs green, including the Valkey job.
8. PR body carries `Closes #649` and **no** closing keyword for #630.

## Step by Step Tasks

### 0. File the partition-key follow-up issue (before the PR opens) — DONE: #658
- Title the defect at `memory_lifecycle.py:330`, cite #474 and
  `context_assembler.py:589-596` as prior art, state that no test covers the
  path today so the fix needs one, and note the fix is a one-line change of
  `partitioned=False` to `partitioned=True` at the call site.

### 1. Model-layer methods
- Add `Model.idle_seconds()`, `Model.load_fields(redis_key, *names)`, and
  `Model.load_raw_hash(redis_key)` to `src/popoto/models/base.py`, beside
  `exists()`.
- `load_fields` emits `HGET` for exactly one name, `HMGET` for more.

### 2. Sorted-field score reader
- Add `SortedFieldMixin.score(model_instance, field_name)` to
  `src/popoto/fields/sorted_field_mixin.py`, beside `count()`/`members()`,
  using `get_partitioned_sortedset_db_key` and resolving the client attribute
  at call time.

### 3. Tombstone store module
- Create `src/popoto/fields/tombstone_store.py`: move `Tombstone`,
  `TOMBSTONE_KEY_PREFIX`, `_TOMBSTONE_FIELDS`, `_TOMBSTONE_REQUIRED_FIELDS`,
  `_unpack_tombstone_entry`, `_tombstone_from_entry`, `_sync`,
  `_decoded_members` verbatim; bind the `POPOTO.MemoryLifecycle` logger with
  the explanatory comment; add `class TombstoneStore` with the ten methods,
  using `popoto.batch()` for the three transactions.

### 4. Recipe swap
- Construct the store in `MemoryLifecycle.__init__`; swap all 15 sites; keep
  `_tombstone_keys()` as a delegating shim; keep every `try`/`except` and
  warning string in the recipe; re-export the moved names via `__all__`; drop
  the `POPOTO_REDIS_DB` and `decode_lazy_field` imports.

### 5. Tests
- Add `tests/test_model_partial_load.py`, `tests/test_sorted_field_score.py`,
  `tests/test_tombstone_store.py`, plus additions-only entries in
  `tests/test_memory_lifecycle.py`.

### 6. Parity validation
- Write the spy capture script from `scripts/scratch_repro.py` (with
  `REDIS_URL=redis://localhost:6379/11` set before `import popoto`), run all
  six paths against a detached `main` worktree and this branch, normalize
  AutoKey UUIDs, diff. Store under `scratchpad/sdlc-649-pr{N}-parity-*`.

### 7. Gates
- `ruff`, `black`, `mypy_ratchet`, the six named test files.

### 8. Documentation
- `docs/query.md`, `docs/features/memory-lifecycle.md`, `CHANGELOG.md`.

### 9. Pull request
- Body carries `Closes #649`, the parity matrix with its empty diffs, the
  environment string, and Risk 3 flagged explicitly for the reviewer.

## Open Questions

1. ~~**Risk 3 — take the partitioned-key fix, or preserve the broken
   behavior?**~~ **Resolved by the supervisor: preserve it.** Pass
   `partitioned=False`, keep `partitioned` as a real parameter, and file the
   fix as its own issue before the PR opens. Rationale recorded under
   Decision 4.
2. **Where should `Tombstone` / `TombstoneStore` be exported from?** Match
   whatever `td_value_field` did in #653 — resolved by inspection during build,
   not a design question.
</content>
