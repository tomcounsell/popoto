---
status: Planning
type: bug
appetite: Large
owner: valorengels
created: 2026-08-07
tracking: https://github.com/tomcounsell/popoto/issues/537
also_tracking: https://github.com/tomcounsell/popoto/issues/538
last_comment_id:
---

# Canonical datetime KeyField identity, and the one migration that repairs it

## Problem

`datetime` is a valid `KeyField` type (`key_field_mixin.py:79`), and a row's Redis
identity is derived from `str(value)`. A `datetime`'s `str()` carries the UTC offset when
the value is aware and omits it when the value is naive, so **the row's identity depends on
how the value happened to decode**, not on the instant it represents. Two open issues are
the same defect seen from opposite sides of the 1.8.2 boundary:

- **#538 (past tense)**: before 1.8.2 an aware value reloaded naive, so a re-save derived a
  *different* key, wrote a second hash, and orphaned the first. 1.8.2 stopped new
  duplicates and shipped no way to clean up existing ones.
- **#537 (future tense)**: 1.8.2 wanted to assume UTC for legacy offset-free rows. That
  assumption was written (commit `0342550`), reverted, and deferred, because stamping UTC
  on read shifts `str()` and creates *exactly the #538 duplication* on every legacy row.

They cannot be planned apart. #538 needs a key-rewrite migration; #537 is blocked on a
key-rewrite migration; and the rewrite target is the same in both cases. Planning them
separately would ship two incompatible key rewrites over the same rows.

**Current behavior** (reproduced on this branch's baseline, `df2b865`, Redis DB 8, Python
3.12.13 -- full transcripts in Spike Results):

```
legacy row on disk:  Ev:2026-08-07 12:00:00.123456
after adopting the legacy-as-UTC decode, load + save:
    Ev hash count: 2
    Ev:2026-08-07 12:00:00.123456           <- original, orphaned
    Ev:2026-08-07 12:00:00.123456+00:00     <- duplicate

rebuild_indexes() under the same decode:
    $KeyF:Ev:at:...123456+00:00 -> ['Ev:...123456+00:00']   <- indexes a key that does not exist
    $Class:Ev                   -> ['Ev:...123456']         <- the real hash, now unindexed
```

**Desired outcome:**

A `datetime` KeyField value's Redis identity is a function of the **instant**, not of the
value's awareness or of the interpreter's parsing behaviour. Two datetimes denoting the
same instant address one row. Legacy offset-free rows can be assumed UTC (#537) without
moving a key byte, because the key never carried the offset in the first place. A single
audit-then-migrate procedure moves an existing keyspace onto the canonical form and reports
the #538 duplicate pairs it finds on the way, rather than silently merging them.

## Freshness Check

**Baseline commit:** `df2b865` (origin/main at plan time; `45d6bfc` is the #525 fix, PR #533)
**Issues filed at:** #537 `2026-08-07T09:37:01Z`, #538 `2026-08-07T09:37:23Z`
**Disposition:** Minor drift -- one issue claim is understated and one PR #532 claim does
not match the merged code. Both are corrected below and neither changes the premise.

**File:line references re-verified:**

- `src/popoto/fields/key_field_mixin.py:79` -- `datetime` in `VALID_KEYFIELD_TYPES` -- still holds.
- `src/popoto/fields/key_field_mixin.py:246-262` -- the `$KeyF:` index set key is
  `DB_key(get_special_use_field_db_key(...), field_value)`, i.e. `str(value)`-derived --
  still holds.
- `src/popoto/models/db_key.py:268-276` -- `__str__` renders each partial as
  `clean(str(partial))` -- still holds (`clean`/`unclean` were rewritten by #533; the
  `str(partial)` call site was not).
- `src/popoto/models/base.py:664-669` -- **`Model.db_key` pre-stringifies key values**
  (`str(getattr(self, key_field_name, "None"))`) before handing them to `DB_key`. Not cited
  in either issue and load-bearing for the fix: canonicalizing inside `DB_key` alone
  changes the `$KeyF:` index key but leaves the primary hash key raw (spike-3).
- `src/popoto/models/encoding.py:497-540` (`_create_lazy_model`) -- **`_redis_key` is
  recomputed from the decoded KeyField values, not taken from the key the row was read
  from.** This is the precise mechanism by which duplication is silent: the instance's idea
  of where it came from follows the decode, so `save()`'s obsolete-key branch
  (`base.py:1353`) never fires and never deletes the original. Not named in either issue.
- `src/popoto/models/base.py:2863-3000` (`rebuild_indexes`) -- sets
  `instance._redis_key = redis_key_str` (the scanned key) but `KeyFieldMixin.on_save` indexes
  `model_instance.db_key.redis_key` (recomputed). #537 says rebuild "creates the divergence";
  measured, it is worse than that -- see spike-2.
- `src/popoto/models/encoding.py:110-113` -- **the merged decoder tries `fromisoformat`
  first and `strptime` second**, the opposite of what PR #532's description says is
  "load-bearing". Harmless today (both branches yield the same naive value) but decisive for
  #537: on 3.11+ `fromisoformat` parses `20260807T12:00:00.123456` (verified on 3.12.13),
  so hanging the UTC assumption off the `strptime` branch would make the assumption apply
  on 3.10 and silently not apply on 3.12.

**Cited sibling issues/PRs re-checked:**

- #521 / PR #532 -- merged `9eda780`, released in 1.8.2. The encoding fix this follows.
- #519, #421 -- closed. Establish naive-as-UTC for *scoring* and aware-UTC `auto_now`
  stamping. The canonical key form below is the same doctrine applied to identity.
- #525 / PR #533 -- merged `45d6bfc` after both issues were filed. It rewrote
  `DB_key.clean`/`unclean`. It does **not** touch `str(partial)`, so the premise is intact,
  and this plan must not touch the escape logic (it composes with it: canonicalize, then
  clean).
- #534 -- open, being fixed in parallel. See Architectural Impact for the interaction.
- #539 -- open (guard-main-push misfiring). Affects only how this plan document lands.

**Commits on main since the issues were filed touching referenced files:** `45d6bfc`
(`db_key.py`, #525) -- irrelevant to the `str(partial)` call site. `867d0b9`, `df2b865` --
lockfile and plan doc.

**Active plans in `docs/plans/` overlapping this area:**
`db_key_colon_escape_self_escaping.md` (Complete, #525) -- adjacent file, disjoint
concern. `atomic_index_maintenance_lua.md` -- audited `rebuild_indexes` for the
`IndexedField` pointer and concluded "rebuild is convergent even with a stale pointer";
that conclusion is about the pointer, and does not cover the recomputed-`db_key`
divergence measured here.

**Bug reproduced against current main:** yes, both halves, see Spike Results.

## Prior Art

- **#521 / PR #532** (merged, 1.8.2) -- made `datetime`/`time` encode with `isoformat()` so
  awareness round-trips. Succeeded at the *value* layer and explicitly declined the
  *identity* layer. Its CHANGELOG line "no offline migration is required, and none is
  possible" is true only because the assumption was dropped; #537 records that.
- **#519** (closed) -- made the sorted-set score a pure function of the stored value,
  treating naive as UTC. Established the doctrine this plan extends to keys, and is the
  reason the deferred assumption "bought nothing": scores were already identical either
  way.
- **#421 / PR #421** (merged, 1.7.1) -- `auto_now`/`auto_now_add` stamp aware UTC.
- **#476** (closed) -- the 1.8.0 `\x00idxset` in-hash pointer, forward-incompatible with
  pre-1.8.0 readers. The repo's canonical example of a write-format change shipped without
  a version boundary in the release note. Its lesson is applied in Documentation below.
- **#525 / PR #533** (merged) -- the immediately preceding change to key bytes. It got the
  version-boundary note right ("keys written by >= 1.8.3 are not decoded correctly by
  < 1.8.3; roll readers forward before writers"). This plan copies that wording pattern.

**No prior attempt has tried to change datetime key identity.** The one attempt in this
family (`0342550`) changed the *decode* and was reverted precisely because it moved keys.

## Why Previous Fixes Failed

| Prior Fix | What It Did | Why It Failed / Was Incomplete |
|-----------|-------------|-------------------------------|
| #519 | Made the sorted-set *score* a pure function of the stored value | Deliberately scoped to scoring; left the stored value lossy |
| #521 / PR #532 | Made the stored *value* preserve the offset | Left *identity* derived from `str(value)`, so the value fix could not be extended to legacy rows |
| `0342550` (reverted) | Assumed UTC for legacy offset-free rows | Changed `str()`, therefore changed the key, therefore duplicated every legacy row on next save. Reverted |

**Root cause pattern:** each fix corrected one projection of the datetime (score, then
value) while identity kept being derived from a *representation* (`str()`) rather than from
the *instant*. Until identity is canonicalized, every future change to datetime decoding
hits the same wall. This plan fixes the projection nobody has touched.

## Research

No relevant external findings -- this is internal key derivation with no external library
or API surface. Two interpreter-level facts were verified locally rather than assumed:

- `datetime.datetime.fromisoformat("20260807T12:00:00.123456")` **succeeds** on CPython
  3.12.13 (returns naive), and per PR #532's own note fails on the 3.10 `requires-python`
  floor. The decoder's try-order is therefore an interpreter-visible behaviour switch the
  moment a branch stops being value-neutral.
- `hypothesis` is not a dependency. Exhaustive/seeded loops, not property-based testing.

## Spike Results

All spikes ran in this worktree's own venv (editable install verified resolving to
`.../agent-a70cdf2bdc5e5263e/src/popoto/__init__.py`), against Redis DB 8, CPython 3.12.13.
Prototype patches were monkeypatches in throwaway scripts; nothing was committed.

### spike-1: Does `save()` actually orphan, given the KeyMutationError guard?
- **Assumption**: "any `save()` writes a second hash" (#538).
- **Method**: prototype.
- **Finding**: **Confirmed, with an important refinement.** An *explicit* KeyField edit
  raises `KeyMutationError` (`base.py:1129-1144`) -- so the duplication is not reachable by
  ordinary key mutation. It is reachable only when the **decode** changes the value, because
  then `_saved_field_values` and the attribute agree (both hold the decoded value), the guard
  sees no change, and `_create_lazy_model` has already recomputed `_redis_key` from the
  decoded value so `save()`'s obsolete-key branch does not fire either. Two independent
  safety nets are both blinded by the same recomputation.
- **Confidence**: high.
- **Impact on plan**: adds Task 1 (identity provenance). Without it, any future decode change
  is silently duplicating again, canonical keys or not.

### spike-2: Reproduce both #537 claims on current main
- **Assumption**: "the legacy-as-UTC decode duplicates rows, and `rebuild_indexes()` makes
  it worse."
- **Method**: prototype -- wrote a genuine pre-1.8.2 row (legacy `%Y%m%dT%H:%M:%S.%f`
  bytes under the naive-derived key), then swapped in the reverted UTC-assuming decoder.
- **Finding**: **Both confirmed.** Load + save produced `Ev hash count: 2`, two `$KeyF:`
  sets with one member each, and both keys in `$Class:Ev`. `rebuild_indexes()` on a
  single-row legacy keyspace produced a *fully crossed* state: `$Class:Ev` holds the real
  key `Ev:...123456`, while `$KeyF:Ev:at:...123456+00:00` holds `Ev:...123456+00:00`, a key
  with no hash behind it. So `query.all()` returns the row and `query.filter(at=...)`
  returns a dangling key. #537 understates this: rebuild does not merely fail to repair,
  it leaves the index pointing at nothing.
- **Confidence**: high.
- **Impact on plan**: Task 4 is mandatory and must land before, not after, any decode
  change.

### spike-3: Is `DB_key.__str__` a sufficient insertion point for canonicalization?
- **Assumption**: "canonicalizing datetime partials inside `DB_key` covers every key
  derivation."
- **Method**: prototype -- monkeypatched `DB_key.__str__` only.
- **Finding**: **No.** The `$KeyF:` index key became canonical
  (`$KeyF:Ev:at:2026-08-07T12:00:00.123456Z`) but the primary hash key stayed raw
  (`Ev:2026-08-07 19:00:00.123456+07:00`), because `Model.db_key` (`base.py:664-669`)
  pre-`str()`s each value before constructing the `DB_key`. Half-canonicalization is worse
  than none: the index and the hash disagree.
- **Confidence**: high.
- **Impact on plan**: the change needs **two** insertion points, and an audit for any other
  site that pre-stringifies a key value.

### spike-4: Does full canonicalization actually collapse both issues?
- **Assumption**: "with canonical keys, the #537 decode change becomes key-invisible and
  #538's duplicate pairs collapse onto one target."
- **Method**: prototype -- spike-3's patch plus a `Model.db_key` that passes raw values.
- **Finding**: **Confirmed on every axis measured.**
  - Aware `+07:00` write, reload, re-save: one hash, one index entry, one class-set member,
    all keyed `Ev:2026-08-07T12:00:00.123456Z`.
  - `query.get()` succeeds for the aware `+07:00` value, the equivalent aware UTC value, and
    the equivalent naive UTC wall clock -- three representations, one identity.
  - Value fidelity is untouched: the reloaded instance still returns
    `tzinfo=timezone(timedelta(seconds=25200))`. The offset lives in the hash, not the key.
  - Legacy row + the UTC-assuming decode + load + save: `Ev hash count: 1`. No duplicate.
  - `rebuild_indexes()` afterwards: convergent, index and hash agree.
- **Confidence**: high (single-row scenarios; not a scale or concurrency test).
- **Impact on plan**: this is the design. It is also the argument for planning the two
  issues together: canonicalization is what makes #537 safe, and the migration onto it is
  what repairs #538.

## Data Flow

1. **Entry point**: a `datetime` value is assigned to a `KeyField`, or passed to
   `query.filter(field=value)`.
2. **`Model.db_key`** (`base.py:664`) collects KeyField values, sorted by field name.
   *Today* it `str()`s them here; after this change it passes the raw objects through.
3. **`DB_key.__str__`** (`db_key.py:268`) renders each partial. *After this change* a
   `datetime` partial is first canonicalized (UTC-normalized, fixed-width ISO, trailing
   `Z`), then passed to the unchanged `clean()`, then joined on `:`.
4. **`KeyFieldMixin.on_save`** (`key_field_mixin.py:246`) builds `$KeyF:Model:field:<value>`
   through the same `DB_key` path, so it inherits the canonical form for free, and adds the
   instance's key as a member.
5. **Storage**: the hash at the canonical key holds the field value encoded by
   `encoding.py`, which keeps the caller's exact offset (#521). Key and value are now
   deliberately different projections: the key is the instant, the value is the instant plus
   the offset the caller supplied.
6. **Read**: `_create_lazy_model` decodes; *after this change* it records the key it was
   read from as `_redis_key` instead of recomputing one.
7. **Re-save**: if the source key differs from the derived key (a pre-migration row), the
   existing obsolete-key branch renames rather than duplicates.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: `DB_key` partials may now be non-string objects for
  `Model.db_key`-constructed keys. Any consumer indexing into a `DB_key`'s partials and
  assuming `str` must be audited (Task 2).
- **Coupling**: adds one shared helper (`canonical_key_str`) used by `DB_key.__str__` and
  by `ModelOptions.compute_index_hash*` (`base.py:273`, `:291`, which hash `str(value)` for
  compound indexes and have the identical fragility). Centralizing *reduces* net coupling:
  today three call sites independently depend on `datetime.__str__`.
- **Data ownership**: unchanged. Redis remains the only store; the hash stays authoritative
  for the value.
- **Reversibility**: the code change is trivially revertible; the *keyspace* migration is
  not. Hence the deploy-level kill switch and the report-first migration ordering.
- **Interaction with #534** (being fixed in parallel, not in scope here): `IndexedFieldMixin`
  builds its value-set key the same `str(value)`-derived way
  (`indexed_field_mixin.py:270-273`), so `IndexedField(type=datetime)` sits on the same
  fragility. Two consequences, both benign: (a) once #534's fix makes such a field savable,
  it is born canonical for free, because it routes through `DB_key`; (b) no migration is
  owed for it, because a field that raised `TypeError` on every save cannot hold legacy
  data -- the same argument PR #532 used for `SortedField(type=time)`. The only coordination
  required is merge ordering, not design.

## Appetite

**Size:** Large

**Team:** Solo dev, PM, code reviewer

**Interactions:**
- PM check-ins: 2-3 (the collision semantics in Open Question 1, the reconciliation default
  in Open Question 2, and the release gating in Open Question 3 are all maintainer calls)
- Review rounds: 2+ (a breaking key-format change with a migration; #476's history says
  this gets a second look)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 8 ping` | Integration tests and migration rehearsals |
| Worktree-resolved editable install | `python -c "import popoto, pathlib, sys; sys.exit(0 if str(pathlib.Path(popoto.__file__).resolve()).startswith(str(pathlib.Path.cwd().resolve())) else 1)"` | CLAUDE.md worktree hazard 1 |
| Full extras installed | `python -c "import numpy, sentence_transformers"` | CLAUDE.md worktree hazard 2 (~95 tests deselect otherwise) |
| Non-UTC process timezone for the tz tests | `python -c "import time; assert time.timezone != 0"` or run under `TZ=Asia/Bangkok` | On a UTC box a dropped offset is indistinguishable from a preserved one (PR #532's precedent) |

## Solution

### Key Elements

- **`canonical_key_str(value)`** -- one helper deciding how any value becomes key bytes.
  For `datetime.datetime`: normalize to UTC (aware via `astimezone`, naive via `replace`,
  per #519/#421 doctrine), render `%Y-%m-%dT%H:%M:%S.%f` with a trailing `Z`. For every
  other type: `str(value)`, byte-identical to today.
- **Identity provenance** -- a loaded instance remembers the key it was read from, so a
  decode-vs-key divergence becomes a rename, never a duplicate.
- **`Model.audit_datetime_keys()`** -- read-only. Reports rows whose stored key is not
  canonical, and rows whose canonical target collides with another row's (the #538
  duplicate pairs), with enough per-row detail to choose.
- **`Model.migrate_datetime_keys()`** -- rewrites non-colliding rows onto the canonical key
  and rebuilds the derived indexes. **Refuses to touch a collision** unless the operator
  passes an explicit strategy.
- **`rebuild_indexes()` divergence guard** -- refuses to write an index entry for a row
  whose scanned key and derived key disagree, and points at the audit.
- **`POPOTO_DATETIME_KEY_LEGACY`** -- a deploy-level kill switch restoring pre-1.9.0 key
  bytes without a model-code edit, so a PyPI adopter can roll readers forward before
  migrating writers.
- **Legacy-as-UTC decode (#537)** -- adopted *last*, once keys can no longer move.

### Flow

Operator on 1.8.x with `KeyField(type=datetime)` rows:

Upgrade to 1.9.0 with `POPOTO_DATETIME_KEY_LEGACY=1` -> keys byte-identical, nothing moves
-> `Model.audit_datetime_keys()` -> read the report (non-canonical rows, collision pairs)
-> resolve collisions (operator's call; report names both hashes and their contents)
-> `Model.migrate_datetime_keys()` -> keys canonical, indexes rebuilt
-> re-run the audit, expect a clean report -> unset the kill switch -> done. Legacy rows
now read as aware UTC, and no key moved when that started.

An operator with no `KeyField(type=datetime)` anywhere does nothing: the audit reports zero
affected models and the kill switch is irrelevant.

### Technical Approach

**1. The canonical form.** UTC-normalized, fixed-width, offset-free, with a literal `Z`:
`2026-08-07T12:00:00.123456Z`. Four properties earn it:

- *Awareness-invariant*: the whole point. `12:00+07:00`, `05:00Z`, and the naive `05:00`
  (assumed UTC) all render identically, so decoding cannot move a key.
- *Fixed width*: `strftime("%H:%M:%S.%f")` always emits six microsecond digits, unlike
  `isoformat()`, which drops `.%f` when microseconds are zero. Fixed width keeps
  lexicographic key order equal to chronological order, which matters for `scan_keys`
  patterns and for humans reading `redis-cli --scan` output.
- *Distinguishable from the two legacy shapes*: legacy is `2026-08-07 12:00:00.123456`
  (space, no offset) or `2026-08-07 12:00:00.123456+00:00` (space, offset). Canonical is
  `T`-separated and `Z`-terminated. The audit can classify any stored key by shape alone,
  with no ambiguity and no need to load the hash.
- *`clean()`-compatible*: it contains colons and hyphens, which `DB_key.clean()` already
  escapes (`{&#58;}` and `/-`). Canonicalization runs strictly *before* `clean()` and does
  not touch #525's escape logic.

**2. Two insertion points, not one** (spike-3): `DB_key.__str__` canonicalizes each
non-`DB_key` partial before `clean()`, and `Model.db_key` stops pre-`str()`ing. Type
dispatch is on `isinstance(partial, datetime.datetime)`, so every other partial type is
byte-identical to today and `datetime.date`/`datetime.time` are untouched.

`datetime.date` has no offset to lose. `datetime.time` is deliberately excluded for PR
#532's reason: `%H:%M:%S.%f` and a naive `time.isoformat()` with microseconds are
byte-identical, so a legacy time is indistinguishable from a deliberately naive one, and an
*aware* time's `str()` already carries its offset stably. Neither has the bug, so neither is
a deferral.

**3. Identity provenance.** `decode_popoto_model_hashmap`/`_create_lazy_model` accept the
source Redis key and assign it to `_redis_key`, falling back to today's recomputation when
a caller has no key to give. Every loader that already knows the key (`Query`,
`DB_key.get_instance`, `rebuild_indexes`) passes it. This converts silent duplication into
`save()`'s existing rename path, which makes the migration *lazily self-healing* for rows
that get re-saved before the operator runs it -- and makes any future decode change safe by
construction.

**4. `rebuild_indexes()` stops making it worse.** After decoding, compare
`instance.db_key.redis_key` against the scanned key. If they differ, do not index the row:
count it, name it in the return value, and log a `WARNING` pointing at
`audit_datetime_keys()`. Rebuilding an index to point at a key that does not exist
(spike-2) is strictly worse than leaving the row unindexed and saying so.

**5. Compound indexes.** `ModelOptions.compute_index_hash` and
`compute_index_hash_from_values` (`base.py:273`, `:291`) hash `str(value)` and carry the
identical fragility for a `datetime` inside a `Meta.indexes` tuple. Route both through
`canonical_key_str`. This changes compound-index hashes for datetime members;
`rebuild_indexes()` already deletes and rebuilds composite indexes, so the migration covers
it with no extra step.

**6. The kill switch.** `POPOTO_DATETIME_KEY_LEGACY=1` (read once at import into
`Defaults`, matching the `DECAY_CONFIDENCE_MODULATION_ENABLED` precedent) makes
`canonical_key_str` fall back to `str(value)` for datetimes -- byte-identical to 1.8.2.
Default is **off** (canonicalization on), per the repo's default-on doctrine, with the
switch as the deploy-level escape hatch for adopters who cannot edit model code.

**7. The decode change (#537), last.** Only after 1-6 does the legacy branch stamp UTC. Two
requirements: the branch must be selected by an explicit **shape test** (an anchored regex
for `%Y%m%dT%H:%M:%S.%f`, or `strptime` attempted first) rather than by `fromisoformat`
raising, because `fromisoformat` parses that shape on 3.11+ and not on 3.10 -- once the two
branches disagree about awareness, try-order becomes an interpreter-visible behaviour
switch. And a post-1.8.2 *deliberately* naive value must stay naive: it is
`2026-08-07T12:00:00.123456` with date separators and can never match the legacy shape.

**8. Migration mechanics.** Expand-migrate-contract, per row, idempotent and resumable:
`DUMP`/`RESTORE` or `RENAME` the hash onto the canonical key (`RENAMENX` so a collision
fails loudly instead of clobbering), fix `$Class:` membership, then one
`rebuild_indexes()` at the end to re-derive `$KeyF:`, sorted, geo, `IndexedField` and
composite indexes from the moved hashes. Re-running after a crash is a no-op for already
canonical rows.

## Failure Path Test Strategy

### Exception Handling Coverage
- No bare `except Exception: pass` exists in the touched files
  (`db_key.py`, `base.py` db_key/rebuild paths, `encoding.py` decoders, `key_field_mixin.py`).
  The decoder's `except ValueError` is a real branch and is being replaced by an explicit
  shape test in Task 6; a test must assert that a genuinely malformed stored string raises
  rather than being silently coerced to a plausible datetime.
- The migration must not swallow a failed `RENAMENX`: a collision has to surface as a
  reported row, and a test asserts the report names both hashes.

### Empty/Invalid Input Handling
- `canonical_key_str(None)` must return `"None"`, byte-identical to today -- `KeyField(null=True)`
  and `_has_unstable_db_key`'s `"None"` placeholder both depend on it. Explicit test.
- Missing KeyField attribute: `Model.db_key`'s `"None"` fallback survives the
  de-stringification. Explicit test.
- `audit_datetime_keys()` on an empty keyspace, on a model with no datetime KeyField, and
  on a model whose rows are all already canonical: three tests, all returning a clean
  empty report rather than raising.
- A datetime with `microsecond == 0` and one with `fold=1`: both must render fixed-width
  and stably.

### Error State Rendering
- `rebuild_indexes()`'s divergence path must be observable: a `WARNING` on the
  `POPOTO.Model` logger naming the model and the count, plus the count in the return value.
  A test asserts the log record, not just the absence of a bad index write.
- `migrate_datetime_keys()` with no strategy on a colliding keyspace must return a report
  and change nothing. A test asserts both hashes still exist afterwards.

## Test Impact

- [ ] `tests/test_datetime_tzinfo_round_trip.py::test_legacy_keyfield_row_keeps_its_stored_key`
      -- REPLACE. It exists precisely to fail if someone reintroduces the assumption without
      doing this work (`#537`), and asserts
      `str(legacy) == "2026-08-07 12:00:00.123456"`. Its replacement must assert the new
      contract: a legacy row decodes aware UTC *and* its derived key is unchanged from the
      canonical key it was migrated to. Keep the docstring's history.
- [ ] `tests/test_datetime_tzinfo_round_trip.py::test_legacy_datetime_decodes_naive_not_assumed_utc`
      -- REPLACE. Inverts under Task 6.
- [ ] `tests/test_datetime_tzinfo_round_trip.py::test_datetime_keyfield_does_not_duplicate_on_resave`
      -- UPDATE. Behaviour is preserved; add the naive-and-aware-UTC-collapse case, which
      is new.
- [ ] `tests/test_datetime_tzinfo_round_trip.py::test_two_offsets_of_the_same_instant_no_longer_collide`
      -- UPDATE (docstring only). It asserts *encoded values* do not collide, which stays
      true. Add a sentence recording that their *keys* now deliberately do, so the next
      reader does not think the two statements conflict.
- [ ] `tests/test_datetime_tzinfo_round_trip.py::test_naive_stays_naive`,
      `test_aware_value_survives_a_real_save_and_load`, `test_resaving_a_reloaded_aware_value_does_not_drift`
      -- KEEP unchanged. They pin value fidelity, which this plan must not disturb; they are
      the regression net for the "key is the instant, value keeps the offset" split.
- [ ] `tests/test_migrations.py` -- UPDATE: add coverage for the new cookbook recipe.
- No other test file references `KeyField(type=datetime)` (verified by grep across
  `tests/`, `src/`, `docs/`), so the blast radius on the existing suite is one file.
- No `xfail`/`pytest.xfail()` markers relate to this bug (the two `xfail` mentions in
  `tests/` are unrelated comments).

## Rabbit Holes

- **Generalizing canonicalization to every KeyField type.** `Decimal`, `float`, and `bool`
  all have `str()` quirks that could theoretically shift identity. None has a reported bug
  and each would need its own migration. `canonical_key_str` should be *shaped* to allow it
  later and dispatch only on `datetime` now.
- **Making `from_redis_key()` return typed values.** It returns strings today and will keep
  returning the canonical string. Making it reconstruct a `datetime` is a separate,
  tempting, unrelated project.
- **Prefix/range semantics on datetime keys.** Fixed-width canonical form makes
  `at__startswith="2026-08"` accidentally meaningful. Do not document or test it as a
  feature; `SortedField` is the supported way to range over time.
- **A generic key-rewrite framework.** The migration is datetime-specific on purpose.
  Recipes 7-9 in the cookbook already cover generic key moves.
- **Repairing inbound `Relationship` references automatically.** See Risk 3 -- detect and
  report, do not rewrite other models' hashes.
- **Online/zero-downtime migration.** The procedure assumes writers are quiesced or the
  kill switch is set. Building a dual-read/dual-write path for a feature with near-zero
  measured adoption is not worth it.

## Risks

### Risk 1: A keyspace is migrated while writers are live
**Impact:** A write landing on the old key after that row has been moved recreates an
orphan, and the operator believes the migration completed.
**Mitigation:** The documented procedure sets `POPOTO_DATETIME_KEY_LEGACY=1` for the whole
fleet first, so all writers agree on the old form; the migration is run; the switch is then
removed fleet-wide. `audit_datetime_keys()` is idempotent and cheap, and the docs instruct
re-running it after the switch is lifted to confirm zero non-canonical rows. The
self-healing rename from Task 1 repairs stragglers on their next save.

### Risk 2: Silent identity merges
**Impact:** Canonicalization deliberately collapses `12:00+07:00`, `05:00Z`, and naive
`05:00` onto one key. A deployment that treated a naive value and an aware UTC value as two
distinct rows loses one of them.
**Mitigation:** This is exactly what the audit's collision report is for, and why
`migrate_datetime_keys()` refuses collisions without an explicit strategy. It is also
consistent with #519, under which those rows already score identically -- the system has
treated them as the same instant since 1.7.x. Called out in the CHANGELOG under a
"read before upgrading" heading, with the audit command inline.

### Risk 3: A rewritten key orphans inbound references
**Impact:** `Relationship` fields store the target's `redis_key` as a value inside *another
model's* hash. Renaming a hash breaks every inbound reference, and `IndexedField` pointer
side keys (`_pointer_side_key(member_key, field_name)`) embed the member key and orphan too.
**Mitigation:** The audit scans model classes for `Relationship` fields targeting the model
being migrated and reports the inbound reference count *before* anything moves; if any
exist, `migrate_datetime_keys()` refuses without an explicit acknowledgement. Orphaned
`IndexedField` pointer side keys for moved members are deleted by the migration itself
(their names are derivable from the old key). Test coverage for both.

### Risk 4: Mixed-version deployment, the #476 failure mode
**Impact:** A 1.9.0 writer creates canonical keys that a 1.8.x reader cannot find, and the
symptom is an empty query result rather than an error -- the hardest shape to diagnose.
**Mitigation:** Name the version boundary explicitly in the CHANGELOG, copying #525's
wording: keys written by >= 1.9.0 for `KeyField(type=datetime)` values are not found by
< 1.9.0; roll readers forward before writers. The kill switch exists so that "roll readers
forward" can be done independently of "change key bytes".

### Risk 5: The change lands where near-zero deployments need it
**Impact:** Real-world exposure may be nil (#538's own scope check: no `KeyField(type=datetime)`
example anywhere in the docs, zero test coverage before #532), so the migration tooling
could be dead code carrying maintenance cost.
**Mitigation:** The tooling is small and is *forced* by the canonicalization -- shipping a
key-format change with only a prose recipe would be worse than not shipping it. It is
deliberately two methods plus a cookbook recipe, not a framework. Open Question 3 puts the
ship/defer call in front of the maintainer with the measurement attached.

## Race Conditions

### Race 1: Concurrent save during a row's rename
**Location:** `migrate_datetime_keys()`; `base.py:1353-1443` (obsolete-key branch).
**Trigger:** the migration `RENAME`s `Ev:old` to `Ev:new` while another process is
mid-`save()` on `Ev:old`.
**Data prerequisite:** the hash must exist at exactly one of the two keys when the writer's
pipeline executes.
**State prerequisite:** all writers agree on which form `canonical_key_str` produces.
**Mitigation:** the documented procedure quiesces writers or pins them with the kill switch,
so no writer is deriving the new form while the migration is producing it. `RENAMENX` fails
rather than clobbering, and the failure is reported. Detection is cheap and idempotent, so
the audit is re-run at the end.

### Race 2: Self-healing rename racing the migration
**Location:** Task 1's provenance change interacting with the migration.
**Trigger:** a live process loads a pre-migration row and saves it (renaming it to
canonical) at the same moment the migration renames it.
**Data prerequisite:** one of the two operations must observe the other's result.
**State prerequisite:** both must compute the same canonical target -- they do, by
construction, which is what makes this benign.
**Mitigation:** both converge on the same key, so the loser is a no-op `RENAMENX` failure
on a row that is already at its target. The migration must therefore treat "source key
missing, target key already canonical and present" as success, not as an error. Explicit
test.

### Race 3: `rebuild_indexes()` during migration
**Location:** `base.py:2898-2935` (it DELetes every index key before rebuilding).
**Trigger:** an operator runs `rebuild_indexes()` concurrently with the migration.
**Mitigation:** pre-existing hazard, already noted in the CHANGELOG's coordinated-step
caveat for #519. The divergence guard from Task 4 makes the outcome safe (rows are skipped
and reported) rather than corrupt. Documented, not locked.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #534] `IndexedField(type=datetime)`'s `TypeError` on save. Being fixed in
  parallel. This plan neither implements nor depends on that fix; it only records that the
  fixed path inherits canonical keys for free and owes no migration.
- [SEPARATE-SLUG #525] `DB_key.clean()`/`unclean()`'s escape logic. Shipped in PR #533 and
  not to be touched. Canonicalization composes with it by running strictly before `clean()`.
- [DESTRUCTIVE] Choosing which copy of a #538 duplicate pair survives. The two hashes can
  have diverged, so the library reports and the operator decides. `migrate_datetime_keys()`
  ships report-only for collisions; any `strategy=` argument is opt-in and never a default.
- [DESTRUCTIVE] Rewriting other models' hashes to repair inbound `Relationship` references
  to a renamed key. Detected and reported; the rewrite is the operator's, because a
  Relationship value is indistinguishable from an ordinary string field's content and a
  blind rewrite could corrupt unrelated data.
- [ORDERED] Removing `POPOTO_DATETIME_KEY_LEGACY` from the codebase. It can only be deleted
  a release after 1.9.0, once adopters have migrated. Not in this plan.
- [EXTERNAL] Running the migration on any real deployment. It moves production keys; only
  an operator can schedule that.

## Update System

No `/update` changes. `POPOTO_DATETIME_KEY_LEGACY` is read from the environment with a safe
default, so no config file has to be propagated. The migration steps for existing
installations are the whole point of the Documentation section below.

## Agent Integration

No agent integration required. This is ORM-internal key derivation with no tool or MCP
surface. The agent-memory recipes (`DefaultMemory`, `MemoryLifecycle`) declare no
`KeyField(type=datetime)`, so they are unaffected -- worth asserting once in a test so a
future recipe edit cannot quietly acquire the migration burden.

## Documentation

### Feature Documentation
- [ ] `docs/fields.md` -- the `KeyField` section gains an explicit note that a `datetime`
      KeyField's identity is the *instant*, that the stored value keeps the caller's offset,
      and that three representations of one instant address one row. There is currently no
      `KeyField(type=datetime)` example anywhere in the docs; add one, because an
      undocumented-but-supported type is how this class of bug got to 1.8.2 uncovered.
- [ ] `src/popoto/models/migrations.py` -- new **Tier 2 recipe 19, "Migrate datetime
      KeyField rows to canonical keys"**, covering the audit, the collision report, the
      migration, and re-verification. Written in the cookbook's existing
      Situation / What happens / Action required shape.
- [ ] `src/popoto/models/migrations.py` -- **recipe 20, "Repair rows duplicated before
      1.9.0"** (#538): how to read the collision report, how to compare the two hashes, and
      the three reconciliation choices with their tradeoffs. Cross-linked from recipe 19.
- [ ] `CHANGELOG.md` -- one `### Changed` entry with a **version boundary** paragraph in
      #525's wording, a **read before upgrading** paragraph for the identity-merge semantics
      (Risk 2), and the kill-switch name.
- [ ] `docs/indexed_fields.md` -- one sentence: index keys for datetime values follow the
      canonical form.

### External Documentation Site
- [ ] `mkdocs build --strict` passes; the migration cookbook page renders both new recipes.

### Inline Documentation
- [ ] `canonical_key_str` docstring states the format, why fixed-width, why `Z`, why naive
      is assumed UTC (citing #519/#421), and that it runs before `clean()`.
- [ ] `encoding.py::_decode_datetime`'s docstring currently explains at length why legacy
      rows are *not* assumed UTC and cites the key-shift as the reason. It must be rewritten,
      not deleted: the reason it gives is exactly what Task 2 removes, and the next reader
      needs that chain.
- [ ] `Model.db_key`'s docstring notes that partials are now raw values.

## Success Criteria

- [ ] `canonical_key_str(v)` is byte-identical to `str(v)` for every non-`datetime` value,
      verified over an exhaustive type sweep including `None`.
- [ ] An aware `+07:00` value, its UTC equivalent, and the equivalent naive value produce
      one identical Redis key, and `query.get()` finds the row by all three.
- [ ] A reloaded aware value still reports the caller's original `utcoffset()` -- key
      canonicalization does not touch value fidelity.
- [ ] With the legacy-as-UTC decode active, loading and re-saving a legacy row leaves
      `hash count == 1` (spike-2's reproduction, inverted).
- [ ] `rebuild_indexes()` on a mixed keyspace writes no index entry pointing at a
      nonexistent key, and reports the skipped rows.
- [ ] `audit_datetime_keys()` on a keyspace containing a #538 duplicate pair reports both
      hashes and does not modify anything.
- [ ] `migrate_datetime_keys()` is idempotent: a second run reports zero changes.
- [ ] `POPOTO_DATETIME_KEY_LEGACY=1` reproduces 1.8.2 key bytes exactly for aware, naive,
      and legacy-decoded values.
- [ ] The legacy decode branch is selected by shape, and gives the same result on 3.10 and
      3.12 for the same stored bytes.
- [ ] Tests pass (`scripts/ci-local.sh --fast`, with the environment stated alongside the
      count per CLAUDE.md).
- [ ] Documentation updated (`/do-docs`), including both new cookbook recipes.

## Team Orchestration

### Team Members

- **Builder (key identity)**
  - Name: `identity-builder`
  - Role: `canonical_key_str`, the two insertion points, the kill switch, compound-index hashing
  - Agent Type: builder (Domain: Redis/Popoto data)
  - Resume: true
- **Builder (provenance and rebuild)**
  - Name: `provenance-builder`
  - Role: source-key provenance on load, `rebuild_indexes()` divergence guard
  - Agent Type: builder (Domain: Redis/Popoto data)
  - Resume: true
- **Builder (migration tooling)**
  - Name: `migration-builder`
  - Role: `audit_datetime_keys()`, `migrate_datetime_keys()`, collision and inbound-reference reporting
  - Agent Type: builder (Domain: Redis/Popoto data)
  - Resume: true
- **Test engineer**
  - Name: `datetime-key-tester`
  - Role: converts the spike reproductions into regression tests; owns the Test Impact list
  - Agent Type: test-engineer
  - Resume: true
- **Documentarian**
  - Name: `datetime-key-docs`
  - Role: cookbook recipes 19 and 20, fields.md, CHANGELOG boundary note
  - Agent Type: documentarian
  - Resume: true
- **Validator**
  - Name: `datetime-key-validator`
  - Role: verifies Success Criteria and the Verification table
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Identity provenance on load
- **Task ID**: build-provenance
- **Depends On**: none
- **Validates**: `tests/test_datetime_tzinfo_round_trip.py`, `tests/test_lazy_load_partial_hash.py`,
  `tests/test_query_results.py`
- **Informed By**: spike-1 (both safety nets are blinded by the same recomputation)
- **Assigned To**: provenance-builder
- **Agent Type**: builder
- **Parallel**: true
- Thread the source Redis key into `decode_popoto_model_hashmap` / `_create_lazy_model`
  (`encoding.py:455-542`) and assign it to `_redis_key`, keeping today's recomputation as
  the fallback when no key is supplied.
- Pass the key from every loader that has one: `Query` hydration paths,
  `DB_key.get_instance`, `rebuild_indexes`.
- Add a test proving that a row whose decoded value derives a different key is *renamed* on
  save, not duplicated. This lands before any decode change, so it must be exercised with a
  deliberately shifted decoder in the test, not by changing production decoding.

### 2. Canonical key derivation
- **Task ID**: build-canonical
- **Depends On**: none
- **Validates**: `tests/test_datetime_tzinfo_round_trip.py`, `tests/test_db_key_escaping.py`,
  `tests/test_key_fields.py`, `tests/test_meta_indexes.py`
- **Informed By**: spike-3 (one insertion point is not enough), spike-4 (two is)
- **Assigned To**: identity-builder
- **Agent Type**: builder
- **Parallel**: true
- Add `canonical_key_str(value)` -- dispatch on `datetime.datetime` only; everything else
  returns `str(value)` unchanged. Format: UTC-normalized `%Y-%m-%dT%H:%M:%S.%f` + `Z`.
- Call it from `DB_key.__str__` (`db_key.py:268-276`) *before* `clean()`. Do not modify
  `clean`, `unclean`, `COLON_ESCAPE`, `GLOB_CHARS`, or `ESCAPABLE`.
- Stop pre-`str()`ing in `Model.db_key` (`base.py:664-669`); preserve the `"None"`
  placeholder semantics that `_has_unstable_db_key` depends on.
- Route `ModelOptions.compute_index_hash` and `compute_index_hash_from_values`
  (`base.py:273`, `:291`) through the same helper.
- Audit for any other site that stringifies a key value before building a `DB_key`
  (`git grep -n "str(" -- src/popoto/fields src/popoto/models | grep -i key`) and record
  the result in the PR, whether or not anything is found.
- Add `POPOTO_DATETIME_KEY_LEGACY` to `Defaults` with an env-var read, defaulting to
  canonicalization ON, and assert byte-identical 1.8.2 output when set.

### 3. Validate identity and provenance
- **Task ID**: validate-identity
- **Depends On**: build-provenance, build-canonical
- **Assigned To**: datetime-key-validator
- **Agent Type**: validator
- **Parallel**: false
- Reproduce spike-4's scenarios as assertions rather than prints.
- Confirm no non-datetime key bytes changed anywhere in the suite.

### 4. `rebuild_indexes()` divergence guard
- **Task ID**: build-rebuild-guard
- **Depends On**: build-canonical
- **Validates**: `tests/test_migrations.py`, `tests/test_check_indexes.py`
- **Informed By**: spike-2 (rebuild leaves the index pointing at a nonexistent key)
- **Assigned To**: provenance-builder
- **Agent Type**: builder
- **Parallel**: false
- Compare the scanned key against `instance.db_key.redis_key`; on divergence, skip the row,
  count it, log a `WARNING` naming the model and pointing at `audit_datetime_keys()`, and
  surface the count in the return value.
- Test the spike-2 keyspace directly: after rebuild, every `$KeyF:` member must name an
  existing hash.

### 5. Audit and migration tooling
- **Task ID**: build-migration
- **Depends On**: build-canonical
- **Validates**: `tests/test_migrations.py`, new `tests/test_datetime_key_migration.py`
- **Informed By**: spike-2 (the exact broken state to repair), spike-4 (the target state)
- **Assigned To**: migration-builder
- **Agent Type**: builder
- **Parallel**: false
- `Model.audit_datetime_keys()` -- classify every scanned key by shape (canonical, legacy
  offset-free, legacy with offset), group by canonical target, report collisions with both
  hashes' contents, and report inbound `Relationship` references. Read-only; no writes at
  all, asserted by a test.
- `Model.migrate_datetime_keys(dry_run=True)` -- default dry run. `RENAMENX` per row,
  `$Class:` fixup, orphaned `IndexedField` pointer side-key cleanup, one `rebuild_indexes()`
  at the end. Refuse collisions and inbound references without explicit opt-in.
- Idempotence and resumability tests, plus the Race 2 case ("source gone, target already
  canonical" is success).
- Put both methods in a dedicated module (`src/popoto/models/datetime_key_migration.py`)
  with thin `Model` classmethods; `base.py` is already ~3600 lines.

### 6. Adopt the legacy-as-UTC decode (#537)
- **Task ID**: build-legacy-utc
- **Depends On**: validate-identity, build-rebuild-guard, build-migration
- **Validates**: `tests/test_datetime_tzinfo_round_trip.py`
- **Informed By**: Freshness Check (merged try-order is `fromisoformat` first, contrary to
  PR #532's description; `fromisoformat` parses the legacy shape on 3.12 but not 3.10)
- **Assigned To**: identity-builder
- **Agent Type**: builder
- **Parallel**: false
- Select the legacy branch by an explicit anchored shape test, not by `fromisoformat`
  raising. Stamp UTC only on that branch.
- Leave `datetime.time` naive (PR #532's reasoning holds unchanged) and `datetime.date`
  untouched.
- Replace the two pinning tests per Test Impact, preserving their historical docstrings.
- Add a test asserting the same stored bytes decode identically regardless of interpreter
  minor version (assert on the branch selection, since one CI Python runs at a time).

### 7. Regression tests from the spikes
- **Task ID**: build-tests
- **Depends On**: build-legacy-utc
- **Assigned To**: datetime-key-tester
- **Agent Type**: test-engineer
- **Parallel**: false
- Convert spikes 1, 2 and 4 into tests, pinning a non-UTC process timezone as
  `test_datetime_tzinfo_round_trip.py` already does.
- Cover the Failure Path Test Strategy checklist in full.
- Add the assertion that no shipped recipe model declares a `KeyField(type=datetime)`.

### 8. Documentation
- **Task ID**: document-feature
- **Depends On**: build-tests
- **Assigned To**: datetime-key-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Cookbook recipes 19 and 20; `docs/fields.md` KeyField note plus the first
  `KeyField(type=datetime)` example in the docs; `docs/indexed_fields.md` sentence;
  CHANGELOG entry with the version boundary and the identity-merge warning.
- Rewrite `_decode_datetime`'s docstring rather than deleting its reasoning.

### 9. Final validation
- **Task ID**: validate-all
- **Depends On**: document-feature
- **Assigned To**: datetime-key-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table; confirm every Success Criterion; state the environment
  (Python version, redis-py version, Redis DB, process timezone) alongside every count.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Datetime key suite | `pytest tests/test_datetime_tzinfo_round_trip.py tests/test_datetime_key_migration.py -q` | exit code 0 |
| Migration suite | `pytest tests/test_migrations.py -q` | exit code 0 |
| Full fast gate | `scripts/ci-local.sh --fast` | exit code 0 |
| Format clean | `black --check src/ tests/` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |
| Escape logic untouched (#525 anti-criterion) | `git diff origin/main -- src/popoto/models/db_key.py \| grep -E '^[-+].*(COLON_ESCAPE\|GLOB_CHARS\|ESCAPABLE)'` | match count == 0 |
| `Model.db_key` no longer pre-stringifies | `grep -c 'str(getattr(self, key_field_name' src/popoto/models/base.py` | match count == 0 |
| Kill switch present | `grep -rn 'POPOTO_DATETIME_KEY_LEGACY' src/popoto/` | exit code 0 |
| Canonical helper is the single entry point | `grep -c 'canonical_key_str(' src/popoto/models/db_key.py src/popoto/models/base.py` | `db_key.py:1`, `base.py:2` (call sites only; the bare-word variant matches docstring mentions too and is not what this row checks) |
| #534 not implemented here (anti-criterion) | `git diff origin/main --name-only \| grep -c 'indexed_field_mixin.py'` | match count == 0 |
| Both cookbook recipes exist | `grep -c '^19\. \|^20\. ' src/popoto/models/migrations.py` | output > 1 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

1. **Is the identity merge acceptable?** Canonicalization deliberately collapses an aware
   value, its UTC equivalent, and the equivalent naive value onto one key. #519 already
   scores them identically, so the system has treated them as one instant since 1.7.x, and
   the alternative (keeping them distinct) is what causes the whole defect class. Confirming
   this is a maintainer call because it is the one genuinely irreversible semantic in the
   plan.

2. **What is `migrate_datetime_keys()`'s default for a collision?** The plan proposes
   report-only with no default strategy, per #538's own suggestion. The alternative,
   newest-wins by an `auto_now`-style field, is convenient and occasionally wrong. Confirm
   report-only, or name the strategy that should be the default.

3. **Ship the tooling, or ship a prose recipe only?** #538's scope check argues real-world
   exposure may be near zero: no `KeyField(type=datetime)` example anywhere in the docs, one
   test file using it (added by #532), and the field type had zero coverage before that. The
   plan argues for shipping tooling anyway, because the canonicalization *forces* a key
   rewrite and a library that breaks keys should provide the repair. If the maintainer
   prefers minimal surface, Task 5 collapses into cookbook recipes 19 and 20 with an inline
   script, and the plan shrinks from Large to Medium.

4. **Release shape.** 1.9.0 as a single minor carrying canonicalization, the migration, and
   the decode change (with the kill switch as the sequencing tool), versus splitting into
   1.9.0 (tooling, provenance, rebuild guard, no key-byte change) and 1.10.0 (canonical
   keys + decode). The single-minor route is planned above; the split is safer for adopters
   and slower.
