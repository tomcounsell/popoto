---
status: Planning
revision_applied: true
revision_applied_at: 2026-09-06T12:02:41Z
type: chore
appetite: Small
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/647
---

# Recipes through the field layer, PR 3: `policy_cache`

## Problem

`src/popoto/recipes/policy_cache.py` is the sharpest instance of the #630
bypass: it does not merely call the shared client, it **owns a Lua script**.
At `bb8ff588` the sites are:

| # | Line | What | What it is asking |
|---|---|---|---|
| 1 | 72 | `from ..redis_db import POPOTO_REDIS_DB, run_lua` | direct handle on the global client and the raw eval helper |
| 2 | 132-172 | the `TD_UPDATE_LUA` script constant | a recipe-owned Lua script that `HGET`s and `HSET`s the `q_value` field of the model hash and hand-rolls Popoto's `__Decimal__` cmsgpack wire format (lines 154, 169-170) |
| 3 | 377-386 | `run_lua(POPOTO_REDIS_DB, TD_UPDATE_LUA, 1, redis_key, ...)` in `update_q_value()` | "atomically apply a TD(0) update to this instance's Q-value" |
| 4 | 416-424 | `_get_redis_key(instance)` reading `instance._redis_key` / `instance.db_key.redis_key` | extract the raw hash key string to hand to `KEYS[1]` |

Two distinct defects, not one:

- **Encoding duplication.** Line 169 reproduces Popoto's internal Decimal
  encoding (`{['__Decimal__']=true, ['as_encodable']=tostring(new_q)}`) inside
  a recipe. That format is owned by the serialization layer. If it ever
  changes, a recipe silently writes values that `DecayingSortedField`'s
  `base_score_field` reader (`decaying_sorted_field.py:149-160`) decodes as its
  `1.0` fallback — with no error raised, because the guard there is
  `type(decoded) == 'number'` or a table carrying `as_encodable`, not a
  field-aware decoder.
- **Layer inversion.** Every other Lua script in the repo is owned by a field
  or a mixin and invoked through a method on that owner:
  `ConfidenceField.update_confidence`, `ValidityField.execute_supersede`,
  `IndexedFieldMixin`, `CoOccurrenceField`, `ExistenceFilter`,
  `PredictionLedgerMixin`, and `Model` itself. `TD_UPDATE_LUA` is the only Lua
  script in the codebase owned by a bare module-level function in `recipes/`.
  Recipes are meant to be *compositions of* primitives; this one is a
  primitive wearing a recipe's clothes.

Consequence: the recipe is a direct consumer of the Redis client object — the
coupling that blocks the storage-backend seam #630 is a prerequisite for — and
it is why `update_q_value` alone among these operations has no pipeline
support and no typed-error remap.

## Freshness Check

**Disposition: Unchanged.** Baseline `bb8ff588` (branch point from
`origin/main`, 2026-09-06).

- Issue #647 was filed 2026-09-06T11:15Z, minutes before this plan.
  `git log --since` on `src/popoto/recipes/policy_cache.py` returns nothing —
  the file has not moved since the issue was written. Its last substantive
  change is `16aa702e` (PR #594).
- Parent #630's inventory (taken at `85c9faa`, 2026-09-04) names one site in
  this recipe. Re-verified by hand at HEAD: one `run_lua` call, one owned
  script, one client import, one raw-key helper. Still exactly that.
- The two sibling PRs that set the pattern are merged and readable on main:
  **PR #634** (`default_memory`) and **PR #644** (`provenance_journal`, merged
  2026-09-06T11:14Z).
- Sibling in flight: **#646** (`graph_traversal`) has a plan committed on main
  at this same baseline (`docs/plans/recipes_field_layer_graph_traversal.md`).
  Different recipe file, and it adds nothing under `src/popoto/fields/`. No
  overlap; coordination signal only.
- No other active plan in `docs/plans/` touches `policy_cache.py`, `q_value`,
  or `DecayingSortedField.base_score_field`.

## Prior Art

- **PR #634 / `docs/plans/recipes_field_layer_default_memory.md`** — #630 PR 1.
  Established the series' rules: new methods resolve the client at call time so
  test spies keep intercepting; parity argued from the *command sequence*, not
  from a green suite alone.
- **PR #644 / `docs/plans/recipes_field_layer_provenance_journal.md`** — #630
  PR 2, merged today. Source of the base-vs-branch command-capture technique
  this plan reuses, and of the "re-read every comment adjacent to a swapped
  line" rule.
- **PR #427 (#410) `4fa3e097`** — the reason `q_value` is a separate storage
  slot at all. It split the Q-value out of the `DecayingSortedField` sorted-set
  score after the two writes were found to clobber each other. The regression
  tests it added (`TestQValueStorageSlotSeparation`,
  `tests/test_policy_cache.py:926`) are the primary oracle for this PR: they
  assert the Q survives `save()`, `touch()`, and an "acted" outcome.
- **PR #601 (#588) / `ValidityField.execute_supersede`**
  (`validity_field.py:897-1022`) — the canonical shape for a field classmethod
  wrapping a Lua script: field resolves the keys, pipeline branch queues and
  returns, non-pipeline branch remaps `ResponseError` through `map_lua_error`,
  return value decoded by the field.
- **`ConfidenceField.update_confidence`** (`confidence_field.py:495-587`) — the
  closest structural twin to what this PR builds: a classmethod that reads
  tuning constants off the field instance, runs one script, decodes a float,
  and syncs the new value back onto the Python instance.

### Why previous fixes did not cover this

PR #427 fixed *which slot* the Q-value lives in. It never questioned *who owns
the writer*, so the Lua stayed in the recipe. #630 is the first issue to name
ownership as the defect.

## Research

No external research. Purely internal refactor: no new dependencies, no
external API, no ecosystem pattern involved.

Valkey compatibility is already settled in-repo and this PR must not disturb
it: `grep -rnE '"\s*(BF|CMS|TOPK|TS|JSON)\.' src/popoto` returns nothing, and
`validity_field.py:30-33` / `existence_filter.py:33-35` carry the "core
commands only" declarations. `TD_UPDATE_LUA` uses `HGET`/`HSET` plus
`cmsgpack` only; moving the script text verbatim keeps that property by
construction.

## Spike Results

### spike-1: Is there any existing field/model helper that reads or writes a single field on the model hash?

- **Assumption**: `Model` or `DecimalField` already offers a single-field
  read/write that the recipe could have called instead of Lua.
- **Method**: code-read.
- **Result**: **False — none exists.** `DecimalField`
  (`fields/shortcuts.py:115-144`) is a bare `Field` subclass that only sets
  `type=Decimal`; it defines no accessors and relies entirely on the bulk
  `HSET`/`HGETALL` save/load path in `models/base.py`. The only single-field,
  non-full-save accessor on `Model` is `touch()` (`models/base.py:2389-2419`),
  which type-checks for `DecayingSortedField` and operates on the ZSET score,
  explicitly *not* the model hash. So the refactor cannot be a swap onto an
  existing method — new API is required, exactly as #647 anticipates.
- **Confidence**: high.
- **Impact if false**: the plan would collapse to a two-line call-site swap.

### spike-2: Where does the repo put a Lua script that a field owns?

- **Assumption**: field-owned scripts are class attributes.
- **Method**: code-read across `src/popoto/fields/` and `src/popoto/models/`.
- **Result**: **Refined.** Every one of the ~18 Lua constants in the repo is a
  **module-level** `NAME_LUA = ...` string in the owning field's module, with a
  KEYS/ARGV contract comment block directly above it, invoked through a method
  on the owning class via `run_lua`. Not one is a class attribute. Examples:
  `SUPERSEDE_LUA` (`validity_field.py:301`),
  `CAPPED_BAYESIAN_UPDATE_LUA` (`confidence_field.py:63`),
  `INDEX_SWAP_LUA` (`indexed_field_mixin.py:128`),
  `DECAY_SCORE_LUA` (`decaying_sorted_field.py:75`),
  `PURGE_ORPHAN_LUA` (`models/base.py:75`).
- **Confidence**: high.
- **Impact if false**: cosmetic only — placement would change, not ownership.

### spike-3: Would changing `q_value`'s field class break `DecayingSortedField(base_score_field="q_value")`?

- **Assumption**: `base_score_field` is resolved by field type and would need
  updating.
- **Method**: code-read.
- **Result**: **False, with a sharp caveat.** `base_score_field` is stored as a
  bare **string name** (`decaying_sorted_field.py:271`), threaded through
  `query.py:549-552` as `ARGV[4]`, and consumed in Lua by a raw
  `redis.call('HGET', member, base_score_field)` (`decaying_sorted_field.py:147`).
  It never looks at the Python field class. The caveat is the decoder: only a
  plain msgpack number, or a table carrying `as_encodable`, is accepted; **any
  other encoding falls through to the `1.0` default silently**
  (`decaying_sorted_field.py:149-160`). So the new field type is free to be any
  class it likes *provided its on-disk encoding stays the `__Decimal__` tagged
  dict*.
- **Confidence**: high.
- **Impact if false**: would force `base_score_field` plumbing changes and blow
  the Small appetite.

### spike-4: Do any tests pin the wire format or the command sequence?

- **Assumption**: a test asserts the raw cmsgpack bytes, constraining the
  refactor.
- **Method**: code-read of `tests/test_policy_cache.py` (1328 lines) and
  `tests/test_guide_examples.py`.
- **Result**: **No.** Despite its name,
  `test_q_value_encoding_round_trip` (`tests/test_policy_cache.py:1057`) goes
  through the Python API and asserts only `isinstance(reloaded.q_value,
  Decimal)` plus numeric closeness. No `Mock`, no `assert_called`, no literal
  `__Decimal__` byte assertion anywhere. The suite is a behavioral oracle, not
  an encoding oracle — which is precisely why this plan requires an explicit
  base-vs-branch command capture rather than trusting a green run.
- **Confidence**: high.
- **Impact if false**: a pinned test would become the parity proof and the
  capture step could be dropped.

## Data Flow

Today, one call:

```
caller: update_q_value(policy, reward=1.0, max_future_q=0.4)
  -> policy_cache._get_redis_key(instance)      # raw key string off db_key
  -> redis_db.run_lua(POPOTO_REDIS_DB, TD_UPDATE_LUA, 1, key, r, a, g, mfq)
       -> lua_script(text)  # process-wide cache, registered against POPOTO_REDIS_DB
       -> Script(keys=[key], args=[...], client=POPOTO_REDIS_DB)
            -> EVALSHA on the server:
                 HGET <key> q_value
                 (decode cmsgpack; __Decimal__ tagged dict or bare number)
                 compute td_error, new_q
                 HSET <key> q_value <cmsgpack __Decimal__ tagged dict>
  <- td_error (string) -> float()
```

After this PR, the same wire sequence, reached one layer lower:

```
caller: update_q_value(policy, reward=1.0, max_future_q=0.4)   # unchanged shim
  -> TDValueField.td_update(instance, "q_value", reward=..., max_future_q=...,
                            alpha=..., gamma=..., pipeline=None)
       -> field resolves the member key from instance.db_key (field layer owns
          key construction, not the recipe)
       -> run_lua(<client resolved at call time>, TD_UPDATE_LUA, 1, key, ...)
            -> identical EVALSHA / HGET / HSET sequence
       -> decode float, sync instance.q_value in memory
  <- td_error
```

The independent second reader is untouched and must stay untouched:

```
PolicyEntry.query.filter(...)  ->  DECAY_SCORE_LUA
                               ->  HGET <member> q_value   # base_score_field
```

## Appetite

**Small.** One recipe, one call site, one new field class that is a thin
subclass of an existing one, plus a verbatim script move. The whole diff should
read as relocation, not redesign. If the work starts requiring changes to
`base_score_field` plumbing, to the serialization layer, or to any test
expectation, the appetite is blown and the plan is wrong — stop and re-plan.

## Prerequisites

- PR #634 and PR #644 merged (both are, on `origin/main` at `bb8ff588`) — they
  define the series conventions this PR follows.
- No dependency on #646, #648, or #649. This PR touches
  `src/popoto/recipes/policy_cache.py` and adds one new module under
  `src/popoto/fields/`; the other lanes in the series touch neither.

## Solution

### The design decision: field, not backend op

#647 requires this plan to pick between promoting `TD_UPDATE_LUA` to a field
and adding a backend-level `td_update` operation, and to argue the choice.

**Chosen: a field.** Add `TDValueField`, a subclass of `DecimalField`, which
owns `TD_UPDATE_LUA` and exposes `td_update()` as a classmethod. `PolicyEntry`
declares `q_value = TDValueField(default=Decimal("0"))`.

Why the field wins:

1. **It is the only shape with precedent.** Eighteen Lua scripts in this repo
   are owned by a field or mixin and driven from a method on the owner
   (spike-2). Zero are owned by the backend. A backend op would be the first of
   its kind and would have to invent its own conventions for keys, pipelines
   and error remap, while the field shape inherits `ConfidenceField`'s and
   `ValidityField`'s conventions wholesale.
2. **The backend seam does not exist yet.** #630 is a *prerequisite for* the
   storage-backend seam, not a consumer of it. Adding a `td_update` op means
   designing that seam's operation vocabulary as a side effect of a recipe
   cleanup — the largest possible rabbit hole for a Small appetite, and a
   design that would be argued from a single call site.
3. **The knowledge being relocated is field knowledge, not backend knowledge.**
   What the recipe wrongly owns is (a) how a `Decimal` is encoded in the model
   hash and (b) which hash field the value lives in. Both are the property of
   the field that declares the column. A backend op would have to be told the
   encoding and the field name by its caller — the duplication would move, not
   disappear.
4. **It makes the field pair honest.** `q_value` and `expected_value` are a
   deliberately coupled pair: `DecayingSortedField(base_score_field="q_value")`
   reads `q_value` out of the model hash in Lua (spike-3). Today one half of
   that pair is a field and the other half's writer is a loose function. After
   this change both halves are fields, and the coupling is expressed in field
   declarations rather than in a recipe's imports.

Why the backend op was rejected rather than deferred: it is not merely more
work, it is the wrong layer. A backend named `td_update` would encode a
reinforcement-learning update rule into the storage abstraction, which then has
to be implemented by every future backend. TD(0) is a modeling concern.

### Key Elements

- **New module `src/popoto/fields/td_value_field.py`**
  - `TD_UPDATE_LUA` — the script text moved **verbatim** from
    `policy_cache.py`, with its KEYS/ARGV contract comment block. A
    Valkey-safety note matching `validity_field.py:30-33` (core commands only:
    `HGET`, `HSET`, `cmsgpack`) goes in the **module docstring**, as Python
    prose *outside* the script string literal — **never as a Lua comment
    inside it**. `lua_script()` caches the `Script` object keyed by the exact
    script text (`redis_db.py:838-854`), so a comment added inside the literal
    changes the SHA and the `EVALSHA` payload. That is a cache-key change, not
    a cosmetic one, and it forfeits the parity argument.
  - `class TDValueField(DecimalField)` — adds no storage of its own; the value
    still lives in the model hash under the field's own name, encoded exactly
    as `DecimalField` encodes it. **No new constructor kwargs.** An earlier
    draft gave the field `alpha`/`gamma` defaults; that is new configuration
    surface, not a relocation, and it is now a No-Go (see Open Question 4).
    `alpha` and `gamma` stay exactly where they are today: caller arguments
    defaulting to `Defaults.TD_ALPHA` / `Defaults.TD_GAMMA`.
  - `TDValueField.td_update(cls, model_instance, field_name, *, reward,
    max_future_q=0.0, alpha=Defaults.TD_ALPHA, gamma=Defaults.TD_GAMMA,
    pipeline=None) -> float | None` — the classmethod wrapper, shaped after
    `ConfidenceField.update_confidence` **in structure only**: it resolves the
    member key from `model_instance.db_key`, raises on an unsaved instance,
    runs the script, decodes the TD error to `float`, and syncs the new Q back
    onto the Python instance. Returns `None` on the pipeline branch (result is
    only available after `execute()`), matching `update_confidence`.
  - **The unsaved-instance guard must NOT be copied wholesale from
    `update_confidence`.** That twin guards with *two* checks — a
    `db_key.redis_key` try/except **and** a separate
    `POPOTO_REDIS_DB.exists(member_key)` round trip on the non-pipeline branch
    (`confidence_field.py:~494`) — and it raises `TypeError`. Today's
    `_get_redis_key` (`policy_cache.py:416-424`) does neither: attribute /
    `db_key` lookup only, raising `ValueError`, zero extra Redis commands.
    `td_update` must reproduce *today's* guard:

    ```python
    redis_key = getattr(model_instance, "_redis_key", None)
    if not redis_key:
        try:
            redis_key = model_instance.db_key.redis_key
        except Exception:
            raise ValueError("Cannot operate on unsaved PolicyEntry")
    ```

    No `.exists()` call. An added `EXISTS` is a new command in the captured
    sequence and makes the mandatory parity diff non-empty — which this plan
    defines as a hard failure, not a nit.
- **`src/popoto/recipes/policy_cache.py`**
  - `q_value = TDValueField(default=Decimal("0"))`.
  - `update_q_value()` becomes a thin delegation to
    `TDValueField.td_update(...)`. Its signature, defaults, return value and
    docstring contract are unchanged — it is public recipe API and the guide
    documents it.
  - `TD_UPDATE_LUA` is **re-exported** from the module (`from
    ..fields.td_value_field import TD_UPDATE_LUA`) so any downstream import of
    the name keeps working; the definition no longer lives here.
  - The `POPOTO_REDIS_DB, run_lua` import is dropped if nothing else in the
    file uses it (it does not — spike-1's inventory found no other site).
  - `_get_redis_key()` is retained only if `initialize_q_value` still needs its
    unsaved-instance guard; otherwise deleted with the same guard expressed by
    the field.

### Flow

1. `update_q_value(policy, reward=r)` — unchanged public entry point.
2. Delegates to `TDValueField.td_update(policy, "q_value", reward=r, ...)`.
3. The field resolves the member key and runs `TD_UPDATE_LUA` through
   `run_lua`, resolving the client at call time (PR #634's rule) so test spies
   and DB rebinds still intercept.
4. The script issues the identical `HGET` / `HSET` pair against the identical
   key with the identical encoding.
5. The float TD error is returned to the caller; `policy.q_value` is refreshed
   in memory.

### Technical Approach

- **Client resolution at call time.** Do not capture `POPOTO_REDIS_DB` at
  import in the new module. Follow the #634 rule and resolve the client inside
  `td_update` (`from ..redis_db import POPOTO_REDIS_DB` at call time, or the
  module-attribute lookup the sibling fields use). `run_lua` itself caches the
  `Script` object against the module-level client but passes the caller's
  client into `Script.__call__`, so the execution target is per-call
  (`redis_db.py:857-880`) — preserve that property, do not narrow it.
- **Move the script verbatim.** Byte-identical script text is what keeps the
  cached `Script` SHA and the on-wire `EVALSHA` payload the same. Reformatting
  the Lua, even whitespace, changes the SHA. Do not touch it.
- **Pipeline branch is additive, not required.** Add it because every sibling
  field has it and because `run_lua`'s pipeline path already exists, but no
  current caller passes a pipeline, so it must not change the default path's
  command sequence. It is argued from precedent, not from a requirement of
  #647 — say so in the PR body so a reviewer does not read it as scope
  attached to the issue.
- **No change to `PolicyEntry.expected_value`.** `base_score_field="q_value"`
  stays a string and keeps resolving to the same hash field with the same
  encoding (spike-3).
- **`Defaults.TD_ALPHA` / `Defaults.TD_GAMMA` stay in
  `fields/constants.py`, and stay *call-time* defaults.** Per CLAUDE.md these
  are tuning magic numbers, not user config. Promoting them to field-declared
  defaults would create a third resolution tier (field-declared > module
  constant > caller) that nothing asks for, and two models declaring different
  `alpha` on the same field name would diverge with no runtime detection —
  the hazard `ConfidenceField` already documents for `evidence_cap`. The
  recipe keeps re-exporting `TD_ALPHA` / `TD_GAMMA` at module level for the
  guide's sake.

## Test Impact

**No test expectation may change.** The existing suite is the oracle.

Tests that must pass untouched (`tests/test_policy_cache.py`):
`test_q_value_update` (136), `test_q_value_update_requires_save` (164),
`test_composite_score_query` (279), `test_end_to_end` (848),
`class TestQValueStorageSlotSeparation` (926) — `test_q_value_survives_save`,
`test_q_value_survives_touch`, `test_q_value_survives_acted_outcome`,
`test_q_value_encoding_round_trip`, `test_negative_q_value_ranking` (1101),
`test_rank_derives_from_stored_q_value` (1150),
`test_td_update_then_save_q_survives` (1184),
`test_save_then_td_then_save_no_reset` (1225),
`test_td_update_nil_q_treated_as_zero` (1271),
`test_decay_rank_with_missing_q_value` (1306). Plus the Q-value block in
`tests/test_guide_examples.py:368-388`.

New tests (additive only, in a new `tests/test_td_value_field.py`):

1. `TDValueField.td_update` on an unsaved instance raises **`ValueError`** —
   the exception the recipe raised before, not `update_confidence`'s
   `TypeError` — **and issues zero Redis commands** while doing so. Assert
   both: the type, and that no `EXISTS` (or any other command) reaches the
   server on that path. The two halves are the same borrowed code path;
   fixing only the exception type while keeping the twin's `.exists()` call
   still breaks parity.
2. `td_update` through a `popoto.batch()` pipeline returns `None`, queues one
   `EVALSHA`, and applies on `execute()`.
3. A model that is *not* `PolicyEntry` can declare a `TDValueField` and get a
   TD update — proving the primitive is genuinely reusable and not
   `PolicyEntry`-shaped.
4. `policy_cache.TD_UPDATE_LUA is td_value_field.TD_UPDATE_LUA` — the
   re-export is a real re-export (Risk 5).

**Parity proof (mandatory, PR #644's technique).** Capture the Redis command
sequence for `test_q_value_update`, `test_td_update_nil_q_treated_as_zero` and
`test_rank_derives_from_stored_q_value` on `origin/main` and on the branch —
against a non-zero, lane-scoped test DB — and diff. The diff must be empty. A
green suite alone is not the proof, because spike-4 established that no test
asserts the wire format.

Run scoped: `pytest tests/test_policy_cache.py tests/test_td_value_field.py
tests/test_guide_examples.py -k policy or q_value`. Never the full suite from
this worktree.

## Rabbit Holes

- **Designing the storage-backend operation vocabulary.** Rejected above. If
  the build starts sketching a backend `td_update`, stop.
- **Generalizing to "any atomic single-field hash update".** A generic
  `Model.hset_field` / `refresh_field` is a tempting adjacent API (spike-1
  showed none exists). It is out of scope: it would need an encoding contract,
  an index-consistency story with `IndexedFieldMixin`, and a much larger test
  surface. Build the narrow field.
- **Rewriting the Lua for clarity.** The script is dense and the `__Decimal__`
  branch reads awkwardly. Leave it byte-identical; readability edits change the
  SHA and forfeit the parity argument.
- **Fixing `expected_value` / `base_score_field`'s silent `1.0` fallback.**
  Spike-3 surfaced a real latent hazard: an unexpected encoding degrades
  silently. It deserves its own issue, not this PR.
- **Touching the other five #630 recipes.** One recipe, one PR.
- **Adding a pipeline parameter to the public `update_q_value`.** The field
  gets one; the recipe shim's signature stays exactly as documented.

## Risks

### Risk 1: A moved script that is not byte-identical

Any whitespace change alters the `Script` SHA and the `EVALSHA` payload,
breaking the parity claim even though behavior is identical. **Mitigation:**
move by cut-and-paste, then verify with a diff of the extracted constant
between base and branch (`git show origin/main:src/popoto/recipes/policy_cache.py`
vs the new module) before running anything.

### Risk 2: Field-class swap changes the on-disk encoding

If `TDValueField` fails to inherit `DecimalField`'s `type=Decimal`, the
`q_value` column encodes differently and `DECAY_SCORE_LUA` silently falls back
to `1.0` — ranking regresses with no exception. **Mitigation:**
`TDValueField(DecimalField)` with no `__init__` override of `type`, plus the
existing `test_rank_derives_from_stored_q_value` and
`test_decay_rank_with_missing_q_value` as detectors, plus the command capture.

### Risk 3: Import cycle

`fields/td_value_field.py` importing from `redis_db` at module level, or
`recipes/policy_cache.py` re-exporting from it, can create a cycle with
`fields/shortcuts.py`. **Mitigation:** import the client inside the function
(which the call-time-resolution rule already requires) and keep the new module
importing only `DecimalField`, `Defaults`, and `run_lua`.

### Risk 4: `ruff` F401 on the now-unused `POPOTO_REDIS_DB` / `run_lua` import

Same failure PR #644 hit. **Mitigation:** delete the import in the same commit
as the call-site swap and run `ruff check src/` before pushing.

### Risk 5: A re-export that is not a real re-export

Downstream code and the guide import `TD_UPDATE_LUA` from
`popoto.recipes.policy_cache`. If the name is dropped rather than re-exported,
that is a silent breaking change in a "no behavior change" PR.
**Mitigation:** explicit re-export plus a test asserting
`policy_cache.TD_UPDATE_LUA is td_value_field.TD_UPDATE_LUA`.

### Risk 6: mypy ratchet

The new module adds typed surface; the ratchet
(`scripts/mypy_ratchet.py`) fails if the total rises above baseline. `fields/`
is not in the `clean` allowlist, so a small increase is survivable only if the
total stays at or below baseline. **Mitigation:** annotate the new
module fully; measure the ratchet in this worktree's environment and state the
redis-py version alongside the number.

## Race Conditions

Unchanged by this PR, and that is the point. The TD update's atomicity comes
entirely from being a single `EVALSHA` — read-modify-write of `q_value` happens
server-side in one script invocation. Two concurrent `td_update` calls on the
same instance serialize at the server, exactly as before. Nothing in the
refactor moves the read or the write out of the script, and nothing introduces
a client-side round trip between them.

The one thing to guard: `td_update` must **not** be reimplemented as a
Python-side `HGET`, compute, `HSET`. That would be a lost-update race and is a
build-time blocker, not a review nit.

## No-Gos (Out of Scope)

- The other five recipes in the #630 series (#646, #648, #649 and the rest).
- Any storage-backend abstraction or backend-level operation vocabulary.
- A generic single-field hash accessor on `Model`.
- Changing `DecayingSortedField`'s `base_score_field` decode fallback.
- Changing any tuning constant value, or promoting `alpha`/`gamma` to user
  config (CLAUDE.md: these are magic numbers for experimental tuning).
- Field-declared `alpha` / `gamma` constructor kwargs on `TDValueField`. They
  are new configuration surface with a silent-divergence hazard and no caller
  asking for them; if a real per-model learning rate is ever needed, that is a
  follow-up issue.
- Any change to `update_q_value`'s public signature or return type.
- Deprecating or renaming `initialize_q_value`.

## Documentation

### Feature Documentation

- `docs/guides/policy-cache-recipe.md` — the Q-value section describes
  `update_q_value` and the TD script. Update it to say the script is owned by
  `TDValueField` and that `update_q_value` is a convenience wrapper. Keep the
  worked example unchanged (it is executed by `tests/test_guide_examples.py`).
- Add `TDValueField` to whatever field reference the docs site carries
  (`docs/` field index / API reference), following how `ConfidenceField` is
  documented.

### Inline Documentation

- The KEYS/ARGV contract comment block moves with the script.
- Add a Valkey-compatibility note to the new module docstring, matching
  `validity_field.py:30-33`.
- Re-read every comment adjacent to a swapped line before finalizing — PR #638
  found a stale justifying comment surviving a refactor; PR #644 made this a
  standing rule for the series.
- `policy_cache.py`'s module docstring lists `update_q_value(): Atomic Q-value
  TD update via Lua script` — restate as "via `TDValueField`".

## Success Criteria

- [ ] `src/popoto/recipes/policy_cache.py` contains no `run_lua` call, no
      `POPOTO_REDIS_DB` reference, and no Lua script definition.
- [ ] `grep -n 'TD_UPDATE_LUA' src/popoto/fields/td_value_field.py` is the only
      definition site; `policy_cache.py` re-exports it.
- [ ] The moved script text is byte-identical to the base version.
- [ ] Every test listed in Test Impact passes with **zero edits to test
      expectations**.
- [ ] The base-vs-branch Redis command capture diff is empty for the three
      named tests.
- [ ] New tests in `tests/test_td_value_field.py` cover unsaved-instance,
      pipeline, non-`PolicyEntry` model, and field-level alpha/gamma.
- [ ] `ruff check src/` exits 0; `black --check src/ tests/` passes.
- [ ] `scripts/mypy_ratchet.py` is at or below baseline, with the environment
      stated in the PR body.
- [ ] No Redis-module command appears in the new module
      (`grep -rnE '"\s*(BF|CMS|TOPK|TS|JSON)\.'` clean).
- [ ] PR body carries `Closes #647`.

## Step by Step Tasks

### 1. New field module

- Create `src/popoto/fields/td_value_field.py`.
- Move `TD_UPDATE_LUA` verbatim from `policy_cache.py:132-172`, with its
  contract comment block; add the Valkey note.
- Add `class TDValueField(DecimalField)` — no new constructor kwargs; `alpha`
  and `gamma` remain call-time arguments of `td_update` defaulting to
  `Defaults.TD_ALPHA` / `Defaults.TD_GAMMA`.
- Put the Valkey-safety note in the module docstring, outside the script
  literal.
- Add `td_update` classmethod modeled on
  `ConfidenceField.update_confidence` (`confidence_field.py:495-587`):
  member-key resolution, unsaved guard, pipeline branch, `run_lua` with the
  client resolved at call time, float decode, in-memory sync.
- Verify byte-identity of the moved script against `origin/main` before
  proceeding: diff the `TD_UPDATE_LUA` string literal character-for-character
  against `git show origin/main:src/popoto/recipes/policy_cache.py` (lines
  132-172). If the diff shows any note text *inside* the literal, the note
  landed in the wrong place and must move to the module docstring.

### 2. Recipe swap

- `q_value = TDValueField(default=Decimal("0"))` in `PolicyEntry`.
- `update_q_value()` delegates to `TDValueField.td_update`; signature, defaults,
  return type and docstring contract unchanged.
- Re-export `TD_UPDATE_LUA`; drop the `POPOTO_REDIS_DB, run_lua` import.
- Re-read every comment adjacent to a changed line; fix stale claims.

### 3. Tests

- Add `tests/test_td_value_field.py` with the four cases from Test Impact.
- Assert the guard path issues zero Redis commands (no `EXISTS`).
- Change no existing test expectation.

### 4. Parity validation

- Capture the Redis command sequence base-vs-branch for
  `test_q_value_update`, `test_td_update_nil_q_treated_as_zero`,
  `test_rank_derives_from_stored_q_value`; diff must be empty.
- Run the scoped pytest selection with a lane-scoped `POPOTO_TEST_DB`
  (never DB 0, never the full suite from this worktree).
- Run `ruff check src/`, `black --check src/ tests/`,
  `scripts/mypy_ratchet.py`; record the environment.

### 5. Documentation

- Update `docs/guides/policy-cache-recipe.md` and the field reference.
- Sequence the docs cascade and any review-driven patches **before**
  `verdict finalize`, then re-review the delta (#642).

### 6. Pull request

- Open against `main` from `session/sdlc-647` with `Closes #647`, the parity
  capture pasted in, and the environment stated next to every count.

## Open Questions

1. **Field name.** `TDValueField` is the issue's own suggestion and this plan
   adopts it. `QValueField` would be more domain-honest (the column is
   literally `q_value`) but leaks RL vocabulary into the field layer;
   `TDValueField` names the update rule instead. Confirm, or say the word and
   it becomes `QValueField`.
2. **Should `update_q_value` gain a `pipeline=` parameter?** The plan says no
   (No-Gos), keeping the recipe shim frozen. The counter-argument is that the
   field gains the capability and the recipe is the only documented way to
   reach it. Deferring costs a follow-up issue if a caller ever wants it.
3. **Does `initialize_q_value` also belong on the field** (as
   `TDValueField.initialize`)? It currently sets the attribute and calls
   `save()` — pure Python, no bypass — so #630 does not require moving it. It
   would be tidier to have both TD entry points on the field. Left alone for
   now to keep the diff a relocation.

4. **Field-declared `alpha` / `gamma`** — dropped in the round-1 revision as
   unrequested configuration surface. Flag if a per-model learning rate is
   actually wanted and it becomes a follow-up issue rather than a No-Go.

## Critique Results

### Round 1 (2026-09-06) — verdict: READY TO BUILD (with concerns)

Roster: Risk & Robustness, Scope & Value, History & Consistency (FULL depth,
independent roster, 3 critics). 0 blockers, 3 concerns, 1 nit. All three
concerns were applied to the plan in the revision commit that follows.

| Critic | Severity | Finding | Applied as |
|---|---|---|---|
| Risk & Robustness | CONCERN | `td_update` "shaped after `ConfidenceField.update_confidence`" would import that twin's *two-check* guard, including a `POPOTO_REDIS_DB.exists()` round trip the current `_get_redis_key` does not make — a new `EXISTS` in the captured command sequence, which makes the mandatory parity diff non-empty. | Key Elements now pins the exact guard body (attribute / `db_key` lookup, `ValueError`, no `.exists()`), and Test Impact case 1 asserts zero Redis commands on that path. |
| Risk & Robustness | CONCERN | The unsaved-instance test as written pinned only the exception type, so a guard that raised `ValueError` *and* called `.exists()` would pass the test while failing parity. | Test Impact case 1 now asserts both halves: `ValueError` **and** zero commands. |
| Scope & Value | CONCERN | Field-level `alpha`/`gamma` constructor kwargs are new configuration surface, not a relocation: a third default tier with a silent cross-process divergence hazard (the one `ConfidenceField` documents for `evidence_cap`), unrequested by #630. | Kwargs dropped. `alpha`/`gamma` stay call-time arguments; the idea is now an explicit No-Go plus Open Question 4. |
| History & Consistency | CONCERN | Key Elements read as if the Valkey-safety note goes inside `TD_UPDATE_LUA`; since `lua_script()` caches by exact script text, a comment inside the literal changes the SHA and forfeits the byte-identity claim the whole PR rests on. | Key Elements now says module docstring, outside the literal, and task 1 diffs the literal character-for-character against base. |
| Scope & Value | NIT | The pipeline branch is argued from precedent, not from a requirement of #647. | Kept, with a note to say so in the PR body. |

Rejected as non-contradictions by History & Consistency: the No-Go on
`update_q_value`'s signature vs. the field's pipeline branch (different
surfaces), and the No-Go on a generic hash accessor vs. `td_update` (TD-rule
specific, not generic).
