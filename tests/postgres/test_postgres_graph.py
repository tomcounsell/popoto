"""``[PG-only]`` behaviour of the co-occurrence graph (#759 M4, plan §2 F).

The model-level parity is the conformance suite (gate (b):
``test_co_occurrence_field``, ``test_graph_traversal``, the co-occurrence arm
of ``test_composite_score_query``) and ``scripts/probe_graph_parity.py``'s
seeded two-leg probe, a slice of which runs here. This file covers what has
no Redis counterpart: the edge table the compiler emits, the delete cleanup
inside the record ``DELETE``, the record-key locks behind every edge write
and what they buy under concurrency, the two ``graph_expand`` paths (the
``WITH RECURSIVE`` statement and the exact replay of the Lua queue), the
Postgres twins of the two conformance tests that plant raw sorted-set
scores, and the divergences the feature page documents.
"""

import importlib.util
import math
import threading
import time
from pathlib import Path

import pytest

import popoto
from popoto.backends import BackendRetryableError, get_backend
from popoto.backends.postgres import graph as graph_mod
from popoto.fields.co_occurrence_field import CoOccurrenceField
from popoto.fields.confidence_field import ConfidenceField
from popoto.fields.constants import Defaults
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.recipes import graph_traversal


class GraphNode(popoto.Model):
    name = popoto.KeyField()
    links = CoOccurrenceField(symmetric=True, max_edges=5)


class GraphOneWay(popoto.Model):
    name = popoto.KeyField()
    links = CoOccurrenceField(symmetric=False, max_edges=3)


class GraphMixed(popoto.Model):
    name = popoto.KeyField()
    agent_id = popoto.KeyField(default="default")
    links = CoOccurrenceField(symmetric=True, max_edges=100)
    certainty = ConfidenceField(initial_confidence=0.8)
    relevance = DecayingSortedField(decay_rate=0.5, partition_by="agent_id")


def _edges(admin, pg_schema, model, field="links"):
    ts = get_backend(model)._table(model._meta.spec)
    rows = admin.execute(
        f'SELECT "src", "dst", "weight" FROM {graph_mod.edge_table(ts, field)} '
        'ORDER BY "src" COLLATE "C", "dst" COLLATE "C"'
    ).fetchall()
    return [tuple(r) for r in rows]


# -- schema ---------------------------------------------------------------------


def test_the_edge_table_ddl_is_pinned():
    """Pure: the compiler emits one edge table per CoOccurrenceField, keyed
    ``(src, dst)``, with no foreign key -- Redis links any two key strings,
    records or not -- and no column on the record table."""
    from popoto.backends.postgres.schema import compile_table

    ts = compile_table(GraphNode._meta.spec, "popoto")
    ddl = ts.create_sql()
    assert ddl[0] == (
        'CREATE TABLE "popoto"."graph_node" ("_pk" text PRIMARY KEY, '
        '"name" text, "_created_at" timestamptz NOT NULL DEFAULT now(), '
        '"_updated_at" timestamptz NOT NULL DEFAULT now(), '
        '"_migrated_from" jsonb, "_estimated_fields" text[])'
    )
    assert ddl[-1] == (
        'CREATE TABLE IF NOT EXISTS "popoto"."graph_node__links__edge" ('
        '"src" text NOT NULL, "dst" text NOT NULL, '
        '"weight" double precision NOT NULL, PRIMARY KEY ("src", "dst"))'
    )
    assert graph_mod.edge_table(ts, "links") == '"popoto"."graph_node__links__edge"'
    spec = GraphOneWay._meta.spec.fields["links"]
    assert spec.options["symmetric"] is False
    assert spec.options["max_edges"] == 3


def test_a_link_writes_both_directed_rows(pg, pg_schema, admin):
    f = GraphNode._meta.fields["links"]
    assert f.link(GraphNode, "a", "b", initial_weight=0.25) == 0.0
    assert _edges(admin, pg_schema, GraphNode) == [("a", "b", 0.25), ("b", "a", 0.25)]
    g = GraphOneWay._meta.fields["links"]
    g.link(GraphOneWay, "a", "b", initial_weight=0.5)
    assert _edges(admin, pg_schema, GraphOneWay) == [("a", "b", 0.5)]


# -- deletes ----------------------------------------------------------------------


def test_deleting_records_removes_their_edges_and_the_reverse_ones(
    pg, pg_schema, admin
):
    """``on_delete`` runs as CTEs of the record ``DELETE``: own rows, and for a
    symmetric field the reverse row in every set the record linked to. A
    reverse row whose own source is deleted in the same statement is not
    deleted twice (two linked records in one bulk delete)."""
    f = GraphNode._meta.fields["links"]
    a, b, c = (GraphNode.create(name=n) for n in "abc")
    ka, kb, kc = (r.db_key.redis_key for r in (a, b, c))
    f.link(GraphNode, ka, kb, initial_weight=0.5)
    f.link(GraphNode, kb, kc, initial_weight=0.5)
    f.link(GraphNode, "outsider", ka, initial_weight=0.5)
    assert GraphNode.bulk_delete([a, b]) == 2
    assert _edges(admin, pg_schema, GraphNode) == []
    assert f.get_linked(GraphNode, kc) == []
    assert f.get_linked(GraphNode, "outsider") == []


def test_an_asymmetric_delete_leaves_other_sets_alone(pg, pg_schema, admin):
    g = GraphOneWay._meta.fields["links"]
    a = GraphOneWay.create(name="a")
    ka = a.db_key.redis_key
    g.link(GraphOneWay, ka, "x", initial_weight=0.5)
    g.link(GraphOneWay, "y", ka, initial_weight=0.5)
    a.delete()
    assert _edges(admin, pg_schema, GraphOneWay) == [("y", ka, 0.5)]


# -- the write path ----------------------------------------------------------------


def test_every_edge_write_takes_the_record_locks_first(pg, monkeypatch):
    """The edge sets' record-key locks (``src``, and ``dst`` when symmetric),
    sorted, then the statement: the backend's one lock order (plan §6)."""
    backend = get_backend(GraphNode)
    sent = []
    real = backend._run

    def spy(sql, params=(), **kw):
        sent.append((sql, params))
        return real(sql, params, **kw)

    monkeypatch.setattr(backend, "_run", spy)
    f = GraphNode._meta.fields["links"]
    f.link(GraphNode, "zed", "amy", initial_weight=0.5)
    f.strengthen(GraphNode, "zed", "amy", delta=0.1)
    f.unlink(GraphNode, "zed", "amy")
    f.weaken_all(GraphNode, "zed", factor=0.5)
    writes = [(s, p) for s, p in sent if "__edge" in s]
    assert len(writes) == 4
    for sql, params in writes:
        assert sql.startswith("SELECT ")
        assert sql.index("pg_advisory_xact_lock") < sql.index("__edge")
    keys = writes[0][1][0]
    assert keys == [
        f"popoto:rec:{backend._table(GraphNode._meta.spec).qualified}:{k}"
        for k in ("amy", "zed")
    ]


def _hammer_links(admin, pg_schema):
    """Eight threads linking 40 distinct targets each into one source's set
    (``max_edges=3``), each reading the committed set size after every link.
    Returns the largest size any thread saw."""
    g = GraphOneWay._meta.fields["links"]
    ts = get_backend(GraphOneWay)._table(GraphOneWay._meta.spec)
    table = graph_mod.edge_table(ts, "links")
    seen = []
    errors = []

    def worker(t):
        import psycopg

        conn = psycopg.connect(pg_schema.url, autocommit=True)
        try:
            for i in range(40):
                g.link(
                    GraphOneWay, "hub", f"t{t}-{i}", initial_weight=(t * 40 + i) / 400
                )
                (n,) = conn.execute(
                    f'SELECT count(*) FROM {table} WHERE "src" = %s', ("hub",)
                ).fetchone()
                seen.append(n)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    return max(seen)


def test_concurrent_links_to_one_set_never_exceed_max_edges(pg, pg_schema, admin):
    """``LINK_WITH_PRUNE_LUA`` counts and prunes atomically: no committed state
    of the set ever holds more than ``max_edges`` edges, and the set ends
    with the three heaviest (an order-independent answer, so the serial one)."""
    assert _hammer_links(admin, pg_schema) <= 3
    rows = _edges(admin, pg_schema, GraphOneWay)
    assert sorted(w for _, _, w in rows) == [317 / 400, 318 / 400, 319 / 400]


def test_concurrent_strengthens_of_one_edge_lose_no_update(pg, pg_schema, admin):
    """``STRENGTHEN_CLAMP_LUA`` is a read-modify-write. Eight threads × 60
    strengthens by the same delta end on the serial fold of the additions,
    bit for bit, in both directions of the symmetric pair."""
    f = GraphNode._meta.fields["links"]
    delta = 0.0013

    def worker():
        for _ in range(60):
            f.strengthen(GraphNode, "p", "q", delta=delta)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    want = 0.0
    for _ in range(480):
        want = min(want + delta, Defaults.CO_OCCURRENCE_WEIGHT_CAP)
    assert _edges(admin, pg_schema, GraphNode) == [("p", "q", want), ("q", "p", want)]


def test_crossing_symmetric_links_do_not_deadlock(pg):
    """``link(a, b)`` and ``link(b, a)`` write the same two sets; the locks
    are taken in ``_pk`` order whatever the argument order, so concurrent
    crossing links serialize instead of deadlocking."""
    f = GraphNode._meta.fields["links"]
    failures = []

    def worker(flip):
        for i in range(30):
            a, b = (f"n{i}", f"m{i}") if flip else (f"m{i}", f"n{i}")
            try:
                f.strengthen(GraphNode, a, b, delta=0.01)
            except BackendRetryableError as exc:  # pragma: no cover
                failures.append(exc)

    threads = [threading.Thread(target=worker, args=(k % 2,)) for k in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert failures == []


def test_edge_writes_join_a_transaction(pg, pg_schema, admin):
    f = GraphNode._meta.fields["links"]
    with pytest.raises(RuntimeError):
        with pg.transaction() as uow:
            f.link(GraphNode, "a", "b", initial_weight=0.5, pipeline=uow)
            assert f.strengthen(GraphNode, "a", "b", delta=0.25, pipeline=uow) == 0.75
            raise RuntimeError("roll back")
    assert _edges(admin, pg_schema, GraphNode) == []


def test_a_redis_pipeline_cannot_carry_an_edge_write(pg, pg_schema, admin):
    """The M1 rule: a Redis pipeline handed to a Postgres model's write runs
    the write at once and is returned untouched; ``strengthen`` returns
    ``None``, as a queued call does."""
    pipe = popoto.get_redis().pipeline()
    f = GraphNode._meta.fields["links"]
    assert f.strengthen(GraphNode, "a", "b", delta=0.2, pipeline=pipe) is None
    assert pipe.command_stack == []
    assert _edges(admin, pg_schema, GraphNode) == [("a", "b", 0.2), ("b", "a", 0.2)]


def test_a_nan_weight_raises_value_error(pg, pg_schema, admin):
    """A documented divergence: ``ZADD`` refuses a NaN score and Redis raises
    ``ResponseError`` out of the script; Postgres raises ``ValueError``. An
    existing edge keeps its weight either way (the Lua returns it before the
    ``ZADD``)."""
    f = GraphNode._meta.fields["links"]
    with pytest.raises(ValueError, match="not a valid float"):
        f.link(GraphNode, "a", "b", initial_weight=math.nan)
    assert _edges(admin, pg_schema, GraphNode) == []
    f.link(GraphNode, "a", "b", initial_weight=0.5)
    assert f.link(GraphNode, "a", "b", initial_weight=math.nan) == 0.0


def test_out_of_range_inputs_take_the_exact_path(pg, pg_schema, admin):
    """A delta past ``1e300`` would overflow ``weight + delta`` in SQL (where
    Postgres raises and C saturates); it is computed in Python floats inside
    one transaction instead, and lands where the Lua lands."""
    f = GraphNode._meta.fields["links"]
    f.link(GraphNode, "a", "b", initial_weight=-1e308)
    assert f.strengthen(GraphNode, "a", "b", delta=1.7e308) == 1.0
    assert f.strengthen(GraphNode, "c", "d", delta=math.inf) == 1.0
    assert f.weaken_all(GraphNode, "a", factor=1e-300) == 1
    f.link(GraphNode, "e", "f", initial_weight=5e-324)
    assert f.weaken_all(GraphNode, "e", factor=0.5) == 1
    assert ("e", "f", 5e-324) not in _edges(admin, pg_schema, GraphNode)


def test_negative_zero_is_stored_as_zero(pg, pg_schema, admin):
    """A sorted set replies ``0`` for a ``-0`` score; the edge row holds the
    zero Redis gives back."""
    f = GraphOneWay._meta.fields["links"]
    f.link(GraphOneWay, "a", "b", initial_weight=-0.0)
    ((_, _, w),) = _edges(admin, pg_schema, GraphOneWay)
    assert w == 0.0 and math.copysign(1, w) == 1


# -- reads -------------------------------------------------------------------------


def test_get_linked_follows_zrevrangebyscore(pg):
    """Ties by member, descending, bytewise; ``min_weight`` inclusive, ``(``
    exclusive; ``limit`` 0 is empty and negative is everything."""
    g = GraphNode._meta.fields["links"]
    for dst, w in [("b", 0.5), ("a", 0.5), ("é", 0.5), ("c", 0.25)]:
        g.link(GraphNode, "s", dst, initial_weight=w)
    assert [d for d, _ in g.get_linked(GraphNode, "s", min_weight=0)] == [
        "é",
        "b",
        "a",
        "c",
    ]
    assert g.get_linked(GraphNode, "s", min_weight="(0.25", limit=-1) == [
        ("é", 0.5),
        ("b", 0.5),
        ("a", 0.5),
    ]
    assert g.get_linked(GraphNode, "s", limit=0) == []
    assert len(g.get_linked(GraphNode, "s", min_weight="-inf", limit=-1)) == 4


def test_the_bfs_statement_is_one_recursive_query(pg, monkeypatch):
    backend = get_backend(GraphNode)
    sent = []
    real = backend._run

    def spy(sql, params=(), **kw):
        sent.append(sql)
        return real(sql, params, **kw)

    f = GraphNode._meta.fields["links"]
    f.link(GraphNode, "a", "b", initial_weight=1.0)
    f.link(GraphNode, "b", "c", initial_weight=1.0)
    monkeypatch.setattr(backend, "_run", spy)
    assert f.propagate(GraphNode, ["a"], depth=2) == {"b": 0.5, "c": 0.25}
    assert len(sent) == 1 and "WITH RECURSIVE" in sent[0]


def test_outside_the_monotone_domain_the_lua_queue_is_replayed(pg, monkeypatch):
    """With ``threshold <= 0`` the step is not monotone, so the visited map's
    order matters: the exact path replays the Lua queue, one statement per
    layer, over the same top-``max_edges`` neighbour lists."""
    backend = get_backend(GraphNode)
    calls = []
    real = backend._graph_neighbours

    def spy(table, nodes, fanout):
        calls.append(list(nodes))
        return real(table, nodes, fanout)

    f = GraphNode._meta.fields["links"]
    f.link(GraphNode, "a", "b", initial_weight=1.0)
    f.link(GraphNode, "b", "c", initial_weight=-0.5)
    monkeypatch.setattr(backend, "_graph_neighbours", spy)
    got = f.propagate(GraphNode, ["a"], depth=3, decay_per_hop=0.5, threshold=-1.0)
    assert got == {"b": 0.5, "c": -0.125}
    # a's list is fetched once (the first layer); the third layer needs only c.
    assert calls == [["a"], ["b"], ["c"]]


def test_the_two_bfs_paths_agree(pg):
    """The ``WITH RECURSIVE`` statement and the exact replay give the same
    answer wherever the statement is used (the probe compares them on every
    shape; this is a fixed graph with cycles, ties and a fan-out cut)."""
    f = GraphNode._meta.fields["links"]
    names = [f"n{i}" for i in range(12)]
    for i, a in enumerate(names):
        for j in (1, 3, 5):
            f.link(
                GraphNode, a, names[(i + j) % 12], initial_weight=((i * j) % 7 + 1) / 7
            )
    for depth in (1, 2, 3, 5):
        sql = f.propagate(GraphNode, ["n0", "n4"], depth=depth, threshold=0.001)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(graph_mod, "_bfs_in_sql_domain", lambda *a: False)
            exact = f.propagate(GraphNode, ["n0", "n4"], depth=depth, threshold=0.001)
        assert sql == exact


# -- Postgres twins of the conformance tests that plant raw sorted-set scores -------


def test_an_over_cap_stored_weight_is_clamped_at_read_time(pg, pg_schema, admin):
    """``test_co_occurrence_field.py::test_propagate_read_time_min_for_overcap_weights``
    on Postgres: the over-cap weight is written to the edge row directly
    (bypassing ``link``/``strengthen``), and ``propagate`` reads it as
    ``min(weight, cap)``."""
    f = GraphNode._meta.fields["links"]
    f.link(GraphNode, "a", "z", initial_weight=0.1)  # creates the table
    ts = get_backend(GraphNode)._table(GraphNode._meta.spec)
    admin.execute(
        f"INSERT INTO {graph_mod.edge_table(ts, 'links')} VALUES ('a', 'b', 2.5)"
    )
    scores = f.propagate(GraphNode, ["a"], depth=1, decay_per_hop=0.5, threshold=0.01)
    cap = Defaults.CO_OCCURRENCE_WEIGHT_CAP
    assert abs(scores["b"] - 0.5 * cap) < 1e-6


def test_decay_modulation_lowers_a_stale_weight(pg, pg_schema, admin):
    """``test_graph_traversal.py::test_decay_modulation_lowers_stale_weight``
    on Postgres: the decay clock is the field's column, backdated by SQL."""
    fresh = GraphMixed.create(name="fresh")
    stale = GraphMixed.create(name="stale")
    ts = get_backend(GraphMixed)._table(GraphMixed._meta.spec)
    admin.execute(
        f'UPDATE {ts.qualified} SET "relevance" = %s WHERE "_pk" = %s',
        (time.time() - 365 * 86400, stale.db_key.redis_key),
    )
    modulated = graph_traversal._modulate_admission(
        GraphMixed,
        [(fresh.db_key.redis_key, 1.0), (stale.db_key.redis_key, 1.0)],
        decay_field_name="relevance",
        threshold=0.0,
    )
    weights = dict(modulated)
    assert weights[fresh.db_key.redis_key] > weights[stale.db_key.redis_key]


# -- the seeded probe, CI-sized ------------------------------------------------------

PROBE = Path(__file__).resolve().parents[2] / "scripts" / "probe_graph_parity.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_graph_parity", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_seeded_graph_probe_finds_no_undocumented_mismatch(pg):
    """``scripts/probe_graph_parity.py`` ran at 600 shapes over three seeds for
    the PR; one seed and 25 shapes run on every Postgres CI job."""
    probe = _load_probe().run(pg, seeds=[759], shapes=25)
    assert probe.shapes == 25
    assert sum(probe.checks.values()) > 500
    assert not probe.mismatches, probe.report()


def _interleaved_links(pg):
    """A set at ``max_edges`` (three light edges); one link runs inside an
    open transaction, a second link of the same set starts from another
    thread, and only then does the first commit. Returns the committed set
    size afterwards and whether the second link had to wait."""
    g = GraphOneWay._meta.fields["links"]
    for i in range(3):
        g.link(GraphOneWay, "hub", f"light{i}", initial_weight=0.1 + i / 100)
    finished = threading.Event()

    def second():
        g.link(GraphOneWay, "hub", "heavy2", initial_weight=0.9)
        finished.set()

    with pg.transaction() as uow:
        g.link(GraphOneWay, "hub", "heavy1", initial_weight=0.8, pipeline=uow)
        thread = threading.Thread(target=second)
        thread.start()
        waited = not finished.wait(0.5)
    thread.join()
    return len(g.get_linked(GraphOneWay, "hub", min_weight="-inf", limit=-1)), waited


def test_a_link_waits_for_a_concurrent_link_of_the_same_set(pg):
    """Deterministic interleaving: the second link queues on the set's record
    lock until the first commits, then prunes on the committed set, so the
    set holds ``max_edges``."""
    size, waited = _interleaved_links(pg)
    assert waited
    assert size == 3


def test_interleaved_links_overflow_without_the_record_lock(pg, monkeypatch):
    """The control: with the record-key lock removed the second link takes its
    snapshot before the first commits -- it then waits only on the row the
    first one pruned, and deletes that same row again rather than the next
    lightest -- so the set commits four edges, and the test above is not
    vacuous."""
    monkeypatch.setattr(
        "popoto.backends.postgres.record_lock_sql", lambda ts, pks: ("", [])
    )
    size, _waited = _interleaved_links(pg)
    assert size == 4
