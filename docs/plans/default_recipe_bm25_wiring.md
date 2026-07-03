---
status: Ready
type: chore
appetite: Small
owner: Claude (SDLC pipeline, issue #445)
created: 2026-07-03
tracking: https://github.com/tomcounsell/popoto/issues/445
last_comment_id: none
---

# Default Recipe BM25 Wiring Check (query-sensitivity regression guard)

## Problem

The CSR harness (#418, `tests/benchmarks/csr/`) validates the ContextAssembler
mode-resolution *mechanism* with per-case model classes that hardcode
`BM25Field` — lexical mode is guaranteed by construction. That was an explicit
scope cut in `docs/plans/deterministic_eval_harness.md` (No-Gos), with this
follow-up mandated at ship time.

Nothing in the test suite binds the *shipped default recipe* — the model users
are steered toward by `docs/guides/agent-memory-quickstart.md` and the
ContextAssembler docs — to a query-sensitive retrieval mode. A #409-class
regression (removing or misconfiguring `BM25Field` on the documented default
recipe) would leave CSR green while production retrieval silently reverts to
the query-blind `composite` path (`_pull_path_composite` never reads query
text for ranking; the June audit measured P@10 ≈ random for that path).

**This drift is not hypothetical.** The existing mirror tests have already
diverged from the docs: the quickstart's Level 2–4 `Memory` models declare
`content_bm25 = BM25Field(source="content")`, but the hand-copied mirrors in
`tests/test_guide_examples.py` (`GuideL2Memory`, `GuideL3Memory`,
`GuideL4Memory`, `GuideL5Memory`, `GuideCAMemory`) and
`tests/test_adoption_ladder.py` (`AttentionMemory` et al.) do **not** declare
any `BM25Field`. Hand-copied mirrors cannot guard doc-level recipe wiring.

**Current behavior:** No test fails if `BM25Field` is dropped from the
quickstart recipe. The suite would stay green while every user following the
docs gets query-blind retrieval from `ContextAssembler(retrieval_mode="auto")`.

**Desired outcome:** A narrow unit test that parses the actual quickstart doc,
materializes the recipe model users end up with at the ContextAssembler step
(Level 5), and asserts the resolved effective mode is query-sensitive
(`lexical` or `hybrid`), never `composite`.

## Freshness Check

**Baseline commit:** `2d7f31f` (main HEAD at plan time)
**Issue filed at:** 2026-07-03T09:56:50Z (same day as planning)
**Disposition:** Unchanged

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:946-954` — `"auto"` resolution:
  BM25+Embedding → `hybrid`; BM25 only → `lexical`; neither → `composite`. Confirmed.
- `src/popoto/recipes/context_assembler.py:1180` — `_pull_path_composite` exists
  and takes `query_cues` only for filtering, not ranking. Confirmed.
- `docs/guides/agent-memory-quickstart.md:56,104,145` — Level 2/3/4 `Memory`
  models each declare `content_bm25 = BM25Field(source="content")`. Confirmed.
- `docs/plans/deterministic_eval_harness.md:592` — No-Gos section mandating this
  follow-up. Confirmed.

**Cited sibling issues/PRs re-checked:**
- #418 — closed, shipped via PR #444 (CSR harness). Landscape unchanged since.
- #409 — closed via PR #426 (BM25 first-class retrieval mode + recipe default).
- #446 — open, parallel pipeline touching `src/popoto/fields/bm25_field.py`
  (source only; this plan never edits that file).

**Commits on main since issue was filed (touching referenced files):** none.

**Active plans in `docs/plans/` overlapping this area:**
`deterministic_eval_harness.md` is Complete; its No-Gos mandate exactly this
work. No active overlap.

## Prior Art

- **PR #400**: Defaulted ContextAssembler to hybrid retrieval (BM25 + vector +
  graph via RRF) — established the pull-path modes.
- **PR #426 (issue #409)**: Made BM25 a first-class retrieval mode and recipe
  default; added the quickstart's `content_bm25` declarations and the
  `auto → lexical` resolution for BM25-only models. This plan guards that
  wiring against regression.
- **PR #444 (issue #418)**: CSR harness; its No-Gos explicitly deferred the
  default-recipe wiring check to this issue.
- **`tests/test_guide_examples.py`**: prior art for doc-mirroring tests — and
  the demonstration of why mirroring by hand is insufficient (already drifted;
  see Problem).

## Research

No relevant external findings — purely internal work (stdlib `ast` + existing
popoto APIs); proceeding with codebase context.

## Spike Results

### spike-1: Doc-parsing + exec + mode resolution works end-to-end
- **Assumption**: "The quickstart's fenced python blocks can be parsed with
  `ast`, filtered to Import/ImportFrom/ClassDef nodes, exec'd cumulatively, and
  the resulting `Memory` class drives ContextAssembler auto-resolution."
- **Method**: prototype (scratchpad script against the real doc, DB 13)
- **Finding**: 5 python blocks precede the Level 5 heading; 4 define
  `class Memory` (Levels 1–4). Level 1 has no BM25Field; Levels 2–4 each have
  `content_bm25`. The last pre-Level-5 recipe resolves to
  `_effective_mode == "lexical"`. `ContextAssembler.__init__` requires
  `score_weights` positionally — pass the quickstart's own
  `{"relevance": 0.6, "confidence": 0.3}`.
- **Confidence**: high
- **Impact on plan**: design confirmed exactly as specified in Technical
  Approach; no fallback needed.

## Data Flow

1. **Entry point**: pytest collects `tests/test_default_recipe_wiring.py`.
2. **Doc parser**: reads `docs/guides/agent-memory-quickstart.md`, splits on
   `## ` headings, collects fenced ```python blocks that precede the Level 5
   (ContextAssembler) section.
3. **AST filter**: per block, keeps only `Import`/`ImportFrom`/`ClassDef`
   nodes (no doc runtime statements execute — no saves, no LLM/embedding
   calls), exec's them cumulatively in one namespace, recording each
   successive `Memory` class definition.
4. **Assertion layer**: the recipe in effect at Level 5 (last `Memory` defined
   before the ContextAssembler section) is checked for a declared `BM25Field`
   and passed to `ContextAssembler(model_class=..., score_weights=...)` with
   default `retrieval_mode="auto"`.
5. **Output**: assertions on `_effective_mode` — must be in
   `{"lexical", "hybrid"}` and explicitly `!= "composite"`.

## Architectural Impact

None at runtime — test-only. New test depends only on stdlib (`ast`, `re`,
`pathlib`) and public popoto imports (`ContextAssembler`, `BM25Field` from the
top-level package, per the quickstart's own import cheat sheet). It reads but
never edits `src/popoto/fields/bm25_field.py`, so no conflict surface with the
parallel #446 pipeline.

## Appetite

**Size:** Small

**Team:** Solo dev (single agent pipeline)

**Interactions:**
- PM check-ins: 0 (scope fixed by issue #445 acceptance criteria)
- Review rounds: 1 (do-pr-review)

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis on localhost:6379 | `redis-cli ping` | pytest plugin flushes the test DB |
| POPOTO_TEST_DB=13 for all runs | `echo $POPOTO_TEST_DB` | avoid clashing with the parallel #446 pipeline on DB 15 |

## Solution

### Key Elements

- **Quickstart recipe extractor** (test-local helper): pulls the successive
  `Memory` class definitions out of the quickstart's python blocks, in
  document order, stopping at the Level 5 (ContextAssembler) heading. Because
  it reads the shipped doc, editing the doc *is* editing the recipe — the test
  cannot drift the way the hand-copied mirrors did.
- **Wiring assertions**: (a) the Level-5-effective recipe declares a
  `BM25Field`; (b) `ContextAssembler` with default `retrieval_mode="auto"`
  resolves that recipe to a query-sensitive mode (`lexical` or `hybrid`),
  asserted both positively (membership) and negatively (`!= "composite"`);
  (c) structural guard — the parser found the Level 5 heading and ≥ 2 `Memory`
  definitions, so a doc restructure fails loudly instead of vacuously passing.

### Flow

Doc edit removing `BM25Field` from the recipe → test parses doc → recipe class
built without BM25 → auto resolves to `composite` → assertion fails with a
message naming the doc, the missing field, and the query-blind consequence.

### Technical Approach

- New file `tests/test_default_recipe_wiring.py`; no existing files modified.
- Parse `docs/guides/agent-memory-quickstart.md` relative to the test file
  (`Path(__file__).resolve().parent.parent / "docs" / ...`) so it works from
  any checkout/worktree.
- Section split on `^## ` headings; the ContextAssembler section is identified
  by "ContextAssembler" in the `## Level 5` heading text (matching on the
  stable "Level 5" prefix, asserting it mentions ContextAssembler).
- Exec only `Import`/`ImportFrom`/`ClassDef` nodes via
  `ast.parse` → filter → `compile` → `exec` into a shared namespace. Class
  definitions do not write to Redis at class-creation time; instances are
  never created and nothing is saved, so the test performs no Redis writes of
  its own (the plugin's flushdb isolation still applies).
- `_effective_mode` is the established assertion surface — already used by
  `tests/test_context_assembler_hybrid.py` and the CSR harness; no new API.
- `score_weights` is a required constructor arg; pass the quickstart Level 5
  values `{"relevance": 0.6, "confidence": 0.3}` (ignored by lexical/hybrid
  paths; harmless).
- Tests (one behavior per test):
  1. `test_quickstart_has_context_assembler_level` — structural guard (Level 5
     heading found; ≥ 2 Memory definitions precede it). Prevents vacuous pass.
  2. `test_default_recipe_declares_bm25_field` — the Level-5-effective recipe
     declares exactly ≥ 1 `BM25Field` (checked via `_meta.fields` +
     `isinstance`).
  3. `test_default_recipe_auto_mode_is_query_sensitive` — resolved
     `_effective_mode in {"lexical", "hybrid"}` and `!= "composite"` for
     `retrieval_mode="auto"` (the default — construct without passing
     `retrieval_mode`, so the test also guards the default value itself).
  4. `test_recipe_levels_after_attention_keep_bm25` — every `Memory` from the
     second definition onward (Levels 2–4) declares `BM25Field`, pinning the
     doc's "query-sensitive from Level 2 on" narrative.

## Failure Path Test Strategy

### Exception Handling Coverage
- No exception handlers in scope — the new test contains none, and no source
  files are touched.

### Empty/Invalid Input Handling
- Structural-guard test (test 1) covers "doc restructured / heading renamed /
  blocks vanished": the extractor raises or the guard assertion fails rather
  than skipping silently. No `pytest.skip` on parse failure — a missing or
  unparseable quickstart is a hard failure by design.

### Error State Rendering
- Assertion messages name the doc path, the missing `BM25Field`, and the
  query-blind `composite` consequence so a future red run is self-explaining.

## Test Impact

No existing tests affected — the change is purely additive (one new test
file). The pre-existing drift in `tests/test_guide_examples.py` /
`tests/test_adoption_ladder.py` mirrors is intentionally **not** fixed here
(see No-Gos).

## Rabbit Holes

- **Fixing the drifted mirror tests** (adding `BM25Field` to `GuideL*Memory` /
  `AttentionMemory` etc.): touches many tests, adds Redis index behavior to
  their runs, and is orthogonal to the wiring guard. Separate cleanup.
- **Executing doc blocks verbatim** (imports + statements): Level 2+ blocks
  save records, Level 5 calls `assemble()`, Level 6 needs a Voyage API key.
  Class-defs-only extraction is the deliberate, sufficient scope.
- **Parsing `docs/features/context-assembler.md` code blocks too**: its
  examples reference `Memory` without defining it — not materializable. The
  quickstart is the canonical recipe source; the feature doc's mode table is
  already covered by unit tests in `test_context_assembler_hybrid.py`.
- **Generalizing into a doc-example test framework**: out of scope; one
  focused test file.

## Risks

### Risk 1: Quickstart restructures (headings renamed, levels reordered)
**Impact:** Extractor could bind to the wrong recipe or find nothing.
**Mitigation:** Structural-guard test fails loudly (explicit assertion on the
Level 5 heading and Memory-definition count); assertion messages tell the doc
editor exactly what invariant the doc must keep.

### Risk 2: Exec'ing doc-derived class definitions has import side effects
**Impact:** A future doc edit adding an exotic import could slow or break the test.
**Mitigation:** Only blocks before Level 5 are exec'd (Level 6's
`VoyageProvider` import never runs); imports in scope are all top-level
popoto names per the doc's own cheat sheet.

### Risk 3: Model class name collision (`Memory` defined 4×)
**Impact:** Popoto keys by class name; duplicate definitions could interact.
**Mitigation:** Spike confirmed class creation is side-effect-free in Redis;
no instances are saved; each definition simply rebinds the namespace entry.
The pytest plugin flushes DB 13/15 per test regardless.

## Race Conditions

No race conditions identified — the test is synchronous, single-threaded, and
performs no Redis writes. Cross-pipeline contention with #446 is handled by
`POPOTO_TEST_DB=13` (they run on a different DB index).

## No-Gos (Out of Scope)

- [SEPARATE-SLUG #446] Any edit to `src/popoto/fields/bm25_field.py` — owned
  by the parallel #446 pipeline; this plan only imports the class via the
  top-level `popoto` package.
- Runtime behavior changes of any kind — the issue mandates test-only; the
  Verification table carries an anti-criterion asserting `src/` is untouched.
- Fixing the drifted `GuideL*Memory`/`AttentionMemory` mirror models — noted
  in this plan as evidence, but it is a separate cleanup with real blast
  radius (BM25 on_save indexing changes those tests' Redis footprints). Not
  filed as an issue by this plan; the PR description will flag it for the
  maintainer to triage. (Tag rationale: candidate future work surfaced by this
  plan, not a deferred obligation of it — issue #445's acceptance criteria do
  not include mirror repair.)

## Update System

No update system changes required — test-only change inside the repo.

## Agent Integration

No agent integration required — pytest-collected regression guard.

## Documentation

No documentation changes required: the test asserts existing documented
behavior (quickstart recipes + `retrieval_mode` docs already describe the
auto-resolution rules and the composite trap at
`docs/guides/agent-memory-quickstart.md:308` and
`docs/features/context-assembler.md`). No new feature surface. The do-docs
gate will verify the docs build and confirm no cascade is needed.

## Success Criteria

- [ ] A test fails if the quickstart's default recipe loses its `BM25Field`
      (verified during build by red-proofing: temporarily strip
      `content_bm25` from a copy of the doc text and confirm the assertion
      trips — proof pasted into the PR description).
- [ ] The test asserts resolved `_effective_mode != "composite"` (and
      membership in `{"lexical", "hybrid"}`) under default/auto mode — not
      merely field presence.
- [ ] No files under `src/` modified; no existing test files modified.
- [ ] Full suite green locally: `POPOTO_TEST_DB=13 scripts/ci-local.sh --fast`.
- [ ] Docs gate run (`/do-docs`) — expected no-op, recorded.

## Team Orchestration

Single-agent pipeline (this SDLC session): the lead agent implements directly;
do-pr-review provides the independent review pass. No subagent team needed for
a one-file test change.

## Step by Step Tasks

### 1. Write the wiring test
- **Task ID**: build-wiring-test
- **Depends On**: none
- **Validates**: `tests/test_default_recipe_wiring.py` (create)
- **Informed By**: spike-1 (confirmed: parser+exec+resolution works; score_weights required)
- Create `tests/test_default_recipe_wiring.py` with extractor helper + 4 tests
  per Technical Approach.
- Run `black` on the new file.

### 2. Red-proof the guard
- **Task ID**: validate-red
- **Depends On**: build-wiring-test
- Feed the extractor a doc variant with `content_bm25` lines stripped
  (in-memory string, not a file edit) and demonstrate the mode assertion
  fails → capture output for the PR description. Implement as a fifth test
  (`test_guard_trips_when_bm25_removed`) so the red-proof is permanent, not a
  one-off.

### 3. Full local verification
- **Task ID**: validate-all
- **Depends On**: validate-red
- `POPOTO_TEST_DB=13 pytest tests/test_default_recipe_wiring.py -q` green.
- `POPOTO_TEST_DB=13 scripts/ci-local.sh --fast` green.
- Confirm `git diff --stat main` touches only `tests/test_default_recipe_wiring.py`
  and `docs/plans/default_recipe_bm25_wiring.md`.

### 4. Ship
- **Task ID**: ship
- **Depends On**: validate-all
- Commit on `feature/445-default-recipe-bm25-wiring`, push, open PR with
  `Closes #445`, red-proof output, and the mirror-drift flag for maintainer
  triage.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| New wiring tests pass | `POPOTO_TEST_DB=13 pytest tests/test_default_recipe_wiring.py -q` | exit code 0 |
| Full fast suite passes | `POPOTO_TEST_DB=13 scripts/ci-local.sh --fast` | exit code 0 |
| Mode assertion present | `grep -c 'composite' tests/test_default_recipe_wiring.py` | output > 0 |
| Test-only: no src changes | `git diff --name-only main...HEAD -- src/ \| wc -l` | match count == 0 |
| bm25_field.py untouched (anti-criterion, #446 conflict guard) | `git diff --name-only main...HEAD -- src/popoto/fields/bm25_field.py \| wc -l` | match count == 0 |
| No new xfails | `grep -c 'xfail' tests/test_default_recipe_wiring.py` | match count == 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| | | (populated by /do-plan-critique) | | |

---

## Open Questions

None — scope is fully determined by issue #445's acceptance criteria, and
spike-1 resolved the single technical unknown (extractor feasibility). The one
judgment call (which `Memory` is "the default recipe") is resolved as: the
recipe in effect when the quickstart reaches its ContextAssembler step, i.e.
the last `Memory` defined before Level 5 — matching the doc's progressive
narrative ("Use any Memory model from Levels 1-4", with Level 2+ adding
query-sensitivity precisely for this purpose).
