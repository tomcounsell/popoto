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

All three spikes were run at plan time against `main` at `9986c086`, with
`REDIS_URL=redis://localhost:6379/9` exported **before** `import popoto` (per
CLAUDE.md's ad-hoc-script rule; DB 9 was flushed before and after and is not
DB 0, DB 14, or DB 15).

**Environment for every number below:** popoto checkout `/Users/valorengels/src/popoto`
at `9986c086`, project `.venv`, Redis on `localhost:6379` DB 9, macOS 25.6.0.

### spike-1: does the post-hoc rename reach `_meta.db_class_key`?
- **Assumption**: "`_meta.db_class_key` is captured at class-creation time and
  the `__name__` rename does not update it."
- **Method**: prototype (direct introspection + `save()`)
- **Finding**: **Confirmed.** Two classes built by the rename idiom with
  prefixes `aaa`/`bbb` both report
  `_meta.db_class_key.redis_key == "ExternalBenchmarkMemory"` and
  `_meta.model_name == "ExternalBenchmarkMemory"` while `__name__` is
  `ExtMemaaa`/`ExtMembbb`. Saved record keys were
  `ExternalBenchmarkMemory:x:<uuid>` for both.
- **Confidence**: high
- **Impact on plan**: Establishes the root cause exactly where the issue says
  it is, and rules out any "it works out at write time" reading.

### spike-2: which fields are actually shared, and which only *look* shared?
- **Assumption**: "Only `ValidityField` is affected; every other field is
  isolated by the rename."
- **Method**: prototype — call the real `_build_external_model_class(prefix,
  with_validity=True)` for two prefixes plus `_build_graph_model_class` for a
  third, `save()` one record each under distinct `agent_id`s, then dump
  `KEYS *`.
- **Finding**: **Assumption false — the issue understates the scope.** Exactly
  one construct honors the rename: `BM25Field` (`$BM25:ExtMemaaa:...`), because
  it resolves the model name at call time rather than through
  `_meta.db_class_key`. Everything else is written under
  `ExternalBenchmarkMemory`:
  - `$Class:ExternalBenchmarkMemory` — shared instance-registry set.
  - `$ConfidencF:ExternalBenchmarkMemory:certainty:data` — **shared and
    unpartitioned** (one hash for all items).
  - `$ValidityF:ExternalBenchmarkMemory:validity:{valid_from,invalid_at,ingested_at}`
    — **shared and unpartitioned** (the reported defect).
  - `$DecayingSortF:ExternalBenchmarkMemory:relevance:<agent_id>`,
    `$KeyF:ExternalBenchmarkMemory:agent_id:<agent_id>`, and the record hashes
    `ExternalBenchmarkMemory:<agent_id>:<uuid>` — shared *prefix*, separated in
    practice only because `agent_id` is unique per benchmark item.
  Note also `$BM25:ExtMemaaa:content_index:tf:ExternalBenchmarkMemory:x:<uuid>`:
  even the correctly-namespaced BM25 keys embed the un-renamed record key.
- **Confidence**: high
- **Impact on plan**: The fix must be the class-construction change (which
  corrects all of these at once), not a `ValidityField`-specific patch. It also
  means `ConfidenceField` is a second genuinely-shared field the issue does not
  mention, and it means the practical isolation of the remaining fields is
  load-bearing on `agent_id` uniqueness — an invariant nothing asserts.

### spike-3: how many sites carry the pattern, and is `type()` viable at each?
- **Assumption**: "The pattern is confined to `external_base.py`, and each site
  can be converted mechanically."
- **Method**: code-read (`grep -rn '\.__name__ = ' tests/ scripts/ src/`) plus
  inspection of each hit.
- **Finding**: **Five sites, all in `tests/benchmarks/`, none in `src/`:**
  1. `scenarios/external_base.py:172` — `_build_graph_model_class`
  2. `scenarios/external_base.py:287` — `_build_external_model_class`
  3. `scenarios/recipe_base.py:73` — `build_benchmark_model`
  4. `association_recall.py:152` — the `AssocMemory` factory
  5. `test_confidence_gate_refusal.py:168` — `_build_refusal_model`
  Each converts to `type(name, bases, namespace)` mechanically. Two
  complications, both already solved elsewhere in the repo: (a)
  `recipe_base.py`'s class body defines a method (`compute_filter_score`) and
  two class attributes read from the `overrides` dict — all go in the namespace
  dict verbatim; (b) `_build_graph_model_class` and `association_recall.py`
  register a **self-referential** `Relationship` post-hoc, which is
  name-independent and stays exactly as it is (the class object must exist
  first — same pattern as `tests/test_graph_traversal.py`). The `with_validity`
  post-hoc `add_field` is likewise name-independent: `ValidityField` derives its
  keys at call time from `_meta.db_class_key`, which is already correct once the
  class is born with the right name.
- **Confidence**: high
- **Impact on plan**: Scope is all five sites, not two. Rules out any need to
  restructure how `safe_prefix` is captured (the issue's option-2 caveat "may
  require restructuring how the class body captures `safe_prefix`" does not
  apply — nothing in any of these class bodies reads the prefix).

## Data Flow

Where the class name travels, and where it is frozen:

1. **Entry point**: `ExternalScenario.setup()` computes a per-item
   `safe_prefix` (a short hash of the item id) and calls
   `_build_external_model_class(safe_prefix, ...)` or
   `_build_graph_model_class(safe_prefix, ...)`
   (`scenarios/external_base.py:517-524`).
2. **Class creation** (today): the `class ExternalBenchmarkMemory(popoto.Model)`
   statement runs. Popoto's metaclass constructs `ModelOptions("ExternalBenchmarkMemory")`,
   which sets `model_name`, `db_class_key = DB_key(model_name)` and
   `db_class_set_key = DB_key("$Class", db_class_key)`
   (`src/popoto/models/base.py:189-191`). **This is the freeze point.** The
   subsequent `__name__` assignment is a no-op with respect to `_meta`.
3. **Field key derivation**: `Field.get_special_use_field_db_key(model, *names)`
   returns `DB_key(cls.field_class_key, model._meta.db_class_key, *names)`
   (`src/popoto/fields/field.py:629`). Every field type that goes through this
   accessor — `ValidityField`, `ConfidenceField`, `DecayingSortedField`,
   `KeyField`'s index, `CoOccurrenceField` — inherits the frozen name.
   `BM25Field` does not route through it and is the sole exception.
4. **Record key derivation**: `Model.db_key` composes from
   `self._meta.db_class_key` (`src/popoto/models/base.py:766`), so instance
   hashes also carry the frozen name.
5. **Write**: `record.save()` writes all of the above into the bench DB
   (14 by default under `run_external.py`, 15 under pytest).
6. **Cleanup**: `ExternalScenario.teardown()` deletes by (a) `record.delete()`
   per saved record, (b) an explicit `ValidityField.get_all_keys(...)` DEL +
   `:open:*` SCAN, (c) `SCAN {self._model_class.__name__}:*` — which matches
   *nothing* today, since no key begins with `ExtMem<hash>:`, (d) `SCAN
   *{agent_prefix}*`, which is what actually removes the agent-partitioned
   keys, and (e) `shutil.rmtree` of the per-class embedding dir.
7. **Residue sweep**: on the next run, `run_external.py::_sweep_stale_benchmark_keys`
   SCAN+DELs `_STALE_KEY_PATTERNS = ("ExternalBenchmarkMemory:*", "ExtMem*",
   "$BM25:ExtMem*")`.

After the fix, step 2's freeze point captures `ExtMem<hash>` and steps 3-7
follow it. Steps 6c and 7 are the two places whose *patterns* must move with
it — both are prefix-anchored globs that will no longer match the keys they
were written for. That is the non-obvious half of this change.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: none that are public. The five factory functions keep
  their signatures and return a `popoto.Model` subclass exactly as before; only
  the construction idiom inside them changes. No `src/` file is touched, so
  popoto's published API and the mypy ratchet are both untouched.
- **Coupling**: **decreases.** Today the harness's key isolation is a joint
  property of three things — the post-hoc rename, `teardown()`'s compensating
  deletes, and the coincidence that `agent_id` is unique per item. After the
  fix it is a property of class construction alone, and `teardown()` becomes
  ordinary hygiene rather than a correctness requirement.
- **Data ownership**: each per-item class owns its whole keyspace rather than
  sharing four key families with every sibling class in the run.
- **Reversibility**: trivial. Five self-contained function bodies plus two
  glob-pattern constants; a `git revert` restores the prior behavior with no
  data migration (benchmark data is ephemeral and swept at run start).

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 1 (confirm the scope widening from 2 sites to 5, and the
  "no `src/` change" call)
- Review rounds: 1

Medium rather than Small because the mechanical edit is the easy half: the
alignment cost is in (a) agreeing the fix belongs in the harness and not in
`src/`, (b) the five-site scope rather than the issue's stated two, and (c) the
two pattern constants (`teardown()`'s SCAN, `_STALE_KEY_PATTERNS`) that must
move with the rename and whose breakage is silent — a stale glob leaks keys, it
does not raise.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable on localhost:6379 | `redis-cli -u redis://localhost:6379/15 ping` | The benchmark tests and the full suite both need it |
| Lane-scoped test DB (DB 15 is shared across worktrees) | `python -c "import os; d=os.environ.get('POPOTO_TEST_DB'); assert d and d != '0', 'set POPOTO_TEST_DB=<n>, n != 0'"` | Concurrent SDLC lanes on this machine collide on DB 15 (73-158 phantom failures) |
| Benchmark extras installed | `python -c "import src.popoto.fields.bm25_field, src.popoto.fields.validity_field"` | The touched factories import BM25/Validity/Confidence/CoOccurrence fields |

This repo has no `scripts/check_prerequisites.py`; run the commands above
directly. `sentence-transformers` is **not** required — every task in this plan
exercises the lexical/graph arms, and the vector arm's `EmbeddingField` branch
is covered by an assertion on `_meta.db_class_key`, which needs no provider.

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
