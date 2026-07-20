---
status: Planning
type: feature
appetite: Medium
owner: dev
created: 2026-07-20
tracking: https://github.com/tomcounsell/popoto/issues/463
last_comment_id:
---

# Confidence-gated retrieval — refuse when memory doesn't know

## Problem

An agent that confidently injects a wrong memory is worse than one that stays
silent. Knowing when to return **nothing** is a live-agent memory capability
most systems drop entirely — the common LoCoMo leaderboards exclude the
adversarial category rather than attempt it. `ContextAssembler` today always
returns its best pull-path candidates regardless of how weakly they match; it
has no mechanism to decline.

**Current behavior:**
`ContextAssembler.assemble()` runs the pull path (composite / lexical / hybrid),
merges with the push path, budget-selects, and injects the top records. There is
no threshold below which the assembler declines to inject a pull-path answer,
and no metadata signal telling the caller "I chose not to answer."

**Desired outcome:**
An opt-in confidence gate: when the top-ranked pull-path candidate's
`ConfidenceField` value is below a caller-supplied threshold, the assembler
either drops all pull-path records ("refuse" mode) or keeps them but flags the
decision in metadata ("flag" mode). The gate is mode-agnostic (works under
composite, lexical, and hybrid retrieval), ships with **no default threshold**
(policy-level sign-off required), and lets Popoto publish an honest refusal
number on the LoCoMo adversarial slice where others publish none.

## Freshness Check

**Baseline commit:** `5c4cb886ab93ff967891b5665df71e3d36e344ae`
**Issue filed at:** 2026-07-10T10:03:17Z
**Disposition:** Minor drift

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py` — the hybrid-mode validation pattern
  the issue's design mirrors is live at lines ~1094-1118 (`retrieval_mode='hybrid'
  requires BM25Field and EmbeddingField`), raising `QueryException` from
  `..exceptions`. Field-capability detection (including
  `self._confidence_field_name`) is at lines ~1027-1062. `assemble()` pull/push
  merge + `all_pull_candidates` handling is at lines ~1165-1311. Competitive
  suppression consuming `all_pull_candidates` is in `_post_effects()` at lines
  ~1634-1646. All still present and structurally as the issue's design assumes.
- `src/popoto/fields/confidence_field.py:499` — `ConfidenceField.get_confidence(cls,
  model_instance, field_name) -> float` confirmed; returns `initial_confidence`
  (default 0.5) when no data. This is the gate-score source.
- Dataset load path drifted from the issue's phrasing: the issue references a
  "committed `snap-research/locomo10.json` snapshot," but the repo does **not**
  commit that file. It is downloaded from the `snap-research/locomo` HuggingFace
  release (`locomo10.json`) on first run and cached at
  `~/.cache/popoto_benchmarks/locomo.json` (see `docs/benchmarks.md:240-244` and
  `tests/benchmarks/datasets/locomo.py:44-45`). The canonical loader is
  `tests.benchmarks.datasets.locomo.iter_items(fixture_path=None)`. The plan uses
  that loader, not a committed path.

**Cited sibling issues/PRs re-checked:**
- #454 (adversarial cat-5 scoring audit) — **CLOSED**, resolved by PR #471
  (merged). Its finding is the load-bearing premise for this plan: only **2/446**
  cat-5 items are genuinely unanswerable ("Not mentioned"); the other 444/446 are
  answerable/evidence-grounded (`docs/benchmarks.md:249-253`). This is why a full
  multi-hour LoCoMo re-run is NOT required and why the refusal number must be
  caveated as statistically thin (≤2 true positives).
- #456 (Track B epic) — parent, open, unchanged.
- PR #479 (issue #457, weighted/query-adaptive RRF hybrid fusion) — **OPEN**,
  branch `feature/457-hybrid-fusion-v2`. Touches
  `src/popoto/recipes/context_assembler.py`, `src/popoto/models/query.py`,
  `docs/benchmarks.md`, and hybrid test files. This branch was cut from latest
  `origin/main` and must NOT depend on #479. See Risks → rebase-conflict.

**Commits on main since issue was filed (touching referenced files):**
- `5c4cb88` fix(#474): partition-aware score proxy — refactored the metacognitive
  helpers but left `assemble()`'s pull/push/merge structure and
  `all_pull_candidates` semantics intact. No conflict with the gate design.
- `7247a41` feat(#464): live-agent telemetry — added `emit_trace` to `assemble()`;
  the gate must slot in alongside `emit_trace`/`assess_quality` without disturbing
  either.

**Active plans in `docs/plans/` overlapping this area:**
- `weighted_query_adaptive_hybrid_fusion.md` (PR #479, open) — overlaps the same
  file but a disjoint region (RRF fusion weighting in `_pull_path_hybrid` and
  `query.py`). Coordinate via rebase; the gate is an additive block in
  `assemble()` that does not touch fusion logic.
- `adversarial_cat5_scoring_audit.md` (PR #471, merged) — the audit this plan's
  refusal metric builds on. Not an overlap; a dependency.

**Notes:** Corrected dataset-load reference (HF download + local cache, not a
committed file) carried into Technical Approach and the benchmark task.

## Prior Art

- **#454 / PR #471 (merged)**: Adversarial cat-5 scoring audit. Established that
  this LoCoMo snapshot's category 5 is answerable/evidence-grounded (444/446),
  with only 2/446 refusal-style. Recommended a "precision of no-answer decisions"
  refusal metric and explicitly deferred it to #463 (this issue). Directly sets
  the honesty bar this plan must clear: caveat first, number second.
- **#464 / PR #473 (merged)**: `emit_trace` telemetry hook on `assemble()`.
  Precedent for adding an opt-in, off-by-default parameter that attaches a new
  `metadata` key only when enabled — the exact pattern the gate's `metadata["gate"]`
  key follows.
- **#370 (metacognitive layer)**: `RetrievalQuality` / `avg_confidence`. Shows the
  established convention for introspecting `ConfidenceField` via
  `get_confidence()` and forwarding capability field names. The gate reuses
  `self._confidence_field_name` detection already done in `__init__`.
- **2026-06-11 maintainer-decisions memo**: policy-level default thresholds
  require explicit maintainer sign-off before shipping. This is why
  `confidence_gate_threshold` ships with **no default** and the tuning value lives
  as `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD`, not `Defaults.*`.

No prior *implementation* attempt at a confidence gate exists — this is greenfield
behavior on top of shipped primitives.

## Research

No relevant external findings needed — this is an internal composition of shipped
Popoto primitives (`ConfidenceField`, `ContextAssembler`) plus the LoCoMo dataset
already vendored via the benchmark harness. Proceeding with codebase context.

## Data Flow

1. **Entry point**: caller constructs `ContextAssembler(..., confidence_gate_threshold=T,
   confidence_gate_mode="refuse"|"flag")` and calls `assemble(query_cues=...)`.
2. **Construction-time validation**: if `confidence_gate_threshold is not None` and
   the model has no `ConfidenceField` (`self._confidence_field_name is None`), raise
   `QueryException` mirroring the hybrid-mode validation. Validate
   `confidence_gate_mode ∈ {"refuse","flag"}` (else `QueryException`).
3. **Pull path**: `_pull_path()` returns `(pull_records, all_pull_candidates)` for
   the effective mode — unchanged.
4. **Gate evaluation (new, additive block, immediately after the pull path)**:
   - If threshold is None → skip entirely; no `metadata["gate"]` key (bit-for-bit
     legacy behavior).
   - If `pull_records` is empty → `gate_score=None`, `applied=False`, `gated=False`;
     still attach the dict.
   - Else read `gate_score = ConfidenceField.get_confidence(pull_records[0],
     self._confidence_field_name)` (rank-0 candidate, mode-agnostic — value is
     always in [0,1] regardless of composite vs RRF score scale).
   - `gated = gate_score < threshold`.
   - **refuse** + gated → drop all `pull_records` AND clear `all_pull_candidates`
     (so competitive suppression in `_post_effects` doesn't punish candidates for a
     refusal that already happened). `applied=True`.
   - **flag** → never drop; `applied` reflects whether the gate ran (True when
     pull_records non-empty). Records untouched.
5. **Merge + budget + post-effects + format**: unchanged; push path is never gated.
6. **Output**: `AssemblyResult.metadata["gate"] = {"applied", "gate_score",
   "threshold", "mode", "gated"}` attached only when threshold is not None.

## Architectural Impact

- **New dependencies**: none. Uses `ConfidenceField.get_confidence` already imported.
- **Interface changes**: two new **keyword-only** ctor kwargs, both defaulting to
  off. `assemble()` signature unchanged. `AssemblyResult` dataclass unchanged (new
  data rides in the existing `metadata` dict).
- **Coupling**: minimal — an additive branch in `assemble()` reading one field. Does
  not touch RRF/fusion, push path, token budget, telemetry, or metacognitive layers.
- **Data ownership**: none changed; read-only on `ConfidenceField`.
- **Reversibility**: trivial — the feature is inert unless a threshold is passed.

## Appetite

**Size:** Medium

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0-1 (design is already fully specified by the requester)
- Review rounds: 1

The gate mechanism is small and fully specified. The weight is in the refusal-metric
benchmark script (honest framing, seeding-limitation documentation) and docs.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli -n 15 ping` | Test DB (isolation plugin uses DB 15) |
| LoCoMo cache (benchmark script only) | `test -f ~/.cache/popoto_benchmarks/locomo.json` | Refusal-metric number-producer; downloaded on first `iter_locomo()` run. Not needed for unit tests. |

The gate mechanism itself has no external dependencies. The refusal-metric
*number* requires the cached LoCoMo dataset (or a live download); the CI unit
tests do not — they use a seeded synthetic fixture.

## Solution

### Key Elements

- **Two keyword-only ctor kwargs** — `confidence_gate_threshold: float | None = None`
  and `confidence_gate_mode: str = "refuse"`. Both off/no-op by default.
- **Construction-time validation** — enabling the gate on a model without a
  `ConfidenceField` raises `QueryException`, mirroring hybrid-mode's
  BM25Field/EmbeddingField validation exactly.
- **Additive gate block in `assemble()`** — evaluated after the pull path, before
  merge; keyed on `get_confidence()` of the rank-0 pull candidate.
- **`metadata["gate"]` dict** — attached only when a threshold is configured.
- **EXPERIMENTAL constant** — `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5` in the
  Tuning Constants block, explicitly labeled NOT a shipped default.
- **Refusal-metric benchmark** — a standalone script/test under `tests/benchmarks/`
  computing refusal precision on the LoCoMo cat-5 slice with the cold-start caveat
  spelled out.

### Flow

Caller passes threshold → `assemble()` runs pull path → reads rank-0 confidence →
compares to threshold → (refuse: drop pull records + clear candidates | flag: keep) →
merge with push → inject → `metadata["gate"]` reports the decision.

### Technical Approach

**1. EXPERIMENTAL constant** (Tuning Constants block, near `RRF_K`/`HYBRID_CANDIDATE_MULTIPLIER`):
```python
EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5  # NOT a shipped default — used only by the benchmark/report script; needs maintainer sign-off before becoming Defaults.CONFIDENCE_GATE_THRESHOLD or a ctor default (see issue #463)
```
Do **not** add `Defaults.CONFIDENCE_GATE_THRESHOLD`. Do **not** give the ctor kwarg
any non-None default.

**2. Constructor** — add `confidence_gate_threshold=None` and
`confidence_gate_mode="refuse"` as keyword-only args (after the existing
`*, retrieval_mode` — keep them in the keyword-only group). Store on `self`. After
field-capability detection, validate:
```python
_VALID_GATE_MODES = {"refuse", "flag"}
if self.confidence_gate_threshold is not None:
    if self.confidence_gate_mode not in _VALID_GATE_MODES:
        raise QueryException(...allowed values...)
    if self._confidence_field_name is None:
        raise QueryException(
            f"confidence_gate_threshold requires ConfidenceField on {model_class.__name__}"
        )
```
Mirror the import style of the hybrid block (`from ..exceptions import QueryException`)
and its message phrasing ("requires X on ClassName").

**3. Gate block in `assemble()`** — insert immediately after the pull-path call
(`pull_records, all_pull_candidates = self._pull_path(...)`) and before the push
path. Compute a local `gate_meta` dict; do NOT mutate `metadata` yet (metadata is
built later). Keep it a small self-contained block so a #479 rebase touches disjoint
lines:
```python
gate_meta = None
if self.confidence_gate_threshold is not None:
    if not pull_records:
        gate_meta = {"applied": False, "gate_score": None,
                     "threshold": self.confidence_gate_threshold,
                     "mode": self.confidence_gate_mode, "gated": False}
    else:
        gate_score = float(ConfidenceField.get_confidence(
            pull_records[0], self._confidence_field_name))
        gated = gate_score < self.confidence_gate_threshold
        if gated and self.confidence_gate_mode == "refuse":
            pull_records = []
            all_pull_candidates = []   # don't punish others for a refusal
        gate_meta = {"applied": True, "gate_score": gate_score,
                     "threshold": self.confidence_gate_threshold,
                     "mode": self.confidence_gate_mode, "gated": gated}
```
Then, where `metadata` is assembled near the end, attach only if configured:
```python
if gate_meta is not None:
    metadata["gate"] = gate_meta
```
This guarantees callers who pass no threshold get bit-for-bit identical metadata.

**4. Refusal-metric benchmark** (`tests/benchmarks/test_confidence_gate_refusal.py`):
- Load cat-5 items via `tests.benchmarks.datasets.locomo.iter_items` (filter
  `metadata["question_type"] == 5` / `metadata["adversarial"]`). The 446-item run
  uses the cached HF dataset (no `fixture_path`); it is **network/cache-gated and
  skipped in CI** (mirror `tests/benchmarks/test_external.py`'s fixture-vs-download
  split — a deterministic seeded fixture drives the CI unit test; the full-corpus
  number-producer is opt-in/offline).
- Identify the 2 genuinely-unanswerable items by ground-truth answer text
  ("Not mentioned" / adversarial_answer refusal style) — NOT by the `adversarial`
  flag alone (444/446 adversarial items are answerable).
- Seed `ConfidenceField` values on indexed records to simulate a realistic
  post-interaction spread. **Document the seeding as a stated limitation**:
  ConfidenceField gating is cold-start-degenerate on a fresh one-shot corpus (every
  candidate starts at the same `initial_confidence` with no observation history to
  diverge it, because LoCoMo's harness does single-shot retrieval with no correction
  loop). A raw unseeded run would yield a constant `gate_score` across all items.
- Run the gate at `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD` in ≥1 retrieval mode.
- Compute refusal precision = TP/(TP+FP), TP = correctly-refused-unanswerable,
  FP = refused-but-answerable.
- Report the number plainly with the cold-start caveat and the ≤2-true-positive
  statistical-thinness warning. Must NOT be presented as a leaderboard number.

## Failure Path Test Strategy

### Exception Handling Coverage
- The new gate block reads `get_confidence()` on the rank-0 record. This is a
  narrow, deterministic read (the record just came back from the pull path). We do
  NOT wrap it in a broad `except Exception: pass` — a failure here is a real bug and
  should surface. If a defensive guard is added, it MUST log `logger.warning` and be
  covered by a test asserting the warning fires (no silent swallow). Default plan:
  no new exception handler in the gate block; state this in the PR.
- `_post_effects` already guards its pipeline; clearing `all_pull_candidates` on
  refuse means the competitive-suppression loop simply iterates an empty list — test
  asserts no `update_confidence` calls happen on a refusal.

### Empty/Invalid Input Handling
- Empty `pull_records` (no candidates) with a configured threshold → `applied=False`,
  `gate_score=None`, `gated=False`, dict still attached. Explicit test.
- `query_cues=None` (pull path skipped) with threshold configured → pull_records
  empty → same empty-path behavior. Explicit test.
- Invalid `confidence_gate_mode` → `QueryException` at construction. Explicit test.
- Threshold configured on a model without ConfidenceField → `QueryException` at
  construction. Explicit test.

### Error State Rendering
- The gate decision is user-visible via `metadata["gate"]`. Tests assert the exact
  dict shape/values for: refuse-triggered, refuse-not-triggered, flag-triggered,
  flag-not-triggered, empty-pull, and threshold-None (key absent).

## Test Impact

- `tests/test_context_assembler.py` — UPDATE (additive): add a gate test class. No
  existing assertions change because the feature is inert without a threshold.
- `tests/test_context_assembler_hybrid.py` — UPDATE (additive): one test proving the
  gate is mode-agnostic (fires identically under hybrid ranking).
- `tests/test_confidence_field.py` — no change expected (gate does not alter
  ConfidenceField internals); add a cross-check only if a helper is extracted.
- `tests/benchmarks/test_confidence_gate_refusal.py` — CREATE: seeded-fixture CI
  unit test for gate mechanics + the offline full-corpus number-producer (skipped
  when the LoCoMo cache is absent).

No existing test behavior is invalidated — every change is additive and gated behind
the new opt-in kwargs.

## Rabbit Holes

- **Per-mode thresholds.** Explicitly avoided by design: gating on `ConfidenceField`
  (always [0,1]) makes the gate mode-agnostic. Do not add composite-vs-RRF score
  normalization.
- **Gating the push path.** Out of scope — the gate only affects pull-path records.
- **Deriving a "real" confidence spread from LoCoMo.** The corpus is single-shot with
  no interaction history; chasing a non-degenerate organic spread is a multi-hour
  rabbit hole. Seed it and document the limitation instead.
- **Touching #479's RRF/fusion weighting.** Keep the gate block lexically disjoint
  from fusion code to minimize rebase conflict.
- **Shipping a default threshold.** Forbidden until maintainer sign-off (policy).

## Risks

### Risk 1: Rebase conflict with open PR #479
**Impact:** #479 (`feature/457-hybrid-fusion-v2`) edits the same file and
`docs/benchmarks.md`. Merging out of order could conflict.
**Mitigation:** Keep the gate as a small additive block in `assemble()` (not in
`_pull_path_hybrid`/fusion). Rebase onto latest `origin/main` immediately before
opening/updating the PR, and again right before merge-readiness if #479 has landed.
For `docs/benchmarks.md`, append a new refusal-metric subsection rather than editing
the cat-5 audit block #479/#471 own.

### Risk 2: Refusal number is statistically thin and could be overstated
**Impact:** Only 2/446 cat-5 items are genuinely unanswerable → TP ≤ 2. A single FP
swings precision wildly. Published naively it looks like a leaderboard claim.
**Mitigation:** Lead with the caveat (cold-start seeding + ≤2 true positives), mirror
PR #471's honesty framing, and label it explicitly NOT leaderboard-comparable in both
the script output and `docs/benchmarks.md`.

### Risk 3: Cold-start degeneracy makes the gate look like a no-op
**Impact:** Without seeding, every candidate has identical `initial_confidence`, so
`gate_score` is constant and the gate either refuses everything or nothing.
**Mitigation:** The benchmark seeds a realistic spread and documents *why*. The unit
tests seed explicit confidence values so gate behavior is deterministic and legible.

## Race Conditions

No race conditions identified. The gate is a synchronous, single-threaded read of one
already-retrieved record's confidence value, evaluated inline in `assemble()` before
any post-effects run. Clearing `all_pull_candidates` happens before `_post_effects`
consumes it, in the same call frame.

## No-Gos (Out of Scope)

- [ORDERED] Promoting `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD` to
  `Defaults.CONFIDENCE_GATE_THRESHOLD` or to a non-None ctor default — blocked on
  maintainer sign-off (2026-06-11 policy-defaults decision).
- [SEPARATE-SLUG #457] Weighted / query-adaptive RRF fusion changes to the hybrid
  pull path — owned by PR #479; this plan must not touch fusion logic.
- Gating the push path or budget selection — the gate is pull-path-only by design
  (requester decision).

## Update System

No update-system changes required — this is a purely internal library feature reached
via the `ContextAssembler` Python API.

## Agent Integration

No agent/MCP integration required — `ContextAssembler` is a library recipe consumed
directly in Python; the new kwargs are part of that same surface.

## Documentation

### Feature Documentation
- [ ] Update `docs/features/agent-memory.md` ContextAssembler section (starts line
  ~1224): document `confidence_gate_threshold` / `confidence_gate_mode`, the
  mode-agnostic gate mechanism, the `metadata["gate"]` shape, "refuse" vs "flag"
  semantics, the no-default policy, and the EXPERIMENTAL constant.
- [ ] Update `docs/benchmarks.md`: add a refusal-metric subsection (append, do not
  edit the #471 cat-5 audit block) reporting the number with the cold-start +
  ≤2-true-positive caveat, and explicitly stating it is not leaderboard-comparable.

### External Documentation Site
- [ ] `mkdocs build --strict` passes (part of `scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Docstring on the two new ctor kwargs + `Raises: QueryException`.
- [ ] Comment on the `all_pull_candidates = []` clear explaining the
  competitive-suppression rationale.
- [ ] EXPERIMENTAL constant carries the mandated NOT-a-shipped-default comment.

## Success Criteria

- [ ] `confidence_gate_threshold=None` (default) → `AssemblyResult.metadata` is
  bit-for-bit identical to pre-change (no `gate` key).
- [ ] Threshold configured on a model without `ConfidenceField` → `QueryException` at
  construction (mirrors hybrid validation).
- [ ] Invalid `confidence_gate_mode` → `QueryException` at construction.
- [ ] "refuse" + gate_score < threshold → zero pull records injected, push path
  intact, `all_pull_candidates` cleared (no competitive suppression on refusal),
  `metadata["gate"]["gated"] is True`, `applied True`.
- [ ] "flag" + gate_score < threshold → records retained, `metadata["gate"]["gated"]
  is True`, nothing dropped.
- [ ] Empty pull_records with threshold configured → `applied False`,
  `gate_score None`, `gated False`, dict attached.
- [ ] Gate is mode-agnostic: identical behavior asserted under composite and hybrid.
- [ ] `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5` present with the required
  comment; no `Defaults.CONFIDENCE_GATE_THRESHOLD` added; ctor default stays None.
- [ ] Refusal-metric benchmark produces a number on the cat-5 slice with the
  cold-start caveat documented; runs only when the LoCoMo cache is present (skipped
  otherwise); the seeded-fixture unit test runs in CI.
- [ ] Targeted tests pass:
  `pytest tests/test_context_assembler.py tests/test_context_assembler_hybrid.py
  tests/test_confidence_field.py tests/benchmarks/test_confidence_gate_refusal.py`.
- [ ] `scripts/ci-local.sh --fast` (or equivalent) passes before PR.
- [ ] Valkey-safe: no Redis modules introduced (gate is a plain hash read via
  `get_confidence`).
- [ ] Documentation updated (`/do-docs`).
- [ ] PR OPEN (not merged); PR description's final section explicitly flags that
  `confidence_gate_threshold` ships with no default and
  `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD` needs maintainer sign-off.

## Team Orchestration

Solo dev executes linearly; the gate mechanism and its tests are tightly coupled and
best built in one pass, then the benchmark, then docs.

### Team Members

- **Builder (gate)**
  - Name: gate-builder
  - Role: Implement ctor kwargs, validation, gate block, EXPERIMENTAL constant, and
    unit tests.
  - Agent Type: builder
  - Domain: Redis/Popoto data
  - Resume: true

- **Builder (benchmark)**
  - Name: refusal-benchmark-builder
  - Role: Refusal-metric script/test + seeded fixture + cold-start caveat.
  - Agent Type: builder
  - Resume: true

- **Documentarian**
  - Name: gate-docs
  - Role: agent-memory.md + benchmarks.md updates.
  - Agent Type: documentarian
  - Resume: true

- **Validator**
  - Name: gate-validator
  - Role: Verify success criteria, run targeted tests + `ci-local.sh --fast`.
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Gate mechanism + unit tests
- **Task ID**: build-gate
- **Depends On**: none
- **Validates**: tests/test_context_assembler.py, tests/test_context_assembler_hybrid.py
- **Assigned To**: gate-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5` with the mandated comment in the
  Tuning Constants block. Do NOT add `Defaults.CONFIDENCE_GATE_THRESHOLD`.
- Add keyword-only ctor kwargs `confidence_gate_threshold=None`,
  `confidence_gate_mode="refuse"`; store on `self`.
- Add construction-time validation (mode in {refuse,flag}; ConfidenceField present),
  raising `QueryException` mirroring the hybrid-mode block's style.
- Add the additive gate block in `assemble()` after the pull path (rank-0
  `get_confidence`, refuse drops pull_records + clears all_pull_candidates, flag
  keeps). Attach `metadata["gate"]` only when threshold is not None.
- Write unit tests covering all Success Criteria rows for the mechanism (refuse/flag ×
  gated/not, empty-pull, threshold-None identical-metadata, both QueryException paths,
  mode-agnostic under hybrid).

### 2. Refusal-metric benchmark
- **Task ID**: build-benchmark
- **Depends On**: build-gate
- **Validates**: tests/benchmarks/test_confidence_gate_refusal.py (create)
- **Assigned To**: refusal-benchmark-builder
- **Agent Type**: builder
- **Parallel**: false
- Load cat-5 items via `tests.benchmarks.datasets.locomo.iter_items`; identify the 2
  genuinely-unanswerable items by answer text, not the adversarial flag.
- Seed ConfidenceField values (realistic spread) and document the cold-start
  limitation in module + function docstrings.
- Run the gate at `EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD`; compute refusal precision
  TP/(TP+FP); print the number with the caveat and the ≤2-TP thinness warning.
- Gate the full-corpus path on cache presence (skip when
  `~/.cache/popoto_benchmarks/locomo.json` absent); provide a deterministic
  seeded-fixture unit test that runs in CI.

### 3. Documentation
- **Task ID**: document-feature
- **Depends On**: build-gate, build-benchmark
- **Assigned To**: gate-docs
- **Agent Type**: documentarian
- **Parallel**: false
- Update `docs/features/agent-memory.md` ContextAssembler section and
  `docs/benchmarks.md` (append refusal subsection) per the Documentation section.

### 4. Final validation
- **Task ID**: validate-all
- **Depends On**: build-gate, build-benchmark, document-feature
- **Assigned To**: gate-validator
- **Agent Type**: validator
- **Parallel**: false
- Run the targeted test set + `scripts/ci-local.sh --fast`; verify every Success
  Criterion; confirm no `Defaults.CONFIDENCE_GATE_THRESHOLD` and ctor default is None;
  confirm the PR description flags the no-default / sign-off requirement.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Gate tests pass | `pytest tests/test_context_assembler.py tests/test_context_assembler_hybrid.py -q` | exit code 0 |
| Confidence tests pass | `pytest tests/test_confidence_field.py -q` | exit code 0 |
| Refusal benchmark test passes | `pytest tests/benchmarks/test_confidence_gate_refusal.py -q` | exit code 0 |
| EXPERIMENTAL constant present | `grep -c "EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5" src/popoto/recipes/context_assembler.py` | output contains 1 |
| No shipped default in Defaults | `grep -c "CONFIDENCE_GATE_THRESHOLD" src/popoto/fields/constants.py` | match count == 0 |
| Ctor default stays None | `grep -c "confidence_gate_threshold=None" src/popoto/recipes/context_assembler.py` | output > 0 |
| gate metadata only when configured | `grep -c 'metadata\["gate"\]' src/popoto/recipes/context_assembler.py` | output > 0 |
| Docs build strict | `mkdocs build --strict` | exit code 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

None outstanding — the requester specified the full design (gate keying, kwargs,
validation, refuse/flag semantics, metadata shape, no-default policy, EXPERIMENTAL
constant, refusal-metric approach, #479 coordination). The only policy-gated item
(promoting the threshold to a shipped default) is explicitly out of scope for this PR
and flagged for maintainer sign-off.
