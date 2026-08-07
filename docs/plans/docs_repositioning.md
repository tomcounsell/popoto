---
status: Complete
type: chore
appetite: Medium
owner: Tom Counsell
created: 2026-08-07
tracking: https://github.com/tomcounsell/popoto/issues/511
last_comment_id: 5213135395
revision_applied: true
revision_applied_at: 2026-08-07T07:05:00Z
---

# Docs Repositioning: Agent Memory First, Radical Transparency

## Problem

A developer evaluating agent-memory options never encounters Popoto's argument for itself. The docs site presents "an ORM for Redis and Valkey" with a Restaurant CRUD hero; the intended flagship (SubconsciousMemory) is bullet 12 of 14 on the homepage and 4 nav levels deep; the public surface carries internal working documents (a GTM playbook, a raw research prompt, a page about a different product); and the copy-paste golden path is a configuration measurably weaker than the one that produced the published benchmark numbers.

Full evidence inventory: [issue #511](https://github.com/tomcounsell/popoto/issues/511). Maintainer alignment and critique findings: [the alignment comment](https://github.com/tomcounsell/popoto/issues/511#issuecomment-5213135395).

**Current behavior:** ORM-first site, buried flagship, internal artifacts published, three contradictory primitive counts, claims that are simultaneously under-stated (latency curve, install weight, #489 negative result, transparency practice — all unpublished or buried) and over-stated ("sub-6ms" vs a 6.021 ms artifact, a MEMTIER "band" claim its source paper contradicts, SIQ 1.0-vs-0.0 against a keyword-matcher stub).

**Desired outcome:** A memory-first site written to the radical-transparency posture: every claim traceable to a committed artifact or cited source with its limits disclosed in the same breath, the flagship promoted to first-class, internal artifacts off the public surface, and the documented golden path matching the benchmarked configuration.

## Freshness Check

**Baseline commit:** `c02796e`
**Issue filed at:** 2026-08-07 (this session)
**Disposition:** Unchanged

- Issue #511 was filed hours ago from live recon; one commit (`c02796e`, SortedField range-read limit pushdown) landed since — no overlap with any file this plan touches.
- All file:line references in #511 were produced by direct inspection this session; the three critique reports (recorded in the issue comment) additionally verified claims empirically (clean-venv install, verbatim quickstart run, committed-JSON inspection).
- Active plans overlap check: `docs/plans/` contains benchmark-strategy plans (`benchmarking_strategy_2026-07.md`, `benchmark_results_docs_publishing.md`, `benchmark_docs_gen_vector_mode.md`) that this plan treats as constraints, not conflicts. Note (critique finding 8): `benchmark_docs_gen_vector_mode.md:171,198,286` mandates the MEMTIER *anchor*; this plan keeps the anchor (96.7 ms/query latency reference, regime context) and kills only the "top of the 0.10–0.30 R@1 band" claim, which MEMTIER's own authors contradict. This is a deliberate, recorded narrowing of #453-era framing, not an accidental reversal.
- Revision-time drift (2026-08-07): `8ea5416` (PR #516) landed on main — equality/hashing, connection pool, single build system (deleted `poetry.lock`, edited `pyproject.toml`). No overlap with this plan's files; it does conflict with sibling PR #524's `pyproject.toml` edits, flagged for merge-time rebase.

## Prior Art

- **#456** Epic: SOTA memory system for live agents — strategy of record. Native benchmarks + retrieval parity; never cross-compare recall with judged accuracy. This plan's claims strategy implements that doctrine on the marketing surface.
- **#453 / PR #466**: benchmark results published on the site via `gen_benchmark_pages.py`; its `INDEX_FRAMING` block is the best-positioned copy on the site and the model for the new tone.
- **#409**: query-blind default retrieval found and fixed at the API level; the docs footgun it left behind (silent composite mode) is handled by sibling issue (code warning) plus explicit docs framing here.
- **#489 / PR #510**: LLM-extraction-vs-heuristic measurement. Extraction lost to raw ingestion on every arm. Currently unpublished; this plan promotes it to a headline transparency asset.
- **#484 / PR #509**: graph-traversal eval. Real-data LoCoMo results (R@1 regression) constrain how the synthetic 1.0-vs-0.0 result may be presented.

## Research

External research was performed by three adversarial critique agents (recorded in the [#511 alignment comment](https://github.com/tomcounsell/popoto/issues/511#issuecomment-5213135395)). Key findings that inform this plan:

- The vendor LoCoMo judged-accuracy "band" (52–92%) is largely one citation chain (Hindsight republishing Backboard's claimed numbers; ByteRover republishing Hindsight); Mem0's 66.88% has an open non-reproduction issue (~0.20 from the official script). → Publishing Popoto's honest 0.36 with CI and refusing the tabulated comparison is both principled and safe.
- Mem0's paper used gpt-4o-mini as generator — "different generator" is NOT a valid reason to decline comparison; the valid reasons are unnamed judge model, generous judge prompt (~10pt swing), unstated N, excluded category, and non-reproduction. Docs must use the valid reasons only.
- mem0ai installs 32 packages / 105 MB / requires an OpenAI key; popoto installs 3 packages / 7.9 MB / zero keys, verified in a clean venv. This is an unclaimed differentiator.
- MEMTIER (arXiv:2605.03675) reports hybrid-RRF retrieval at 96.7 ms/query on comparable hardware — the legitimate latency anchor. Its LoCoMo R@1 values are its own baselines and its authors call them uninformative → the "band" claim is killed, the latency anchor is kept.

## Data Flow

Reader journeys the restructured site must serve (this replaces a code data-flow trace; the change is content/structure):

1. **Memory-seeker (new primary):** search/README → homepage hero (SubconsciousMemory snippet + honest evidence line) → Adopt page (quickstart, benchmarked config) → benchmarks/method pages when skeptical.
2. **Evaluating skeptic:** homepage → Benchmarks → committed artifacts. Every number they can find in the repo JSON must match what the site says, including the unflattering ones.
3. **ORM-seeker (preserved):** homepage "built on a full Redis/Valkey ORM" → classic Getting Started/Core Features docs, intact.

## Appetite

**Size:** Medium (docs piece only; code/funnel/integration work is in sibling issues)

**Team:** Solo dev + documentarian/builder subagents; maintainer reviews copy at two checkpoints (post-hygiene, post-homepage).

**Interactions:**
- PM check-ins: 1–2 (homepage copy sign-off; claims-page sign-off)
- Review rounds: 1

## Prerequisites

No environment prerequisites — this is docs + mkdocs work. `mkdocs serve` requires the docs extras already in `.[dev]`.

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| mkdocs builds today | `mkdocs build --strict` | Baseline before restructuring |

## Solution

### Key Elements

- **Integrity pass (ships first, zero strategic risk):** fix false/stale statements, kill unsupportable claims, remove internal artifacts from the public nav, reconcile counts, fix broken links.
- **Claims layer:** one honest, reusable set of claim formulations (the reworded versions from the evidence critique), used consistently by homepage, overview, and benchmarks pages.
- **Homepage + nav restructure:** memory-first hero, three reader journeys, SubconsciousMemory first-class.
- **Golden-path alignment:** quickstart/recipe teach the benchmarked configuration; extraction documented as a measured opt-in; query-blind mode explained as a design choice with explicit "when this is right / wrong" framing.

### Flow

Homepage (SubconsciousMemory hero + one evidence line + "built on a full Redis ORM") → "Add memory to your agent" quickstart (benchmarked config, works with local Redis, zero keys) → SubconsciousMemory reference (first-class) → Primitives/Tuning (understand layer) → Benchmarks (method + full disclosure) → classic ORM docs (untouched journey).

### Technical Approach

**Phase A — Integrity fixes (no repositioning dependency; ship immediately):**
- `docs/benchmarks.md`: delete the false "No self-benchmarked judged numbers are committed" line; fix "sub-6-ms p50" → the actual 6.021 ms (or the curve phrasing); remove the stale all-zeros "Baseline Numbers (v1.6.3)" section; replace bare issue-number explanations with self-contained prose.
- **MEMTIER band claim lives in `docs/scripts/gen_benchmark_pages.py` (lines ~172, 190, 207, 363, 372-378), not in any `docs/*.md` file** (critique blocker 3). Kill the "band" framing there; keep the MEMTIER anchor (96.7 ms/query, regime context) per the `benchmark_docs_gen_vector_mode.md` doctrine. Update `tests/benchmarks/test_gen_benchmark_pages.py` in the same change — it currently asserts `"MEMTIER" in note` for framing notes containing bare decimals, so the test must be updated to the new framing, not deleted.
- `docs/features/llm-memory-extraction.md`: replace "not yet measured" with the #489 result and the do-not-enable-by-default warning the eval recommended.
- **Unpublish the 7 internal pages by moving them out of the rendered tree** (critique blocker 1: MkDocs renders every `.md` under `docs/` whether or not it is in `nav`; nav removal alone leaves pages live and search-indexed). Move to `docs/plans/` (build-excluded via `exclude_docs`): `guides/launch-announcements.md`, `features/kitchen-edge-case-demo.md`, `guides/research-prompt-memory-systems.md`, `guides/epistemic-flow-cognitive-agent-architectures.md`, `benchmarks/memory_lifecycle_baseline.md`, `guides/popoto-memory-roadmap.md`, `guides/programmable-memory-systems-neuroscience-design-spec.md` (spec may return later reframed as an essay). Remove their nav entries in the same change.
- **Fix all inbound links to the moved pages** (critique blocker 2 — these break `mkdocs build --strict` the moment the pages move): `recipes.md:584`, `features/README.md:17`, `features/confidence-field.md:275`, `features/agent-memory.md:1229,1851,1852,1853`, plus any others surfaced by the strict build. Replace each with a link to the surviving equivalent (per-primitive page, quickstart, or benchmarks) or drop the reference.
- Strip PR/issue numbers, sweep dates, and maintainer notes from user-facing pages and copy-paste snippets (`agent-memory.md` status table, quickstart/recipe `_wf_min_threshold` comments, `fields.md`, per-primitive pages). Exception: `guides/tuning-magic-numbers.md` keeps its sweep provenance — documenting tuning provenance is that page's purpose (critique finding 4). Strip the maintainer HTML comment from the quickstart — `test_default_recipe_wiring.py` does not reference it (critique nit 10a).
- **No version-history framing anywhere in user docs (maintainer direction 2026-08-07): state the status quo only.** Remove "was X before", "(default; was 0.2 before sweep …)", "Before this change …", "New in vN" / "since v1.x" callouts, "v1.4.4 Feature Demos"-style sections, "unchanged byte-for-byte from the original" compatibility notes, and any before/after change-log voice. Current behavior, stated plainly; the CHANGELOG is the only history surface. This applies to every page the plan touches and is a review criterion for every phase, not just Phase A.
- Fix the two links into the excluded `plans/` tree; reconcile primitive count site-wide to the canonical **14 primitives** (recipes and composed layers listed separately, never summed into the primitive count); fix the two "5 levels" occurrences → 6.
- `mkdocs.yml`: add `site_description`.
- Rewrite `docs/llms.txt` memory-first (critique finding 7: it is hand-maintained, opens ORM-first, and hardcodes a nav-shaped index — no generator owns it; `llms-full.txt` regenerates via `gen_llms_full.py` and needs no hand edits).

**Phase B — Claims layer (uses the evidence critique's exact reworded formulations):**
- LongMemEval-S vs agentmemory: keep, with the granularity disclosure in the same paragraph; R@1 0.894 is the headline figure; cite MemPalace 96.6% R@5 from the same source.
- Latency: publish the curve (3.0 ms @ 1k → 6.0 ms @ 20k, zero errors), scope it (in-process, lexical path; hybrid 41.5 ms, graph 22.1 ms), anchor against MEMTIER's 96.7 ms/query, never against hosted-service latencies.
- Judged accuracy: publish 0.3636 with n=77, CI ≈ 0.25–0.47, protocol details, and the git chronology (#456 predates the number); explicitly refuse the vendor-band tabulation with the valid reasons only.
- Graph traversal: capability framing with the real-data LoCoMo trade-off (R@5/R@10 up, R@1 down, 3.7× slower) beside the synthetic result.
- SIQ: harness and problem statement only; no Popoto score until #486/#487 land.
- Promote undersold assets: #489 negative result; 3-package/7.9 MB/zero-key install vs mem0ai's 32/105 MB/key-required; Valkey support; transparency practice (judge-prompt SHA, environment capture, published negative results).
- LoCoMo retrieval numbers: not amplified anywhere new until the scoring-defect re-run (sibling issue) completes; existing generated pages gain a short "under re-measurement" notice via `gen_benchmark_pages.py` framing text.
- Add the "composite mode is query-blind by design — when that's right and when it's wrong" explainer, linked from the CSR results page and the SubconsciousMemory reference.

**Phase C — Homepage + nav:**
- `docs/index.md`: SubconsciousMemory hero snippet (benchmarked config), one evidence line (R@1 0.894 LongMemEval-S + latency curve + install weight), "built on a full Redis/Valkey ORM you'd want anyway," Restaurant demoted into Getting Started. Production-uses list updated or cut.
- Nav: `Home → Add Memory to Your Agent (quickstart + SubconsciousMemory) → Understand & Tune (primitives, composite scoring, tuning) → Benchmarks & Method → Redis ORM (classic docs) → API Reference`. SubconsciousMemory leaves "Recipes"; remaining recipes stay grouped under the understand layer.
- `features/agent-memory.md`: cut to a ~1,500-word orientation page (what the system is, how the pieces compose, one diagram, links) — per-primitive pages become the single source of truth; no duplicated parameter tables.

**Phase D — Golden path:**
- Quickstart restructured so Level 1 is query-sensitive (BM25Field from the first model) and the progression ends at the benchmarked SubconsciousMemory configuration; extraction appears as a measured opt-in with the #489 numbers; Level 6 no longer silently regresses (embedding added alongside BM25, not instead of it).
- Recipe page teaches raw-turn ingestion as the measured-best write path; heuristic extraction documented with its 0.21-vs-0.36 cost.
- Where the batteries-included default model (sibling code issue) is not yet shipped, the docs show the full model with a "this will become an import" note; docs update to the import once it ships.

## Failure Path Test Strategy

No exception handlers in scope — this is documentation work. The failure paths are build-time and claim-integrity:
- `mkdocs build --strict` gates broken links/nav references.
- `tests/test_default_recipe_wiring.py` parses the quickstart; restructuring must keep it passing (or update it deliberately in the same PR).
- Claim integrity is checked by the Verification table greps (no killed claims reintroduced).

## Test Impact

- [ ] `tests/test_default_recipe_wiring.py` — UPDATE if quickstart level structure changes (the test hard-anchors on `^## Level 5`, requires `ContextAssembler` in the Level 5 body, and ≥2 `class Memory` definitions before it).
- [ ] `tests/benchmarks/test_gen_benchmark_pages.py` — UPDATE: asserts `"MEMTIER" in note` for framing notes with bare decimals; must be updated alongside the Phase B framing change (critique blocker 3 corrected the earlier claim that no other tests parse docs content).

## Rabbit Holes

- **Rewriting the neuroscience spec as a public essay now.** Removal from nav is the deliverable; the reframe is future work.
- **Redesigning `gen_benchmark_pages.py`.** Only framing-text edits are in scope; the Spec/artifact machinery stays.
- **Perfecting the three-journey IA.** Memory-first ordering with the ORM docs intact is the bar; don't spend days on nav taxonomy for a site with current traffic of ~6 visitors/fortnight — the structure exists to serve the funnel work in the sibling issues.
- **Waiting on benchmark re-runs to ship Phases A/C.** Only LoCoMo amplification waits; everything else ships now.
- **Publishing judged accuracy with ever-more caveats.** Use the evidence critique's formulation verbatim-ish and stop; caveat-stacking reads as insecurity.

## Risks

### Risk 1: Repositioned site points traffic at a mode the site's own CSR page flags as query-blind
**Impact:** A skeptic reads the flagship pitch, clicks Benchmarks, finds the query-blindness detector firing on composite mode, concludes bad faith.
**Mitigation:** Phase B's "query-blind by design" explainer ships in the same PR as the homepage change, linked from both pages.

### Risk 2: Docs promise a config the API makes hard (no default model, silent composite mode) until sibling code work ships
**Impact:** Expectation-violation bounce; the exact failure critic-adoption measured (caesar salad above the answer).
**Mitigation:** Sequencing — quickstart's first model includes BM25Field so the copy-paste path is query-sensitive today; the "becomes an import" note manages the boilerplate honestly; sibling issues are linked from the docs where relevant.

**Status (#513 shipped):** the import now exists. `from popoto.recipes import DefaultMemory, SubconsciousMemory` gives the benchmarked configuration with no schema authoring, `score_weights` defaults to `{"relevance": 1.0}`, and `retrieval_mode='auto'` falling through to composite logs a `WARNING` naming the missing `BM25Field`. The quickstart opens with a "Level 0: Import the defaults" section (six lines to a working memory loop) and the SubconsciousMemory recipe leads with the zero-argument constructor. This plan's golden-path copy should reference the import rather than a hand-authored schema.

### Risk 3: Claims drift back in via future benchmark artifact updates
**Impact:** Killed claims (sub-6ms, band, 1.0-vs-0.0) reappear as artifacts regenerate pages.
**Mitigation:** Kill-list greps in the Verification table; framing text lives in `gen_benchmark_pages.py` under version control.

## Race Conditions

No race conditions identified — documentation-only change; the docs build is single-process.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #512] PyPI/README/metadata repositioning (tagline, keywords, dead PyPI links, empty homepage URL).
- [SEPARATE-SLUG #513 — SHIPPED] Code fixes: query-blind resolution warning, batteries-included `DefaultMemory` model with benchmarked `score_weights` default, content-first injected-context format, removal of the false PydanticAI-guide claim in `src/popoto/recipes/subconscious_memory.py`. See the Risk 2 status note above for what the golden-path copy can now assume.
- [SEPARATE-SLUG #514] LoCoMo gold-aware ID-selection scoring defect (`tests/benchmarks/scenarios/external_base.py:578-591`) fix + full re-run; broken `*_latest` symlinks. This plan's LoCoMo constraint depends on it.
- [SEPARATE-SLUG #515] Harness integration: make SubconsciousMemory easily added to Claude Code, Codex, Hermes, and OpenClaw agents (MCP-server surface + per-harness wiring guides). Maintainer direction 2026-08-07: no PydanticAI dependency; target agent harnesses, not Python frameworks.
- [EXTERNAL] Redis-partnership positioning conversation (Mirko Ortensi dialogue) — maintainer-owned relationship decision; this plan's copy avoids foreclosing it but does not manage it.

## Update System

No update system changes — docs deploy via the existing `deploy-docs` skill / GitHub Pages flow.

## Agent Integration

No agent integration in this plan; the harness-integration sibling issue owns that surface.

## Documentation

This plan IS documentation work. Meta-docs:
- [ ] `docs/features/README.md` index updated to match the new nav and single primitive count.
- [ ] `llms.txt` / `llms-full.txt` regeneration verified post-restructure (`gen_llms_full.py`).

## Success Criteria

- [ ] All #511 acceptance criteria pass (homepage hero, first-class SubconsciousMemory, three journeys, artifact removal, no PR-numbers/sweep-dates in user copy, deduped overview, golden-path alignment, claims traceability, `mkdocs build --strict`)
- [ ] Kill-list absent site-wide: "sub-6ms", MEMTIER band claim, SIQ score presentation, standalone graph "1.0 vs 0.0"
- [ ] Judged 0.36 published with CI, protocol, chronology; no tabulation against vendor accuracies
- [ ] agentmemory comparison carries the granularity disclosure in the same section
- [ ] #489 negative result published as a named transparency asset
- [ ] One primitive count site-wide; quickstart level count correct
- [ ] `pytest -k default_recipe_wiring` passes
- [ ] Maintainer sign-off on homepage and claims copy

## Team Orchestration

- **Builder (integrity + claims)** — Name: docs-integrity-builder — Role: Phases A+B edits — Agent Type: builder — Resume: true
- **Builder (homepage + nav + golden path)** — Name: docs-structure-builder — Role: Phases C+D — Agent Type: builder — Resume: true
- **Validator (claims audit)** — Name: claims-validator — Role: verify every number on the changed pages against committed artifacts; run kill-list greps — Agent Type: validator — Resume: true
- **Validator (build + journeys)** — Name: site-validator — Role: `mkdocs build --strict`, nav walk-through of the three journeys, recipe-wiring test — Agent Type: validator — Resume: true

## Step by Step Tasks

### 1. Integrity fixes (Phase A)
- **Task ID**: build-integrity
- **Depends On**: none
- **Validates**: `mkdocs build --strict`; kill-list greps
- **Assigned To**: docs-integrity-builder
- **Agent Type**: builder
- **Parallel**: true
- Execute every Phase A bullet; commit as one reviewable PR-sized change

### 2. Claims layer (Phase B)
- **Task ID**: build-claims
- **Depends On**: build-integrity
- **Validates**: `mkdocs build --strict`; `pytest tests/benchmarks/test_gen_benchmark_pages.py -q`; kill-list greps
- **Assigned To**: docs-integrity-builder
- **Agent Type**: builder
- **Parallel**: false
- Write the claims formulations into `benchmarks.md` and the new query-blind explainer; edit `gen_benchmark_pages.py` framing text + its test. Does NOT touch `features/agent-memory.md` — that file is owned exclusively by Task 4 (critique finding 5).

### 3. Validate claims against artifacts
- **Task ID**: validate-claims
- **Depends On**: build-claims
- **Assigned To**: claims-validator
- **Agent Type**: validator
- **Parallel**: false
- Cross-check every number on changed pages against `tests/benchmarks/results/` JSON; run kill-list greps; report pass/fail per claim

### 4. Homepage, nav, overview restructure (Phase C)
- **Task ID**: build-structure
- **Depends On**: build-integrity, build-claims
- **Validates**: `mkdocs build --strict`; internal-pages-unpublished check; primitive-count and level-count greps
- **Assigned To**: docs-structure-builder
- **Agent Type**: builder
- **Parallel**: false
- Rewrite index.md, restructure mkdocs.yml nav, cut agent-memory.md to orientation page (sole owner of that file — sequenced after build-claims to avoid the two-writers conflict, critique finding 5); rewrite `docs/llms.txt` memory-first

### 5. Golden path alignment (Phase D)
- **Task ID**: build-golden-path
- **Depends On**: build-structure, build-claims
- **Validates**: `pytest tests/test_default_recipe_wiring.py -q`; `mkdocs build --strict`
- **Assigned To**: docs-structure-builder
- **Agent Type**: builder
- **Parallel**: false
- Restructure quickstart and recipe per Phase D; update `test_default_recipe_wiring.py` deliberately in the same change (it anchors on `^## Level 5`, `ContextAssembler` in the L5 body, and ≥2 `class Memory` definitions)

### 6. Final validation
- **Task ID**: validate-all
- **Depends On**: all
- **Assigned To**: site-validator
- **Agent Type**: validator
- **Parallel**: false
- `mkdocs build --strict`; walk the three journeys; `pytest -k default_recipe_wiring`; verify all success criteria; report

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Strict build | `mkdocs build --strict` | exit code 0 |
| Recipe wiring test | `pytest -k default_recipe_wiring -q` | exit code 0 |
| Gen-pages test | `pytest tests/benchmarks/test_gen_benchmark_pages.py -q` | exit code 0 |
| No sub-6ms claim | `grep -ri "sub-6" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| No sweep dates in user docs | `grep -rn "sweep 2026" docs/ --include="*.md" \| grep -v -e plans/ -e tuning-magic-numbers.md \| wc -l` | match count == 0 |
| No PR links in overview | `grep -c "pull/" docs/features/agent-memory.md` | match count == 0 |
| Judged number published | `grep -r "0.3636\|0.36 " docs/benchmarks.md \| wc -l` | output > 0 |
| Granularity disclosure present | `grep -c "granularity" docs/benchmarks.md` | output > 0 |
| False claim gone | `grep -c "No self-benchmarked judged numbers" docs/benchmarks.md` | match count == 0 |
| Extraction result published | `grep -c "not yet measured" docs/features/llm-memory-extraction.md` | match count == 0 |
| Internal pages out of nav | `grep -c "launch-announcements\|kitchen-edge-case\|research-prompt\|epistemic-flow\|memory_lifecycle_baseline" mkdocs.yml` | match count == 0 |
| Internal pages unpublished | `ls docs/guides/launch-announcements.md docs/features/kitchen-edge-case-demo.md docs/guides/research-prompt-memory-systems.md docs/guides/epistemic-flow-cognitive-agent-architectures.md docs/benchmarks/memory_lifecycle_baseline.md docs/guides/popoto-memory-roadmap.md docs/guides/programmable-memory-systems-neuroscience-design-spec.md 2>/dev/null \| wc -l` | match count == 0 |
| MEMTIER band killed | `grep -c "0.10–0.30\|0.10-0.30" docs/scripts/gen_benchmark_pages.py` | match count == 0 |
| No SIQ/graph score theater | `grep -rin "1.0 vs 0.0\|1.0-vs-0.0" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| No vendor-band tabulation | `grep -rn "52–92\|52-92" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| Canonical primitive count (14) | `grep -rn "12 primitives\|21 primitives" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| Level count fixed | `grep -rn "5 levels\|five levels\|5-level" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| No version-history framing | `grep -rn "was 0\.\|Before this change\|since v1\.\|New in v\|v1\.4\.4\|v1\.6\.3" docs/ --include="*.md" \| grep -v -e plans/ -e tuning-magic-numbers.md \| wc -l` | match count == 0 |
| Site description set | `grep -c "site_description" mkdocs.yml` | output > 0 |

## Critique Results

`/do-plan-critique` run 2026-08-07 against `63cf035`. Depth: FULL (3 critics —
Risk & Robustness, Scope & Value, History & Consistency) plus automated
structural checks. **Verdict: NEEDS REVISION** (3 blockers, 5 concerns, 2 nits).
Premise-level critique by three adversarial reviewers is separate and remains
recorded in the [#511 alignment comment](https://github.com/tomcounsell/popoto/issues/511#issuecomment-5213135395).

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | Scope & Value + Risk & Robustness (both) | Removing the seven internal pages from `nav:` does not take them off the public surface. MkDocs builds and publishes every `.md` under `docs/` regardless of nav membership; only `exclude_docs`, deletion, or a move drops a page. The pages stay live at their URLs and stay in the `search` plugin index, while the Verification row `grep -c "launch-announcements\|..." mkdocs.yml == 0` passes. Verification confirms the goal is unmet. | Phase A + Verification | `git mv` the seven files under `docs/plans/` (already in `exclude_docs`) or add an `internal/` path to `exclude_docs` in `mkdocs.yml`. Then add a row distinct from the nav grep, e.g. `ls docs/guides/launch-announcements.md docs/features/kitchen-edge-case-demo.md 2>/dev/null \| wc -l` == 0. |
| BLOCKER | Scope & Value + Risk & Robustness (both) | Second-order cost of the above, unbudgeted: seven inbound links from pages that stay published point at removal-list pages. Excluding/moving the targets makes them dangling and fails Task 1's own `mkdocs build --strict` gate. Verified: `docs/recipes.md:584`, `docs/features/README.md:17`, `docs/features/confidence-field.md:275`, `docs/features/agent-memory.md:1229/1851/1852/1853`. Phase A lists only "the two links into the excluded `plans/` tree". | Phase A (task `build-integrity`) | Fix all seven in the same commit as the exclusion. Note `agent-memory.md:1851-1853` sit in a "further reading" list that Phase C deletes anyway — but Phase A is declared shippable alone ("ship immediately"), so the fix must land in Phase A, not be deferred to C. |
| BLOCKER | History & Consistency + Risk & Robustness (both) | Test Impact's "No other tests parse docs content" is false, and the MEMTIER edit targets the wrong file. `grep -rin memtier docs/ --include="*.md"` returns zero hits outside `plans/` — the band claim lives only in `docs/scripts/gen_benchmark_pages.py:172,190,207,363,372-378`, not `docs/benchmarks.md`. `tests/benchmarks/test_gen_benchmark_pages.py` (193 lines) asserts on that framing text, including `assert "MEMTIER" in note` for any note containing a bare decimal. | Phase A/B retarget + Test Impact | Retarget the Phase A bullet to `docs/scripts/gen_benchmark_pages.py:372-378`. Add `tests/benchmarks/test_gen_benchmark_pages.py` to Test Impact as UPDATE. Run `pytest tests/benchmarks/test_gen_benchmark_pages.py -q` as Task 2 validation: any replacement note carrying a bare decimal without the string `MEMTIER` trips the assertion at ~lines 120-139. |
| CONCERN | Scope & Value | The sweep-date Verification row is unsatisfiable without destroying content. `grep -rn "sweep 2026" docs/ --include="*.md" \| grep -v plans/` returns 20 hits across 7 files; 6 are in `docs/guides/tuning-magic-numbers.md`, a page whose entire purpose is documenting empirical tuning provenance. Expecting 0 forces deletion of load-bearing sourcing. | Phase A + Verification | Change the command to `grep -rn "sweep 2026" docs/ --include="*.md" \| grep -v -e plans/ -e tuning-magic-numbers.md \| wc -l`, and add a carve-out to the Technical Approach bullet naming the in-scope files (`fields.md`, quickstart/recipe comments, per-primitive pages) and the retained page. |
| CONCERN | Risk & Robustness | Tasks 2 (`build-claims`) and 4 (`build-structure`) are marked `Parallel: true` with each other but both edit `docs/features/agent-memory.md` (86 KB): Task 2 writes claims into "overview", Task 4 cuts the same file to ~1,500 words. Two different builders, one file, opposite directions. | Team Orchestration / Tasks | Either drop "overview" from Task 2's file scope (claims land in `benchmarks.md` + the query-blind explainer only), or make Task 4 depend on Task 2 and remove `Parallel: true`. If both branches touch the file, rebase rather than merge — a silent last-writer-wins discards one builder's work. |
| CONCERN | History & Consistency + structural check | Verification covers 3 of 8 Success Criteria. No row exists for: MEMTIER band, SIQ score presentation, standalone graph "1.0 vs 0.0", the agentmemory granularity disclosure, "no tabulation against vendor accuracies", one-primitive-count-site-wide, or quickstart level count. All can silently remain while every listed check passes. | Verification | Add rows targeting the real storage locations (including `docs/scripts/gen_benchmark_pages.py`, not just `docs/**/*.md`): `grep -c "0.10–0.30" docs/scripts/gen_benchmark_pages.py` == 0; a check for "1.0 vs 0.0"/"1.0-vs-0.0"; a primitive-count check once the canonical number is chosen. |
| CONCERN | Scope & Value | `docs/llms.txt` is hand-maintained — no script generates it (`gen_llms_full.py` produces only `llms-full.txt`). It opens ORM-first ("Python Redis/Valkey ORM with Django-like syntax…") and hardcodes a nav-shaped link index that goes stale on restructure. No phase owns it, and the Documentation checklist conflates it with the auto-generated file. For an "adoption at scale" north star this is the file an evaluating agent reads first. | Phase C + Documentation | Add an explicit Phase C task to hand-edit `docs/llms.txt`'s opening description and section links to the memory-first framing, separate from the `gen_llms_full.py` regen check. Note the Phase A bullet "strip it from `llms.txt` generation if feasible" is a non-op — there is no generation step to strip. |
| CONCERN | History & Consistency | The Freshness Check names two `docs/plans/` docs as benchmark-framing doctrine constraints but omits a third that also mandates the MEMTIER anchor: `docs/plans/benchmark_docs_gen_vector_mode.md:171,198,286`. The plan reverses doctrine shipped as an acceptance criterion of #453/PR #466 (`benchmark_results_docs_publishing.md:147,202`) without recording the full blast radius. | Freshness Check / Prior Art | `grep -n "MEMTIER" docs/plans/benchmark_docs_gen_vector_mode.md` before editing `gen_benchmark_pages.py`; reconcile or explicitly supersede all three doctrine references in the same PR. |
| CONCERN | Structural check | Only Task 1 carries a `Validates` field. Tasks 2, 4, and 5 have none — including Task 5 (`build-golden-path`), the one task that can break `tests/test_default_recipe_wiring.py`. | Step by Step Tasks | Add `Validates` to Task 2 (`pytest tests/benchmarks/test_gen_benchmark_pages.py -q`), Task 4 (`mkdocs build --strict`), Task 5 (`pytest -k default_recipe_wiring -q`). The wiring test hard-anchors on `re.match(r"^## Level 5\b", ...)`, requires `ContextAssembler` in the Level 5 body, and requires >= 2 `class Memory` definitions before Level 5 — a Phase D renumber breaks the extractor, not just an assertion. |
| NIT | Structural check | Phase A's conditional "Keep the invisible maintainer HTML comment in the quickstart only if `test_default_recipe_wiring.py` requires it" is already resolvable: the test contains no reference to any HTML comment. The conditional resolves to "strip it". | Phase A | — |
| NIT | Structural check | Phase A says fix "5 levels" → 6; only two occurrences exist (`docs/guides/agent-memory-quickstart.md:10`, `docs/features/agent-memory.md:3`), not the three the issue reports. Live primitive counts are 12 (`agent-memory.md:1586`) and 14 (`quickstart:14,344`); the plan does not say which becomes canonical. | Phase A | — |

**Prerequisite status:** `mkdocs build --strict` could NOT be executed — this worktree has no venv with mkdocs installed (`python -c "import mkdocs"` → ModuleNotFoundError). The baseline gate is UNVERIFIED and must be run before Phase A begins.

---

## Resolved Questions (maintainer, 2026-08-07)

1. **Homepage evidence line:** the trio as drafted — LongMemEval-S R@1 0.894, the latency curve (3.0 ms @1k → 6.0 ms @20k), and the 3-package/zero-API-key install. Matches the README hero shipped in PR #524.
2. **Judged-accuracy placement:** Benchmarks section only, with CI, protocol, and chronology. The homepage transparency line links to Benchmarks generally, never deep-links the number. Ships only after the #514 re-run.
3. **Removed pages:** clean removal, no stubs, no redirect plugin. All seven URLs 404; the files remain in the repo under `docs/plans/`.

## Build Notes (2026-08-07)

Deviations from the plan as written, and why:

1. **Judged 0.36 shipped in this PR.** Resolved Question 2 gated it on the #514
   re-run, which merged as PR #528 the same day. The correction changed how
   retrieved turns collapse to *result IDs for recall scoring*; the judged stage
   consumes retrieved memory **text** in rank order, so `judged_accuracy` is
   untouched by the defect. The Benchmarks section says exactly that, and labels
   the retrieval summary co-reported inside the judged artifact as
   pre-correction, pointing at #530. The pre-correction hybrid/graph/judged
   retrieval arms are not amplified anywhere.
2. **Sampling scope disclosed alongside the judged number.** The committed
   judged artifacts are a 100-question stratified sample of a derived
   two-dialogue LoCoMo subset (conv-26, conv-30; 788 turns, 304 QA), not the
   full 1986. The plan did not name this; publishing the CI without it would
   have understated the uncertainty.
3. **Extraction mechanism corrected.** The plan attributed the loss to the
   extraction prompt discarding evidence. That holds for the three Claude arms
   (accuracy falls monotonically with turn-drop rate) but not for the heuristic
   arm, which drops 0.3% of turns and still loses 16 points by fragmenting each
   turn into ~3 sentences. Both mechanisms are documented.
4. **Four primitives relocated rather than deleted.** Cutting
   `features/agent-memory.md` to an orientation page would have destroyed the
   only reference for AccessTrackerMixin, EventStreamMixin, TagField, and
   StreamConsumer, none of which has a per-primitive page. They moved to
   `fields.md` (fields and mixins) and `recipes.md` (StreamConsumer), and the
   InteractionWeight lifetime table moved to `decaying-sorted-field.md`.
5. **Canonical count of 14 defined explicitly.** 19 rows existed in the status
   table. The overview now lists 14 primitives and 7 composed layers
   (ContextAssembler, Hybrid Retrieval, SubconsciousMemory, MemoryLifecycle,
   PolicyCache, StreamConsumer, Metacognitive Layer) in a separate table, never
   summed.
6. **Wiring test tightened, not just kept passing.** Level 1 now declares
   `BM25Field`, so `test_default_recipe_wiring.py` asserts it on **every**
   quickstart model rather than from the second onward.
7. **Em dashes removed from newly authored prose**, matching the memory-first
   README shipped in PR #524. Pre-existing pages keep their own punctuation;
   this was not a repo-wide sweep.
8. **Query-blind explainer is a new page**
   (`docs/guides/query-blind-retrieval.md`), linked from the generated
   benchmarks Overview, the SubconsciousMemory recipe, the quickstart, and the
   agent-memory overview.
