---
status: Planning
type: chore
appetite: Small
created: 2026-09-07
tracking: https://github.com/tomcounsell/popoto/issues/648
---

# Recipes through the field layer, PR 4: `context_assembler`

## Problem

`src/popoto/recipes/context_assembler.py` reaches past the field layer at seven
sites:

| Line | Site | What it knows that it should not |
| --- | --- | --- |
| 646 | `POPOTO_REDIS_DB.pipeline()` | how to open a transaction |
| 745 | `POPOTO_REDIS_DB.zcard(zkey)` | that cardinality is a `ZCARD` |
| 754 | `run_lua(CYCLIC_DECAY_LUA, 4, ...)` | **the cyclic KEYS layout** |
| 770 | `run_lua(DECAY_SCORE_LUA, 4, ...)` | **the decaying KEYS layout** |
| 1713 | `zrangebyscore(invalid_at_key, "-inf", t)` | the closed-interval read |
| 1715 | `zrangebyscore(valid_from_key, f"({t}", "+inf")` | the exclusive-bound syntax |
| 2556 | `POPOTO_REDIS_DB.pipeline()` | how to open a transaction |

The two `run_lua` sites are the reason this recipe was split out of #630 as "the
dangerous one". `DECAY_SCORE_LUA` carries this comment inside the script text:

```lua
-- Confidence hash is KEYS[2] *in this script only*. The CyclicDecayField fork
-- of this math binds KEYS[2] = cycles and KEYS[3] = pressure, so its confidence
-- hash is KEYS[4]. The indices are deliberately different -- do not "unify"
-- them: reusing KEYS[2] there would cmsgpack.unpack the cycles array as a
-- confidence dict, which corrupts silently instead of erroring.
```

The two scripts have **incompatible KEYS layouts by design**, and the failure
mode of confusing them is silent corruption, not an error. The recipe currently
holds both layouts in an `if is_cyclic:` branch inside
`_decayed_partition_scores`, ~60 lines apart, each with its own hand-written
`numkeys` literal and its own explanatory comment warning what happens if the
literal and the argument list drift. `models/query.py` (L626-673) holds a third
and fourth copy of the same two layouts. Four copies of a mapping whose
misapplication corrupts data silently is the defect; the recipe's copy is this
PR's share of it.

## Freshness Check

Verified 2026-09-07 against `1c302a7a` (worktree `.worktrees/sdlc-648`):

- All seven sites are present at the stated lines.
- The `do not "unify" them` comment is at `decaying_sorted_field.py:77-81`,
  inside the `DECAY_SCORE_LUA` string literal.
- `DecayingSortedField` and `CyclicDecayField` have **no** ranking method. Every
  `EVAL` of either script is built by a caller: `query.py:635`, `query.py:654`,
  `context_assembler.py:754`, `context_assembler.py:770`. There is no field-level
  seam to route through yet — this PR creates it.
- `ValidityField` has `get_interval_keys`, `resolve_valid_keys`, `is_valid_at`,
  `_members_valid_at`. It has no exclusion-set read; `_resolve_excluded_keys`
  lives on the recipe.
- `SortedFieldMixin.count(model_instance, field_name)` exists but takes a model
  **instance**; the recipe holds a bare partition ZSET key string, so `count()`
  does not fit the `zcard` site.
- `popoto.batch()` (`src/popoto/batch.py:40`) returns
  `POPOTO_REDIS_DB.pipeline(transaction=transaction)` with `transaction=True`
  by default, which is exactly what a bare `.pipeline()` returns.

## Prior Art

- **PR #634** (`default_memory`): established `popoto.batch()` for transactions
  and `Model.exists()` for existence probes.
- **PR #644** (`provenance_journal`): established the base-vs-branch Redis
  command-sequence capture as the neutrality proof.
- **PR #653** (`policy_cache`, merged as `1c302a7a`): relocated `TD_UPDATE_LUA`
  into a new `TDValueField`, and established that **script text must stay
  byte-identical** when relocating — `lua_script()` caches the registered
  `Script` keyed by exact text, so a comment edit changes the SHA and therefore
  the `EVALSHA` payload on the wire. Notes go in the module docstring, outside
  the literal.

This PR differs from #653 in one respect: #653 **moved** a script that a recipe
owned into a field that should own it. Here the field already owns both scripts.
What the recipe owns is the *invocation protocol* — the KEYS array. So nothing
moves; a method is added and the protocol knowledge moves into it.

## Research

### spike-1: Can the field build the KEYS array, or does the recipe hold something the field cannot see?

The recipe's `_decayed_partition_scores` passes, per eval group:

- `zkey` — a partition ZSET key it resolved via
  `field.get_partitioned_sortedset_db_key(record, field_name).redis_key`, i.e.
  already a field-layer call.
- `conf_hash_key, conf_s, conf_c0` — from `confidence_modulation_args(...)`, a
  module-level function in `decaying_sorted_field.py`. Already field-layer.
- `gate_invalid_key, gate_valid_key, gate_as_of` — from `validity_gate_args(...)`,
  same module. Already field-layer.
- `cardinality` — its own `ZCARD` on `zkey`.
- `now`, `field.decay_rate`, `field.base_score_field` — the field's own state.

Every input is either field-layer output or field state. **Nothing** the recipe
holds is invisible to the field. Answer: yes, the field can build the array.

### spike-2: Is the recipe's cyclic companion-key derivation the same as query.py's?

The recipe derives `zkey + ":cycles"` / `zkey + ":pressure"` (L759-760).
`query.py` calls `CyclicDecayField.get_cycles_hash_key_from_parts(...)`, whose
body is `cls.get_sortedset_db_key(...).redis_key + ":cycles"`
(`cyclic_decay_field.py:417-431`). The instance form
`get_cycles_hash_key` is `get_partitioned_sortedset_db_key(...).redis_key +
":cycles"`. All three are "the ZSET key plus a suffix", and the recipe's `zkey`
*is* the partitioned ZSET key. They agree. A field method that takes the ZSET
key and appends the suffixes reproduces both call sites exactly.

### spike-3: Do the two scripts differ in ARGV as well as KEYS?

Yes. `DECAY_SCORE_LUA` takes seven ARGV (the seventh being the validity gate's
`as_of`); `CYCLIC_DECAY_LUA` takes six and has **no** validity gate at all —
deliberately, per the No-Go recorded in `query.py:613-621` and pinned by
`tests/test_validity_field.py::TestCyclicDecayGatingGap`. So the seam must be
polymorphic on the field class, not a single function with a KEYS-layout
parameter: the cyclic variant must *drop* the validity arguments, not relocate
them.

### spike-4: Does anything pin the current wire sequence?

`tests/test_context_assembler.py` and the metacognitive-proxy no-drift test
`test_proxy_matches_top_by_decay` pin *scores*, not commands. Score identity is
implied by wire identity, so the parity capture (below) is the stronger oracle
and the tests remain a check on it, not a substitute.

## Data Flow

Before (recipe holds the protocol):

```
_decayed_partition_scores
  ├─ ZCARD zkey                          [recipe]
  ├─ if is_cyclic:  EVAL CYCLIC_DECAY_LUA 4 zkey zkey:cycles zkey:pressure conf
  │                      + 6 ARGV                                     [recipe]
  └─ else:          EVAL DECAY_SCORE_LUA  4 zkey conf invalid valid
                         + 7 ARGV                                     [recipe]
```

After (field holds the protocol):

```
_decayed_partition_scores
  └─ field.rank_decayed(zkey, now=..., confidence=..., validity=...)
       │
       ├─ DecayingSortedField.rank_decayed
       │    ├─ ZCARD zkey
       │    └─ EVAL DECAY_SCORE_LUA 4 zkey conf invalid valid + 7 ARGV
       │
       └─ CyclicDecayField.rank_decayed  (override)
            ├─ ZCARD zkey
            └─ EVAL CYCLIC_DECAY_LUA 4 zkey zkey:cycles zkey:pressure conf
                    + 6 ARGV   (validity args accepted and dropped)
```

The `if is_cyclic:` branch becomes method resolution. The recipe never names a
KEYS index again.

## Appetite

Small. One new method plus one override, one new `ValidityField` read method,
two `batch()` swaps, a new test module, a parity capture, and the docs cascade.

## Prerequisites

None. `popoto.batch()` and the `confidence_modulation_args` /
`validity_gate_args` resolvers already exist.

## Solution

### The design decision: a polymorphic field method, not a shared helper

#648 asks whether "the recipe calls the field's ranking method with the extra
keys/args it needs and the FIELD builds the KEYS array" actually works. It does,
and the polymorphic form is *the direct answer to the warning comment*.

The hazard the comment names is that one piece of code has to remember two
mutually incompatible index maps. Any design that keeps both maps in one
function — a `build_decay_keys(is_cyclic)` helper, a `layout=` parameter — moves
the hazard without reducing it, and does so into a place where a future reader
has less context, not more. Splitting the maps across the two classes that
*define* them means neither body contains an index it must not use:
`DecayingSortedField.rank_decayed` mentions confidence at position 2 and knows
nothing about cycles; `CyclicDecayField.rank_decayed` mentions cycles at 2,
pressure at 3, confidence at 4, and knows nothing about the validity gate. The
"do not unify" instruction is then enforced by class boundaries rather than by a
comment asking readers not to.

Rejected alternative — **a backend-level `decay_rank` op**. Same reasoning as
#653's rejection of a backend `td_update`: the storage-backend seam does not
exist yet (#630 is a prerequisite *for* it), and a power-law decay ranking with
confidence modulation and a validity gate is not a storage primitive.

Rejected alternative — **route `query.py` through the new method in this PR
too**. It is the obvious follow-up and would collapse all four copies to one,
but #648's contract is "one PR, this recipe only", and `top_by_decay` is a
public query path whose parity would need its own capture matrix. Filed as a
follow-up note in the PR body instead; the method is designed so query.py drops
in without changes to its signature.

### The validity read: an exclusion-set method, not a generic `window(lo, hi)`

#630 suggests a `window(lo, hi)` read method for the `zrangebyscore` pair. The
plan rejects that shape and proposes
`ValidityField.resolve_excluded_keys(model_class, field_name, as_of)` instead.

`window(lo, hi)` would leave the recipe holding everything that is actually
dangerous here: *which* of the two interval ZSETs to read, that the `valid_from`
bound is **exclusive** and therefore spelled `f"({t}"` while the `invalid_at`
bound is inclusive `"-inf"`/`t`, and that the `+inf` open sentinel must not
match. The recipe would still name `invalid_at` and `valid_from` and compose
Redis range syntax; it would just do it through a thinner wrapper. That is
motion, not a seam.

An exclusion-set method moves all of it, and — more importantly — it puts the
"THIS RETURNS AN EXCLUSION SET, NOT A WHITELIST" doctrine directly beside
`ValidityField.resolve_valid_keys`, the method that comment explicitly warns
against substituting. Today the warning lives in a recipe and the trap lives in
a field, in a different file. After this change they are adjacent.

The recipe keeps the two decisions that are recipe policy and not field policy:
whether the model declares a validity field at all, and the
`Defaults.VALIDITY_GATING_ENABLED` kill-switch read (which must stay at call
time, not import time).

### Key Elements

1. **`DecayingSortedField.rank_decayed`** (new instance method,
   `decaying_sorted_field.py`)

   ```
   rank_decayed(zset_key, *, now, n=None, confidence=MODULATION_DISABLED,
                validity=VALIDITY_GATE_DISABLED) -> list
   ```

   - `n=None` means "every member": the method issues the `ZCARD` itself and
     returns `[]` when the set is empty, **without** issuing the `EVAL`. This is
     what absorbs the recipe's `zcard` site and its `if not cardinality: continue`
     in one move, and it preserves the ZCARD-then-EVAL order on the wire.
   - `n=<int>` skips the `ZCARD` entirely, which is what `query.py` would need.
   - Returns the raw flat `[member, score, ...]` reply, undecoded. Decoding is
     already duplicated at both call sites and normalizing it would change what
     the recipe's `float()`/`bytes.decode()` loop sees. Out of scope.

2. **`CyclicDecayField.rank_decayed`** (override, `cyclic_decay_field.py`)

   Same signature. Builds `[zset, zset+":cycles", zset+":pressure", conf_hash]`
   and six ARGV. Accepts `validity=` and **ignores** it, with a docstring
   pointing at the `TestCyclicDecayGatingGap` No-Go so the drop is visibly
   deliberate rather than an oversight.

3. **Reuse `VALIDITY_GATE_DISABLED`** — the `("", "", "")` gate-off triple is
   **already a bound module-level constant** at `decaying_sorted_field.py:470`,
   consumed by `validity_gate_args` at L517 and L521. (An earlier draft of this
   plan claimed it was only documented in a comment; that was wrong — the
   critique caught it.) Nothing is added here: `rank_decayed` takes it as the
   default for `validity=`, exactly as it takes the existing
   `MODULATION_DISABLED` (`decaying_sorted_field.py:312`) for `confidence=`.
   **Do not redefine it** — a second binding of the same tuple would be
   harmless at runtime and actively confusing to read.

4. **`ValidityField.resolve_excluded_keys`** (new classmethod,
   `validity_field.py`) — derives both interval keys, issues the two
   `ZRANGEBYSCORE`s in the existing order, returns the decoded drop set.

5. **Two `popoto.batch()` swaps** — L646 and L2556.

### Technical Approach

- Neither script's text is touched. No `lua_script()` cache key moves, no
  `EVALSHA` payload changes.
- `run_lua` and `POPOTO_REDIS_DB` imports in `context_assembler.py` are trimmed
  to what remains in use; `OUTAGE_ERRORS` stays.
- Core commands only (`ZCARD`, `EVAL`/`EVALSHA`, `ZRANGEBYSCORE`, `MULTI`/`EXEC`).
  No Redis-module commands, so Valkey behavior is unchanged.

## Test Impact

**No existing test expectation may change.** New module
`tests/test_decay_rank_seam.py`:

1. `rank_decayed` on a plain `DecayingSortedField` returns the same members and
   scores as `Query.top_by_decay` for the same ZSET (the seam agrees with the
   existing caller).
2. `rank_decayed` with `n=None` on an empty ZSET returns `[]` and issues **zero**
   `EVAL`s (the `if not cardinality: continue` contract).
3. `rank_decayed` with `n=<int>` issues no `ZCARD`.
4. `CyclicDecayField.rank_decayed` honours cycles/pressure — non-equal to the
   plain-decay result for a model with cycle data.
5. `CyclicDecayField.rank_decayed` given a non-disabled `validity=` produces the
   same result as with the gate disabled (the deliberate drop, pinned).
6. `ValidityField.resolve_excluded_keys` returns closed ∪ future, and returns an
   **empty set** (not a superset) for a record absent from both ZSETs — the
   unmanaged-record rule.

Regression scope to run: `tests/test_context_assembler.py`,
`tests/test_validity_field.py`, `tests/test_decaying_sorted_field.py`,
`tests/test_cyclic_decay_field.py`, `tests/test_batch.py`,
`tests/test_agent_memory*.py`, plus the new module.

## Parity Validation

Capture the Redis command sequence base-vs-branch via a `MONITOR`-style
connection callback, for four paths:

1. `assemble()` on a model carrying **both** a plain `SortedField` and a
   `DecayingSortedField` in its `score_weights`, with no validity field
   (exercises L745, L770, L2556 — and L646).

   The two-field fixture is required, not incidental. `_partition_scores_for_field`
   returns early via `_decayed_partition_scores` whenever
   `isinstance(f, DecayingSortedField)` (`context_assembler.py:633-641`), and
   `CyclicDecayField` is a subclass (`cyclic_decay_field.py:208`) so it takes the
   same early return. **L646 is reachable only for a plain, non-decaying
   `SortedField`.** An earlier draft of this plan claimed a lone
   `DecayingSortedField` fixture would exercise L646; it cannot, and that would
   have left one of the two `batch()` swaps outside the empty-diff oracle
   entirely. Giving the fixture both fields drives both branches of
   `_partition_scores_for_field` in a single capture.
2. `assemble()` on a model with a `CyclicDecayField` (exercises L754).
3. `assemble()` on a model with a `ValidityField` (exercises L1713/L1715).
4. `assess()` / the metacognitive proxy path with confidence modulation on.

Normalize per-run AutoKey UUIDs. **The diff must be empty.** Capture scripts and
both outputs go in `scratchpad/sdlc-648-parity-*` (untracked).

## Rabbit Holes

- **Do not refactor `query.py`.** Four copies become two in this PR; the other
  two are a follow-up with its own parity matrix.
- **Do not gate `CYCLIC_DECAY_LUA`.** It is an explicit No-Go in three places.
- **Do not normalize the decode.** Returning decoded `(member, score)` tuples
  would be nicer and would change what both callers' parsing loops receive.
- **Do not "improve" the recipe's best-effort `except Exception` warnings.**

## Risks

**Risk 1 — the two layouts get crossed during the move.** The exact hazard the
warning names. Mitigated by writing each layout in the class that owns its
script and never in a shared body, and by the parity capture, which would show a
changed `EVAL` argument vector immediately.

**Risk 2 — `n=None` changes the wire order.** If the `ZCARD` moved after the
`EVAL`, or fired for `n=<int>`, the capture diff would be non-empty. Pinned by
tests 2 and 3.

**Risk 3 — the cyclic override silently gains a validity gate.** Test 5 pins the
drop.

**Risk 4 — ruff F401 on newly unused imports** in `context_assembler.py`.

**Risk 5 — mypy ratchet.** Baseline 1044 at mypy 2.3.1 / redis-py 8.1.0 /
Python 3.12. New code must not raise it; the `cast("list[Any]", ...)` narrowing
at L1721-1722 moves into `ValidityField` with the reads.

## Race Conditions

Unchanged. `resolve_excluded_keys` is still a point-in-time snapshot of a live
store — a supersession landing mid-`assemble()` is not reflected until the next
call, the same accepted property tag scoping has. Moving the two reads into the
field does not make them atomic with each other, and deliberately does not: a
`MULTI` around them would change the wire sequence.

## No-Gos (Out of Scope)

- Any change to `DECAY_SCORE_LUA` or `CYCLIC_DECAY_LUA` text.
- Any change to an existing test expectation.
- Routing `query.py` through the new method.
- Adding a validity gate to the cyclic script.
- A backend-level operation vocabulary.

## Documentation

- `docs/features/decaying-sorted-field.md` — the new `rank_decayed` seam and why
  the layouts are split across classes.
- `docs/features/validity-and-supersession.md` — `resolve_excluded_keys`
  alongside `resolve_valid_keys`, carrying the exclusion-not-whitelist doctrine.
- `docs/guides/context-assembler-recipe.md` (if present) — no behavior change to
  document; note the internal seam only.
- `CHANGELOG.md`.

## Success Criteria

1. All seven direct-Redis sites in `context_assembler.py` are gone.
2. The recipe names no Lua KEYS index.
3. Base-vs-branch Redis command capture diff is **empty** for all four paths.
4. No existing test expectation changed.
5. New tests pass; regression scope passes.
6. `ruff check src/` clean, `black --check src/ tests/` clean, mypy ratchet ≤ 1044.
7. Both CI jobs (Redis and Valkey) pass.

## Step by Step Tasks

1. `VALIDITY_GATE_DISABLED` constant + `DecayingSortedField.rank_decayed`.
2. `CyclicDecayField.rank_decayed` override.
3. `ValidityField.resolve_excluded_keys`.
4. Recipe swap: L646, L745/754/770, L1713/1715, L2556; trim imports.
5. `tests/test_decay_rank_seam.py`.
6. Parity capture, all four paths, empty diff.
7. Regression scope + lint + ratchet.
8. Docs cascade + CHANGELOG.

## Pipeline Sequencing (#642)

Issue #648's own contract requires this and the sibling plan
`recipes_field_layer_policy_cache.md` carries it, so it is recorded here as a
task, not left to memory: **all review-driven patching and the entire docs
cascade land BEFORE `verdict finalize`, and the delta is then re-reviewed.**

The substrate refuses to open DOCS without a finalized REVIEW verdict, but DOCS
commits move the head that the verdict's trailer pins. Finalizing first
therefore *guarantees* a trailer mismatch against the merged head — that is
#642. Patch, cascade docs, re-review the delta, and only then finalize.
