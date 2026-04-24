---
status: Planning
type: chore
appetite: Small
owner: Tom
created: 2026-04-24
tracking: https://github.com/tomcounsell/popoto/issues/370
last_comment_id:
---

# Integration Feedback DX Gaps (v1.5.0 → v1.6.0)

## Problem

After the `ai` repo integrated popoto v1.5.0's agent-memory stack (`"used"` outcome, `RetrievalQuality`, `error_summary`), five discrete DX gaps surfaced. Everything works — these are papercuts, not bugs. Three are pure docs; two add small additive public APIs. All five fit naturally into one patch-style PR.

**Current behavior:**
- Integrators must read five source files to reconstruct the outcome-effects matrix; the module docstring in `src/popoto/fields/observation.py` does not include the table.
- Custom BM25/RRF retrieval pipelines (i.e. anything not built on `ContextAssembler.assemble()`) cannot compute a `RetrievalQuality` over an already-retrieved list of records. The only entry point is `ContextAssembler.assess()`, which is a pre-retrieval probe tied to assembler state.
- `import popoto; popoto.__version__` raises `AttributeError`. Integrators must fall back to `pip show popoto`.
- `ObservationProtocol.on_context_used()` raises `ValueError` on unknown outcomes (correct behavior) but the docstring does not tell integrators to coerce application-specific outcomes to the five valid values first.
- The v1.5.0 changelog announces `"used"` but does not advise existing integrators how to migrate custom outcomes like `"echoed"` into the new vocabulary.

**Desired outcome:**
One PR, one version bump (patch or minor — see Semver Implications), zero breaking changes. The outcome-effects table lives next to the code. Custom pipelines can build `RetrievalQuality` from records. `popoto.__version__` resolves via `importlib.metadata`. The strict-validation footgun is documented. Integrators migrating from bespoke outcomes have a one-sentence migration note to point at.

## Freshness Check

**Baseline:** `main` at `093fda4` (Merge PR #369, release/v1.5.0). Verified 2026-04-24 — two days after the issue was filed on 2026-04-22.

| Reference | State |
|---|---|
| `src/popoto/__init__.py` (no `__version__`) | **Unchanged.** 206 lines, `__all__` list ends at line 206, no `__version__` anywhere. |
| `src/popoto/fields/observation.py` (module docstring) | **Unchanged.** Module docstring at lines 1–41 lists the five outcomes but no effects-per-field table. |
| `src/popoto/recipes/context_assembler.py` `_build_quality` | **Minor drift.** Issue says `_build_quality()`; actual method is `_compute_quality()` at line 900. Public signature target is still `RetrievalQuality.from_records(records, query_cues=None)`. |
| `CHANGELOG.md` v1.5.0 entry | **Unchanged.** Starts at line 8; no migration note for `"echoed"` or bespoke outcomes. |
| `on_context_used()` strict validation | **Unchanged.** `VALID_OUTCOMES` set at line 51; `ValueError` raised at line 153. |

No active plan in `docs/plans/` touches these files (checked `ls -lt docs/plans/ | head -10`). Proceeding on unchanged premises.

**Disposition: Minor drift** — update the Technical Approach to reference `_compute_quality` (the real name) and proceed.

## Research

No external research needed — all five items are self-contained in the popoto codebase. The `importlib.metadata` pattern for `__version__` is the standard Python approach since PEP 566 / Python 3.8 and is directly applicable here (popoto requires Python ≥ 3.10, per `pyproject.toml`).

## Spike Results

No spikes needed. Each item is either a docstring/markdown edit or a mechanical refactor of existing logic. Design decisions worth flagging are captured inline in Technical Approach below — none require time-boxed investigation.

## Solution

Group the five items by kind and ship them in one feature branch + PR.

### Group A: Pure docs (items 1, 4, 5)

**Item 1 — Outcome effects comparison table in module docstring.**
File: `src/popoto/fields/observation.py`
Change: The module docstring (lines 1–41) currently describes the five outcomes prose-style. Insert the five-row effects matrix (already written in issue #370) between the "Five outcomes:" prose block and the "RecallProposal:" block. Use the integrator-facing row labels the issue proposed:

```
| Effect              | acted           | used            | dismissed | deferred | contradicted        |
|---------------------|-----------------|-----------------|-----------|----------|---------------------|
| ConfidenceField     | strengthen      | —               | —         | —        | weaken              |
| CyclicDecayField    | strengthen      | —               | weaken    | —        | weaken (aggressive) |
| DecayingSortedField | touch           | —               | —         | —        | —                   |
| AccessTracker       | confirm         | confirm         | discard   | discard  | discard             |
| PredictionLedger    | auto-resolve    | moderate err    | auto-resolve | —     | auto-resolve        |
```

Write it as a reStructuredText / rST literal block so it renders cleanly in both the rendered docs (ReadTheDocs) and `help(ObservationProtocol)`. Keep the narrative around it: one sentence before the table ("Effects summary — each row lists what the field/mixin does for each outcome") and preserve the existing examples below.

**Item 4 — Docstring note on `on_context_used()` strict validation.**
File: `src/popoto/fields/observation.py` at the `on_context_used()` docstring (lines 131–146, `Raises:` block at line 144).
Change: Append a one-paragraph "Note:" section immediately above the existing `Raises:` block:

```
Note:
    This method validates ``outcome_map`` strictly against ``VALID_OUTCOMES``.
    Application-specific outcomes (e.g. a custom ``"echoed"`` label) must be
    coerced to one of the five valid values before calling, otherwise a
    ``ValueError`` is raised. See the Metacognitive Layer feature doc for
    guidance on mapping bespoke outcomes.
```

**Item 5 — `"echoed"` → `"used"` / `"dismissed"` migration hint.**
File: `CHANGELOG.md` v1.5.0 entry (lines 8–47).
Change: Add a "Migration" sub-section immediately before the existing "Notes" block (line 42) with the issue's proposed text:

```
#### Migration

If you were using a custom ``"echoed"`` outcome (or any application-specific
label semantically between ``"used"`` and ``"dismissed"``):

- Map it to ``"used"`` if the agent reasoned over the memory (staged read
  should be confirmed; prediction auto-resolves with moderate error).
- Map it to ``"dismissed"`` if the overlap was purely coincidental keyword
  match (staged read discarded; confidence/cycle weakened).

``on_context_used()`` raises ``ValueError`` on unknown outcome labels — coerce
to a valid value before calling.
```

Also mirror the short version into `docs/features/observation-protocol.md` FAQ if one exists, or add a two-line "Migrating custom outcomes" bullet at the end of the "Outcomes" section. Verified that file exists; add the bullet there.

### Group B: Code + docs (items 2, 3)

**Item 2 — `RetrievalQuality.from_records(records, query_cues=None)` factory.**

File: `src/popoto/recipes/context_assembler.py`

The existing `_compute_quality()` method at line 900 is bound to `ContextAssembler` state (`self.model_class`, `self._confidence_field_name`, `self._existence_filter`, `self._decaying_sorted_field_name`, `self.score_weights`, `self.max_items`, `self.surfacing_threshold`). To expose it standalone without forcing integrators to instantiate an assembler, refactor in three steps:

1. **Extract the four helpers** (`_avg_confidence`, `_compute_score_spread`, `_compute_fok`, `_staleness_ratio`, plus the shared `_score_proxy_for_records` and `_cue_familiarity`) into module-level functions that take their state via explicit parameters:
   - `_avg_confidence(records, confidence_field_name)` — field name can be introspected from the first record's `_meta.fields` when not passed.
   - `_score_proxy_for_records(records, model_class, score_weights)`.
   - `_compute_score_spread(records, model_class, score_weights)`.
   - `_cue_familiarity(cue_value, existence_filter, model_class)`.
   - `_compute_fok(query_cues, pull_candidates, model_class, score_weights, max_items, surfacing_threshold, existence_filter)`.
   - `_staleness_ratio(records, model_class, score_weights, surfacing_threshold, decaying_sorted_field_name)`.

2. **Keep the `ContextAssembler` instance methods as thin wrappers** that forward to the module-level functions — preserves all existing behavior, all existing tests pass unchanged.

3. **Add `RetrievalQuality.from_records()` classmethod:**

   ```python
   @classmethod
   def from_records(
       cls,
       records,
       query_cues=None,
       score_weights=None,
       max_items=10,
       surfacing_threshold=0.5,
   ) -> "RetrievalQuality":
       """Build a RetrievalQuality over an already-retrieved list of records.

       Intended for custom retrieval pipelines (BM25, RRF, hybrid) that want
       the metacognitive layer without adopting ContextAssembler. All model
       capabilities (ConfidenceField, ExistenceFilter, DecayingSortedField)
       are introspected from ``records[0]._meta.fields``.

       Args:
           records: Non-empty list of Popoto Model instances. When empty,
               returns a zero-valued RetrievalQuality.
           query_cues: Optional dict of query cues — same shape as
               ``ContextAssembler.assess(query_cues=...)``. When None, fok_score
               is 0.0 and per_cue_fok is empty.
           score_weights: Optional dict mapping sorted-field names to weights.
               Used for score_spread and staleness_ratio. When None, both
               default to 0.0 and ``score_distribution`` is empty.
           max_items: Denominator for partial_retrieval_count in the FOK
               formula. Default 10 — matches ``ContextAssembler`` default.
           surfacing_threshold: Threshold for subthreshold_activation and
               staleness_ratio. Default 0.5 — matches ``ContextAssembler``.

       Returns:
           A RetrievalQuality dataclass. Field semantics match the assembler
           path exactly — see class docstring.
       """
   ```

   Behavior:
   - Empty `records` → return `RetrievalQuality()` (zero-valued, no warning — this is a valid "nothing retrieved" state).
   - `model_class = type(records[0])`. Introspect capability fields via `_meta.fields.items()`.
   - If `query_cues` is falsy, skip FOK entirely (not a warning — `from_records` may legitimately be used for score-only quality probes).
   - If `score_weights` is None, skip score_spread / staleness_ratio computation; set both to 0.0 and leave `score_distribution` empty.
   - Re-use the extracted module-level helpers verbatim.

   Export from `src/popoto/__init__.py`: already exported (`RetrievalQuality` is in `__all__` at line 199). No change needed; the new classmethod is attached to the existing symbol.

**Item 3 — `popoto.__version__` from package metadata.**
File: `src/popoto/__init__.py`.
Change: Insert at the top of the module (after the existing imports, before `__all__`):

```python
from importlib.metadata import PackageNotFoundError, version as _get_version

try:
    __version__ = _get_version("popoto")
except PackageNotFoundError:  # pragma: no cover — fallback for source-tree imports
    __version__ = "0.0.0+unknown"
```

Add `"__version__"` to `__all__`. Rationale for `importlib.metadata` over a static string:
- `pyproject.toml` is the single source of truth for the version (currently `1.5.0`).
- release-please already bumps `pyproject.toml` on release — no risk of version skew.
- Zero runtime cost; `importlib.metadata` reads the installed distribution's `METADATA` file once.
- Python 3.10+ minimum (per `pyproject.toml requires-python = ">=3.10"`), so `importlib.metadata` is always available in stdlib.

The `PackageNotFoundError` fallback handles the edge case of importing from an uninstalled source tree (e.g. `sys.path.insert(0, "./src")` during dev). The sentinel `"0.0.0+unknown"` is PEP 440 compliant.

## Semver Implications

Items 1, 4, 5 are pure docs — no semver effect.

Items 2 and 3 add new public API:
- `RetrievalQuality.from_records()` — new classmethod on an existing exported class.
- `popoto.__version__` — new module-level string.

Both are purely additive and backwards-compatible. Under semantic versioning this is a **MINOR** bump (`1.5.0 → 1.6.0`) by strict interpretation, since `feat:` commits trigger minor bumps under release-please's conventional-commits rules. If Tom prefers to absorb this into a patch release (`1.5.1`), the commits can all be marked `fix:` / `docs:` — but that conflicts with the conventional-commits spec. **Recommendation: minor bump, `v1.6.0`.** Covered by the Open Question at the end of the plan.

No breaking changes. No deprecations. No migration required for existing integrators beyond what item 5 already documents.

## Prior Art

- **#352** (closed, merged as v1.5.0): Added `RetrievalQuality`, `ContextAssembler.assess()`, `ContextAssembler.assemble(assess_quality=True)`. This plan extends that surface area with a classmethod factory — it does not revisit any of the design decisions from #352.
- **#198** (closed): Added `ObservationProtocol` + `RecallProposal`. The outcome effects table this plan adds (item 1) documents behavior shipped in #198; no code changes to the protocol itself.
- **#228** (closed): Added `PredictionLedgerMixin`. The `auto-resolve` column in the item 1 table comes from this.
- **#254** (closed): "Agent Memory DX" quickstart — closest DX precedent in this repo. Demonstrated that pure-docs improvements ship as a single PR with no semver effect, and that code+docs DX changes (like this plan) can be bundled with feature work.

No prior attempts at `popoto.__version__` — `gh issue list --state all --search "version __version__"` returned no matches apart from #370 itself.

## Why Previous Fixes Failed

N/A — no prior failed fixes for these five items.

## Data Flow

Single-file edits, no multi-component data flow to trace:

- Item 1: static docstring.
- Item 2: new classmethod on `RetrievalQuality` delegates to refactored module-level helpers that read the same Redis keys (`ZSCORE` via pipeline, ExistenceFilter `might_exist`, ConfidenceField companion hash). Reads only, no writes. Same side-effect profile as `ContextAssembler.assess()`.
- Item 3: `importlib.metadata.version("popoto")` — reads the installed distribution's `METADATA` file at first call. No Redis involvement.
- Item 4: static docstring.
- Item 5: static markdown.

## Documentation

- [ ] Update module docstring in `src/popoto/fields/observation.py` with the effects table (item 1).
- [ ] Update `on_context_used()` docstring in `src/popoto/fields/observation.py` with the strict-validation note (item 4).
- [ ] Add "Migration" sub-section to `CHANGELOG.md` v1.5.0 entry (item 5).
- [ ] Add one-line "Migrating custom outcomes" bullet to `docs/features/observation-protocol.md` (item 5).
- [ ] Add `RetrievalQuality.from_records()` docstring inline with the classmethod (item 2). No separate doc page — the feature doc `docs/features/metacognitive-layer.md` already covers `RetrievalQuality`; add a sub-section "Building RetrievalQuality from a custom pipeline" pointing to the new classmethod with a five-line example.
- [ ] Add a one-paragraph "Version introspection" note to `docs/api-reference.md` (or `docs/index.md` if no API reference entry for `popoto` module itself) mentioning `popoto.__version__` (item 3).

## Update System

No update-system changes required — this feature is purely internal to the popoto library. Downstream projects consuming popoto will pick up the changes through their normal `pip install -U popoto` cycle.

## Agent Integration

No agent integration required — popoto is a library, not an agent-runtime tool. None of the five items introduce new tools, MCP surfaces, or bridge-level integrations.

## Failure Path Test Strategy

Each item's failure mode and its matching test:

| Item | Failure mode | Test |
|---|---|---|
| 1 | Table missing from rendered help / RTD | Docstring is verified by a new `test_observation_module_docstring_contains_effects_table` that checks `"acted"`, `"used"`, `"dismissed"`, `"deferred"`, `"contradicted"` all appear in `popoto.fields.observation.__doc__`, plus the five row labels (`ConfidenceField`, `CyclicDecayField`, `DecayingSortedField`, `AccessTracker`, `PredictionLedger`). |
| 2 | Custom pipeline gets different numbers than assembler path | Add `tests/test_context_assembler.py::TestRetrievalQualityFromRecords::test_from_records_matches_assemble_quality` — save N records, run both `ContextAssembler.assemble(..., assess_quality=True)` and `RetrievalQuality.from_records(same_records, query_cues=same_cues, score_weights=same_weights)`, assert the four scalar fields match within float tolerance. |
| 2 | Empty records raises instead of returning zero-valued quality | `test_from_records_empty_returns_zero` — `RetrievalQuality.from_records([])` returns a RetrievalQuality with all zero/empty fields and does not raise. |
| 2 | `query_cues=None` / `score_weights=None` path raises | `test_from_records_no_cues_no_weights` — returns a valid RetrievalQuality with `fok_score=0.0`, `score_spread=0.0`, `per_cue_fok={}`, `score_distribution=[]`. |
| 3 | `popoto.__version__` raises `AttributeError` | `tests/test_version.py::test_version_attribute_exists` — asserts `popoto.__version__` is a non-empty string matching `re.match(r"^\d+\.\d+\.\d+", popoto.__version__)`. |
| 3 | Version string drifts from `pyproject.toml` | Same test also opens `pyproject.toml` and asserts `popoto.__version__` begins with the project version string (allows dev suffixes like `1.5.0.dev1`). |
| 4 | Docstring missing the coerce-first guidance | `test_on_context_used_docstring_mentions_coerce` — `"coerce"` and `"ValueError"` both appear in `ObservationProtocol.on_context_used.__doc__`. |
| 5 | Migration note missing from v1.5.0 changelog | `test_changelog_has_used_migration_note` — reads `CHANGELOG.md`, asserts the v1.5.0 section contains both `"echoed"` and `"Migration"`. |

## Test Impact

- [ ] `tests/test_context_assembler.py` (existing `TestRetrievalQuality` class) — **UPDATE**: existing methods `_compute_quality`, `_avg_confidence`, `_compute_score_spread`, `_compute_fok`, `_staleness_ratio`, `_score_proxy_for_records`, `_cue_familiarity` become thin wrappers around new module-level functions. Tests that call these instance methods directly (via `assembler._compute_quality(...)` etc.) should continue to pass unchanged — the wrappers preserve signatures. Verify by running `pytest tests/test_context_assembler.py` before and after the refactor.
- [ ] `tests/test_observation_protocol.py` — **UPDATE**: add `test_on_context_used_docstring_mentions_coerce` to the existing file. No existing tests change disposition.
- [ ] No other test files touched.

## Rabbit Holes

- **Do not** refactor `ContextAssembler._compute_quality()`'s internals beyond extracting helpers — tempting to clean up the `score_proxy_for_records` ZSCORE plumbing, but that's out of scope.
- **Do not** add a new "coerce helper" like `ObservationProtocol.coerce_outcome(raw, default="dismissed")`. Item 4 is a docstring note, not an API change. If integrators want a coercion helper, file a separate issue.
- **Do not** backport the migration note into older changelog entries. v1.5.0 is the release that introduced `"used"`, so the migration note belongs there and only there.
- **Do not** switch `__version__` to a static string maintained by release-please. `importlib.metadata` is the standard Python approach and has no skew risk.
- **Do not** update `docs/features/metacognitive-layer.md` with a new tier of examples beyond the five-line `from_records` snippet. The feature doc is already comprehensive; this plan adds one sub-section, nothing more.

## No-Gos

- No new Redis module usage (`BF.*`, `CMS.*`, etc.). The `_score_proxy_for_records` extraction preserves the existing pipelined `ZSCORE` approach verbatim — no module commands, Valkey-compatible.
- No changes to `VALID_OUTCOMES`. The set stays at `{"acted", "dismissed", "deferred", "contradicted", "used"}`. Item 4 documents the strictness; it does not loosen it.
- No deprecation of `ContextAssembler.assess()` or any existing API surface.
- No changes to `__init__.py` exports beyond adding `__version__`. `RetrievalQuality` is already exported.

## Step by Step Tasks

1. **Branch:** `feature/integration_feedback_dx_gaps_370` from `main` at `093fda4`.
2. **Item 3 (smallest, lowest risk):** Add `__version__` block to `src/popoto/__init__.py` and `"__version__"` to `__all__`. Write `tests/test_version.py`. Run `pytest tests/test_version.py`.
3. **Item 1:** Edit module docstring in `src/popoto/fields/observation.py`. Add the effects table between the "Five outcomes" block (line 26) and "RecallProposal:" block (line 27). Add `test_observation_module_docstring_contains_effects_table` in `tests/test_observation_protocol.py`.
4. **Item 4:** Edit `on_context_used()` docstring in `src/popoto/fields/observation.py` (lines 131–146). Add the "Note:" block above `Raises:`. Add `test_on_context_used_docstring_mentions_coerce`.
5. **Item 5:** Add "Migration" sub-section to `CHANGELOG.md` under the v1.5.0 entry, immediately before "Notes" (line 42). Add a one-line bullet to `docs/features/observation-protocol.md` "Outcomes" section. Add `test_changelog_has_used_migration_note`.
6. **Item 2a (refactor helpers):** In `src/popoto/recipes/context_assembler.py`, move bodies of `_avg_confidence`, `_compute_score_spread`, `_compute_fok`, `_staleness_ratio`, `_score_proxy_for_records`, `_cue_familiarity` into module-level functions that take explicit state. Replace instance-method bodies with one-line delegating wrappers. Run full test suite (`pytest tests/test_context_assembler.py`) — all tests must pass unchanged.
7. **Item 2b (classmethod):** Add `RetrievalQuality.from_records()` classmethod. Introspect model class from `records[0]`; dispatch to the module-level helpers. Add three test methods under a new `TestRetrievalQualityFromRecords` class in `tests/test_context_assembler.py`.
8. **Item 2c (docs):** Add "Building RetrievalQuality from a custom pipeline" sub-section to `docs/features/metacognitive-layer.md` with a five-line example. Add one-paragraph `popoto.__version__` note to `docs/api-reference.md`.
9. **Ruff/mypy:** `black src/ tests/` and `mypy src/`. Fix any warnings.
10. **Full test suite:** `pytest` (requires Redis on localhost:6379 per `CLAUDE.md`). All tests pass.
11. **Commit strategy (conventional commits for release-please):**
    - `docs(observation): add outcome effects table to module docstring` (item 1)
    - `feat(recipes): RetrievalQuality.from_records factory for custom pipelines` (item 2)
    - `feat(popoto): add __version__ via importlib.metadata` (item 3)
    - `docs(observation): clarify on_context_used strict validation` (item 4)
    - `docs(changelog): add "used" migration note for custom outcomes` (item 5)
12. **PR:** open against `main`. Title: `DX: integration feedback items from #370`. Body: links to #370 and lists the five items with checkboxes. Reference the issue *without* a closing keyword — the plan PR tracks the tracking issue; merge of this PR will close #370 because it IS the implementation PR (not a plan PR — the plan is already on main).

## Success Criteria

- [ ] `popoto.__version__` returns a PEP 440 version string matching `pyproject.toml`.
- [ ] `RetrievalQuality.from_records([...], query_cues={"topic": "x"}, score_weights={"relevance": 1.0})` returns a `RetrievalQuality` with the same scalar fields as `ContextAssembler(...).assemble(..., assess_quality=True).metadata["quality"]` when both are run against the same records and cues.
- [ ] `RetrievalQuality.from_records([])` returns a zero-valued `RetrievalQuality` without raising.
- [ ] The effects table is visible in `help(popoto.ObservationProtocol)` output and in ReadTheDocs.
- [ ] `ObservationProtocol.on_context_used.__doc__` contains the string `"coerce"` and the string `"ValueError"`.
- [ ] `CHANGELOG.md` v1.5.0 entry contains the strings `"echoed"` and `"Migration"`.
- [ ] All existing `tests/test_context_assembler.py` tests pass unchanged after the helper refactor.
- [ ] New tests (`test_version.py`, new methods in `test_observation_protocol.py` and `test_context_assembler.py`) all pass.
- [ ] `black src/ tests/` produces no diff. `mypy src/` produces no new errors.

## Open Questions

1. **Semver bump target.** Items 2 and 3 are purely additive. Release-please will pick up `feat:` commits and bump to `v1.6.0`. Do you want that, or would you rather downgrade the commit types to `fix:` / `docs:` and keep the release at `v1.5.1`? My recommendation is `v1.6.0` (honest to conventional-commits spec; the new public API justifies a minor).
2. **`RetrievalQuality.from_records` signature — extra parameters.** I proposed `(records, query_cues=None, score_weights=None, max_items=10, surfacing_threshold=0.5)` to match `ContextAssembler` defaults. Alternative: a single `config=None` parameter taking a small dataclass, or require `score_weights` (since score_spread/staleness_ratio are useless without it). Preference?
3. **Item 5 placement.** The migration note currently lives in the `CHANGELOG.md` v1.5.0 entry (under "Migration" sub-section). A one-line bullet also goes into `docs/features/observation-protocol.md`. Is that redundant, or is cross-placement the point? I think the cross-placement helps discoverability, but if you prefer changelog-only, say so.
4. **`__version__` fallback value.** I chose `"0.0.0+unknown"` (PEP 440 compliant, won't pass a `version >=` check). Any preference for a different sentinel? Some projects use `"unknown"` plain; some raise `PackageNotFoundError` loudly.
