---
status: Planning
type: chore
appetite: Small
owner: Tom
created: 2026-04-24
tracking: https://github.com/tomcounsell/popoto/issues/370
last_comment_id:
revision_applied: true
---

# Integration Feedback DX Gaps (v1.5.0 → v1.6.0)

## Revision Notes (2026-04-24)

Plan revised after `/do-plan-critique` surfaced 6 concerns + 1 nit. All concerns embedded as Implementation Notes in the relevant sections. Frontmatter `revision_applied: true`.

- **C1** (Parity test precondition) — Failure Path Test Strategy row for item 2 now specifies identical `score_weights`/`max_items`/`surfacing_threshold`/`query_cues` on both assembler and `from_records` sides and uses `math.isclose(rel_tol=1e-9, abs_tol=1e-9)` instead of `==`.
- **C2** (Helper signature convention) — Technical Approach (Item 2a) pins: classmethod does ALL introspection once at the entry point; module-level helpers are pure, keyword-only, and do not introspect.
- **C3** (Numeric fixture test) — New step 6 (Item 2a-pre) writes `test_assess_numeric_fixture` with hardcoded expected scalars *before* the helper refactor, providing a regression guard.
- **C4** (Mixed model guard) — Technical Approach (Item 2b) adds `TypeError` for heterogeneous record lists; new test `test_from_records_mixed_models_raises`.
- **C5** (Docstring xref) — Item 4's `Note:` block now points to `docs/features/observation-protocol.md` (where the protocol lives), not `metacognitive-layer.md`.
- **C6** (Table placement) — Item 1 reworked: upgrade the existing "Effects Matrix" prose in `docs/features/observation-protocol.md` to a table; module docstring gets a one-line "See Also" pointer. No table duplication.

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

**Item 1 — Outcome effects comparison table (pointer-to-existing-table, not duplicated).**

Per critique concern **C6**: `docs/features/observation-protocol.md` already has an "Effects Matrix" section (line 96), currently in prose form. Upgrade that section to the five-row table and have the module docstring *point* to it rather than duplicating — duplication creates drift risk (two copies of the same matrix going out of sync on future field additions).

**File 1:** `docs/features/observation-protocol.md` — replace the prose bullets in the "Effects Matrix" section (lines 98–104) with the table:

```
| Effect              | acted           | used            | dismissed    | deferred | contradicted        |
|---------------------|-----------------|-----------------|--------------|----------|---------------------|
| ConfidenceField     | strengthen      | —               | —            | —        | weaken              |
| CyclicDecayField    | strengthen      | —               | weaken       | —        | weaken (aggressive) |
| DecayingSortedField | touch           | —               | —            | —        | —                   |
| AccessTracker       | confirm         | confirm         | discard      | discard  | discard             |
| PredictionLedger    | auto-resolve    | moderate err    | auto-resolve | —        | auto-resolve        |
```

Prefix with one sentence ("Each row lists what the field/mixin does for each outcome") and retain the existing explanatory bullets below the table (they provide the "why" the table cannot convey).

**File 2:** `src/popoto/fields/observation.py` — module docstring (lines 1–41). Add a single reference line between the "Five outcomes:" prose block and "RecallProposal:" block:

```
See Also:
    For the effects-per-field matrix (what each outcome does to ConfidenceField,
    CyclicDecayField, DecayingSortedField, AccessTracker, PredictionLedger), see
    the "Effects Matrix" section of docs/features/observation-protocol.md.
```

This preserves the DRY invariant (one canonical table), keeps the module docstring short and scannable, and still gives the integrator a `help(ObservationProtocol)`-surfaced breadcrumb to the full matrix.

**Item 4 — Docstring note on `on_context_used()` strict validation.**
File: `src/popoto/fields/observation.py` at the `on_context_used()` docstring (lines 131–146, `Raises:` block at line 144).
Change: Append a one-paragraph "Note:" section immediately above the existing `Raises:` block:

```
Note:
    This method validates ``outcome_map`` strictly against ``VALID_OUTCOMES``.
    Application-specific outcomes (e.g. a custom ``"echoed"`` label) must be
    coerced to one of the five valid values before calling, otherwise a
    ``ValueError`` is raised. See ``docs/features/observation-protocol.md``
    (where the protocol lives) for guidance on mapping bespoke outcomes.
```

Per critique concern **C5**: the cross-reference targets `observation-protocol.md`, not `metacognitive-layer.md`. The Metacognitive Layer feature doc covers `RetrievalQuality` and `assess()`, not the protocol. Integrators hitting the `ValueError` need the outcome-vocabulary docs, which live in `observation-protocol.md`.

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

**Helper signature convention (per critique concern C2):** The classmethod does ALL introspection *once* at the entry point — it resolves `model_class = type(records[0])`, walks `records[0]._meta.fields.items()` to find capability field names (`confidence_field_name`, `existence_filter`, `decaying_sorted_field_name`), then dispatches to module-level helpers that are **pure functions** taking **explicit named arguments**. Helpers do not introspect, do not read instance state, and do not accept a model class as a "maybe introspect if None" fallback. This pins the split so every helper has exactly one caller contract and the classmethod owns all introspection. It also preserves the instance-method wrappers' behavior verbatim — they introspect `self.*` once and forward the same explicit kwargs to the same helpers.

The existing `_compute_quality()` method at line 900 is bound to `ContextAssembler` state (`self.model_class`, `self._confidence_field_name`, `self._existence_filter`, `self._decaying_sorted_field_name`, `self.score_weights`, `self.max_items`, `self.surfacing_threshold`). To expose it standalone without forcing integrators to instantiate an assembler, refactor in three steps:

1. **Extract the helpers as pure module-level functions with explicit named arguments** (no introspection fallbacks per C2):
   - `_avg_confidence(records, *, confidence_field_name)` — `confidence_field_name` is **required**. The caller (classmethod or instance method) does the introspection upfront and passes it in.
   - `_score_proxy_for_records(records, *, model_class, score_weights)`.
   - `_compute_score_spread(records, *, model_class, score_weights)`.
   - `_cue_familiarity(cue_value, *, existence_filter, model_class)`.
   - `_compute_fok(query_cues, pull_candidates, *, model_class, score_weights, max_items, surfacing_threshold, existence_filter)`.
   - `_staleness_ratio(records, *, model_class, score_weights, surfacing_threshold, decaying_sorted_field_name)`.

   Keyword-only args (`*`) prevent positional-call confusion across the six helpers and make the classmethod/instance-method wrappers trivial to audit.

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
   - **Mixed model guard (per critique concern C4):** if `records` contains more than one concrete model class (i.e. `len({type(r) for r in records}) > 1`), raise `TypeError("RetrievalQuality.from_records requires a homogeneous list of records; got N distinct model classes: [...]")`. Rationale: score weights and capability field names are per-model-class; heterogeneous lists would silently produce incorrect FOK, score_spread, and staleness_ratio values. Fail loudly at the boundary.
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

- [ ] Upgrade the "Effects Matrix" prose in `docs/features/observation-protocol.md` to the five-row table (item 1, C6).
- [ ] Add a one-paragraph "See Also" pointer in `src/popoto/fields/observation.py` module docstring referencing `docs/features/observation-protocol.md` (item 1, C6 — no table duplication).
- [ ] Update `on_context_used()` docstring in `src/popoto/fields/observation.py` with the strict-validation note, xref pointing to `docs/features/observation-protocol.md` (item 4, C5).
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
| 1 | Effects table missing from feature doc | `test_observation_protocol_doc_contains_effects_table` — reads `docs/features/observation-protocol.md`, asserts the five outcomes (`acted`, `used`, `dismissed`, `deferred`, `contradicted`) and five row labels (`ConfidenceField`, `CyclicDecayField`, `DecayingSortedField`, `AccessTracker`, `PredictionLedger`) all appear in the file. |
| 1 | Module docstring missing the pointer to the feature doc | `test_observation_module_docstring_points_to_feature_doc` — asserts `popoto.fields.observation.__doc__` contains the string `"docs/features/observation-protocol.md"`. |
| 2 | Wrapper refactor silently misroutes state (C3) | `TestRetrievalQualityAssembler::test_assess_numeric_fixture` — build a deterministic fixture (N=3 records with known scores, known cues, known `score_weights`, `max_items=5`, `surfacing_threshold=0.5`), call `ContextAssembler(...).assess(query_cues=...)`, assert each scalar field (`fok_score`, `score_spread`, `staleness_ratio`, `avg_confidence`) equals a **hardcoded expected value** within `math.isclose(rel_tol=1e-9, abs_tol=1e-9)`. This is the refactor safety net: if the instance-method wrappers drift from the pre-refactor implementation, numeric values will diverge from the hardcoded constants. |
| 2 | Custom pipeline gets different numbers than assembler path (C1) | `TestRetrievalQualityFromRecords::test_from_records_matches_assemble_quality` — save N records, run both `ContextAssembler(model_class=M, score_weights=W, max_items=10, surfacing_threshold=0.5).assemble(query_cues=Q, assess_quality=True)` and `RetrievalQuality.from_records(same_records, query_cues=Q, score_weights=W, max_items=10, surfacing_threshold=0.5)` — **using identical `score_weights`, `max_items`, `surfacing_threshold`, and `query_cues` on both sides** (pre-condition per C1). Assert all four scalar fields match via `math.isclose(rel_tol=1e-9, abs_tol=1e-9)` — **not `==`** (float comparison). Per-cue and score_distribution collections compared element-wise with the same tolerance. |
| 2 | Empty records raises instead of returning zero-valued quality | `test_from_records_empty_returns_zero` — `RetrievalQuality.from_records([])` returns a RetrievalQuality with all zero/empty fields and does not raise. |
| 2 | `query_cues=None` / `score_weights=None` path raises | `test_from_records_no_cues_no_weights` — returns a valid RetrievalQuality with `fok_score=0.0`, `score_spread=0.0`, `per_cue_fok={}`, `score_distribution=[]`. |
| 2 | Mixed model classes silently produce garbage values (C4) | `test_from_records_mixed_models_raises` — given records of two distinct concrete model classes, `RetrievalQuality.from_records([m1, m2])` raises `TypeError` whose message contains both class names. |
| 3 | `popoto.__version__` raises `AttributeError` | `tests/test_version.py::test_version_attribute_exists` — asserts `popoto.__version__` is a non-empty string matching `re.match(r"^\d+\.\d+\.\d+", popoto.__version__)`. |
| 3 | Version string drifts from `pyproject.toml` | Same test also opens `pyproject.toml` and asserts `popoto.__version__` begins with the project version string (allows dev suffixes like `1.5.0.dev1`). |
| 4 | Docstring missing the coerce-first guidance | `test_on_context_used_docstring_mentions_coerce` — `"coerce"`, `"ValueError"`, and `"observation-protocol.md"` (C5 xref target) all appear in `ObservationProtocol.on_context_used.__doc__`. |
| 5 | Migration note missing from v1.5.0 changelog | `test_changelog_has_used_migration_note` — reads `CHANGELOG.md`, asserts the v1.5.0 section contains both `"echoed"` and `"Migration"`. |

## Test Impact

- [ ] `tests/test_context_assembler.py` (existing `TestRetrievalQuality` class) — **UPDATE**: existing methods `_compute_quality`, `_avg_confidence`, `_compute_score_spread`, `_compute_fok`, `_staleness_ratio`, `_score_proxy_for_records`, `_cue_familiarity` become thin wrappers around new module-level functions. Tests that call these instance methods directly (via `assembler._compute_quality(...)` etc.) should continue to pass unchanged — the wrappers preserve signatures. Verify by running `pytest tests/test_context_assembler.py` before and after the refactor.
- [ ] `tests/test_context_assembler.py` — **ADD** `TestRetrievalQualityAssembler::test_assess_numeric_fixture` (C3). Hardcoded-expected-value test that pins the pre-refactor assembler behavior — runs before the helper extraction to capture the ground-truth constants, then runs after the extraction to verify the wrappers route correctly. Write this test **first** (step 6.5 below) so it is already protecting the refactor when step 6 runs.
- [ ] `tests/test_context_assembler.py` — **ADD** `TestRetrievalQualityFromRecords` class with four methods: `test_from_records_matches_assemble_quality` (C1 parity test with `math.isclose`), `test_from_records_empty_returns_zero`, `test_from_records_no_cues_no_weights`, `test_from_records_mixed_models_raises` (C4 guard).
- [ ] `tests/test_observation_protocol.py` — **UPDATE**: add `test_on_context_used_docstring_mentions_coerce` (also asserts the C5 xref target `"observation-protocol.md"` is in the docstring). Add `test_observation_module_docstring_points_to_feature_doc`. No existing tests change disposition.
- [ ] `tests/test_observation_protocol.py` (or a new `tests/test_feature_docs.py`) — **ADD** `test_observation_protocol_doc_contains_effects_table` reading `docs/features/observation-protocol.md`.
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
3. **Item 1 (pointer-to-table, C6):** Upgrade the "Effects Matrix" prose bullets in `docs/features/observation-protocol.md` (lines 98–104) to the five-row table. Add a one-paragraph "See Also" pointer in the `src/popoto/fields/observation.py` module docstring referencing `docs/features/observation-protocol.md`. Add `test_observation_protocol_doc_contains_effects_table` and `test_observation_module_docstring_points_to_feature_doc`.
4. **Item 4 (C5 xref fix):** Edit `on_context_used()` docstring in `src/popoto/fields/observation.py` (lines 131–146). Add the "Note:" block above `Raises:`, pointing to `docs/features/observation-protocol.md` (NOT `metacognitive-layer.md`). Add `test_on_context_used_docstring_mentions_coerce` (also asserts `"observation-protocol.md"` is in the docstring).
5. **Item 5:** Add "Migration" sub-section to `CHANGELOG.md` under the v1.5.0 entry, immediately before "Notes" (line 42). Add a one-line bullet to `docs/features/observation-protocol.md` "Outcomes" section. Add `test_changelog_has_used_migration_note`.
6. **Item 2a-pre (C3 refactor safety net — MANDATORY before step 6 refactor):** Write `TestRetrievalQualityAssembler::test_assess_numeric_fixture` with a deterministic fixture (fixed record count, fixed scores, fixed cues, fixed weights) and **hardcoded expected scalar values** captured by running the pre-refactor code. Run it once against the unrefactored code to confirm it passes — this captures the ground truth. If you skip this step, the C2 refactor has no numeric regression guard.
7. **Item 2a (refactor helpers, per C2):** In `src/popoto/recipes/context_assembler.py`, move bodies of `_avg_confidence`, `_compute_score_spread`, `_compute_fok`, `_staleness_ratio`, `_score_proxy_for_records`, `_cue_familiarity` into module-level functions with keyword-only explicit args. Replace instance-method bodies with one-line delegating wrappers (instance method introspects `self.*` and forwards as kwargs). Run full test suite (`pytest tests/test_context_assembler.py`) — all tests must pass unchanged, **including the new `test_assess_numeric_fixture` from step 6**.
8. **Item 2b (classmethod, per C2 + C4):** Add `RetrievalQuality.from_records()` classmethod. Classmethod does ALL introspection once (`model_class = type(records[0])`, capability field names from `records[0]._meta.fields`), guards against mixed model classes with `TypeError` (C4), then dispatches to module-level helpers via kwargs. Add four test methods under a new `TestRetrievalQualityFromRecords` class in `tests/test_context_assembler.py`: `test_from_records_matches_assemble_quality` (C1 — `math.isclose`, identical assembler config on both sides), `test_from_records_empty_returns_zero`, `test_from_records_no_cues_no_weights`, `test_from_records_mixed_models_raises`.
9. **Item 2c (docs):** Add "Building RetrievalQuality from a custom pipeline" sub-section to `docs/features/metacognitive-layer.md` with a five-line example. Add one-paragraph `popoto.__version__` note to `docs/api-reference.md`.
10. **Ruff/mypy:** `black src/ tests/` and `mypy src/`. Fix any warnings.
11. **Full test suite:** `pytest` (requires Redis on localhost:6379 per `CLAUDE.md`). All tests pass.
12. **Commit strategy (conventional commits for release-please):**
    - `docs(observation): upgrade effects matrix to table; point module docstring at feature doc` (item 1)
    - `feat(recipes): RetrievalQuality.from_records factory for custom pipelines` (item 2)
    - `feat(popoto): add __version__ via importlib.metadata` (item 3)
    - `docs(observation): clarify on_context_used strict validation` (item 4)
    - `docs(changelog): add "used" migration note for custom outcomes` (item 5)
13. **PR:** open against `main`. Title: `DX: integration feedback items from #370`. Body: links to #370 and lists the five items with checkboxes. Reference the issue *without* a closing keyword — the plan PR tracks the tracking issue; merge of this PR will close #370 because it IS the implementation PR (not a plan PR — the plan is already on main).

## Success Criteria

- [ ] `popoto.__version__` returns a PEP 440 version string matching `pyproject.toml`.
- [ ] `RetrievalQuality.from_records(records, query_cues=Q, score_weights=W, max_items=M, surfacing_threshold=T)` returns a `RetrievalQuality` whose four scalar fields satisfy `math.isclose(rel_tol=1e-9, abs_tol=1e-9)` against `ContextAssembler(score_weights=W, max_items=M, surfacing_threshold=T).assemble(query_cues=Q, assess_quality=True).metadata["quality"]` — same records on both sides (C1 parity).
- [ ] `RetrievalQuality.from_records([])` returns a zero-valued `RetrievalQuality` without raising.
- [ ] `RetrievalQuality.from_records([m1, m2])` where `type(m1) != type(m2)` raises `TypeError` whose message names both classes (C4).
- [ ] The effects table is visible in `docs/features/observation-protocol.md` (upgraded from prose). The module docstring contains a one-line pointer to that file (C6).
- [ ] `ObservationProtocol.on_context_used.__doc__` contains the strings `"coerce"`, `"ValueError"`, and `"observation-protocol.md"` (C5 xref).
- [ ] `CHANGELOG.md` v1.5.0 entry contains the strings `"echoed"` and `"Migration"`.
- [ ] `TestRetrievalQualityAssembler::test_assess_numeric_fixture` passes both before and after the helper refactor with identical hardcoded expected values (C3 refactor safety net).
- [ ] All existing `tests/test_context_assembler.py` tests pass unchanged after the helper refactor.
- [ ] New tests (`test_version.py`, new methods in `test_observation_protocol.py` and `test_context_assembler.py`) all pass.
- [ ] `black src/ tests/` produces no diff. `mypy src/` produces no new errors.

## Open Questions

All resolved 2026-04-24 — see decisions below.

1. **Semver bump target.** ✅ **Resolved: `v1.6.0`.** Release-please picks up `feat:` commits; the new public API (`RetrievalQuality.from_records`, `popoto.__version__`) justifies a minor bump per conventional-commits spec.
2. **`RetrievalQuality.from_records` signature.** ✅ **Resolved: keep the multi-parameter form** `(records, query_cues=None, score_weights=None, max_items=10, surfacing_threshold=0.5)` — matches `ContextAssembler` defaults and keeps signature patterns consistent across the recipe. No `config=None` dataclass wrapper.
3. **Item 5 placement.** ✅ **Resolved: cross-placement stands.** "Migration" sub-section in `CHANGELOG.md` v1.5.0 entry + one-line bullet in `docs/features/observation-protocol.md` "Outcomes" section.
4. **`__version__` fallback value.** ✅ **Resolved: `"0.0.0+unknown"`** (PEP 440 compliant; won't satisfy a `version >=` check).
