"""CI-facing pytest suite for the RLT harness (#460).

Tiny synthetic corpora (tens of records, not thousands), pytest DB-15
isolation exclusively (via ``popoto.pytest_plugin`` — no manual DB
selection here, unlike ``rlt/run_rlt.py``'s CLI). Never exercises the real
10^3-2x10^4 scaling range or DB 14/0 — that is explicitly deferred to the
manual CLI + a tracked follow-up issue (see ``docs/benchmarks.md``'s RLT
section and ``docs/plans/rlt_benchmark.md``).
"""

import json
import tempfile
from pathlib import Path

import pytest

from tests.benchmarks.rlt.comparators import NativeAdapter, NullAdapter
from tests.benchmarks.rlt.corpus import (
    RltCorpusError,
    build_corpus,
    teardown_corpus,
)
from tests.benchmarks.rlt.latency import (
    LatencyReport,
    measure_latency,
    percentile,
)
from tests.benchmarks.rlt.mixed_workload import (
    RltMixedWorkloadError,
    run_mixed_workload,
)
from tests.benchmarks.rlt.pareto import ParetoPoint, pareto_frontier
from tests.benchmarks.rlt.run_rlt import FORBIDDEN_DBS, RltDbError, validate_db
from tests.benchmarks.rlt.scaling import run_scaling_curve
from tests.benchmarks.rlt.throughput import (
    RltThroughputError,
    measure_throughput,
)

# ---------------------------------------------------------------------------
# percentile()
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_p50_odd_length(self):
        values = [10, 20, 30, 40, 50]
        # nearest-rank: idx = int(5 * 0.50) = 2 -> sorted[2] = 30
        assert percentile(values, 50) == 30

    def test_p99_near_max(self):
        values = list(range(1, 101))  # 1..100
        # idx = int(100 * 0.99) = 99 -> sorted[99] = 100
        assert percentile(values, 99) == 100

    def test_single_element(self):
        assert percentile([42.0], 50) == 42.0
        assert percentile([42.0], 99) == 42.0

    def test_unsorted_input(self):
        assert percentile([30, 10, 20], 50) == 20

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            percentile([], 50)


# ---------------------------------------------------------------------------
# measure_latency()
# ---------------------------------------------------------------------------


class TestMeasureLatency:
    def test_all_success(self):
        report = measure_latency(lambda q: q * 2, [1, 2, 3, 4, 5])
        assert report.n_samples == 5
        assert report.n_errors == 0
        assert report.p50 is not None
        assert report.p99 is not None

    def test_partial_failure_does_not_abort(self):
        def flaky(q):
            if q == 3:
                raise RuntimeError("boom")
            return q

        report = measure_latency(flaky, [1, 2, 3, 4, 5])
        assert report.n_samples == 4
        assert report.n_errors == 1
        assert report.p50 is not None

    def test_all_errors_returns_degraded_report_not_a_crash(self):
        def always_fails(q):
            raise RuntimeError("always fails")

        report = measure_latency(always_fails, [1, 2, 3])
        assert report.n_samples == 0
        assert report.n_errors == 3
        assert report.p50 is None
        assert report.p95 is None
        assert report.p99 is None


# ---------------------------------------------------------------------------
# measure_throughput()
# ---------------------------------------------------------------------------


class TestMeasureThroughput:
    def test_single_threaded(self):
        report = measure_throughput(lambda q: q, list(range(20)), concurrency=1)
        assert report.n_queries == 20
        assert report.n_errors == 0
        assert report.queries_per_second > 0
        assert report.concurrency == 1

    def test_concurrent(self):
        report = measure_throughput(lambda q: q, list(range(20)), concurrency=4)
        assert report.n_queries == 20
        assert report.n_errors == 0
        assert report.concurrency == 4

    def test_empty_queries_raises(self):
        with pytest.raises(RltThroughputError):
            measure_throughput(lambda q: q, [])

    def test_invalid_concurrency_raises(self):
        with pytest.raises(RltThroughputError):
            measure_throughput(lambda q: q, [1], concurrency=0)

    def test_errors_counted(self):
        def flaky(q):
            if q % 2 == 0:
                raise RuntimeError("boom")
            return q

        report = measure_throughput(flaky, list(range(10)), concurrency=1)
        assert report.n_errors == 5


# ---------------------------------------------------------------------------
# Pareto frontier
# ---------------------------------------------------------------------------


class TestParetoFrontier:
    def test_empty_returns_empty(self):
        assert pareto_frontier([]) == []

    def test_single_point_is_frontier(self):
        p = ParetoPoint("a", p99_ms=10.0, recall=0.9)
        assert pareto_frontier([p]) == [p]

    def test_dominated_point_excluded(self):
        fast_and_accurate = ParetoPoint("a", p99_ms=10.0, recall=0.95)
        slow_and_less_accurate = ParetoPoint("b", p99_ms=50.0, recall=0.80)
        frontier = pareto_frontier([fast_and_accurate, slow_and_less_accurate])
        assert frontier == [fast_and_accurate]

    def test_tradeoff_points_both_on_frontier(self):
        fast_less_accurate = ParetoPoint("a", p99_ms=10.0, recall=0.70)
        slow_more_accurate = ParetoPoint("b", p99_ms=50.0, recall=0.95)
        frontier = pareto_frontier([fast_less_accurate, slow_more_accurate])
        assert set(p.config_label for p in frontier) == {"a", "b"}

    def test_identical_points_both_kept(self):
        a = ParetoPoint("a", p99_ms=10.0, recall=0.9)
        b = ParetoPoint("b", p99_ms=10.0, recall=0.9)
        frontier = pareto_frontier([a, b])
        assert set(p.config_label for p in frontier) == {"a", "b"}


# ---------------------------------------------------------------------------
# --db validation (run_rlt.py)
# ---------------------------------------------------------------------------


class TestDbValidation:
    def test_db_0_rejected(self):
        with pytest.raises(RltDbError):
            validate_db(0)

    def test_db_14_rejected(self):
        with pytest.raises(RltDbError):
            validate_db(14)

    def test_db_15_rejected(self):
        with pytest.raises(RltDbError):
            validate_db(15)

    def test_forbidden_dbs_are_exactly_0_14_15(self):
        assert FORBIDDEN_DBS == frozenset({0, 14, 15})

    def test_other_db_accepted(self):
        assert validate_db(13) == 13


# ---------------------------------------------------------------------------
# Corpus + scaling curve (tiny synthetic sizes, DB 15 only)
# ---------------------------------------------------------------------------


class TestCorpus:
    def test_build_and_teardown(self):
        corpus = build_corpus(10, seed=0, query_count=3)
        try:
            assert corpus.size == 10
            assert len(corpus.record_keys) == 10
            assert len(corpus.queries) == 3
            for q in corpus.queries:
                assert len(q.relevant_ids) > 0
                assert q.relevant_ids.issubset(set(corpus.record_keys))
        finally:
            teardown_corpus(corpus)

    def test_zero_size_raises(self):
        with pytest.raises(RltCorpusError):
            build_corpus(0)

    def test_deterministic_given_seed(self):
        corpus_a = build_corpus(5, seed=1, query_count=2)
        corpus_b = build_corpus(5, seed=1, query_count=2)
        try:
            topics_a = sorted(q.text for q in corpus_a.queries)
            topics_b = sorted(q.text for q in corpus_b.queries)
            assert topics_a == topics_b
        finally:
            teardown_corpus(corpus_a)
            teardown_corpus(corpus_b)


class TestScalingCurve:
    def test_tiny_synthetic_sizes(self):
        # NEVER the real 10^3-2x10^4 range in the CI-facing suite.
        points = run_scaling_curve(corpus_sizes=[5, 10], query_count=2)
        assert len(points) == 2
        assert points[0].corpus_size == 5
        assert points[1].corpus_size == 10
        for point in points:
            assert isinstance(point.latency, LatencyReport)

    def test_empty_sizes_returns_empty(self):
        assert run_scaling_curve(corpus_sizes=[]) == []


# ---------------------------------------------------------------------------
# Mixed workload (small thread/call counts)
# ---------------------------------------------------------------------------


class TestMixedWorkload:
    def test_small_workload_runs_to_completion(self):
        writes = []

        def read_call(q):
            return q

        def write_call(v):
            writes.append(v)

        report = run_mixed_workload(
            read_call=read_call,
            read_args=list(range(6)),
            write_call=write_call,
            write_args=list(range(6)),
            n_threads=2,
        )
        assert isinstance(report.baseline_read, LatencyReport)
        assert isinstance(report.baseline_write, LatencyReport)
        assert isinstance(report.degraded_read, LatencyReport)
        assert isinstance(report.degraded_write, LatencyReport)
        assert report.baseline_read.n_samples == 6
        assert report.baseline_write.n_samples == 6
        assert report.degraded_read.n_samples == 6
        assert report.degraded_write.n_samples == 6
        # Degradation ratio is a positive float when both sides have p99.
        if report.read_degradation_ratio is not None:
            assert report.read_degradation_ratio > 0

    def test_empty_args_raises(self):
        with pytest.raises(RltMixedWorkloadError):
            run_mixed_workload(
                read_call=lambda q: q,
                read_args=[],
                write_call=lambda v: v,
                write_args=[1],
            )

    def test_invalid_threads_raises(self):
        with pytest.raises(RltMixedWorkloadError):
            run_mixed_workload(
                read_call=lambda q: q,
                read_args=[1],
                write_call=lambda v: v,
                write_args=[1],
                n_threads=0,
            )


# ---------------------------------------------------------------------------
# Comparator adapters — Protocol contract (native + null)
# ---------------------------------------------------------------------------


class TestComparators:
    def test_native_adapter_query_returns_ids_and_latency(self):
        corpus = build_corpus(8, seed=0, query_count=2)
        try:
            adapter = NativeAdapter(corpus.model_class, corpus.agent_id)
            query = corpus.queries[0]
            ids, latency_ms = adapter.query(query.text)
            assert isinstance(ids, list)
            assert isinstance(latency_ms, float)
            assert latency_ms >= 0
        finally:
            teardown_corpus(corpus)

    def test_null_adapter_satisfies_protocol_contract(self):
        adapter = NullAdapter(synthetic_latency_ms=2.5)
        adapter.ingest("some content")
        ids, latency_ms = adapter.query("anything")
        assert ids == []
        assert latency_ms == 2.5

    def test_both_adapters_share_call_shape(self):
        corpus = build_corpus(5, seed=0, query_count=1)
        try:
            native = NativeAdapter(corpus.model_class, corpus.agent_id)
            null = NullAdapter()
            for adapter in (native, null):
                ids, latency_ms = adapter.query(corpus.queries[0].text)
                assert isinstance(ids, list)
                assert isinstance(latency_ms, float)
        finally:
            teardown_corpus(corpus)


# ---------------------------------------------------------------------------
# Artifact serialization round-trip
# ---------------------------------------------------------------------------


class TestArtifactRoundTrip:
    def test_json_round_trip(self):
        from tests.benchmarks.rlt.run_rlt import build_machine_metadata, save_reports

        points = run_scaling_curve(corpus_sizes=[5], query_count=1)
        aggregate = {
            "benchmark": "rlt",
            "run_date": "2026-07-21",
            "run_timestamp": "2026-07-21T00:00:00+00:00",
            "machine": build_machine_metadata("redis"),
            "scaling_curve": [p.to_dict() for p in points],
            "notes": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            import tests.benchmarks.rlt.run_rlt as run_rlt_mod

            original_dir = run_rlt_mod.RESULTS_DIR
            run_rlt_mod.RESULTS_DIR = Path(tmpdir)
            try:
                json_path, md_path = save_reports(aggregate, "redis", dry_run=False)
                assert json_path.exists()
                assert md_path.exists()
                loaded = json.loads(json_path.read_text())
                assert loaded["machine"]["backend"] == "redis"
                assert loaded["scaling_curve"][0]["corpus_size"] == 5
            finally:
                run_rlt_mod.RESULTS_DIR = original_dir

    def test_dry_run_writes_nothing(self):
        from tests.benchmarks.rlt.run_rlt import save_reports

        with tempfile.TemporaryDirectory() as tmpdir:
            import tests.benchmarks.rlt.run_rlt as run_rlt_mod

            original_dir = run_rlt_mod.RESULTS_DIR
            run_rlt_mod.RESULTS_DIR = Path(tmpdir)
            try:
                json_path, md_path = save_reports({}, "redis", dry_run=True)
                assert json_path is None
                assert md_path is None
                assert list(Path(tmpdir).iterdir()) == []
            finally:
                run_rlt_mod.RESULTS_DIR = original_dir
