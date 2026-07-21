---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-07-21
tracking: https://github.com/tomcounsell/popoto/issues/460
last_comment_id:
revision_applied: false
---

# RLT Benchmark — Retrieval Latency & Throughput (flagship native harness, Track A)

## Problem

**Current behavior:** Popoto's benchmark suite (internal sweeps, Tier 1-4 scenarios, the
LongMemEval-S/LoCoMo external harness, the Tier-5 judged-answer harness, and the SIQ harness
being built alongside this plan for #459) all measure **retrieval/injection quality** — did
the right memory come back. None of them measure **latency or throughput**. Popoto's whole
substrate premise — RAM-speed Redis/Valkey memory, no separate vector-service round-trip — is
currently unbenchmarked against competitors on the axis that premise is actually about: speed
under load, not just correctness.

**Desired outcome** (issue #460, strategy `docs/plans/benchmarking_strategy_2026-07.md` §2.2):
a publishable, native RLT harness measuring:

1. **Latency:** p50/p95/p99 per retrieval + end-to-end assemble (retrieve→rank→inject) latency.
2. **Throughput:** queries/sec at a fixed corpus size.
3. **Scaling curve:** latency vs corpus size, 10³ → 2×10⁴ (the maintainer scale ceiling;
   larger points are informational only — do not over-engineer past 20k).
4. **Live mixed workload** (the live-agent-specific axis no static benchmark measures):
   concurrent turn-ingest writes + assembly reads, measuring read-latency degradation under
   write load and vice versa.
5. **Recall-vs-p99 Pareto frontier**, jointly with the retrieval harness: "at equal p99, who
   recalls more; at equal recall, who's faster" — the honest joint artifact, not two
   separately-reported numbers.

Comparators: Mem0 (managed), Zep (Postgres+graph), one vector-DB baseline (pgvector/Qdrant).
Same corpus, same queries, same machine, pinned versions. Constraints: Valkey-safe; results on
**both** Redis and Valkey (first benchmark where the two backends could measurably differ);
machine metadata in artifacts (existing `run_external.py` convention).

## Freshness Check

**Baseline commit:** `6cd452d` (origin/main at worktree creation, 2026-07-21 — includes #461
LLM extraction, #462 graph traversal, #476 index-pointer fix, #480 partition-aware score
proxy).

**Issue filed:** 2026-07-10 (epic #456 batch, alongside #457/#459/#461-#463 — siblings already
planned/shipped per session context; #459 (SIQ) is being planned/built concurrently in a
sibling session on the same machine).

**File:line references re-verified:**
- `tests/benchmarks/run_external.py` — machine-metadata block (`compute_aggregate()`,
  `"machine": {python_version, platform, cpu_count}`), `POPOTO_BENCH_DB` isolation pattern
  (default 14, rejects 0), `save_reports()` JSON+MD artifact convention with `_latest` pointers.
  Confirmed current — this plan's artifact schema extends the `"machine"` block with a
  `"backend"` field (redis/valkey + version) that does not exist yet anywhere in the repo.
- `tests/benchmarks/scenarios/external_base.py` — `ExternalScenario` / per-item uniquely
  prefixed Model class pattern (`_build_external_model_class`), `ContextAssembler(...,
  retrieval_mode="auto")` usage, `teardown()` key-sweep. Confirmed as the pattern this plan's
  corpus-builder reuses for the RLT Model class.
- `src/popoto/recipes/context_assembler.py:1212-1251` — `assemble()` signature. Confirmed
  `t0 = time.time()` already exists inside `assemble()` (used for `metadata["assembly_ms"]`
  or similar downstream) — re-verified the method returns an `AssemblyResult` whose `metadata`
  is a plain dict, giving the harness a place to read end-to-end latency without needing to
  time `assemble()` from outside (though the harness will time from outside anyway, for control
  over what "end-to-end" includes — see Technical Approach).
- `tests/benchmarks/metrics/retrieval.py` — `recall_at_k()`, `precision_at_k()`, `mrr()`. Pure
  functions, no Redis dependency. Confirmed as the reuse point for the recall half of the
  Pareto frontier (RLT computes p99 latency; recall is computed by calling these functions over
  RLT's own retrieved/relevant sets on the same corpus/queries).
- `tests/test_stress.py` — existing threading-based concurrent-write test pattern in the repo
  (`import threading`... concurrent `save()` calls against `POPOTO_REDIS_DB`). Confirmed as
  prior art for the mixed-workload harness's thread pool approach; redis-py's connection pool
  is thread-safe (each thread checks out its own connection), so concurrent reads/writes from a
  `ThreadPoolExecutor` are safe without additional locking in the harness itself.
- `docs/benchmarks.md` — exists (documents Tiers 1-5 + CSR + external harness + SIQ once #459
  lands). Confirmed as the target for the new "RLT" section (required before merge, do-docs
  gate).
- **Machine-contention constraint** (relayed by the PM, not independently re-verified via
  `gh`/`ps` since this is a cross-process constraint outside git history): a heavy sequential
  LoCoMo+LongMemEval benchmark chain is reported running on this same machine against the
  external-benchmark DB (DB 14) for the duration of this session. Because RLT measures wall-clock
  latency, any real measurement run sharing the machine with that chain would be invalid in
  both directions. This plan treats that as a hard constraint on **what work happens now**
  (build harness + fast unit tests only) rather than something to re-verify — the mitigation
  (never touch DB 14, defer real runs) is correct regardless of whether the chain is still
  running by build time.

**Active plans in `docs/plans/` overlapping this area:** `siq_benchmark.md` (#459) is a sibling
harness under `tests/benchmarks/` built concurrently in this window — no file overlap expected
(SIQ owns `tests/benchmarks/siq/`, RLT owns `tests/benchmarks/rlt/`), but both plans touch
`docs/benchmarks.md` additively (new sections) and both may touch `tests/benchmarks/README.md`.
Flagged as a **merge-order** risk (see Risks), not a scope conflict — coordinate via whichever
PR lands second doing a small rebase-and-append to those two shared files rather than a
conflicting rewrite.

**Disposition:** Minor drift (SIQ sibling plan noted; nothing else moved). Proceeding to
Phase 1.

## Prior Art

- **`run_external.py` / `external_base.py`** (#437, #455, #458) — established the artifact
  convention (JSON+MD, `_latest` pointers, machine metadata, `--dry-run`), the DB-isolation
  discipline (dedicated bench DB, never db0), and the per-item uniquely-prefixed Model class
  pattern. RLT's corpus builder and artifact writer follow this shape directly.
- **`tests/benchmarks/csr/`** (#418) — deterministic, pytest-native (DB 15), fast harness with
  hand-verified expected values checked into tests. RLT's **unit tests** (not its headline
  measurement runs) follow this discipline: tiny synthetic corpora, millisecond runtime, DB 15
  only.
- **Issue #465 / README "External-harness DB isolation & residue"** — the two prior DB-0 leak
  incidents from running harness code outside pytest. Directly informs this plan's absolute
  rule: any standalone/manual RLT measurement script must require an explicit non-zero,
  non-14 DB argument (reject both), mirroring `_resolve_bench_db()`'s db0 rejection extended to
  also reject 14 by default for this specific harness (14 is reserved for the concurrently
  running #457-chain per the contention constraint).
- **`docs/plans/siq_benchmark.md`** (#459) — sibling plan in this same batch; establishes the
  precedent (in this same session) for scoping a flagship native harness as harness+fixtures
  +unit-tests now, real/competitor runs as a tracked follow-up, when project constraints (there:
  no live-service adapters wired; here: machine contention) block a full real-numbers run in
  the same PR.
- No prior closed issue/PR attempted latency/throughput benchmarking anywhere in this repo —
  greenfield within the benchmark suite.

## Research

No relevant external findings beyond what's already cited in
`docs/plans/benchmarking_strategy_2026-07.md` §2.2 (Mem0/Zep/vector-DB comparator framing).
This plan's technical approach (percentile computation, mixed-workload thread pool, scaling
curve runner) is standard benchmarking methodology already well-covered by codebase context
(`tests/test_stress.py`'s threading pattern, `run_external.py`'s p50/p95 computation at
`compute_aggregate()` lines ~382-392) — no new library or API research needed. Proceeding on
codebase context.

## Data Flow

1. **Corpus builder (`rlt/corpus.py`):** builds a synthetic corpus of *N* memory records (sized
   per the scaling-curve step, 10³-2×10⁴) into a uniquely-prefixed Popoto Model class (mirrors
   `_build_external_model_class`), with a small deterministic query set (fixed-size, e.g. 50
   queries) whose ground-truth relevant ids are known (planted, not derived) — reusing the CSR
   pattern of authored ground truth rather than inferring it, so `recall_at_k()` from
   `metrics/retrieval.py` can be called directly against RLT's own retrieval results for the
   Pareto step.
2. **Latency runner (`rlt/latency.py`):** for a given corpus + query set, runs each query
   through `ContextAssembler.assemble()`, times each call (`time.perf_counter()`, not
   `time.time()` — monotonic, immune to wall-clock adjustment, matching stdlib best practice
   for interval timing), and computes p50/p95/p99 over the collected latencies via a pure
   `percentile(values, p)` function (no numpy dependency — this project's benchmark code stays
   dependency-light per `metrics/retrieval.py`'s existing pure-function convention).
3. **Throughput runner (`rlt/throughput.py`):** fixed corpus size, runs queries back-to-back
   (single-threaded baseline) and reports queries/sec = `n_queries / total_elapsed_seconds`;
   optionally repeats with a `ThreadPoolExecutor(max_workers=W)` to report throughput under
   concurrency (bounded `W`, default small, e.g. 4-8 — this is a benchmark tuning constant, not
   user config).
4. **Scaling-curve runner (`rlt/scaling.py`):** iterates the corpus builder across a list of
   corpus sizes (default `[1_000, 5_000, 10_000, 20_000]`, informational sizes beyond 20k
   supported but never exercised by the default/CI path), building a fresh corpus at each size,
   running the latency runner, and returning `[{corpus_size, p50, p95, p99}, ...]`. Rebuilds
   (not incrementally grows) the corpus at each size for simplicity and to avoid decay-state
   drift between sizes — an explicit `# corpus is rebuilt fresh per size, not grown
   incrementally` comment documents the choice.
5. **Mixed-workload runner (`rlt/mixed_workload.py`):** given a corpus, spins up two
   `ThreadPoolExecutor` pools (or one pool with mixed task types) — a **write** stream
   (continuous `Model.save()` calls simulating turn-ingest) and a **read** stream (continuous
   `assemble()` calls) running concurrently for a bounded duration or bounded call count
   (whichever is smaller, default bounded by count for determinism in unit tests). Collects
   separate latency distributions for reads-under-write-load and writes-under-read-load, plus a
   reads-only and writes-only baseline in the same run for a degradation ratio
   (`degraded_p99 / baseline_p99`).
6. **Pareto computation (`rlt/pareto.py`):** given a set of `(config_label, p99_ms,
   recall_at_k)` tuples (one per retrieval-mode/comparator run), computes the Pareto-optimal
   frontier (points not dominated by any other point on both axes — lower p99 AND higher
   recall) via a pure function, independently unit-testable with hand-constructed point sets.
7. **Comparator adapter interface (`rlt/comparators.py`):** a `RltAdapter` Protocol
   (`ingest(record) -> None`, `query(q) -> (List[str], latency_ms)`) mirroring `judge.py`'s
   lazy-import/optional-dependency pattern from Tier 5. Ships the Protocol + one
   `NativeAdapter` (wraps steps 2-3 above against Popoto) + one `NullAdapter` (returns empty
   results with a fixed synthetic latency — a dependency-free scaffold proving the Protocol
   contract end-to-end in a unit test without any real competitor). Real Mem0/Zep/vector-DB
   adapters are out of scope for this PR (see No-Gos) and tracked as a follow-up issue.
8. **Artifact writer (`rlt/run_rlt.py`, CLI):** mirrors `run_external.py`'s shape — argparse,
   `--corpus-sizes`, `--mixed-workload`, `--backend {redis,valkey}` (informational label; the
   actual connection still targets whatever `REDIS_URL` points at — the harness does not spin
   up a Valkey server itself, see Technical Approach), `--db` (required, explicit, rejects 0
   and 14 by default), `--dry-run`. Writes `results/rlt/rlt_{date}_{backend}.{json,md}` +
   `_latest` pointers. This is the entrypoint that runs the **real** headline measurements —
   deliberately NOT invoked by this PR's CI/pytest path (see Rabbit Holes / No-Gos).
9. **CI-facing unit tests (`test_rlt.py`):** tiny synthetic corpora (tens of records, not
   thousands), pytest DB-15 isolation exclusively, validating the pure math (percentile,
   throughput, Pareto) and the harness plumbing (scaling-curve runner with e.g.
   `corpus_sizes=[5, 10]`, mixed-workload runner with e.g. 2 threads / 10 calls) — never the
   real 10³-2×10⁴ range, never DB 14.

## Architectural Impact

- **New dependencies:** none required for the default suite. No numpy — percentile/statistics
  computed with pure Python (`statistics` stdlib module is available and sufficient for
  p50/p95/p99 over a sorted list; used in place of hand-rolled index math where it doesn't
  compromise the "no tolerance-band flakiness" property of a unit test with a tiny fixed
  sample).
- **Interface changes:** none to `src/popoto/` — `ContextAssembler.assemble()` and
  `Model.save()` already expose everything the harness needs; this is a pure test/benchmark
  addition.
- **Coupling:** `tests/benchmarks/rlt/` depends on `tests/benchmarks/metrics/retrieval.py`
  (import, no duplication, for the Pareto recall axis) and mirrors
  `tests/benchmarks/scenarios/external_base.py` conventions (uniquely-prefixed Model class,
  teardown key-sweep) without importing it — RLT's corpus shape (flat synthetic records, no
  conversation turns) differs enough from `ExternalScenario`'s conversation-turn shape that
  copying the *pattern* is right, not sharing the class.
- **Data ownership:** none — benchmark-only, no production schema changes.
- **Reversibility:** fully additive; deleting `tests/benchmarks/rlt/` reverts cleanly.

## Appetite

**Size:** Medium

**Team:** Solo dev (this session)

**Interactions:**
- PM check-ins: 0-1
- Review rounds: 1 (do-pr-review gate)

## Prerequisites

No prerequisites. The optional `--backend valkey` CLI path requires a Valkey server reachable
via `REDIS_URL` at manual-run time — not required for this PR's CI-facing tests, which run
against whatever Redis/Valkey the pytest DB-15 fixture already targets (no new infra).

## Solution

### Key Elements

- **`tests/benchmarks/rlt/__init__.py`** — package marker.
- **`tests/benchmarks/rlt/corpus.py`** — `build_corpus(model_class_factory, n, seed=0) ->
  RltCorpus` (records + a fixed deterministic query set with authored ground truth), reusing
  the uniquely-prefixed-Model-class pattern; `teardown_corpus()` scans/deletes by prefix.
- **`tests/benchmarks/rlt/latency.py`** — `percentile(values, p) -> float` (pure); `measure_
  latency(assembler, queries, agent_id) -> LatencyReport` (p50/p95/p99 + raw sample count) for
  both per-retrieval and end-to-end-assemble timing (they are the same call in this harness —
  `assemble()` already *is* retrieve→rank→inject end to end; the plan does not invent a
  separate "per-retrieval" sub-timer inside `assemble()` since that would require changing
  `src/popoto/`, which is explicitly out of scope — see Technical Approach for how "per
  retrieval" vs "end-to-end assemble" are both satisfied by timing `assemble()` itself, which
  *is* the full retrieve→rank→inject pipeline).
- **`tests/benchmarks/rlt/throughput.py`** — `measure_throughput(assembler, queries, agent_id,
  concurrency=1) -> ThroughputReport` (queries/sec, optionally via `ThreadPoolExecutor`).
- **`tests/benchmarks/rlt/scaling.py`** — `run_scaling_curve(corpus_sizes, ...) ->
  List[ScalingPoint]`.
- **`tests/benchmarks/rlt/mixed_workload.py`** — `run_mixed_workload(model_class, corpus,
  n_write_calls, n_read_calls, n_threads) -> MixedWorkloadReport` (baseline read/write latency,
  degraded read/write latency, degradation ratios).
- **`tests/benchmarks/rlt/pareto.py`** — `pareto_frontier(points: List[ParetoPoint]) ->
  List[ParetoPoint]` (pure function, minimizing p99 and maximizing recall).
- **`tests/benchmarks/rlt/comparators.py`** — `RltAdapter` Protocol, `NativeAdapter`,
  `NullAdapter`.
- **`tests/benchmarks/rlt/run_rlt.py`** — CLI entry point (argparse, `--db` required/explicit,
  rejects `0` and `14`, `--corpus-sizes`, `--mixed-workload`, `--backend {redis,valkey}` label,
  `--dry-run`) + `save_reports()` (JSON+MD, `_latest` pointers) mirroring `run_external.py`.
  **Not invoked by CI** — manual/ad-hoc entrypoint for the real measurement runs, deferred per
  the machine-contention constraint (see Rabbit Holes).
- **`tests/benchmarks/test_rlt.py`** — CI-facing pytest suite (DB 15 only): `percentile()` unit
  tests (hand-computed expected values, e.g. `percentile([1,2,...,100], 50) == 50` or similar
  documented convention), throughput calculation tests, scaling-curve runner test with tiny
  synthetic sizes (`[5, 10]`, not `[1000, ..., 20000]`), mixed-workload harness test with small
  thread/call counts (asserts it runs to completion and produces both baseline and
  degraded reports — not asserting specific latency numbers, which would be flaky; asserts
  structural correctness: report has both read and write distributions, degradation ratio is a
  positive float), Pareto-frontier computation tests (hand-constructed point sets with a known
  correct frontier), artifact JSON round-trip serialization test (`save_reports()` writes valid
  JSON matching the documented schema, `--dry-run` writes nothing).
- **`docs/benchmarks.md`** — new "RLT (Retrieval Latency & Throughput)" section: what it
  measures (5 metrics), how to run the unit tests (`pytest tests/benchmarks/test_rlt.py`) vs.
  the real measurement CLI (`python -m tests.benchmarks.rlt.run_rlt`, explicitly marked
  "not run in this PR — see follow-up issue #TBD"), the artifact schema, the Redis-vs-Valkey
  backend labeling convention, and an explicit "no fabricated numbers; real headline
  measurement + real Mem0/Zep/vector-DB comparator runs tracked in follow-up issue #TBD" note.

### Flow

Corpus builder → `assemble()` calls (timed) → `latency.py`/`throughput.py`/`scaling.py`/
`mixed_workload.py` reports → `pareto.py` (joint with `metrics/retrieval.py` recall) →
aggregate dict → `test_rlt.py` asserts structural/pure-math correctness on tiny synthetic data;
`run_rlt.py` (manual, not CI) optionally writes `results/rlt/rlt_{date}_{backend}.{json,md}`
against a real corpus once the machine is confirmed quiet.

### Technical Approach

- **"Per retrieval" vs "end-to-end assemble" latency are the same measured call.**
  `ContextAssembler.assemble()` already *is* the full retrieve→rank→inject pipeline (confirmed
  in Freshness Check) — there is no separate lower-level "just the pull query" API surface to
  time independently without changing `src/popoto/`, which is out of scope. The plan satisfies
  metric (1) by timing `assemble()` end to end and reporting it under both labels in the
  artifact (`"assemble_latency"` is the canonical measurement; `"retrieval_latency"` is
  documented as an alias for the same number in this harness, with a note explaining why, so a
  reader isn't confused into thinking two different pipelines were measured).
- **Percentile computation uses `statistics.quantiles()`** (stdlib, no numpy) with
  `n=100, method="inclusive"` for p50/p95/p99, falling back to `sorted(values)[int(len(values)
  * p/100)]` (nearest-rank) only if a fixture size makes `statistics.quantiles()` raise (it
  requires ≥2 data points) — unit tests cover both code paths explicitly with tiny + single-
  element inputs.
- **Mixed workload uses `concurrent.futures.ThreadPoolExecutor`**, matching the stdlib-only,
  no-new-dependency posture and `tests/test_stress.py`'s existing threading precedent.
  redis-py's connection pool is thread-safe (confirmed in Freshness Check) so no additional
  locking is needed in the harness; each worker thread calls `Model.save()` or
  `assembler.assemble()` independently.
- **Backend labeling (`--backend {redis,valkey}`) is informational, not a server-spin-up.**
  The harness connects to whatever `POPOTO_REDIS_DB`/`REDIS_URL` already points at (same
  connection-swap pattern as `run_external.py`'s `_point_connection_at_db`, extended only to
  also validate/reject DB 14 by default) — it does not provision a Valkey instance. Running the
  "both backends" requirement (issue constraint) means running the CLI twice, once against a
  Redis instance and once against a Valkey instance, both supplied externally by whoever runs
  it (documented in `docs/benchmarks.md`, tracked as part of the deferred real-run follow-up).
  `aggregate["machine"]["backend"]` records the server's `INFO server` `redis_version` /
  presence of a Valkey-specific field (`redis_conn.info().get("redis_version")` plus a
  best-effort `"valkey"` substring check against `INFO server`'s `os`/`redis_mode`-adjacent
  fields — Valkey's `INFO` reports its own version string distinctly from Redis, verified via
  `redis-cli INFO server` locally showing `redis_version:` populated by both servers under that
  same key name for compatibility, so the harness treats an explicit `--backend` CLI flag as
  the authoritative label and the `INFO`-derived string as a cross-check note, not the source
  of truth — avoids over-engineering server-type sniffing).
- **DB hygiene for the manual CLI (`run_rlt.py`):** `--db` is a **required** argument (no
  default, unlike `run_external.py`'s `POPOTO_BENCH_DB` default-14 pattern) — this harness
  explicitly does not default to 14, because 14 is the DB the concurrently running #457 chain
  uses per the contention constraint. Passing `--db 0` or `--db 14` raises a `ValueError`
  before any connection is made, mirroring `_resolve_bench_db()`'s db0 rejection extended to
  also reject 14 for this specific entrypoint (a comment explains why 14 specifically, so a
  future reader isn't confused when `run_external.py` itself defaults to 14).
  `--dry-run` never opens a corpus-writing connection choice at all — it validates arguments
  and prints the plan without connecting.
- **Numeric constants are experimental tuning constants** (corpus sizes, thread counts, call
  counts, latency-report bucket count) — hardcoded module-level constants with docstring
  rationale, not env vars or user config, per project convention.

## Failure Path Test Strategy

### Exception Handling Coverage
- `latency.py`'s `measure_latency()` catches per-query `assemble()` exceptions the same way
  `run_external.py`'s `run_item()` does (capture as an error result, continue the loop) — a
  single failing query never aborts the whole latency run. Unit test: inject one query with a
  malformed `agent_id` type expected to raise, assert the report still returns with `n_errors
  == 1` and valid percentiles over the remaining successful samples.
- `mixed_workload.py`: a worker-thread exception is captured (not silently swallowed — logged
  and counted) via `ThreadPoolExecutor.submit()` + `future.result()` inspection in the
  collection loop, not a bare `except: pass` inside the worker function itself.
- `run_rlt.py`'s `--db` validation raises `ValueError` (not silently defaulting) for `0`, `14`,
  or non-integer input — unit test asserts each.

### Empty/Invalid Input Handling
- `percentile([], p)` — defined behavior: raises `ValueError` with a clear message (mirrors
  `statistics.quantiles([])`'s own behavior) rather than silently returning `0.0`, since a
  silent zero would be indistinguishable from "the actual measured latency was zero" in a
  downstream artifact. Unit test asserts the raise.
- `pareto_frontier([])` returns `[]` (documented, tested) rather than raising — an empty point
  set has a well-defined (empty) frontier.
- `run_scaling_curve(corpus_sizes=[])` returns `[]` (tested) — no corpus built, no error.

### Error State Rendering
- Not user-facing (benchmark/test code) — `run_rlt.py` failures print to stderr and exit
  non-zero, matching `run_external.py`'s posture.

## Test Impact

No existing tests affected — greenfield addition (`tests/benchmarks/rlt/` and
`tests/benchmarks/test_rlt.py` are new files; no existing scenario, adapter, or metrics module
is modified). `docs/benchmarks.md` and `tests/benchmarks/README.md` get additive new sections
(coordinate with the concurrently-landing SIQ PR on merge order for these two shared files —
see Risks).

## Rabbit Holes

- **Do not run the real headline latency/throughput/scaling/mixed-workload measurements in
  this PR.** The machine-contention constraint (a concurrently running heavy sequential
  benchmark chain sharing this machine) makes any real measurement invalid in both directions
  right now. `run_rlt.py` is built, tested for correctness on tiny synthetic data, and
  documented — but not invoked for a real run. A follow-up issue tracks running it for real
  once the machine is confirmed quiet, targeting an explicitly isolated DB.
  Deferral reason: **infrastructure-blocked** (shared-machine contention, not a scope or
  design gap).
- **Do not build real Mem0/Zep/vector-DB comparator adapters in this PR.** Each is a
  heavyweight optional dependency + a live external service (managed API key, Postgres+graph
  deployment, or a vector-DB instance) — wiring all three is explicitly a follow-up, not
  blocking this harness landing. The `RltAdapter` Protocol + `NullAdapter` is sufficient to
  prove the extension point now, matching the SIQ sibling plan's identical posture for its own
  competitor adapters.
  Deferral reason: **scope-narrowing** — the appetite is a harness + fixtures + native
  measurement code, not a live four-way (native + 3 competitors) bake-off requiring live
  service provisioning.
- **Do not attempt to spin up or manage a Valkey server from within the harness.** The
  `--backend` flag is a label for whatever server the caller already pointed `REDIS_URL` at;
  provisioning Valkey (a separate server binary/container) is an operational concern outside
  this harness's scope.
  Deferral reason: **out-of-scope**.
- **Do not exceed the 20k corpus-size ceiling in the default/CI scaling-curve path.** Points
  beyond 20k are explicitly informational-only per the issue; building a distributed/sharded
  corpus generator to reach larger scales would be over-engineering relative to the maintainer
  scale target.
  Deferral reason: **out-of-scope**.

## Risks

### Risk 1: Machine contention makes "no real measurement in this PR" a hard requirement,
risking an incomplete-feeling deliverable
**Impact:** The issue's headline ask (actual latency numbers) is not satisfied by this PR.
**Mitigation:** This is the PM-directed scope for this session (explicit constraint, not a
process gap) — the plan builds 100% of the measurement code + unit tests + artifact schema +
docs, and files a tracked follow-up issue for the real runs. The harness is fully exercised by
unit tests on synthetic data, so the *code path* that will produce real numbers later is
verified correct now; only the *numbers themselves* are deferred.

### Risk 2: Merge-order conflict with the concurrently-landing SIQ PR (#459) on shared files
(`docs/benchmarks.md`, `tests/benchmarks/README.md`)
**Impact:** Whichever PR merges second may need a small rebase to avoid clobbering the other's
additive section.
**Mitigation:** Both plans add new, clearly-delimited sections (own headings) to these two
files rather than rewriting existing content — a rebase-and-append is mechanical, not a real
conflict. Flagged explicitly here so the build/merge step checks `git log` for the sibling PR
before opening this one's PR.

### Risk 3: Thread-based mixed-workload measurement could be dominated by Python GIL contention
rather than genuine Redis/Valkey server-side latency, producing a misleading "degradation"
number
**Impact:** A published mixed-workload number that actually reflects client-side GIL
scheduling, not server behavior, would misrepresent Popoto's real concurrent-load
characteristics.
**Mitigation:** Out of scope to fully solve in this harness-building PR (would require a
multi-process, not multi-thread, load generator to fully isolate GIL effects) — documented as
an explicit caveat in `docs/benchmarks.md`'s RLT section ("mixed-workload latency includes
client-side thread-scheduling overhead; a multi-process load generator is a candidate follow-up
if server-only isolation is needed") rather than silently presented as a clean server-only
number. This keeps the harness honest without over-engineering process-based load generation
into this appetite.

## Race Conditions

- **Mixed-workload read/write threads share the same corpus/Model class.** Established
  ordering: the corpus is fully planted (all initial records saved) *before* the mixed-workload
  threads start, so read threads always have a non-empty baseline to query even before any
  concurrent write lands — read results during the mixed phase are expected to vary (that's the
  point of the measurement) but never crash on an empty corpus. Write threads use `AutoKeyField`
  (mirrors existing per-item patterns), so concurrent writes never collide on a key. No shared
  mutable Python state between threads beyond thread-safe collections
  (`concurrent.futures.Future` results collected after `as_completed()`), so no explicit lock is
  needed in the harness.
- **`ThreadPoolExecutor` shutdown ordering:** the harness waits for all submitted futures
  (`concurrent.futures.wait()` / iteration over `as_completed()`) before computing percentiles,
  so no measurement is taken on a latency list still being appended to by a background thread.

## No-Gos (Out of Scope)

- **Real headline latency/throughput/scaling/mixed-workload measurement runs against a real
  corpus** — tag: **follow-up-issue** (tracked as a new GitHub issue filed at PR time,
  referencing this plan; run once the machine is confirmed quiet, targeting an explicitly
  isolated DB, never DB 0 or DB 14).
- **Real Mem0/Zep/vector-DB comparator adapter implementations and a live competitor
  comparison run** — tag: **follow-up-issue** (same tracked issue as above, or a dedicated one
  — decided at PR time; the `RltAdapter` Protocol is the extension point).
- **Corpus sizes beyond ~20k in the default/CI path** — tag: **out-of-scope** (project ceiling
  per issue constraints; larger points are informational-only per the issue, not required).
- **Changing `ContextAssembler`'s or `Model`'s public API** — tag: **out-of-scope** — nothing
  in this harness requires new instrumentation hooks; `assemble()` and `save()` already expose
  everything needed via external timing.
- **Provisioning/spinning up a Valkey server from the harness** — tag: **out-of-scope** — the
  `--backend` flag is a label over an externally-supplied connection.
- **A CI gate that runs `run_rlt.py`'s real-corpus measurement path automatically** — tag:
  **deferred** — mirrors the SIQ/Tier-5 posture: real measurement runs are opt-in/manual, never
  a required CI step (and, specifically for this plan, not run at all in this PR per the
  machine-contention constraint).

## Open Questions

None — design is fully specified by the issue body, the PM's explicit machine-contention scope
constraint, and mirrors an established in-repo pattern (Tier 5 / CSR / the concurrently-planned
SIQ sibling) closely enough that no unresolved judgment calls remain for the human. Proceeding
to critique.
