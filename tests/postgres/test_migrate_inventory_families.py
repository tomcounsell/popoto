"""Inventory prefixes and written-but-unlisted families (#822 review).

The migration inventory keys its dispositions on the prefixes the code
actually writes. A hand-spelled ``$UniqueF`` for what ``UniqueField`` really
writes (``$UniquF``, from ``"UniqueField".strip("Field")``) made the tool
stop on every model with a ``UniqueField`` or ``SortedField``. The drift test
here derives every prefix from the field classes themselves.
"""

import importlib
import pkgutil
import shutil

import pytest

np = pytest.importorskip("numpy")
psycopg = pytest.importorskip("psycopg")

import popoto  # noqa: E402
from popoto.backends import set_backend  # noqa: E402
from popoto.fields.field import _FIELD_CLASS_KEYS  # noqa: E402
from popoto.migrate_redis_to_postgres import (  # noqa: E402
    FAMILY_DISPOSITIONS,
    UNSUPPORTED,
    InventoryStop,
    MigrationConfig,
    ModelMapping,
    run_migration,
)

from .test_migrate_redis_to_postgres import (  # noqa: E402
    REDIS_SERVER,
    _copy_key,
    _FixtureServer,
    _scratch_client,
    needs_redis_server,
)

#: Field classes whose ``field_class_key`` namespace never reaches Redis as a
#: key prefix: their data lives in the record hash, in files, or under
#: another prefix the inventory lists. Each needs a reason.
NEVER_A_KEY_PREFIX = {
    "Field": "the abstract base; no instance writes a key under it",
    "AutoKeyField": "writes no index key (the value is the record key)",
    "ContentField": "content lives in files",
    "EmbeddingField": "vectors live in .npy files and the record hash",
    "TDValueField": "values live in the record hash",
    "BM25Field": "writes $BM25, listed under its own prefix",
    "ExistenceFilter": "writes $EF, listed under its own prefix",
    "FrequencySketch": "writes $FS, listed under its own prefix",
}
#: Plain value fields: the value lives in the record hash and the field
#: maintains no index key.
for _scalar in (
    "IntField FloatField DecimalField StringField BooleanField BytesField "
    "ListField DictField SetField TupleField DateField TimeField "
    "DatetimeField DataFrameField"
).split():
    NEVER_A_KEY_PREFIX[_scalar] = "value lives in the record hash; no index key"

#: Inventory entries that are not a Field class's ``field_class_key``.
NON_FIELD_FAMILIES = {
    "record",
    "$Class",
    "$AT:meta",
    "$AT:staged",
    "$AT:access_log",
    "$CyclicDecayF:cycles",
    "$CyclicDecayF:pressure",
    "$IdxPtr",
    "$TagPtr",
    "$Index",
    "$BM25",
    "$EF",
    "$FS",
    "$WF",
    "$PL",
    "$TOMB",
    "$TOMBPRIOR",
    "$RP",
    "$NR",
    "$CSQ",
    "stream",
}


def _field_prefixes():
    # Importing every field module registers every class.
    import popoto.fields as pf

    for mod in pkgutil.iter_modules(pf.__path__):
        importlib.import_module(f"popoto.fields.{mod.name}")
    return {str(k): v for k, v in _FIELD_CLASS_KEYS.items()}


def test_every_field_prefix_the_code_writes_is_in_the_inventory():
    missing = {
        key: cls
        for key, cls in _field_prefixes().items()
        if cls not in NEVER_A_KEY_PREFIX and key not in FAMILY_DISPOSITIONS
    }
    assert not missing, (
        "field classes whose index namespace the migration inventory does not "
        f"recognise (a rename would stop a client's migration): {missing}"
    )


def test_the_inventory_names_no_field_prefix_nothing_writes():
    """The converse: a hand-spelled ``$IndexedF`` is dead weight that hides
    the real prefix's absence."""
    real = set(_field_prefixes())
    dead = {
        p for p in FAMILY_DISPOSITIONS if p not in real and p not in NON_FIELD_FAMILIES
    }
    assert not dead, f"inventory entries no Field class writes: {dead}"


def test_the_real_prefixes_are_spelled_as_written():
    assert str(popoto.UniqueField.field_class_key) == "$UniquF"
    assert str(popoto.SortedField.field_class_key) == "$SortF"
    assert str(popoto.IndexedField.field_class_key) == "$IndexF"
    for prefix in ("$UniquF", "$SortF", "$IndexF"):
        assert FAMILY_DISPOSITIONS[prefix][0] == "rebuildable"


# -- end to end ---------------------------------------------------------------------


class MigFamRow(popoto.Model):
    row_id = popoto.KeyField()
    email = popoto.UniqueField()
    color = popoto.IndexedField(default="")
    score = popoto.SortedField(type=float, default=0.0)
    note = popoto.StringField(default="")


def _wipe():
    client = _scratch_client()
    for key in list(client.scan_iter(match="*MigFam*", count=1000)):
        client.delete(key)


def _snapshot(tmp_path, *, extra=None):
    previous = set_backend("redis")
    try:
        _wipe()
        MigFamRow.create(row_id="a", email="a@x", color="red", score=3.0, note="n1")
        MigFamRow.create(row_id="b", email="b@x", color="blue", score=1.0, note="n2")
        MigFamRow.create(row_id="c", email="c@x", color="red", score=2.0, note="n3")
        client = _scratch_client()
        # Written by the library but not by a plain save: a recall proposal,
        # never-record telemetry, and a query temp key still inside its TTL.
        client.zadd("$RP:MigFamRow:pending:default", {"MigFamRow:a": 1.0})
        client.hset("$NR:MigFamRow:counts", "entropy", 2)
        client.set("$CSQ:MigFamRow:composite:abc", "x", ex=60)
        if extra:
            extra(client)
        directory = tmp_path / "fixture-server"
        directory.mkdir()
        server = _FixtureServer(directory)
        try:
            for key in client.scan_iter(match="*MigFam*", count=1000):
                _copy_key(client, server.client, key)
            rdb = server.save_and_stop()
        except BaseException:
            server.process.kill()
            raise
    finally:
        _wipe()
        set_backend(previous)
    snapshot = tmp_path / "snap.rdb"
    shutil.copyfile(rdb, snapshot)
    return snapshot


def _config(tmp_path, pg, rdb, run, **kw):
    return MigrationConfig(
        rdb_path=rdb,
        run_dir=tmp_path / run,
        source_id="laptop-a",
        mappings=(ModelMapping(model=MigFamRow),),
        content_dir=tmp_path,
        postgres_dsn=pg.dsn,
        postgres_schema=pg.schema,
        redis_server=REDIS_SERVER,
        batch_size=4,
        operator="pytest",
        **kw,
    )


@pytest.fixture(autouse=True)
def _unbound():
    MigFamRow._meta.backend = None
    yield


@needs_redis_server
def test_unique_and_sorted_fields_and_every_written_family_migrate_clean(tmp_path, pg):
    rdb = _snapshot(tmp_path)
    report = run_migration(_config(tmp_path, pg, rdb, "run"))
    assert report.verdict == "clean", report.summary()
    lossy = dict(report.lossy)
    assert lossy["recall_proposal_keys_not_carried"] == 1
    assert lossy["never_record_telemetry_keys_not_carried"] == 1
    assert lossy["transient_query_keys_skipped"] == 1

    previous = set_backend("postgres")
    try:
        MigFamRow._meta.backend = "postgres"
        assert [r.row_id for r in MigFamRow.query.filter(email="b@x")] == ["b"]
        assert {r.row_id for r in MigFamRow.query.filter(color="red")} == {"a", "c"}
        ordered = MigFamRow.query.filter(order_by="score")
        assert [r.row_id for r in ordered] == ["b", "c", "a"]
        with pytest.raises(Exception):
            MigFamRow.create(row_id="z", email="a@x")
    finally:
        MigFamRow._meta.backend = None
        set_backend(previous)


@needs_redis_server
def test_a_tombstone_archive_stops_the_run_and_accept_unclassified_cannot_waive_it(
    tmp_path, pg
):
    assert FAMILY_DISPOSITIONS["$TOMB"][0] == UNSUPPORTED

    def tombstones(client):
        client.hset("$TOMB:MigFamRow:data", "MigFamRow:gone", b"payload")
        client.zadd("$TOMB:MigFamRow:index", {"MigFamRow:gone": 1.0})

    rdb = _snapshot(tmp_path, extra=tombstones)
    for i, accept in enumerate((False, True)):
        with pytest.raises(InventoryStop) as excinfo:
            run_migration(
                _config(tmp_path, pg, rdb, f"run{i}", accept_unclassified=accept)
            )
        assert "$TOMB" in str(excinfo.value)
        assert "cannot carry" in str(excinfo.value)
