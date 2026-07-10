---
status: Planning
type: feature
appetite: Small
owner: valorengels
created: 2026-07-10
tracking: https://github.com/tomcounsell/popoto/issues/453
last_comment_id: 4934106534
---

# Publish Benchmark Results on the Docs Site (auto-generated from committed artifacts)

## Problem

Popoto's benchmark numbers live in three disconnected places — the committed
`*_latest.{json,md}` artifacts under `tests/benchmarks/results/`, a hand-edited
summary table in `docs/benchmarks.md`, and PR descriptions. None is a
first-class results page on the public docs site ([popoto.io](https://popoto.io/)).

**Current behavior:**
- Every benchmark run requires a human to hand-transcribe numbers from the JSON
  artifact into `docs/benchmarks.md`, or the published numbers silently go stale.
- `docs/benchmarks/memory_lifecycle_baseline.md` is a tracked file but is **not
  referenced anywhere in `mkdocs.yml`'s `nav:`** — an orphan page reachable only
  by direct URL, invisible to browsing visitors.
- `docs/benchmarks.md` is buried under `Cookbook → Benchmarking`.

**Desired outcome:**
- A dedicated top-level **Benchmarks** nav section whose results pages are
  auto-rendered from the committed `*_latest.{json,md}` artifacts at build time —
  so a future benchmark run's artifact commit appears on the next docs deploy
  with zero manual doc-writing.
- The existing benchmark how-to (`docs/benchmarks.md`) and the previously-orphaned
  baseline doc (`docs/benchmarks/memory_lifecycle_baseline.md`) are both navigable
  under that section.

## Freshness Check

**Baseline commit:** 6fdec37 (`git rev-parse HEAD` at plan time)
**Issue filed at:** 2026-07-10 (same day; content-framing comment posted 2026-07-10T09:50:07Z)
**Disposition:** Unchanged

**File:line references re-verified:**
- `mkdocs.yml:68` — `- Benchmarking: 'benchmarks.md'` under Cookbook — still holds, confirmed verbatim.
- `docs/scripts/gen_api_pages.py` — established `mkdocs-gen-files` generator pattern (module docstring, skip-with-stderr-note, `mkdocs_gen_files.open`/`set_edit_path`) — still present, directly reusable.
- `tests/benchmarks/results/external/{longmemeval_s,locomo}_latest{,_hybrid}.{json,md}` and `tests/benchmarks/results/csr/csr_latest.{json,md}` — all present as symlinks to dated reports. Confirmed.
- `docs/benchmarks/memory_lifecycle_baseline.md` — exists, and `grep` confirms it appears nowhere in `mkdocs.yml` nav. This is an **add**, not a move (matches the issue's Recon "Revised" note).

**Cited sibling issues/PRs re-checked:**
- #452 (LoCoMo full run) / #443 (LongMemEval-S hybrid) — merged; produced the current headline numbers this issue publishes. No re-measurement needed.
- #375 (docs site setup) — established the generator pattern being extended.
- #418 (CSR harness) — CSR artifact present and current.

**Commits on main since issue filed (touching referenced files):** none affecting `mkdocs.yml`, `docs/scripts/`, or the `_latest` artifacts.

**Active plans in `docs/plans/` overlapping this area:** `benchmarking_strategy_2026-07.md` and `retrieval_arm_diagnostics_vector_baseline.md` concern *producing* benchmark numbers; this plan only *publishes* existing ones. No conflict — this plan is a leaf consumer of their artifacts.

**Notes:** No drift. All issue claims hold against current main.

## Prior Art

- **#375 (PR)**: Set up the docs site (Material theme, `mkdocs-gen-files` API reference, `llms.txt`, CI auto-deploy). Established the exact generator pattern this plan extends. Success.
- **#452 / #443 (PRs)**: Ran the full LoCoMo and LongMemEval-S benchmarks and wrote the current hand-maintained tables in `docs/benchmarks.md`. This plan publishes those already-accurate numbers; it does not re-measure.
- **#418 (issue)**: Introduced the deterministic CSR harness as a per-PR CI gate. Its `csr_latest` artifact is one of the pages this plan generates.
- No prior attempt to auto-generate benchmark pages exists — greenfield generator, reusing an established pattern.

## Research

No external WebSearch needed — the `mkdocs-gen-files` plugin is already a project dependency and its usage is fully demonstrated by `docs/scripts/gen_api_pages.py` and `gen_llms_full.py` in-repo. The content-framing requirements (MEMTIER anchor arXiv:2605.03675, LoCoMo variant taxonomy, metric-family non-convertibility) are supplied verbatim in the issue's 2026-07-10 content-framing comment and in project memory (`project_benchmark_track_status.md`: metric-family doctrine, MEMTIER anchor). No new measurement or citation lookup required.

## Data Flow

1. **Entry point**: `mkdocs build` (locally via `scripts/ci-local.sh docs`, in CI via `deploy-docs.yml`) invokes the `gen-files` plugin.
2. **Generator**: `docs/scripts/gen_benchmark_pages.py` runs. For each known artifact spec it resolves `tests/benchmarks/results/**/<name>_latest.{json,md}`.
3. **Per artifact**: reads the `.md` (pre-rendered tables) for page body and the `.json` (`summary`, `by_question_type`, `run_date`, `retrieval_mode`, `dataset`) for the index headline row. Emits a virtual page via `mkdocs_gen_files.open("benchmarks/results/<slug>.md", "w")`.
4. **Missing artifact**: skip silently with a `[gen_benchmark_pages] skipping ...` stderr note (mirrors `gen_api_pages.py`), never raising — so `--strict` stays green.
5. **Index page**: emit `benchmarks/results/index.md` carrying the framing prose (metric families, MEMTIER regime anchor, LoCoMo variant naming, cat-5 caveat) + a headline table of whichever pages were generated.
6. **Nav**: `mkdocs.yml` static `nav:` references the generated pages plus `benchmarks.md` and `benchmarks/memory_lifecycle_baseline.md` under a new top-level `Benchmarks` section.
7. **Output**: rendered pages appear under `/benchmarks/` on the deployed site.

## Architectural Impact

- **New dependencies**: none. `mkdocs-gen-files` already in `pyproject.toml`.
- **Interface changes**: none (build-time only; no library code touched).
- **Coupling**: adds a build-time read dependency from docs → `tests/benchmarks/results/`. Decoupled via graceful degradation so the docs build never hard-fails on artifact churn.
- **Data ownership**: unchanged — artifacts remain owned by the harnesses; docs are a read-only consumer.
- **Reversibility**: trivial. Delete the script, remove its `scripts:` entry, restore the Cookbook nav line.

## Appetite

**Size:** Small

**Team:** Solo dev, code reviewer

**Interactions:**
- PM check-ins: 0 (scope is fully specified by the issue + content-framing comment)
- Review rounds: 1 (PR review against plan + content-framing correctness)

## Prerequisites

No external prerequisites. The docs toolchain and all input artifacts are already in the repo. Local gate: `scripts/ci-local.sh docs` (runs `mkdocs build --strict`).

## Solution

### Key Elements

- **`docs/scripts/gen_benchmark_pages.py`**: a `mkdocs-gen-files` build-time generator that emits one virtual results page per available `*_latest` artifact plus a section index, following `gen_api_pages.py` conventions (module docstring documenting output layout + skip rules; `mkdocs_gen_files.open`/`set_edit_path`; skip-with-stderr-note on missing input).
- **Artifact spec table (in the script)**: an explicit list of (dataset label, retrieval mode, artifact path, page slug) tuples for LongMemEval-S lexical, LongMemEval-S hybrid, LoCoMo lexical, LoCoMo hybrid, CSR. Explicit — not a directory glob — so page titles/order are deterministic and the framing text can be attached per dataset.
- **Framing content (authored in the script, per issue content-framing comment)**: the section index carries (1) metric-family non-convertibility warning, (2) MEMTIER retrieval-regime anchor for LoCoMo, (3) explicit LoCoMo variant naming (10 dialogues / 5 categories incl. adversarial / 1986 QA pairs), (4) LongMemEval-S as the headline with the agentmemory like-for-like win, (5) cat-5 adversarial caveat. Per-page notes carry the mode-specific framing.
- **`mkdocs.yml` nav edit**: new top-level `Benchmarks` section; remove `Cookbook → Benchmarking`; register the new script in `gen-files → scripts:`.

### Flow

Docs site nav → click **Benchmarks** tab → **Overview** (framing + headline table) → pick a dataset page (e.g. *LongMemEval-S (hybrid)*) → see the run's rendered tables → or open *Benchmarking* (how-to) / *Memory Lifecycle Baseline*.

### Technical Approach

- **Page body from `.md`, headline from `.json`.** The committed `.md` artifacts are already nicely rendered (Summary + By-question-type tables); embed their body verbatim rather than re-deriving tables from JSON (avoids drift and re-formatting bugs). Read the `.json` only for the index headline row (dataset, mode, run_date, R@1/R@5/R@10/MRR from `summary`). This keeps the "artifact commit auto-publishes" guarantee: the numbers on the page are exactly the committed artifact's numbers.
- **H1 handling.** The `.md` artifacts start with `# Popoto External Benchmark: <dataset>`. Emit a page H1 from the spec (e.g. `# LongMemEval-S — Hybrid Retrieval`) and either keep or demote the artifact's own H1 to avoid two `# ` headings; simplest is to strip the artifact's leading H1 line and splice the remaining body under the page's H1. Document this transform in the module docstring.
- **Graceful degradation.** Resolve each artifact path; if either the `.md` (required for body) is missing, `print(..., file=sys.stderr)` and `continue`. Never raise. The index lists only pages actually generated. This satisfies the "delete an artifact, build still passes" acceptance criterion.
- **Symlink safety.** `_latest.*` are symlinks; read via normal file open (follows symlinks). Guard against dangling symlinks (`Path.exists()` returns False for a broken link → treated as missing → skipped).
- **Valkey-safety.** The generator writes only framing prose + embedded harness markdown; it introduces no Redis-module command strings. A grep gate (`FT.|BF.|CMS.|TOPK.`) over the script and, where feasible, the built output confirms absence.
- **literate-nav interplay.** The API reference uses `literate-nav` with `SUMMARY.md`; the benchmark pages are referenced by explicit static `nav:` entries instead (like the rest of the hand-written docs), so they don't need a SUMMARY and won't collide with the reference tree.

## Failure Path Test Strategy

### Exception Handling Coverage
- The generator's only tolerated failure is a missing/dangling artifact, handled by an explicit `exists()` check + `continue` + stderr note (not a bare `except: pass`). Verified by the graceful-degradation acceptance test (temporarily hide an artifact, assert `mkdocs build --strict` still exits 0 and the page is absent).
- No other exception handlers are introduced.

### Empty/Invalid Input Handling
- Missing `.md`: page skipped (covered above).
- Present-but-empty `.md`: page emitted with just its H1 (degenerate but non-fatal). Acceptable — artifacts are harness-produced, never empty in practice; not worth special-casing.
- Malformed `.json`: the index headline row for that dataset is omitted (wrap the per-artifact JSON read in a narrow try/continue with stderr note); the `.md`-derived page still renders. Documented in the script.

### Error State Rendering
- User-visible output is the built docs. The "error" path (a dataset not yet run) renders as that page simply being absent from nav/index — verified by the graceful-degradation check. No stack trace or broken link is surfaced.

## Test Impact

No existing tests affected — this is an additive, docs-only, build-time change. It modifies no library code and no existing test asserts on `mkdocs` nav structure or `docs/scripts/` output. The relevant gate is the docs build itself (`scripts/ci-local.sh docs` → `mkdocs build --strict`), plus a one-shot graceful-degradation check run manually during build/verify.

## Rabbit Holes

- **Re-deriving tables from JSON.** Tempting for "purity," but the `.md` artifacts are already rendered and committed. Re-rendering risks format drift and duplicates harness logic. Embed the `.md`; read JSON only for the index headline. (Dropped item from the issue Recon.)
- **Re-writing / re-measuring headline numbers.** Explicitly out of scope — #452/#443 already produced them. Publish, don't re-measure.
- **Cross-comparing recall vs. judge-accuracy leaderboards.** Forbidden by the content-framing comment. Do not build a "vs Mem0/Zep" table. MEMTIER is the only cross-anchor, and only as a retrieval-regime band.
- **Auto-discovering artifacts by glob.** Would make page titles/order/ framing non-deterministic and couple the page set to filesystem accidents. Use an explicit spec list.
- **Touching `literate-nav`/`SUMMARY.md`.** The benchmark pages use static nav; don't entangle them with the API-reference literate-nav machinery.

## Risks

### Risk 1: Two `# ` H1 headings per page (page H1 + embedded artifact H1)
**Impact:** Ugly rendering, possible `toc`/anchor duplication warnings under `--strict`.
**Mitigation:** Strip the artifact's leading `# ...` line before splicing the body under the generator's own H1. Verified by `mkdocs build --strict` producing no new warnings.

### Risk 2: Content-framing correctness (public-site accuracy requirement)
**Impact:** Publishing recall beside judge-accuracy, or omitting the LoCoMo variant/cat-5/MEMTIER framing, would be a factual defect on the public site — the issue flags this as a correctness requirement, not style.
**Mitigation:** The five framing requirements are encoded as explicit prose in the index + per-page notes; PR review (`/do-pr-review`) checks each against the issue's content-framing comment as an acceptance gate.

### Risk 3: `--strict` fails on a new orphan/unreferenced generated page
**Impact:** Build breaks if a generated page isn't reachable from nav.
**Mitigation:** Every generated page is explicitly listed in the static `nav:` under Benchmarks. The index links the rest. Confirm with `mkdocs build --strict` (which warns on unreferenced pages).

## Race Conditions

No race conditions identified — the generator is synchronous, single-threaded, runs once per build, and reads immutable committed artifacts. No shared mutable state, no concurrency.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #453-followups] The cat-5 evidence-matching audit and the vector-only retrieval baseline run are explicitly follow-ups named in the content-framing comment ("do NOT block this issue"). *(Tracked in the issue's comment as filed-separately follow-ups; this plan only publishes the cat-5 caveat, it does not resolve the audit.)*
- Re-measuring or re-writing any benchmark numbers — [ORDERED] depends on the harness runs already completed in #452/#443; publishing consumes their committed output.
- A "Popoto vs. leaderboard systems" comparison table — [EXTERNAL] forbidden by the 2026-07-10 content-framing methodology review; metric families are non-convertible.

_Note: the two follow-ups referenced above are described in the issue's content-framing comment as separately-filed; if a bare issue number is required by the No-Gos validator, the PR will cite the comment URL as the tracking anchor since no standalone code outcome is deferred here._

## Update System

No update-system changes required — this is a docs build-time feature deployed via the existing `deploy-docs.yml` GitHub Pages workflow. No new config to propagate.

## Agent Integration

No agent integration required — this is a docs-site/build-time change with no runtime or MCP surface.

## Documentation

This change *is* documentation. Specifics:
- [ ] `docs/scripts/gen_benchmark_pages.py` module docstring documents output layout + skip rules (mirrors `gen_api_pages.py`).
- [ ] The generated Benchmarks section is self-documenting (index explains the metric regime).
- [ ] `docs/benchmarks.md` how-to and `docs/benchmarks/memory_lifecycle_baseline.md` become navigable (no content rewrite needed).
- [ ] `/do-docs` run after build to catch any cascade (e.g. `llms.txt` / cross-links).

## Success Criteria

- [ ] `docs/scripts/gen_benchmark_pages.py` exists, is registered in `mkdocs.yml`'s `gen-files → scripts:`, and generates a virtual page per available `*_latest` artifact (LongMemEval-S lexical + hybrid, LoCoMo lexical + hybrid, CSR).
- [ ] Deleting/renaming any one `_latest` artifact and re-running `mkdocs build --strict` does not fail the build — the page is simply absent (graceful degradation).
- [ ] `mkdocs.yml` has a new top-level `Benchmarks` nav section containing the generated pages, `benchmarks.md`, and `benchmarks/memory_lifecycle_baseline.md`.
- [ ] The `Cookbook → Benchmarking` nav entry is removed (not duplicated).
- [ ] `mkdocs build --strict` passes with no new warnings or errors.
- [ ] `scripts/ci-local.sh docs` passes.
- [ ] The LongMemEval-S hybrid page surfaces R@1 0.894 / R@5 0.986 / R@10 0.992 / MRR 0.932 and notes it beats the agentmemory reference (R@5 0.952 / R@10 0.986 / MRR 0.882).
- [ ] The LoCoMo page reports hybrid underperforming lexical factually (R@1 0.1667 vs 0.2986, MRR 0.2835 vs 0.4124), names the variant (10 dialogues / 5 categories incl. adversarial / 1986 QA pairs), and anchors it to the MEMTIER retrieval regime (LoCoMo R@1 0.10–0.30 band).
- [ ] CSR page frames its numbers as a report-only CI regression gate, not a leaderboard metric.
- [ ] The index carries the metric-family non-convertibility warning and the cat-5 adversarial caveat.
- [ ] A grep for `FT.|BF.|CMS.|TOPK.` across the new script and generated output returns nothing (Valkey-safety).
- [ ] Documentation cascade checked (`/do-docs`).

## Team Orchestration

Small, single-file-plus-config change. One builder does the generator + nav edit; one code reviewer verifies content-framing correctness against the issue comment. No parallel fan-out needed.

### Team Members

- **Builder (benchmark-pages)**
  - Name: benchmark-pages-builder
  - Role: Implement `gen_benchmark_pages.py`, edit `mkdocs.yml` nav + plugin registration, verify `mkdocs build --strict` and graceful degradation.
  - Agent Type: builder
  - Resume: true

- **Reviewer (content-framing)**
  - Name: content-framing-reviewer
  - Role: Verify the five content-framing requirements and Valkey-safety are met in generated output.
  - Agent Type: code-reviewer
  - Resume: true

## Step by Step Tasks

### 1. Implement the benchmark-pages generator
- **Task ID**: build-generator
- **Depends On**: none
- **Validates**: `mkdocs build --strict` (docs gate), `scripts/ci-local.sh docs`
- **Assigned To**: benchmark-pages-builder
- **Agent Type**: builder
- **Parallel**: false
- Create `docs/scripts/gen_benchmark_pages.py` with a module docstring documenting output layout + skip rules (mirror `gen_api_pages.py`).
- Define an explicit artifact spec list: LongMemEval-S lexical (`external/longmemeval_s_latest.{json,md}`), LongMemEval-S hybrid (`external/longmemeval_s_latest_hybrid.{json,md}`), LoCoMo lexical (`external/locomo_latest.{json,md}`), LoCoMo hybrid (`external/locomo_latest_hybrid.{json,md}`), CSR (`csr/csr_latest.{json,md}`).
- For each spec: skip (stderr note, no raise) if the `.md` is missing/dangling; else strip the artifact's leading H1 and emit `benchmarks/results/<slug>.md` under a generator-authored H1; `set_edit_path` back to the artifact.
- Read `.json` `summary` (guarded try/continue) for the index headline row.
- Emit `benchmarks/results/index.md` with the five framing elements (metric-family non-convertibility, MEMTIER LoCoMo anchor, LoCoMo variant naming, LongMemEval-S headline + agentmemory win, cat-5 caveat) + a headline table of generated pages.
- Attach per-page mode-specific notes (CSR = report-only gate; LoCoMo hybrid underperformance stated factually; LongMemEval-S hybrid = like-for-like win).

### 2. Wire nav + plugin registration
- **Task ID**: build-nav
- **Depends On**: build-generator
- **Assigned To**: benchmark-pages-builder
- **Agent Type**: builder
- **Parallel**: false
- Register `docs/scripts/gen_benchmark_pages.py` in `mkdocs.yml` `plugins → gen-files → scripts:`.
- Add top-level `Benchmarks` nav section (peer of Getting Started/Core Features/Agent Memory/Cookbook/API Reference) listing: Overview (`benchmarks/results/index.md`), the generated result pages, `benchmarks.md` (how-to), `benchmarks/memory_lifecycle_baseline.md`.
- Remove `Cookbook → Benchmarking: 'benchmarks.md'` (line 68).
- Run `scripts/ci-local.sh docs` (or `mkdocs build --strict`); fix any warnings.

### 3. Verify graceful degradation + Valkey-safety
- **Task ID**: build-verify
- **Depends On**: build-nav
- **Assigned To**: benchmark-pages-builder
- **Agent Type**: builder
- **Parallel**: false
- Temporarily hide one `_latest` artifact, confirm `mkdocs build --strict` still exits 0 and the page is absent; restore.
- `grep -REn 'FT\.|BF\.|CMS\.|TOPK\.' docs/scripts/gen_benchmark_pages.py` returns nothing; spot-check built `site/benchmarks/` output for the same.

### 4. Content-framing review
- **Task ID**: review-framing
- **Depends On**: build-verify
- **Assigned To**: content-framing-reviewer
- **Agent Type**: code-reviewer
- **Parallel**: false
- Verify each of the five content-framing requirements is present and correct in generated output.
- Confirm no recall-vs-judge-accuracy cross-tabulation appears anywhere.
- Report pass/fail.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Docs build strict | `mkdocs build --strict` | exit code 0 |
| CI docs gate | `scripts/ci-local.sh docs` | exit code 0 |
| Generator registered | `grep -c 'gen_benchmark_pages.py' mkdocs.yml` | output > 0 |
| Benchmarks nav section added | `grep -c '    - Benchmarks:' mkdocs.yml` | output > 0 |
| Cookbook Benchmarking removed | `grep -c "Benchmarking: 'benchmarks.md'" mkdocs.yml` | match count == 0 |
| LongMemEval-S hybrid headline present | `mkdocs build && grep -rl '0.894' site/benchmarks/` | exit code 0 |
| No Redis-module commands in generator | `grep -REn 'FT\.\|BF\.\|CMS\.\|TOPK\.' docs/scripts/gen_benchmark_pages.py` | exit code 1 |
| Baseline doc now navigable | `grep -c 'memory_lifecycle_baseline.md' mkdocs.yml` | output > 0 |

## Critique Results

<!-- Populated by /do-plan-critique (war room). Leave empty until critique is run. -->
| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|

---

## Open Questions

None blocking. The issue body + content-framing comment fully specify scope, acceptance criteria, and the mandatory framing. Two design choices were made without needing a product decision and are documented above:
1. **Page body sourced from the `.md` artifact (verbatim), index headline from `.json`** — chosen to guarantee published numbers equal committed numbers and to avoid re-rendering drift (see Rabbit Holes).
2. **Explicit artifact spec list rather than a directory glob** — chosen for deterministic page order/titles and per-dataset framing attachment (see Rabbit Holes).

If the maintainer prefers one page per *dataset* (lexical+hybrid merged) rather than one page per *artifact*, that is a cosmetic reshaping of the spec list and can be adjusted at build with no architectural change.
