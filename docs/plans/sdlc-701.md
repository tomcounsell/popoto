---
status: Planning
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/701
last_comment_id: none
---

# #701 — Per-item benchmark model classes must own their Redis namespace from class creation

## Problem

Five benchmark model factories in `tests/benchmarks/` build a Popoto `Model`
subclass inside a `class` statement and then rename it *after the fact*:

```python
class ExternalBenchmarkMemory(popoto.Model):
    ...
ExternalBenchmarkMemory.__name__ = f"ExtMem{safe_prefix}"
ExternalBenchmarkMemory.__qualname__ = f"ExtMem{safe_prefix}"
```

`ModelOptions.__init__` (`src/popoto/models/base.py:189-191`) captures
`model_name` / `db_class_key` / `db_class_set_key` at **class-creation** time,
from the name in the `class` statement. The rename never reaches `_meta`, so
every per-item class built by a given factory shares one `db_class_key`.

**Current behavior:** measured on `9986c086` (see Spike Results for the exact
reproduction) two items built from `_build_external_model_class(..., with_validity=True)`
plus one from `_build_graph_model_class(...)` produce this keyspace:

```
$BM25:ExtMemaaa:content_index:df                 <- isolated (BM25 resolves the name at call time)
$BM25:ExtMembbb:content_index:df                 <- isolated
$Class:ExternalBenchmarkMemory                   <- SHARED
$ConfidencF:ExternalBenchmarkMemory:certainty:data   <- SHARED
$DecayingSortF:ExternalBenchmarkMemory:relevance:itemA   <- shared prefix, agent_id-partitioned
$KeyF:ExternalBenchmarkMemory:agent_id:itemA     <- shared prefix, agent_id-partitioned
$ValidityF:ExternalBenchmarkMemory:validity:ingested_at  <- SHARED, unpartitioned
$ValidityF:ExternalBenchmarkMemory:validity:invalid_at   <- SHARED, unpartitioned
$ValidityF:ExternalBenchmarkMemory:validity:valid_from   <- SHARED, unpartitioned
ExternalBenchmarkMemory:itemA:7f81a846...        <- shared prefix, agent_id-partitioned
```

**The issue body understates the blast radius, and the correction matters.**
It states that "each item otherwise gets its own isolated `ExtMem<hash>`
namespace for every other field (BM25, embedding, etc.)". That is true only of
`BM25Field` (and the on-disk embedding directory, which is keyed off
`__name__`). Everything else lands under `ExternalBenchmarkMemory`. Those other
fields are *effectively* isolated today only because `agent_id` is unique per
item and they are partitioned by it — an accident of the harness's data, not of
its class construction. `ValidityField` and `ConfidenceField` have no such
partition and are genuinely shared.

**Desired outcome:** each per-item class owns a distinct `db_class_key` from
the moment it exists, so isolation is a property of construction rather than of
a compensating `teardown()` and a coincidence of `agent_id` uniqueness. The
repo already has the correct convention in three sibling factories
(`tests/benchmarks/siq/corpus.py:263`, `csr/corpus.py:176`, `rlt/corpus.py:81`),
each of which builds with `type()` and documents *precisely this hazard* in its
docstring. The external harness is the outlier.

## Freshness Check

**Baseline commit:** `9986c086` (`fix(#689): stop shipping tests/ in the sdist (#703)`) — the tip of `main` at plan time apart from a concurrent lane's plan-only commit `75a1ecba`.
**Issue filed at:** 2026-09-07T12:55:57Z
**Disposition:** Unchanged (with a scope *widening*, not a drift — see Notes)

**File:line references re-verified:**

- `tests/benchmarks/scenarios/external_base.py:287-288` — the
  `__name__`/`__qualname__` rename in `_build_external_model_class` — **still
  present, verbatim.**
- `tests/benchmarks/scenarios/external_base.py:172-173` — the same rename in
  `_build_graph_model_class` — **still present** (the issue names this factory
  only in its "audit this too" note; it has the identical defect).
- `tests/benchmarks/scenarios/external_base.py:918-963` — `ExternalScenario.teardown()`'s
  validity-cleanup branch, guarded on `"validity" in self._model_class._meta.fields`
  — **still present**, and its explanatory comment (lines 927-944) states the
  root cause correctly.
- `tests/benchmarks/test_external.py:1204` — `test_no_leaked_validity_keys_after_teardown`
  — **still present**, in `TestSupersessionArm`.
- `tests/benchmarks/README.md:267-274` — the "Known limitation" paragraph naming
  this defect and pointing at "the follow-up issue filed from #692" (i.e. #701)
  — **still present**, and repeats the same understatement corrected above.
- `src/popoto/models/base.py:189-191` — `ModelOptions.__init__` capturing
  `db_class_key` from `model_name` — **still present.**

**Cited sibling issues/PRs re-checked:**

- **#692** — "bench: LongMemEval-S harness has no supersession producer" —
  **CLOSED.** Its implementation merged as PR **#702** at
  2026-09-08T04:46:20Z, i.e. *after* #701 was filed. #701 was written against
  the #692 branch; the code it describes reached `main` afterwards. Verified at
  HEAD that every cited construct landed unchanged.
- **#693** — "ValidityField declared + save-only = gate never fires; is silent
  inertness intended?" — **OPEN.** Orthogonal: #693 is about `src/` defaults for
  `ValidityField`; #701 is about harness class construction. No overlap.

**Commits on main since the issue was filed (touching referenced files):**

- `22c2320f` `bench(#692): supersession producer for the external LongMemEval-S/LoCoMo harness (#702)`
  — **introduced** the code under discussion. Does not fix it; it added the
  `teardown()` containment that the issue credits.
- `c16faf8c` `CyclicDecayField: ... (#698)` — irrelevant (does not touch
  `tests/benchmarks/scenarios/` or `models/base.py`).
- `9986c086` `fix(#689): stop shipping tests/ in the sdist (#703)` — irrelevant
  to the defect, but see Risks: `MANIFEST.in` now `prune tests`, so nothing in
  this plan can reach the published sdist.

**Active plans in `docs/plans/` overlapping this area:** none. `sdlc-692.md`
(status `Ready`, `revision_applied: true`) is the *shipped* predecessor and is
the plan that documents the containment `teardown()`; this plan removes the
premise its Technical Approach step 5 was written against, so that step's
comment text is a Documentation task here. `sdlc-699.md` is being written
concurrently by another lane against `src/popoto/fields/cyclic_decay_field.py`
— no file overlap.

**Notes:** No drift. The one substantive finding is a **widening**: the defect
is not confined to `ValidityField`, and it is not confined to
`_build_external_model_class`. Five factories carry the post-hoc-rename pattern
and every non-BM25 field type inherits the shared `db_class_key`. The issue's
suggested-fix option 2 is the right one and is already the documented repo
convention; options 1 and 3 are rejected in Technical Approach.

## Prior Art

- **PR #702 / issue #692** — "supersession producer for the external
  LongMemEval-S/LoCoMo harness". *Succeeded* at its own goal and, as a side
  effect, discovered this defect and shipped a containment (`teardown()`
  deleting the shared validity keys per item) plus a `tests/benchmarks/README.md`
  "Known limitation" note. It deliberately did not fix the root cause. This plan
  is its designated follow-up.
- **`tests/benchmarks/siq/corpus.py` (`build_trace_model_class`)** — builds with
  `type()` and its docstring says: *"a post-hoc `__name__` rename would leave the
  SortedField index shared across traces — cross-trace contamination"*. This is
  the target shape; it is already in the repo and already documented.
- **`tests/benchmarks/csr/corpus.py` (`_build_csr_model_class`, PR #444)** —
  same `type()` shape, docstring explicitly names *"the `external_base` style"*
  as the anti-pattern it is avoiding. So the correct fix was known and applied
  in a sibling harness before this issue was filed; `external_base` was simply
  never retrofitted.
- **`tests/benchmarks/rlt/corpus.py` (`build_corpus_model_class`)** — same
  `type()` shape, same reasoning, cites "the SIQ/CSR convention".
- **Issue #465 / `run_external.py::_sweep_stale_benchmark_keys`** — the startup
  SCAN+DEL of leaked benchmark residue, and issue #490 which generalized it to
  caller-supplied patterns for CSR. Its `_STALE_KEY_PATTERNS` are written
  against today's (defective) key shapes and must move with this fix.

**No prior attempt to fix this specific defect exists.** There is therefore no
"Why Previous Fixes Failed" section — the containment in #702 was explicitly
scoped as containment, not as a fix, and it worked.

## Research

Not applicable — this is purely internal. No external library, API, ecosystem
pattern, or version-sensitive behavior is involved: the change is a Python
class-construction idiom (`type(name, bases, ns)` vs. a `class` statement
followed by a rename) applied to this repo's own benchmark harness, and the
correct target shape already exists in three sibling files in the same tree.

No relevant external findings — proceeding with codebase context.

## Spike Results

<!-- skeleton -->

## Data Flow

<!-- skeleton -->

## Architectural Impact

<!-- skeleton -->

## Appetite

<!-- skeleton -->

## Prerequisites

<!-- skeleton -->

## Solution

<!-- skeleton -->

## Failure Path Test Strategy

<!-- skeleton -->

## Test Impact

<!-- skeleton -->

## Rabbit Holes

<!-- skeleton -->

## Risks

<!-- skeleton -->

## Race Conditions

<!-- skeleton -->

## No-Gos (Out of Scope)

<!-- skeleton -->

## Update System

<!-- skeleton -->

## Agent Integration

<!-- skeleton -->

## Documentation

<!-- skeleton -->

## Success Criteria

<!-- skeleton -->

## Team Orchestration

<!-- skeleton -->

## Step by Step Tasks

<!-- skeleton -->

## Verification

<!-- skeleton -->

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

---

## Open Questions

<!-- skeleton -->
