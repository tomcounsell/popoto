---
status: Complete
type: bug
appetite: Small
owner: valorengels
created: 2026-08-07
tracking: https://github.com/tomcounsell/popoto/issues/525
last_comment_id:
revision_applied: true
revision_applied_at: 2026-08-07T08:41:29Z
---

# DB_key colon escape must be self-escaping

## Problem

`DB_key.clean()` escapes `:` to the literal seven-character sequence `{&#58;}`, but it
does not escape that sequence when it already appears in the *input*. `unclean()`
therefore cannot tell an escape that `clean()` produced from one the caller supplied,
and the round trip silently returns a different string.

**Current behavior** (verified against `main` @ `286deb8`):

```
'{&#58;}'    -> clean -> '{&#58;}'            -> unclean -> ':'      LOSSY
'a{&#58;}b'  -> clean -> 'a{&#58;}b'          -> unclean -> 'a:b'    LOSSY
'{&#58;}-/:' -> clean -> '{&#58;}/-//{&#58;}' -> unclean -> ':-/:'   LOSSY
```

The slash family (`/` → `//`, and `'?*^[]-` → `/<char>`) is self-escaping and round-trips
correctly even for inputs that look like escape output. Only the colon escape is broken.

`DB_key.from_redis_key()` is documented as the inverse of `__str__` and is used by
`Query` to recover KeyField values from stored keys without fetching the object. Any
caller relying on that inverse gets a *wrong but plausible* string when the stored value
contained `{&#58;}` — the worst failure shape for a key.

Blast radius in practice is small (a value must contain that exact seven-character
sequence), but the failure is silent.

**Desired outcome:**

`DB_key.unclean(DB_key.clean(v)) == v` for every string `v`, including strings containing
the literal `{&#58;}` sequence, with no change to the on-disk encoding of any value that
does *not* contain that sequence, and with the new `unclean()` still correctly decoding
every key written by the current implementation.

## Freshness Check

**Baseline commit:** `286deb8c0b3b378872e8eaf95933af03846cf4bf` (main)
**Issue filed at:** 2026-08-07T07:06:43Z
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/models/db_key.py:156-160` (`clean`) — colon replace is a plain
  `value.replace(":", "{&#58;}")` with no self-escaping — still holds.
- `src/popoto/models/db_key.py:177-184` (`unclean`) — decodes `{&#58;}` → `:`
  unconditionally, before the slash unescaping — still holds.
- `src/popoto/models/db_key.py:133` (`from_redis_key`) — splits on `":"` and uncleans each
  partial — still holds.

**Reproduction re-run on current main:** confirmed, output matches the issue verbatim.

**Cited sibling issues/PRs re-checked:** none cited in the issue body. Prior-art search
(`gh issue list --state all --search "db_key clean escape"`, `gh pr list --state merged
--search "db_key escaping"`) returned only #525 itself.

**Commits on main since issue was filed touching referenced files:**
`git log --since=2026-08-07T00:00:00Z -- src/popoto/models/db_key.py` → none.

**Active plans in `docs/plans/` overlapping this area:** none. `atomic_index_maintenance_lua.md`
mentions `DB_key.clean()`'s escaping only as a *reason not to reimplement it in Lua*; it
does not modify `db_key.py`, and the fix here does not weaken that argument.

## Prior Art

No prior issue or merged PR has touched `DB_key`'s escaping — the escape scheme has never
been changed by a tracked fix.

**Closest precedent: issue #476 (closed) — "1.8.0 atomic-index `\x00idxset` pointer field
pollutes the model hash."** That change altered the on-disk shape and was
forward-incompatible for pre-fix readers, which became a live mixed-deploy hazard (a
pre-1.8.0 reader hit an `ExtraData` crash and an apparently-empty index). This plan makes a
change in the same *class* — a write-format change with no migration gate — so the
dismissal in Risk 2 has to be argued against #476, not asserted.

**Why the no-migration-gate decision is safe here and was not there:**

| | #476 (1.8.0 atomic index) | This fix (#525) |
|---|---|---|
| Values whose stored bytes change | **Every model hash** — the pointer field was written unconditionally | Only values containing the literal seven-character `{&#58;}` sequence |
| State of those values before the change | Correct and readable | **Already lossy at write time** — the information needed to decode them was never stored |
| Old reader on new bytes | Crashed / silently empty on ordinary data | Wrong string, for inputs that are already decoded wrong today |
| New reader on old bytes | n/a | Byte-identical to the old reader (spike-2: 0 divergences / 200k encodings) |

The exposure #476 created — ordinary data becoming unreadable — has no analogue here,
because no value that decodes correctly today changes its encoding. The residual
forward-incompatibility is confined to a value set that is already corrupt.

**The lesson taken from #476:** its release note did not name a version boundary, which is
what made the mixed deploy hard to diagnose in the field. This plan's release note must name
one explicitly (see Documentation / Task 4): *keys written by Popoto ≥ 1.8.2 for values
containing the literal `{&#58;}` sequence are not decoded correctly by Popoto < 1.8.2; roll
readers forward before writers.* (Substitute the actual release version if the version bump
differs at ship time.)

## Research

No relevant external findings — this is purely internal string-escaping logic with no
external library, API, or ecosystem surface. Proceeding with codebase context.

One repo-local constraint that does bear on the approach: `hypothesis` is **not** a
dependency (`pyproject.toml` `[dev]` = pytest, pytest-asyncio, mypy, black, ruff, tiktoken).
The "property test" the issue asks for must be written as a seeded/exhaustive loop over a
small alphabet rather than by adding a new dependency.

## Spike Results

### spike-1: Is the issue's suggested fix direction actually viable?
- **Assumption**: "The simplest route is to route the colon escape through the existing
  slash-escape mechanism" (i.e. add `:` to the `'?*^[]-` glob list so `:` → `/:`).
- **Method**: code-read + prototype
- **Finding**: **Not viable.** `/:` still contains a literal colon, and `from_redis_key()`
  splits the Redis key on `":"`. `"a:b"` would encode to `"a/:b"` and then split into
  `["a/", "b"]` — the delimiter invariant breaks. The colon escape must produce output
  containing **no** literal colon, so it cannot be a slash-prefix escape.
- **Confidence**: high
- **Impact on plan**: The fix keeps `{&#58;}` as the colon escape and instead makes it
  self-escaping by routing the *literal sequence* through the slash mechanism.

### spike-2: Does a self-escaping `{&#58;}` round-trip, stay backward-compatible, and leave ordinary encodings untouched?
- **Assumption**: "`clean()` can pre-escape a literal `{&#58;}` as `/{&#58;}`, and a
  single-pass `unclean()` parser will decode both old and new output correctly."
- **Method**: prototype (throwaway script, not committed)
- **Finding**: **Confirmed on all three axes.** Prototype:
  - `clean()`: `/` → `//`; then `'?*^[]-` → `/<char>`; then `{&#58;}` → `/{&#58;}`; then
    `:` → `{&#58;}`.
  - `unclean()`: single left-to-right scan — on `/` emit the next character literally and
    skip both; else if the string starts with `{&#58;}` at this position emit `:` and skip
    seven; else emit the character.
  - Over 200,000 random strings from an alphabet of `a b / : - * [ ] ^ ? '` plus the literal
    `{&#58;}` and its constituent characters (`{ } & # 5 8 ;`), length 0–6:
    - **0** round-trip failures for the new pair.
    - **0** divergences between `unclean_new(clean_old(v))` and `unclean_old(clean_old(v))`
      — the new reader decodes every legacy-written key identically to the old reader.
    - Encoding differs between old and new `clean()` for **only** those values containing
      the literal `{&#58;}` — i.e. exactly the values that are already broken today.
- **Confidence**: high
- **Impact on plan**: This is the implementation. The sequential-replace form of `unclean()`
  cannot express the fix (the colon decode must not fire on a `/`-escaped occurrence, and
  it runs *before* slash unescaping), so `unclean()` becomes a single-pass scanner.

## Data Flow

1. **Entry point**: a model instance is saved, or a query builds a key pattern. A KeyField
   value reaches `DB_key.__str__` (`db_key.py:197-206`).
2. **`DB_key.clean(str(partial))`**: escapes the partial. Output must contain no literal
   `:` so the join is unambiguous.
3. **Join**: partials joined with `":"` → the Redis key string; written to Redis, and also
   embedded in index Set keys and relationship field values.
4. **Read back**: `DB_key.from_redis_key(redis_key)` (`db_key.py:131-133`) decodes bytes,
   `split(":")`, and `unclean()`s each partial.
5. **Output**: partials handed to `Query` (`src/popoto/models/query.py:2915`,
   `src/popoto/models/query.py:3305`), `Relationship`
   (`src/popoto/fields/relationship.py:288, 309, 411`), and
   `src/popoto/recipes/graph_traversal.py:211` as recovered field values.

Step 5 is where the silent corruption surfaces today. `clean()` is also called directly for
glob-pattern construction at `fields/indexed_field_mixin.py:528,539` and
`fields/key_field_mixin.py:475,480`; those call sites are unaffected because the new
encoding is byte-identical for any value without a literal `{&#58;}`.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none. `clean()` / `unclean()` keep their signatures; `unclean()`
  changes implementation from sequential `str.replace` to a single-pass scanner.
- **Coupling**: unchanged; the fix is contained to `db_key.py`.
- **Data ownership**: unchanged.
- **Reversibility**: high — a single-file revert. Forward-compatibility is the only
  asymmetry: keys written by the *new* `clean()` for values containing `{&#58;}` would be
  mis-decoded by a *pre-fix* reader. Those exact values are already corrupt today, so the
  practical exposure is nil (see Risks).

## Appetite

**Size:** Small

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on localhost:6379 | `redis-cli ping` | Test suite requires a live server (DB 15) |

## Solution

### Key Elements

- **Self-escaping colon escape**: `clean()` pre-escapes any literal `{&#58;}` in the input
  as `/{&#58;}` *before* encoding real colons, so the escape sequence gains the same
  self-escaping property `/` already has.
- **Single-pass `unclean()`**: a left-to-right scanner replaces the ordered
  `str.replace` chain, so a `/`-escaped `{&#58;}` is never mistaken for an escape the
  encoder produced.
- **Round-trip property test**: exhaustive over short strings from a hostile alphabet plus
  a seeded randomized sweep over longer ones, asserting `unclean(clean(v)) == v`.
- **Legacy-decode regression test**: pins that the new `unclean()` still decodes the
  encodings the current implementation produces, so no stored key becomes unreadable.

### Flow

Value with `{&#58;}` in it → `clean()` → escape sequence neutralized as `/{&#58;}` →
joined into a Redis key → `from_redis_key()` → single-pass `unclean()` → **original value
recovered exactly** (today: a corrupted string with a spurious `:`).

### Technical Approach

Three module-level constants in `src/popoto/models/db_key.py`, so the two methods, the
docstrings and the tests all reference one source of truth:

```python
COLON_ESCAPE = "{&#58;}"          # what a literal ":" encodes to
GLOB_CHARS = "'?*^[]-"           # slash-prefixed by clean()
ESCAPABLE = "/" + GLOB_CHARS + "{"   # every char clean() can emit after a "/"
```

`clean()` — one new line, inserted between the glob loop and the colon encode:

```python
value = value.replace("/", "//")
for char in GLOB_CHARS:
    value = value.replace(char, f"/{char}")
value = value.replace(COLON_ESCAPE, "/" + COLON_ESCAPE)   # NEW: self-escape the escape
value = value.replace(":", COLON_ESCAPE)
```

Order is load-bearing: the pre-escape must run *after* `/`-doubling (so the inserted `/` is
an escape, not data) and *before* the colon encode (so the `{&#58;}` the encoder itself
emits is not re-escaped).

`unclean()` — replace the ordered replace chain with a single left-to-right scan:

- at `/` **whose following character is in `ESCAPABLE`**: emit that character literally,
  advance 2. This subsumes `//` → `/`, `/<glob char>` → `<glob char>`, and the new `/{` →
  `{` (which fronts a `/{&#58;}` pre-escape).
- else at a position where the string starts with `COLON_ESCAPE`: emit `:`, advance
  `len(COLON_ESCAPE)`.
- else: emit the character, advance 1.
- a trailing lone `/`, or a `/` followed by a character outside `ESCAPABLE`, is emitted
  as-is — matching the current decoder exactly.

**Why the `/` branch is narrowed rather than greedy** (critique concern, Risk & Robustness):
a greedy `if ch == "/" and i + 1 < n` branch consumes *any* following character, which
silently widens the escape set relative to today's decoder on input `clean()` never
produced — `unclean("/a")` is `"/a"` today but would become `"a"`, and `"x/y"` → `"xy"`.
Spike-2's parity sweep could not surface this because it only fed `clean_old()`-produced
encodings, where a `/` is always followed by `/` or a glob char. Restricting the branch to
`ESCAPABLE` keeps parity on *arbitrary* input, not just well-formed input, and costs one
membership test. The property test's malformed-input arm pins this explicitly rather than
only asserting "does not raise" (which passes under either behavior).

The scanner is what makes the fix correct: with sequential replaces, the colon decode
necessarily runs before slash unescaping (slashes must be undone last), so it cannot
distinguish `/{&#58;}` from `{&#58;}` without a fragile lookbehind. Spike-2 verified the
scanner is byte-identical to the current `unclean()` on every legacy-produced encoding.

## Failure Path Test Strategy

### Exception Handling Coverage
No exception handlers in scope — `clean()`/`unclean()` are pure string functions with no
`try`/`except` in `db_key.py`.

### Empty/Invalid Input Handling
- `clean("")` → `""`, `unclean("")` → `""` — covered by an explicit test case and by the
  exhaustive length-0 arm of the property test.
- `unclean()` on strings that were never produced by `clean()` (raw class-name partials, a
  trailing lone `/`, a bare `{`) must not raise — covered by a malformed-input test.
- `clean()` is called as `clean(str(partial))`, so `None`/int partials are stringified
  upstream; no `None` handling changes here.

### Error State Rendering
No user-visible output surface — this is a library-internal encoding function. Corruption is
silent by nature, which is precisely why the property test (not example tests) is the gate.

## Test Impact

- [ ] `tests/test_tag_field.py` — **no change expected, and it is the repo's encoding-stability
      canary.** `tests/test_tag_field.py:263` (`test_index_key_is_a_plain_redis_set`) is the
      only test in the repo that hard-codes escaped bytes:
      `idx_key = "$TagF:TaggedMemory:tags:agent{&#58;}valor"`, produced from the source value
      `agent:valor`. It passes unchanged because that source value contains no literal
      `{&#58;}`, so the new pre-escape never fires and the encoding is byte-identical. If this
      test breaks, the encoding changed more than intended — the validator must report it by
      name (see Task 3), not just as part of a whole-suite pass.
- [ ] `tests/test_key_fields.py`, `tests/test_immutable_keys.py`,
      `tests/test_keyfield_migration.py`, `tests/test_keyfield_stale_reads.py`,
      `tests/test_relationship_edge_cases.py` — **no change expected**: none assert on the
      escaped byte form, and the encoding is unchanged for every value without a literal
      `{&#58;}`. The build must confirm this by running the full suite, and must NOT relax
      any of these tests to accommodate the fix — a break here means the encoding changed
      more than intended.
- [ ] New file `tests/test_db_key_escaping.py` — CREATE: round-trip property test, legacy
      decode-compatibility test, malformed-input test, and the three issue reproductions as
      hard assertions.

No expected-failure markers relate to this bug: `grep -rn 'xfail' tests/` hits only
`tests/test_field_index_edge_cases.py` and `tests/benchmarks/test_overrides_reach.py`,
neither of which concerns key escaping. Nothing to convert.

## Rabbit Holes

- **Redesigning the escape scheme.** Switching to percent-encoding, base64, or a
  single-character sentinel would fix the bug *and* invalidate every key in every existing
  database. Out of scope — keep `{&#58;}`.
- **Following the issue's stated fix direction literally.** Routing `:` through the slash
  escape is disproved by spike-1: it emits a literal colon and breaks the delimiter. Do not
  re-litigate.
- **Adding `hypothesis`.** A seeded loop gives the same coverage here for zero dependency
  cost. A new dev dependency for one test file is not worth the CI surface.
- **Writing a migration for already-ambiguous keys.** Keys whose values contained
  `{&#58;}` are *already* unrecoverable — the information needed to disambiguate was never
  written. There is nothing to migrate to; see Risks.
- **Escaping `{` unconditionally.** Tempting for symmetry, but it changes the encoding of
  every value containing a brace (and interacts with Redis Cluster hash tags). Escape the
  sequence, not the character.

## Risks

### Risk 1: Encoding change for values containing the literal `{&#58;}`
**Impact:** A key written before the fix for such a value decodes to the (wrong) legacy
value; a key written after decodes to the correct value. A row stored pre-fix will not be
found by a post-fix lookup of the same input value.
**Mitigation:** These values are already broken — the current encoding loses information at
write time, so no correct behavior is being regressed. The affected set requires a value
containing that exact seven-character sequence, which no realistic domain value does. Call
this out explicitly in the changelog/docs note rather than attempting a migration.

### Risk 2: Mixed-version deployment (new writer, old reader)
**Impact:** An old reader decoding a new writer's `/{&#58;}` runs its colon replace first,
yielding `/:`, then unescapes slashes — producing a wrong value. Same class of corruption as
today, for the same already-broken input set.
**Mitigation:** The new reader is fully backward-compatible (spike-2: 0 divergences over
200k legacy encodings; independently re-verified by the critique at 207,240 cases), so
rolling readers forward first is safe. Forward-incompatibility is confined to the
already-corrupt value set — see the Prior Art comparison against **#476**, which is the same
class of change but changed *every* model hash and so broke ordinary data. That difference,
not the change class, is what makes a release note sufficient here and a code gate
unnecessary.

The one thing #476 got wrong that this must not repeat: its note did not state a version
boundary, which is what made the mixed deploy hard to diagnose. The release note here must
name it explicitly — *keys written by Popoto ≥ 1.8.2 for values containing the literal
`{&#58;}` sequence are not decoded correctly by Popoto < 1.8.2; upgrade readers before
writers* — using whatever version the fix actually ships in.

### Risk 3: The single-pass scanner subtly diverges from the old decoder on some legacy input
**Impact:** Stored keys become unreadable — far worse than the bug being fixed.
**Mitigation:** A dedicated legacy decode-compatibility test asserts
`unclean_new(clean_old(v)) == unclean_old(clean_old(v))` over the same hostile alphabet,
with the old implementations inlined in the test as frozen reference functions. Spike-2
already ran this at 200k samples with zero divergences.

## Race Conditions

No race conditions identified — `clean()` and `unclean()` are pure, synchronous,
side-effect-free classmethods operating on their argument only. No shared mutable state, no
I/O, no async.

## No-Gos (Out of Scope)

Nothing deferred — every relevant item is in scope for this plan.

## Update System

No update system changes required — this is a library-internal encoding fix with no new
dependencies, config, or deployment surface. It ships in the normal release.

## Agent Integration

No agent integration required — `DB_key` is internal to the ORM and is not exposed as a tool
or MCP surface.

## Documentation

### Feature Documentation
- [ ] No `docs/features/` entry — this is a bug fix to existing documented behavior, not a
      new feature.

### External Documentation Site
- [ ] `docs/configuration.md:129` shows `HGETALL Restaurant:Burger{&#58;}Palace` as an
      example of the colon escaping (inside the "Debugging with redis-cli" command block —
      **not** line 113, which is a blank line in a different section). Verify it is still
      accurate (it is — that value has no literal `{&#58;}`), and leave it unless the docs
      pass finds it misleading.
- [ ] `mkdocs build --strict` passes (`scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Update the `clean()` docstring (`db_key.py:136-155`) to document the self-escaping
      property and the load-bearing ordering of the four steps.
- [ ] Update the `unclean()` docstring (`db_key.py:163-176`) — the current text describes
      the sequential replace order ("colons first, then glob characters, then slashes last"),
      which no longer describes the implementation.
- [ ] Update the class-level "Key Escaping" note (`db_key.py:19-22`) to state the round-trip
      guarantee.
- [ ] Add a release-note line about the encoding change for values containing the literal
      `{&#58;}` sequence. It **must name the version boundary** ("keys written by ≥ X.Y.Z for
      values containing the literal `{&#58;}` are not decoded correctly by < X.Y.Z; upgrade
      readers before writers") — the omission of exactly this in #476's note is what made
      that mixed deploy hard to diagnose.

## Success Criteria

- [x] `DB_key.unclean(DB_key.clean(v)) == v` holds for all `v` in the property test's
      exhaustive + randomized sweep, including the three reproductions from issue #525.
- [x] The new `unclean()` decodes every legacy-produced encoding identically to the current
      implementation (frozen-reference comparison test).
- [x] `DB_key.clean(v)` output is byte-identical to the current implementation for every `v`
      that does not contain the literal `{&#58;}` sequence (asserted by test).
- [x] `clean()` output never contains a literal `:`, so `from_redis_key()`'s `split(":")`
      stays unambiguous (asserted by test).
- [x] The new `unclean()` does not widen the escape set on input `clean()` never produced:
      `unclean("/a") == "/a"` and `unclean("x/y") == "x/y"`, with parity against the frozen
      legacy decoder on the fixed malformed-input list (asserted by test).
- [x] `tests/test_tag_field.py::test_index_key_is_a_plain_redis_set` passes unmodified and is
      reported by name by the validator.
- [x] Full existing suite passes unmodified — no existing test relaxed to accommodate the fix.
- [x] Tests pass (`/do-test`)
- [x] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (db-key)**
  - Name: `db-key-builder`
  - Role: Implement the self-escaping `clean()` and single-pass `unclean()` in
    `src/popoto/models/db_key.py`, plus docstrings.
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Test engineer (escaping)**
  - Name: `escaping-test-engineer`
  - Role: Write `tests/test_db_key_escaping.py` — round-trip property test, legacy
    decode-compatibility test, encoding-stability test, malformed-input test.
  - Agent Type: test-engineer
  - Resume: true

- **Validator (db-key)**
  - Name: `db-key-validator`
  - Role: Verify success criteria and that no existing test was modified.
  - Agent Type: validator
  - Resume: true

### Step by Step Tasks

### 1. Implement the self-escaping colon escape
- **Task ID**: build-db-key
- **Depends On**: none
- **Validates**: tests/test_db_key_escaping.py (create), tests/test_key_fields.py
- **Informed By**: spike-1 (slash-routing the colon is not viable — it emits a literal
  colon and breaks `split(":")`), spike-2 (pre-escape `{&#58;}` → `/{&#58;}` plus a
  single-pass scanner: 0 round-trip failures, 0 legacy-decode divergences, encoding changes
  only for values containing the literal sequence)
- **Assigned To**: db-key-builder
- **Agent Type**: builder
- **Parallel**: true
- Add module-level constants in `src/popoto/models/db_key.py`:
  `COLON_ESCAPE = "{&#58;}"`, `GLOB_CHARS = "'?*^[]-"`, `ESCAPABLE = "/" + GLOB_CHARS + "{"`.
  Use them in both methods and in the docstrings so the two cannot drift.
- In `clean()`, insert `value = value.replace(COLON_ESCAPE, "/" + COLON_ESCAPE)` between the
  glob-escape loop and the `:` → `COLON_ESCAPE` replace. Do not reorder the other steps.
- Rewrite `unclean()` as a single left-to-right scanner: `/` **whose next char is in
  `ESCAPABLE`** → emit that char literally (advance 2); else `value.startswith(COLON_ESCAPE,
  i)` → emit `:` (advance `len(COLON_ESCAPE)`, never a literal `7`); else emit the char
  (advance 1). A trailing lone `/`, or a `/` followed by a char outside `ESCAPABLE`, is
  emitted as-is.
- **The `ESCAPABLE` guard is required, not optional.** A greedy `/`-branch changes
  `unclean("/a")` from `"/a"` to `"a"` and `unclean("x/y")` from `"x/y"` to `"xy"`, silently
  widening the escape set on input `clean()` never produced. Spike-2's parity sweep only fed
  well-formed encodings and could not have caught it.
- Update the `clean()` / `unclean()` docstrings and the module "Key Escaping" note — the
  existing `unclean()` docstring describes the replace ordering and becomes wrong.
- Do NOT change `__str__`, `from_redis_key`, or any call site.

### 2. Write the escaping test suite
- **Task ID**: build-escaping-tests
- **Depends On**: none
- **Validates**: tests/test_db_key_escaping.py (create)
- **Informed By**: spike-2 (alphabet and sample counts that exercised the failure), Research
  (`hypothesis` is not a dependency — use exhaustive + seeded loops)
- **Assigned To**: escaping-test-engineer
- **Agent Type**: test-engineer
- **Parallel**: true
- Create `tests/test_db_key_escaping.py`.
- Round-trip property test: exhaustive over all strings of length 0–3 from the alphabet
  `a`, `/`, `:`, `-`, `*`, `[`, `]`, `^`, `?`, `'`, `{`, `}`, `&`, `#`, `5`, `8`, `;`, plus
  the multi-character token `{&#58;}`; then a seeded (`random.Random(0)`) sweep of ~50k
  longer strings. Assert `DB_key.unclean(DB_key.clean(v)) == v`. (`5` and `8` are in the
  alphabet deliberately: without them the exhaustive arm cannot build near-miss sequences
  like `{&#5` adjacent to `8;}`, and it would not match the spike-2 alphabet that produced
  the confidence claim.)
- Explicit regression cases from issue #525: `'{&#58;}'`, `'a{&#58;}b'`, `'{&#58;}-/:'`.
- Legacy decode-compatibility test: inline the pre-fix `clean`/`unclean` as frozen reference
  functions named `_legacy_clean` / `_legacy_unclean`, and assert
  `DB_key.unclean(_legacy_clean(v)) == _legacy_unclean(_legacy_clean(v))` over the same sweep.
- Encoding-stability test: for every `v` in the sweep that does not contain `{&#58;}`,
  assert `DB_key.clean(v) == _legacy_clean(v)`; and for every `v` that does, assert the two
  differ (the fix actually engaged).
- Delimiter-safety test: assert `":" not in DB_key.clean(v)` over the sweep.
- Composite-key test: `DB_key.from_redis_key(str(DB_key("Model", v1, v2)))` recovers
  `["Model", v1, v2]` for a handful of hostile `v1`/`v2` including `{&#58;}`.
- Malformed-input test: `DB_key.unclean()` on `""`, `"/"`, `"{"`, `"{&#58"`, `"a/"` must
  return without raising.
- **Non-widening test (pins the `ESCAPABLE` guard).** "Does not raise" passes under both the
  greedy and the narrowed scanner, so assert the values directly, against the *frozen legacy
  decoder* as the oracle: `DB_key.unclean("/a") == "/a"` and `DB_key.unclean("x/y") == "x/y"`
  (a greedy scanner returns `"a"` and `"xy"` here — that is the regression being pinned
  against), and more generally
  `DB_key.unclean(s) == _legacy_unclean(s)` for a fixed list of never-produced-by-`clean()`
  strings: `"/a"`, `"x/y"`, `"/"`, `"a/"`, `"/{"`, `"//"`, `"/-"`, `"{&#58;}"`, `"/{&#58;}"`.
  (`"/{"` and `"/{&#58;}"` are the two the new encoder *can* produce, and are the intended
  points of divergence from `_legacy_unclean`; list them as expected-divergence cases with
  their new values rather than as parity cases.)
- Keep the whole file pure-Python (no Redis calls) so it runs fast.

### 3. Validate
- **Task ID**: validate-db-key
- **Depends On**: build-db-key, build-escaping-tests
- **Assigned To**: db-key-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/test_db_key_escaping.py -q` and the full suite.
- **Report `tests/test_tag_field.py::TestTagFieldIndex::test_index_key_is_a_plain_redis_set`
  by name** (the `tests/test_tag_field.py:263` hard-coded `"$TagF:TaggedMemory:tags:agent{&#58;}valor"`
  assertion). It is the repo's only escaped-bytes assertion and therefore the encoding-stability
  canary; a whole-suite green is not a sufficient report for it.
- Confirm `git diff --stat` shows no modifications to pre-existing test files.
- Confirm the diff to `src/` is confined to `src/popoto/models/db_key.py`.
- Use the exact Verification-table commands as written — they are exit-code-correct.
  Do **not** substitute a bare `git diff --name-only ... | grep -cv ...`: `grep -c` exits 1
  when nothing fails to match, so the success path reads as a failure to any exit-code-checking
  runner.
- Report pass/fail against Success Criteria.

### 4. Documentation
- **Task ID**: document-fix
- **Depends On**: validate-db-key
- **Assigned To**: db-key-builder
- **Agent Type**: builder
- **Parallel**: false
- Verify `docs/configuration.md:129` is still accurate (the `HGETALL
  Restaurant:Burger{&#58;}Palace` line — line 113 is a blank line in a different section).
- Add a release note about the encoding change for values containing the literal `{&#58;}`.
- Run `scripts/ci-local.sh docs`.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Escaping tests pass | `python -m pytest tests/test_db_key_escaping.py -q` | exit code 0 |
| Full suite passes | `python -m pytest -q` | exit code 0 |
| Issue #525 repro fixed | `python -c "import sys;sys.path.insert(0,'src');from popoto.models.db_key import DB_key as D;print(all(D.unclean(D.clean(v))==v for v in ['{&#58;}','a{&#58;}b','{&#58;}-/:']))"` | output contains True |
| clean() emits no literal colon | `python -c "import sys;sys.path.insert(0,'src');from popoto.models.db_key import DB_key as D;print(any(':' in D.clean(v) for v in ['a:b','{&#58;}','x','a/b:c']))"` | output contains False |
| Colon escape still `{&#58;}` (no scheme swap) | `python -c "import sys;sys.path.insert(0,'src');from popoto.models.db_key import DB_key as D;print(D.clean('a:b'))"` | output contains `a{&#58;}b` |
| Non-widening scanner pinned | `python -m pytest tests/test_db_key_escaping.py -q -k "malformed or widen or legacy"` | exit code 0 |
| Encoding canary (`test_tag_field`) | `python -m pytest tests/test_tag_field.py -q` | exit code 0 |
| Fix confined to db_key.py | `git fetch -q origin main && [ -z "$(git diff --name-only origin/main...HEAD -- src/ \| grep -v '^src/popoto/models/db_key\.py$')" ]` | exit code 0 |
| No pre-existing test modified | `git fetch -q origin main && [ -z "$(git diff --name-only origin/main...HEAD -- tests/ \| grep -v '^tests/test_db_key_escaping\.py$')" ]` | exit code 0 |

> Both `git diff` rows use `[ -z "$(…)" ]` rather than `grep -c`: `grep -c` exits **1** when
> nothing fails to match, so the old `grep -cv` form printed `0` while exiting non-zero — the
> success path looked like a failure to any `set -e` or exit-code-checking runner (verified
> in-repo). The `git fetch -q origin main` prefix also guards against a stale `origin/main`
> ref in a fresh worktree. Both rows use the three-dot `origin/main...HEAD` form (diff from
> the merge-base) rather than two-dot `origin/main` (a direct tree diff): once a branch has
> merged `origin/main` in, an unrelated concurrent change on main (e.g. #521's datetime fix)
> is fully absorbed and shows no diff either way, but two-dot form is spuriously red on a
> branch that has *not yet* merged main's newer tip, since it diffs file content rather than
> commit ancestry. Three-dot expresses the intended property — "did this branch's own history
> touch anything outside db_key.py / the escaping test file" — regardless of main's pace.
| Format clean | `python -m black --check src/popoto/models/db_key.py tests/test_db_key_escaping.py` | exit code 0 |
| Docs build | `mkdocs build --strict` | exit code 0 |

## Critique Results

**Verdict:** READY TO BUILD (with concerns) — 0 blockers, 5 concerns, 3 nits. Depth: FULL
(Risk & Robustness, Scope & Value, History & Consistency). Core algorithm independently
re-verified by the critique (207,240 cases: 0 round-trip failures, 0 legacy-decode
divergences, 0 encoding changes for values without the literal sequence).

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| CONCERN | Risk & Robustness (Adversary) | The single-pass scanner's `/` branch consumes *any* following character, widening the escape set. It diverges from the current `unclean()` on input that `clean()` never produced: `unclean("/a")` is `"/a"` today, `"a"` after the fix (also `"x/y"` → `"xy"`). Spike-2 only compared against `clean_old()`-produced encodings, so the sweep could not surface this. The plan claims parity for a trailing lone `/` but is silent on this widening. | Task 1 (build-db-key), Task 2 (build-escaping-tests) | Either narrow the branch to the characters `clean()` can actually emit — `if ch == "/" and i + 1 < n and v[i+1] in ESCAPABLE` where `ESCAPABLE = "/" + GLOB_CHARS + "{"` — or keep the greedy form and pin it deliberately with `assert DB_key.unclean("/a") == "a"` in the malformed-input test plus a docstring line saying `/` escapes any following character. Do not leave it unpinned: the malformed-input test as written only asserts "does not raise", which passes under both behaviors. |
| CONCERN | Risk & Robustness (Operator) | Two Verification-table commands are exit-code-broken and Task 3 instructs the validator to run them. `git diff --name-only origin/main -- src/ \| grep -cv 'src/popoto/models/db_key.py'` prints `0` but **exits 1** when no lines fail to match (verified in-repo) — the success case looks like a failure to any `set -e` or exit-code-checking runner. The same applies to the tests-unmodified row. `origin/main` may also be stale in a fresh worktree. | Task 3 (validate-db-key), Verification table | Replace both rows with a form whose exit code carries the meaning, e.g. `git fetch -q origin main && [ -z "$(git diff --name-only origin/main -- src/ \| grep -v '^src/popoto/models/db_key\.py$')" ]`. Note `grep -c` returns 1 on zero matches by design; `\|\| true` also works but then the exit code is meaningless and the validator must read stdout. |
| CONCERN | History & Consistency (Archaeologist) | Prior Art states "No prior issues or merged PRs found related to this work," but issue **#476** (closed) is a directly analogous precedent: the 1.8.0 atomic-index change altered the on-disk shape and was forward-incompatible for pre-fix readers, which became a live mixed-deploy hazard. Risk 2 dismisses the same class of exposure with "no code gate needed" — the identical reasoning. | Prior Art, Risk 2, Task 4 (document-fix) | Cite #476 in Prior Art with the concrete distinction that makes the dismissal safe here: #476 changed **every** model hash (so every pre-1.8.0 reader broke on ordinary data), whereas this change alters the encoding only for values containing the literal seven-character `{&#58;}` sequence, which are already lossy at write time. Then make the release note name the exact version boundary ("keys written by ≥X.Y for values containing the literal `{&#58;}` are not readable by <X.Y") — #476's note lacked that and it is what made the mixed deploy hard to diagnose. |
| CONCERN | History & Consistency (Consistency Auditor) | Test Impact enumerates five test files as unaffected and asserts "none assert on the escaped byte form" — but the survey missed `tests/test_tag_field.py:263`, which hard-codes `"$TagF:TaggedMemory:tags:agent{&#58;}valor"` and is the only test in the repo that pins escaped bytes. | Test Impact, Task 3 (validate-db-key) | Add `tests/test_tag_field.py` to the Test Impact no-change list. It passes unchanged because the source value `agent:valor` contains no literal `{&#58;}` (the pre-escape does not fire), which makes it the repo's natural encoding-stability canary — the validator should call it out by name rather than only reporting a whole-suite pass. |
| CONCERN | Scope & Value (Simplifier) | Three file:line citations are stale and one is a directly actionable task instruction. `query.py:2655` / `query.py:3045` do not contain `from_redis_key` (actual call sites: `query.py:2915`, `query.py:3305`); `relationship.py:288, 309, 411` omits its directory (actual `src/popoto/fields/relationship.py`, line numbers correct); `docs/configuration.md:113` is a blank line inside "### Print Redis Info" — the `HGETALL Restaurant:Burger{&#58;}Palace` example is at **line 129**. | Data Flow, Documentation, Task 4 (document-fix) | Correct the three references. Task 4 says "Verify `docs/configuration.md:113` is still accurate" — a documentarian following that literally inspects the wrong section and reports a false pass. Use `docs/configuration.md:129`. |
| NIT | History & Consistency (Consistency Auditor) | Spike-2's alphabet includes the individual characters `5` and `8`; Task 2's property-test alphabet omits them, so the exhaustive arm cannot construct near-miss sequences like `{&#5` adjacent to `8;}`. | Task 2 (build-escaping-tests) | Add `5` and `8` to the Task 2 alphabet so it matches the spike that produced the confidence claim. |
| NIT | Scope & Value (Simplifier) | Task 4 sets **Agent Type: documentarian** but assigns it to `db-key-builder`, whose roster entry declares **Agent Type: builder**. | Team Orchestration | Either add a documentarian to the roster or change Task 4's Agent Type to `builder`; a strict orchestrator may refuse the mismatched assignment. |
| NIT | Risk & Robustness (Skeptic) | The plan states "Extract the escape sequence and the glob-character set to module-level constants so the two methods cannot drift" but does not name them, while Task 1 later writes `COLON_ESCAPE` in a code fragment. | Technical Approach, Task 1 | Name both constants in the Technical Approach (`COLON_ESCAPE = "{&#58;}"`, `GLOB_CHARS = "'?*^[]-"`) so the docstrings, the scanner's advance-by-7, and the tests reference one source of truth. Prefer `len(COLON_ESCAPE)` over a literal `7` in the scanner. |

## Revision Notes (post-critique, 2026-08-07)

All 5 concerns and 3 nits are absorbed. No blockers were raised. Nothing was rejected.

| # | Finding | Resolution | Where |
|---|---------|-----------|-------|
| C1 | Greedy `/` branch widens the escape set (`"x/y"` → `"xy"`) | **Adopted the narrowing option** (not the pin-the-greedy-behavior option): new `ESCAPABLE = "/" + GLOB_CHARS + "{"` constant guards the `/` branch, restoring parity with the legacy decoder on *arbitrary* input, not just well-formed input. Verified by hand against the legacy decoder for `"/a"`, `"x/y"`, `"//"`, `"/-"`, `"a/"`, `"{&#58;}"` (parity) and `"/{"`, `"/{&#58;}"` (intended divergence, the new encoder's own output). A dedicated non-widening test replaces the "does not raise" assertion, which passed under both behaviors. | Technical Approach, Task 1, Task 2, Success Criteria |
| C2 | Two Verification rows exit 1 on their success path | Both `grep -cv` rows replaced with `git fetch -q origin main && [ -z "$(… \| grep -v '^…$')" ]`, whose exit code carries the meaning. The `git fetch` prefix also removes the stale-`origin/main`-in-a-worktree hazard. Task 3 now forbids substituting the `grep -c` form. | Verification table, Task 3 |
| C3 | Prior Art missed #476, an analogous forward-incompatible format change | Prior Art now cites #476 and argues the no-migration-gate decision against it with a four-row comparison: #476 changed **every** model hash (breaking ordinary data), this changes only values already lossy at write time. The distinguishing fact is carried into Risk 2, and #476's actual failure mode — a release note with no version boundary — is turned into a hard requirement on Task 4's release note (`≥ 1.8.2` / `< 1.8.2`, substituting the real ship version). | Prior Art, Risk 2, Documentation, Task 4 |
| C4 | Test Impact missed `tests/test_tag_field.py:263` | Added as the first Test Impact entry and framed as the repo's encoding-stability canary (source value `agent:valor` has no literal `{&#58;}`, so the pre-escape never fires). Task 3 must report it by name; added to Success Criteria and the Verification table. | Test Impact, Task 3, Verification, Success Criteria |
| C5 | Three stale file:line refs | Corrected and re-verified in-repo: `query.py` → `src/popoto/models/query.py:2915` and `:3305`; `relationship.py` → `src/popoto/fields/relationship.py:288, 309, 411` (lines were right, path prefix missing); `docs/configuration.md:113` → `:129`. Task 4's instruction was the actionable one — it now names 129 and says explicitly that 113 is a blank line elsewhere. | Data Flow, Documentation, Task 4 |
| N1 | Property-test alphabet omits `5` and `8` | Added to Task 2's alphabet with a note on why (near-miss sequences like `{&#5` + `8;}`), matching spike-2. | Task 2 |
| N2 | Task 4 Agent Type/roster mismatch | Task 4's Agent Type changed `documentarian` → `builder` to match `db-key-builder`'s roster entry (no new roster member needed for a two-bullet docs task). | Task 4 |
| N3 | Constants referenced but never named | `COLON_ESCAPE`, `GLOB_CHARS` and the new `ESCAPABLE` are now defined in the Technical Approach; the scanner advances by `len(COLON_ESCAPE)`, never a literal `7`. | Technical Approach, Task 1 |

---

## Decisions Taken

The critique raised no blocker against either default below, so both stand as decided. They
remain recorded (rather than deleted) because they are the two places a maintainer could
still redirect the work cheaply, and because #476 shows the cost of an undocumented format
decision.

1. **Encoding-change acceptance — DECIDED: release note, no migration.** The fix changes the stored encoding for values containing
   the literal `{&#58;}` sequence. Those values are already corrupted by the current
   implementation (the information is lost at write time, so no migration can recover them),
   so this plan treats a release note as sufficient and writes no migration. See the Prior
   Art comparison against #476 for why this is safe here and was not there. Say the word if
   this should instead become a versioned-encoding change.
2. **Escape scheme retained — DECIDED: keep `{&#58;}`.** Keeping it means the fix is a
   three-line change with near-zero migration cost. Replacing it with something shorter and
   inherently unambiguous would be cleaner but invalidates every existing key. This plan
   keeps it; flag if the long-term direction is a versioned key format.
