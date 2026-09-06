---
status: Ready
type: chore
appetite: Small
owner: Tom Counsell
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/606
last_comment_id: none
---

# Journal annotation close: `execute_supersede` vs. `SupersessionProtocol`

## Problem

`ProvenanceJournal._write` closes an annotation target's validity interval by
calling `ValidityField.execute_supersede(...)` directly
(`src/popoto/recipes/provenance_journal.py:1135-1146`). `execute_supersede` is
the low-level script driver: it takes raw member key strings and builds the
`EVAL` argument vector. The recipe layer is supposed to orchestrate over
`SupersessionProtocol`, which is the supported public spelling of
"append a successor and close an incumbent atomically".

The in-code comment at `provenance_journal.py:1119-1134` currently justifies the
direct call on the grounds that `SupersessionProtocol` cannot express two things
this call site needs:

1. an **explicit `old_member`** — the annotation target is named by the caller
   and validated by the D7 pre-flight, not resolved through an identity pointer;
2. **`assert_valid_from=False`** — valid-time is already written at construction
   (`provenance_journal.py:944`, `validity=instant`), so re-asserting it through
   the script would be wrong.

Issue #606 asks for one of two outcomes: extend `SupersessionProtocol` to
express those, or record a permanent decision that `execute_supersede` is
correct here. **That premise is stale, and the evidence below settles it: the
protocol already expresses both, via `SupersessionProtocol.save_and_invalidate`,
which has been on `main` since PR #601 (#588 plan D6).** The real question is
therefore narrower than the issue framed it — not "can the protocol express
this?" but "does routing this call site through `save_and_invalidate` preserve
byte-identical behavior, and what does it cost?"

This plan answers that with a call-by-call comparison of the two spellings
against `SUPERSEDE_LUA`, and lands the conversion where it is provably neutral.

**Current behavior:** the annotate-and-close pipeline is assembled by hand in
`_write` — `entry.save(pipeline=pipe)`, a defence-in-depth `saved`/verdict
check, then `ValidityField.execute_supersede(..., mode="invalidate",
old_member=target_key, assert_valid_from` defaulted to `False`, `pipeline=pipe)`,
then the recipe's own `pipe.execute()` and `map_lua_error` remap.

**Desired behavior:** the same pipeline, byte-identical on the wire, assembled
by `SupersessionProtocol.save_and_invalidate` — with the journal keeping every
pre-flight check the protocol structurally cannot express, and keeping its own
`AnnotationResult` contract and error text.

## Freshness Check

**Disposition: Minor drift (premise correction).** Baseline commit
`3cf8c2d0` (`origin/main`, "M4 (#563): reference resolution at capture with
typed abstention (#622)").

| Issue reference | Status at `3cf8c2d0` |
|---|---|
| `provenance_journal.py:1119-1142` — the `execute_supersede` call and its comment | **Drifted, claim holds.** Now `1119-1146`. M4 (#622) already re-pointed the comment away from #563 to #606, as the issue predicted. The `execute_supersede` call itself is unchanged. |
| `provenance_journal.py:960-1013` — D7 pre-flight | **Unchanged.** Firewall scan at `:969`, target-exists / cross-agent / kind-target checks at `:971-1013`. |
| `provenance_journal.py:944` — valid-time set at construction | **Unchanged.** `validity=instant` in the `JournalEntry(...)` constructor. |
| `#563` (M4) | **Closed**, merged as `3cf8c2d0` / PR #622. Its decline of the conversion is recorded in the in-code comment. Capture path confirmed: M4 appends targetless `kind="assert"` entries and never reaches the close branch. |
| `#588` (PR #601, `90fc3d30`) | **Merged.** This is the commit that introduced `save_and_invalidate` and moved the membership decision inside `SUPERSEDE_LUA`. **It invalidates the issue's stated premise** (see Evidence). |
| `#589` (PR, `67965a1e`) | **Merged.** Shipped `_write`'s pre-flight and the defence-in-depth `saved` check. |

Commits touching the three relevant files since the issue area was last
surveyed: `3cf8c2d0`, `90fc3d30`, `16aa702e`, `67965a1e`, `a4f7fbf4`. All read.

Overlapping active plans in `docs/plans/`: `provenance_journal_m1.md` (Complete)
and `recipes_field_layer_default_memory.md` (Complete). **`recipes_field_layer_default_memory.md`
is PR 1 of issue #630, whose PR 2 target is this same module.** That is a
sequencing signal, not a blocker: #606 is scheduled to merge first, and #630's
`provenance_journal` PR rebases on the result. The two touch disjoint call
sites (#606 the `execute_supersede` close; #630 the `exists`/`zscore`/`pipeline()`
reads), so no content conflict is expected beyond line drift.

No bug to reproduce — this is a chore/refactor issue, not a defect.

## Research

Skipped per the skill's stated exclusion: this is purely internal refactoring of
one module against another module in the same package. No external library, API,
or ecosystem pattern is involved. The authoritative sources are `SUPERSEDE_LUA`
and the two Python call paths, all read in full and quoted under Evidence.

No relevant external findings — proceeding with codebase context.

## Prior Art

| Ref | Relevance |
|---|---|
| **#588 / PR #601** (`90fc3d30`) | Introduced `SupersessionProtocol.save_and_invalidate` and `_save_and_close`, and moved the membership decision into `SUPERSEDE_LUA`. This is the work that makes the conversion possible; the issue was filed as if it had not happened. |
| **#589 / PR** (`67965a1e`) | Shipped M1's `_write`, its D7 pre-flight, the pipeline validator, and the "annotation was not written" backstop. Its round-2 and round-3 reviews are why the pre-flight scans `entry._never_record_scan_values()` rather than a hand-written list, and why the WATCH check is `watching and not explicit_transaction`. Both constraints survive this change untouched. |
| **#563 / PR #622** (`3cf8c2d0`) | Declined the conversion as out of scope for a capture-path milestone, and filed it here. Confirmed correct: M4 never reaches the close branch. |
| **#476** | The precedent for hoisting a validation out of a Lua script into `pre_save_validate` so it fires before any eager index write. Cited in `ValidityField.pre_save_validate`; not changed here. |

No prior *failed* attempt at this conversion exists — #563 declined it without
attempting it. The "Why Previous Fixes Failed" section is therefore omitted.

## Evidence

This section is the plan's argument. Every claim below was verified by reading
the code at `3cf8c2d0`.

### E1 — `save_and_invalidate` already expresses both requirements

`supersession.py:396-442`:

```python
@staticmethod
def save_and_invalidate(new_instance, *, closes, at=None,
                        field_name=None, pipeline=None) -> SupersedeResult:
    old_member = _member_key(closes)
    ...
    return _save_and_close(..., mode="invalidate", identity_digest="",
                           old_member=old_member,
                           entry_point="save_and_invalidate")
```

- **Explicit `old_member`:** yes. `closes` is named by the caller and passed
  straight through as `old_member`; the docstring is explicit that "being named
  makes it a caller *assertion* — a `closes` that does not exist at EXEC time
  raises rather than being read as 'no incumbent'". That is exactly the
  semantics `_write` relies on (`provenance_journal.py:1168-1174` documents the
  same assertion behavior).
- **`assert_valid_from=False`:** yes, and unconditionally.
  `_save_and_close` (`supersession.py:670-685`) passes
  `assert_valid_from=False` with the comment "Plan D3: `at` is a close-time
  assertion about the incumbent, never a start-time assertion about the
  successor."

Requirement (1) and (2) from the issue are both already satisfied. The in-code
comment at `provenance_journal.py:1119-1134` is out of date.

### E2 — the wire-level argument vectors are identical

`_write` (`provenance_journal.py:1135-1146`) vs. `_save_and_close`
(`supersession.py:670-685`), both against `ValidityField.execute_supersede`:

| `execute_supersede` arg | `_write` today | `_save_and_close` | Same? |
|---|---|---|---|
| `new_member` | `entry.db_key.redis_key` | `_member_key(new_instance)` → same attribute | ✅ |
| `mode` | `"invalidate"` | `"invalidate"` | ✅ |
| `now` (ARGV[2]) | `instant` | `clock` (real now) | ⚠️ see E3 |
| `valid_from` (ARGV[3]) | `instant` | `at` → `instant` | ✅ |
| `ingested_at` (ARGV[4]) | `instant` | `clock` | ⚠️ see E4 |
| `close_at` (ARGV[6]) | `instant` | `at` → `instant` | ✅ |
| `old_member` (ARGV[7]) | `target_key` | `_member_key(closes)` | ✅ if `closes` is the target instance (E5) |
| `identity_digest` | omitted → `""` | `""` | ✅ |
| `assert_valid_from` (ARGV[8]) | omitted → `False` | `False` | ✅ |
| `pipeline` | `pipe` | `pipe` | ✅ |

### E3 — `now` is inert on this path

`SUPERSEDE_LUA` (`validity_field.py:308, 313-316`) reads `now` only as the
**fallback** for the three time arguments:

```lua
local now = tonumber(ARGV[2] or '') or 0
local valid_from  = tonumber(ARGV[3] or '') or now
local ingested_at = tonumber(ARGV[4] or '') or now
local close_at    = tonumber(ARGV[6] or '') or now
```

`now` appears nowhere else in the script. Both call sites pass all three of
`valid_from` / `ingested_at` / `close_at` explicitly, so `now` is never
consulted. The `instant`→`clock` change is unobservable.

### E4 — `ingested_at` is already a no-op on this path

The script writes ingest time as `redis.call('ZADD', ig_key, 'NX', ingested_at, new_member)`
(`validity_field.py:401`) — **`NX`**. `ValidityField.on_save` runs during
`entry.save(pipeline=pipe)`, which is queued *earlier in the same MULTI*, and it
already routes through `SUPERSEDE_LUA` in `mode="open"` with
`ingested_at=now` (the real save clock, `validity_field.py` `on_save`). By the
time the `invalidate` EVAL runs, `ig_key` already holds a score for
`new_member`, so its `ZADD NX` is a no-op.

This holds identically for both spellings, so `ingested_at=instant` (today) and
`ingested_at=clock` (`_save_and_close`) produce the same stored state. Verified
mechanically by S1 below.

### E5 — the target instance is already loaded by the pre-flight

`save_and_invalidate` takes an *instance* (`closes`), not a raw key. `_write`
holds a raw `target_key`. But the pre-flight already hydrates it:

```python
stored_target = model.query.get(redis_key=target_key)   # :980
```

and refuses the write when it is `None` or belongs to another agent. `should_close`
is `model.kind_is_closing(kind) and target_key is not None`, and the pre-flight
block that loads `stored_target` is guarded by the *same* `target_key is not
None`, so on every path that reaches the close, `stored_target` is a live
instance. `_member_key(stored_target)` reads `stored_target.db_key.redis_key`,
which is the key it was fetched by. No new Redis command, no API change.

### E6 — the pipeline validators are the same three checks

`_validate_caller_pipeline` (`supersession.py`) carries a docstring stating it is
"the same three checks, and the same reasoning, as `ProvenanceJournal._write`'s
pre-flight": `isinstance(Pipeline)`, `transaction is True`, and
`watching and not explicit_transaction`. `_write` must keep its own copy because
it validates on the *non-closing* path too (a plain append with a caller
pipeline never reaches the protocol). The protocol's re-check is then guaranteed
to pass — redundant, cheap, and not observable.

Only the error *messages* differ (`ProvenanceJournal:` prefix vs.
`SupersessionProtocol.save_and_invalidate:`). Because `_write`'s validator runs
first and raises, the journal's messages are the ones callers see. Verified by
S3.

### E7 — one real behavioral gap: the declined-save error

This is the only genuine incompatibility.

`_write` (`provenance_journal.py:1075-1096`) checks **two** things after the
save and raises `RuntimeError("... the annotation was not written -- ...")`:

```python
blocked = getattr(entry, "_never_record_verdict", None)
if not saved or blocked is not None:
```

`_save_and_close` (`supersession.py:651-658`) checks only `not saved` and raises
`RuntimeError("SupersessionProtocol.save_and_invalidate: save() of ... was declined ...")`.

Two consequences:

1. **A message change.** `tests/test_provenance_journal.py:1078-1100`
   (`test_a_save_that_returns_falsy_raises_instead_of_closing_the_target`)
   monkeypatches `JournalEntry.save` to return `False` and asserts
   `pytest.raises(RuntimeError, match="annotation was not written")`. A naive
   conversion changes that text and breaks the oracle.
2. **A missing check.** `_save_and_close` does not inspect
   `_never_record_verdict`, so the truthy-return-with-verdict case (a firewall
   gate that returns the pipeline rather than `False`) would slip past it.

Both are fixable inside `_save_and_close` without touching the journal's
contract — see Solution S-2.

### E8 — the non-closing path stays hand-assembled

`_write` saves the entry on *every* path; the close happens only when
`should_close and coupling_enabled`. `save_and_invalidate` always closes, so the
conversion necessarily produces two save spellings in one function:

```python
if should_close and coupling_enabled:
    result = SupersessionProtocol.save_and_invalidate(entry, closes=stored_target, ...)
else:
    saved = entry.save(pipeline=pipe)
```

This is the honest cost of the conversion and the strongest argument the "record
a permanent decision" option had. It is accepted rather than dismissed: the
duplication is two lines, both of which are `save` against the same pipe, and
the post-save verdict check is hoisted into `_save_and_close` (S-2) so the two
paths cannot drift on the part that actually matters.

## Spike Results

### spike-S1: are the two spellings byte-identical on the wire?

- **Assumption:** routing the close through `save_and_invalidate` issues the
  same Redis commands, in the same order, with the same arguments, and leaves
  the same stored state.
- **Method:** prototype. A throwaway pytest module built a target + entry twice,
  once via the current hand-assembled spelling and once via
  `save_and_invalidate`, both onto a caller pipeline with the same `instant`,
  and compared `pipe.command_stack` before `execute()` plus the resulting index
  scores after.
- **Environment:** worktree `.worktrees/sdlc-606`, Python 3.12.14,
  redis-py 8.1.0, pytest 9.1.1, `POPOTO_TEST_DB=9`.
- **Result: CONFIRMED, with the two predicted inert deltas and nothing else.**
  - Command **name** sequence: identical.
  - Command **arity** sequence: identical.
  - `close_index`: identical (both `8`).
  - Close `EVALSHA` argument vector: identical at every position except
    **ARGV[2] (`now`)** and **ARGV[4] (`ingested_at`)** — exactly the two
    positions E3 and E4 predicted, and no others. `numkeys=6`, all six KEYS,
    `new_member`, `valid_from`, `mode='invalidate'`, `close_at`, `old_member`
    and `assert_valid_from=''` all matched.
  - Resulting state, both spellings: `valid_from[entry] == instant`,
    `invalid_at[target] == instant`, `invalid_at[entry] == +inf`,
    `chain:fwd[target] == entry`, `chain:rev[entry] == target`.
  - **E4 proved mechanically:** `ingested_at[entry]` came back as the *real save
    clock* in both runs, not the `instant` that the current spelling passes as
    ARGV[4]. The differing argument is swallowed by the script's `ZADD ... NX`,
    because `ValidityField.on_save` already wrote that member earlier in the
    same MULTI. Both spellings therefore store the same ingest time.
- **Confidence: high.** Measured, not reasoned.
- **Impact if false:** the plan flips to the "record a permanent decision"
  branch of the issue's acceptance criteria. It is not false.

No other spikes were needed: every remaining assumption (E1, E5, E6, E7) is a
direct read of code quoted above rather than a question about runtime behavior.

## Solution

Convert, and close the two gaps E7 identifies inside `SupersessionProtocol` so
the conversion is text- and semantics-preserving.

**S-1 — `_write` routes the closing branch through `save_and_invalidate`.**

In `provenance_journal.py`, keep the whole pre-flight, the pipeline validator,
`coupling_enabled` / `should_close`, the `target_key is None` RuntimeError guard,
the caller-pipeline `AnnotationResult` contract, the `pipe.execute()` and its
`map_lua_error` remap. Replace only the `entry.save(pipeline=pipe)` +
`ValidityField.execute_supersede(...)` pair on the closing branch with a single
`SupersessionProtocol.save_and_invalidate(entry, closes=stored_target,
at=instant, field_name=VALIDITY_FIELD_NAME, pipeline=pipe)`, taking
`close_index` from the returned `SupersedeResult.close_index`.

`stored_target` must be lifted out of the `if target_key is not None:` pre-flight
block into a function-scope local initialized to `None`, so the close branch can
read it.

**S-2 — `_save_and_close` gains the two things E7 found missing.**

- Also inspect `new_instance._never_record_verdict` after `save()`, matching
  `_write`'s two-part condition. This is a strictly stronger guard for every
  existing `save_and_supersede` / `save_and_invalidate` caller.
- Raise a *typed* `SupersedeDeclinedError(RuntimeError)` instead of a bare
  `RuntimeError`, carrying the declining verdict when there is one. `_write`
  catches it and re-raises its own `RuntimeError` with the existing
  "annotation was not written" text and reason string, so
  `tests/test_provenance_journal.py:1094` is untouched. Subclassing
  `RuntimeError` keeps every existing `pytest.raises(RuntimeError, ...)` on the
  protocol side green.

**S-3 — replace the stale comment.**

`provenance_journal.py:1119-1134` becomes a short note recording *why the
journal still owns the pre-flight and the execute* (firewall, cross-agent
ownership, kind/target consistency, the backdate pre-read, `AnnotationResult`),
and pointing at #606 as the decision record. The two claims that are no longer
true — "the protocol does not offer an explicit `old_member`" and "…or
`assert_valid_from=False`" — are deleted, not softened.

## Rabbit Holes

- **Do not unify `_write`'s pipeline validator with `_validate_caller_pipeline`.**
  They are duplicated on purpose (E6): the journal validates on the non-closing
  path too, and its error text names the model. Deduplicating means either the
  journal loses its message or the protocol gains a caller-supplied prefix
  parameter. Both are worse than three duplicated `if`s.
- **Do not convert the `mode="open"` path.** `ValidityField.on_save` also calls
  `execute_supersede`; that is the field's own internal use, not a recipe
  bypassing the protocol, and it is out of scope.
- **Do not touch `#630`'s four sites** (`exists`, `zscore`, `pipeline()`, the
  public `pipeline=` parameter) in this PR. They are the next PR in this module
  and merging them together would make either review harder.
- **Do not "improve" `AnnotationResult` to wrap or subclass `SupersedeResult`.**
  They answer different questions (`target_closed: bool | None` vs.
  `closed_key: str | None`) and the journal's is public API.

## No-Gos

- No change to the annotate-and-close atomicity guarantee: one MULTI/EXEC,
  entry HSET and interval close in the same transaction, in the same order.
- No change to any existing test expectation. `tests/test_provenance_journal.py`
  and `tests/test_validity_field.py` are the oracle and stay byte-identical.
- No new public API on `ProvenanceJournal`, and no signature change to
  `save_and_invalidate` / `save_and_supersede`.
- No removal of any D7 pre-flight check.

## Data Flow

```
ProvenanceJournal.supersede/confirm/retract
  └─ _write(...)
       1. _require_journal_shape / kind & target consistency
       2. JournalEntry(...) constructed with validity=instant      ← valid-time written here
       3. _scan_or_block(agent_id, *subject_tags, *entry._never_record_scan_values())
       4. target exists?  →  POPOTO_REDIS_DB.exists(target_key)
          stored_target = model.query.get(redis_key=target_key)    ← NOW ALSO the `closes` arg
          cross-agent ownership check
       5. backdate pre-read (zscore on valid_from index)
       6. caller-pipeline validation (3 checks)
       ── end pre-flight ──
       7. pipe = caller's or POPOTO_REDIS_DB.pipeline()
       8a. closing branch:
             SupersessionProtocol.save_and_invalidate(
                 entry, closes=stored_target, at=instant,
                 field_name="validity", pipeline=pipe)
               └─ new_instance.save(pipeline=pipe)
                    └─ ValidityField.on_save → EVAL SUPERSEDE_LUA mode='open'
                          ZADD vf NX / ZADD ig NX / ZADD ia NX +inf
               └─ declined-save guard (saved + _never_record_verdict)   ← S-2
               └─ close_index = len(pipe.command_stack)
               └─ ValidityField.execute_supersede(mode='invalidate',
                      old_member=stored_target.db_key.redis_key,
                      valid_from=close_at=instant, assert_valid_from=False)
                    └─ EVAL SUPERSEDE_LUA → ZADD ia close_at old_member
                                            HSET fwd/rev chain links
       8b. non-closing branch: entry.save(pipeline=pipe)
       9. caller owns pipe → AnnotationResult(target_closed=None, close_index=...)
      10. else pipe.execute() → map_lua_error on ResponseError
          target_closed = bool(results[close_index])
```

The command sequence issued to Redis is unchanged in both count and order. The
only difference is which Python frame issues each one.

## Risks

| Risk | Mitigation |
|---|---|
| ~~`_save_and_close`'s `clock` differs from the journal's `instant` in a way E3/E4 missed.~~ | **Retired by spike-S1** — measured identical on the wire and in stored state. |
| The new `_never_record_verdict` check in `_save_and_close` breaks an existing `save_and_supersede` caller. | The check only fires where the *old* code would already have queued a close behind an unwritten record — a bug in every case. Full `tests/test_validity_field.py` run is the check. |
| `SupersedeDeclinedError` is a behavior change for direct protocol callers. | It subclasses `RuntimeError`; every existing `except RuntimeError` / `pytest.raises(RuntimeError)` still matches. Exported from `popoto.fields.supersession` only; not added to the top-level `popoto` namespace in this PR. |
| Line drift collides with #630 PR 2 in this module. | #630 PR 2 is sequenced *after* this merges and rebases on it (same lane, strictly serial). |
| DB-15 contention producing phantom failures. | Every gate runs with `POPOTO_TEST_DB=9`, stated with each count. |

## Success Criteria

Environment for every count below: worktree
`/Users/valorengels/src/popoto/.worktrees/sdlc-606`, Python 3.12.14,
redis-py 8.1.0, pytest 9.1.1, `POPOTO_TEST_DB=9`, editable install of this
checkout verified via `python -c "import popoto; print(popoto.__file__)"`.

- **Baseline (measured at `3cf8c2d0`, before any change): `tests/test_provenance_journal.py`
  + `tests/test_validity_field.py` = 237 passed.** The same 237 must pass after,
  with zero test-file edits to those two files.
- `ruff check src/` exits 0.
- `black --check src/ tests/` exits 0.
- `scripts/mypy_ratchet.py` does not rise above baseline.
- `grep -n "execute_supersede" src/popoto/recipes/provenance_journal.py` returns
  only comment/prose lines — no call.
- S1 spike passes: the queued command stacks of old and new spellings are
  byte-identical.
- No `xfail`/`skip` added or removed.

No expected-failure markers exist for this area (`grep -rn 'pytest.mark.xfail\|pytest.xfail('
tests/test_provenance_journal.py tests/test_validity_field.py` → no matches), so
there is nothing to convert.

## Step by Step Tasks

0. ~~S1 spike~~ — **done at plan time, passed.** See Spike Results.
1. Add `SupersedeDeclinedError(RuntimeError)` to
   `src/popoto/fields/supersession.py` (or the module where the other
   supersession errors live — check `exceptions.py` first and follow it).
2. Extend `_save_and_close` to check `_never_record_verdict` alongside `not
   saved`, and raise `SupersedeDeclinedError` carrying the verdict.
3. Lift `stored_target` to a function-scope local in `_write`, initialized
   `None`, assigned in the existing pre-flight block. No new Redis read.
4. Replace the closing branch's `entry.save` + `execute_supersede` pair with
   `SupersessionProtocol.save_and_invalidate(...)`, wrapped in
   `except SupersedeDeclinedError` → re-raise the existing
   "annotation was not written" `RuntimeError` with the same reason string.
   Take `close_index` from `SupersedeResult.close_index`.
5. Keep `entry.save(pipeline=pipe)` + the existing declined-save `RuntimeError`
   on the non-closing branch.
6. Rewrite the `provenance_journal.py:1119-1134` comment per S-3.
7. Add the import of `SupersessionProtocol` to `provenance_journal.py`; confirm
   no import cycle (`recipes/` already imports from `fields/`).
8. Run `POPOTO_TEST_DB=9 pytest tests/test_provenance_journal.py
   tests/test_validity_field.py -q`; require 237 passed.
9. Run `ruff check src/`, `black --check src/ tests/`,
    `scripts/mypy_ratchet.py`.
10. `/do-docs`, then the merge gate.

## Documentation

- `docs/features/agent-memory.md` and any provenance-journal guide: check
  whether either documents the journal as calling `execute_supersede`; if so,
  update to name `save_and_invalidate`.
- `SupersessionProtocol.save_and_invalidate` docstring: add
  `SupersedeDeclinedError` to its `Raises:` block (and to
  `save_and_supersede`'s, which shares `_save_and_close`).
- Add a CHANGELOG entry if the repo keeps one for behavior-neutral internal
  changes; check first rather than assuming.

## Open Questions

None blocking. The one judgment call — accepting two save spellings in `_write`
(E8) as the price of routing the close through the supported API — is recorded
as a decision in the Solution rather than deferred, on the grounds that S-2
moves the part that can actually drift (the declined-save guard) into the shared
helper.
