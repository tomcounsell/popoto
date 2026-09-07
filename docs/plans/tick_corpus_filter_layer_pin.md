---
title: Single-layer pin for the tick() semantic corpus filter
slug: tick_corpus_filter_layer_pin
status: Complete
type: bug
appetite: Small
tracking: https://github.com/tomcounsell/popoto/issues/684
last_comment_id:
revision_applied: false
revision_applied_at:
---

# Single-layer pin for the tick() semantic corpus filter

## Problem

`MemoryLifecycle.tick()` protects a semantic record from being forgotten through
three independent layers in `src/popoto/recipes/memory_lifecycle.py`:

1. **Corpus filter** — `_tick_pass` drops semantic records out of the hydrated
   corpus before either phase runs (`memory_lifecycle.py:957-959` on
   `05d9634d`; the issue cites `963-965`, see Freshness Check).
2. **Policy tier check** — `_default_should_forget` returns `False` on
   `tier == "semantic"` before it reads idle time (`memory_lifecycle.py:326`).
3. **Re-check-tier guard** — before tombstoning, `_tick_pass` re-reads the live
   tier from Redis and skips a record that has become semantic since hydration
   (`memory_lifecycle.py:1035-1039`).

Layers 2 and 3 each have a dedicated single-layer pin. Layer 1 has none. Its
only coverage is the end-to-end `test_tick_does_not_forget_semantic`, which was
verified during #674 to pass with any two of the three layers deleted. The
corpus filter could therefore be removed outright and the suite would stay
green — the same class of defect #674 fixed, one layer over.

## Freshness Check

Baseline: `05d9634d` (`fix(#658): MemoryLifecycle importance score honours
partition_by (#683)`), the tip of `origin/main` at plan time.

**Disposition: Minor drift.**

- The issue cites `memory_lifecycle.py:963-965`. On the current baseline the
  filter lives at `957-959`. #683 landed between filing and planning and
  touched `memory_lifecycle.py`, shifting the line numbers; the code is
  otherwise identical to the snippet quoted in the issue. The plan uses the
  corrected references.
- All three layers were re-read on the baseline and confirmed present and
  unchanged in behaviour.
- `grep -rn "corpus filter\|non_semantic_records" tests/` returns nothing —
  confirmed independently that no test names or reaches layer 1.
- Cited PR #681 (`fix(#674): make forget-guard tests reach the guard they are
  named for`) is **MERGED** as of 2026-09-07T08:48Z. Its result is the
  docstring convention this plan extends, not a change to the root cause.

## Prior Art

- **#674 / PR #681 (merged)** — fixed two forget-guard tests that were
  structurally unable to reach the guard they were named for, because
  `_tick_pass` re-hydrates the corpus and drops semantic (or absent) records
  before the forget phase. The remedy was to inject the state change from
  *inside* `should_forget`, placing it after hydration. That PR also
  established the docstring convention: each test names the layer it pins, and
  the end-to-end test names its per-layer counterparts. This issue was
  explicitly scoped out of that PR on the team-lead's instruction.
- **#658 / PR #683 (merged)** — partition-aware importance scoring in the same
  file. Unrelated to the forget path; the only effect here is line drift.

## Why Previous Fixes Failed

PR #681 did not fail — it was scoped. It fixed layers 2 and 3's *tests*; layer
1 was deliberately left for this issue rather than widening that PR.

The structural reason layer 1 resists a naive test is worth stating, because it
is what will make a careless fix vacuous: **any outcome-level assertion about
`tick()` is caught by layers 2 and 3.** Remove the corpus filter and a semantic
record reaches the forget phase, but `_default_should_forget` returns `False`
on tier, so nothing is deleted and every observable outcome is unchanged. A
test that asserts "the semantic record survives" therefore cannot distinguish
"layer 1 works" from "layers 2 and 3 cleaned up after it." The pin must observe
the *composition of the corpus*, not the outcome.

## Appetite

**Small.** One test function plus a docstring edit on an existing test. No
production-code change.

## Solution

`non_semantic_records` is a local inside `_tick_pass`, so it cannot be asserted
on directly. The observable proxy is **which records the forget phase is
invoked over**: phase 2 iterates `non_semantic_records` and calls
`self._should_forget(record, self)` on each. Passing a recording
`should_forget` that delegates to `_default_should_forget` gives a faithful
transcript of the corpus reaching phase 2 while leaving layers 2 and 3 fully
intact — the delegate *is* layer 2, and layer 3 is never modified.

New test in `tests/test_memory_lifecycle.py`:

```
test_tick_corpus_filter_excludes_semantic_records
```

Shape:

1. Build a recording `should_forget` that appends each candidate's tier to a
   list and returns `_default_should_forget(rec, lifecycle)` unchanged.
2. Construct `MemoryLifecycle(..., should_forget=recording)` with
   `FORGET_IMPORTANCE_FLOOR = 1.1` and `FORGET_IDLE_SECONDS = -1.0` — the same
   thresholds `test_tick_does_not_forget_semantic` uses, and for the same
   reason #674 documents: the condition is `idle > FORGET_IDLE_SECONDS`, so a
   freshly-saved record only clears the idle gate against a negative floor.
3. Save one semantic and one episodic record (both `confirm_accesses=0`, so
   the episodic one is not promoted in phase 1 and therefore is not skipped by
   the `promoted_this_pass` check in phase 2).
4. **Precondition assertion**: `TrackedMemory.query.all()` contains *both*
   records' `_redis_key`s. Without this the test is vacuous if the hydration
   query ever stops returning semantic records — the filter would have nothing
   to remove and the test would pass for the wrong reason.
5. **Positive control**: assert `"episodic"` appears in the transcript. This
   proves the forget phase ran at all; without it the test passes when
   `tick()` no-ops.
6. **The pin**: assert `"semantic"` does *not* appear in the transcript.

Under mutation M1b (filter removed), step 6 fails while layers 2 and 3 keep
every outcome identical — which is exactly the discrimination the issue asks
for.

Docstring work (acceptance criterion 3): update
`test_tick_does_not_forget_semantic`'s docstring to name the new test as the
layer-1 pin alongside the two it already names.

## Data Flow

`tick()` → `_tick_pass()`:

1. `model_class.query.all()` (or `.filter(**partition_filters).no_track().all()`)
   → `all_non_semantic` — despite the name, this is the **unfiltered** corpus.
2. **Layer 1**: list comprehension filters on `_get_tier(r) != "semantic"` →
   `non_semantic_records`.
3. Phase 1 iterates `non_semantic_records`, skips non-episodic, may promote and
   record `id(record)` in `promoted_this_pass`.
4. Phase 2 iterates `non_semantic_records`, skips `promoted_this_pass`, calls
   `self._should_forget(record, self)` ← **the observation point**.
5. **Layer 2** is `self._should_forget` when it is the default.
6. **Layer 3**: on a truthy decision, `load_fields(live_key, tier_field)`
   re-reads the tier; `"semantic"` → skip.
7. Otherwise `self.tombstone(record, reason="lifecycle")`.

The test taps step 4, which is downstream of layer 1 and upstream of layers 2
and 3 — the only point in the flow where layer 1's effect is observable in
isolation.

## Rabbit Holes

- **Do not refactor `_tick_pass` to expose the corpus.** Extracting a
  `_hydrate_corpus()` seam would make the test trivial but is a production-code
  change on a hot path, outside a Small appetite, and would itself need review.
  The recording-callable tap is behaviour-preserving.
- **Do not monkeypatch `_get_tier` or the module.** That tests the mock.
- **Do not assert on `summary` counts.** Those are outcomes; layers 2 and 3
  neutralise them by construction.

## No-Gos

- No change to `src/popoto/recipes/memory_lifecycle.py`.
- No change to the behaviour of any existing test beyond the required docstring
  edit on `test_tick_does_not_forget_semantic`.
- No new test model or fixture — the existing `TrackedMemory` and the autouse
  `clean_db` fixture are sufficient.

## Risks

- **The positive control could mask a partial regression.** If the episodic
  record were promoted in phase 1 it would be skipped in phase 2 and the
  transcript would be empty, failing the positive control rather than passing
  vacuously. `confirm_accesses=0` keeps it below `PROMOTION_ACCESS_COUNT`, and
  the positive control is the guard if that ever changes. Acceptable.
- **Redis DB contention across worktrees.** Documented in CLAUDE.md; this lane
  uses its own `POPOTO_TEST_DB`.

## Step by Step Tasks

1. Add `test_tick_corpus_filter_excludes_semantic_records` to
   `tests/test_memory_lifecycle.py`, immediately after
   `test_tick_does_not_forget_semantic`, with a docstring naming layer 1 in the
   convention PR #681 established.
2. Import `_default_should_forget` and `_get_tier` from
   `src.popoto.recipes.memory_lifecycle` in the test module (or locally in the
   test, matching the file's existing style for narrow imports).
3. Update `test_tick_does_not_forget_semantic`'s docstring to name the new test
   as the layer-1 pin.
4. Update the module docstring's Coverage list to mention the corpus-filter
   pin.
5. **Mutation proof (mandatory).** Apply M1b — replace the layer-1
   comprehension with `[r for r in all_non_semantic]` — leaving layers 2 and 3
   untouched. Run the new test and confirm it FAILS. Run
   `test_tick_does_not_forget_semantic` and confirm it still PASSES (this is
   the evidence that the new test discriminates where the old one cannot).
   Restore the source and confirm both pass. Record the table in the PR body.
6. Additionally mutate the new test's own positive control (drop the semantic
   record from the fixture) and confirm the precondition assertion fires —
   proving the test cannot pass on an empty corpus.
7. Gates: `ruff check src/` exits 0; `black --check src/ tests/` clean;
   `scripts/mypy_ratchet.py` at or below the baseline recorded in
   `scripts/mypy_baseline.json` at measurement time.

## Success Criteria

- `test_tick_corpus_filter_excludes_semantic_records` passes on unmodified
  `main`.
- It FAILS under mutation M1b with layers 2 and 3 unmodified.
- `test_tick_does_not_forget_semantic` PASSES under the same mutation,
  documenting the coverage gap this closes.
- Both test docstrings name the layer each pins.
- `tests/test_memory_lifecycle.py` passes in full.
- Lint, format, and type gates clean.

## Documentation

No user-facing documentation change. The plan's Data Flow section and the test
docstrings are the durable record; `CLAUDE.md` needs no update because this
adds no new tooling or invariant a contributor must know before running the
suite.

## Open Questions

None. The issue is precise, its recon was verified independently against the
current baseline, and the acceptance criteria are mechanical.
