---
status: Ready
type: feature
appetite: Small
owner: valorengels
created: 2026-07-14
revision_applied: true
revision_applied_at: 2026-07-14T06:04:09Z
tracking: https://github.com/tomcounsell/popoto/issues/469
last_comment_id: none
---

# Benchmark Docs Generator: Publish Vector-Mode Results (and stop the silent gap)

## Problem

The benchmark docs generator `docs/scripts/gen_benchmark_pages.py` decides which
result artifacts get published to the docs site ([popoto.io](https://popoto.io/))
from an **explicit hardcoded `SPECS` tuple**, not from directory auto-discovery.
Today that list covers lexical + hybrid (LongMemEval-S, LoCoMo) + CSR only —
there is **no vector-mode `Spec`**.

The vector retrieval mode merged in #455 / PR #467 produces `_vector`-suffixed
artifacts (`external/{dataset}_latest_vector.{json,md}`). Because no vector
`Spec` exists, the moment such an artifact is committed it will **not** publish
to the docs site — a maintainer must hand-add a `Spec`.

**Current behavior:**
- The generator degrades gracefully — a missing artifact is skipped, never
  raised, so `mkdocs build --strict` stays green. This is deliberate.
- The failure mode is therefore **silent**: a `_vector` artifact can exist in
  the repo while its docs page never appears and nothing errors. Easy to forget,
  hard to notice.
- Verified 2026-07-14: no `_vector` artifact is currently committed under
  `tests/benchmarks/results/external/`, so nothing is dropped *today* — this is a
  latent gap that trips the moment vector artifacts land.

**Desired outcome:**
- A vector-mode results page publishes automatically the moment a
  `{dataset}_latest_vector.{json,md}` artifact is committed — zero further
  hand-editing, same as lexical/hybrid.
- Any *future* unmapped `_latest` artifact (a new retrieval mode nobody wired a
  `Spec` for) produces a **loud build-time warning** instead of silently
  vanishing — the root complaint of this issue.
- Deterministic page order, per-page titles, and per-dataset framing prose are
  preserved (the deliberate #466 design choice); metric-family doctrine is
  respected (never cross-compare recall vs. judge-accuracy; never fabricate
  numbers).

## Freshness Check

**Baseline commit:** 0b4c629 (`git rev-parse origin/main` at plan time)
**Issue filed at:** 2026-07-14 (same day as planning)
**Disposition:** Unchanged

**File:line references re-verified against current main:**
- `docs/scripts/gen_benchmark_pages.py` — `SPECS: tuple[Spec, ...]` present
  (`longmemeval_s_lexical`, `longmemeval_s_hybrid`, `locomo_lexical`,
  `locomo_hybrid`, `csr`); none reference a `_vector` stem. `Spec` frozen
  dataclass fields (`slug`, `title`, `nav_title`, `stem`, `kind`, `note`)
  confirmed verbatim. `_write_page` skips a missing `.md` with a stderr note and
  returns `None`; `main()` is called unconditionally at module bottom. Confirmed.
- `tests/benchmarks/run_external.py:601-602` — non-lexical runs write
  `{slug}_latest{suffix}.{json,md}` where `suffix` is `_vector` for vector mode.
  So vector artifacts land as `external/{dataset}_latest_vector.{json,md}`.
  Confirmed.
- `tests/benchmarks/results/external/` — only `_latest` / `_latest_hybrid`
  symlinks per dataset; no `_vector` artifact present. Confirmed (matches the
  issue's 2026-07-14 verification).
- `mkdocs.yml:124-128` — `gen-files` plugin runs `docs/scripts/gen_benchmark_pages.py`.
  Confirmed. The plugin execs the script via `runpy.run_path`, which sets
  `__name__ == "<run_path>"` (verified empirically), not `"__main__"`.

**Cited sibling issues/PRs re-checked:**
- PR #467 (#455, vector retrieval mode) — **merged** (commit 19bc96f). Produces
  the `_vector` artifacts this plan publishes. Its `docs/benchmarks.md` framing
  states vector isolates **only the dense arm**, not the graph/co-occurrence arm
  inside hybrid — the framing note for the vector pages must echo this.
- PR #466 (benchmark results docs publishing) — **merged** (commit 661168d).
  Introduced this generator and the deliberate explicit-list (not-glob) design.
- Issue #453 — parent (publish benchmark results). Plan
  `benchmark_results_docs_publishing.md` present; this plan is a leaf extension.

**Commits on main since issue filed (touching referenced files):** none since
0b4c629 touch `docs/scripts/gen_benchmark_pages.py` or the external artifacts.

**Active plans in `docs/plans/` overlapping this area:**
- `benchmark_results_docs_publishing.md` (#453) — introduced the generator; this
  plan extends its `SPECS` list. No conflict.
- Per the routing note, **#457** (weighted/query-adaptive hybrid fusion) also
  touches the benchmark/docs area and may conflict at merge. Mitigation: rebase
  on `origin/main` immediately before merge and resolve conflicts in
  `gen_benchmark_pages.py` / `docs/benchmarks.md` cleanly.

**Notes:** No drift. All issue claims hold against current main.

## Prior Art

- **PR #466 — benchmark results docs publishing**: introduced
  `gen_benchmark_pages.py` with the explicit `SPECS` list and the
  graceful-degradation skip design. This plan extends it; it does **not** revert
  the explicit-list decision.
- **PR #467 (#455) — vector retrieval mode**: produces the `_vector` artifacts.
  Merged. Its `docs/benchmarks.md` note is the canonical framing source for what
  vector mode measures ("isolates only the dense arm").
- No prior attempt to publish vector results exists; this is the first.

## Research

No relevant external findings — the work is entirely internal to the repo's
`mkdocs-gen-files` generator and the committed artifact layout. `runpy.run_path`
`__name__` behavior (`"<run_path>"`) was verified empirically rather than from
docs.

## Data Flow

1. **Artifact commit** — a benchmark run commits
   `tests/benchmarks/results/external/{dataset}_latest_vector.{json,md}`.
2. **Build trigger** — `mkdocs build --strict` runs the `gen-files` plugin,
   which `runpy.run_path`s `gen_benchmark_pages.py`.
3. **Spec resolution** — `main()` iterates `SPECS`; for each, `_write_page`
   resolves `RESULTS_ROOT/{stem}.md`. Present → page emitted; missing → skipped
   with stderr note (graceful degradation, unchanged).
4. **Orphan scan (new)** — after the spec loop, the generator scans
   `RESULTS_ROOT/external` for `*_latest*.md` artifacts not referenced by any
   `Spec.stem`, and emits a **stderr warning** per orphan (never raises).
5. **Output** — `benchmarks/results/{slug}.md` pages + `index.md` + `SUMMARY.md`
   under the built site; vector pages appear once their artifacts exist.

## Architectural Impact

- **New dependencies**: none.
- **Interface changes**: `SPECS` gains two entries; `main()` gains a
  post-loop orphan-scan call. The module bottom changes from an unconditional
  `main()` to a `if __name__ in ("__main__", "<run_path>"): main()` guard so the
  module is importable by a unit test without executing the generator (still runs
  under mkdocs, which uses `runpy` → `"<run_path>"`).
- **Coupling**: unchanged — still decoupled from filesystem accidents for
  *ordering/framing*; the orphan scan only *warns*, it never auto-publishes, so
  the deterministic-list guarantee holds.
- **Reversibility**: trivial — revert the two `Spec`s and the orphan-scan helper.

## Appetite

**Size:** Small

**Team:** Solo dev, PM (routing), code reviewer (PR gate)

**Interactions:**
- PM check-ins: 0-1
- Review rounds: 1

## Prerequisites

No external prerequisites. Local verification needs Redis on `localhost:6379`
(pytest plugin isolates on DB 15) and the docs toolchain (`mkdocs build
--strict` via `scripts/ci-local.sh docs`). Both are already provisioned in this
environment.

## Solution

### Key Elements

- **Two vector `Spec`s** — `longmemeval_s_vector` (stem
  `external/longmemeval_s_latest_vector`) and `locomo_vector` (stem
  `external/locomo_latest_vector`), both `kind="external"`. Placed so page order
  reads lexical → hybrid → vector per dataset, preserving deterministic ordering.
- **Doctrine-safe framing notes** — each vector `Spec.note` is a static
  admonition that (a) states vector = pure cosine over all-MiniLM-L6-v2,
  isolating **only the dense arm** (echoing PR #467), (b) anchors LoCoMo in the
  MEMTIER retrieval regime, and (c) contains **no fabricated recall numbers** —
  numbers come from the artifact body/JSON at build time, so the note stays
  honest before any artifact lands.
- **Orphan-artifact scan** — a new helper walks `RESULTS_ROOT/external` for
  `*_latest*.md` files whose stem is referenced by no `Spec`, printing a loud
  `[gen_benchmark_pages] WARNING: unmapped artifact ...` to stderr. Never raises
  → `--strict` stays green; converts the silent gap into a visible one.
- **Importability guard** — guard the `main()` call so a unit test can import the
  module and exercise the pure helpers without running the full generator.

### Flow

Benchmark run commits `{dataset}_latest_vector.{json,md}` → next docs deploy runs
`gen_benchmark_pages.py` → vector `Spec` resolves the now-present artifact → a
`… — Vector (Dense/Embedding) Retrieval` page appears in the Benchmarks nav with
its framing note, exactly like lexical/hybrid — with zero code change at that
point.

### Technical Approach

- Add the two `Spec`s to the `SPECS` tuple in dataset-then-mode order
  (`longmemeval_s_lexical`, `longmemeval_s_hybrid`, **`longmemeval_s_vector`**,
  `locomo_lexical`, `locomo_hybrid`, **`locomo_vector`**, `csr`). Slug prefixes
  keep `_dataset_table("longmemeval_s")` / `_dataset_table("locomo")` grouping
  correct with no change to the index tables.
- Framing notes: `!!! note` admonitions. LongMemEval-S vector note frames it as
  the dense-arm isolation diagnostic (not the headline; hybrid remains headline).
  LoCoMo vector note carries the MEMTIER retrieval-regime anchor and the
  "isolates only the dense arm, not the graph/co-occurrence arm" caveat from
  PR #467. No hardcoded Recall@k values in either note.
- Add `_iter_spec_stems()` and `_warn_orphan_artifacts(specs, root=RESULTS_ROOT)`
  helpers. `_warn_orphan_artifacts` globs `root/"external"/"*_latest*.md"`,
  derives each artifact's stem relative to `root` (preserving the `external/`
  prefix, dropping `.md`), and warns for any not present in the set of
  `Spec.stem` values. Called from `main()` after the spec loop (order
  irrelevant; it only prints). **The `root` parameter is injectable** (defaults
  to `RESULTS_ROOT`) so tests pass a `tmp_path` root and never write into or
  read from the real `tests/benchmarks/results/external/` — this keeps
  Verification criterion 6 (no new files under `results/external/`) safe and the
  tests hermetic. Stem derivation stays relative to `root` so a mapped
  `Spec.stem` like `external/longmemeval_s_latest_vector` compares equal.
- Change module bottom to `if __name__ in ("__main__", "<run_path>"): main()`.
  Verified: mkdocs-gen-files execs via `runpy.run_path` → `__name__ ==
  "<run_path>"`, so the generator still runs at build; a unit `import` (module
  name `gen_benchmark_pages`) does not trigger `main()`.

## Failure Path Test Strategy

### Exception Handling Coverage
- The generator's existing skip paths (`_write_page` missing `.md`,
  `_read_summary` missing/malformed `.json`) already print-and-continue; this
  plan does not add new `except: pass`. The orphan scan itself performs no
  risky I/O beyond `Path.glob` (which returns empty on a missing dir) and
  `print`. A test asserts the orphan scan **warns** (observable stderr), not
  swallows.

### Empty/Invalid Input Handling
- Orphan scan on an empty/absent `external/` dir → no warnings, no error (glob
  yields nothing). Covered by a test using a temp dir with no artifacts.
- Vector `Spec` with no committed artifact → page skipped (existing graceful
  path), index/summary omit it. Verified by the `docs` gate staying green on the
  current repo state (no vector artifacts present).

### Error State Rendering
- User-visible output is the built docs. The failure path (artifact absent) is
  the *intended* graceful skip; verified by `mkdocs build --strict` staying
  green with no vector artifacts present (the current repo state).

## Test Impact

No existing tests are affected — there is currently **no** test module for
`gen_benchmark_pages.py` (confirmed: `grep -rl gen_benchmark_pages tests/`
returns nothing). This plan adds a new test module; it modifies no existing
tests and changes no runtime behavior of the shipped library.

New test module `tests/benchmarks/test_gen_benchmark_pages.py` (import-safe via
the `main()` guard):
- `test_vector_specs_present_and_ordered` — both vector specs exist, use the
  correct `external/{dataset}_latest_vector` stems, `kind="external"`, and sit
  after their dataset's hybrid spec in `SPECS`.
- `test_vector_notes_have_no_fabricated_numbers` — the vector `Spec.note`
  strings contain no `Recall@` / decimal metric literals (doctrine: don't
  fabricate numbers before artifacts exist).
- `test_orphan_artifact_scan_warns` — pass a `tmp_path` root containing an
  `external/foo_latest_experimental.md` (unmapped) via
  `_warn_orphan_artifacts(SPECS, root=tmp_path)`; assert a warning names the
  artifact.
- `test_orphan_scan_silent_when_all_mapped` — a `tmp_path` root whose only
  `external/*_latest*.md` files correspond to existing `Spec.stem`s emits no
  warning; an empty root emits no warning and does not raise.

All orphan-scan tests pass an injected `tmp_path` root — they never write into
or monkeypatch the real `RESULTS_ROOT`.

## Rabbit Holes

- **Full glob-based auto-discovery (issue direction (b))**: tempting, but it
  would forfeit the deliberate #466 deterministic-order + per-page-framing
  design and risk publishing a page with no doctrine-safe framing note. The
  orphan **warning** captures the safety benefit (no silent drop) without the
  cost. Out of scope.
- **Backfilling actual vector artifacts / running the vector benchmark**: this
  plan wires *publishing*; producing the `_vector` numbers is #455/#457 work and
  requires the ~90MB MiniLM download and a real run. Do not fabricate artifacts.
- **Restructuring the index headline tables to add a vector column**: the
  existing `_external_table` already renders any `kind="external"` row; vector
  pages will appear as rows automatically once their artifacts exist. No table
  surgery needed.

## Risks

### Risk 1: Vector framing note drifts from doctrine (cross-compares metric families)
**Impact:** Publishing a note that tabulates recall beside judge-accuracy, or
invents recall numbers, would violate benchmark doctrine and mislead readers.
**Mitigation:** Notes carry no numbers; they only frame *what* vector measures
(dense arm isolation) and anchor LoCoMo in the MEMTIER regime. A test asserts the
notes contain no metric literals.

### Risk 2: `main()` guard breaks the mkdocs build
**Impact:** If the guard predicate is wrong, the generator silently stops running
at build and *all* benchmark pages vanish.
**Mitigation:** Guard is `__name__ in ("__main__", "<run_path>")`; `<run_path>`
is empirically confirmed as the `runpy.run_path` name mkdocs uses. The `docs`
gate (`mkdocs build --strict`) in `scripts/ci-local.sh` is a hard verification
that the build still emits the existing pages.

### Risk 3: Merge conflict with #457 in the same files
**Impact:** #457 touches the benchmark/docs area; a dirty merge could drop a spec
or corrupt framing.
**Mitigation:** Rebase on `origin/main` immediately before merge; resolve
`gen_benchmark_pages.py` conflicts by keeping both sets of specs/logic; re-run
the `docs` gate post-rebase.

## Race Conditions

No race conditions identified — the generator is a synchronous, single-threaded
build-time script; the orphan scan is read-only `Path.glob` + `print`.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #457] Producing/tuning the actual vector-mode benchmark numbers
  (weighted/query-adaptive fusion, running the MiniLM vector arm) is #457 work,
  not this publishing wiring.
- Nothing else deferred — the publishing gap and the silent-drop root cause are
  both fully addressed in this plan.

## Update System

No update-system changes required — this is a build-time docs generator change
with no runtime library or deployment surface.

## Agent Integration

No agent integration required — this touches the docs build only; no tool/MCP
surface or bridge entry point is involved.

## Documentation

### Feature Documentation
- The generator's module docstring "Output layout" list must add the two vector
  page paths so the file self-documents what it emits.
- `docs/benchmarks.md` already documents vector mode (added in PR #467); confirm
  no additional prose is needed there. Run the `/do-docs` cascade to catch any
  cross-references (e.g. the Benchmarks overview) that should mention the vector
  pages.

## Verification

| # | Criterion | Check |
|---|-----------|-------|
| 1 | Two vector specs present, correct stems, correct order | `pytest tests/benchmarks/test_gen_benchmark_pages.py::test_vector_specs_present_and_ordered` |
| 2 | Vector notes contain no fabricated metric numbers | `pytest tests/benchmarks/test_gen_benchmark_pages.py::test_vector_notes_have_no_fabricated_numbers` |
| 3 | Unmapped artifact triggers a loud stderr warning (silent gap closed) | `pytest tests/benchmarks/test_gen_benchmark_pages.py::test_orphan_artifact_scan_warns` |
| 4 | Orphan scan silent when all artifacts mapped / dir empty | `pytest tests/benchmarks/test_gen_benchmark_pages.py::test_orphan_scan_silent_when_all_mapped` |
| 5 | Docs still build clean with no vector artifacts present (graceful) | `scripts/ci-local.sh docs` (`mkdocs build --strict`) |
| 6 | No fabricated vector artifacts committed | `git status` shows no new files under `tests/benchmarks/results/external/` |
