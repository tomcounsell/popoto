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
  trades one leak for another.
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
`*:ExtMem<hash>*`, which now matches everything the item wrote → next item
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
  Build a shared base namespace dict and add `content_index` /
  `embedding` conditionally, then one `type()` call — this also removes the
  triplicated field list. The post-`type()` `with_validity` block
  (`.validity = ValidityField()` + `_meta.add_field`) is **unchanged**: it is
  name-independent, and `ValidityField` derives its keys at call time from a
  `_meta.db_class_key` that is now already correct.
- `scenarios/external_base.py::_build_graph_model_class` — same conversion. Its
  post-`type()` self-referential `Relationship(model=cls, null=True)` +
  `_meta.add_field("prev_turn", …)` block is **unchanged and must stay
  post-hoc**: the class object cannot reference itself inside its own namespace
  dict any more than inside its own body.
- `association_recall.py::_build_model` — two class-body variants (with/without
  `CoOccurrenceField`); same conditional-namespace treatment, same post-hoc
  self-referential `related = Relationship(model=AssocMemory, null=True)`.
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
pre-fix runs is still swept. Symmetrically, `teardown()`'s
`SCAN {class_name}:*` gains a `SCAN *:{class_name}:*` companion.

**Explicit-validity branch in `teardown()`: keep it.** After the fix its
premise (a *shared* namespace) is false, but the branch itself is still
correct, still targeted, and is covered by two tests worth keeping — including
`test_teardown_on_arm_none_is_real_noop`, which guards against an
unconditional DEL masking a leak. Rewrite the comment (Documentation task), do
not delete the code. See Rabbit Holes for why the tempting deletion is a trap.

## Failure Path Test Strategy

### Exception Handling Coverage

`ExternalScenario.teardown()` is built from four `try: … except Exception: pass`
blocks (`scenarios/external_base.py:930-991`), and `_build_refusal_model`'s
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
- [ ] `safe_prefix` containing `:` or `-` — cannot occur; callers already strip
      both (`prefix.replace(":", "").replace("-", "")[:8]` in
      `_build_recipe_model_class`, and the equivalent in `ExternalScenario.setup()`).
      The invariant test asserts the produced `db_class_key` contains no `:`,
      which mechanically pins this.
- [ ] No agent-output processing is involved; no empty-output loop risk.

### Error State Rendering

Not user-visible. The harness's only output is a benchmark report; a failed
teardown manifests as leaked keys, which the new leak test asserts against
rather than rendering.

## Test Impact

- [ ] `tests/benchmarks/test_external.py::TestSupersessionArm::test_no_leaked_validity_keys_after_teardown`
      — **UPDATE**: it currently asserts absence of
      `$ValidityF:ExternalBenchmarkMemory:validity:*`. That literal is dead after
      the fix, so the test would pass vacuously (the #661 vacuity trap). Widen it
      to assert (a) zero keys matching `*ExternalBenchmarkMemory*` anywhere, and
      (b) zero keys matching `*{model_class.__name__}*` after teardown.
- [ ] `tests/benchmarks/test_external.py::TestSupersessionArm::test_teardown_on_arm_none_is_real_noop`
      — **UPDATE**: keep the intent (arm `none` must take a genuine no-op), but
      re-derive its expected key names from the per-item class name rather than
      the base name.
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
  `*:{class_name}:*` widening also lands (so deleting it before that is a
  regression), and `test_teardown_on_arm_none_is_real_noop` exists specifically
  to catch an unconditional cleanup masking a leak. Keep the branch; rewrite its
  comment.
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
**Impact:** `*:ExtMem*` and `*:{class_name}:*` are broader than the patterns
they join. On the dedicated bench DB (14) or the pytest DB (15) this is
harmless, but a run misconfigured onto a shared database could delete unrelated
keys.
**Mitigation:** Both globs remain anchored on the `ExtMem`/`{class_name}`
literal, which is a hash-suffixed benchmark-only prefix; `run_external.py`
already rejects DB 0 and defaults to 14. The updated
`test_custom_patterns_sweep_only_matching_keys` keeps its `SomeOtherModel:keepme`
survivor assertion, which is precisely the over-reach detector. Do **not**
generalize to a bare `*ExtMem*` without the leading anchor set.

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
