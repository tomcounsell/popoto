---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-21
tracking: https://github.com/tomcounsell/popoto/issues/459
last_comment_id:
revision_applied:
---

# SIQ Benchmark — Subconscious Injection Quality (flagship native harness)

## Problem

**Current behavior:** Every benchmark Popoto has today — internal sweeps, the Tier-1..4
scenario harness, the LongMemEval-S/LoCoMo external harness (`run_external.py`), and the
Tier-5 judged-answer harness (`judge.py`, PR #475) — measures **query-driven retrieval**: an
explicit question or `query_cues` dict is given, and the harness scores whether the right
evidence came back. Popoto's actual differentiator (strategy doc
`docs/plans/benchmarking_strategy_2026-07.md` §2.1, "the framing that drives everything") is
the **query-blind composite mode**: with no `BM25Field`/`EmbeddingField`, `ContextAssembler`
ranks purely by importance/confidence/decay and can inject memory *before* the user ever asks
for it. No public benchmark — and no benchmark in this repo — measures that.

**Desired outcome** (issue #459, strategy §2.1): a publishable, deterministic, competitor-fair
harness — **SIQ** — that runs multi-turn agent traces where a later turn needs a memory
established earlier, but the turn's own message never lexically cues it (coreference,
implication, or need-to-know that's simply never restated). It scores:

1. **Injection Precision/Recall @ budget** — of the records `ContextAssembler.assemble()`
   actually injects under `max_items`/`max_tokens`, how many are ground-truth useful for the
   current turn.
2. **Anticipation lead time** — how many turns *before* the turn where the memory becomes
   explicitly relevant did injection start (a pure query-driven retriever scores 0 here by
   construction, since it never sees a cue to act on).
3. **Budget efficiency** — useful-tokens / injected-tokens, tying quality to the token budget
   the assembler already enforces.

Mem0/Zep/Hindsight-style adapters can run the same traces; they are expected to score near-zero
on injection precision/recall (they retrieve only in response to a query) — that's the
demonstration, not a flaw in the harness.

## Freshness Check

**Baseline commit:** `856bf32` (origin/main).
**Issue filed:** 2026-07-10T10:03:10Z (epic #456 batch, alongside #457/#460-#463 — all already
planned/shipped per session context; no commits since filing touch the SIQ-relevant files
beyond the unrelated `llm_memory_extraction_path`/`confidence_gated_retrieval` plans).
**Disposition:** Unchanged.

**File:line references re-verified:**
- `src/popoto/recipes/context_assembler.py:926-1000` — `ContextAssembler.__init__`, mode
  resolution (`auto`→`hybrid`/`lexical`/`composite`). Confirmed: composite mode's pull path
  (`_pull_path_composite`, line 1389) ranks via `composite_score(indexes=score_weights)` and
  never inspects `query_cues` *content* for ranking (only an `ExistenceFilter` pre-check on cue
  *keys* at line ~1397-1409) — i.e. it is genuinely query-blind. This is the mode SIQ exercises.
- `src/popoto/recipes/context_assembler.py:1126-1260` — `assemble()`. Confirmed `emit_trace=True`
  attaches `metadata["trace"]`: `[{"key","rank","score","source"}]` per selected record, captured
  before post-retrieval decay mutation — exactly the per-turn injection record SIQ needs for
  anticipation lead time, with no new instrumentation required.
- `tests/benchmarks/judge.py` (392 lines) — pinned `JUDGE_MODEL = "gpt-4o-mini"`,
  `LLM_JUDGE_PROMPT` (Mem0/GAM, sha256-pinned), lazy `openai` import, `is_judge_available()`
  graceful skip. Confirmed present and stable; SIQ reuses this module directly rather than
  duplicating a judge.
- `tests/benchmarks/scenarios/external_base.py` (502 lines) — `ExternalScenario`, per-item
  unique-prefixed Model class pattern (`_build_external_model_class`), `teardown()` key sweep.
  Confirmed as the pattern SIQ's per-trace model class follows.
- `tests/benchmarks/csr/corpus.py` — `PlantedMemory`/`CsrTestCase` schema + `lint_case()`
  authoring guard (adversarial-query token-disjointness). Confirmed as the pattern for SIQ's
  fixture schema + a new lint (turn message must NOT lexically cue the target memory).
- `tests/benchmarks/conftest.py` — pytest DB-15 isolation fixtures. Confirmed present; SIQ's
  core suite (`test_siq.py`) uses these directly, unlike `run_external.py` which is a
  non-pytest CLI on DB 14.
- `docs/benchmarks.md` — exists, documents Tiers 1-5 + CSR + external harness. Confirmed as the
  target for the new SIQ section (required before merge per repo docs-gate convention).

**Active plans in `docs/plans/` overlapping this area:** none. `judged_answer_harness.md`
(#458, Tier 5) is the closest sibling and is complete/merged; SIQ is additive, not overlapping.

## Prior Art

- **PR #475 (#458)** — Tier-5 judged-answer harness. Established the pinned-judge pattern
  (`judge.py`), the `{dataset}_{date}_{mode}.{json,md}` artifact convention, and the
  optional-`openai`-dependency posture. SIQ reuses `judge.py` verbatim and mirrors the artifact
  convention with a `siq_` prefix.
- **Issue #465 / README "External-harness DB isolation & residue"** — documents the DB-0 leak
  incident from running harness code outside pytest. Directly informs the constraint that SIQ's
  core suite must be pytest-native (DB 15), not a DB-14 CLI harness like `run_external.py`.
- **`tests/benchmarks/csr/`** (#418) — deterministic corpus-planting harness with authoring
  lints enforced at load/plant time. SIQ's fixture schema and lint follow this pattern closely
  (planted corpus → per-turn assertions, lint enforced before any test can silently pass on a
  malformed fixture).
- No prior closed issue/PR attempted query-blind injection benchmarking — this is greenfield
  within the benchmark suite.

## Research

No relevant external findings beyond what's already cited in
`docs/plans/benchmarking_strategy_2026-07.md` §2.1 (Hindsight, Mem0/GAM, MemPro citations) —
those are training-data-era arXiv papers already read for the Tier-5 plan; nothing new to
re-fetch for this harness-design task. Proceeding on codebase context.

## Data Flow

1. **Fixture (committed, static):** `tests/benchmarks/siq/fixtures/*.json` — a `SiqTrace`:
   ordered list of turns, each with `speaker`, `message`, optionally an `establishes` memory
   (content + a synthetic `topic`/`entity` key) and optionally a `should_recall` list (memory
   ids ground-truth-useful at this turn, decided by the fixture author from the narrative, not
   computed).
2. **Load + lint (`siq/corpus.py`):** `load_trace()` parses the JSON into frozen dataclasses;
   `lint_trace()` enforces: (a) every `should_recall` id was `establishes`-ed at an earlier
   turn, (b) the current turn's `message` shares **no** token (real BM25 tokenizer, same as
   CSR's lint) with the establishing turn's content for any `should_recall` id — this is the
   mechanical proof that the cue is genuinely absent, not just eyeballed, (c) every trace has at
   least one turn with a non-empty `should_recall` occurring $\geq 2$ turns after its
   establishing turn (else the trace can't exercise anticipation lead time).
3. **Replay (`siq/runner.py`):** for each turn in order — if the turn `establishes` a memory,
   save it into a per-trace uniquely-prefixed Popoto Model (mirrors
   `_build_external_model_class`) with `importance`/`confidence`/decay-timestamp fields set from
   the fixture (deterministic, not derived from wall clock — planted `timestamp` field, CSR
   convention). Then call `ContextAssembler(model_class, score_weights, max_items=...,
   max_tokens=..., retrieval_mode="composite").assemble(query_cues=None, agent_id=trace_id,
   emit_trace=True)` — **query-blind by construction** (`query_cues=None` skips the pull path
   entirely per `assemble()`'s own branch at line ~1176-1178; the injected set on a
   `query_cues=None`/composite-mode call is push-path only, so the plan additionally calls with
   `query_cues={"agent_id": trace_id}` — a cue carrying no lexical content about the target
   memory, only the partition key — to exercise the composite pull path while remaining
   provably cue-blind per the lint in step 2). Collect the `metadata["trace"]` records — the
   real per-turn injected set.
4. **Score (`siq/metrics.py`):** compare each turn's injected-id set against `should_recall`
   (and the complement, for precision) to get precision/recall @ budget; find, for each memory,
   the first turn index it appears in the injected set and diff against the first turn its id
   appears in any `should_recall` to get anticipation lead time; sum injected-vs-useful token
   counts (via the assembler's own `_count_record_tokens`-equivalent, exposed for the harness)
   for budget efficiency.
5. **Aggregate + report (`siq/run_siq.py`, pytest `test_siq.py`):** JSON + Markdown artifact
   under `tests/benchmarks/results/siq/`, mirroring the Tier-5 naming
   (`siq_{date}_{mode}.{json,md}` + `siq_latest_{mode}.*`), where `{mode}` is `native` (Popoto)
   or an adapter name (`stub`, and — follow-up — `mem0`/`zep`/`hindsight`).
6. **Competitor adapters (`siq/adapters.py`):** a `SiqAdapter` Protocol (`ingest_turn(turn) ->
   None`, `snapshot_injected(turn_index) -> List[str]`) mirroring `judge.py`'s
   `JudgeProtocol`/lazy-import pattern. This plan ships the Protocol + one reference
   `NativeAdapter` (wraps the `ContextAssembler` replay above) + one `QueryOnlyStubAdapter`
   (a trivial "only returns memories whose content lexically overlaps the current turn's
   message" adapter — deterministic, no network, no extra dependency — used to prove in a unit
   test that a query-driven strategy scores ~0 recall/anticipation on these fixtures, which is
   the harness's core validity check). Real Mem0/Zep/Hindsight adapters are explicitly
   out-of-scope for this PR (see No-Gos) and tracked as a follow-up issue.

## Architectural Impact

- **New dependencies:** none required for the default suite (no new hard deps; `openai` stays
  optional, reused from `judge.py`).
- **Interface changes:** none to `src/popoto/` — `ContextAssembler.assemble(emit_trace=True)`
  already exposes everything SIQ needs; this is a pure test/benchmark addition.
- **Coupling:** `tests/benchmarks/siq/` depends on `tests/benchmarks/judge.py` (import, no
  duplication) and mirrors `tests/benchmarks/csr/corpus.py` conventions without importing it
  (different schema; copying the *pattern*, not the code, avoids coupling two independent
  fixture families).
- **Data ownership:** none — benchmark-only, no production schema changes.
- **Reversibility:** fully additive; deleting `tests/benchmarks/siq/` reverts cleanly.

## Appetite

**Size:** Medium

**Team:** Solo dev (this session)

**Interactions:**
- PM check-ins: 0-1
- Review rounds: 1 (do-pr-review gate)

## Prerequisites

No prerequisites — this work has no external dependencies. The optional judged-usefulness path
reuses `judge.py`'s existing `OPENAI_API_KEY` gate (skips gracefully without it, same as Tier 5).

## Solution

### Key Elements

- **`tests/benchmarks/siq/corpus.py`** — `SiqTurn`/`SiqTrace` frozen dataclasses,
  `load_trace()`, `lint_trace()` (cue-blindness + anticipation-window authoring guards using the
  real BM25 tokenizer, mirroring `csr/corpus.py`'s lint).
- **`tests/benchmarks/siq/fixtures/*.json`** — 4-6 committed deterministic traces (6-12 turns
  each), covering: coreference ("her new apartment" → earlier "I'm moving to Austin"),
  implication (mentions a deadline without restating a constraint established 5 turns earlier),
  need-to-know (a stated preference/allergy relevant to a later unrelated-seeming request), and
  one negative-control trace (no `should_recall` overlap at all — precision/recall must both
  read as trivially perfect-or-undefined without crashing, i.e. the harness handles the
  zero-ground-truth edge case).
- **`tests/benchmarks/siq/runner.py`** — replay driver: builds the per-trace Model class
  (mirrors `external_base._build_external_model_class`), plants turns in order, calls
  `ContextAssembler.assemble(..., emit_trace=True)` at each turn, returns a `TurnResult` list.
- **`tests/benchmarks/siq/adapters.py`** — `SiqAdapter` Protocol + `NativeAdapter` (wraps
  `runner.py`) + `QueryOnlyStubAdapter` (deterministic, dependency-free negative-control
  adapter proving the harness discriminates query-blind from query-driven behavior).
- **`tests/benchmarks/siq/metrics.py`** — `precision_recall_at_budget()`,
  `anticipation_lead_time()`, `budget_efficiency()`, each pure functions over `TurnResult`
  lists + `SiqTrace` ground truth — independently unit-testable with hand-computed expected
  values.
- **`tests/benchmarks/siq/run_siq.py`** — CLI entry point (mirrors `run_external.py`'s
  shape: argparse, `--adapter native|stub`, `--limit`, artifact writer) for ad-hoc/manual runs.
  Defaults to DB 15 semantics are N/A here since it's a CLI (not pytest) — it follows
  `run_external.py`'s `POPOTO_BENCH_DB` pattern (default 14, rejects 0) for the *same* isolation
  reason, so it never collides with the pytest suite's DB 15 or the live #457 chain's DB 14 run
  (constraint: this CLI is optional/manual-only; the **required, CI-facing** artifact is
  `test_siq.py`, which runs under pytest's DB-15 plugin like every other `test_*.py` in
  `tests/benchmarks/`).
- **`tests/benchmarks/test_siq.py`** — the CI-facing pytest suite: lint tests (every committed
  fixture passes `lint_trace()`), `NativeAdapter` replay tests against each fixture asserting
  precision/recall/lead-time/efficiency values (hand-verified per fixture, checked into the
  test as expected constants — deterministic, no tolerance-band flakiness), and the
  `QueryOnlyStubAdapter` discriminant test (asserts stub recall ≈ 0 / native recall > 0 on the
  same fixture — the harness's own validity proof). Uses `tests/benchmarks/conftest.py`'s DB-15
  fixtures exclusively.
- **`docs/benchmarks.md`** — new "SIQ (Subconscious Injection Quality)" section: what it
  measures, the three metrics, how to run it (`pytest tests/benchmarks/test_siq.py` +
  `python -m tests.benchmarks.siq.run_siq`), the competitor-fairness framing, and an explicit
  "committed fixtures, no fabricated competitor numbers — full Mem0/Zep/Hindsight adapters
  tracked in follow-up issue #TBD" note.

### Flow

Fixture JSON (committed) → `load_trace()`/`lint_trace()` → `runner.replay(trace, adapter)` →
per-turn `TurnResult` (injected ids, scores, tokens) → `metrics.py` scoring functions →
aggregate dict → `test_siq.py` asserts against checked-in expected values, `run_siq.py`
optionally writes `results/siq/siq_{date}_{mode}.{json,md}`.

### Technical Approach

- Composite (query-blind) mode is the only retrieval mode SIQ's `NativeAdapter` exercises —
  this is deliberate: SIQ measures the query-blind path specifically, not lexical/hybrid (those
  are already covered by `run_external.py`/Tier 5).
- Ground truth (`should_recall` per turn) is **authored**, not derived, exactly like CSR's
  `relevant_ids` — no LLM judge in the critical-path scoring loop. An LLM judge (reusing
  `judge.py`'s pinned `gpt-4o-mini`) is offered only as an **optional** secondary "usefulness"
  cross-check (`--judged` flag on `run_siq.py`, skipped by default and in the pytest suite,
  gated by `is_judge_available()`) for cases where a fixture author wants a second opinion on
  borderline `should_recall` calls — never a hard dependency of the metrics.
- Per-trace Model classes get a random unique prefix (`uuid4().hex[:8]`, mirrors
  `external_base.py`) so parallel test runs and repeated invocations never collide on Redis
  keys; `teardown()` scans and deletes by prefix, same as `ExternalScenario`.
- `anticipation_lead_time` is computed per-memory as
  `first_should_recall_turn - first_injected_turn` (positive = anticipated, 0 = exactly on time,
  undefined/excluded if never injected before or at the relevant turn — recorded as a
  miss, not a negative number, to avoid silently rewarding never-injected memories with
  "infinite lead").
- Fixture authoring script: none required for v1 (fixtures are hand-authored JSON, small
  enough — 4-6 traces × ~10 turns — that a generation script would be over-engineering for this
  appetite); if traces grow past a dozen, a future issue can add an authoring helper whose
  *output* still gets committed (never generated at test time, per the CSR/issue constraint).

## Failure Path Test Strategy

### Exception Handling Coverage
- `runner.py`'s `assemble()` call sits inside the same try/except shape as
  `external_base.py`'s pull path (`ContextAssembler` internally logs+returns `[]` on query
  failures rather than raising) — no new bare `except Exception: pass` introduced by this plan.
  If `lint_trace()` finds an authoring violation it raises `SiqAuthoringError` (mirrors
  `CsrAuthoringError`) — a test asserts this is raised (not swallowed) for a deliberately
  malformed fixture fixture-in-test (inline dict, not a committed file).

### Empty/Invalid Input Handling
- `metrics.precision_recall_at_budget()` on a turn with empty `should_recall` and empty
  injected set: precision/recall defined as `None` (undefined), not `0/0` ZeroDivisionError —
  tested explicitly (the negative-control fixture exercises this).
- `runner.replay()` on a trace with zero turns: raises `SiqAuthoringError` at lint time (trace
  must have ≥1 turn with `establishes` and ≥1 with `should_recall`), never reaches the replay
  loop with empty input.

### Error State Rendering
- Not user-facing (benchmark/test code) — `run_siq.py` failures print to stderr and exit
  non-zero, matching `run_external.py`'s posture; no silent swallow of a fixture that fails to
  parse.

## Test Impact

No existing tests affected — this is a greenfield addition (`tests/benchmarks/siq/` and
`tests/benchmarks/test_siq.py` are new files; no existing scenario, adapter, or metrics module
is modified). `docs/benchmarks.md` gets an additive new section (no existing section rewritten).

## Rabbit Holes

- **Do not build real Mem0/Zep/Hindsight adapters in this PR.** Each is a heavyweight optional
  dependency + API key + live-service call; wiring all three "competitor-fair" is explicitly a
  follow-up (tracked below), not blocking this harness landing. `QueryOnlyStubAdapter` is
  sufficient to prove the harness's discriminating power now.
  Deferral reason: **scope-narrowing** — the appetite is a harness + fixtures + native scoring,
  not a live three-way competitor bake-off.
- **Do not build a fixture-generation/authoring UI or LLM-based trace generator.** 4-6 hand
  authored traces satisfy "deterministic, committed, CSR discipline" at this scale; a generator
  adds RNG-adjacent complexity the constraints explicitly warn against.
  Deferral reason: **out-of-scope** — the issue explicitly wants committed fixtures, not
  runtime generation.
- **Do not attempt to make anticipation lead time comparable across adapters with different
  internal turn granularities** (e.g. an adapter that batches multiple turns) — out of scope;
  the metric assumes one `assemble()`/adapter-snapshot call per trace turn, which all three
  shipped components (`NativeAdapter`, `QueryOnlyStubAdapter`, future competitor adapters) honor
  by Protocol contract.
  Deferral reason: **scope-narrowing**.

## Risks

### Risk 1: Fixture ground truth (`should_recall`) is subjective / could be gamed
**Impact:** A benchmark whose ground truth is hand-picked by the same team building the system
under test invites (fair) skepticism about self-grading.
**Mitigation:** `lint_trace()`'s mechanical cue-blindness check (real BM25 tokenizer,
token-disjoint enforcement) is the objective, auditable half of authoring — it can't be gamed
by picking easy `should_recall` ids, only by picking hard ones (the constraint only gets
*harder* to satisfy, never easier). The `QueryOnlyStubAdapter` discriminant test is published
alongside — anyone can re-run it and see the near-zero baseline independent of Popoto's own
scoring. The optional LLM-judge cross-check (pinned model+prompt) offers a second, external
opinion for anyone who wants one.

### Risk 2: Composite mode's `query_cues={"agent_id": trace_id}` call could accidentally leak
lexical signal if the model ever grows a BM25Field
**Impact:** Would silently flip `NativeAdapter` from composite (query-blind) to
lexical/hybrid mode, invalidating "query-blind by construction."
**Mitigation:** The per-trace Model class in `runner.py` declares **no** `BM25Field`/
`EmbeddingField` (verified by an explicit unit test asserting
`ContextAssembler(...)._effective_mode == "composite"` before any scoring runs) — mirrors the
CSR harness's own mode-assertion discipline.

## Race Conditions

No race conditions identified — the harness is single-threaded, sequential-turn replay within
one pytest process; per-trace Model classes are uniquely prefixed so parallel `pytest -n`
workers (if ever used) don't collide, matching `external_base.py`'s existing safety property.

## No-Gos (Out of Scope)

- **Real Mem0/Zep/Hindsight adapter implementations and a live competitor comparison run** —
  tag: **follow-up-issue** (tracked as a new GitHub issue filed at PR time, referencing this
  plan and the `SiqAdapter` Protocol as the extension point).
- **Fixture corpora beyond ~20k memories / dozens of traces** — tag: **out-of-scope** (project
  ceiling per constraints; SIQ's traces are small by design, this is a per-turn precision
  benchmark, not a scale benchmark — RLT (#460/§2.2) covers scale/latency).
- **Changing `ContextAssembler`'s public API** — tag: **out-of-scope** (`emit_trace=True`
  already exposes everything needed; no source changes required).
- **A CI gate that runs `run_siq.py`'s optional `--judged` LLM path automatically** — tag:
  **deferred** (mirrors Tier 5's posture: judged runs are opt-in/manual, gated on
  `OPENAI_API_KEY`, never a required CI step).

## Open Questions

None — design is fully specified by the issue body and mirrors an established in-repo pattern
(Tier 5 / CSR) closely enough that no unresolved judgment calls remain for the human. Proceeding
to critique.
