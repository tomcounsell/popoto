---
status: Planning
type: chore
appetite: Small
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/630
---

# Recipes through the field layer, PR 2: `provenance_journal`

## Problem

`src/popoto/recipes/provenance_journal.py` reaches past the field layer three
times and calls the shared client directly. At `4def49fb` the sites are:

| # | Line | Call | What it is asking |
|---|---|---|---|
| 1 | 984 | `POPOTO_REDIS_DB.exists(target_key)` | "does this record exist" |
| 2 | 1014 | `POPOTO_REDIS_DB.zscore(valid_from_key, target_key)` | "what is the target's stored `valid_from`" |
| 3 | 1075 | `POPOTO_REDIS_DB.pipeline()` | "open a transaction" |

Plus a fourth, different in kind: the public `pipeline=` parameter on `append`,
`annotate`, `retract`, `amend` and `_write` is typed `Optional["Pipeline"]` and
validated with `isinstance(pipeline, redis.client.Pipeline)` at line 1026. That
one is a documented public contract, not a bypass, and issue #630 explicitly
keeps it as a deprecated shim.

Sites 1 and 2 duplicate key-layout and encoding knowledge that the field and
model layers already own — site 2 rebuilds the `valid_from` key through
`ValidityField.get_interval_keys` and then reads it by hand, when
`ValidityField.get_valid_from` is the method that exists for exactly this.
Site 3 makes the recipe a direct consumer of the Redis client object, which is
the coupling that blocks the storage-backend seam #630 is a prerequisite for.

## Freshness Check

**Disposition: Minor drift.** Baseline `4def49fb` (main head, 2026-09-06).

- Issue #630's inventory was taken at `85c9faa`. The four `provenance_journal`
  sites it names (`exists`, `zscore`, `pipeline()`, the public `pipeline=`) are
  all still present and still exactly four; only their line numbers moved.
- `provenance_journal.py` changed since the inventory, most recently by
  **PR #638 (issue #606, merged `4def49fb`)**, which routed the annotate-and-close
  through `SupersessionProtocol.save_and_invalidate`. That rewrote the block that
  *uses* `pipe` but touched none of the three client calls. The `~L1068` pipeline
  line the issue cites is now line 1075.
- PR #634 (#630 PR 1, merged `bf85ec3b`) landed the pattern this PR follows:
  additive read methods on the field/model layer, recipe call sites swapped,
  existing tests unchanged as the oracle.
- No active plan in `docs/plans/` overlaps this file.

## Prior Art

- **PR #634 / `docs/plans/recipes_field_layer_default_memory.md`** — #630 PR 1.
  Established: new methods resolve the client attribute at call time so test
  spies keep intercepting; `counters.py` kept out of the package namespace;
  parity argued from the command sequence, not from the test result alone.
- **PR #638 (#606)** — the previous change to this file. Its finding that a
  justifying comment had gone stale is the direct reason this plan re-reads
  every comment adjacent to a swapped line rather than only the line itself.
- **PR #601 (#588)** — introduced `ValidityField.get_valid_from`, the method
  site 2 should have been calling.

## Research

No external research needed: purely internal, no new dependencies, no ecosystem
API involved.

## Spike Results

### spike-1: Does a field/model method already exist for each site?

- **Assumption**: sites 1 and 2 need new API.
- **Method**: code-read.
- **Result**: **Half false.** Site 2 needs **no new API at all** —
  `ValidityField.get_valid_from(model, field_name, member_key)`
  (`validity_field.py:1170`) issues exactly `ZSCORE <valid_from_key> <member>`
  and returns `Optional[float]`, which is what the call site computes by hand.
  Site 1 has no equivalent: `Model` has no `exists`; only `DB_key.exists()`
  (`models/db_key.py:302`) does, and the recipe holds a key *string*.
- **Confidence**: high.
- **Impact if false**: none — verified by reading both methods.

### spike-2: Can `popoto.batch()` return anything other than a real `Pipeline`?

- **Assumption**: a `Batch` wrapper object is a viable return type.
- **Method**: code-read.
- **Result**: **No.** `isinstance(pipeline, redis.client.Pipeline)` appears at
  **20 sites** across `models/base.py` and eight field modules
  (`confidence_field.py:399`, `cyclic_decay_field.py:467`,
  `existence_filter.py:424`, `sorted_field_mixin.py:620`, …). Several of them
  are of the shape `pipeline if isinstance(...) else POPOTO_REDIS_DB` — a
  non-`Pipeline` batch object would **silently fall back to the shared client**
  and execute immediately, voiding the very atomicity the batch exists for. It
  would be a silent correctness bug, not a type error.
- **Confidence**: high.
- **Impact if false**: none; this decides the design (see Technical Approach).

### spike-3: Would an execute-on-exit context manager work at the call site?

- **Assumption**: `with batch() as pipe:` fits site 3.
- **Method**: code-read of `_write`.
- **Result**: **No.** `_write` conditionally does *not* execute: when the caller
  supplied a pipeline it returns the queued pipe in `AnnotationResult` and the
  caller executes (`provenance_journal.py:1188-1202`). When it owns the pipe it
  executes inside a `try` that remaps `ResponseError` through `map_lua_error`
  (`:1204-1218`). An `__exit__` that executed would double-execute in the first
  case and swallow the remap in the second.
- **Confidence**: high.
- **Impact if false**: none; `batch()` is a factory, not an execute-on-exit CM.

## Data Flow

`JournalEntry.append/annotate/retract/amend` → `_write` → D7 pre-flight (sites 1
and 2, both **before** any command is queued) → `pipe = <site 3>` → `entry.save`
or `SupersessionProtocol.save_and_invalidate` (both take `pipeline=pipe`) →
either return `pipe` to the caller or `pipe.execute()` + `map_lua_error`.

The swap touches only how the three reads/constructions are *spelled*. The
command stream, its order, and who executes it are unchanged.

## Appetite

**Small.** Three call sites, one new model method, one new module of ~15 lines.
The design work is already done (spikes above); the risk is in parity, not in
construction.

## Prerequisites

- PR #634 merged (`bf85ec3b`) — pattern reference only, no code dependency.
- PR #638 merged (`4def49fb`) — this branch is rebased on it.

## Solution

### Key Elements

1. **`Model.exists(db_key=None, redis_key=None, **kwargs)`** — a classmethod on
   the base model, signature-mirroring `Model.load` → `Query.get`
   (`models/query.py:2269`), **including its `isinstance(db_key, str)`
   short-circuit**. See the Technical Approach: without that short-circuit the
   method is wrong at exactly this call site (critique round 1 blocker).
   Returns `bool`.
2. **`popoto.batch(transaction=True)`** — a new `src/popoto/batch.py` whose
   `batch()` returns `POPOTO_REDIS_DB.pipeline(transaction=transaction)`: a
   **real** `redis.client.Pipeline`, for the reason spike-2 gives. It is the
   supported way for a recipe or a user to open a Popoto transaction without
   importing the client.
3. **Three call-site swaps** in `provenance_journal.py`, after which the module
   no longer imports `POPOTO_REDIS_DB` at all.
4. **The `pipeline=` parameter stays exactly as it is** — same type, same
   `isinstance` validation, same three transactionality checks — and is
   *documented* as the deprecated raw-pipeline shim that also (and preferably)
   accepts `popoto.batch()`. Because `batch()` returns a real `Pipeline`, it is
   already accepted by the existing check; no validation code changes.

### Flow

Unchanged. Every swap is a same-command substitution:

| Site | Before | After | Redis commands |
|---|---|---|---|
| 1 | `POPOTO_REDIS_DB.exists(target_key)` | `model.exists(redis_key=target_key)` | `EXISTS k` → same |
| 2 | `ValidityField.get_interval_keys(...)` + `POPOTO_REDIS_DB.zscore(vf, target_key)` | `ValidityField.get_valid_from(model, VALIDITY_FIELD_NAME, target_key)` | `ZSCORE vf k` → same |
| 3 | `POPOTO_REDIS_DB.pipeline()` | `batch()` | none (client-side) |

### Technical Approach

**`Model.exists`.** A naive `DB_key(db_key or cls(**kwargs).db_key).exists()` is
**wrong**, and wrong in a way that would have shipped: `DB_key.__init__`'s
`flatten` (`models/db_key.py:112-122`) keeps a `str` argument as ONE opaque
partial, and `__str__` then runs `clean()` on it (`:191-196`), which replaces
every literal `:` with `COLON_ESCAPE`. So `str(DB_key("JournalEntry:pk123"))` is
`JournalEntry{&#58;}pk123` — a key that does not exist. `Model.load` escapes
this only because it delegates to `Query.get`, which short-circuits a `str`
`db_key` into `redis_key` before any `DB_key` construction
(`models/query.py:2269-2271`).

`Model.exists` therefore mirrors that short-circuit:

```python
@classmethod
def exists(cls, db_key=None, redis_key=None, **kwargs) -> bool:
    if isinstance(db_key, str) and not redis_key:
        redis_key, db_key = db_key, None
    if not db_key and not redis_key:
        db_key = cls(**kwargs).db_key
    key = redis_key if redis_key else db_key.redis_key
    return POPOTO_REDIS_DB.exists(key) > 0
```

which is byte-for-byte the expression `DB_key.exists()` (`db_key.py:302-311`)
already evaluates. Site 1 calls it as `model.exists(redis_key=target_key)` —
named explicitly, so the string path is not reached by accident.

`tests/test_model_exists.py` must cover a **multi-segment** redis key (the shape
site 1 passes, e.g. `instance.db_key.redis_key`), not only a single-segment key
or a `DB_key` instance: a single-segment test passes against the buggy body.

**Name collision.** `exists` becomes a reserved-ish name on `Model`: a subclass
declaring a field called `exists` would shadow the classmethod. `limit`,
`order_by` and `values` are the documented reserved names and `exists` is not
being added to that list in this PR — it is a classmethod, so a *field* named
`exists` shadows it on instances only, exactly as `load`/`save`/`delete`
already behave. Recorded in Risks, not guarded.

**`batch()` semantics.** A `redis.client.Pipeline` is already a context manager
whose `__exit__` calls `reset()`. `batch()` inherits that unchanged: `with
batch() as pipe:` cleans up but does **not** execute — you still call
`pipe.execute()`. Inventing execute-on-exit here would give `popoto.batch()`
different semantics from the raw pipelines the `pipeline=` shim still accepts,
which is precisely the divergence a deprecation shim must not have.

**Export.** `batch` is a new top-level public name in `popoto.__all__`, so it
needs a `CHANGELOG.md` `[Unreleased]` entry (the lesson from PR #638's review).
It is exported — unlike `counters.py`, which PR #634 deliberately kept out of
the namespace — because issue #630 names the API `popoto.batch()` literally.

That is the whole justification, and it is deliberately narrower than "the
single construction point a future `Backend` protocol swaps" (critique round 1
concern). Spike-2 disproves that stronger claim: the 20 `isinstance(pipeline,
redis.client.Pipeline)` sites would each have to change before any backend could
return a non-`Pipeline` batch, so `batch()` is **not** a sufficient swap point.
What it does buy today is that recipes stop importing the client, which is the
step #630 actually asks for; the isinstance sites are a separate, known blocker
for the seam and are out of scope here.

## Failure Path Test Strategy

### Exception Handling Coverage

- `Model.exists()` with neither `db_key` nor sufficient kwargs raises whatever
  `cls(**kwargs).db_key` raises today — same as `Model.load`. Not caught.
- Site 1's `ValueError` for a missing target and site 2's backdate `ValueError`
  must fire on exactly the same inputs as before; both are covered by existing
  tests in `tests/test_provenance_journal.py`.

### Empty/Invalid Input Handling

- `get_valid_from` returns `None` for a member absent from the index — the same
  `None` `zscore` returns, and the site's `if stored_valid_from is not None`
  guard is unchanged.

### Error State Rendering

- `map_lua_error` remap on `pipe.execute()` is untouched.

## Test Impact

**No existing test expectation changes.** Existing oracles:

- `tests/test_provenance_journal.py` — the whole D7 pre-flight, the
  caller-pipeline validation, the annotate-and-close atomicity.
- `tests/test_validity_field.py` — `get_valid_from` and the interval keys.

New tests:

- `tests/test_model_exists.py` (or an added class in an existing model test) —
  `Model.exists` by `db_key` and by KeyField kwargs, present and absent.
- `tests/test_batch.py` — `batch()` returns a transactional
  `redis.client.Pipeline`; `batch(transaction=False)` returns a
  non-transactional one; commands queue rather than execute; the object is
  accepted by `JournalEntry.append(pipeline=...)`.

## Rabbit Holes

- **Do not** design the `Backend` protocol. Explicit non-goal of #630.
- **Do not** give `batch()` execute-on-exit semantics (spike-3, Technical
  Approach).
- **Do not** swap the other five recipes. One PR per recipe.
- **Do not** touch `SupersessionProtocol` or the D7 pre-flight logic. #638 just
  rewrote that block; this PR changes three expressions inside it and nothing
  about its structure.

## Risks

### Risk 1: `batch()` diverges from `POPOTO_REDIS_DB.pipeline()`
If `batch()` ever grows behavior the raw path lacks, the deprecated `pipeline=`
shim and the blessed path stop being interchangeable. Mitigation: `batch()` is a
one-line delegation this PR and the docstring says so.

### Risk 2: `Model.exists` shadowed by a field named `exists`
See Technical Approach. Mitigation: documented, consistent with `load`/`save`.

### Risk 3: An extra Redis command shifts a fault-injection count
`Model.exists` routes through `DB_key.exists()`, which issues one `EXISTS`, the
same as the direct call. Mitigation: verify by command-sequence capture, not by
test result alone (PR #634 Risk 1, PR #638's parity method).

### Risk 4: `ruff` F401 on the now-unused `POPOTO_REDIS_DB` import
Mitigation: remove the import; `ruff check src/` is a gate.

## Race Conditions

None introduced. Sites 1 and 2 are pre-flight reads outside any transaction
before and after; site 3 constructs the same transactional pipeline.

## No-Gos (Out of Scope)

- The `Backend` protocol.
- Any change to `pipeline=`'s type, validation, or acceptance.
- The other five recipes in the #630 inventory.
- Removing or deprecating `POPOTO_REDIS_DB` / `get_redis()` (#630 non-goal).

## Documentation

### Feature Documentation
- `docs/features/provenance-journal.md` — the `pipeline=` parameter section
  gains the `popoto.batch()` spelling and the deprecated-shim note.

### External Documentation Site
- Wherever the client/pipeline is documented for users, add `popoto.batch()`.

### Inline Documentation
- Docstrings on `Model.exists` and `batch()`; a one-line comment at each swapped
  site only where the substitution is not self-evident.

## Success Criteria

1. `grep -n "POPOTO_REDIS_DB\|run_lua" src/popoto/recipes/provenance_journal.py`
   returns nothing.
2. `tests/test_provenance_journal.py` and `tests/test_validity_field.py` pass
   with **zero expectation changes** (`git diff` on those files shows only
   additions, if any).
3. New tests for `Model.exists` and `batch()` pass.
4. `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py` all
   pass (mypy at or below baseline, environment stated).
5. Command-sequence parity demonstrated for the annotate-and-close path.
6. `popoto.batch` exported and in `CHANGELOG.md [Unreleased]`.

## Step by Step Tasks

### 1. `Model.exists`
Add the classmethod to `src/popoto/models/base.py` beside `load`. Add tests.
Validate: `pytest tests/test_model_exists.py` (DB 9).

### 2. `popoto.batch()`
Add `src/popoto/batch.py`, export `batch` from `src/popoto/__init__.py`
(`__all__`). Add tests. Validate: `pytest tests/test_batch.py` (DB 9).

### 3. Recipe swap
Swap the three sites in `src/popoto/recipes/provenance_journal.py`, delete the
`POPOTO_REDIS_DB` import, and re-read every comment adjacent to a swapped line
for staleness. Validate: `grep` success criterion 1 + `ruff check src/`.

### 4. Parity validation
Capture the command sequence for `append`, `annotate` and a closing `retract`
before and after (spy on the client), and diff. Validate: identical sequences.

### 5. Documentation
`docs/features/provenance-journal.md`, `CHANGELOG.md [Unreleased]`, docstrings.

### 6. Pull request
Branch `session/sdlc-630-journal`, `Closes #630`? **No** — #630 covers six
recipes; this PR references it without a closing keyword.

## Critique Results

### Round 1 (2026-09-06) — verdict: NEEDS REVISION → revised, routed to BUILD

FULL depth, independent roster of 3 critics (Risk & Robustness, Scope & Value,
History & Consistency). 1 blocker, 1 concern, 1 nit. Per the lane's one-round
cap, the revision below was applied and the plan routed straight to BUILD
without a re-critique.

| # | Sev | Critic | Finding | Resolution |
|---|---|---|---|---|
| 1 | BLOCKER | Risk & Robustness | `DB_key(db_key or ...)` escapes the `:` in a redis-key string (`db_key.py:112-122`, `:191-196`), so `Model.exists` as drafted would `EXISTS` a key that never exists; `Model.load` only escapes this via `Query.get`'s `isinstance(db_key, str)` short-circuit (`query.py:2269-2271`). | Verified independently against both files. Technical Approach rewritten with the short-circuiting body; site 1 now calls `exists(redis_key=...)`; test requirement for a multi-segment key added. |
| 2 | CONCERN | Scope & Value | The "single construction point a future `Backend` protocol swaps" rationale for exporting `batch()` is disproved by the plan's own spike-2 (the 20 isinstance sites would also have to change). | Rationale narrowed to "issue #630 names the API literally" + "recipes stop importing the client"; the isinstance sites recorded as a separate known blocker. |
| 3 | NIT | History & Consistency | "20+ sites" is exactly 20. | Corrected to "20 sites". |

History & Consistency verified every other factual claim in the plan (line
numbers 984/1014/1026/1075, `get_valid_from` at `validity_field.py:1170`,
spike-3's ranges, commits `4def49fb` / `bf85ec3b`, the `counters.py` precedent)
as accurate.
