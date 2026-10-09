"""Postgres tables an earlier release made for a model without a KeyField
are still current now that ``_auto_key`` is registered at class creation
(#826).

Two table shapes could exist. The common one was created after the model's
first instance -- the only way a row could ever be written -- and already
has ``_auto_key``: its fingerprint must be unchanged, so the new code binds
it with no migration and no ``SchemaDriftError`` even under
``POPOTO_SCHEMA_AUTO=0``. The rare one was created by a query before any
instance existed, so it has no ``_auto_key`` column (and so no rows): the
new code's first use gains the column additively, exactly as the old code's
first instance did -- and under ``POPOTO_SCHEMA_AUTO=0`` that first use (a
query now, not only a save) raises ``SchemaDriftError`` instead.
"""

import pytest

psycopg = pytest.importorskip("psycopg")

import popoto  # noqa: E402
from popoto.backends import build_model_spec  # noqa: E402
from popoto.backends.postgres.schema import (  # noqa: E402
    POPOTO_SCHEMA_TABLE,
    compile_table,
    ensure_table,
)
from popoto.models.base import ModelOptions  # noqa: E402

from ..test_auto_key_registration import _legacy_options  # noqa: E402


def _registry(admin, schema, table):
    row = admin.execute(
        f'SELECT fingerprint, columns FROM "{schema}"."{POPOTO_SCHEMA_TABLE}" '
        "WHERE table_name = %s",
        (table,),
    ).fetchone()
    admin.commit()
    return row


def test_a_table_created_after_a_first_instance_is_current(pg, admin, monkeypatch):
    class PgAkLegacy(popoto.Model):
        title = popoto.StringField(default="")
        rank = popoto.SortedField(type=int, default=0)

    legacy = compile_table(build_model_spec(_legacy_options(PgAkLegacy)), pg.schema)
    assert ensure_table(admin, legacy, auto=True) == "created"
    admin.execute(
        f'INSERT INTO {legacy.qualified} ("_pk", "_auto_key", "title", "rank") '
        "VALUES (%s, %s, %s, %s)",
        ("PgAkLegacy:" + "a" * 32, "a" * 32, "old", 3),
    )
    admin.commit()
    stored = _registry(admin, pg.schema, legacy.table)

    current = compile_table(PgAkLegacy._meta.spec, pg.schema)
    assert current.fingerprint() == legacy.fingerprint() == stored[0]
    # auto=False: any create or migration would raise SchemaDriftError.
    assert ensure_table(admin, current, auto=False) == "current"

    monkeypatch.setenv("POPOTO_SCHEMA_AUTO", "0")
    pg.forget_tables()
    [record] = PgAkLegacy.query.all()  # loaded before any instance exists
    assert record._auto_key == "a" * 32
    record.title = "edited"
    record.save()
    assert {r._auto_key: r.title for r in PgAkLegacy.query.all()} == {
        "a" * 32: "edited"
    }
    assert _registry(admin, pg.schema, legacy.table) == stored


def test_a_table_a_query_created_before_any_instance_gains_the_column(pg, admin):
    class PgAkKeyless(popoto.Model):
        title = popoto.StringField(default="")

    keyless_options = ModelOptions("PgAkKeyless")
    keyless_options.add_field("title", PgAkKeyless._meta.fields["title"])
    keyless_options.mixins = PgAkKeyless._meta.mixins
    keyless = compile_table(build_model_spec(keyless_options), pg.schema)
    assert "_auto_key" not in keyless.column_map()
    assert ensure_table(admin, keyless, auto=True) == "created"

    pg.forget_tables()
    assert PgAkKeyless.query.count() == 0  # first use: the additive migration
    stored = _registry(admin, pg.schema, keyless.table)
    assert stored[0] == compile_table(PgAkKeyless._meta.spec, pg.schema).fingerprint()
    record = PgAkKeyless.create(title="new")
    assert [r._auto_key for r in PgAkKeyless.query.all()] == [record._auto_key]


def test_a_query_created_table_under_schema_auto_off_raises_drift(
    pg, admin, monkeypatch
):
    """The rare table shape with ``POPOTO_SCHEMA_AUTO=0``: an explicit refusal.

    A table a query created before any instance has no ``_auto_key`` column.
    Under the old code a query against it still matched (the spec had no
    ``_auto_key`` either) and only the first instance hit the additive change;
    now ``_auto_key`` is in the spec from class creation, so the *first use*
    needs that change. With automatic DDL off that is a ``SchemaDriftError``
    naming ``POPOTO_SCHEMA_AUTO=0`` -- never a silent write to a table missing
    the key column -- and the table and its registry row are left untouched.
    Re-enabling automatic DDL lets the same first use migrate it.
    """
    from popoto.backends.types import SchemaDriftError

    class PgAkStrict(popoto.Model):
        title = popoto.StringField(default="")

    strict_options = ModelOptions("PgAkStrict")
    strict_options.add_field("title", PgAkStrict._meta.fields["title"])
    strict_options.mixins = PgAkStrict._meta.mixins
    strict = compile_table(build_model_spec(strict_options), pg.schema)
    assert "_auto_key" not in strict.column_map()
    assert ensure_table(admin, strict, auto=True) == "created"
    stored = _registry(admin, pg.schema, strict.table)

    monkeypatch.setenv("POPOTO_SCHEMA_AUTO", "0")
    pg.forget_tables()
    with pytest.raises(SchemaDriftError, match="POPOTO_SCHEMA_AUTO=0"):
        PgAkStrict.query.count()
    with pytest.raises(SchemaDriftError, match="POPOTO_SCHEMA_AUTO=0"):
        PgAkStrict.create(title="refused")
    assert _registry(admin, pg.schema, strict.table) == stored

    monkeypatch.setenv("POPOTO_SCHEMA_AUTO", "1")
    pg.forget_tables()
    assert PgAkStrict.query.count() == 0
    record = PgAkStrict.create(title="new")
    assert [r._auto_key for r in PgAkStrict.query.all()] == [record._auto_key]
