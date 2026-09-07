---
status: Planning
type: chore
appetite: Small
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/662
---

# Retire `query.py`'s four decay KEYS layouts through `rank_decayed`

## Problem

`src/popoto/models/query.py` hand-builds the `KEYS` arrays for the two decay Lua
scripts at four places, across two methods:

| # | Method | Cyclic branch | Plain branch |
|---|---|---|---|
| 1 | `QueryBuilder.top_by_decay` | `query.py:635` | `query.py:654` |
| 2 | `QueryBuilder._materialize_decay_field` | `query.py:1606` | `query.py:1623` |

Each site is an `if isinstance(field, CyclicDecayField)` branch selecting between
two **incompatible** layouts:

- `DECAY_SCORE_LUA` — `KEYS[1]` zset, `KEYS[2]` confidence, `KEYS[3]` invalid_at,
  `KEYS[4]` valid_from.
- `CYCLIC_DECAY_LUA` — `KEYS[1]` zset, `KEYS[2]` cycles, `KEYS[3]` pressure,
  `KEYS[4]` confidence. No validity gate.

Getting the mapping wrong does not raise. From
`src/popoto/fields/decaying_sorted_field.py:78`:

> Confidence hash is `KEYS[2]` *in this script only*. The `CyclicDecayField` fork
> of this math binds `KEYS[2] = cycles` and `KEYS[3] = pressure`, so its
> confidence hash is `KEYS[4]`. The indices are deliberately different -- do not
> "unify" them: reusing `KEYS[2]` there would `cmsgpack.unpack` the cycles array
> as a confidence dict, which corrupts silently instead of erroring.

#648 (PR #656) built the seam that removes the need for callers to know any of
this: a polymorphic `rank_decayed`, overridden per class, so neither method body
contains an index it must not use. It retired the third call site
(`recipes/context_assembler.py`). These four copies are what remain.

**A second, separable defect surfaced while scoping this** — see Research
spike-2. The companion-key derivation in `query.py` is wrong for any subclass of
`CyclicDecayField`, and this refactor fixes it as an unavoidable consequence.

## Freshness Check

- `rank_decayed` exists on both classes as of `1d50bd83`
  (`decaying_sorted_field.py:302`, `cyclic_decay_field.py:417`) and is already
  the sole KEYS builder for the assembler path.
- `MODULATION_DISABLED` / `VALIDITY_GATE_DISABLED` are the declared "off"
  triples; `confidence_modulation_args` / `validity_gate_args` are the resolvers
  `query.py` already calls, and their tuple shapes match `rank_decayed`'s
  `confidence=` / `validity=` parameters exactly.
- `tests/test_validity_field.py::TestDecayEvalCallSites` inventories the
  `run_lua` decay call sites and asserts their numkeys. It currently lists the
  two `query.py` methods plus `DecayingSortedField.rank_decayed`; retiring the
  `query.py` copies changes what that inventory should contain.
- `tests/test_validity_field.py::TestCyclicDecayGatingGap` pins the ungated
  cyclic path as an intentional No-Go.
- Verified on branch `session/sdlc-662` off `origin/main` at `42e800e6`.

## Prior Art

- **#648 / PR #656** — built `rank_decayed`; retired the assembler's pair.
  Establishes the polymorphic-split design and the parity-oracle method.
- **#653 / #647** — rejected a backend-level `td_update` op; the storage-backend
  seam does not exist. Same conclusion applies here: the field is the seam.
- **#642** — reviewer-authored commits invalidate their own verdict trailer.
  Drives the pipeline ordering in the last section.
- **#630** — the umbrella, now closed. This is its last outstanding debt.

## Research

### spike-1: Is every one of the four calls expressible through `rank_decayed`?

Yes, with `n` passed explicitly at all four. Argument-by-argument, per site:

| current arg | `rank_decayed` parameter | identical? |
|---|---|---|
| `sortedset_db_key.redis_key` | `zset_key` | yes, same expression |
| `conf_hash_key, conf_s, conf_c0` | `confidence=(...)` | yes, same resolver output |
| `gate_invalid_key, gate_valid_key, gate_as_of` | `validity=(...)` | yes, plain branch only |
| `str(now)` | `now=` (stringified inside) | yes |
| `str(effective_decay_rate)` / `str(field.decay_rate)` | `decay_rate=` | yes |
| `str(n)` / `str(999999)` | `n=` | yes **iff passed explicitly** |
| `effective_base_score_field` / `base_score_field` | `base_score_field=` | yes |

The one hazard is `n`. `rank_decayed(n=None)` issues a `ZCARD` first and
short-circuits an empty set to `[]` with no `EVAL`. `_materialize_decay_field`
currently passes a `str(999999)` "get all members" sentinel and always issues the
`EVAL`. **Decision: keep the sentinel** — pass `n=999999` explicitly. This makes
the swap byte-identical and needs no re-baselining. Converting the sentinel to
`n=None` would be a separate, wire-visible change with its own justification
burden, and is out of scope here (No-Go 3).

`top_by_decay` already guarantees `n > 0` via the `if n <= 0: return []` guard at
`query.py:567`, so passing its `n` through is likewise unconditional.

### spike-2: Is the cyclic companion-key derivation the same in both paths?

**No — and the difference is a live bug.** `rank_decayed` derives the companion
hashes as `zset_key + ":cycles"` / `+ ":pressure"`. `query.py` instead calls
`CyclicDecayField.get_cycles_hash_key_from_parts(model_class, ...)`, hard-bound
to the `CyclicDecayField` class, while building the zset key from
`field.__class__`.

`FieldBase` auto-assigns a **distinct** `field_class_key` per class
(`fields/field.py:142`, `DB_key(f"${name.strip('Field')}F")`) and enforces
uniqueness at `:143-149`. So for any subclass, the two derivations diverge.
Measured (Python 3.12.14, redis-py 8.1.0, DB 4):

```
class MyCyclic(CyclicDecayField): pass

field.__class__ zset key            : $MyCyclicF:M:relevance
zset + ":cycles"  (rank_decayed)    : $MyCyclicF:M:relevance:cycles
CyclicDecayField.from_parts (query) : $CyclicDecayF:M:relevance:cycles   <-- differs
on_save writes to (instance path)   : $MyCyclicF:M:relevance:cycles
```

`on_save` uses the instance path, so **writes** land under `$MyCyclicF:` while
**query reads** look under `$CyclicDecayF:`. The Lua script short-circuits on a
nil `HGET`, so a subclassed `CyclicDecayField` silently degrades to plain decay
at query time: no cycles, no pressure, no error. `rank_decayed`'s derivation
agrees with `on_save` and is the correct one.

This refactor therefore **fixes** that defect, because adopting `rank_decayed` at
these sites necessarily adopts its derivation. Preserving the bug would require
deliberately special-casing it, which is not defensible. Consequence for parity:
byte-identical for `CyclicDecayField` itself (`field.__class__ is
CyclicDecayField`, the only shape in the suite, the docs, and the recipes), and
intentionally divergent for subclasses. Both are proven, not assumed — see
Parity Validation.

### spike-3: Can the two branches collapse into one call?

Yes. `validity_gate_args` is already called unconditionally at `query.py:622`
(and `:1594`), *before* the isinstance branch, so the gate triple is computed on
the cyclic path today and simply not passed to the script. `rank_decayed`'s
cyclic override accepts `validity=` and deliberately ignores it. So both sites
reduce to a single unconditional `field.rank_decayed(...)`, with dispatch doing
the work the `isinstance` branch did.

This deletes the `isinstance(field, CyclicDecayField)` branch at both sites, and
with it the `CYCLIC_DECAY_LUA` / `DECAY_SCORE_LUA` / `CyclicDecayField` imports
that only the branch needed. `top_by_decay` keeps its `DecayingSortedField`
import — used by the field-name inference scan at `:529` and the type check at
`:551`, both unrelated.

### spike-4: What documents the cyclic gating gap, and where must it live after?

The 16-line comment at `query.py:606-621` explains that a direct `top_by_decay`
on a `CyclicDecayField` returns superseded records, names
`TestCyclicDecayGatingGap` and the "Known limitations" docs section, and says
"if you gate the cyclic script, update all three." The gap is unchanged by this
refactor — it just relocates from *"this branch dispatches to the ungated
script"* to *"the cyclic override ignores `validity=`"*. The comment must be
rewritten to describe the new mechanism, **not deleted**: it is the only place a
`top_by_decay` reader learns the gate does not apply to them.

## Data Flow

Before, per site:

```
QueryBuilder
  -> confidence_modulation_args()  -> (key, s, c0)
  -> validity_gate_args()          -> (invalid, valid, as_of)
  -> isinstance(field, CyclicDecayField)?
       yes -> CyclicDecayField.get_cycles_hash_key_from_parts()    [wrong cls]
           -> CyclicDecayField.get_pressure_hash_key_from_parts()  [wrong cls]
           -> run_lua(CYCLIC_DECAY_LUA, 4, zset, cycles, pressure, conf, ...)
       no  -> run_lua(DECAY_SCORE_LUA, 4, zset, conf, invalid, valid, ...)
```

After, per site:

```
QueryBuilder
  -> confidence_modulation_args()  -> (key, s, c0)
  -> validity_gate_args()          -> (invalid, valid, as_of)
  -> field.rank_decayed(zset_key, now=, n=, confidence=, validity=,
                        decay_rate=, base_score_field=)
       -> DecayingSortedField.rank_decayed  -> run_lua(DECAY_SCORE_LUA, 4, ...)
       -> CyclicDecayField.rank_decayed     -> run_lua(CYCLIC_DECAY_LUA, 4, ...)
          (companion keys derived from zset_key; validity accepted, ignored)
```

The `KEYS` arrays exist in exactly one place each afterwards.

## Appetite

Small, with a caveat worth stating (critique NIT-1). The refactor proper is two
call sites and a mechanical substitution whose correctness argument is already
made. The size is inflated above a typical Small by the spike-2 bug fix riding
along — it brings its own regression test, its own parity path, a `### Fixed`
changelog entry, and a doc update. Kept at Small because the *risk* profile is
Small (no new abstraction, no signature change, an existing seam), not because
the file count is.

## Prerequisites

- #648 merged (`1d50bd83`) — `rank_decayed` exists. Satisfied.
- #630 closed; no other lane holds `models/query.py`. Confirmed by the PM.

## Solution

### Key Elements

1. **`top_by_decay`** — replace the `isinstance` branch (`query.py:626-673`)
   with one `field.rank_decayed(...)` call passing `n=n`,
   `decay_rate=effective_decay_rate`,
   `base_score_field=effective_base_score_field`, and both triples. Delete the
   now-dead `cycles_hash_key` / `pressure_hash_key` locals and the
   `CYCLIC_DECAY_LUA` / `DECAY_SCORE_LUA` / `CyclicDecayField` imports.
2. **`_materialize_decay_field`** — same substitution at `query.py:1599-1640`,
   passing `n=999999` to preserve the sentinel exactly. `decay_rate` and
   `base_score_field` are omitted: `field` *is* the receiver, so
   `self.decay_rate` and `self.base_score_field or ""` are the same values the
   current code passes, by identity rather than by coincidence.
3. **Rewrite the gating-gap comment** at `query.py:606-621` to locate the gap in
   `CyclicDecayField.rank_decayed`'s ignored `validity=` rather than in a
   dispatch branch that no longer exists. Keep the three-place update note.
4. **Update `TestDecayEvalCallSites`** — the inventory should list the two
   `rank_decayed` implementations, not the retired `query.py` copies. This is
   the inventory following the code, the same relocation #648 made.

   **Both tests in the class break, not one** (critique BLOCKER-1). Precisely:

   - `test_decay_eval_call_sites_pass_four_keys` (`tests/test_validity_field.py:920-938`)
     builds a `sites` dict containing `QueryBuilder.top_by_decay` and
     `QueryBuilder._materialize_decay_field`, and asserts
     `len(numkeys) == 1` for each. After the substitution both sources contain
     zero `run_lua(DECAY_SCORE_LUA, ...)` matches, so both assertions fail.
     Fix: drop the two `query.py` entries, keep
     `DecayingSortedField.rank_decayed`. The numkeys assertions themselves are
     untouched.
   - `test_cyclic_decay_lua_sites_are_not_matched` (`:945-964`) opens with
     `assert "CYCLIC_DECAY_LUA" in inspect.getsource(QueryBuilder.top_by_decay)`
     followed by `assert len(_decay_eval_numkeys(...top_by_decay...)) == 1`.
     Both invert. Fix: replace that pair with the **absence** claim — neither
     script literal appears in `top_by_decay` any more — and keep the existing
     `CyclicDecayField.rank_decayed` half (`:959-964`, including
     `assert _decay_eval_numkeys(cyclic_source) == []` and
     `assert "DECAY_SCORE_LUA" not in cyclic_source`) **exactly as is**. That
     half is the executable form of the "do not unify" rule and must not weaken.
   - That test's **docstring is also stale**: it says "``top_by_decay`` still
     holds both scripts in one body." Rewrite it, or the test documents the
     opposite of what it now asserts.

   Note the distinction this makes from #648: there, the change was purely a
   relocation of an inventory row. Here one assertion genuinely **inverts**
   (presence becomes absence) because the thing it asserted the presence of is
   what the issue exists to delete. That is still the inventory following the
   code, but it is a stronger claim and is called out rather than glossed.
   `test_query_top_by_decay_delegates_rather_than_evaluating` (`:966-969`)
   targets `Query.top_by_decay`, the thin wrapper, and is unaffected.
5. **Add a regression test for the subclass companion-key fix**, so the spike-2
   defect cannot come back.

### Technical Approach

Straight substitution, one site at a time, with a parity capture after each so a
regression is attributable to a single edit. No signature changes to
`rank_decayed` — it was designed for exactly these callers, and needing to change
it would be evidence the design was wrong.

## Test Impact

- `tests/test_validity_field.py::TestDecayEvalCallSites` — **both** tests in the
  class need edits; see Key Elements item 4 for the exact assertions and line
  numbers. `test_decay_eval_call_sites_pass_four_keys` loses its two `query.py`
  inventory entries (numkeys assertions unchanged);
  `test_cyclic_decay_lua_sites_are_not_matched` has its first two assertions
  inverted from presence to absence and its docstring rewritten, while its
  `CyclicDecayField.rank_decayed` half stays byte-identical.
- `tests/test_validity_field.py::TestCyclicDecayGatingGap` — **must not change.**
  If it needs changing, the No-Go was violated.
- New: `tests/test_cyclic_subclass_companion_keys.py` — asserts a
  `CyclicDecayField` subclass reads the companion hashes `on_save` wrote, i.e.
  that cycles/pressure survive subclassing. Fails on `42e800e6`, passes after.
- Regression scope (narrow, per lane discipline): `test_validity_field.py`,
  `test_decaying_sorted_field.py`, `test_cyclic_decay_field.py`,
  `test_composite_score_query.py`, `test_query_results.py`,
  `test_confidence_field.py`, `test_decay_rank_seam.py`,
  `test_context_assembler*.py`, plus the new file.
- Command-spy tests, if any are added, must patch **every distinct client
  object** and assert the capture is non-empty on its own line before any
  content assertion (the #656/#661 trap: field modules bind the client at import
  time, `tests/test_connection.py` rebinds the module attribute, and an empty
  capture diffs clean against another empty capture). Reproduce under
  full-suite ordering with `test_connection.py` ahead.

## Parity Validation

Base-vs-branch Redis command capture, base `42e800e6`, normalizing AutoKey
UUIDs, asserting **identical `EVALSHA` digests** (`lua_script()` caches the
registered `Script` keyed by exact text, so a comment edit inside a script body
would change the SHA — none is planned).

Paths, extending the #656 harness rather than rewriting it:

| path | exercises | expectation |
|---|---|---|
| A | `top_by_decay` on a plain `DecayingSortedField` | empty diff |
| B | `top_by_decay` on a `CyclicDecayField` | empty diff |
| C | `top_by_decay` with a `ValidityField` (gate on) | empty diff |
| D | `composite_score` → `_materialize_decay_field`, plain | empty diff |
| E | `composite_score` → `_materialize_decay_field`, cyclic | empty diff |
| F | `top_by_decay` with confidence modulation on | empty diff |
| G | `top_by_decay` on a **subclass** of `CyclicDecayField` | **diff expected** — companion key prefix changes from `$CyclicDecayF:` to the subclass's. This is the spike-2 fix; the diff is the evidence it landed. |

Path G is the only non-empty one, and it must be non-empty. A clean G would mean
the fix did not take effect.

## Rabbit Holes

- **Unifying the two Lua scripts.** Explicitly forbidden by both script headers.
  Not in scope, ever, in this issue.
- **Converting the `999999` sentinel to `n=None`.** Wire-visible, needs its own
  justification. No-Go 3.
- **Auditing the other 33 modules holding a stale `POPOTO_REDIS_DB` import**
  (#655). `rank_decayed` already uses the module-level name like everything
  around it; changing that here mixes two refactors.

## Risks

1. **The `n` sentinel.** Forgetting `n=999999` at `_materialize_decay_field`
   silently inserts a `ZCARD` and changes the wire sequence. Caught by parity
   paths D and E.
2. **Collapsing the branch drops the validity triple on the plain path.** Caught
   by parity path C, and by `TestDecayEvalCallSites`' numkeys assertion.
3. **The subclass fix is wider than measured.** Mitigated by making path G an
   explicit expected-diff rather than an unexamined one, and by the new
   regression test asserting the read/write keys agree.
4. **`_materialize_decay_field` omits `decay_rate` / `base_score_field`.** Safe
   only because `field` is the receiver. If a future caller passes a different
   field object, this silently changes. Mitigated by passing `zset_key` from the
   same `field.__class__` expression, keeping the coupling visible in one line.

## Race Conditions

None introduced. The substitution issues the same commands, in the same order,
from the same call sites; `rank_decayed` adds no state and holds no lock. The
`ZCARD`-then-`EVAL` non-atomicity of the `n=None` path is not reached, because
all four sites pass `n` explicitly.

## No-Gos (Out of Scope)

1. **Do not gate `CYCLIC_DECAY_LUA`.** `KEYS` 1-4 are taken and the header
   forbids renumbering. `TestCyclicDecayGatingGap` must pass unchanged.
2. **Do not unify the two KEYS layouts.** The class boundary is the enforcement
   mechanism; a shared body with a `layout=` flag relocates the hazard rather
   than removing it. (Rejected in #648's plan for the same reason.)
3. **Do not convert the `999999` sentinel.**
4. **Do not change `rank_decayed`'s signature.**
5. **Do not touch `fields/` export/import carriers** — `sdlc-556` owns them.
   Also out: `.github/workflows/` (sdlc-611), `scripts/mypy_baseline.json`,
   `setup.cfg`, `pytest_plugin.py` (sdlc-651).

## Documentation

- `docs/features/decaying-sorted-field.md` — the "Ranking a partition ZSET
  directly" section says the assembler uses `rank_decayed`; update to say the
  query path does too, and that the layouts now exist in one place each.
- `docs/features/cyclic-decay-field.md` — document the companion-key derivation
  rule (suffix of the zset key, so it follows the *field's own class*) and the
  subclass fix.
- `docs/features/validity-and-supersession.md` — the "Known limitations" entry
  for the cyclic gap must still describe where the gap lives after the branch is
  gone.
- `CHANGELOG.md` — `### Fixed` for the subclass companion-key defect (a
  user-visible behavior change) and `### Changed` for the deduplication.

## Success Criteria

1. `models/query.py` contains zero hand-built `KEYS` arrays for either decay
   script; `grep -c 'DECAY_SCORE_LUA\|CYCLIC_DECAY_LUA' src/popoto/models/query.py`
   returns only comment/doc references, no `run_lua` call.
2. The two layouts exist in exactly one place each, both inside the field class
   that owns the corresponding script.
3. Parity paths A-F empty-diff; path G non-empty and explained.
4. `TestCyclicDecayGatingGap` passes unchanged.
5. New subclass regression test fails on `42e800e6` and passes on the branch.
6. Narrow regression scope green; ruff, black, `mkdocs build --strict` clean.
7. mypy ratchet at or below the baseline **read from
   `scripts/mypy_baseline.json` at measurement time** — not assumed to be 1042,
   since #663 may re-bank it. State the number read and the environment.

## Step by Step Tasks

Commit boundaries are load-bearing here (critique CONCERN-1): the mechanical
substitution and the spike-2 behavior fix ship in the same PR but as **separate
commits**, so `git bisect` and `git revert` can isolate the user-visible fix from
the no-op refactor if either misbehaves after merge.

1. Substitute `top_by_decay` (both branches → one call); rewrite the gating-gap
   comment. Validate: parity A, B, C, F.
2. Substitute `_materialize_decay_field` with `n=999999`. Validate: parity D, E.
3. Remove now-dead imports and locals. Validate: `ruff check src/`.
4. Update **both** tests in `TestDecayEvalCallSites` per Key Elements item 4,
   including the stale docstring. Validate: `pytest tests/test_validity_field.py`.
   — *Commit 1 ends here: the mechanical refactor, green on its own.*
5. Add the subclass companion-key regression test and the `### Fixed` changelog
   entry; confirm the test fails on `42e800e6` and passes on the branch.
   Validate: parity G non-empty.
   — *Commit 2: the behavior fix's evidence, separable in history.*
6. Narrow regression scope + mypy ratchet + lint + docs build.

## Pipeline Sequencing (#642)

Patches and the docs cascade land as commits first, then re-review the delta,
then `verdict finalize` (head stable, trailer valid), then the DOCS marker, then
MERGE. The state print and `gh pr merge` go in **separate** tool calls. If a
marker refuses, report it and proceed with it missing — never mint a verdict.
