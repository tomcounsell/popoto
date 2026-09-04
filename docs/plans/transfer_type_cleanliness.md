---
status: Ready
type: chore
appetite: Small
owner: valorengels
created: 2026-09-04
tracking: https://github.com/tomcounsell/popoto/issues/572
last_comment_id: none
---

# Transfer Package Type Cleanliness

## Problem

`src/popoto/transfer/` shipped in PR #558 with `mypy src/popoto/transfer/` failing. Two
genuine `Optional`-narrowing bugs were fixed in commit `744c3dc`. The remaining errors were
waived by maintainer ruling in a PR comment
(https://github.com/tomcounsell/popoto/pull/558#issuecomment-5277221524) rather than fixed,
on the reasoning that the rest of `src/` is not mypy-clean either.

A waiver that lives only in a PR comment decays. Nothing re-runs mypy on this package, so
the errors will either be fixed piecemeal with no tracking or grow silently.

**Current behavior:**

```
$ .venv/bin/mypy src/popoto/transfer/
Found 49 errors in 4 files (checked 5 source files)
```

The count reproduces exactly against the issue's figure. Breakdown by category:

| Category | Count | Shape |
|---|---|---|
| `type-arg` | 32 | Bare `dict` / `list` / `set` / `tuple` under `disallow_any_generics = True` |
| `no-untyped-def` | 15 | Missing parameter or return annotations under `disallow_untyped_defs = True` |
| `attr-defined` | 2 | `klass.export_state` / `klass.import_state` on an `__mro__` element mypy types as bare `type` |

Breakdown by file:

| File | Errors |
|---|---|
| `src/popoto/transfer/export.py` | 19 |
| `src/popoto/transfer/import_.py` | 16 |
| `src/popoto/transfer/format.py` | 8 |
| `src/popoto/transfer/results.py` | 6 |
| `src/popoto/transfer/__init__.py` | 0 |

There is no mypy waiver to remove. `setup.cfg` has one `[mypy]` block and one
`[mypy-tests.*]` override; neither mentions `transfer`, and `grep -rn "type: ignore"
src/popoto/transfer/` returns nothing. The waiver exists only as prose in the PR comment,
which is precisely why issue #572 was filed.

**Desired outcome:**

`mypy src/popoto/transfer/` exits 0 under the repo's existing strict `setup.cfg`, with no
new blanket ignores, no config override, and byte-identical runtime behavior. The PR-comment
waiver is discharged by fixing the code, not by re-recording it.

## Freshness Check

**Baseline commit:** `0eef7362bffc7a29739db6fdb4b78a6b70adc5cf` — the commit this freshness
check was run against. It is **not** the build's comparison point: `6c39681` and `bd1d337`
sit between it and the plan commit, so the suite and mypy comparisons use the branch point
`8c242cf` instead.
**Issue filed at:** 2026-08-13T07:14:51Z
**Disposition:** Unchanged

**File:line references re-verified:**

- Issue claims "the remaining **49 errors**" — re-measured at baseline: exactly 49. Holds.
- Issue claims the `import_.py:350-356` Optional-narrowing bugs were fixed in `744c3dc` —
  `_validate_manifest` now returns the narrowed dict and the caller consumes it
  (`src/popoto/transfer/import_.py:359` region). No `Optional`-narrowing errors remain in
  the current output. Holds.
- Issue claims "`mypy src/popoto/recipes/` reports errors of the identical shape in the same
  environment" — re-measured: 154 errors, dominated by `no-untyped-def` (101) and `type-arg`
  (29). Same shape, so the waiver's consistency argument was factually correct at filing time
  and still is. Note the issue's own caution holds: neither the 141 nor the 147 figure quoted
  in the PR discussion is canonical, and 154 here is likewise environment-bound, not a
  citable constant.

**Cited sibling issues/PRs re-checked:**

- #554 — closed. Its implementation PR #558 merged 2026-08-13T09:19:17Z. This is the PR that
  introduced the package and carried the waiver.
- #506 — still open ("Gate mypy or make config honest — 1150 errors, strict setup.cfg not
  enforced"). It owns repo-wide mypy gating and the `recipes/` sweep. Its stated Not-in-scope
  is "Fixing all 1150 lines in one PR — plan should scope an incremental allowlist or
  per-module rollout." This plan is exactly one such per-module increment, so the two do not
  conflict.

**Commits on main since issue was filed (touching referenced files):**

`git log --since=2026-08-13T07:14:51Z -- src/popoto/transfer/ setup.cfg` returns nothing. The
package and the mypy config are untouched since the merge that created them. `src/popoto/
models/base.py` has moved (four commits), but only in areas unrelated to the two
`export_records` / `import_records` delegates at `base.py:2793` and `base.py:2828`, which
still forward verbatim to this package.

**Active plans in `docs/plans/` overlapping this area:**
`docs/plans/generic_export_import_roundtrip.md` is the shipped #554 plan, already Complete.
No active plan touches `src/popoto/transfer/`.

**Notes:** No drift. Every claim in issue #572 reproduces at baseline.

## Prior Art

- **#554 / PR #558**: "Generic export/import with per-field round-trip fidelity" — created
  `src/popoto/transfer/`. Its plan's Verification table carried a "Types clean: exit code 0"
  row that the merged code did not satisfy. The maintainer waived the row rather than block
  the merge, and filed #572 to make the waiver defensible. This plan closes that loop.
- **#506**: "Gate mypy or make config honest" — open, unplanned. Owns the repo-wide decision
  (enforce strictly vs. relax `setup.cfg` to reality) and the CI wiring. Deliberately not
  touched here.
- No prior attempt to annotate `src/popoto/transfer/` exists. The package has exactly one
  commit in its history (`31535a3`), so there is no repeated-fix pattern to analyze and no
  `## Why Previous Fixes Failed` section is warranted.

## Research

No relevant external findings — proceeding with codebase context and training data. The work
is entirely internal: adding annotations to four first-party modules under a mypy
configuration this repo already ships. No new library, API, or ecosystem pattern is involved.

## Spike Results

### spike-1: Can `model_class` be annotated `type[Model]` without a circular import or a cascade of new errors?

- **Assumption**: "Annotating the eight `model_class` parameters as `type[Model]` will either
  break the import cycle at runtime or make mypy reject `model_class._meta.fields` (since
  `_meta` is attached by the `ModelBase` metaclass at `base.py:504`, not declared on `Model`)."
- **Method**: prototype (isolated file, repo's own `setup.cfg`)
- **Finding**: **False on both counts.** A file annotating `def f(model_class: type[Model])`
  and iterating `model_class._meta.fields.items()` type-checks clean. Runtime circularity is
  a non-issue because all four modules already carry `from __future__ import annotations`
  (verified: present in `export.py`, `format.py`, `import_.py`, `results.py`), so a
  `TYPE_CHECKING`-guarded import of `Model` never executes. This also matches how
  `models/base.py` already defers its own import of this package to inside the delegate
  method bodies.
- **Confidence**: high
- **Impact on plan**: The eight `model_class` parameters get a real type instead of `Any`.
  Import goes under `if TYPE_CHECKING:`.
- **ERRATUM (recorded at BUILD, mypy 2.3.1):** the first half of this finding **did not
  hold** in the gate environment. `model_class._meta` raised `attr-defined` on every one of
  the five sites, because `_meta` is attached by the metaclass at `base.py:504` and never
  declared on `Model`. The runtime-circularity half of the finding did hold. The fix was to
  declare `_meta: "ModelOptions"` on `Model`, mirroring the `query: Query` annotation two
  lines above it — a one-line, runtime-inert addition that also removed 27 errors from
  `base.py`. Spike results are environment-bound like every other measurement here.

### spike-2: What discharges the two `attr-defined` errors without a blanket ignore?

- **Assumption**: "`klass.export_state(instance)` where `klass` comes from
  `model_class.__mro__` can only be silenced with `# type: ignore[attr-defined]`."
- **Method**: prototype (isolated file, repo's own `setup.cfg`)
- **Finding**: **False.** A local `Protocol` declaring `export_state` plus a `cast` at the
  call site type-checks clean:

  ```python
  class _ExportsState(Protocol):
      __name__: str

      @staticmethod
      def export_state(instance: Any) -> Any: ...

  carried = cast("_ExportsState", klass).export_state(instance)
  ```

  This is not a silencer — it names the duck-typed contract the module's own docstring
  already describes ("Both passes test for the presence of the protocol member, never for
  membership of a named class"). The `if "export_state" not in klass.__dict__: continue`
  guard immediately above the cast is what makes the cast sound.
- **Confidence**: high
- **Impact on plan**: Zero `# type: ignore` comments are needed anywhere in the package. The
  "targeted ignores only, with a reason" allowance in the issue scope goes unused.

## Data Flow

Unchanged by this work. Annotations under `from __future__ import annotations` are stored as
strings in `__annotations__` and never evaluated at runtime. The one structural change is two
`cast()` calls, which `typing.cast` implements as `return val` — an identity function.

For reference, the flow this package implements and which must remain byte-identical:

1. **Entry point**: `Model.export_records(...)` at `src/popoto/models/base.py:2793` defers an
   import and delegates to `transfer.export.export_records`.
2. **export.py**: resolves keys, hydrates in chunks, walks `_meta.fields` and
   `type(instance).__mro__` for duck-typed `export_state`, accumulates an `ExportResult`.
3. **format.py**: coerces values to JSON primitives through the shared encoder registry,
   builds the manifest, serializes lines.
4. **import_.py**: validates the manifest before any write, resolves embedding provenance,
   conflict-checks and saves in batches, restores carried state, reconciles into an
   `ImportReport`.
5. **Output**: JSONL on the stream (export) or an `ImportReport` ledger (import).

## Architectural Impact

- **New dependencies**: None. `Protocol` and `cast` come from `typing`, already imported in
  the touched modules or trivially addable.
- **Interface changes**: None at runtime. Public signatures gain annotations; no parameter is
  added, removed, renamed, or reordered, and no default changes.
- **Coupling**: `export.py` and `import_.py` gain a `TYPE_CHECKING`-only reference to
  `popoto.models.base.Model`. This is a type-level edge with no runtime import, and it
  documents a dependency that already exists implicitly (every `model_class` argument is a
  `Model` subclass).
- **Data ownership**: Unchanged.
- **Reversibility**: Trivial. The whole change is annotations plus two casts; reverting is a
  clean `git revert`.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (scope is fully determined by a reproducible error list)
- Review rounds: 1

**Justification for Small over Medium:** 49 errors sounds large, but 47 of them are
mechanical and both spikes came back resolved with high confidence. The package is 1256 lines
across 4 files with a single commit of history. There is no design question left open: the
parameter type is `type[Model]`, the generics are readable off the surrounding code, and the
two `attr-defined` sites have a proven Protocol fix. The one genuine risk (annotating a
container narrower than what actually flows through it) is caught by the existing 73-test
transfer suite, which must pass unchanged.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey reachable | `redis-cli -n 5 PING` | Transfer tests hit Redis; DB 5 keeps this lane off DB 0 and off the shared DB 15 |
| mypy installed in the venv | `.venv/bin/mypy --version` | The gate this plan is measured against |
| Editable install resolves to this checkout | `.venv/bin/python -c "import popoto, pathlib; print(pathlib.Path(popoto.__file__).resolve())"` | CLAUDE.md worktree hazard 1: a stale editable install silently tests another tree |

## Solution

### Key Elements

- **`results.py` — dataclass field generics**: The six bare `dict` / `list` defaults on
  `ExportResult` and `ImportReport` get concrete parameters. These are the package's public
  return types, so this is the highest-value annotation in the change.
- **`format.py` — serialization boundary types**: `build_manifest`'s four mapping parameters
  and its return, `dump_line`'s argument, `parse_line`'s return, and `_encoder_for`'s return.
- **`export.py` — collector signatures**: The eight `collect_*` / `_*` helpers plus
  `export_records` gain `model_class: type[Model]` (or `instance: Model`) and concrete return
  mappings.
- **`import_.py` — validator and batch signatures**: `_check_choice`'s `allowed` tuple,
  `_validate_manifest`, `_resolve_embedding_provenance`, `_restore_state`, `_process_batch`,
  and `import_records`.
- **Duck-typed MRO protocols**: Two module-local `Protocol` classes naming the
  `export_state` / `import_state` contract, consumed via `cast` at exactly the two sites that
  currently error.

### Flow

Not a user-facing feature. The developer-facing flow:

`mypy src/popoto/transfer/` → 49 errors → annotate 4 modules → `mypy src/popoto/transfer/` →
exit 0, and `pytest tests/test_transfer_*.py` → 73 passed, unchanged.

### Technical Approach

- **Do not touch `setup.cfg`.** There is no `transfer` override to remove, and relaxing the
  global config is #506's decision, not this plan's.
- **Do not add a single `# type: ignore`.** Spike 2 proved none is required. Note that
  `warn_unused_ignores = True` is already on, so a speculative ignore would itself become a
  new error — the config actively punishes the lazy path.
- **Import `Model` under `if TYPE_CHECKING:`** in `export.py` and `import_.py`. All four
  modules already have `from __future__ import annotations`, so the annotation strings are
  never evaluated and the existing runtime import deferral in `models/base.py` is preserved
  exactly.
- **Prefer the honest type over `Any`.** `Any` would silence `type-arg` just as well, and it
  is the wrong answer: `dict[str, Any]` for a JSON object says something true and useful,
  where `dict[Any, Any]` says nothing. Reserve `Any` for values that genuinely are arbitrary,
  such as a decoded JSON leaf or an encoder's return.
- **Annotate the two `list` accumulators that mypy flags as `var-annotated`-adjacent**
  (`landed: list = []` at `import_.py:185`, `batch: "list[dict]" = []` at `import_.py:359`)
  to their real element types rather than widening.
- **Byte-identical runtime is the hard constraint.** The only executable statements added are
  two `typing.cast` calls, which are identity at runtime. No control flow, no reordering, no
  changed default, no new import that executes.

## Failure Path Test Strategy

### Exception Handling Coverage

- [x] The package has three `except` blocks that are deliberately non-silent and already
      covered: `format.py`'s `PackageNotFoundError` fallback (returns `"0.0.0+unknown"`),
      `export.py`'s `except Exception` around `klass.export_state(instance)` (appends a
      warning to `result.warnings` — an observable state change, asserted by the fidelity
      tests), and `import_.py`'s `ValueError` on a malformed JSON line (adds an `ERRORED`
      outcome to the report). None is `except Exception: pass`.
- [x] This change adds no new exception handler and modifies no existing one. The annotation
      work must not alter which exceptions are caught or what the handlers do.

### Empty/Invalid Input Handling

- [x] Documented and already covered by the existing suite: an export of an empty model
      yields `{"filter": null, "matched_count": 0}`; an import of a file with no manifest is
      refused with `ModelException` before any write; a blank line in the JSONL stream is
      skipped by `iter_lines`; a non-object JSON line raises `ValueError` and is counted
      `errored`.
- [x] No new function is introduced, so there is no new empty-input surface to test. The
      annotations must not narrow a parameter in a way that contradicts the empty/None cases
      the existing tests already drive — that is exactly what the unchanged-suite gate
      catches.
- [x] Not agent-output processing; no silent-loop risk.

### Error State Rendering

- [x] `ExportResult.summary()` and `ImportReport.summary()` render warnings, errors, and the
      five outcome categories. `partial` is surfaced first because it is the only category
      that leaves degraded data behind. Existing reconciliation tests assert this.
- [x] Annotating `warnings: list` → `list[str]` and `outcomes: list` → `list[RecordOutcome]`
      must not change what `summary()` prints. Covered by the unchanged-suite gate.

## Test Impact

No existing tests affected — this change is annotations plus two identity-function casts, so
no runtime behavior, signature arity, or return value changes. The 73 tests across
`tests/test_transfer_roundtrip.py`, `tests/test_transfer_reconciliation.py`, and
`tests/test_transfer_fidelity_fields.py` must pass **unchanged**; modifying any of them is
itself evidence the change was not behavior-preserving and is grounds to reject the diff.

No new tests are added. A test cannot observe an annotation, and mypy exiting 0 is the
verification. `[mypy-tests.*] ignore_errors = True` in `setup.cfg` means the test files are
outside the gate regardless.

## Rabbit Holes

- **Fixing `src/popoto/recipes/` too.** It has 154 errors of the same shape in this
  environment and it is tempting to sweep both. Don't. That is #506's per-module rollout, and
  bundling it turns a Small into a Large and makes the diff unreviewable.
- **Adding mypy to `scripts/ci-local.sh` or a CI workflow.** *Residual gap accepted: with no
  gate, this package's count can drift back above zero with no CI signal. Closing that gap is
  #506's mandate, and Task 4 posts the achieved zero into #506 so the per-module rollout has a
  recorded baseline.* Neither runs mypy today. Wiring
  a gate is the whole substance of #506 and it needs the repo-wide decision first — gating on
  a package-scoped path would encode an allowlist this plan has no mandate to design.
- **Quoting an error count as a repo constant.** Per CLAUDE.md, the delta is
  redis-py/stub-version dependent. Every number in this plan is measured under one stated
  environment and is not portable. Chasing a "true" count across environments is wasted time;
  the target is 0 for this package, which is version-stable in a way a nonzero count is not.
- **Refactoring while annotating.** Several helpers would read better restructured. Any
  restructuring breaks the byte-identical guarantee and destroys the reviewer's ability to
  confirm the diff is inert. Annotate in place.
- **Introducing a `TypedDict` for the manifest or the record shape.** It is the "right"
  model and it would be a genuine improvement, but it changes `build_manifest`'s and
  `parse_line`'s contracts and ripples into the format-version discussion. `dict[str, Any]`
  discharges the error; a `TypedDict` migration is a separate design.

## Risks

### Risk 1: An annotation is narrower than what actually flows through the code

**Impact:** mypy goes green while a real call path passes a type the annotation forbids.
Nothing breaks at runtime (annotations are inert strings here), but the type information is a
lie and the next person to trust it writes a real bug.
**Mitigation:** Derive every generic from the code that produces and consumes the value, not
from what looks tidy. Where a value is genuinely heterogeneous — a decoded JSON leaf, an
encoder's output — use `Any` deliberately rather than guessing a union. The 73-test suite
exercises Decimal, datetime, set, tuple, bytes, and non-string-keyed mappings through the
round trip, so a wrong guess about the encoder boundary shows up as a test failure.

### Risk 2: The `Protocol` + `cast` reads as a disguised ignore

**Impact:** A reviewer reasonably objects that `cast` silences the checker just as
`# type: ignore` would, and the change fails review.
**Mitigation:** Keep the soundness argument adjacent to every cast. The two sites need two
different forms, because only one of them has a statement-level guard:

- **`export.py:191-194` — adjacent-guard form.** Place the cast directly under its
  `if "export_state" not in klass.__dict__: continue` guard, so the guard that makes the cast
  sound is on the preceding lines.
- **`import_.py:147-150` — guard-carrying-comprehension form.** There is no statement-level
  guard at the `klass.import_state(...)` call site (`import_.py:159`); the
  `"import_state" in klass.__dict__` test is a filter inside the `by_name = {...}`
  comprehension, and the only statement above the call is the derived `if klass is None:`
  raise. So put the cast *inside the comprehension*, on the same expression as the filter
  that justifies it, and annotate the binding:

  ```python
  by_name: dict[str, _ImportsState] = {
      klass.__name__: cast("_ImportsState", klass)
      for klass in type(instance).__mro__
      if "import_state" in klass.__dict__
  }
  ```

  The call at `:159` then type-checks with no cast of its own. **Do not move code to
  manufacture an adjacent guard** — restructuring would violate the "Refactoring while
  annotating" Rabbit Hole and destroy the reviewer's ability to confirm the diff is inert.

In both forms the Protocol is named after the contract the module docstring already describes.
This is documenting a duck-typed interface, not suppressing a diagnostic — and unlike an
ignore, it makes the subsequent `export_state` / `import_state` call *checked*.

### Risk 3: The measured count is environment-bound and a reviewer reproduces a different one

**Impact:** Confusion about whether the work is complete; a repeat of the 141-vs-147 mess in
the #558 discussion.
**Mitigation:** State the environment with every number (see the Environment note below), and
gate on **0**, not on a delta. Zero is the one figure that does not drift with stub versions.
If a reviewer's environment shows nonzero after the change, that is a real finding about a
site this environment's stubs happen to narrow, and it should be recorded rather than argued
away.

### Risk 4: Running the suite lands on the wrong Redis DB

**Impact:** DB 0 is a live agent store on this machine. Per CLAUDE.md and the #577 incident,
writing there has already caused real data loss twice.
**Mitigation:** Every pytest invocation in this plan sets `POPOTO_TEST_DB=5`. DB 5 also keeps
this lane clear of the shared DB 15 that concurrent worktrees contend over (CLAUDE.md
worktree hazard 4). No ad-hoc script outside pytest is needed for this work at all.

## Race Conditions

No race conditions identified. This change adds no concurrency, no shared mutable state, and
no cross-process data flow. Every statement added is an annotation (not executed) or a
`typing.cast` (identity). The package's own existing concurrency property — that export is
explicitly *not* a point-in-time snapshot, which is why `vanished` is a counted outcome rather
than an error — is unchanged and out of scope.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #506] Repo-wide mypy cleanliness, the `src/popoto/recipes/` sweep (154 errors
  in this environment), single-sourcing the mypy config into `pyproject.toml`, and adding a
  mypy gate to `scripts/ci-local.sh` or CI. #506 owns all of it and explicitly scopes itself
  to an incremental per-module rollout, which this plan is one increment of.
- [SEPARATE-SLUG #506] Any relaxation of `setup.cfg`'s strict flags. This plan meets the
  config as written; deciding whether the config should be softened is #506's Option B.
- Nothing else is deferred. Every error in `mypy src/popoto/transfer/` is fixed here.

## Update System

No update system changes required. This is a source-internal typing change with no new
dependency, no config file, and no migration. Existing installations are unaffected — the
package's runtime bytecode behavior is identical.

## Agent Integration

No agent integration required. `src/popoto/transfer/` is reached through
`Model.export_records` / `Model.import_records`, which are already public and already
exported from `popoto/__init__.py:102`. This change adds no capability and no new surface for
an agent to invoke.

## Documentation

### Feature Documentation

- [ ] No change to `docs/guides/export-import.md`. It documents user-facing behavior, and no
      user-facing behavior changes. Confirm by reading it that it makes no claim about the
      package's type-checking status that would become stale.

### External Documentation Site

- [ ] No page changes expected. Run `mkdocs build --strict` as part of the standard gates to
      confirm nothing broke.

### Inline Documentation

- [ ] Add a one-line comment above each `Protocol` explaining that it names the duck-typed
      MRO contract the module docstring describes, so a future reader does not mistake the
      `cast` for a suppression.
- [ ] Do not restate types in docstrings. The `Args:` blocks in this package describe meaning,
      not type; annotations now carry the type. Leave the prose alone.

## Success Criteria

- [ ] `mypy src/popoto/transfer/` exits 0 — all 49 errors resolved
- [ ] Zero `# type: ignore` comments in `src/popoto/transfer/` (spike 2 proved none is needed)
- [ ] `setup.cfg` unchanged — no per-package override added or removed
- [ ] All 73 transfer tests pass **unchanged** (no test file is modified)
- [ ] Full suite shows no new failures against the branch point actually used for the
      comparison — `8c242cf`, the merge base of this branch and `origin/main`
- [ ] Runtime-inert: the diff contains only annotations, `TYPE_CHECKING` imports, `Protocol`
      declarations, and `cast` calls
- [ ] Tests pass (`/do-test`)
- [ ] Documentation updated (`/do-docs`)

## Team Orchestration

### Team Members

- **Builder (transfer-types)**
  - Name: `transfer-types-builder`
  - Role: Annotate all four modules to mypy-clean without changing runtime behavior
  - Agent Type: builder
  - Resume: true

- **Validator (transfer-types)**
  - Name: `transfer-types-validator`
  - Role: Verify mypy is 0, the suite is unchanged, and the diff is runtime-inert
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Annotate `results.py` and `format.py`

- **Task ID**: build-leaf-modules
- **Depends On**: none
- **Validates**: `.venv/bin/mypy src/popoto/transfer/results.py src/popoto/transfer/format.py` → 0 errors
- **Informed By**: spike-1 (all four modules already carry `from __future__ import annotations`)
- **Assigned To**: `transfer-types-builder`
- **Agent Type**: builder
- **Parallel**: false
- `results.py` (6 `type-arg` errors at lines 69, 74, 75, 141, 142, 143): parameterize the
  `ExportResult` and `ImportReport` dataclass fields — `filter_kwargs`, `fidelity`,
  `warnings`, `errors`, `outcomes`. Derive each element type from what the producing code
  appends: `warnings` and `errors` are f-string messages, `outcomes` holds `RecordOutcome`,
  `fidelity` is the merged manifest `fields` + `mixins` roll-up.
- `format.py` line 61: give `_encoder_for` a return type. It returns
  `encoder_decoder.encoder` from the `EncoderDecoder` namedtuple at
  `src/popoto/models/encoding.py:64`, which is untyped, or `None`.
- `format.py` lines 174–179: parameterize `build_manifest`'s `filter_kwargs`, `fields`,
  `mixins`, `embedding_provenance` and its return. These are all JSON objects.
- `format.py` lines 215, 232: `dump_line`'s `obj` parameter and `parse_line`'s return.
- Change nothing else. No reordering, no docstring type restatement, no `# type: ignore`.

### 2. Annotate `export.py`

- **Task ID**: build-export
- **Depends On**: build-leaf-modules
- **Validates**: `.venv/bin/mypy src/popoto/transfer/export.py` → 0 errors
- **Informed By**: spike-1 (`type[Model]` type-checks against `model_class._meta.fields`),
  spike-2 (Protocol + cast discharges `attr-defined` with no ignore)
- **Assigned To**: `transfer-types-builder`
- **Agent Type**: builder
- **Parallel**: false
- Add `from typing import TYPE_CHECKING` and, under the guard, `from ..models.base import
  Model`. Runtime imports must not change — `models/base.py` defers its import of this module
  for a reason.
- Line 44: `_render_filter(q_objects, filters: dict)` — annotate both the varargs and the
  mapping; return is already `"str | None"`.
- Line 60 and the eight `no-untyped-def` sites at 99, 109, 121, 141, 165, 187, 206, 214:
  annotate `model_class` as `type[Model]`, `instance` as `Model`, and parameterize each
  `dict` return. These are `collect_embedding_provenance`, `collect_field_policies`,
  `collect_mixin_policies`, `_model_state`, `_matches_client_filters`, and `export_records`.
- Line 194 (`attr-defined`): declare a module-local `_ExportsState` Protocol with an
  `export_state` member and a `__name__` attribute, then `cast("_ExportsState", klass)` at
  the call. Place it directly under the `if "export_state" not in klass.__dict__: continue`
  guard so the soundness argument is adjacent, and add the explanatory comment from the
  Documentation section.

### 3. Annotate `import_.py`

- **Task ID**: build-import
- **Depends On**: build-export
- **Validates**: `.venv/bin/mypy src/popoto/transfer/import_.py` → 0 errors
- **Informed By**: spike-1, spike-2
- **Assigned To**: `transfer-types-builder`
- **Agent Type**: builder
- **Parallel**: false
- Same `TYPE_CHECKING` import of `Model`.
- Line 36: `_check_choice`'s `allowed: tuple` → a string tuple.
- Lines 43, 75/77/80, 122, 172/174/178/185, 278, 359: annotate `model_class`, the `manifest`
  and `record` mappings, the `drop` / `regenerate` sets, and the `landed` and `batch` list
  accumulators. Covers `_validate_manifest`, `_resolve_embedding_provenance`,
  `_restore_state`, `_process_batch`, and `import_records`.
- Line 159 (`attr-defined`): an `_ImportsState` Protocol with an `import_state` member. Do
  **not** cast at the call site — there is no statement-level guard there. Use the
  guard-carrying-comprehension form from Risk 2: annotate the `by_name` binding at
  `import_.py:147-150` as `dict[str, _ImportsState]` and apply `cast("_ImportsState", klass)`
  inside the comprehension, on the same expression as the `"import_state" in klass.__dict__`
  filter that makes it sound. `klass.import_state(instance, from_jsonable(carried))` at `:159`
  then type-checks unchanged. Move no code.
- Preserve the `744c3dc` fix: `_validate_manifest`'s narrowed return must still be consumed
  by the caller, not discarded. Annotating must not reintroduce the discarded-return bug the
  issue says was already fixed.

### 4. Validate

- **Task ID**: validate-transfer-types
- **Depends On**: build-import
- **Assigned To**: `transfer-types-validator`
- **Agent Type**: validator
- **Parallel**: false
- Run every row of the Verification table and report each result verbatim.
- Confirm the diff is runtime-inert: read it and reject any changed default, reordered
  statement, altered control flow, or new executing import.
- Confirm no test file appears in the diff.
- Report the environment the gates **actually ran in** (mypy version, redis-py version,
  checkout path, baseline SHA) alongside every count, per CLAUDE.md. Do not restate the
  authoring environment recorded below.
- Run the repo-wide mypy row and confirm the total did not increase. `check_untyped_defs =
  True` means mypy checks the bodies of `Model.export_records` / `Model.import_records`
  (`src/popoto/models/base.py:2812-2816`, `:2847-2855`), which call straight into the newly
  narrowed signatures — so this change can add errors *outside* the package while
  `mypy src/popoto/transfer/` still reads 0.
- Post the achieved zero for `src/popoto/transfer/` as a comment on #506, so its per-module
  mypy rollout has a recorded baseline for this package.

## Verification

**Two environments, stated separately per CLAUDE.md.**

*Authoring environment (where the plan's Problem-section figures were first measured):* mypy
2.1.0 (compiled), redis-py 7.1.1, Python venv at `/Users/valorengels/src/popoto/.venv`,
primary checkout (not a worktree), baseline `0eef7362`.

*Gate environment (where every row below actually runs):* mypy 2.3.1 (compiled), redis-py
8.1.0, Python venv at `.worktrees/sdlc-572/.venv`, worktree `.worktrees/sdlc-572`, branch point
`8c242cf`. The 49-error figure re-measured here reproduces the authoring figure exactly — 2
`attr-defined` / 15 `no-untyped-def` / 32 `type-arg`, split `export.py` 19 / `import_.py` 16 /
`format.py` 8 / `results.py` 6 — so no number in this plan changes between the two.

Per CLAUDE.md, the mypy error delta is redis-py-version-dependent; the *target* of 0 is not,
which is why the package gate below asserts zero rather than a delta. The one repo-wide row is
necessarily a delta and is therefore gated as "not greater than", against a baseline measured
in the gate environment.

| Check | Command | Expected |
|-------|---------|----------|
| Transfer package types clean | `.venv/bin/mypy src/popoto/transfer/` | exit code 0 |
| Zero remaining errors reported | `.venv/bin/mypy src/popoto/transfer/ 2>&1 \| grep -c "^src/popoto/transfer/.*error:"` | match count == 0 |
| No type-ignore waivers added | `grep -rn "type: ignore" src/popoto/transfer/ \| wc -l \| tr -d " "` | output is 0 |
| No mypy config override for transfer | `grep -c "transfer" setup.cfg \|\| true` | output is 0 |
| setup.cfg untouched | `git fetch origin main && git diff --name-only $(git merge-base HEAD origin/main) -- setup.cfg \| wc -l \| tr -d " "` | output is 0 |
| No test file modified | `git diff --name-only $(git merge-base HEAD origin/main) -- tests/ \| wc -l \| tr -d " "` | output is 0 |
| Repo-wide mypy did not regress | `.venv/bin/mypy src/ 2>&1 \| tail -1` | error total <= 1126 (baseline at `8c242cf`, mypy 2.3.1 / redis-py 8.1.0, worktree `.worktrees/sdlc-572`); gate on "not greater than", never on the constant |
| Transfer tests pass unchanged | `POPOTO_TEST_DB=5 .venv/bin/python -m pytest tests/test_transfer_roundtrip.py tests/test_transfer_reconciliation.py tests/test_transfer_fidelity_fields.py -q` | exit code 0 |
| Transfer test count still 73 | `POPOTO_TEST_DB=5 .venv/bin/python -m pytest tests/test_transfer_roundtrip.py tests/test_transfer_reconciliation.py tests/test_transfer_fidelity_fields.py --collect-only -q 2>&1 \| grep -oE "^[0-9]+ tests collected"` | output contains 73 |
| Full suite passes | `POPOTO_TEST_DB=5 .venv/bin/python -m pytest -q` | exit code 0 |
| Lint clean | `.venv/bin/ruff check src/` | exit code 0 |
| Format clean | `.venv/bin/black --check src/ tests/` | exit code 0 |
| Docs build | `.venv/bin/mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | History & Consistency, Risk & Robustness | Risk 2's mitigation and Task 3 both assert the `import_.py` cast sits "under the existing `\"import_state\" in klass.__dict__` guard". It does not: that membership test is a filter inside the `by_name = {...}` dict comprehension at `import_.py:147-150`, and the call at `import_.py:159` is preceded only by the derived `if klass is None: raise ModelException(...)`. The plan's own adjacency standard is unsatisfiable at one of its two target sites, and making it true by restructuring would violate the "Refactoring while annotating" Rabbit Hole. | Task 3 (build-import), Risk 2 | Do **not** move code. Prefer the no-cast form: annotate the comprehension result as `by_name: dict[str, _ImportsState] = {klass.__name__: cast("_ImportsState", klass) for klass in type(instance).__mro__ if "import_state" in klass.__dict__}` at `import_.py:147-150`, so the cast is literally on the same expression as the `__dict__` membership filter that makes it sound. Then `klass.import_state(instance, from_jsonable(carried))` at `:159` type-checks with no cast at the call site. Restate Risk 2's mitigation as two forms: adjacent-guard for `export.py:191-194`, guard-carrying-comprehension for `import_.py:147-150`. |
| CONCERN | Risk & Robustness | No Verification row measures repo-wide mypy. `check_untyped_defs = True` means mypy checks the bodies of `Model.export_records` / `Model.import_records` (`src/popoto/models/base.py:2812-2816`, `:2847-2855`), which call straight into the newly narrowed `type[Model]` / `dict[...]` signatures, so this change can add errors outside the package while `mypy src/popoto/transfer/` reads 0. | Verification table, Task 4 (validate-transfer-types) | Add a row: `.venv/bin/mypy src/ 2>&1 \| tail -1` → total error count must not exceed the pre-change baseline measured in the SAME environment (1126 errors in 67 files at HEAD `8c242cf`, mypy 2.3.1 / redis-py 8.1.0, worktree `.worktrees/sdlc-572`). Gate on "not greater than", never on an absolute constant — per CLAUDE.md the repo-wide count is stub-version dependent, unlike the package target of 0. |
| CONCERN | Risk & Robustness | Two Verification rows do not produce what their Expected column claims. `grep -rc "type: ignore" src/popoto/transfer/` prints one `path:0` line **per file** (5 lines), never a bare `0`, and exits 1; `grep -c "transfer" setup.cfg` prints `0` but also exits 1. Both were run and confirmed. A gate runner that checks exit status will read a clean package as a failure. | Verification table (rows "No type-ignore waivers added", "No mypy config override for transfer") | Replace with count-summing, exit-safe forms: `grep -rn "type: ignore" src/popoto/transfer/ \| wc -l \| tr -d " "` → `0`, and `grep -c "transfer" setup.cfg \|\| true` → `0` (or `! grep -q "transfer" setup.cfg`). Expected column should say "output is 0" for both, not "match count == 0", since grep's exit code is 1 on zero matches. |
| CONCERN | Scope & Value | The issue's stated harm is decay ("Nothing re-runs mypy on this package"). Reaching 0 without any gate does not stop the package drifting back above 0 on the next commit, and the plan puts every enforcement mechanism in Rabbit Holes / No-Gos without ever naming the residual gap it is accepting. | Rabbit Holes, Risks | Deferring the gate to #506 is correct — the fix is honesty, not scope. Append to the "Adding mypy to `scripts/ci-local.sh` or a CI workflow" Rabbit Hole: "Residual gap accepted: with no gate, this package's count can drift back above zero with no CI signal. Closing that gap is #506's mandate. Task 4 must post the achieved zero into #506 so the per-module rollout has a recorded baseline." Add that cross-link as a Task 4 bullet. |
| CONCERN | History & Consistency | The Verification section states the baseline environment as "mypy 2.1.0 (compiled), redis-py 7.1.1, ... primary checkout (not a worktree), baseline `0eef7362`" while invoking CLAUDE.md's environment doctrine, but the gates will run in `.worktrees/sdlc-572` under mypy 2.3.1 / redis-py 8.1.0 off HEAD `8c242cf`. The doctrine requires stating the environment the gates actually run in. | Verification (Environment note) | The 49-error figure was re-measured in the worktree and reproduces exactly (2 attr-defined / 15 no-untyped-def / 32 type-arg; export 19 / import_ 16 / format 8 / results 6), so no number changes — only the note. Record both environments explicitly and have Task 4 report the environment it actually ran in rather than restating the authoring one. |
| NIT | Structural check | Two Verification rows compare against the moving ref `origin/main` (`git diff --name-only origin/main -- setup.cfg` / `-- tests/`). `origin/main` is currently `cbc7cc1`, one commit **ahead** of branch HEAD `8c242cf`; both rows output 0 today only because that commit is docs-only. Once main lands any `tests/` change, the "No test file modified" gate fails on someone else's commit. | Verification table | Compare against the merge base instead: `git diff --name-only $(git merge-base HEAD origin/main) -- tests/ \| wc -l \| tr -d " "`. Also `git fetch origin main` first, or the ref is whatever was last fetched into this worktree. |
| NIT | Structural check | The plan's stated baseline commit `0eef7362` is a real ancestor of the branch but is two commits behind its actual base (`6c39681`, `bd1d337` sit between it and the plan commit `8c242cf`), so "Full suite shows no new failures against the `0eef7362` baseline" names the wrong comparison point. | Freshness Check, Success Criteria | Restate the baseline as the branch point actually used for the suite comparison. |
