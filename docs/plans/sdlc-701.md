---
status: Ready
type: bug
appetite: Medium
owner: Valor Engels
created: 2026-09-08
tracking: https://github.com/tomcounsell/popoto/issues/701
last_comment_id: none
revision_applied: true
revision_applied_at: 2026-09-08T05:24:51Z
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
- `tests/benchmarks/scenarios/external_base.py:919-1010` — `ExternalScenario.teardown()`'s
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
  3. `scenarios/recipe_base.py:35` — `_build_recipe_model_class` (an earlier
     draft called this `build_benchmark_model` at line 73; that is the
     `__name__` assignment *inside* the function, not the function — nit N2)
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

**Team:** One builder (`harness-factory-builder`), one test engineer
(`namespace-test-engineer`), one documentarian (`bench-documentarian`), one
validator (`namespace-validator`) — four roles across five tasks. There is no
code-reviewer role; the diff review is Task 3, owned by the validator.

(History: critique C6 replaced an earlier "solo dev" line that contradicted Team
Orchestration, and merged two *builders* — `ext-factory-builder` and
`sibling-factory-builder` — into the single `harness-factory-builder` above. The
C6 replacement text itself then misdescribed the roster; critique C9 corrected it
to the four roles named here, which match Team Orchestration verbatim.)

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

**Status at critique time: PARTIAL.** `redis-cli … ping` → PONG and the
benchmark extras import cleanly, but **`POPOTO_TEST_DB` was unset**. The build
lane MUST export a non-zero, lane-scoped `POPOTO_TEST_DB=<n>` before running
anything — this is not advisory. DB 15 is shared across every worktree on this
machine and concurrent lanes have produced 73–158 phantom failures (Risk 4), and
an unset value is exactly the condition under which a reviewer misreads
contention as a regression from this change. Every count reported from a test run
must state its DB number alongside it.

This repo has no `scripts/check_prerequisites.py`; run the commands above
directly. `sentence-transformers` is **not** required — every task in this plan
exercises the lexical/graph arms, and the vector arm's `EmbeddingField` branch
is covered by an assertion on `_meta.db_class_key`, which needs no provider.

## Solution

### Key Elements

- **`type()`-built factories**: every per-item / per-case / per-trial benchmark
  model class is born with its final, prefix-bearing name, so `_meta` captures
  the right `db_class_key` the first time. Five call sites, matching the shape
  already used by `siq/corpus.py`, `csr/corpus.py`, and `rlt/corpus.py`.
- **Pattern constants that follow the rename**: `ExternalScenario.teardown()`'s
  class-name SCAN and `run_external.py::_STALE_KEY_PATTERNS` are prefix-anchored
  globs written against the *old* key shapes. After the fix the harness's keys
  live under `$<FieldType>F:ExtMem<hash>:…` and `$Class:ExtMem<hash>`, which
  neither `ExtMem<hash>:*` nor `ExtMem*` matches. Both must widen, or the fix
  trades one leak for another. **The widened `teardown()` glob is
  `*{class_name}*` — unanchored on both sides.** A `*:{class_name}:*` form
  cannot match `$Class:ExtMem<hash>`, which `ModelOptions.__init__` creates as
  `DB_key("$Class", db_class_key)` (`src/popoto/models/base.py:191`) with
  **nothing after** the class name. See critique C1; that trailing-colon form was
  in an earlier draft of Task 1 and is wrong.
- **A construction-time invariant test**: a test that asserts
  `cls._meta.db_class_key.redis_key == cls.__name__` for every benchmark
  factory, so the pattern cannot come back silently in a sixth factory.
- **Docs that stop describing a defect that no longer exists**:
  `tests/benchmarks/README.md`'s "Known limitation" paragraph and
  `external_base.py`'s teardown comment (and its ancestor,
  `docs/plans/sdlc-692.md` Technical Approach step 5) all assert the shared
  namespace as a live fact.

### Flow

Benchmark run start → `_sweep_stale_benchmark_keys` clears residue matching the
**new** patterns → per item, `setup()` calls a factory → factory returns a class
whose `_meta.db_class_key` **is** `ExtMem<hash>` → every field and every record
writes under that name → `teardown()` SCANs `ExtMem<hash>:*` **and**
`*ExtMem<hash>*` (no trailing colon anchor — see C1), which now matches
everything the item wrote → next item
starts on an empty namespace, with no dependence on `agent_id` uniqueness.

### Technical Approach

**The fix is the issue's option 2, and only option 2.** Options 1 and 3 are
rejected, for reasons that should not be relitigated at build time:

- *Option 1 — compute `db_class_key` lazily from `type(self).__name__`.* This
  is a `src/` behavior change to every model in the library. `db_class_key` is
  read at `src/popoto/models/base.py:766` (record keys), `:3610` (instance
  scans), `src/popoto/fields/field.py:629` (every special-use field key),
  `key_field_mixin.py:484`, `datetime_key_migration.py:489,512`, and
  `extraction/decision_log.py:949`. Making it mutable would mean a class whose
  `__name__` changes silently repoints at a different keyspace — turning a
  benchmark-harness footgun into a library-wide one, and breaking the stability
  guarantee that a persisted key can be recomputed. **Rejected.**
- *Option 3 — an override hook on `ValidityField`.* Adds public API surface to
  `src/` to serve one test-tree caller, and only fixes one of the four affected
  key families (spike-2: `ConfidenceField`, `$Class`, `$KeyF`,
  `$DecayingSortF` would all still be wrong). **Rejected.**
- *Option 2 — build the class with its final name.* No `src/` change, fixes all
  key families at once, and is already the convention in three sibling
  factories that each document this exact hazard. **Selected.**

Per-site notes (from spike-3, so the builder does not re-derive them):

- `scenarios/external_base.py::_build_external_model_class` has three mutually
  exclusive class-body variants (bm25+embedding / embedding-only / bm25-only).
  **Convert them 1:1 — one literal `type()` call per existing `class` block,
  each with its own literal namespace dict (critique C5).** Do *not* consolidate
  into a shared base dict with conditional field insertion. Consolidation is a
  structural dedup refactor riding inside a one-line-per-site correctness fix; it
  makes Risk 3 ("`type()` conversion silently drops a class attribute")
  materially harder to review in a harness CI never runs, and it is not the shape
  `siq`/`csr`/`rlt` use — each of those hard-codes one literal namespace dict.
  The 1:1 form keeps the diff line-for-line comparable against the pre-conversion
  bodies, which is the only real defense here. If a later reader wants the
  triplication removed, that is a separate, independently reviewable change.
  The post-`type()` `with_validity` block
  (`.validity = ValidityField()` + `_meta.add_field`) is **unchanged**: it is
  name-independent, and `ValidityField` derives its keys at call time from a
  `_meta.db_class_key` that is now already correct.
- `scenarios/external_base.py::_build_graph_model_class` — same conversion. Its
  post-`type()` self-referential `Relationship(model=cls, null=True)` +
  `_meta.add_field("prev_turn", …)` block is **unchanged and must stay
  post-hoc**: the class object cannot reference itself inside its own namespace
  dict any more than inside its own body.
- `association_recall.py::_build_model` — two class-body variants (with/without
  `CoOccurrenceField`); same 1:1 treatment (one `type()` call per variant, per
  C5 — not a branched namespace dict), same post-hoc self-referential
  `related = Relationship(model=AssocMemory, null=True)`.
- `scenarios/recipe_base.py::_build_recipe_model_class` — the only site whose
  class body carries a **method** (`compute_filter_score`) and two class
  attributes read from the `overrides` dict (`_wf_min_threshold`,
  `_wf_priority_threshold`). All three go into the namespace dict verbatim; the
  method as a plain `def` defined just above the `type()` call. Note this class
  has a **mixin base** (`WriteFilterMixin, popoto.Model`) — the bases tuple must
  preserve that order.
- `test_confidence_gate_refusal.py::_build_refusal_model` — the simplest site;
  single class body, direct conversion.

Every converted site sets `__module__` and `__qualname__` in the namespace dict
(as `siq`/`csr`/`rlt` do), so tracebacks and reprs stay readable.

**Pattern updates.** `run_external.py::_STALE_KEY_PATTERNS` must cover keys of
the form `$<Anything>:ExtMem<hash>…` and `$Class:ExtMem<hash>`, not just
`ExtMem*` and `$BM25:ExtMem*`. A `*:ExtMem*` glob covers all of them and is safe
here because the sweep runs against the dedicated bench DB (14 by default, DB 0
rejected) — but it must be *added alongside* the existing patterns, not
replace them: `ExternalBenchmarkMemory:*` stays so that residue left by
pre-fix runs is still swept. `*:ExtMem*` **is** correct for the sweep — it does
match `$Class:ExtMem12345678`, because that key has a colon *before* the class
name; it is only the `teardown()` companion that must drop the trailing anchor.

Symmetrically, `teardown()`'s `SCAN {class_name}:*` gains a
`SCAN *{class_name}*` companion (**not** `*:{class_name}:*` — C1). The
`{class_name}:*` pass is retained rather than subsumed: it is the cheaper,
prefix-anchored scan for the record hashes, and keeping both makes the diff
additive.

**The new leak test must not reuse `teardown()`'s pattern constant.** It derives
its own `*{class_name}*` match independently. Importing the glob under test into
the test that checks it is exactly how a defect of this shape goes green.

**Explicit-validity branch in `teardown()`: keep it.** After the fix its
premise (a *shared* namespace) is false, but the branch itself is still
correct and still targeted. Rewrite the comment (Documentation task), do not
delete the code. See Rabbit Holes for why the tempting deletion is a trap.

**But its no-op guard test must be rebuilt, not merely re-pointed (critique
C3).** `test_teardown_on_arm_none_is_real_noop`
(`tests/benchmarks/test_external.py:1226-1249`) seeds a sentinel into
`ValidityField.get_all_keys(validity_cls, "validity")` where `validity_cls =
_build_external_model_class("teardownchk", with_validity=True)`, then runs an
**arm-none** scenario built from a *different* per-item class and asserts the
sentinel survives. That is meaningful **only** because both classes collapse
onto the shared `ExternalBenchmarkMemory` namespace today. After this fix the
sentinel lives at `$ValidityF:ExtMemteardownchk:validity:*` while the scenario's
class is `ExtMem<item-hash>`, so no teardown behavior — guarded or unguarded —
can reach it and the test passes unconditionally. It becomes the #661 vacuity
trap, landing on the very test cited as the reason to keep this branch.

Re-deriving the sentinel's key "from the per-item class name" does **not**
repair it: the arm-none class declares no `validity` field at all, so it has no
validity keys to seed.

**And a key-count replacement does not repair it either (critique C7).** An
earlier revision of this plan specified a two-part assertion whose part (b) was
"assert the arm-none class produced zero `$ValidityF:{arm_none_class_name}:*`
keys, before and after teardown". That is *also* unfalsifiable by the mutation
this plan's build gate names. Re-verified against source at revision time:
`ValidityField.get_all_keys()` (`src/popoto/fields/validity_field.py:781-796`)
derives its five key names *purely* from `_meta.db_class_key`, via
`get_special_use_field_db_key` → `DB_key(cls.field_class_key,
model._meta.db_class_key, *field_names)` (`src/popoto/fields/field.py:629`); it
never consults whether the model declares a `validity` field. On the arm-none
class those five names were never written, so removing the
`"validity" in self._model_class._meta.fields` guard makes the DEL an
**unconditional no-op** — zero keys before, zero keys after, in both the guarded
and the unguarded build. Deleting the branch is falsifiable; removing its guard
is not.

**The replacement must therefore assert that the guard was *evaluated*, not that
its output looks empty.** Spy on the call, not on the keyspace:

1. **Guard-evaluation leg (the falsifiability proof).** Wrap the scenario run in
   `unittest.mock.patch.object(ValidityField, "get_all_keys", wraps=ValidityField.get_all_keys)`
   and assert:
   - (i) for the **arm-none** scenario, **no** entry in `mock.call_args_list` has
     that scenario's `_model_class` as its first positional argument; and
   - (ii) for a **validity-declaring** scenario, exactly such a call **is**
     present.
   Removing the `"validity" in self._model_class._meta.fields` guard turns (i)
   red immediately. This leg — and only this leg — is what Success Criterion 5
   is measured against.
2. **Key-deletion leg (independent, catches branch *deletion*).** Run a scenario
   whose class **does** declare validity, seed under
   `ValidityField.get_prefix_db_key(scenario._model_class, "validity")`, and
   assert `teardown()` removes it. Keep this, but it must **not** stand alone as
   the falsifiability proof.

Match the spy on identity (`call.args[0] is scenario._model_class`), not on the
class *name*, so a same-named class from another factory cannot satisfy it.

If the builder cannot make leg 1 non-vacuous, it must say so explicitly in the PR
body rather than ship a green test that cannot fail.

## Failure Path Test Strategy

### Exception Handling Coverage

`ExternalScenario.teardown()` is built from four `try: … except Exception: pass`
blocks (`scenarios/external_base.py:924-1012` at HEAD; the plan's original
`919-1010` was measured against baseline `9986c086` — nit N3, no semantic drift), and `_build_refusal_model`'s
companion `_teardown_model` has the same shape. This plan does **not** convert
them to logging handlers — swallowing is deliberate in a teardown path (a
teardown failure must not abort a 500-item run), and changing it is out of
scope. Instead, the observable behavior each `except` is hiding is asserted
directly:

- [ ] The new construction-invariant test asserts on `_meta.db_class_key`, a
      pure in-memory property, so it cannot be masked by a swallowed Redis error.
- [ ] The new post-teardown leak test asserts **zero** keys matching
      `*ExternalBenchmarkMemory*` and zero matching `*{class_name}*` after
      `teardown()` — a swallowed exception in any of the four blocks surfaces as
      surviving keys rather than as a silent pass. This is the existing
      `test_no_leaked_validity_keys_after_teardown` shape, widened from validity
      keys to the whole keyspace.

### Empty/Invalid Input Handling

- [ ] `safe_prefix=""` — every factory would produce class name `ExtMem`
      (`RecipeMem`, `Assoc`, `RefusalMem`), which is still a valid, distinct
      Python identifier and a valid Redis key component. It is degenerate, not
      erroneous: two empty-prefix classes would collide with each other exactly
      as today's classes do. Document the behavior; do not add validation — no
      caller can produce it (`safe_prefix` is always a non-empty hash slice).
- [ ] `safe_prefix` containing `:` or `-` — **claim narrowed per critique C4.**
      Two of the five factories sanitize inside the factory
      (`prefix.replace(":", "").replace("-", "")[:8]` in
      `_build_recipe_model_class`, and the equivalent in
      `ExternalScenario.setup()`). The other three do **not**:
      `association_recall.py::_build_model` and
      `test_confidence_gate_refusal.py::_build_refusal_model` take `prefix`
      through unmodified. Re-verified at plan-revision time — their callers pass
      `uuid.uuid4().hex[:8]` (`association_recall.py:172`,
      `test_confidence_gate_refusal.py:208,383`), which is clean hex, so the
      claim *holds in practice* but is a property of the **callers**, not of the
      factories. A `:` reaching a class name would corrupt the colon-delimited
      key scheme everywhere `DB_key` composes (`src/popoto/fields/field.py:629`).
      Still do **not** add validation — no caller can produce it. Instead the new
      invariant test is the enforcement, and it must be parametrized over each
      factory's **real caller-supplied prefix expression**, not a hand-picked
      clean hex literal. A test that feeds in a literal it chose itself restates
      the claim; one that feeds in what the caller actually passes can falsify
      it.
- [ ] No agent-output processing is involved; no empty-output loop risk.

### Error State Rendering

Not user-visible. The harness's only output is a benchmark report; a failed
teardown manifests as leaked keys, which the new leak test asserts against
rather than rendering.

## Test Impact

- [ ] `tests/benchmarks/test_external.py::TestSupersessionArm::test_no_leaked_validity_keys_after_teardown`
      — **UPDATE for coverage, not for vacuity (critique C2).** An earlier draft
      of this plan claimed the test asserts absence of
      `$ValidityF:ExternalBenchmarkMemory:validity:*` and would go vacuous. That
      is a misreading: the real assertion is
      `scan(cursor, match="$ValidityF:*", count=200)`
      (`tests/benchmarks/test_external.py:1220`) — a field-type-wide wildcard
      that keeps matching post-fix keys under any class name. **The test is not
      vacuous today and does not become vacuous.** Do not go looking for a
      literal that is not in the file. The update is still worth making, for a
      different reason: `$ValidityF:*` misses `$Class:*`, `$ConfidencF:*`,
      `$KeyF:*`, `$DecayingSortF:*` and the record hashes that spike-2 showed are
      equally affected. Widen it to assert (a) zero keys matching
      `*ExternalBenchmarkMemory*` anywhere, and (b) zero keys matching
      `*{model_class.__name__}*` after teardown.
- [ ] `tests/benchmarks/test_external.py::TestSupersessionArm::test_teardown_on_arm_none_is_real_noop`
      — **REBUILD, not update (critique C3).** This test *does* go vacuous after
      the fix, and re-deriving its key names from the per-item class name does
      not repair it, because the arm-none class declares no `validity` field.
      **Nor does a before/after key count on the arm-none class's own keyspace
      (C7)** — `get_all_keys()` never checks whether the model declares
      `validity`, so an unconditional DEL there is a silent no-op and the count
      is identical with and without the guard. Replace the foreign-sentinel
      assertion with the two-leg assertion specified in Technical Approach
      ("Explicit-validity branch in `teardown()`"): a `patch.object(ValidityField,
      "get_all_keys", wraps=...)` **guard-evaluation** leg (the falsifiability
      proof) plus an independent **key-deletion** leg. This is the highest-risk
      item in the test set: it is the test the plan cites as the reason to keep
      the explicit validity branch, so a vacuous version removes that
      justification silently — and it has now been specified vacuously once
      already.
- [ ] `tests/benchmarks/test_external.py::TestStaleKeySweep::test_sweeps_all_stale_patterns_and_reports_count`
      — **UPDATE**: add the post-fix key shapes it must now sweep
      (`$ValidityF:ExtMem12345678:validity:valid_from`,
      `$Class:ExtMem12345678`, `$KeyF:ExtMem12345678:agent_id:x`) to the `stale`
      dict, and keep `ExternalBenchmarkMemory:abc` so pre-fix residue stays
      covered.
- [ ] `tests/benchmarks/test_external.py::TestStaleKeySweep::test_custom_patterns_sweep_only_matching_keys`
      — **UPDATE**: it asserts `ExtMem12345678:xyz` *survives* a CSR-patterned
      sweep. Still true, but re-check that the widened default patterns are not
      what is being passed; the assertion that `SomeOtherModel:keepme` survives
      is the one that must hold against the new `*:ExtMem*` glob.
- [ ] `tests/benchmarks/test_external.py:712-713` (embedding-listener test using
      the literal `"ExtMemLeakCheck"`) — **no change**: it names a class directly
      and never goes through a factory.
- [ ] `tests/benchmarks/test_supersession_axis.py:159` — **no change**: the
      literal `"ExternalBenchmarkMemory:incumbent"` is an opaque fake return
      value in a `_FakeResult`, not a key that is ever written or matched.
- [ ] `tests/benchmarks/test_confidence_gate_refusal.py` — **UPDATE** if it
      asserts on model-class key shapes; otherwise no change beyond the factory
      body itself.
- [ ] `tests/benchmarks/test_defaults_sync.py` — **no change**: this plan adds no
      `Defaults` constant. (Named explicitly because narrow-scope lane test
      selection routinely misses it and it then fails in CI after review.)
- [ ] **NEW** `tests/benchmarks/test_model_class_namespacing.py` — the
      construction invariant, one parametrized case per factory.

No `src/` tests are affected — no `src/` file is modified.

## Rabbit Holes

- **"While we're here, make `db_class_key` lazy in `src/`."** This is the
  issue's option 1 and it is a library-wide behavior change with seven call
  sites and a persistence-stability guarantee behind it. It also is not needed:
  option 2 fixes every symptom. Do not open this.
- **Deleting `teardown()`'s explicit validity branch because "the SCAN covers it
  now."** Tempting and wrong twice over: the SCAN would only cover it after the
  `*{class_name}*` widening also lands (so deleting it before that is a
  regression), and `test_teardown_on_arm_none_is_real_noop` exists specifically
  to catch an unconditional cleanup masking a leak. Keep the branch; rewrite its
  comment. Note that this rabbit hole's second half is *conditional on that test
  still being able to fail* — see C3 in Technical Approach; the test must be
  rebuilt in the same change, or this justification quietly evaporates.
- **Rewriting the sweep to `FLUSHDB` on the bench database.** Faster, obviously
  correct, and prohibited by this repo's DB-0 doctrine — `run_external.py`
  resolves its DB from `POPOTO_BENCH_DB` and a misconfiguration would meet
  popoto's `Db0FlushRefusedError` rather than a clean failure. SCAN+DEL with
  correct patterns is the shape the repo has already settled on (#465, #490).
- **Unifying the five factories into one generic builder.** They differ in
  bases (`WriteFilterMixin`), field sets, key-field names (`turn_id` / `mem_id`
  / `memory_key`), and post-hoc relationship registration. A shared builder
  would need a parameter per difference and would couple five independent
  benchmarks. Convert them in place.
- **Chasing the `$BM25:ExtMemaaa:…:tf:ExternalBenchmarkMemory:x:<uuid>` shape as
  a separate bug.** It is not one — it is the record key embedded in the BM25
  term-frequency key, and it corrects itself the moment the record key does.

## Risks

### Risk 1: A widened glob sweeps or tears down keys it should not
**Impact:** `*:ExtMem*` and `*{class_name}*` are broader than the patterns
they join. On the dedicated bench DB (14) or the pytest DB (15) this is
harmless, but a run misconfigured onto a shared database could delete unrelated
keys.
**Mitigation:** Both globs remain anchored on the `ExtMem`/`{class_name}`
literal, which is a hash-suffixed benchmark-only prefix; `run_external.py`
already rejects DB 0 and defaults to 14. The updated
`test_custom_patterns_sweep_only_matching_keys` keeps its `SomeOtherModel:keepme`
survivor assertion, which is precisely the over-reach detector.

Note the two globs are bounded differently, and C1 changed one of them.
`teardown()`'s `*{class_name}*` is unanchored on both sides but carries the
**full hash-suffixed class name** (`ExtMem<hash>`), so its breadth is bounded by
the per-item name, not by punctuation — dropping the colon anchors costs
nothing. `_STALE_KEY_PATTERNS`' `*:ExtMem*` has no hash and is bounded by its
leading `:` anchor instead; do **not** generalize *that* one to a bare
`*ExtMem*`.

### Risk 2: The updated leak test passes vacuously
**Impact:** The single highest-value assertion here is "no keys survive
teardown". Written carelessly against a literal that no longer exists, it goes
green while the defect persists — the exact shape of the #661 trap and of the
four stale spy tests CLAUDE.md describes.
**Mitigation:** The test must first assert that the pre-teardown keyspace is
**non-empty** and contains the expected `ExtMem<hash>` names, then assert it is
empty after. Red-state proof: run the new tests against `HEAD~1` (pre-fix) and
paste the failures into the PR.

### Risk 3: `type()` conversion silently drops a class attribute
**Impact:** `_build_recipe_model_class` carries a method and two mixin config
attributes; `_build_graph_model_class` and `_build_model` carry post-hoc
relationship registration. A dropped attribute changes what a benchmark
measures without failing loudly.
**Mitigation:** The invariant test asserts, per factory, that the produced class
exposes the same `_meta.fields` key set as a reference list, and that
`WriteFilterMixin` is still in `RecipeMemory.__mro__`. Benchmarks are not run in
CI, so a diff-level review of each converted body is a named review task.
Critique C5 tightens this: the 1:1 conversion rule (one `type()` per existing
`class` block) exists precisely to keep that review tractable. If the builder
nonetheless consolidates any site's variants into a single conditional namespace
dict, the Task 4 diff review must produce an **explicit per-variant field-name
checklist in the PR body**, enumerating every field of every pre-conversion
variant against the post-conversion result — a visual scan is not sufficient.

### Risk 4: Concurrent SDLC lanes on shared Redis DB 15 produce phantom failures
**Impact:** 73-158 spurious failures have been observed; a reviewer could read
them as regressions from this change.
**Mitigation:** Set `POPOTO_TEST_DB=<n>` for this lane (Prerequisites table) and
state the DB alongside every count reported from a test run, per repo doctrine.
`tests/test_version.py::test_version_matches_pyproject` fails by construction on
a stale editable install — expected noise, not a regression.

## Race Conditions

No race conditions identified. Every path touched here is synchronous and
single-threaded: the factories are pure class construction, `teardown()` and
`_sweep_stale_benchmark_keys` are sequential SCAN+DEL loops, and benchmark items
run strictly one at a time (`external_base.py`'s
`stop_invalidation_listeners()` comment states the sequential-item invariant
explicitly, and relies on it).

One adjacent concurrency fact is worth stating because it is *improved* rather
than introduced: today two benchmark processes running against the same
database would corrupt each other's `$ValidityF:ExternalBenchmarkMemory:validity:*`
and `$ConfidencF:ExternalBenchmarkMemory:certainty:data` regardless of item
ordering, since those keys are name-shared across every class. After this fix
they are namespaced per item, so the only remaining cross-process collision
would require a `safe_prefix` hash collision.

## No-Gos (Out of Scope)

Nothing deferred — every relevant item is in scope for this plan.

The two alternatives the issue raises (lazy `db_class_key` in `src/`; an
override hook on `ValidityField`) are **rejected on the merits**, not deferred;
the reasoning is recorded in Technical Approach so a later reader does not
mistake rejection for a missing follow-up. No issue is filed for either, because
neither should be done.

## Update System

No update-system changes required. popoto is a published library plus an mkdocs
site; this change lives entirely under `tests/benchmarks/`, adds no dependency,
no config file, and no extra. Since #689 `MANIFEST.in` carries `prune tests`, so
nothing in this plan reaches the published sdist or wheel — there is no consumer
propagation path at all. `uv.lock` is untouched.

## Agent Integration

No agent integration required. Nothing here is reachable from an agent or tool
surface: these are benchmark-harness factory functions invoked by
`tests/benchmarks/run_external.py` and by pytest. popoto exposes no MCP tool
that constructs benchmark model classes.

## Documentation

### Feature Documentation

- [ ] `tests/benchmarks/README.md:267-274` — rewrite the "**Known limitation**"
      paragraph under "External-harness supersession axis (#692)". It currently
      states the shared-`ValidityField`-namespace defect as a live fact and
      points at "the follow-up issue filed from #692". Replace with a short
      statement that per-item classes are built with `type()` so
      `_meta.db_class_key` is correct from creation, and cite #701.
- [ ] `tests/benchmarks/README.md` — while there, correct the same understatement
      the issue made: note that the defect covered `$Class`, `$KeyF`,
      `$DecayingSortF`, `$ConfidencF` and record keys too, not only
      `$ValidityF`. This is the durable record of what was actually wrong.
- [ ] No `docs/features/` page exists for the benchmark harness and none is
      warranted — `tests/benchmarks/README.md` is the harness's documentation
      and is the correct home for this.

### External Documentation Site

- [ ] No mkdocs page changes. `tests/benchmarks/README.md` is not part of the
      docs site (`mkdocs.yml` sources `docs/`), so `mkdocs build --strict`
      is unaffected. Run it anyway as a regression gate.

### Inline Documentation

- [ ] `scenarios/external_base.py::teardown` — rewrite the 18-line comment at
      lines 930-947 (re-measured at HEAD; nit N3). Its premise ("these five keys are SHARED across every item
      in a run") becomes false. The branch stays; the justification changes from
      *required for correctness* to *targeted cleanup of keys that the
      prefix-anchored SCANs do not reach*, and it must still say why the
      declared-field guard (not an arm guard) is the right condition.
- [ ] Each of the five converted factories — add the one-line docstring note the
      `siq`/`csr`/`rlt` factories already carry: built with `type()` so the
      metaclass captures the unique `db_class_key` at creation; a post-hoc
      `__name__` rename would not reach `_meta`.
- [ ] `run_external.py:107-111` — update the `_STALE_KEY_PATTERNS` comment to
      explain the added glob and why `ExternalBenchmarkMemory:*` is retained
      (pre-fix residue).
- [ ] `docs/plans/sdlc-692.md` — **read-only**. It is a shipped plan and a
      historical record; its Technical Approach step 5 described the world
      correctly at the time. Do not edit it. Note the supersession in
      `sdlc-701.md` instead (this plan is that note).

## Success Criteria

- [ ] For all five factories, `cls._meta.db_class_key.redis_key == cls.__name__`
      and the name carries the per-item prefix.
- [ ] Two per-item classes from the same factory with different prefixes share
      **zero** Redis key prefixes: a save on each produces two disjoint key sets
      (the spike-2 reproduction, re-run, now showing no `ExternalBenchmarkMemory`
      key at all).
- [ ] `$ValidityF`, `$ConfidencF`, `$Class`, `$KeyF`, `$DecayingSortF` and record
      keys all carry `ExtMem<hash>` after the fix.
- [ ] After `ExternalScenario.teardown()`, zero keys match `*ExtMem<hash>*` and
      zero match `*ExternalBenchmarkMemory*`, asserted after first proving the
      pre-teardown keyspace was non-empty. (Satisfiable only with the unanchored
      `*{class_name}*` teardown glob — C1.)
- [ ] `test_teardown_on_arm_none_is_real_noop` is **falsifiable after the fix**,
      proved by its **guard-evaluation leg** (C7): with
      `patch.object(ValidityField, "get_all_keys", wraps=...)` in place, the test
      asserts no call carries the arm-none scenario's `_model_class` as first
      positional argument, and that a validity-declaring scenario *does* produce
      such a call. Demonstrate it goes red when the
      `"validity" in self._model_class._meta.fields` guard is removed (i.e. made
      unconditional). **A key-count assertion cannot satisfy this criterion** —
      `get_all_keys()` derives names from `_meta.db_class_key` alone
      (`validity_field.py:781-796` → `field.py:629`), so on the arm-none class an
      unconditional DEL removes nothing and a before/after key count is identical
      in both builds (C7). The key-deletion leg is a separate, independent
      assertion that catches branch *deletion*; it does not count as this proof.
- [ ] `_sweep_stale_benchmark_keys` removes both pre-fix
      (`ExternalBenchmarkMemory:*`) and post-fix (`$…:ExtMem*`) residue.
- [ ] Converted `RecipeMemory` still has `WriteFilterMixin` in its MRO and still
      exposes `compute_filter_score`.
- [ ] Converted graph/assoc classes still expose their self-referential
      `Relationship` in `_meta.fields`.
- [ ] `grep -rn '\.__name__ = ' tests/ src/ scripts/` returns no matches.
- [ ] `tests/benchmarks/README.md` no longer describes the limitation as live.
- [ ] Tests pass (`/do-test`), with `POPOTO_TEST_DB` set for this lane and the DB
      number stated alongside the count.
- [ ] `ruff check src/`, `black --check src/ tests/`, `scripts/mypy_ratchet.py`,
      `mkdocs build --strict` all clean.
- [ ] Documentation updated (`/do-docs`).
- [ ] No `src/` file modified (anti-criterion — see Verification).

## Team Orchestration

### Team Members

Revised per critique C6 — the two builders below were merged into one. They had
`Depends On: none`, were both `Parallel: true`, touched disjoint files, and
shared no state, so a second agent bought a handoff and no concurrency.

- **Builder (all five factories)**
  - Name: `harness-factory-builder`
  - Role: convert all five `type()` sites — the two `external_base.py`
    factories, `recipe_base.py`, `association_recall.py`,
    `test_confidence_gate_refusal.py` — and update `teardown()` +
    `_STALE_KEY_PATTERNS`
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Test engineer (namespacing invariant + leak tests)**
  - Name: `namespace-test-engineer`
  - Role: write the new invariant test and update the four affected
    `test_external.py` cases; produce red-state proof against pre-fix HEAD
  - Agent Type: test-engineer
  - Resume: true

- **Documentarian**
  - Name: `bench-documentarian`
  - Role: `tests/benchmarks/README.md`, the `teardown()` comment, factory
    docstrings, `_STALE_KEY_PATTERNS` comment
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: `namespace-validator`
  - Role: verify all Success Criteria and run the Verification table
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Convert all five factories, and move the two glob patterns with them
- **Task ID**: build-factories
- **Depends On**: none
- **Validates**: `tests/benchmarks/test_external.py`,
  `tests/benchmarks/test_supersession_axis.py`,
  `tests/benchmarks/test_confidence_gate_refusal.py`
- **Informed By**: spike-1 (rename never reaches `_meta`), spike-2 (four key
  families affected, not one), spike-3 (five sites; `type()` viable at each;
  post-hoc `Relationship` and `with_validity` blocks stay post-hoc), critique
  C1 (glob shape), C5 (1:1 conversion, no dedup)
- **Assigned To**: `harness-factory-builder`
- **Agent Type**: builder
- **Parallel**: false

*Merged from the former Tasks 1 and 2 per critique C6 — they had no ordering
constraint and touched disjoint files, so splitting them bought a handoff and no
concurrency.*

**Conversion rule for every site: one literal `type()` call per existing `class`
block, each with its own literal namespace dict (C5).** Do not consolidate
variants behind a conditionally-assembled dict. Set `__module__` and
`__qualname__` in each dict. The diff must stay line-for-line comparable against
the pre-conversion bodies — that comparability is the only guard against Risk 3
in a harness CI never runs.

- Convert `_build_external_model_class` (`scenarios/external_base.py:186`) —
  three `type()` calls, one per mutually exclusive variant (bm25+embedding /
  embedding-only / bm25-only). Keep the `with_validity` block after the `type()`
  call, unchanged.
- Convert `_build_graph_model_class` (`scenarios/external_base.py:127`). Keep the
  post-`type()` self-referential `prev_turn` `Relationship` + `_meta.add_field`
  block, unchanged and post-hoc.
- Convert `_build_recipe_model_class` (`scenarios/recipe_base.py:35`). Bases
  tuple must stay `(WriteFilterMixin, popoto.Model)` in that order; hoist
  `compute_filter_score` to a local `def` just above the `type()` call and put it
  in the namespace dict along with `_wf_min_threshold` and
  `_wf_priority_threshold`.
- Convert `_build_model` (`association_recall.py:103`) — one `type()` per
  `with_cooccur` variant. Keep the post-`type()` `related` `Relationship`
  registration.
- Convert `_build_refusal_model` (`test_confidence_gate_refusal.py:152`).
- Delete all five `__name__`/`__qualname__` assignment pairs
  (`external_base.py:172-173,287-288`, `recipe_base.py:73-74`,
  `association_recall.py:152-153`, `test_confidence_gate_refusal.py:168`).
- In `ExternalScenario.teardown()`, add a second SCAN pass over
  **`*{class_name}*`** alongside the existing `{class_name}:*` pass. **Not
  `*:{class_name}:*`** — that form cannot match `$Class:ExtMem<hash>`, which has
  nothing after the class name, and would leave Success Criterion 4
  unsatisfiable (critique C1). Keep the explicit validity branch and the
  agent-prefix pass.
- In `run_external.py`, add `*:ExtMem*` to `_STALE_KEY_PATTERNS`, retaining
  `ExternalBenchmarkMemory:*`, `ExtMem*`, and `$BM25:ExtMem*`. The sweep's
  leading-colon form **is** correct — `$Class:ExtMem12345678` has a colon before
  the class name — so only the `teardown()` companion drops the anchor.

### 2. Namespacing invariant + leak tests
- **Task ID**: build-namespace-tests
- **Depends On**: none (write first, expect red)
- **Validates**: `tests/benchmarks/test_model_class_namespacing.py` (create),
  `tests/benchmarks/test_external.py`
- **Informed By**: spike-2 (exact key families to assert), Risk 2 (vacuity),
  critique C2 (what the leak test really asserts), C3 (arm-none rebuild), C4
  (parametrize on real prefixes), C1 (derive the glob independently)
- **Assigned To**: `namespace-test-engineer`
- **Agent Type**: test-engineer
- **Parallel**: true
- Create `tests/benchmarks/test_model_class_namespacing.py`: parametrized over
  all five factories, assert `_meta.db_class_key.redis_key == cls.__name__`,
  that the name carries the prefix, and that `":" not in db_class_key.redis_key`.
  **Parametrize on each factory's real caller-supplied prefix expression**
  (`uuid.uuid4().hex[:8]` for `association_recall` and
  `test_confidence_gate_refusal`; the `setup()`/`_build_recipe_model_class`
  sanitized forms elsewhere), not on a hand-picked clean hex literal — only three
  of five factories sanitize internally, so a self-chosen literal restates the
  claim instead of testing it (C4).
- In the same file, assert per factory that `_meta.fields` matches a reference
  key set (Risk 3), that `WriteFilterMixin in RecipeMemory.__mro__`, and that
  the self-referential relationship fields survive conversion.
- Add a two-prefix disjointness test: build two classes, save one record each,
  assert the two key sets are disjoint and that **no** key contains
  `ExternalBenchmarkMemory`.
- Widen `test_no_leaked_validity_keys_after_teardown` for **coverage** (C2): its
  current `match="$ValidityF:*"` (`test_external.py:1220`) is not vacuous
  post-fix, but it misses `$Class:*`, `$ConfidencF:*`, `$KeyF:*`,
  `$DecayingSortF:*` and the record hashes. Assert non-empty pre-teardown, then
  zero keys matching `*ExternalBenchmarkMemory*` and zero matching
  `*{model_class.__name__}*` after. **Derive that glob in the test itself** — do
  not import or reuse `teardown()`'s pattern constant (C1).
- **Rebuild** `test_teardown_on_arm_none_is_real_noop` (C3, respecified by C7) —
  do not merely re-point its key names, and **do not use a key count as the
  falsifiability proof**. Its foreign sentinel becomes unreachable once the
  classes stop sharing a namespace; the arm-none class declares no `validity`
  field to seed; and because `ValidityField.get_all_keys()` derives names from
  `_meta.db_class_key` alone (`validity_field.py:781-796` → `field.py:629`),
  removing the guard makes the DEL an unconditional no-op that no
  before/after count on that class can detect (C7). Implement the two legs from
  Technical Approach:
  - **(a) Guard-evaluation leg — the proof.** Wrap the scenario run in
    `unittest.mock.patch.object(ValidityField, "get_all_keys", wraps=ValidityField.get_all_keys)`.
    Assert (i) for the arm-none scenario, **no** `mock.call_args_list` entry has
    that scenario's `_model_class` as `call.args[0]` (compare by `is`, not by
    class name); and (ii) for a validity-declaring scenario, exactly such a call
    is present. Verify this leg goes **red** when the
    `"validity" in self._model_class._meta.fields` guard is deleted — that
    mutation run is the evidence Success Criterion 5 asks for; paste it in the PR.
  - **(b) Key-deletion leg — independent.** Seed under a validity-declaring
    scenario's own `ValidityField.get_prefix_db_key(...)` and assert `teardown()`
    removes it. This catches *deletion* of the branch; it is not the
    falsifiability proof.
  If leg (a) cannot be made falsifiable, say so in the PR rather than ship a
  green test that cannot fail.
- Update both `TestStaleKeySweep` cases with post-fix key shapes, keeping the
  `SomeOtherModel:keepme` survivor assertion (Risk 1).
- **Red-state proof**: run the new/updated tests against pre-fix `HEAD`, capture
  the failures, and paste them into the PR description.

### 3. Validate the conversions
- **Task ID**: validate-conversions
- **Depends On**: build-factories, build-namespace-tests
- **Assigned To**: `namespace-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run the Verification table. Diff-review each converted factory body
  field-by-field against its pre-conversion form (benchmarks are not run in CI —
  this review is the only guard against a silently dropped attribute).
- Re-run the spike-2 reproduction and confirm zero `ExternalBenchmarkMemory` keys.
- Report pass/fail with the Redis DB number and package versions stated.

### 4. Documentation
- **Task ID**: document-namespacing
- **Depends On**: validate-conversions
- **Assigned To**: `bench-documentarian`
- **Agent Type**: documentarian
- **Parallel**: false
- Rewrite `tests/benchmarks/README.md`'s "Known limitation" paragraph; record
  the corrected (wider) scope of what was broken.
- Rewrite the `teardown()` validity-branch comment (`external_base.py:930-947`).
- Add the `type()` rationale line to all five factory docstrings.
- Update the `_STALE_KEY_PATTERNS` comment.
- Do **not** edit `docs/plans/sdlc-692.md`.

### 5. Final validation
- **Task ID**: validate-all
- **Depends On**: document-namespacing
- **Assigned To**: `namespace-validator`
- **Agent Type**: validator
- **Parallel**: false
- Full suite with `POPOTO_TEST_DB` set; `ruff check src/`;
  `black --check src/ tests/`; `scripts/mypy_ratchet.py`;
  `mkdocs build --strict`.
- Confirm every Success Criteria checkbox, including the no-`src/`-change
  anti-criterion.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Benchmark harness tests pass | `python -m pytest tests/benchmarks/ -q` | exit code 0 |
| Namespacing invariant tests pass | `python -m pytest tests/benchmarks/test_model_class_namespacing.py -q` | exit code 0 |
| External-harness tests pass | `python -m pytest tests/benchmarks/test_external.py -q` | exit code 0 |
| Full suite passes | `python -m pytest -q` | exit code 0 |
| Lint clean | `python -m ruff check src/` | exit code 0 |
| Format clean | `python -m black --check src/ tests/` | exit code 0 |
| Type ratchet holds | `python scripts/mypy_ratchet.py` | exit code 0 |
| Docs build | `python -m mkdocs build --strict` | exit code 0 |
| No post-hoc class renames remain | `grep -rn '\.__name__ = ' tests/ src/ scripts/ \| wc -l` | match count == 0 |
| Anti-criterion — no `src/` file modified | `git diff --name-only origin/main...HEAD -- src/ \| wc -l` | match count == 0 |
| Anti-criterion — `ValidityField` gains no override hook | `git diff origin/main...HEAD -- src/popoto/fields/validity_field.py \| wc -l` | match count == 0 |
| Anti-criterion — `db_class_key` not made lazy | `git diff origin/main...HEAD -- src/popoto/models/base.py \| wc -l` | match count == 0 |
| Anti-criterion — sdlc-692 plan untouched | `git diff --name-only origin/main...HEAD -- docs/plans/sdlc-692.md \| wc -l` | match count == 0 |
| Teardown's second SCAN uses the unanchored glob (C1) | `grep -c 'match=f"\*{class_name}\*"' tests/benchmarks/scenarios/external_base.py` | output == 1 |
| Sweep covers post-fix key shapes | `grep -c ':ExtMem\*' tests/benchmarks/run_external.py` | output > 0 |
| README limitation note updated | `grep -c 'Known limitation' tests/benchmarks/README.md` | output > 0 |
| No shared base-class keys survive a two-class save | `python -m pytest tests/benchmarks/test_model_class_namespacing.py -q -k disjoint` | exit code 0 |

## Critique Results

### Round 2 (2026-09-08) — verdict: READY TO BUILD (with concerns)

FULL depth, independent roster (Risk & Robustness, Scope & Value, History &
Consistency). **0 blockers, 3 concerns, 1 nit.** Critique cycle 2 of 2, so the
concerns below are accepted on the record; embed their implementation notes and
build.

**Round-1 fold-in verified as landed.** The revised body was re-read against
source, not against the resolution table. C1's `*{class_name}*` shape is stated
identically in Technical Approach "Pattern updates", Flow, Task 1, Success
Criterion 4 and the Verification row; C2's vacuity claim is retracted and the
real assertion (`match="$ValidityF:*"`, `test_external.py:1220`) is quoted
correctly; C4's parametrize-on-real-caller-expression rule is in both Failure
Path Test Strategy and Task 2; C5's 1:1 `type()` rule is in Technical Approach,
Task 1 and Risk 3; C6's task merge landed (6 tasks → 5, `build-factories`, and
`validate-conversions` depends on the merged ID). N2 and N3 are corrected. No
round-1 finding is unaddressed.

Three of the four findings below are **new defects introduced or left by the
revision itself**, not re-litigation.

#### R2-C7 — The C3 replacement test is not falsifiable by the mutation Success Criteria names as its proof

*Critic: Risk & Robustness (Adversary). Verified independently against source.*
*Location: Technical Approach ("Explicit-validity branch in `teardown()`");
Test Impact; Success Criteria bullet 5; Task 2.*

The revision replaced `test_teardown_on_arm_none_is_real_noop` with a two-part
assertion, and added a Success Criterion that the replacement is "**falsifiable
after the fix**: demonstrated by showing it goes red when the explicit validity
branch's guard is removed (i.e. made unconditional)." The specified replacement
cannot go red under that mutation.

`ValidityField.get_all_keys()` (`src/popoto/fields/validity_field.py:781-796`)
computes its five key names *purely* from `_meta.db_class_key`, via
`get_special_use_field_db_key` → `DB_key(cls.field_class_key,
model._meta.db_class_key, *field_names)` (`src/popoto/fields/field.py:629`). It
never consults whether the model declares a `validity` field. On the arm-none
class those five names were therefore never written, so an *unconditional*
`get_REDIS_DB().delete(*keys.values())` deletes nothing. Part (b) — "assert the
arm-none class produced zero `$ValidityF:{arm_none_class_name}:*` keys, before
and after teardown" — is true in both the guarded and the unguarded build. Part
(a) is falsifiable by *deleting the branch*, but not by *removing its guard*,
which is the mutation the criterion names. So the plan replaces one vacuous test
with another and ships a build gate that the specified code cannot pass. This is
the #661 trap surviving its own remedy.

**Implementation Note:** assert that the branch was *evaluated conditionally*,
not that its output looks empty. Wrap the scenario run in
`unittest.mock.patch.object(ValidityField, "get_all_keys", wraps=ValidityField.get_all_keys)`
and assert (i) for the arm-none scenario, no entry in `mock.call_args_list` has
that scenario's `_model_class` as its first positional argument, and (ii) for a
validity-declaring scenario, exactly such a call is present. Removing the
`"validity" in self._model_class._meta.fields` guard turns (i) red immediately,
which a key-count assertion never can. Keep part (a)'s key-deletion assertion as
a second, independent leg — it catches deletion of the branch — but do not let
it stand alone as the falsifiability proof.

#### R2-C8 — The new `teardown()` SCAN pass would inherit a stale `POPOTO_REDIS_DB` snapshot, silently scanning the wrong database

*Critic: Structural check (independent). Verified against source and CLAUDE.md.*
*Location: Step by Step Tasks > Task 1 ("add a second SCAN pass over
`*{class_name}*` alongside the existing `{class_name}:*` pass").*

`tests/benchmarks/scenarios/external_base.py:83` holds
`from src.popoto.redis_db import POPOTO_REDIS_DB, get_REDIS_DB` — a **plain
module-level import** of the rebindable global, exactly the shape CLAUDE.md
names as stale ("Do not add a new one"). Inside `teardown()` the two idioms are
already mixed: the explicit validity branch correctly calls `get_REDIS_DB()`
(lines 949, 955, 959), while the class-name SCAN and the agent-prefix SCAN use
the snapshot (`POPOTO_REDIS_DB.scan/.delete` at lines 971, 975, 986, 990).
Task 1 says to add the new pass "alongside the existing `{class_name}:*` pass",
so a builder mirroring the adjacent line inherits the stale binding.

`tests/test_connection.py:117` calls `set_REDIS_DB_settings(host=..., port=...)`,
which *rebinds* `redis_db`'s module global. After that point the snapshot at
line 83 addresses the pre-reconfiguration client. The plan's own highest-value
new assertion — Success Criterion 4, "after `teardown()`, zero keys match
`*ExtMem<hash>*`" — is written to be checked through `get_REDIS_DB()` (the
existing leak test at `test_external.py:1207` already does). So in a **full-suite
run** teardown would sweep one database while the assertion reads another: the
widened glob deletes nothing observable and the leak test fails, and the failure
presents as a namespacing regression rather than as a connection-binding bug.
File-scoped it passes. This is precisely the #655/#661 full-suite-only failure
mode CLAUDE.md documents.

**Implementation Note:** write the new pass as
`get_REDIS_DB().scan(cursor, match=f"*{class_name}*", count=200)` /
`get_REDIS_DB().delete(*keys)` — never `POPOTO_REDIS_DB.scan`. Convert the four
adjacent pre-existing call sites (`external_base.py:971,975,986,990`) in the same
edit so the function has one idiom, and drop `POPOTO_REDIS_DB` from the line-83
import if no other site in the file uses it. This is a two-line-per-site
mechanical change, in scope because Task 1 is already editing this function; it
is not the #655 sweep. Add it to Task 1's bullet list and to the Task 3
diff-review checklist.

#### R2-C9 — The C6-revised Appetite line still contradicts Team Orchestration

*Critics: Scope & Value (Simplifier) and History & Consistency (Consistency
Auditor) — independently, the strongest convergence this round.*
*Location: Appetite ("Team") vs. Team Orchestration.*

C6's fix rewrote the Appetite team line but described the wrong roster. It reads
"Two builders (one for the harness conversion + tests, one validator) plus a
documentarian and a code reviewer". Team Orchestration names four members with
Agent Types builder / test-engineer / documentarian / validator: exactly **one**
is a builder, the tests are owned by a separate `namespace-test-engineer`, the
validator is not a builder, and **no code-reviewer role exists anywhere in the
plan**. The prose also misstates what C6 merged — the merge was builder+builder
(`ext-factory-builder` + `sibling-factory-builder`), never builder+test-engineer.
So the section C6 existed to reconcile is still inconsistent, in a new way.

**Implementation Note:** replace the Appetite Team line with the roster verbatim:
"One builder (`harness-factory-builder`), one test engineer
(`namespace-test-engineer`), one documentarian (`bench-documentarian`), one
validator (`namespace-validator`) — four roles across five tasks." Delete the
"code reviewer" mention or add the role to Team Orchestration; do not leave the
two sections describing different teams going into build. Purely editorial — no
task, dependency or Success Criterion changes.

#### Nit

**N4 — The C1 Verification row is itself a source-text grep proxy, the shape N1
flagged.** `grep -c 'match=f"\*{class_name}\*"' tests/benchmarks/scenarios/external_base.py`
== 1 (plan Verification table) pins one exact f-string spelling and false-fails
any behaviorally equivalent rewrite; Success Criterion 4 and Task 2's widened
leak test already gate the real behavior. It can only false-fail, never
false-pass, so it is harmless — but N1 asked for exactly this row to become a
real assertion and it did not. Consider dropping it. (Scope & Value)

---

### Round 1 (2026-09-08) — verdict: READY TO BUILD (with concerns)

**Critics**: Risk & Robustness, Scope & Value, History & Consistency (FULL depth)
**Mode**: independent roster (3 critics)
**Findings**: 9 total (0 blockers, 6 concerns, 3 nits)
**Verdict**: READY TO BUILD (with concerns)

### Blockers

None.

### Concerns

**C1 — `teardown()`'s proposed second SCAN glob does not match `$Class:ExtMem<hash>`, and contradicts two other sections of this plan.**
*Critics: Risk & Robustness (Adversary); Structural check (independent).*
*Location: Step by Step Tasks > Task 1; cross-referenced with Flow and Success Criteria.*
Task 1 instructs `*:{class_name}:*`. That glob requires a colon **after** the
class name, so it cannot match `$Class:ExtMem<hash>` — a key that
`ModelOptions.__init__` creates as `DB_key("$Class", db_class_key)`
(`src/popoto/models/base.py:191`) with nothing following the class name. The
existing `{class_name}:*` pass cannot match it either (the key does not *start*
with the class name), and the agent-prefix pass `*{agent_prefix}*` does not
contain it. The Flow section already writes the working form
(`*:ExtMem<hash>*`, no trailing anchor), and Success Criterion 4 ("zero keys
match `*ExtMem<hash>*`") is unsatisfiable under Task 1's literal — so the plan
instructs the builder to write a glob that fails the plan's own criterion.
**Implementation Note:** use `match=f"*{class_name}*"` for the second SCAN pass,
not `match=f"*:{class_name}:*"`. Reconcile Task 1 with the Flow section, which
is the correct version. The new leak test must derive its own match pattern
independently (`*{class_name}*`) rather than reusing `teardown()`'s constant —
copying the glob under test into the test that checks it is how this defect
would go green. The same reasoning applies to `_STALE_KEY_PATTERNS`: `*:ExtMem*`
does match `$Class:ExtMem12345678`, so that one is correct as written; only the
`teardown()` companion is wrong.

**C2 — The Test Impact bullet for `test_no_leaked_validity_keys_after_teardown` misquotes what that test asserts.**
*Critics: History & Consistency (Consistency Auditor); verified independently.*
*Location: Test Impact.*
The plan states the test "currently asserts absence of
`$ValidityF:ExternalBenchmarkMemory:validity:*`" and would therefore go
vacuous. The real assertion is `scan(cursor, match="$ValidityF:*", count=200)`
(`tests/benchmarks/test_external.py:1220`) — a field-type-wide wildcard that
keeps matching post-fix keys under any class name. The test is not vacuous
today and does not become vacuous; the #661 justification for the UPDATE is
built on a misreading.
**Implementation Note:** the update is still worth doing, but for a different
reason — **coverage**, not vacuity. The existing scan covers only `$ValidityF:*`
and misses `$Class:*`, `$ConfidencF:*`, `$KeyF:*`, `$DecayingSortF:*` and the
record hashes that spike-2 showed are equally affected. Widen it to assert zero
keys matching `*ExternalBenchmarkMemory*` **and** zero matching
`*{model_class.__name__}*`, and correct the Test Impact prose so the builder does
not go looking for a literal that is not in the file.

**C3 — `test_teardown_on_arm_none_is_real_noop` becomes genuinely vacuous after this fix, and neither the plan nor the History critic caught it.**
*Critics: Structural check / aggregation cross-validation (contradicts History & Consistency's NIT, which concluded no change was needed).*
*Location: Test Impact; Technical Approach ("Explicit-validity branch in `teardown()`: keep it"); Rabbit Holes.*
The test seeds a sentinel into `ValidityField.get_all_keys(validity_cls,
"validity")` where `validity_cls = _build_external_model_class("teardownchk",
with_validity=True)`, then runs an **arm-none** scenario (a different per-item
class) and asserts the sentinel survives
(`tests/benchmarks/test_external.py:1226-1249`). It is meaningful *today* only
because both classes collapse onto the shared `ExternalBenchmarkMemory`
namespace: a hypothetical unconditional DEL in `teardown()` would hit the very
key the sentinel occupies, turning the test red. **After the fix the sentinel
lives at `$ValidityF:ExtMemteardownchk:validity:*` while the scenario's class is
`ExtMem<item-hash>`** — so no teardown behavior, guarded or unguarded, can
reach the sentinel, and the test passes unconditionally. The plan's guidance
("re-derive its expected key names from the per-item class name") does not
repair this: the arm-none class declares no `validity` field at all, so it has
no validity keys to seed. This is the #661 vacuity trap landing on the one test
the plan's Technical Approach cites as the reason to keep the explicit validity
branch.
**Implementation Note:** rebuild the no-op guard so the sentinel and the
scenario share a class. Concretely: seed the sentinel under
`ValidityField.get_prefix_db_key(scenario._model_class, "validity")` for a
scenario whose class *does* declare validity, and assert the branch fires; then
run the arm-none scenario and assert its own (validity-free) class produced no
`$ValidityF:` keys at all — i.e. replace "a foreign sentinel survives" with
"this item's guard was evaluated and correctly took the no-op path", asserted on
the arm-none class's own keyspace. If that cannot be made non-vacuous, say so in
the PR rather than shipping a green test that cannot fail.

**C4 — The "`safe_prefix` cannot contain `:` or `-`" claim is verified for two of five factories and asserted for all five.**
*Critic: Risk & Robustness (Skeptic).*
*Location: Failure Path Test Strategy > Empty/Invalid Input Handling.*
The plan cites the stripping code in `_build_recipe_model_class` and
`ExternalScenario.setup()`. `association_recall.py::_build_model` and
`test_confidence_gate_refusal.py::_build_refusal_model` take `prefix` directly
with no stripping shown, and their callers were not inspected. A `:` in a class
name corrupts the colon-delimited key scheme everywhere `DB_key` composes
(`src/popoto/fields/field.py:629`).
**Implementation Note:** do not add validation (the plan is right that it is not
needed); instead make the new invariant test the enforcement, and parametrize it
over each factory's **real** caller-supplied prefix expression rather than a
hand-picked clean hex literal, so `":" not in cls._meta.db_class_key.redis_key`
can actually falsify the claim rather than restate it.

**C5 — Task 1 bundles a deduplication refactor into a namespacing correctness fix.**
*Critic: Scope & Value (Simplifier).*
*Location: Step by Step Tasks > Task 1; Technical Approach.*
"a single `type()` call over a conditionally-assembled namespace dict,
collapsing the three duplicated class bodies" is a structural refactor riding
inside a one-line-per-site correctness change. The minimal fix is three literal
`type()` calls mirroring today's three `class` blocks 1:1 — which is also the
shape `siq`/`csr`/`rlt` actually use (each hard-codes one literal namespace
dict). This is the plan's own Risk 3 ("`type()` conversion silently drops a
class attribute") made materially harder to review, in a harness that CI never
runs.
**Implementation Note:** prefer one `type()` call per existing `class` variant,
so the diff is line-for-line comparable. If the consolidated-dict form is kept
anyway, the Task 4 diff review must enumerate every field name in each variant
against the pre-conversion body explicitly — a per-variant field-name checklist
in the PR body, not a visual scan.

**C6 — Appetite and Team Orchestration disagree about the team.**
*Critic: Scope & Value (Simplifier).*
*Location: Appetite vs. Team Orchestration.*
Appetite says "Team: Solo dev, code reviewer"; Team Orchestration names five
agent roles across six tasks. `ext-factory-builder` and
`sibling-factory-builder` both have `Depends On: none`, both are `Parallel:
true`, and they share no state.
**Implementation Note:** merge Tasks 1 and 2 under a single builder (they touch
disjoint files and have no ordering constraint, so one agent doing both in
sequence removes a handoff with no change to output), or correct the Appetite
line. Do not leave the two sections contradicting each other going into build.

### Nits

**N1 — Two Verification rows are grep-count proxies.** `grep -c 'type(' … > 1`
and `grep -c 'class_name' … > 2` do not confirm behavior and are already
subsumed by the invariant/disjointness tests and by the existing
`grep -rn '\.__name__ = '` absence row. (Scope & Value)

**N2 — spike-3 names the recipe factory wrongly.** Spike Results item 3 calls it
`scenarios/recipe_base.py:73` — `build_benchmark_model`. The function is
`_build_recipe_model_class`, defined at `recipe_base.py:35`; line 73 is the
`__name__` assignment inside it. Technical Approach and Task 2 use the correct
name. (Structural)

**N3 — Minor line-number drift against HEAD.** `teardown()` is at
`external_base.py:924-1012` (plan says 919-1010) and its validity comment at
930-947 (plan says 927-944). The plan's baseline was `9986c086`; HEAD is
`06748cfe`. No semantic drift. (Structural)

### Structural Check Results

| Check | Status | Detail |
|-------|--------|--------|
| Required sections | PASS | All plan sections present and non-empty |
| Task numbering | PASS | Tasks 1-6, no gaps |
| Dependencies valid | PASS | All `Depends On` IDs resolve; no cycles |
| File paths exist | PASS | 14 of 15 exist; `tests/benchmarks/test_model_class_namespacing.py` is intentionally new |
| Prerequisites met | PARTIAL | `redis-cli … ping` → PONG; benchmark extras import OK; **`POPOTO_TEST_DB` is unset** — must be set to a non-zero lane-scoped DB before the build lane runs |
| Cross-references | FAIL | Success Criterion 4 (`zero keys match *ExtMem<hash>*`) is unsatisfiable under Task 1's `*:{class_name}:*` glob — see C1 |

All five `__name__` rename sites re-verified present at HEAD `06748cfe`:
`external_base.py:172,287`, `recipe_base.py:73`, `association_recall.py:152`,
`test_confidence_gate_refusal.py:168`.

### Revision Applied

All six concerns and all three nits were folded into the plan body on
2026-09-08. C1–C4 were independently re-verified against the working tree before
editing, not taken on the critique's word:

| Finding | Re-verified | Where the plan now handles it |
|---|---|---|
| C1 glob cannot match `$Class:ExtMem<hash>` | yes | Technical Approach "Pattern updates"; Flow; Task 1; Verification row; Success Criterion 4 |
| C2 leak test really scans `$ValidityF:*` | yes — `test_external.py:1220` | Test Impact bullet rewritten: coverage rationale, vacuity claim retracted |
| C3 arm-none test goes vacuous | yes — `test_external.py:1226-1249` | Technical Approach (two-part replacement); Test Impact; Task 2; new Success Criterion; Rabbit Holes caveat |
| C4 prefix sanitization is 3-of-5, caller-supplied | yes — `association_recall.py:172`, `test_confidence_gate_refusal.py:208,383` pass `uuid4().hex[:8]` | Failure Path Test Strategy; Task 2 parametrization rule |
| C5 dedup refactor bundled in | n/a (judgment) | Technical Approach 1:1 conversion rule; Task 1; Risk 3 escape clause |
| C6 Appetite vs. Team Orchestration | n/a (judgment) | Appetite line corrected; Tasks 1+2 merged into one builder; roster reduced to four roles |
| Prerequisites PARTIAL (`POPOTO_TEST_DB` unset) | n/a | Prerequisites section now states it as a hard build-lane gate |
| N1 grep-count proxies | — | one row dropped, the other replaced with a real C1 assertion |
| N2 recipe factory misnamed | yes | spike-3 item 3 corrected |
| N3 line-number drift | yes | `teardown()` refs updated to 924-1012 / 930-947 |

Task count went 6 → 5 and task IDs `build-external-factories` /
`build-sibling-factories` were merged into `build-factories`; the
`validate-conversions` dependency list was updated to match.

---

## Resolved Questions

*Closed at the revision pass. Critique reviewed all three and challenged none of
the defaults; they stand as recorded. Retained rather than deleted because each
one's reversal cost is the thing a build-time reader will want.*

Three judgment calls were made rather than left blocking, so the pipeline can
proceed. Each is stated with the default taken and the cost of reversing it.

1. **Scope widened from two factories to five.** The issue names
   `_build_external_model_class` and asks that `_build_graph_model_class` be
   audited. Spike-3 found three more sites with the identical defect
   (`recipe_base.py`, `association_recall.py`, `test_confidence_gate_refusal.py`).
   **Default taken:** convert all five, because leaving a known-broken idiom in
   the tree is how it came back here in the first place, and the per-site edit is
   mechanical. **Reversal cost:** low — drop task 2 and its three sites; the plan
   still stands for the two `external_base.py` factories.
2. **No `src/` change.** The issue's options 1 and 3 both touch `src/`;
   Technical Approach rejects both. **Default taken:** harness-only fix.
   **Reversal cost:** high — option 1 is a library-wide keyspace-stability
   change and would need its own issue, plan, and migration story. If a
   maintainer wants `db_class_key` to be derivable at call time as a library
   property, that is a different piece of work, not a variant of this one.
3. **`teardown()`'s explicit validity branch is kept, not deleted.** After the
   fix it is redundant with the widened SCAN, but it is targeted, cheap, and
   carries the `test_teardown_on_arm_none_is_real_noop` guard. **Default
   taken:** keep and re-comment. **Reversal cost:** low, but deleting it costs
   the no-op test its subject.

Item 3 above ("keep the explicit validity branch") survived critique but its
*justification* was narrowed: the branch is kept, and the test that justified
keeping it must be rebuilt to stay falsifiable (C3). The default is unchanged;
the work it implies grew.

One thing genuinely worth a human eye: the `*:ExtMem*` glob added to
`_STALE_KEY_PATTERNS` is broader than anything the sweep uses today (Risk 1). It
is bounded by the dedicated bench DB and by the `ExtMem` anchor, and the
existing survivor assertion covers over-reach — but if there is an operational
reason the bench DB might not be dedicated in some deployment, say so now.
