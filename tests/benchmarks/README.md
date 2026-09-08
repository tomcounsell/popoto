# Benchmark Harness for Agent-Memory Constants

Systematic parameter sweep framework for tuning Popoto's ~25 behavioral constants,
plus an external benchmark harness for evaluating retrieval quality against
published datasets (LongMemEval-S, LoCoMo).

## Structure

```
tests/benchmarks/
    conftest.py              # Redis fixtures and cleanup
    overrides.py             # Constant override injection context manager
    sweep.py                 # ParameterGrid, SweepRunner, ResultsAggregator
    run_sweeps.py            # CLI entry point for internal parameter sweeps
    run_external.py          # CLI entry point for external dataset benchmarks
    test_harness.py          # Tests for scenarios, metrics, overrides
    test_sweep.py            # Tests for sweep engine
    test_external.py         # Tests for external benchmark (fixture-based, no network)
    test_csr.py              # CI gate for the deterministic CSR harness
    test_siq.py              # CI gate for the SIQ (query-blind injection) harness
    test_rlt.py               # CI gate for the RLT (latency/throughput) harness
    rlt/
        __init__.py           # RLT overview (5 metrics -> 1 submodule each)
        corpus.py              # Synthetic lexical corpus + authored-ground-truth queries
        latency.py             # percentile(), measure_latency() (p50/p95/p99)
        throughput.py          # measure_throughput() (queries/sec, optional concurrency)
        scaling.py              # run_scaling_curve() (latency vs. corpus size)
        mixed_workload.py       # run_mixed_workload() (concurrent ingest + reads)
        pareto.py                # pareto_frontier() (recall-vs-p99 joint artifact)
        comparators.py           # RltAdapter Protocol + NativeAdapter + NullAdapter
        run_rlt.py                # CLI entry point (--db required, rejects 0/14/15)
    siq/
        __init__.py          # SIQ overview + design invariants
        corpus.py            # SiqTrace/SiqTurn/SiqMemory schema, load, lint, plant
        metrics.py           # precision/recall @ budget, anticipation lead, efficiency
        adapters.py          # SiqAdapter Protocol + NativeAdapter + QueryOnlyStubAdapter
        runner.py            # turn-by-turn replay driver
        run_siq.py           # CLI entry point + report writer (bench DB, db0 rejected)
        fixtures/
            *.json           # Deterministic committed multi-turn traces (no runtime RNG)
    csr/
        __init__.py          # CSR constants (DEFAULT_TOP_K, alert thresholds)
        satisfaction.py      # Assertion engine (InTopK, RanksAbove, ...)
        corpus.py            # PlantedMemory/CsrTestCase schema, plant(), lint
        run_csr.py           # CLI entry point + report writer
        suites/
            default.py       # Seed suite (~8 cases, #408-#416 enumeration)
    metrics/
        retrieval.py         # precision@k, recall@k, nDCG, calibration error, MRR
    scenarios/
        base.py              # Base Scenario class
        external_base.py     # ExternalScenario for dataset-driven benchmarks
        factual_recall.py    # Factual knowledge retrieval
        multi_step_reasoning.py  # Co-occurrence chain retrieval
        temporal_scheduling.py   # Cyclic decay task scheduling
    datasets/
        __init__.py          # BenchmarkItem namedtuple
        longmemeval_s.py     # LongMemEval-S adapter (500 questions)
        locomo.py            # LoCoMo adapter (1986 QA pairs, 10 dialogues)
        fixtures/
            longmemeval_s_sample.json  # 3-question fixture (offline testing)
            locomo_sample.json         # 2-dialogue fixture (offline testing)
    results/
        sweep_*.json         # Timestamped internal sweep results
        latest.json          # Symlink to most recent internal sweep
        external/
            longmemeval_s_*.{json,md}  # External benchmark reports
            locomo_*.{json,md}         # External benchmark reports
        csr/
            csr_*.{json,md}            # Deterministic CSR reports
        siq/
            siq_*_{adapter}.{json,md}  # SIQ reports (native / query_stub adapters)
        rlt/
            rlt_*_{backend}.{json,md}  # RLT reports (redis committed; valkey deferred — see docs/benchmarks.md)
```

## Quick Start

```bash
# Internal parameter sweeps (no network, ~6 seconds)
python -m tests.benchmarks.run_sweeps --tier all --interactions

# External benchmark (requires dataset download + Redis):
python -m tests.benchmarks.run_external --dataset longmemeval-s
python -m tests.benchmarks.run_external --dataset locomo

# Point the external harness at a different bench DB (default 14):
POPOTO_BENCH_DB=13 python -m tests.benchmarks.run_external --dataset locomo

# Deterministic CSR harness (no network, no model download):
pytest tests/benchmarks/test_csr.py -q          # CI gate
python -m tests.benchmarks.csr.run_csr          # write report artifact

# SIQ — Subconscious Injection Quality (query-blind injection; #459):
pytest tests/benchmarks/test_siq.py -q          # CI gate (db15 plugin, no network)
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq --adapter native
POPOTO_BENCH_DB=13 python -m tests.benchmarks.siq.run_siq --adapter query_stub
# NOTE: the CLI plants to a dedicated bench DB (default 14; db0 rejected). Point
# it away from any in-flight external run (which also uses db14). The CI-facing
# surface is test_siq.py, which runs under the pytest db15 isolation plugin.

# RLT — Retrieval Latency & Throughput (#460):
pytest tests/benchmarks/test_rlt.py -q          # CI gate (db15 plugin, tiny synthetic corpora)
# Manual real-corpus run — --db is required and rejects 0/14/15. A native-Popoto
# Redis run is committed (results/rlt/); the Valkey run + real competitor
# adapters remain deferred (see docs/benchmarks.md):
python -m tests.benchmarks.rlt.run_rlt --db 13 --backend redis --mixed-workload

# External benchmark smoke test (fixture-based, no download):
python -m tests.benchmarks.run_external \
    --dataset longmemeval-s \
    --fixture tests/benchmarks/datasets/fixtures/longmemeval_s_sample.json \
    --limit 3 --dry-run

# Representative limited run: --limit selects a subset spanning the whole
# (category-grouped) dataset. Default --sample stride is deterministic;
# --sample stratified guarantees every question_type is represented;
# --sample shuffle --seed N is a seeded random sample; --sample head is the
# legacy contiguous prefix (benchmarks only the easiest category — opt-in).
# Reports record sample_mode/seed/limit and a per-question_type breakdown.
python -m tests.benchmarks.run_external --dataset longmemeval-s --limit 12 --sample stratified --seed 0

# Run all tests
pytest tests/benchmarks/ -x -q
```

## External-harness DB isolation & residue (issue #465)

The external harness (`run_external.py`) is a **benchmark, not a pytest test**,
so the pytest db15 test-isolation plugin does **not** apply to it. Each
benchmark item writes `ExtMem<hash>` model keys plus the special-use field keys
derived from that name (`$BM25:ExtMem<hash>*`, `$Class:ExtMem<hash>`,
`$ValidityF:`/`$ConfidencF:`/`$KeyF:`/`$DecayingSortF:ExtMem<hash>*`) to Redis.
Runs from before #701 wrote those field keys under the shared
`ExternalBenchmarkMemory` name instead. `ExternalScenario.teardown()`
deletes them per-item, but a **killed / interrupted / wedged** run leaves that
residue behind permanently — and if the harness shares db0 with a live store,
the residue pollutes it (a dogfood machine accumulated **1,825** leaked keys).

Two belt-and-braces mitigations, both scoped to the `run_external.py`
entrypoint (they activate only at run start — never at import time, so pytest
collection and the db15 plugin are unaffected):

1. **Dedicated bench DB.** At startup the harness points the Popoto connection
   at a dedicated non-default DB (**default 14**, mirroring the plugin's db15
   posture: tests → 15, benchmarks → 14). Override with `POPOTO_BENCH_DB=<n>`.
   `POPOTO_BENCH_DB=0` is **rejected** — db0 is typically production — so a
   misconfiguration can't repollute the live database. Host/port/auth from
   `REDIS_URL` are preserved.
2. **Startup sweep.** Before any ingestion the harness `SCAN`s (non-blocking,
   Valkey-safe) and `DEL`s any stale `ExternalBenchmarkMemory:*` /
   `*:ExternalBenchmarkMemory*` / `ExtMem*` / `$BM25:ExtMem*` / `*:ExtMem*`
   keys left by a prior run on the bench DB, logging the count. The
   unanchored `*:ExtMem*` pattern (#701) is what reaches the field-key
   families that carry the class name *after* a colon rather than as a
   leading prefix; `*:ExternalBenchmarkMemory*` is its pre-#701 counterpart,
   so residue from older runs is swept for both the model keys and those
   field-key families (pinned by
   `test_sweeps_pre_701_shared_name_field_keys`). The authoritative list is
   `_STALE_KEY_PATTERNS` in `run_external.py`.

### Cleaning existing db0 pollution

If earlier runs (before this fix) leaked keys into db0, sweep them once with a
non-blocking `SCAN`+`DEL` (works on both Redis and Valkey — no modules):

```bash
# Dry run first — list what would be deleted (per pattern):
for p in 'ExternalBenchmarkMemory:*' '*:ExternalBenchmarkMemory*' 'ExtMem*' '$BM25:ExtMem*' '*:ExtMem*'; do
  redis-cli -n 0 --scan --pattern "$p"
done

# Delete them (redis-cli --scan streams via SCAN, not the blocking KEYS):
for p in 'ExternalBenchmarkMemory:*' '*:ExternalBenchmarkMemory*' 'ExtMem*' '$BM25:ExtMem*' '*:ExtMem*'; do
  redis-cli -n 0 --scan --pattern "$p" | xargs -r -L 100 redis-cli -n 0 DEL
done
```

Use `valkey-cli` in place of `redis-cli` for Valkey; adjust `-n 0` if the
pollution is on another DB.

## External-harness supersession axis (#692)

`run_external.py` accepts an optional **supersession producer** axis, orthogonal
to `--extraction` and `--retrieval-mode`. It exists to make bitemporal validity
gating (`ValidityField` / `SupersessionProtocol.save_and_supersede`) *measurable*
on a real benchmark corpus: without a producer that calls it, the exclusion set
is always empty and the gate has nothing to subtract.

```bash
# Arm A — baseline (default; byte-identical to every prior committed artifact):
python -m tests.benchmarks.run_external --dataset longmemeval-s --supersession none

# Arm B — producer runs, read-time gating OFF (isolates the write-side cost):
python -m tests.benchmarks.run_external --dataset longmemeval-s \
    --supersession content-identity --no-validity-gating

# Arm C — producer runs, read-time gating ON (the default when the arm is active):
python -m tests.benchmarks.run_external --dataset longmemeval-s \
    --supersession content-identity
```

The producer (`tests/benchmarks/supersession_axis.py`) is a harness-local,
**label-blind** heuristic — `identity_of(unit_text)` takes exactly one
positional (text-only) parameter, so `relevant_ids`/`question_type` structurally
cannot reach it, mirroring #514's `collapse_to_ranking_unit` gold-blindness. It
recognizes a narrow "I `<verb>` [`<preposition>`] ..." sentence pattern (e.g. "I
work at Acme Corp.") as an identity claim; every identity-bearing write —
including the first claim of a group — routes through
`SupersessionProtocol.save_and_supersede`, closing any prior claim with the same
identity key. It is a measurement device, not a library recommendation; nothing
in `src/` uses it.

| Arm | Model declares `ValidityField` | Producer runs | `Defaults.VALIDITY_GATING_ENABLED` |
|---|---|---|---|
| **A — baseline** (`--supersession none`) | no | no | n/a |
| **B — producer, gate off** (`--supersession content-identity --no-validity-gating`) | yes | yes | `False` |
| **C — producer, gate on** (`--supersession content-identity`) | yes | yes | `True` (default) |

Non-`none` arms compose `_sup-{arm}[_nogate]` into the artifact filename
(alongside the existing mode/extraction/judged suffixes), so a supersession run
can never clobber the committed `--supersession none` baseline. The aggregate
report gains a `supersession` block (`identity_writes`, `identity_groups`,
`n_supersessions`, `n_excluded_keys_total`, `n_excluded_hits_total`,
`producer_failures` — printed even when zero, so "producer found nothing" is
never confused with "producer errored on everything").

### What the resulting delta does and does not establish

A two-arm (baseline vs. producer+gating) comparison cannot attribute its delta,
because the producer and the gate are two changes shipped together — hence
three arms, not two.

- **A → B** isolates everything the change does *other than gate*: the extra
  per-save Lua command, the field declaration, the producer's write ordering.
  On a recall metric this should be ~0; a non-zero A→B is a finding about the
  harness, not about validity, and must be reported before C is read at all.
- **B → C** is the only pair that isolates the gate. Both arms have identical
  stored state; they differ only in whether the three gating layers subtract.

**What C − B does establish:** that V0's gating machinery, on a real corpus at
real scale, subtracts the records a producer closed and does not subtract
records it did not — i.e. the gate is live and its effect is measurable rather
than structurally nil. The *sign and magnitude of the retrieval cost or benefit
of subtracting superseded records*, **conditional on this producer**. If C − B
is negative, the gate is removing records the retriever wanted; that is a real
finding about subtractive gating and must be published. Operational facts worth
having regardless of sign: exclusion-set cardinality per item, per-item
supersession counts, and the retrieval-latency cost of the two extra
`ZRANGEBYSCORE` reads per `assemble()`.

**What it does not establish:**

- **It is not "V0 validity gating improves LongMemEval-S by X".** V0 ships no
  producer. Every number here is a property of the pair (this heuristic, V0's
  gate), and the heuristic is a harness artifact that nothing in `src/` uses.
  Attributing the delta to V0 alone is the metric-attribution error this axis
  exists to avoid.
- **It is not an upper bound, or a lower bound.** A better identity function
  would produce a different number in an unknown direction. Nothing here
  brackets the achievable effect.
- **It says nothing about the `knowledge-update` category specifically** beyond
  what a per-category breakdown with n too small to exclude noise can show.
  Report the breakdown; draw no category-level conclusion without an interval
  that excludes zero.
- **It is not comparable to any judged-accuracy number.** Metric-family
  doctrine: the committed n=500 LongMemEval-S baseline is recall-family. The
  three arms are compared to each other within that family and to nothing else.
  `--judged` is not supported in combination with `--supersession`.
- **It does not resolve #693.** Whether save-only inertness is the right
  library default is untouched by this work; the producer here is an explicit
  imperative caller, which is exactly the shape #693 questions.

Every number produced under this axis carries Python version, redis-py
version, platform, Redis DB, and the baseline commit SHA, per repo doctrine.
Numbers from different redis-py versions are not compared.

**Fixed (#701):** every per-item benchmark model class is now built with
`type(class_name, bases, namespace_dict)` rather than a `class` statement
followed by a post-hoc `cls.__name__` rename. `_meta.db_class_key` (and
every field's derived Redis key — `$Class:`, `$ValidityF:`, `$ConfidencF:`,
`$KeyF:`, `$DecayingSortF:`, and the record hash itself) is captured once,
at class-creation time, from the name the metaclass actually sees — so a
post-hoc rename never reached it and every affected field shared one
namespace across items, not only `ValidityField`. `ExternalScenario.teardown()`
still deletes the per-item and per-class-name keyspace as before, now as
cheap targeted cleanup rather than the sole thing preventing cross-item
contamination — see `tests/benchmarks/test_model_class_namespacing.py` for
the construction-invariant coverage.

## Adding a New Constant

1. Add it to `VALID_RANGES` in `overrides.py`
2. If it's a module-level constant, add to `MODULE_CONSTANTS` in `overrides.py`
3. Add its sweep grid to the appropriate tier in `run_sweeps.py`
4. Run the sweep and check results

## Adding a New Scenario

1. Subclass `Scenario` in `scenarios/`
2. Implement `setup()`, `run()`, `teardown()`
3. Return `ScenarioResult` with retrieved_ids, relevant_ids, relevance_scores
4. Add to `ALL_SCENARIOS` in `run_sweeps.py`

## Adding a CsrTestCase

CSR cases live in `csr/suites/default.py` and are typed Python fixtures:

1. Author a small corpus (8–15 `PlantedMemory` items) with a handful of
   relevant memories on a topic and inert distractors.
2. Write the `standard_query` (shares vocabulary with the relevant memories)
   and the `adversarial_query` — a semantically equivalent, hand-authored
   paraphrase. Two authoring rules (enforced at load by the lint, which
   uses the real BM25 tokenizer):
   - **No shared indexed tokens with any relevant memory** — a weak
     paraphrase behaves like the standard query.
   - **≥ 1 indexed token shared with a distractor memory** — BM25 must
     return hits, or the lexical path silently falls back to the
     query-blind composite path (`plant()` also enforces this post-plant
     with a real `BM25Field.search`).

   Best practice (not a rule): avoid BM25 score ties between
   assertion-referenced ids. Ties are now deterministic — broken by member
   key ascending inside the Lua script — but distinct scores keep ranking
   assertions (`RanksAbove`, `InTopK(k=1)`) meaningful: a tie broken by key
   is not evidence of ranking quality. Give the ids named in assertions
   clearly distinct term overlap with the query.
3. Declare typed assertions (`InTopK`, `RanksAbove`, `NoneOlderThan`,
   `CoversTopic`, `Excludes`), set `relevant_ids` (the lint's ground truth),
   and append the case to `SUITE`. The module-load lint rejects rule
   violations and empty assertion lists.
