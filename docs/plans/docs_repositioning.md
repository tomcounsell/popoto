---
status: Planning
type: chore
appetite: Medium
owner: Tom Counsell
created: 2026-08-07
tracking: https://github.com/tomcounsell/popoto/issues/511
last_comment_id: 5213135395
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
- Active plans overlap check: `docs/plans/` contains benchmark-strategy plans (`benchmarking_strategy_2026-07.md`, `benchmark_results_docs_publishing.md`) that this plan treats as constraints, not conflicts — the benchmark-framing doctrine in `docs/scripts/gen_benchmark_pages.py` is preserved.

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
- `docs/benchmarks.md`: delete the false "No self-benchmarked judged numbers are committed" line; fix "sub-6-ms p50" → the actual 6.021 ms (or the curve phrasing); remove the stale all-zeros "Baseline Numbers (v1.6.3)" section; kill the MEMTIER "band" claim (keep MEMTIER's 96.7 ms/query as the latency anchor); replace bare issue-number explanations with self-contained prose.
- `docs/features/llm-memory-extraction.md`: replace "not yet measured" with the #489 result and the do-not-enable-by-default warning the eval recommended.
- Remove from nav (stay in repo or `docs/plans/`): `guides/launch-announcements.md`, `features/kitchen-edge-case-demo.md`, `guides/research-prompt-memory-systems.md`, `guides/epistemic-flow-cognitive-agent-architectures.md`, `benchmarks/memory_lifecycle_baseline.md`, `guides/popoto-memory-roadmap.md`, `guides/programmable-memory-systems-neuroscience-design-spec.md` (spec may return later reframed as an essay).
- Strip PR/issue numbers, sweep dates, and maintainer notes from user-facing pages and copy-paste snippets (`agent-memory.md` status table, quickstart/recipe `_wf_min_threshold` comments, `fields.md`, per-primitive pages). Keep the invisible maintainer HTML comment in the quickstart only if `test_default_recipe_wiring.py` requires it, and strip it from `llms.txt` generation if feasible.
- Fix the two links into the excluded `plans/` tree; reconcile primitive count to one number site-wide; fix "5 levels" → 6.
- `mkdocs.yml`: add `site_description`.

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

- [ ] `tests/test_default_recipe_wiring.py` — UPDATE if quickstart level structure changes (the test parses heading structure and `content_bm25` presence by design).
- No other tests parse docs content.

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

### Risk 3: Claims drift back in via future benchmark artifact updates
**Impact:** Killed claims (sub-6ms, band, 1.0-vs-0.0) reappear as artifacts regenerate pages.
**Mitigation:** Kill-list greps in the Verification table; framing text lives in `gen_benchmark_pages.py` under version control.

## Race Conditions

No race conditions identified — documentation-only change; the docs build is single-process.

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #512] PyPI/README/metadata repositioning (tagline, keywords, dead PyPI links, empty homepage URL).
- [SEPARATE-SLUG #513] Code fixes: query-blind resolution warning, batteries-included default `Memory` model with benchmarked `score_weights` default, injected-context format (UUIDs/epoch floats at 2.8× overhead), removal of the false PydanticAI-guide claim at `src/popoto/recipes/subconscious_memory.py:29`.
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
- **Assigned To**: docs-integrity-builder
- **Agent Type**: builder
- **Parallel**: false
- Write the claims formulations into benchmarks.md, overview, and the new query-blind explainer; edit `gen_benchmark_pages.py` framing text only

### 3. Validate claims against artifacts
- **Task ID**: validate-claims
- **Depends On**: build-claims
- **Assigned To**: claims-validator
- **Agent Type**: validator
- **Parallel**: false
- Cross-check every number on changed pages against `tests/benchmarks/results/` JSON; run kill-list greps; report pass/fail per claim

### 4. Homepage, nav, overview restructure (Phase C)
- **Task ID**: build-structure
- **Depends On**: build-integrity
- **Assigned To**: docs-structure-builder
- **Agent Type**: builder
- **Parallel**: true (with build-claims)
- Rewrite index.md, restructure mkdocs.yml nav, cut agent-memory.md to orientation page

### 5. Golden path alignment (Phase D)
- **Task ID**: build-golden-path
- **Depends On**: build-structure, build-claims
- **Assigned To**: docs-structure-builder
- **Agent Type**: builder
- **Parallel**: false
- Restructure quickstart and recipe per Phase D; keep/update `test_default_recipe_wiring.py`

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
| No sub-6ms claim | `grep -ri "sub-6" docs/ --include="*.md" \| grep -v plans/ \| wc -l` | match count == 0 |
| No sweep dates in user docs | `grep -rn "sweep 2026" docs/ --include="*.md" \| grep -v "plans/" \| wc -l` | match count == 0 |
| No PR links in overview | `grep -c "pull/" docs/features/agent-memory.md` | match count == 0 |
| Judged number published | `grep -r "0.3636\|0.36 " docs/benchmarks.md \| wc -l` | output > 0 |
| False claim gone | `grep -c "No self-benchmarked judged numbers" docs/benchmarks.md` | match count == 0 |
| Extraction result published | `grep -c "not yet measured" docs/features/llm-memory-extraction.md` | match count == 0 |
| Internal pages out of nav | `grep -c "launch-announcements\|kitchen-edge-case\|research-prompt\|epistemic-flow\|memory_lifecycle_baseline" mkdocs.yml` | match count == 0 |
| Site description set | `grep -c "site_description" mkdocs.yml` | output > 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| | | *(Pre-plan critique was run at the premise level by three adversarial reviewers — see the [#511 alignment comment](https://github.com/tomcounsell/popoto/issues/511#issuecomment-5213135395). /do-plan-critique of this document not yet run.)* | | |

---

## Open Questions

1. **Homepage evidence line copy** — the hero's one-line proof will cite R@1 0.894 (LongMemEval-S), the latency curve, and the 3-package/zero-key install. Sign off on that exact trio, or prefer a different lead stat?
2. **Judged-accuracy placement** — Benchmarks section only (recommended), or also linked from the homepage's transparency line? Homepage linkage is braver and more on-brand for the moat, but it puts 0.36 one click from the hero.
3. **Roadmap memo + neuroscience spec** — both leave the nav now; confirm neither needs a public stub/redirect (external links may point at them).
