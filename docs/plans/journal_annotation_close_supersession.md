---
status: Ready
type: chore
appetite: Small
owner: Tom Counsell
created: 2026-09-06
tracking: https://github.com/tomcounsell/popoto/issues/606
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-06T00:00:00Z
critique_verdict: READY TO BUILD (with concerns)
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
  `_write`'s two-part condition (`supersession.py:651-658` today checks only
  `if not saved:`; it becomes `if not saved or blocked is not None:`).
- Raise a *typed* `SupersedeDeclinedError(RuntimeError)` instead of a bare
  `RuntimeError`, carrying the declining verdict when there is one. `_write`
  catches it and re-raises its own `RuntimeError` with the existing
  "annotation was not written" text and reason string, so
  `tests/test_provenance_journal.py:1094` is untouched. Subclassing
  `RuntimeError` keeps every existing `pytest.raises(RuntimeError, ...)` on the
  protocol side green.

**Why the check goes in the shared helper and not in `_write` (critique round 1).**
All three critics flagged this placement, and two proposed keeping the check
local to `_write` instead. That option is rejected on ordering grounds and the
reason is recorded here so it is not re-litigated at review:

`_save_and_close` queues the close `EVAL` *before it returns*
(`supersession.py:669-685`). A verdict check performed by `_write` after the
call would therefore run with the close already queued. On the caller-owned
pipeline path `_write` raises and hands the caller a pipe carrying both the
declined save and the close — a caller who then executes commits a membership
change with no provenance behind it, which is precisely the failure this
module's backstop exists to prevent. Today's ordering (check between save and
close) is only reproducible inside the helper. So the check moves, and the two
objections are answered directly rather than by moving it back:

- **Duck typing (`getattr`) rather than `isinstance(NeverRecordMixin)`.**
  Deliberate, and it does not add a `fields/` → `privacy/` import. Verified in
  this tree: `_never_record_verdict` has exactly one producer,
  `privacy/never_record.py:608,674,679`, and two consumers, both already using
  the `getattr(..., None)` spelling —
  `recipes/provenance_journal.py:1085` and `recipes/subconscious_memory.py:538`.
  The `getattr` form is the established in-repo idiom for this attribute, not an
  improvisation. An `isinstance` guard would make `fields.supersession` import
  `privacy.never_record`, which imports `fields.constants` — not a true cycle,
  but a new `fields → privacy → fields` edge for zero behavioral gain.
- **Blast radius on other callers.** Measured, not assumed:
  `grep -rn "save_and_supersede\|save_and_invalidate" src/ tests/` finds **zero
  production call sites** outside `supersession.py` itself — every other hit is
  a comment or docstring (`models/base.py:1442`,
  `recipes/provenance_journal.py:1127`, `fields/validity_field.py:973`). The
  only live callers are six tests in `tests/test_validity_field.py`
  (`:1759, :2047, :2064, :2082, :2089, :2096`). This is a hardening change with
  test-suite exposure, not a shipped-caller-breakage risk.
- **The new path gets its own direct coverage.** A new test in
  `tests/test_validity_field.py` calls `save_and_invalidate` with a
  firewall-blocked instance and asserts `SupersedeDeclinedError`, so the shared
  helper's new branch is not validated only transitively through the journal's
  wrapping `except`. Adding a test is not an edit to an existing expectation and
  does not violate the No-Go.

**S-3 — replace the stale comment.**

`provenance_journal.py:1119-1134` becomes a short note recording *why the
journal still owns the pre-flight and the execute* (firewall, cross-agent
ownership, kind/target consistency, the backdate pre-read, `AnnotationResult`),
and pointing at #606 as the decision record. The two claims that are no longer
true — "the protocol does not offer an explicit `old_member`" and "…or
`assert_valid_from=False`" — are deleted, not softened.

It must **not** claim the conversion achieves full protocol orchestration. The
non-closing branch still calls `entry.save(pipeline=pipe)` directly (E8) and
that is intended, so the new comment says so plainly — otherwise a future reader
re-opens it as a bug.

**S-4 — the defence-in-depth comment moves with the guard it explains.**

`provenance_journal.py:1072-1084` is a twelve-line comment sitting above a
single `blocked = getattr(entry, "_never_record_verdict", None)` check that
today covers both branches. After S-1/S-2 that guard exists in two places: the
non-closing branch keeps its copy in `_write`, and the closing branch's copy
lives inside `_save_and_close`. Left untouched, the explanation would sit above
only the non-closing copy and the closing one would ship with no rationale at
all. The comment is therefore split: a shortened form stays with `_write`'s
copy, and `_save_and_close` gains a comment carrying the same reasoning and a
back-reference to this module — matching the cross-reference style already used
at `provenance_journal.py:1119-1134`.

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
| The new `_never_record_verdict` check in `_save_and_close` breaks an existing caller. | **Measured, and the exposure is test-only.** `grep -rn "save_and_supersede\|save_and_invalidate" src/ tests/` finds zero production call sites outside `supersession.py`; the only live callers are six tests in `tests/test_validity_field.py` (`:1759, :2047, :2064, :2082, :2089, :2096`). The check also only fires where the old code would already have queued a close behind an unwritten record — a bug in every case. |
| `SupersedeDeclinedError` is a behavior change for direct protocol callers. | It subclasses `RuntimeError`; every existing `except RuntimeError` / `pytest.raises(RuntimeError)` still matches. **Revised at build time:** it *is* exported from the top-level `popoto` namespace and added to `__all__`, alongside `SupersedeResult`. Adding it there is what makes it catchable by an adopter without reaching into `popoto.fields.supersession`, and a purely additive export cannot break an existing caller. Recorded in `CHANGELOG.md` under `[Unreleased] Changed` and documented in `docs/features/validity-and-supersession.md`. |
| Line drift collides with #630 PR 2 in this module. | #630 PR 2 is sequenced *after* this merges and rebases on it (same lane, strictly serial). |
| DB-15 contention producing phantom failures. | Every gate runs with `POPOTO_TEST_DB=9`, stated with each count. |

## Success Criteria

Environment for every count below: worktree
`/Users/valorengels/src/popoto/.worktrees/sdlc-606`, Python 3.12.14,
redis-py 8.1.0, pytest 9.1.1, `POPOTO_TEST_DB=9`, editable install of this
checkout verified via `python -c "import popoto; print(popoto.__file__)"`.

- **Baseline (measured at `3cf8c2d0`, before any change): `tests/test_provenance_journal.py`
  + `tests/test_validity_field.py` = 237 passed.** All 237 must still pass, with
  **zero edits to any existing test's expectations**. `tests/test_provenance_journal.py`
  is untouched entirely. `tests/test_validity_field.py` gains exactly one new
  test (the `SupersedeDeclinedError` case from S-2), so the post-change count is
  **238 passed**.
- `ruff check src/` exits 0.
- `black --check src/ tests/` exits 0.
- `scripts/mypy_ratchet.py` does not rise above baseline.
- `grep -n "execute_supersede" src/popoto/recipes/provenance_journal.py` returns
  only comment/prose lines — no call.
- **Call-site inventory, re-run after the change:**
  `grep -rn "save_and_supersede\|save_and_invalidate" src/ tests/` — the only
  production call site outside `supersession.py` is the new one in
  `recipes/provenance_journal.py`. Any other production hit is unplanned and
  must be explained before merge.
- S1 spike passed at plan time (see Spike Results); not re-run in build.
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
6. Split the `provenance_journal.py:1072-1084` defence-in-depth comment per S-4:
   a shortened form stays with `_write`'s remaining copy of the guard, and
   `_save_and_close` gains a comment carrying the same reasoning plus a
   back-reference to `ProvenanceJournal._write`.
7. Rewrite the `provenance_journal.py:1119-1134` comment per S-3, including the
   explicit note that the non-closing branch stays outside the protocol.
8. Add one new test to `tests/test_validity_field.py`: call
   `SupersessionProtocol.save_and_invalidate` with a never-record-blocked
   instance and assert `SupersedeDeclinedError`. Do not touch any existing test.
9. ~~Add the import of `SupersessionProtocol`~~ — **not needed.** It is already
   imported at `provenance_journal.py:194` and used by `chain()` at `:872`.
   Verify, do not re-add.
10. Run `POPOTO_TEST_DB=9 pytest tests/test_provenance_journal.py
    tests/test_validity_field.py -q`; require **238 passed** (237 baseline + the
    one new test from step 8).
11. Run `ruff check src/`, `black --check src/ tests/`,
    `scripts/mypy_ratchet.py`, and the call-site inventory grep from Success
    Criteria.
12. `/do-docs`, then the merge gate.

## Documentation

- `docs/features/agent-memory.md` and any provenance-journal guide: check
  whether either documents the journal as calling `execute_supersede`; if so,
  update to name `save_and_invalidate`.
- `SupersessionProtocol.save_and_invalidate` docstring: add
  `SupersedeDeclinedError` to its `Raises:` block (and to
  `save_and_supersede`'s, which shares `_save_and_close`).
- Add a CHANGELOG entry if the repo keeps one for behavior-neutral internal
  changes; check first rather than assuming.

## Critique Round 1

**Depth:** FULL. **Mode:** independent roster (3 critics — Risk & Robustness,
Scope & Value, History & Consistency). **Verdict: READY TO BUILD (with concerns)**
— 0 blockers, 3 concerns, 3 nits. Revision applied below; per the lane's
one-round cap this routes to BUILD without re-critique.

Structural checks: required sections PASS (15/15 present, none empty); task
numbering PASS (0–12, no gaps); dependencies PASS (no `Depends On` graph);
file paths PASS (8/8 referenced paths exist); cross-references PASS (every
success criterion maps to a task; no No-Go or Rabbit Hole appears as planned
work).

| # | Severity | Critics | Finding | Resolution |
|---|---|---|---|---|
| C1 | CONCERN | Risk & Robustness, Scope & Value (independent) | Moving the `_never_record_verdict` check into the shared `_save_and_close` widens its blast radius to every protocol caller via a duck-typed `getattr` rather than an `isinstance(NeverRecordMixin)` guard; the plan never enumerated the other callers. | **Placement kept, justified, and bounded.** S-2 now records the ordering argument for why the check cannot live in `_write` (the close is queued before the helper returns, so a `_write`-side check would raise with the close already on a caller-owned pipe). The `getattr` spelling is kept and justified against the measured single producer (`privacy/never_record.py:608,674,679`) and the existing in-repo idiom (`recipes/subconscious_memory.py:538`), avoiding a new `fields → privacy` import edge. The call-site inventory was run: zero production callers outside `supersession.py`; six test callers named. Added as a Success Criterion. |
| C2 | CONCERN | Scope & Value | The stated motivation — the recipe should orchestrate over `SupersessionProtocol` — is only half achieved, since the non-closing branch still calls `entry.save()` directly (E8); "record the permanent decision" may be the better-scoped answer. | **Conversion stands, claim narrowed.** E8 already priced this and the S1 spike shows the conversion is provably neutral, so the trade is a real reduction in duplicated key/ARGV knowledge for two lines of branch duplication. S-3 now requires the new comment to state plainly that the non-closing branch stays outside the protocol, so it is not re-litigated as a bug later. |
| C3 | CONCERN | History & Consistency | The `provenance_journal.py:1072-1084` defence-in-depth comment explains a guard that becomes two guards; left in place it would document only the non-closing copy and orphan the one inside `_save_and_close`. | **Accepted.** New **S-4** and task 6 split the comment across both copies with a back-reference. |
| N1 | NIT | Risk & Robustness | Task 7 ("add the import of `SupersessionProtocol`") is a no-op — the import already exists at `provenance_journal.py:194`. | Accepted; task struck and replaced with a verify-only step 9. |
| N2 | NIT | Scope & Value | The new `SupersedeDeclinedError` path ships with no direct regression coverage; it is exercised only transitively through the journal's `except`. | Accepted; new task 8 adds one direct test to `tests/test_validity_field.py`, and the expected count moves 237 → 238. |
| N3 | NIT | History & Consistency | The Risks row overstated exposure by implying a shipped caller relies on the weaker check. | Accepted; row reworded with the measured inventory. |

All three critics independently confirmed the plan's central technical claims
(E1–E8 and spike-S1) against the source. No finding disputed the conversion's
correctness.

## Open Questions

None blocking. The one judgment call — accepting two save spellings in `_write`
(E8) as the price of routing the close through the supported API — is recorded
as a decision in the Solution rather than deferred, on the grounds that S-2
moves the part that can actually drift (the declined-save guard) into the shared
helper.
