"""A model without a KeyField has its ``_auto_key`` from class creation (#826).

The implicit ``AutoKeyField`` used to be registered by the first
``Model.__init__``. Until a process constructed an instance, the model had no
key field at all, and the lazy read paths (``query.all()`` / ``filter()``)
decode records with ``object.__new__``, never ``__init__``. A process that
only loaded, edited and saved records therefore saved each one under the
bare class name and deleted the original: every edited record collapsed onto
one key. These tests pin the fix and pin that nothing else moved -- field
order, the spec, the Postgres table fingerprint and the key format are what
construction-first always produced.
"""

import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

import popoto
from popoto.backends import build_model_spec
from popoto.fields.shortcuts import AutoKeyField
from popoto.models.base import ModelOptions

MODEL_SOURCE = textwrap.dedent("""
    import popoto


    class AkLoadFirst(popoto.Model):
        name = popoto.Field(type=str, null=True)
        score = popoto.IntField(default=0)
    """)

#: Runs in a fresh interpreter: import the model, load, edit, save. It never
#: constructs an instance before the saves, which is the shape that lost data.
CHILD = textwrap.dedent("""
    import json
    import sys

    sys.path.insert(0, sys.argv[1])
    import ak_load_first_model as m

    key_fields = sorted(m.AkLoadFirst._meta.key_field_names)
    loaded = list(m.AkLoadFirst.query.all())
    for obj in loaded:
        obj.name = obj.name + "-edited"
        obj.save()
    print(json.dumps({"key_fields": key_fields, "loaded": len(loaded)}))
    """)


def _model_module(tmp_path):
    (tmp_path / "ak_load_first_model.py").write_text(MODEL_SOURCE)
    sys.path.insert(0, str(tmp_path))
    try:
        sys.modules.pop("ak_load_first_model", None)
        import ak_load_first_model
    finally:
        sys.path.remove(str(tmp_path))
    return ak_load_first_model


def _redis_url() -> str:
    kwargs = popoto.get_redis().connection_pool.connection_kwargs
    db = kwargs.get("db")
    assert db not in (0, "0", None), "the child process only ever binds a test DB"
    return f"redis://{kwargs.get('host', 'localhost')}:{kwargs.get('port', 6379)}/{db}"


def _child_env(backend) -> dict:
    env = dict(os.environ)
    # REDIS_URL is read at import time; it names the test DB on both legs so
    # the child can never touch DB 0.
    env["REDIS_URL"] = _redis_url()
    env.pop("POPOTO_BACKEND", None)
    if not backend.is_redis:
        env["POPOTO_BACKEND"] = "postgres"
        env["POPOTO_POSTGRES_URL"] = backend.dsn
        env["POPOTO_POSTGRES_SCHEMA"] = backend.schema
    return env


@pytest.mark.conformance
def test_a_process_that_only_loads_keeps_each_records_key(tmp_path, backend):
    module = _model_module(tmp_path)
    model = module.AkLoadFirst
    for record in model.query.all():
        record.delete()
    first = model.create(name="a", score=1)
    second = model.create(name="b", score=2)
    ids = {first._auto_key: "a", second._auto_key: "b"}

    child = tmp_path / "child.py"
    child.write_text(CHILD)
    result = subprocess.run(
        [sys.executable, str(child), str(tmp_path)],
        env=_child_env(backend),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    # The stored state first: before #826 the Redis leg failed here, both
    # records collapsed onto the bare key "AkLoadFirst" and the originals
    # deleted (the Postgres leg refused the load with SchemaDriftError).
    if backend.is_redis:
        redis = popoto.get_redis()
        assert not redis.exists("AkLoadFirst"), "a record saved under the bare name"
        for auto_key in ids:
            assert redis.exists(f"AkLoadFirst:{auto_key}")
    after = {r._auto_key: r.name for r in model.query.all()}
    assert after == {k: v + "-edited" for k, v in ids.items()}
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"key_fields": ["_auto_key"], "loaded": 2}
    for record in model.query.all():
        record.delete()


# -- the shape is the one construction-first always produced -------------------


def _legacy_options(model) -> ModelOptions:
    """``_meta`` as the pre-#826 code left it after a first instance: the
    declared fields as the metaclass registered them, then
    ``self._meta.add_field("_auto_key", AutoKeyField())`` from ``__init__``."""
    options = ModelOptions(model.__name__)
    for name, field in model._meta.fields.items():
        if name != "_auto_key":
            options.add_field(name, field)
    options.add_field("_auto_key", AutoKeyField())
    options.order_by = model._meta.order_by
    options.ttl = model._meta.ttl
    options.indexes = model._meta.indexes
    options.backend = model._meta.backend
    options.abstract = model._meta.abstract
    options.mixins = model._meta.mixins
    return options


def _shape(meta):
    return (
        list(meta.field_names),
        list(meta.explicit_fields),
        list(meta.hidden_fields),
        sorted(meta.key_field_names),
        sorted(meta.auto_field_names),
        sorted(meta.sorted_field_names),
        sorted(meta.indexed_field_names),
        meta.db_key_length,
    )


def _make(name):
    """A fresh model class named ``name`` (fresh field instances each time)."""
    attrs = {
        "__module__": __name__,
        "title": popoto.Field(type=str, null=True),
        "_hidden": popoto.Field(type=str, null=True),
        "rank": popoto.SortedField(type=int, default=0),
        "tag": popoto.IndexedField(type=str, null=True),
    }
    return type(name, (popoto.Model,), attrs)


def test_construct_first_and_load_first_have_the_same_meta():
    constructed = _make("AkShapeA")
    constructed(title="x")
    never = _make("AkShapeB")
    assert _shape(never._meta) == _shape(constructed._meta)
    # _auto_key comes last, after the declared hidden fields, as __init__
    # appended it; the hash layout and msgpack field set are unchanged.
    assert never._meta.field_names == ["title", "rank", "tag", "_hidden", "_auto_key"]
    assert never._meta.key_field_names == {"_auto_key"}


def test_an_instance_no_longer_changes_meta_or_spec():
    model = _make("AkShapeC")
    before = _shape(model._meta)
    spec = model._meta.spec
    model(title="x")
    assert _shape(model._meta) == before
    assert model._meta.spec is spec


def test_the_spec_and_table_match_what_construction_first_produced():
    """A Postgres table an earlier release created for such a model (after
    its first instance, the only way a row could be written) has this
    fingerprint, so it is recognised as current: no migration, no drift."""
    from popoto.backends.postgres.schema import compile_table

    model = _make("AkShapeD")
    legacy = _legacy_options(model)
    assert _shape(legacy) == _shape(model._meta)
    assert build_model_spec(legacy) == model._meta.spec
    old = compile_table(build_model_spec(legacy), "popoto")
    new = compile_table(model._meta.spec, "popoto")
    assert new.fingerprint() == old.fingerprint()
    assert new.create_sql() == old.create_sql()


def test_the_generated_key_format_is_unchanged():
    model = _make("AkShapeE")
    instance = model(title="x")
    assert re.fullmatch(r"AkShapeE:[0-9a-f]{32}", instance.db_key.redis_key)
    assert instance.db_key.redis_key == f"AkShapeE:{instance._auto_key}"


def test_a_declared_key_field_gets_no_auto_key():
    class AkKeyed(popoto.Model):
        slug = popoto.KeyField()
        title = popoto.Field(null=True)

    assert AkKeyed._meta.key_field_names == {"slug"}
    assert "_auto_key" not in AkKeyed._meta.fields


def test_subclasses_are_registered_on_their_own_fields():
    """Fields are not inherited between popoto models; each class decides
    from its own declarations, as the first instance of each used to."""

    class AkParent(popoto.Model):
        title = popoto.Field(null=True)

    class AkChildKeyed(AkParent):
        slug = popoto.KeyField()

    class AkChildPlain(AkParent):
        note = popoto.Field(null=True)

    assert AkParent._meta.key_field_names == {"_auto_key"}
    assert AkChildKeyed._meta.key_field_names == {"slug"}
    assert "_auto_key" not in AkChildKeyed._meta.fields
    assert AkChildPlain._meta.field_names == ["note", "_auto_key"]
    assert (
        AkChildPlain._meta.fields["_auto_key"] is not AkParent._meta.fields["_auto_key"]
    )


def test_an_abstract_model_is_registered_like_any_other():
    class AkAbstract(popoto.Model):
        title = popoto.Field(null=True)

        class Meta:
            abstract = True

    assert AkAbstract._meta.key_field_names == {"_auto_key"}


def test_the_auto_key_helper_reads_meta_without_constructing():
    """``_get_auto_key_field_name`` used to construct an instance to trigger
    the registration, and returned None when that failed (a required
    field): ``check_indexes`` then skipped orphan detection for the model."""

    class AkRequired(popoto.Model):
        title = popoto.Field(type=str, null=False)

        def __init__(self, **kwargs):
            raise AssertionError("constructed")

    assert AkRequired._get_auto_key_field_name() == "_auto_key"
