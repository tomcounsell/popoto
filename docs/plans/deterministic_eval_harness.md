---
status: Ready
type: feature
appetite: Large
owner: valor
created: 2026-06-30
tracking: https://github.com/tomcounsell/popoto/issues/418
last_comment_id:
revision_applied: true
---

# Deterministic Memory-Recall Eval Harness (CSR + Adversarial Gap)

## Problem

Popoto Agent Memory has **two** retrieval-quality harnesses today, and both are the
wrong tool for catching regressions in CI:

1. **External harness** (`tests/benchmarks/run_external.py`, #394) — LongMemEval-S /
   LoCoMo. Realistic, but the headline `hybrid` numbers require an embedding model
   (`all-MiniLM-L6-v2`), so the comparison is **stochastic** (float embeddings, model
   versioning) and slow (~275k turns embedded on CPU). It is a manual, committed-artifact
   benchmark, not a per-PR gate.
2. **Internal parametric sweep** (`tests/benchmarks/run_sweeps.py`) — tunes ~25 behavioral
   constants against synthetic scenarios and reports P@k / nDCG / MRR. It optimizes
   constants; it does not assert *named, verifiable retrieval properties* that must hold.

There is **no eval that is deterministic** (same corpus + query → bit-identical score every
run), runs in CI with **no LLM judge and no Redis-module dependency**, and works identically
on Redis and Valkey. The cost of that gap is concrete: finding **#409** (RETR-1) —
"default recipe retrieval is query-independent; P@10 = the random baseline; gibberish and a
real query return the *bit-identical* 10-memory set" — shipped to users and was caught only
by a hand-rolled adversarial audit, not by any standing test. A deterministic harness that
re-runs each test with an **adversarially paraphrased** query would have flagged #409
automatically — by its actual signature: a query-blind path produces the **identical ranked
list** for the standard and adversarial query (`Gap = 0` exactly) *and* a **low standard-query
CSR** (the ranking ignores the query, so it cannot satisfy query-relevance assertions). A
**large** `Standard − Adversarial` gap means the opposite pathology: retrieval that only works
when the query shares surface tokens with the stored memory (keyword dependence — expected of
pure BM25, and worth reporting, but not the #409 signature).

Issue #418 asks for that harness, adapting **CogBench's *scoring methodology* (not its task
suite)**: each test is `(planted corpus, query, set of deterministic verifiable assertions)`,
scored by a **Constraint Satisfaction Rate** with no judge and no classifier, plus an
adversarial re-run and an **Adversarial Gap**.

**Current behavior:** retrieval quality is measured only by stochastic external benchmarks
and constant sweeps. No standing, deterministic, assertion-based regression gate exists; a
query-independence regression like #409 can reach `main` undetected.

**Desired outcome:** a new `tests/benchmarks/csr/` harness that

- defines test cases as `(planted corpus, standard query, adversarial query, assertions)`;
- plants each corpus into the isolated test DB, runs both queries through
  `ContextAssembler.assemble()` (the primary retrieval path), and scores deterministic
  assertions (`in_top_k`, `ranks_above`, `none_older_than`, `covers_topic`, `excludes`);
- reports **RSR (standard)**, **RSR (adversarial)**, **Adversarial Gap**, and per-primitive
  pass rates to a committed `tests/benchmarks/results/csr/` artifact (md + json);
- runs as a **pytest** under the existing DB-15 isolation, fully deterministic (lexical/BM25
  retrieval only — no embeddings, no LLM, no Redis module), so it is usable as a CI gate.

## Freshness Check

**Baseline commit:** `ffe5a20` (`main` HEAD — "Plan: Run the full LongMemEval-S hybrid
benchmark and commit the artifact (#442)").
**Issue filed at:** 2026-06-11T08:57:39Z.
**Disposition:** Unchanged — premise fully intact. #418 is greenfield (no prior plan), and
the infrastructure it builds on is stable.

**Working-tree note (important):** this plan was researched on branch
`feature/442-hybrid-full-run` (`c60860a`), whose working tree carries the **uncommitted #442
changes** (shared embedding provider in `external_base.py`, mode-suffixed `save_reports`) and
untracked files (`src/popoto/embeddings/sentence_transformers.py`, `tests/embeddings/`). The
CSR harness does **not** depend on any of that #442 work — it depends only on infrastructure
already on `main` (`BenchmarkItem`, `Scenario`/`ScenarioResult`, `metrics/retrieval.py`,
`ContextAssembler.assemble()`). File:line references below were read on the branch but point
at code that predates #442 and is identical on `main`, except where explicitly flagged.

**File:line references re-verified:**
- `tests/benchmarks/datasets/__init__.py:14-23` — `BenchmarkItem` namedtuple
  (`item_id, history, query, relevant_ids, metadata`) — **confirmed**. The CSR test-case
  schema is a sibling shape, not a reuse of this (assertions, not `relevant_ids`).
- `tests/benchmarks/scenarios/base.py:15-41` — `ScenarioResult` dataclass; `:44` — `Scenario`
  ABC with `setup()/run()/teardown()/execute()` — **confirmed**. CSR reuses the
  setup→run→teardown lifecycle shape.
- `tests/benchmarks/scenarios/external_base.py:96-155` — `_build_external_model_class`
  (per-item unique-name Model class for Redis key isolation) — **confirmed**; this is the
  corpus-planting pattern CSR adapts. (On the branch this calls the #442
  `_get_shared_provider()`; on `main` it constructs the provider inline. CSR is lexical-only
  and declares no `EmbeddingField`, so the difference does not reach CSR.)
- `external_base.py:291-294` — retrieval via
  `assemble(query_cues={"topic": self.item.query}, agent_id=...)` — **confirmed**; CSR drives
  the same primary path. `:238-245` — record→`redis_key`→id reverse-map; CSR uses the same
  technique to map `assemble()` records back to planted test IDs. `:354-401` — `teardown()`
  scan-and-delete by class/agent prefix — **confirmed**.
- `tests/benchmarks/metrics/retrieval.py` — `precision_at_k`, `recall_at_k` (any-hit),
  `mean_reciprocal_rank`, pure functions — **confirmed**; CSR adds a new `satisfaction`
  module rather than overloading these.
- `tests/benchmarks/run_external.py:389-454` — `save_reports` (json + md + `_latest`
  pointer); `:297-386` — `build_markdown_report` — **confirmed**; CSR's report writer mirrors
  these conventions (own `results/csr/` dir, own builder).
- `src/popoto/recipes/context_assembler.py:987-993` — `assemble(query_cues, agent_id,
  partition_filters, assess_quality)` signature — **confirmed**. `:142-156` —
  `AssemblyResult` with ordered `.records` list — **confirmed**; assertions evaluate against
  `.records`. `:946-979` — `retrieval_mode="auto"` resolves to `hybrid`/`lexical`/`composite`
  by field presence; explicit `lexical` requires a `BM25Field` — **confirmed**.
- `src/popoto/fields/bm25_field.py:292` — Lua `table.sort(results, fn a,b -> a[2] > b[2])`,
  sort by BM25 score **descending** — **confirmed**. Lua `table.sort` is **not stable**, so
  equal-score ties can reorder run-to-run; this is the central determinism risk (see Risks).

**Active plans in `docs/plans/` overlapping this area:** none for CSR/adversarial/deterministic
(`ls docs/plans/ | grep -iE 'csr|determin|cogbench|constraint|adversar'` → none — confirms the
project-memory note that #418 is an unplanned, separate track). Adjacent-but-distinct:
`external_benchmark_harness.md` (#394, shipped) and the #442 hybrid-run plan share the
`tests/benchmarks/` tree but no code; `bm25_first_class_retrieval_mode.md` (#409) is the bug
this harness is meant to catch — it is a *consumer* relationship, not an overlap.

**Notes:** No drift. The assemble()/AssemblyResult/BM25 surfaces the harness binds to are
stable and predate #442.

## Research

No external research required. CogBench's *methodology* is fully described in the issue
(constraint-satisfaction scoring + adversarial gap); we are explicitly **not** using its
hosted service or its question-generation task suite. All the machinery the harness binds to
(`ContextAssembler.assemble()`, `BM25Field` lexical search, the DB-15 pytest isolation, the
`Scenario` lifecycle, the `save_reports`/markdown conventions) is in-repo and already
understood. No new third-party library, API, or ecosystem pattern is introduced. The one
genuinely new idea — authored adversarial paraphrases as committed fixture data — is a data
convention, not a dependency.

## Prior Art

- **#394 / `external_benchmark_harness.md` (shipped):** `Scenario`/`ScenarioResult`,
  per-item unique-name Model classes, `save_reports`/markdown report conventions,
  `metrics/retrieval.py`. CSR reuses the lifecycle and report shape; it does **not** reuse
  `BenchmarkItem` (which carries `relevant_ids`, not assertions) — CSR introduces its own
  `CsrTestCase` schema.
- **#409 / `bm25_first_class_retrieval_mode.md`:** the query-independence bug and its
  200-memory adversarial PoC (gibberish ⇒ identical injected set). CSR generalizes that
  PoC into a standing, named regression: a query-blindness detector (`identical rankings
  across standard/adversarial + low standard CSR`) that a query-blind path trips. The #409
  audit corpus (10 topics × 20 facts) is a ready-made
  template for the seed suite, shrunk to small focused corpora.
- **#442 hybrid-run plan:** establishes the `retrieval_mode` threading and the
  artifact-naming discipline (mode-suffixed files, never clobber a committed baseline) the
  CSR report writer follows in spirit (its own `results/csr/` namespace).
- **Internal sweep (`run_sweeps.py`):** distinct purpose (constant tuning), distinct output.
  CSR is a *third* harness, not an extension of the sweep — it asserts properties, it does
  not optimize constants. The plan keeps them separate by design (see Architectural Impact).

No prior deterministic-assertion harness exists. This is the first.

## Data Flow

1. **Entry (CI path):** `pytest tests/benchmarks/test_csr.py` — runs the seed suite under the
   popoto pytest plugin (DB 15, auto-flushed). **Entry (manual path):**
   `python -m tests.benchmarks.csr.run_csr` — runs the same suite and writes a committed
   report artifact.
2. **Suite load:** `suites/default.py` yields a list of `CsrTestCase` objects, each carrying
   a `planted_corpus` (list of `PlantedMemory`: id, content, timestamp, topic, importance), a
   `standard_query`, an `adversarial_query` (an authored paraphrase sharing **no** indexed
   tokens with the relevant memories but **at least one** indexed token with a distractor
   memory, so BM25 still returns hits — see Risk 2), a `primitive` label (e.g.
   `lexical_recall`), and a list of typed `assertions`. A pure suite-load lint (using the real
   BM25 tokenizer, `popoto.fields._tokenizer.tokenize`) enforces both token rules at load.
3. **Plant:** `corpus.plant(test_case)` builds a per-case unique-name Model class
   (`BM25Field` on `content`, a `SortedField importance`, a `FloatField timestamp`, a
   `StringField topic`) — adapted from `external_base._build_external_model_class` — and saves
   each `PlantedMemory` into DB 15, returning an `id → record` map and
   `record.db_key.redis_key → planted_id` reverse-map. **Post-plant guard:**
   `BM25Field.search(model_class, "content", case.adversarial_query, limit=1)` must return
   ≥1 hit, else the case raises at load (a zero-hit adversarial query would silently execute
   the composite fallback, `context_assembler.py:1314-1326` — see Risk 2).
4. **Retrieve (×2):** a `ContextAssembler(model_class, score_weights={},
   max_items=ASSEMBLER_MAX_ITEMS, retrieval_mode="auto")` — `score_weights` is a required
   positional parameter (`context_assembler.py:845-857`); the empty dict is safe because the
   lexical pull path never consults it — resolves to **`lexical`** because the model has a
   `BM25Field` and no `EmbeddingField` (`context_assembler.py:946-979`). It is called twice:
   `assemble(query_cues={"topic": standard_query}, agent_id=...)` and
   `assemble(query_cues={"topic": adversarial_query}, ...)`. Each returns an ordered
   `AssemblyResult.records`, mapped back to an ordered list of `planted_id`s via the
   reverse-map. Before each call the runner records a pre-flight
   `BM25Field.search(...)` hit count and derives `executed_path` (`"lexical"` if ≥1 hit,
   `"composite-fallback"` if 0) so a fallback can never masquerade as a lexical result.
5. **Score:** `satisfaction.evaluate(assertions, ranked_ids, planted_meta)` returns, per
   assertion, a deterministic `True/False`. The case's **CSR fraction** = passed / total
   assertions, computed once for the standard ranking and once for the adversarial ranking.
   The runner also records `rankings_identical = (ranked_ids_std == ranked_ids_adv)` — the
   query-blindness signal.
6. **Teardown:** scan-and-delete the per-case class/agent keys (same as `external_base`); the
   pytest plugin also flushes DB 15 between tests as a backstop.
7. **Aggregate:** mean CSR across cases → **RSR (standard)** and **RSR (adversarial)**;
   `Adversarial Gap = RSR_std − RSR_adv`; per-`primitive` pass-rate breakdown; a per-case
   **query-blindness flag** (`rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`) — the
   #409 signature.
8. **Output:** `run_csr.py` writes `tests/benchmarks/results/csr/csr_{YYYYMMDD}.{json,md}` and
   `csr_latest.{json,md}`. `test_csr.py` asserts determinism (two runs → identical RSR), suite
   health (every case executes; report shape is well-formed; every scored lexical run has
   `executed_path == "lexical"`), and the discriminative check (composite control blind,
   lexical path query-sensitive — see Solution item 5).

## Architectural Impact

- **New module, not an extension.** The CSR engine (typed assertions over a ranked list) and
  the adversarial-rewrite *convention* (authored paraphrase fixtures) are genuinely new and
  do not fit the sweep (`overrides`/`ParameterGrid`) or the external (`BenchmarkItem`/
  `relevant_ids`) shapes. New package `tests/benchmarks/csr/` keeps the three harnesses
  cleanly separated.
- **Reused infra:** DB-15 isolation (popoto pytest plugin), the `Scenario`-style
  setup→run→teardown lifecycle, the per-item unique-name Model-class trick for key isolation,
  `metrics/retrieval.py` helpers where useful, and the `save_reports`/markdown report
  conventions.
- **New dependencies:** none. Lexical-only retrieval uses `BM25Field` (pure Lua + core Redis
  commands) — no numpy, no embedding model, no Redis module. Runs identically on Redis and
  Valkey.
- **Interface changes:** none to `src/popoto/`. The harness consumes the public
  `ContextAssembler.assemble()` API read-only. All new code lives under `tests/`.
- **Reversibility:** high — the package is additive and self-contained; deleting
  `tests/benchmarks/csr/` and `test_csr.py` removes the feature with zero blast radius.

## Appetite

**Size:** Large (Medium-Large feature; the MVP below is the Medium core).

**Team:** Solo dev, code reviewer.

**Interactions:**
- PM check-ins: 1 (to confirm the MVP cut and the Open-Question defaults — chiefly
  lexical-only scope, "report-the-gap vs gate-on-the-gap", and explicit sign-off on the
  narrowed #408–#416 coverage enumeration in Technical Approach).
- Review rounds: 1–2 (assertion-engine correctness and determinism are the review substance;
  the report writer and fixtures are mechanical).

The assertion engine, corpus loader, runner, and a ~8-case seed suite are ~1 day of focused
work. The long pole is **authoring good adversarial paraphrases** (true semantic rewrites
that share no indexed tokens with the relevant memories yet still hit at least one distractor
in the BM25 index) and **designing corpora that avoid BM25 score ties** so
`ranks_above` assertions are deterministic — careful data design, not code volume.

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis/Valkey on :6379 | `redis-cli ping` | Plant/retrieve target (DB 15 in tests) |
| popoto editable install current | `python -c "import popoto; print(popoto.__version__)"` | DB-15 plugin + `ContextAssembler` import |
| BM25 lexical path works | `python -c "from src.popoto.fields.bm25_field import BM25Field; print('ok')"` | Lexical retrieval (no numpy needed) |
| pytest plugin active | `pytest -p no:popoto --co -q 2>/dev/null; echo ok` | Confirms DB-15 isolation is wired |

No embedding model, no `[benchmark]` extra, no network — the harness is deliberately
dependency-light so it can run on every CI leg.

## Solution

### MVP (this plan ships exactly this)

1. **`tests/benchmarks/csr/satisfaction.py` — the assertion engine (the new core).** Pure,
   deterministic predicates over an ordered `list[planted_id]` plus a `planted_meta` dict
   (`id → {timestamp, topic, ...}`). Assertion types:
   - `InTopK(memory_id, k)` — `memory_id` appears within the first `k` ranked ids.
   - `RanksAbove(higher_id, lower_id)` — `higher_id` precedes `lower_id` (both must appear;
     a missing id fails the assertion deterministically).
   - `NoneOlderThan(timestamp, k=DEFAULT_TOP_K)` — no id in the top-k has
     `planted_meta[id].timestamp < timestamp`.
   - `CoversTopic(topic, k=DEFAULT_TOP_K)` — every planted id with that topic appears in the
     top-k (the "result set covers all N planted facts on topic Q" constraint).
   - `Excludes(memory_id, k=DEFAULT_TOP_K)` — `memory_id` does **not** appear in the top-k.
   Each assertion is a small dataclass with an `evaluate(ranked_ids, planted_meta) -> bool`.
   `evaluate_all(assertions, ...)` returns `(passed: int, total: int, per_assertion: list)`.
2. **`tests/benchmarks/csr/corpus.py` — planting + schema.** Dataclasses `PlantedMemory`
   (`id, content, timestamp, topic, importance=0.5`) and `CsrTestCase` (`case_id,
   planted_corpus, standard_query, adversarial_query, primitive, assertions`, plus assembler
   overrides `retrieval_mode="auto"` / `score_weights={}` so the composite control case (item
   4e) can declare `retrieval_mode="composite"`, `score_weights={"importance": 1.0}` — this
   is fixed in the schema now because Tasks 4–6 depend on it). `plant(case)`
   builds a per-case unique-name Model (`BM25Field(source="content")`, `SortedField
   importance`, `FloatField timestamp`, `StringField topic`, `KeyField agent_id`), saves the
   corpus, and returns `(model_class, agent_id, id_to_record, reverse_map)`. The
   `SortedField importance` gives every case's model a sorted index, so the composite control
   case (item 4e) can rank via `composite_score` — without it, `composite_score({})` raises
   and ranks nothing (`query.py:516`). `plant()` ends with the **post-plant adversarial
   guard**: `BM25Field.search(model_class, "content", case.adversarial_query, limit=1)` must
   return ≥1 hit or `plant()` raises a clear authoring error. `cleanup(...)` scans/deletes by
   class + agent prefix (lifted from `external_base.teardown`).
3. **`tests/benchmarks/csr/run_csr.py` — runner + report.** For each case: plant → build a
   `ContextAssembler(model_class, score_weights=case.score_weights,
   max_items=ASSEMBLER_MAX_ITEMS, retrieval_mode=case.retrieval_mode)` — defaults
   `score_weights={}` / `retrieval_mode="auto"` resolve to lexical; the composite control
   overrides both → `assemble()` twice (standard, adversarial)
   → map records to planted ids → `evaluate_all` for each → record per-case
   standard/adversarial CSR, `rankings_identical`, and per-run `executed_path` (from a
   pre-flight `BM25Field.search` hit count: `"lexical"` if ≥1 hit, `"composite-fallback"` if
   0), per `primitive`. Aggregate to RSR(std), RSR(adv), Adversarial Gap, per-primitive pass
   rates, and per-case query-blindness flags. Write `csr_{date}.{json,md}` + `csr_latest.*`
   into `results/csr/`, mirroring `run_external.save_reports`/`build_markdown_report`. The
   JSON carries `executed_path` and `rankings_identical` per run so a composite fallback can
   never masquerade as a lexical number.
4. **`tests/benchmarks/csr/suites/default.py` — the seed suite (~8 cases).** Small focused
   corpora (8–15 memories each) covering: (a) basic lexical recall (`InTopK`/`CoversTopic`);
   (b) ordering (`RanksAbove` on clearly score-separated items); (c) recency
   (`NoneOlderThan`); (d) **the #409 scenario** — a corpus + a standard query that shares
   vocabulary with the relevant memories and an adversarial paraphrase that does not (but
   does overlap a distractor, keeping BM25 non-empty). On the lexical path this case shows
   **different** rankings for the two queries and a high standard CSR — the healthy
   signature; (e) **a composite control case** — the same corpus planted fresh, run through
   `ContextAssembler(model_class, score_weights={"importance": 1.0},
   retrieval_mode="composite")` (non-empty weights over the model's `SortedField`, otherwise
   `composite_score` raises `QueryException` and ranks nothing — `query.py:516`). Because
   `_pull_path_composite` never reads query text for ranking (`context_assembler.py:
   1180-1248`), the control deterministically shows `ranked_ids(std) == ranked_ids(adv)`
   (Gap = 0 exactly) and a low standard CSR — the #409 query-blind signature the detector
   must fire on. The file header enumerates **#408–#416** with `covered (case_id=...)` /
   `not-applicable (reason)` per issue (see Technical Approach).
5. **`tests/benchmarks/test_csr.py` — the CI gate.** Pytest that (a) runs the full seed suite
   end-to-end with no errors; (b) **determinism**: runs the suite twice and asserts identical
   RSR(std), RSR(adv), and Gap; (c) asserts the report aggregate is well-formed (RSR in
   `[0,1]`, Gap = std − adv, every primitive present); (d) the **discriminative check**: the
   composite control shows `rankings_identical == True` and `csr_std(control) <
   csr_std(lexical #409 case)` on the same corpus, while the lexical #409 case shows
   `rankings_identical == False` and `executed_path == "lexical"` for every scored run —
   i.e. the query-blindness detector fires on the known-blind path and stays quiet on the
   lexical path. Runs under DB-15 isolation; lexical only — no model download, no Redis
   module.
6. **Named constants (experimental-tuning magic numbers, per `feedback_magic_numbers`):** at
   module top of `satisfaction.py`/`run_csr.py` — `DEFAULT_TOP_K = 10`,
   `ASSEMBLER_MAX_ITEMS = 20`, `ADVERSARIAL_GAP_ALERT = 0.30` (reporting threshold above which
   the markdown flags "likely keyword-dependent retrieval" — a **large** gap means keyword
   dependence, not query-blindness), and `QUERY_BLIND_CSR_ALERT = 0.5` (reporting threshold:
   a case with `rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT` is flagged with the
   #409 query-blind signature). Both flags are **reported, not enforced** in the MVP; the CI
   gate's discriminative check uses exact comparisons (ranking equality, control-vs-lexical
   CSR ordering), not these thresholds. Constants live in code, never as user config.
7. **Docs:** a new "Deterministic CSR harness" section in `docs/benchmarks.md` and a short
   `tests/benchmarks/README.md` subsection (structure + how to add a `CsrTestCase`).

### Flow

`pytest test_csr.py` (or `python -m ...csr.run_csr`) → load `suites/default` (token lint) →
per case: plant corpus in DB 15 (+ post-plant adversarial BM25-hit guard) → `assemble()` ×2
(standard + adversarial, `executed_path` recorded) → map records → planted ids →
`evaluate_all` ×2 → per-case CSR + `rankings_identical` → teardown → aggregate
RSR(std)/RSR(adv)/Gap/per-primitive/query-blindness flags → write
`results/csr/csr_{date}.*` (manual path) and assert determinism + shape + the discriminative
check (CI path).

### Technical Approach

- **Why lexical-only (the determinism backbone):** `BM25Field.search(model_class, field_name,
  query_text, limit)` (`bm25_field.py:487`) is pure Lua over Redis sorted sets — no floats
  from an embedding model, no model-version drift, no Redis module, identical on Redis and
  Valkey. With a `BM25Field` and no `EmbeddingField`, `retrieval_mode="auto"` resolves to
  `"lexical"` (`context_assembler.py:946-979`), so `assemble()` is genuinely query-sensitive
  *and* deterministic. Hybrid/embedding CSR is a **No-Go** for the MVP precisely because float
  embeddings + model versioning break "same input → identical score."
- **Gap semantics (what each signal means):** lexical mode is routed through
  `_pull_path_hybrid` (`context_assembler.py:1171-1177`), which ranks by BM25 token overlap —
  so a zero-relevant-token adversarial query legitimately misses the relevant memories and a
  **large Adversarial Gap is the expected, healthy lexical signature** (keyword dependence,
  reported via `ADVERSARIAL_GAP_ALERT`). Query-blindness (#409) has the **opposite**
  signature: `_pull_path_composite` never reads query text for ranking
  (`context_assembler.py:1180-1248` — `query_cues` only feeds an ExistenceFilter existence
  short-circuit, and the CSR model declares no ExistenceFilter), so standard and adversarial
  queries produce the **identical** ranked list — `Gap = 0` exactly — while the standard CSR
  is low because the ranking ignores the query. The detector is therefore
  `rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`, and the discriminating signal
  between a healthy and a query-blind path is **lexical-mode CSR vs composite-mode CSR on the
  standard query**, never the size of the gap.
- **The lexical→composite fallback and the empty-result contract:** on zero BM25 hits (and no
  vector signal), `_pull_path_hybrid` falls back to `_pull_path_composite` with only a
  `logger.warning` (`context_assembler.py:1314-1326`). With the CSR runner's
  `score_weights={}`, that fallback calls `composite_score(indexes={})`, which raises
  `QueryException` (`query.py:516`); `_pull_path_composite` swallows it
  (`context_assembler.py:1216-1218`) and returns `[], []` — so `result.records == []`,
  every `InTopK`/`RanksAbove`/`CoversTopic` assertion fails, and `Excludes`/`NoneOlderThan`
  vacuously pass. This chain is pinned by an explicit test (see Failure Path Test Strategy),
  and two guards keep it out of scored runs: the post-plant adversarial BM25-hit check
  (authoring error at load, not silent degradation) and the per-run `executed_path` record
  (a `"composite-fallback"` run is never reported as a lexical number).
- **CSR tests the mode-resolution MECHANISM, not the shipped default-recipe wiring.** Every
  CSR case plants a bespoke model that hardcodes `BM25Field` — so the harness proves
  `ContextAssembler` + BM25 are query-sensitive when a `BM25Field` is present, but cannot
  catch a regression that removes/misconfigures `BM25Field` on the *real* default recipe
  model (which would silently revert `auto` to composite). That wiring property is a
  one-line unit test best owned next to the recipe, not a planted-corpus benchmark; it is
  filed as an explicit follow-up (see No-Gos) rather than adding a
  `plant(case, model_class=None)` override path, which would couple the harness schema to
  recipe churn for a property a unit test asserts more directly.
- **#408–#416 coverage enumeration (the explicit scope cut).** Issue #418 asks the seed suite
  to cover "the primitives implicated in #408–#416", but a ranked-list assertion DSL can only
  express retrieval-ranking properties. The plan makes the cut explicit instead of silent —
  this exact enumeration is REQUIRED verbatim as the header comment of `suites/default.py`,
  and Task 8 verifies it exists there:
  - **#408** (token-budget counter honesty) — `not-applicable`: token accounting inside
    `assemble()`, not a retrieval-ranking property; already fixed/closed via PR #419.
  - **#409** (query-blind default retrieval) — `covered (case_id=query_blind_409,
    case_id=composite_control_409)`: the harness's central target.
  - **#410** (PolicyCache Q-values alias the DecayingSortedField score slot) —
    `not-applicable`: Q-value storage semantics, not retrieval ranking.
  - **#411** (StreamConsumer PEL never redelivers) — `not-applicable`: stream delivery
    semantics; no ranked list involved.
  - **#412** (concurrent-save index corruption) — `not-applicable`: multi-process write
    race; CSR is single-threaded by design (see Race Conditions).
  - **#413** (lifecycle tick / AccessTracker pollution) — `not-applicable`: lifecycle side
    effects, not pull-path ranking; CSR models declare no lifecycle machinery.
  - **#414** (CMS depth-7 hash rows affine-correlated) — `not-applicable`: frequency-sketch
    accuracy, not a ranking property; CSR models declare no FrequencySketch.
  - **#415** (temporal phase + chi-squared) — `not-applicable`: temporal statistics, not a
    ranked-retrieval property.
  - **#416** (co-occurrence clamp) — `not-applicable` in the MVP: CSR models declare no
    `CoOccurrenceField`, so the graph-propagation branch is inert; a graph-propagation CSR
    case is named follow-up breadth once #416 is fixed.
  The PM check-in (see Appetite) signs off on this narrower interpretation of #418's
  acceptance text explicitly.
- **Assertions bind to `AssemblyResult.records`:** `assemble()` returns an ordered
  `.records` list (`context_assembler.py:142-156`). The harness maps each record to its
  planted id via `record.db_key.redis_key → planted_id` (the exact reverse-map technique at
  `external_base.py:238-245,318-321`), producing the `ranked_ids` list every assertion
  consumes. `InTopK`/`RanksAbove`/`Excludes` are list-position checks; `NoneOlderThan`/
  `CoversTopic` join through `planted_meta`. No private assembler internals are touched.
- **Adversarial rewriting stays deterministic by being *authored data*, not generated.** The
  `adversarial_query` is a human-written paraphrase committed in the suite fixture — it shares
  **no indexed tokens with the relevant memories** but **at least one indexed token with a
  distractor memory** (so BM25 returns hits and the lexical path actually executes — see the
  fallback contract above), and is semantically equivalent to the standard query. Both token
  rules are enforced with the **real BM25 tokenizer** — `from popoto.fields._tokenizer import
  tokenize` (lowercase, `\W+` split, `MIN_TOKEN_LENGTH=3` filter, stop-word removal; the
  single source of truth BM25 uses for indexing and query-term extraction), never a bespoke
  whitespace splitter. The suite-load lint asserts, per case:
  `set(tokenize(case.adversarial_query)) & set(tokenize(m.content)) == set()` for every
  relevant memory `m`, AND a non-empty intersection with the union of all corpus-content
  token sets. There is **no runtime rewrite step, no synonym RNG, no LLM** — so the
  adversarial input is byte-identical every run. (A seeded programmatic synonym-substituter
  is explicitly deferred to Open Questions / follow-up; it reintroduces a determinism surface
  for no MVP benefit.)
- **Tie determinism (the sharp edge):** `bm25_field.py:292` sorts by score **descending**
  with Lua `table.sort`, which is **not stable** — equal-score ties may reorder across runs.
  `ranks_above` on tied scores would be flaky. Mitigation in the MVP is **data design**:
  author each corpus so the ids named in `RanksAbove`/`InTopK` assertions have clearly
  distinct BM25 scores (different term overlap with the query), never ties. The `test_csr.py`
  double-run determinism check is the tripwire: if a suite case is tie-sensitive, the two runs
  diverge and the test fails, forcing the corpus to be fixed. A deterministic secondary
  sort-by-id in `BM25Field` would be a more robust fix but touches `src/popoto/` and is a
  **follow-up**, not in scope here.
- **Report writer:** a thin `build_markdown_report` + `save_reports` pair local to
  `run_csr.py` (do not overload the external harness's writer — different metrics, different
  results dir). JSON carries the full per-case/per-assertion detail plus per-run
  `executed_path` and per-case `rankings_identical` for reproducibility; the markdown carries
  RSR(std), RSR(adv), Gap, the `ADVERSARIAL_GAP_ALERT` flag (keyword dependence), any
  per-case query-blindness flags (`rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`),
  and the per-primitive table.

## Failure Path Test Strategy

### Exception Handling Coverage
- `assemble()` raising for a case must mark that case `status="error"` (not crash the run) and
  the case must surface in the report with zero passed assertions — assert via a deliberately
  malformed case in a unit test.
- `plant()` on a corpus with a duplicate `PlantedMemory.id` must raise a clear `ValueError`
  (ids are the assertion key space; collisions silently corrupt scoring) — assert the raise.
- `plant()` on a case whose `adversarial_query` gets **zero BM25 hits across the whole
  corpus** must raise a clear authoring error (else the lexical run silently degrades to the
  composite fallback) — assert the raise with a deliberately disjoint query.
- **The empty-fallback contract (pinned explicitly):** an assembler with `score_weights={}`
  driven to the composite fallback (zero BM25 hits) must yield `QueryException` from
  `composite_score(indexes={})` (`query.py:516`), swallowed by `_pull_path_composite`
  (`context_assembler.py:1216-1218`), producing `result.records == []` — so
  `InTopK`/`RanksAbove`/`CoversTopic` fail and `Excludes`/`NoneOlderThan` vacuously pass.
  Assert this end-to-end (plant a corpus, assemble with a no-overlap query, check
  `records == []` and the assertion outcomes) so a future popoto change to either link of
  the chain trips a test, not a silent behavior shift.
- A `RanksAbove`/`InTopK` referencing an id **not present** in the planted corpus must fail
  the assertion deterministically (return `False`), never `KeyError` — unit-test both.

### Empty/Invalid Input Handling
- Empty `ranked_ids` (assembler returns nothing): every `InTopK`/`RanksAbove`/`CoversTopic`
  → `False`; `Excludes` → `True`; `NoneOlderThan` → `True` (vacuously). Unit-test each.
- A case with an empty assertion list contributes CSR = 1.0 (0/0 → defined as fully satisfied)
  **or** is rejected at load — decide and pin with a test (recommended: reject at load, since
  an assertion-free case is almost always an authoring mistake).
- `k <= 0` or `k` larger than the corpus: clamp to the available list length; unit-test the
  boundary.

### Error State Rendering
- The markdown report renders an error case as a visible row (not a silent drop); the
  `ADVERSARIAL_GAP_ALERT` flag appears iff `Gap >= ADVERSARIAL_GAP_ALERT`; and the per-case
  query-blindness flag appears iff `rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`
  — assert all rendering branches.

## Test Impact

- [ ] `tests/benchmarks/csr/test_satisfaction.py` — NEW: unit tests for every assertion type
  (pass, fail, missing-id, empty-list, boundary-k). Pure functions, no Redis — fast.
- [ ] `tests/benchmarks/test_csr.py` — NEW: end-to-end seed-suite run + determinism (double
  run identical) + aggregate-shape assertions + the discriminative check (composite control:
  `rankings_identical == True`, lower standard CSR than the lexical #409 case; lexical #409
  case: `rankings_identical == False`, `executed_path == "lexical"` on every scored run) +
  the empty-fallback contract test (`score_weights={}` + zero-hit query →
  `result.records == []` → InTopK/CoversTopic fail, Excludes/NoneOlderThan vacuously pass).
  Requires Redis (DB 15).
- [ ] `tests/benchmarks/csr/test_corpus.py` — NEW: `plant()` key isolation, reverse-map
  correctness, duplicate-id raise, zero-BM25-hit adversarial-query raise, `cleanup()` leaves
  DB 15 clean.
- [ ] Existing `tests/benchmarks/test_external.py`, `test_sweep.py`, `test_harness.py` —
  UNAFFECTED: CSR is additive; no shared mutable surface. Confirm green after the add.
- [ ] No change to any `src/popoto/` test — the harness only consumes the public API.

## Rabbit Holes

- **Building a programmatic adversarial-paraphrase generator.** Tempting, but a synonym/
  back-translation rewriter pulls in a tokenizer or model, reintroduces nondeterminism, and is
  exactly the judge-free guarantee #418 protects. Author paraphrases by hand in the fixture.
- **Making the assertion DSL general-purpose** (boolean combinators, regex matchers, score
  predicates). The five concrete assertion types cover every example the issue names. Stop
  there; add types only when a real case needs one.
- **Fixing BM25 tie-stability in `src/popoto/`.** A secondary sort-by-id in the Lua is a real
  improvement but a separate change with its own blast radius. The MVP routes around ties via
  corpus design + the determinism test; do not expand scope into the field implementation.
- **Hybrid/embedding CSR.** Float embeddings and model versioning break determinism; this is a
  No-Go for the MVP, not a stretch goal to squeeze in.
- **Turning the gap into a hard CI failure threshold now.** We have no calibrated baseline yet;
  a premature `assert gap < X` would be either toothless or flaky. The MVP *reports* the gap
  and gates only on determinism, suite health, and the exact-comparison discriminative check.
  Threshold gating is a follow-up once the seed suite's numbers are observed across a few
  runs.
- **Large curated corpora / many primitives.** A focused ~8-case suite proves the harness and
  catches #409-class regressions. Scaling to hundreds of memories or every primitive is
  follow-up breadth, not MVP correctness.

## Risks

### Risk 1: BM25 score ties make `ranks_above`/`in_top_k` nondeterministic
**Impact:** Lua `table.sort` is unstable (`bm25_field.py:292`); tied scores can reorder run to
run, making a passing suite flaky.
**Mitigation:** Design corpora so assertion-referenced ids have distinct BM25 scores; the
`test_csr.py` double-run determinism check is the standing tripwire that fails fast on any
tie-sensitive case. Document the rule in the README "adding a CsrTestCase" section. A
field-level stable sort is filed as a follow-up, not done here.

### Risk 2: Adversarial-query authoring errors — shared tokens, or zero corpus-wide hits
**Impact:** Two failure modes, in opposite directions. (a) A weak paraphrase that still shares
keywords with the relevant memories makes the adversarial run behave like the standard run,
weakening the case's discriminative value. (b) A paraphrase that shares **no tokens with the
entire corpus** (easy on small 8–15-item corpora) makes `BM25Field.search` return zero hits,
and `_pull_path_hybrid` silently falls back to the query-blind composite path
(`context_assembler.py:1314-1326`, `logger.warning` only) — the "lexical adversarial" number
then measures composite-fallback behavior (an empty result set under `score_weights={}`, per
the fallback contract in Technical Approach), not lexical query-sensitivity.
**Mitigation:** Three layers. (1) A pure suite-load lint using the **real BM25 tokenizer**
(`from popoto.fields._tokenizer import tokenize` — lowercase, `\W+` split, short-token
filter, stop words) asserts per case that `tokenize(adversarial_query)` is set-disjoint from
`tokenize(m.content)` for every relevant memory `m`, AND intersects the union of all corpus
token sets (distractor overlap is allowed and required). (2) A post-plant guard:
`BM25Field.search(model_class, "content", adversarial_query, limit=1)` must return ≥1 hit or
`plant()` raises — an authoring error at load, never a silent runtime fallback. (3) The
runner records per-run `executed_path` (pre-flight BM25 hit count → `"lexical"` /
`"composite-fallback"`), and the CI gate asserts every scored lexical run executed the
lexical path — so a fallback can never masquerade as a lexical result. Plus the
discriminative sanity check in `test_csr.py`: the composite control case (sorted-index field
+ non-empty `score_weights`, so it actually ranks) must show `ranked_ids(std) ==
ranked_ids(adv)` (Gap = 0 — the true query-blind signature) with a standard CSR **below** the
lexical #409 case's, proving the detector (`identical rankings + low standard CSR`)
discriminates query-independence.

### Risk 3: Cross-case Redis contamination inflates/deflates scores
**Impact:** Leftover keys from a prior case pollute a later case's retrieval.
**Mitigation:** Per-case unique-name Model class + `agent_id` partition (the proven
`external_base` pattern), explicit `cleanup()` after each case, and the popoto pytest plugin's
DB-15 `flushdb()` between tests as a backstop. `test_corpus.py` asserts a clean DB after
teardown.

### Risk 4: Valkey divergence
**Impact:** Any Redis-module command (`FT.*`/`BF.*`/`CMS.*`) would break Valkey.
**Mitigation:** Lexical BM25 is pure Lua + core commands; the harness adds none. A Verification
grep over the new package asserts zero module commands.

### Risk 5: Determinism of `assemble()` beyond BM25 ordering
**Impact:** If `ContextAssembler` mixes in a push path (cyclic decay) or recency-decayed
scoring tied to wall-clock, results could vary by run time.
**Mitigation:** The CSR Model declares **no** `CyclicDecayField`, so the push path is inert
(`assemble` skips it when `_cyclic_decay_field_name is None`). Timestamps used by
`NoneOlderThan` are **planted fixture values**, not wall-clock, and are read from
`planted_meta`, not from any decay computation. The double-run determinism test covers the
residual.

## Race Conditions

None. Planting, retrieval, and scoring are synchronous and single-threaded per case; cases run
sequentially. There is no shared mutable state between cases beyond Redis keys, which are
namespaced per case and cleaned between cases. No background tasks, no async, no concurrent
writers.

## No-Gos (Out of Scope)

- **Hybrid / embedding-backed CSR.** Nondeterministic (float embeddings, model versioning) and
  dependency-heavy — contradicts the deterministic-CI mandate. Lexical-only for the MVP. A
  separate issue if ever pursued.
- **LLM-judged or classifier-scored assertions.** Explicitly forbidden by #418 and by this
  harness's reason to exist.
- **Runtime/programmatic adversarial generation** (synonym RNG, back-translation, LLM
  rewrite). Adversarial inputs are authored fixture data only.
- **Hard CI gating on absolute RSR or Gap thresholds.** The MVP reports them and the
  `ADVERSARIAL_GAP_ALERT`/query-blindness flags; it gates only on determinism, suite health,
  and the discriminative check's **exact** comparisons (ranking equality; control-vs-lexical
  CSR ordering) — never on a calibrated numeric threshold. Threshold gating is a follow-up
  after baselines stabilize.
- **Large curated corpora, full primitive coverage, dashboards, trend/ratchet integration.**
  Follow-up breadth.
- **Non-ranking primitives from #408–#416** (#408 token budget, #410 Q-value storage, #411
  PEL redelivery, #412 concurrent-save races, #413 lifecycle side effects, #414 CMS hashes,
  #415 temporal stats, #416 co-occurrence clamp). Structurally outside a ranked-list
  assertion DSL — the explicit per-issue enumeration lives in Technical Approach and is
  mirrored as the `suites/default.py` header comment; PM signs off on the narrowed
  interpretation at the check-in.
- **Default-recipe wiring check.** CSR validates the mode-resolution *mechanism* (every CSR
  model hardcodes `BM25Field`), not that the shipped default recipe model still declares one.
  That property is a narrow follow-up: a unit test next to the recipe asserting the default
  recipe model declares `BM25Field` and that `ContextAssembler(..., retrieval_mode="auto")`
  resolves to `lexical`/`hybrid` for it — **file this as its own issue at ship time**; do not
  thread a `plant(case, model_class=None)` override through the harness for it.
- **Stable-sort fix inside `BM25Field`** (`src/popoto/`). Routed around via corpus design;
  separate issue.
- **JSON external-authoring schema for suites.** MVP suites are typed Python (type-checked, no
  parser). JSON authoring is an Open Question / follow-up.

## Update System

No update/deploy-system changes. The harness is a developer/CI tool under `tests/`; it ships no
runtime code and adds no dependency to the published package.

## Agent Integration

None. No agent/MCP tool surface is touched. The harness exercises the existing
`ContextAssembler` agent-memory primitive read-only.

## Documentation

### Feature Documentation
- [ ] `docs/benchmarks.md` — add a "Deterministic CSR Harness" section: what CSR/RSR/Adversarial
  Gap mean (large gap = keyword dependence; query-blindness = identical rankings + low
  standard CSR), how it complements #394 (deterministic CI vs stochastic LLM-judged), the
  lexical-only rationale, and how to run it (`pytest tests/benchmarks/test_csr.py` and the
  `run_csr` CLI).
- [ ] `tests/benchmarks/README.md` — add `csr/` to the structure tree and a short
  "Adding a CsrTestCase" recipe (corpus → standard/adversarial queries → typed assertions →
  the three authoring rules: no shared tokens with relevant memories, ≥1 distractor token so
  BM25 hits, no score ties).

### External Documentation Site
- [ ] `mkdocs build --strict` passes after the edits (`scripts/ci-local.sh docs`).

### Inline Documentation
- [ ] Module docstrings on `satisfaction.py`, `corpus.py`, `run_csr.py` explaining the
  `(corpus, query, assertions)` model and the determinism guarantees.
- [ ] Each assertion dataclass documents its pass/fail/edge semantics (missing id, empty list,
  boundary k).
- [ ] A header comment in `suites/default.py` carrying the **#408–#416 enumeration**
  (`covered (case_id=...)` / `not-applicable (reason)` per issue), naming the #409-catching
  case pair, and stating the authoring rules: no shared tokens with relevant memories,
  ≥1 token overlap with a distractor (BM25 must hit), no score ties.

## Success Criteria

- [ ] `tests/benchmarks/csr/` exists with `satisfaction.py`, `corpus.py`, `run_csr.py`, and
  `suites/default.py`; all assertion types are implemented and unit-tested.
- [ ] `python -m tests.benchmarks.csr.run_csr` runs the seed suite and writes
  `results/csr/csr_{date}.{json,md}` + `csr_latest.*` reporting RSR(std), RSR(adv), Adversarial
  Gap, and a per-primitive pass-rate table.
- [ ] `pytest tests/benchmarks/test_csr.py` passes and is **deterministic**: two consecutive
  runs produce bit-identical RSR(std), RSR(adv), and Gap.
- [ ] The seed suite includes the #409-style case pair, and the discriminative check holds:
  the **composite control** (query-blind by construction) shows **identical rankings for the
  standard and adversarial queries (Gap = 0)** and a standard CSR **below** the lexical #409
  case's standard CSR, while the lexical case shows **different rankings** and
  `executed_path == "lexical"` on every scored run — i.e. the query-blindness detector
  (`rankings_identical AND low standard CSR`) fires on the known-blind path and stays quiet
  on the lexical path. (A large lexical Adversarial Gap is expected and reported as keyword
  dependence; it is **not** the #409 signature.)
- [ ] Every adversarial query passes both authoring guards: token-disjoint from the relevant
  memories AND ≥1 BM25 hit against the planted corpus (post-plant
  `BM25Field.search(..., limit=1)` guard) — no scored lexical run ever executed the
  composite fallback (`executed_path` recorded per run).
- [ ] `suites/default.py` opens with the **#408–#416 enumeration header comment** (each issue
  marked `covered (case_id=...)` or `not-applicable (reason)`, matching Technical Approach);
  Task 8 verifies its presence.
- [ ] No LLM/judge call, no embedding model, no Redis-module command anywhere in the new
  package (Verification grep passes); runs identically on Redis and Valkey.
- [ ] Numeric constants (`DEFAULT_TOP_K`, `ASSEMBLER_MAX_ITEMS`, `ADVERSARIAL_GAP_ALERT`,
  `QUERY_BLIND_CSR_ALERT`) are named module-level constants, not user config.
- [ ] Docs updated; `mkdocs build --strict` passes.

## Step by Step Tasks

### 1. Scaffold the package and constants
- **Task ID:** scaffold
- **Depends On:** none
- **Validates:** `python -c "import tests.benchmarks.csr"` imports clean
- Create `tests/benchmarks/csr/__init__.py`, `suites/__init__.py`, and a `results/csr/`
  directory (with a `.gitkeep`). Define the module-level constants `DEFAULT_TOP_K`,
  `ASSEMBLER_MAX_ITEMS`, `ADVERSARIAL_GAP_ALERT`, `QUERY_BLIND_CSR_ALERT` with magic-number
  comments.

### 2. Build the assertion engine
- **Task ID:** assertions
- **Depends On:** scaffold
- **Validates:** `tests/benchmarks/csr/test_satisfaction.py` (new)
- Implement `InTopK`, `RanksAbove`, `NoneOlderThan`, `CoversTopic`, `Excludes` dataclasses each
  with `evaluate(ranked_ids, planted_meta) -> bool`, and `evaluate_all(...)`. Unit-test
  pass/fail/missing-id/empty-list/boundary-k for every type (pure, no Redis).

### 3. Build the corpus loader + test-case schema
- **Task ID:** corpus
- **Depends On:** scaffold
- **Validates:** `tests/benchmarks/csr/test_corpus.py` (new)
- Implement `PlantedMemory`, `CsrTestCase` (including the `retrieval_mode`/`score_weights`
  assembler-override fields the composite control case needs — the schema is fixed here
  because Tasks 4–6 depend on it), `plant()` (per-case unique-name Model with
  `BM25Field`/`SortedField importance`/`timestamp`/`topic`, save corpus, build reverse-map,
  then the **post-plant adversarial guard**: `BM25Field.search(model_class, "content",
  case.adversarial_query, limit=1)` must return ≥1 hit or raise) and `cleanup()`. Tests:
  key isolation, reverse-map correctness, duplicate-id raise, zero-BM25-hit adversarial
  raise, clean DB after teardown.

### 4. Build the runner + report writer
- **Task ID:** runner
- **Depends On:** assertions, corpus
- **Validates:** runner produces a well-formed json+md aggregate (asserted in test_csr.py)
- Implement `run_csr.py`: per-case plant → `ContextAssembler(model_class, score_weights={},
  max_items=ASSEMBLER_MAX_ITEMS, retrieval_mode="auto")` (or the case's overrides for the
  composite control) → `assemble()` ×2 (standard, adversarial) with a pre-flight
  `BM25Field.search` hit count recorded as `executed_path` per run → map records →
  `evaluate_all` ×2 → record `rankings_identical` → aggregate RSR(std)/RSR(adv)/Gap/
  per-primitive/query-blindness flags → `save_reports`/`build_markdown_report` into
  `results/csr/` (mirror `run_external` conventions). Include the `ADVERSARIAL_GAP_ALERT`
  and query-blindness flags in the markdown; `executed_path` + `rankings_identical` in the
  JSON.

### 5. Author the seed suite
- **Task ID:** suite
- **Depends On:** assertions, corpus
- **Validates:** suite-load lint (token rules via the real tokenizer, no empty assertion
  list) passes
- Write `suites/default.py` with ~8 cases: basic lexical recall, ordering (`RanksAbove` on
  score-separated ids), recency (`NoneOlderThan`), coverage (`CoversTopic`), the
  **#409-style query-independence case** (`query_blind_409`), and its **composite control**
  (`composite_control_409`: `retrieval_mode="composite"`, `score_weights={"importance":
  1.0}` over the model's `SortedField`). Add the load-time lint using
  `from popoto.fields._tokenizer import tokenize`: per case,
  `set(tokenize(adversarial_query))` is disjoint from every relevant memory's
  `set(tokenize(content))` AND intersects the union of all corpus token sets. Open the file
  with the **#408–#416 enumeration header comment** (verbatim from Technical Approach).

### 6. Wire the CI gate test
- **Task ID:** gate
- **Depends On:** runner, suite
- **Validates:** `pytest tests/benchmarks/test_csr.py`
- Implement `test_csr.py`: full-suite run (no errors), **determinism** (double run → identical
  RSR/Gap), aggregate-shape assertions, the **discriminative check** (composite control:
  `rankings_identical == True` and `csr_std(control) < csr_std(query_blind_409)`; lexical
  #409 case: `rankings_identical == False` and `executed_path == "lexical"` on every scored
  run), and the **empty-fallback contract test** (`score_weights={}` + zero-hit query →
  `result.records == []` → InTopK/CoversTopic fail, Excludes/NoneOlderThan vacuously pass).

### 7. Documentation
- **Task ID:** docs
- **Depends On:** runner, gate
- **Validates:** `scripts/ci-local.sh docs` (`mkdocs build --strict`)
- Add the CSR section to `docs/benchmarks.md`, the `csr/` entry + "Adding a CsrTestCase" recipe
  to `tests/benchmarks/README.md`, and module/assertion docstrings.

### 8. Final validation
- **Task ID:** validate-all
- **Depends On:** docs
- **Validates:** all Success Criteria
- Run `pytest tests/benchmarks/csr/ tests/benchmarks/test_csr.py -q`; Valkey-safety grep over
  the new package; confirm a committed `results/csr/csr_{date}.*` artifact; confirm constants
  are module-level; **verify the #408–#416 enumeration header comment exists in
  `suites/default.py`** and every issue number appears with a `covered`/`not-applicable`
  marking; `git status` stages only the new package, `test_csr.py`, the report, and the docs.

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Package imports | `python -c "import tests.benchmarks.csr; print('ok')"` | `ok` |
| Assertion units pass | `pytest tests/benchmarks/csr/test_satisfaction.py -q` | exit 0 |
| Corpus units pass | `pytest tests/benchmarks/csr/test_corpus.py -q` | exit 0 |
| CI gate passes | `pytest tests/benchmarks/test_csr.py -q` | exit 0 |
| Determinism (two runs identical) | `python -m tests.benchmarks.csr.run_csr --dry-run > /tmp/a.txt 2>&1; python -m tests.benchmarks.csr.run_csr --dry-run > /tmp/b.txt 2>&1; diff <(grep -E 'RSR\|Gap' /tmp/a.txt) <(grep -E 'RSR\|Gap' /tmp/b.txt) && echo IDENTICAL` | `IDENTICAL` |
| Report artifact written | `python -m tests.benchmarks.csr.run_csr && ls tests/benchmarks/results/csr/csr_*.json` | one match |
| Report carries the metrics | `python -c "import json,glob; d=json.load(open(sorted(glob.glob('tests/benchmarks/results/csr/csr_*.json'))[-1])); s=d['summary']; print('rsr_std' in s, 'rsr_adv' in s, 'adversarial_gap' in s)"` | `True True True` |
| Report records executed path per run | `python -c "import json,glob; d=json.load(open(sorted(glob.glob('tests/benchmarks/results/csr/csr_*.json'))[-1])); c=d['cases'][0]; print('executed_path' in c['runs'][0], 'rankings_identical' in c)"` | `True True` |
| #408–#416 enumeration header present | `python -c "src=open('tests/benchmarks/csr/suites/default.py').read(); missing=[n for n in range(408,417) if ('#%d' % n) not in src]; print(missing or 'ok')"` | `ok` |
| No Redis-module commands | `grep -rEn "FT\.\|BF\.\|CMS\." tests/benchmarks/csr/` | no matches (exit 1) |
| No LLM/judge import | `grep -rEn "anthropic\|openai\|sentence_transformers\|EmbeddingField" tests/benchmarks/csr/` | no matches (exit 1) |
| Lexical mode resolved | `python -c "from tests.benchmarks.csr.corpus import plant; from tests.benchmarks.csr.suites.default import SUITE; from src.popoto.recipes.context_assembler import ContextAssembler; mc,ag,_,_=plant(SUITE[0]); a=ContextAssembler(model_class=mc, score_weights={}, retrieval_mode='auto'); print(a._effective_mode)"` | `lexical` |
| Docs build | `mkdocs build --strict` | exit 0 |

## Critique Results

| Severity | Critic | Finding | Addressed By | Implementation Note |
|----------|--------|---------|--------------|---------------------|
| BLOCKER | history-consistency | Success Criterion 4 / Risk 2 inverted the Adversarial Gap semantics: a query-blind composite path produces **identical** rankings for both queries (Gap = 0 exactly, `_pull_path_composite` never reads query text for ranking — `context_assembler.py:1180-1248`), while lexical BM25 shows the **large** gap; also, with `score_weights={}` and no sorted-index field the composite control would rank nothing (`composite_score` raises at `query.py:516`, swallowed at `:1216-1218`) | Problem; Technical Approach ("Gap semantics"); Solution items 2, 4d/4e, 5d, 6; Risk 2; Success Criteria (discriminative bullet); Task 6 | #409 detector reframed as `rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`; discriminating signal = lexical-mode vs composite-mode CSR on the **standard** query; composite control kept, given a `SortedField importance` + `score_weights={"importance": 1.0}` so it actually ranks |
| BLOCKER | risk-robustness | An adversarial query with zero token overlap against the **entire** 8–15-item corpus makes `_pull_path_hybrid` silently fall back to the query-blind composite path (`context_assembler.py:1314-1326`, warning only) — the "lexical adversarial" number would measure composite fallback (empty results with `score_weights={}`) | Data Flow steps 2–4; Solution items 2–3; Risk 2 (three-layer mitigation); Tasks 3–5; Success Criteria (authoring-guards bullet) | Post-plant guard: `BM25Field.search(model_class, "content", adversarial_query, limit=1)` must return ≥1 hit or `plant()` raises; authoring rule requires distractor overlap; per-run `executed_path` recorded in the JSON and asserted `== "lexical"` by the gate |
| CONCERN | risk-robustness | Behavior of the `score_weights={}` composite fallback was unspecified (empty result vs arbitrary order vs raise), so a fallback could mask a bug | Technical Approach ("empty-result contract"); Failure Path Test Strategy; Test Impact; Task 6 | Verified chain: `composite_score(indexes={})` raises `QueryException` (`query.py:516`) → swallowed by `_pull_path_composite` (`context_assembler.py:1216-1218`) → `result.records == []` → InTopK/RanksAbove/CoversTopic fail, Excludes/NoneOlderThan vacuously pass; pinned by an explicit end-to-end test |
| CONCERN | scope-value | #418's acceptance text asks the seed suite to cover "the primitives implicated in #408–#416", but a ranked-list DSL cannot express most of them — the scope cut was silent | Technical Approach ("#408–#416 coverage enumeration"); Solution item 4; No-Gos; Success Criteria (enumeration bullet); Tasks 5 & 8; Appetite (PM sign-off) | Per-issue `covered (case_id=...)` / `not-applicable (reason)` enumeration written in the plan and REQUIRED verbatim as the `suites/default.py` header comment; Task 8 + a Verification row check every issue number appears |
| CONCERN | scope-value | CSR's per-case model hardcodes `BM25Field`, so it validates the mode-resolution mechanism, not the shipped default-recipe wiring; the `plant()` signature question had to be settled before Task 3 | Technical Approach ("mode-resolution MECHANISM" bullet); No-Gos ("Default-recipe wiring check") | Option (a) chosen and justified: a narrow follow-up unit test next to the recipe (assert the default recipe model declares `BM25Field` and `auto` resolves to lexical/hybrid), filed as its own issue at ship time; no `plant(case, model_class=None)` override — keeps the schema stable for Tasks 4–6 |
| CONCERN | history-consistency | Data Flow step 4 called `ContextAssembler(model_class, retrieval_mode="auto", max_items=K)` — missing the required positional `score_weights` (`context_assembler.py:845-857`), contradicting the Verification table | Data Flow step 4; Solution item 3; Task 4 | Call shape fixed everywhere to `ContextAssembler(model_class, score_weights={}, max_items=ASSEMBLER_MAX_ITEMS, retrieval_mode="auto")`, matching the Verification row |
| CONCERN | history-consistency | The no-shared-token lint was specified as "lowercasing/whitespace tokenization", which diverges from BM25's real preprocessing (lowercase, `\W+` split, `MIN_TOKEN_LENGTH=3`, stop words) | Risk 2; Technical Approach ("authored data" bullet); Task 5 | Lint reuses the real tokenizer: `from popoto.fields._tokenizer import tokenize`; asserts `set(tokenize(adversarial_query))` disjoint from every relevant memory's `set(tokenize(content))` AND intersecting the union of corpus token sets |
| NIT | war-room summary | Plan referenced `BM25Field.keyword_search`, which does not exist | Technical Approach ("Why lexical-only") | Corrected to `BM25Field.search(model_class, field_name, query_text, limit)` (`bm25_field.py:487`) |

---

## Open Questions

_Recommended defaults proposed; proceed unless PM overrides:_

1. **Suite authoring format — Python dataclasses vs JSON fixtures?** RECOMMENDED: **typed
   Python dataclasses** for the MVP. Assertions are typed predicates (not naturally JSON), the
   suite is in-repo test data, and Python gives type-checking + the load-time lint for free.
   A declarative JSON schema (`{"type":"in_top_k","memory":"dep-1","k":5}`) for external
   authoring is a clean follow-up once the assertion vocabulary stabilizes.

2. **Report the Adversarial Gap, or gate the build on it?** RECOMMENDED: **report-only** in the
   MVP (`ADVERSARIAL_GAP_ALERT = 0.30` flags keyword dependence in the markdown; the
   query-blindness flag reports `rankings_identical AND csr_std < QUERY_BLIND_CSR_ALERT`;
   `test_csr.py` gates only on determinism, suite health, and the exact-comparison
   discriminative check). We have no calibrated baseline yet, so a hard `assert gap < X`
   would be toothless or flaky. Promote thresholds to a gate in a follow-up once a few runs
   establish the seed suite's stable numbers.

3. **Per-case CSR: all-or-nothing or fractional?** RECOMMENDED: **fractional**
   (passed / total assertions per case), then RSR = mean of case fractions. Fractional gives
   smoother regression signal and partial-credit visibility; all-or-nothing hides which
   assertion broke. (The issue's "Satisfaction *Rate*" framing supports fractional.)

4. **Default top-k for assertions — 10?** RECOMMENDED: **`DEFAULT_TOP_K = 10`**, matching the
   #409 audit's P@10 framing and the recipe's `max_items` defaults, with
   `ASSEMBLER_MAX_ITEMS = 20` so the assembler returns a comfortable superset. Per-assertion
   `k` overrides remain available where a case needs a tighter bound.

5. **Should the CSR gate run on every CI leg, or only when memory code changes?** RECOMMENDED:
   **every leg** — it is lexical-only, dependency-light, and fast (small corpora), so the cost
   is negligible and the always-on tripwire is the whole point. Path-filtering is a premature
   optimization; revisit only if runtime becomes material.
