"""Hydration-count guards for QueryBuilder materialization (#632).

`list(builder)` calls `__iter__` and then asks the *iterable* for a length
hint, which lands on `QueryBuilder.__len__`. Both used to execute the whole
pipeline, so one materialization issued two HGETALLs per row -- a flat 2x on
every read path in every consumer (tomcounsell/ai#2639).

These tests count the hash reads a query actually issues, so the 2x cannot
come back silently. They assert the *cost*, not just the results: a correctness
test passes either way, which is exactly why this went unnoticed.
"""

import os
import sys

import pytest
import redis

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto  # noqa: E402


class HydrationCountModel(popoto.Model):
    name = popoto.KeyField()
    weight = popoto.SortedField(type=float)


class HashReadCounter:
    """Counts hash reads queued on any redis pipeline.

    `Query.get_many_objects` batches one HGETALL (or HMGET under `values=`) per
    key into a single pipeline, so counting the queued commands counts rows
    hydrated -- independent of how many pipelines were used.
    """

    def __init__(self):
        self.hgetall = 0
        self.hmget = 0

    @property
    def total(self) -> int:
        return self.hgetall + self.hmget


@pytest.fixture
def rows():
    """Five rows; four match `weight >= 1.0`.

    Clears the model explicitly rather than trusting the suite-wide flush: a
    sibling test here writes an extra row, and these assertions are exact
    counts, so a leaked row would read as a hydration-count regression.
    """
    for existing in HydrationCountModel.query.all():
        existing.delete()
    for i in range(5):
        HydrationCountModel.create(name=f"row{i}", weight=float(i))
    return 5


@pytest.fixture
def count_hash_reads(rows, monkeypatch):
    """Count hash reads issued by the test body only.

    Depends on `rows` so seeding -- including its cleanup read, whose size
    depends on what earlier tests left behind -- completes before the counter
    is installed. Counting setup would make these exact assertions depend on
    suite state rather than on the query under test.
    """
    counter = HashReadCounter()
    orig_hgetall = redis.client.Pipeline.hgetall
    orig_hmget = redis.client.Pipeline.hmget

    def hgetall(self, *args, **kwargs):
        counter.hgetall += 1
        return orig_hgetall(self, *args, **kwargs)

    def hmget(self, *args, **kwargs):
        counter.hmget += 1
        return orig_hmget(self, *args, **kwargs)

    monkeypatch.setattr(redis.client.Pipeline, "hgetall", hgetall)
    monkeypatch.setattr(redis.client.Pipeline, "hmget", hmget)
    return counter


def test_list_hydrates_each_row_once(count_hash_reads, rows):
    """`list(builder)` reads each matching row exactly once.

    Before #632 this was 8 reads for 4 rows.
    """
    results = list(HydrationCountModel.query.filter(weight__gte=1.0))

    assert len(results) == 4
    assert count_hash_reads.total == 4, (
        f"expected one hash read per row, got {count_hash_reads.total} for "
        f"{len(results)} rows"
    )


def test_list_costs_the_same_as_all(count_hash_reads, rows):
    """The materialization protocol adds no reads over an explicit `all()`.

    This is the invariant the defect broke, stated without depending on the
    query shape hydrating 1:1.
    """
    builder_all = HydrationCountModel.query.filter(weight__gte=1.0)
    all_rows = builder_all.all()
    reads_for_all = count_hash_reads.total

    count_hash_reads.hgetall = count_hash_reads.hmget = 0
    builder_list = HydrationCountModel.query.filter(weight__gte=1.0)
    list_rows = list(builder_list)
    reads_for_list = count_hash_reads.total

    assert [r.name for r in list_rows] == [r.name for r in all_rows]
    assert (
        reads_for_list == reads_for_all
    ), f"list() cost {reads_for_list} reads vs all()'s {reads_for_all}"


@pytest.mark.parametrize(
    "materialize",
    [list, tuple, lambda qb: sorted(qb, key=lambda r: r.name), lambda qb: [*qb]],
    ids=["list", "tuple", "sorted", "unpack"],
)
def test_every_materializing_builtin_hydrates_once(count_hash_reads, rows, materialize):
    """`tuple`, `sorted` and unpacking take the same iter-then-length-hint path."""
    materialize(HydrationCountModel.query.filter(weight__gte=1.0))

    assert count_hash_reads.total == 4


def test_bare_len_still_executes(count_hash_reads, rows):
    """A `len()` with no preceding iteration must not be served from a stale park."""
    builder = HydrationCountModel.query.filter(weight__gte=1.0)

    assert len(builder) == 4
    assert count_hash_reads.total == 4, "bare len() must execute the query"


def test_reiteration_requeries(count_hash_reads, rows):
    """Re-iterating a builder still re-executes, so it still sees fresh data.

    The one-shot park is handed from `__iter__` to `__len__` only; it never
    turns the builder into a cached result set.
    """
    builder = HydrationCountModel.query.filter(weight__gte=1.0)

    first = [r.name for r in builder]
    reads_after_first = count_hash_reads.total
    second = [r.name for r in builder]

    assert first == second
    assert reads_after_first == 4
    assert count_hash_reads.total == 8, "each iteration must execute the query"


def test_iteration_sees_rows_written_after_the_previous_pass(rows):
    """The park must not make a re-iteration miss newly written rows."""
    builder = HydrationCountModel.query.filter(weight__gte=1.0)

    before = [r.name for r in builder]
    HydrationCountModel.create(name="row_late", weight=9.0)
    after = [r.name for r in builder]

    assert "row_late" not in before
    assert "row_late" in after


def test_len_after_mutation_is_not_served_from_the_park(rows):
    """A builder mutated after iteration must re-execute for its length."""
    builder = HydrationCountModel.query.filter(weight__gte=1.0)

    iterated = [r.name for r in builder]
    assert len(iterated) == 4

    # limit() mutates in place and returns self; the parked result is stale for it.
    assert len(builder.limit(2)) == 2


def test_len_matches_list_length_under_limit(rows):
    """The length hint never changes what a materialization contains."""
    builder = HydrationCountModel.query.filter(weight__gte=1.0).limit(3)
    materialized = list(builder)

    assert len(materialized) == 3
