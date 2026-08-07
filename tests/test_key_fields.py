"""
Tests for KeyField behavior: composite keys, auto keys, and the Redis Sets
that KeyFields maintain as indexes.

These assertions previously ran at module level, i.e. during import. That is
before the pytest plugin's session-scoped ``_popoto_test_db`` fixture swaps the
connection, so they executed against DB 0 -- the developer's real database --
and left residue that turned later runs into spurious collection errors (#522).

Everything now lives in test functions, so the plugin's autouse
``_popoto_flush_db`` gives each test a clean DB 15 and no teardown is needed.
Each test creates the data it needs.
"""

import pytest

from src import popoto
from src.popoto.exceptions import ModelException

# Import the module, not the value: ``from ... import POPOTO_REDIS_DB`` binds
# the client object at import time and would not follow the plugin's DB swap.
import src.popoto.redis_db as redis_db


class UniqueKeyModel(popoto.Model):
    name = popoto.UniqueKeyField()
    description = popoto.Field(null=True)


class TwoKeyModel(popoto.Model):
    band = popoto.KeyField()
    role = popoto.KeyField()
    name = popoto.Field()


class AutoKeyModel(popoto.Model):
    value = popoto.Field(default="empty")


class KeySetModel(popoto.Model):
    uuid = popoto.AutoKeyField()
    band = popoto.KeyField(unique=False, null=True)
    role = popoto.KeyField(unique=False, null=True)
    name = popoto.Field()


class MutableKeyModel(popoto.Model):
    uuid = popoto.AutoKeyField()
    status = popoto.KeyField(default="pending")
    data = popoto.Field(null=True)


def _field_set_key(model, field_name, value):
    """Redis key of the Set that indexes ``field_name == value``."""
    base = model._meta.fields[field_name].get_special_use_field_db_key(
        model, field_name
    )
    return f"{base}:{value}"


class TestUniqueKeyField:
    def test_saved_instance_equals_loaded_instance(self):
        lisa = UniqueKeyModel(name="Lalisa Manobal")
        lisa.description = "Famous K-Pop Rapper for BlackPink"
        lisa.save()

        assert lisa == UniqueKeyModel.query.get(name="Lalisa Manobal")


class TestCompositeKeyQueries:
    @pytest.fixture
    def band_members(self):
        return {
            "lisa": TwoKeyModel.create(
                band="BLACKPINK", role="rapper", name="Lalisa Manobal"
            ),
            "jennie": TwoKeyModel.create(
                band="BLACKPINK", role="vocals", name="Jennie Kim"
            ),
            "jisoo": TwoKeyModel.create(
                band="BLACKPINK", role="singer", name="Kim Ji-soo"
            ),
            "solar": TwoKeyModel.create(
                band="Mamamoo", role="singer", name="Kim Yong-sun"
            ),
            "moonbyul": TwoKeyModel.create(
                band="Mamamoo", role="rapper", name="Moon Byul-yi"
            ),
        }

    def test_startswith(self, band_members):
        assert len(TwoKeyModel.query.filter(band__startswith="BLACK")) == 3

    def test_endswith(self, band_members):
        assert len(TwoKeyModel.query.filter(band__endswith="PINK")) == 3

    def test_in(self, band_members):
        assert len(TwoKeyModel.query.filter(band__in=["BLACKPINK", "Mamamoo"])) == 5

    def test_exact_match(self, band_members):
        assert len(TwoKeyModel.query.filter(band="BLACKPINK")) == 3

    def test_exact_match_is_case_sensitive(self, band_members):
        assert len(TwoKeyModel.query.filter(band="blackpink")) == 0

    def test_filter_on_second_key_field(self, band_members):
        assert len(TwoKeyModel.query.filter(role="singer")) == 2

    def test_get_by_full_composite_key(self, band_members):
        assert band_members["moonbyul"] == TwoKeyModel.query.get(
            band="Mamamoo", role="rapper"
        )


class TestAutoKeyField:
    def test_instance_is_queryable_and_has_auto_key(self):
        names = AutoKeyModel.create(value="Nayeon, Jeongyeon, Momo, Sana, Jihyo")

        assert names in AutoKeyModel.query.all()
        assert hasattr(names, "_auto_key")
        assert "_auto_key" in names._meta.fields
        assert names._auto_key in names.db_key

    def test_delete_removes_instance(self):
        AutoKeyModel.create(value="temporary")
        for item in AutoKeyModel.query.all():
            item.delete()

        assert len(AutoKeyModel.query.all()) == 0


class TestIllegalKeyFieldTypes:
    @pytest.mark.parametrize("data_type", [list, dict])
    def test_container_typed_key_field_is_rejected(self, data_type):
        with pytest.raises(ModelException):

            class IllegalKeyModel(popoto.Model):
                band = popoto.KeyField(type=data_type)


class TestKeyFieldRedisSets:
    """KeyFields maintain Redis Sets as indexes; they must stay in sync."""

    @pytest.fixture
    def members(self):
        return {
            "lisa": KeySetModel.create(
                band="BLACKPINK", role="rapper", name="Lalisa Manobal"
            ),
            "jisoo": KeySetModel.create(
                band="BLACKPINK", role="singer", name="Kim Ji-soo"
            ),
            "solar": KeySetModel.create(
                band="Mamamoo", role="singer", name="Kim Yong-sun"
            ),
            "moonbyul": KeySetModel.create(
                band="Mamamoo", role="rapper", name="Moon Byul-yi"
            ),
            "anonymous": KeySetModel.create(name="anonymous"),
        }

    def test_class_wide_set_matches_query_all(self, members):
        class_set_key = KeySetModel._meta.db_class_set_key
        assert len(redis_db.POPOTO_REDIS_DB.smembers(class_set_key.redis_key)) == len(
            KeySetModel.query.all()
        )

    def test_class_wide_set_matches_stored_keys(self, members):
        assert len(KeySetModel.query.all()) == len(
            redis_db.POPOTO_REDIS_DB.keys(f"{KeySetModel._meta.db_class_key}:*")
        )

    def test_keyfield_set_holds_exactly_the_matching_instances(self, members):
        bp_key = _field_set_key(KeySetModel, "band", "BLACKPINK")

        assert redis_db.POPOTO_REDIS_DB.smembers(bp_key) == {
            members["lisa"].db_key.redis_key.encode(),
            members["jisoo"].db_key.redis_key.encode(),
        }

    def test_second_keyfield_maintains_its_own_set(self, members):
        singer_key = _field_set_key(KeySetModel, "role", "singer")
        assert len(redis_db.POPOTO_REDIS_DB.smembers(singer_key)) == 2

    def test_filter_by_single_and_composite_key(self, members):
        assert len(KeySetModel.query.filter(band="Mamamoo")) == 2
        assert (
            KeySetModel.query.filter(band="Mamamoo", role="singer")[0]
            == members["solar"]
        )
        assert (
            KeySetModel.query.filter(band="BLACKPINK", role="rapper")[0]
            == members["lisa"]
        )

    def test_delete_shrinks_the_keyfield_set(self, members):
        bp_key = _field_set_key(KeySetModel, "band", "BLACKPINK")

        members["lisa"].delete()
        assert len(redis_db.POPOTO_REDIS_DB.smembers(bp_key)) == 1

        members["jisoo"].delete()
        assert len(redis_db.POPOTO_REDIS_DB.smembers(bp_key)) == 0
        assert len(KeySetModel.query.filter(band="BLACKPINK")) == 0

    def test_isnull_filter_finds_instance_with_null_key_fields(self, members):
        results = KeySetModel.query.filter(role__isnull=True, band__isnull=True)
        assert list(results) == [members["anonymous"]]

    def test_deleting_everything_empties_all_sets(self, members):
        class_set_key = KeySetModel._meta.db_class_set_key
        bp_key = _field_set_key(KeySetModel, "band", "BLACKPINK")
        singer_key = _field_set_key(KeySetModel, "role", "singer")

        for item in KeySetModel.query.all():
            item.delete()

        assert len(KeySetModel.query.all()) == 0
        assert len(redis_db.POPOTO_REDIS_DB.smembers(class_set_key.redis_key)) == 0
        assert len(redis_db.POPOTO_REDIS_DB.smembers(bp_key)) == 0
        assert len(redis_db.POPOTO_REDIS_DB.smembers(singer_key)) == 0


class TestKeyFieldIndexCleanupOnMutation:
    """#149: on_save() must remove the instance from its old index."""

    @pytest.fixture
    def jobs(self):
        return {
            "job1": MutableKeyModel.create(status="pending", data="job1"),
            "job2": MutableKeyModel.create(status="pending", data="job2"),
        }

    def test_setup_places_both_jobs_in_pending(self, jobs):
        pending_key = _field_set_key(MutableKeyModel, "status", "pending")

        assert len(MutableKeyModel.query.filter(status="pending")) == 2
        assert len(MutableKeyModel.query.filter(status="running")) == 0
        assert len(redis_db.POPOTO_REDIS_DB.smembers(pending_key)) == 2

    def test_mutating_key_moves_instance_between_indexes(self, jobs):
        pending_key = _field_set_key(MutableKeyModel, "status", "pending")
        running_key = _field_set_key(MutableKeyModel, "status", "running")

        # migrate_key=True is required: KeyFields are immutable by default
        jobs["job1"].status = "running"
        jobs["job1"].save(migrate_key=True)

        assert (
            len(MutableKeyModel.query.filter(status="pending")) == 1
        ), "Ghost entry: job1 still appears in pending index after status change"
        assert (
            len(MutableKeyModel.query.filter(status="running")) == 1
        ), "job1 should appear in running index after status change"
        assert len(redis_db.POPOTO_REDIS_DB.smembers(pending_key)) == 1
        assert len(redis_db.POPOTO_REDIS_DB.smembers(running_key)) == 1

    def test_correct_instances_land_in_each_index(self, jobs):
        jobs["job1"].status = "running"
        jobs["job1"].save(migrate_key=True)

        assert MutableKeyModel.query.filter(status="pending")[0].data == "job2"
        assert MutableKeyModel.query.filter(status="running")[0].data == "job1"

    def test_second_mutation_cleans_the_intermediate_index(self, jobs):
        running_key = _field_set_key(MutableKeyModel, "status", "running")
        completed_key = _field_set_key(MutableKeyModel, "status", "completed")

        jobs["job1"].status = "running"
        jobs["job1"].save(migrate_key=True)
        jobs["job1"].status = "completed"
        jobs["job1"].save(migrate_key=True)

        assert len(MutableKeyModel.query.filter(status="running")) == 0
        assert len(MutableKeyModel.query.filter(status="completed")) == 1
        assert len(redis_db.POPOTO_REDIS_DB.smembers(running_key)) == 0
        assert len(redis_db.POPOTO_REDIS_DB.smembers(completed_key)) == 1
