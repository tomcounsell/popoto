"""Regression tests for #540 — index pointer side keys collide with model globs.

#476 moved the index pointer out of the model hash into a side key derived by
suffixing the model hash key with ``\\x00idxptr\\x00{field}``, on the theory
that a NUL byte keeps the key out of any glob a caller would write. It does
not: Redis glob ``*`` matches any byte, NUL included, so that side key is
matched by every pattern that matches the hash key itself.

``AutoKeyField`` is the one KeyField lookup with no index Set behind it — it
resolves through ``scan_keys(get_key_pattern(value))`` (see
``KeyFieldMixin.filter_query``). Since 1.8.1 that scan returned the STRING
pointer keys alongside the real hashes, and the follow-up HGETALL failed with
``WRONGTYPE``. Downstream this surfaced as ``get_by_id()`` returning ``None``
for a row ``query.filter()`` finds.

The fix namespaces both pointer keys under ``$IdxPtr:`` / ``$TagPtr:``,
alongside Popoto's other internal keys, so they sit outside every model's key
space. A model name can never start with ``$``.
"""

import popoto
from popoto.fields.indexed_field_mixin import IndexedFieldMixin
from popoto.fields.tag_field import TagFieldMixin
from popoto.redis_db import POPOTO_REDIS_DB, scan_keys


class Sess540(popoto.Model):
    id = popoto.AutoKeyField()
    project_key = popoto.KeyField()
    status = popoto.IndexedField(default="pending")


class Tagged540(popoto.Model):
    id = popoto.AutoKeyField()
    project_key = popoto.KeyField()
    labels = popoto.TagField(null=True)


def test_autokey_lookup_agrees_with_indexed_filter():
    """#540: the three views of the same row must agree.

    Asserting only that the indexed filter finds the row, and that its pk
    matches the one held after save(), is exactly what let the direct
    autokey lookup rot silently.
    """
    s = Sess540(project_key="p540", status="pending")
    s.save()
    pk = s.id

    # 2. the indexed filter finds it
    by_index = list(Sess540.query.filter(project_key="p540"))
    assert len(by_index) == 1

    # 3. the pk from that result is the pk we held
    assert by_index[0].id == pk

    # 1. and the direct autokey lookup finds it too
    by_key = list(Sess540.query.filter(id=pk))
    assert len(by_key) == 1, "filter(id=<autokey>) lost a row filter() finds"
    assert by_key[0].id == pk

    # feeding the filter-derived pk back in agrees as well
    assert [o.id for o in Sess540.query.filter(id=by_index[0].id)] == [pk]


def test_autokey_lookup_survives_reload_of_every_field():
    """The downstream shape: save, reload by autokey, read the values back."""
    s = Sess540(project_key="p540reload", status="running")
    s.save()

    loaded = list(Sess540.query.filter(id=s.id))
    assert loaded, "reload by autokey returned nothing"
    assert loaded[0].project_key == "p540reload"
    assert loaded[0].status == "running"


def test_model_key_glob_matches_only_model_hashes():
    """No internal key may live inside a model's own key space.

    This is the root-cause assertion: it fails for any future internal key
    derived by suffixing the model hash key, not just the two fixed here.
    """
    s = Sess540(project_key="p540glob", status="pending")
    s.save()
    t = Tagged540(project_key="p540glob", labels=["alpha", "beta"])
    t.save()

    for model, obj in ((Sess540, s), (Tagged540, t)):
        matched = scan_keys(f"{model.__name__}:*")
        assert matched, f"no keys matched for {model.__name__}"
        for key in matched:
            key_s = key.decode() if isinstance(key, bytes) else key
            key_type = POPOTO_REDIS_DB.type(key)
            assert key_type == b"hash", (
                f"{model.__name__} key glob matched a " f"{key_type!r} key: {key_s!r}"
            )


def test_pointer_side_keys_are_namespaced_outside_model_key_space():
    """Both pointer derivations must start with a ``$`` namespace."""
    idx_ptr = IndexedFieldMixin._pointer_side_key("Sess540:abc:proj", "status")
    tag_ptr = TagFieldMixin._tag_pointer_side_key("Tagged540:abc:proj", "labels")

    assert idx_ptr.startswith("$")
    assert tag_ptr.startswith("$")
    assert "\x00" not in idx_ptr
    assert "\x00" not in tag_ptr


def test_pre_540_pointer_key_is_adopted_and_scrubbed_on_save():
    """A record written by 1.8.1/1.8.2 self-heals onto the new key on next save.

    The old pointer names the Set the record currently belongs to; that
    membership must survive the migration, and the colliding key must go.
    """
    s = Sess540(project_key="p540mig", status="pending")
    s.save()

    model_key = s.db_key.redis_key
    new_ptr = IndexedFieldMixin._pointer_side_key(model_key, "status")
    old_ptr = IndexedFieldMixin._pre_540_pointer_side_key(model_key, "status")

    # Simulate pre-#540 on-disk state: pointer at the old key only.
    old_set = POPOTO_REDIS_DB.get(new_ptr)
    POPOTO_REDIS_DB.delete(new_ptr)
    POPOTO_REDIS_DB.set(old_ptr, old_set)

    # Next save moves the record to a new value — the old Set must be cleaned.
    s.status = "running"
    s.save()

    assert POPOTO_REDIS_DB.exists(old_ptr) == 0, "colliding pre-#540 key survived"
    assert POPOTO_REDIS_DB.exists(new_ptr) == 1
    assert (
        POPOTO_REDIS_DB.sismember(old_set, model_key) == 0
    ), "record left stranded in its old index Set"
    assert [o.id for o in Sess540.query.filter(status="running")] == [s.id]
    assert list(Sess540.query.filter(status="pending")) == []


def test_pre_540_tag_pointer_key_is_adopted_and_scrubbed_on_save():
    """Same migration guarantee for TagField's pointer Set."""
    t = Tagged540(project_key="p540tagmig", labels=["alpha", "beta"])
    t.save()

    model_key = t.db_key.redis_key
    new_ptr = TagFieldMixin._tag_pointer_side_key(model_key, "labels")
    old_ptr = TagFieldMixin._pre_540_tag_pointer_side_key(model_key, "labels")

    old_sets = POPOTO_REDIS_DB.smembers(new_ptr)
    assert old_sets
    POPOTO_REDIS_DB.delete(new_ptr)
    POPOTO_REDIS_DB.sadd(old_ptr, *old_sets)

    t.labels = ["gamma"]
    t.save()

    assert POPOTO_REDIS_DB.exists(old_ptr) == 0, "colliding pre-#540 key survived"
    for stale_set in old_sets:
        assert (
            POPOTO_REDIS_DB.sismember(stale_set, model_key) == 0
        ), "record left stranded in a dropped tag Set"
    assert [o.id for o in Tagged540.query.filter(labels__contains="gamma")] == [t.id]
    assert list(Tagged540.query.filter(labels__contains="alpha")) == []


def test_scan_survives_stray_non_hash_key_in_model_glob_during_rolling_upgrade():
    """A mixed-version node writing a pre-#540 pointer must not break reads.

    #547 (this PR) namespaces pointer keys under ``$IdxPtr:``/``$TagPtr:`` so
    a same-version node never collides with a model's key glob. But during a
    *rolling upgrade*, a node still running 1.8.1/1.8.2 keeps writing its
    pointer as a STRING suffixed directly onto the model hash key -- which
    DOES match the glob `KeyFieldMixin.filter_query` scans for AutoKeyField
    lookups. Before the type guard in `_scan_hash_keys`, that STRING key fed
    straight into the pipelined HGETALL and raised WRONGTYPE for the whole
    batch, reproducing #540 (get_by_id()/filter(id=...) returning nothing for
    rows that exist) for the duration of the mixed-version window.

    This simulates that window directly: inject a STRING key that collides
    with the AutoKeyField glob without going through any real save path, and
    confirm the read is unaffected rather than crashing or dropping rows.
    """
    s = Sess540(project_key="p540rolling", status="pending")
    s.save()

    model_key = s.db_key.redis_key
    colliding_key = f"{model_key}\x00idxptr\x00status"
    POPOTO_REDIS_DB.set(colliding_key, "$IndexF:Sess540:status:pending")

    try:
        # Sanity: the collision really does land inside the AutoKeyField glob.
        matched = scan_keys(f"{Sess540.__name__}:*")
        matched_types = {key: POPOTO_REDIS_DB.type(key) for key in matched}
        assert any(
            t not in (b"hash", "hash") for t in matched_types.values()
        ), "test setup failed to produce a non-hash key inside the model glob"

        # The real assertion: the AutoKeyField lookup path (the ORM-level
        # equivalent of the downstream `get_by_id()` wrapper in #540) must
        # still find the row instead of raising WRONGTYPE or silently
        # returning nothing.
        loaded = Sess540.query.get(id=s.id)
        assert (
            loaded is not None
        ), "query.get(id=...) regressed under a stray STRING key"
        assert loaded.id == s.id

        by_key = list(Sess540.query.filter(id=s.id))
        assert len(by_key) == 1
        assert by_key[0].id == s.id
    finally:
        POPOTO_REDIS_DB.delete(colliding_key)
