"""
Tests for Model.__eq__ identity semantics (#503).

Equality is *identity* equality, not value equality: two instances are equal
when they are the same class with the same db_key.

The exception is a never-saved instance whose KeyFields are *all* None.
db_key renders a missing KeyField as the literal string "None", so every such
instance collapses onto one class-wide placeholder key; they compare equal
only to themselves.

A partially-set key is NOT an exception -- it still addresses one specific
record -- and neither is a persisted None key, since KeyField(null=True)
(typically paired with an AutoKeyField) stores None as a legitimate key
component. See TestNullableKeyFieldEquality.
"""

import pytest

from src.popoto import Model, KeyField, Field, AutoKeyField


class EqNullableKey(Model):
    uuid = AutoKeyField()
    band = KeyField(unique=False, null=True)
    role = KeyField(unique=False, null=True)
    name = Field()


class EqUser(Model):
    name = KeyField()
    email = Field(type=str, null=True)


class EqCompositeKey(Model):
    org = KeyField()
    email = KeyField()


class EqOtherUser(Model):
    name = KeyField()


class TestSavedInstanceEquality:
    """Persisted instances compare by db_key."""

    def test_same_key_is_equal(self):
        a = EqUser(name="alice")
        a.save()
        b = EqUser.query.get(name="alice")
        assert a == b

    def test_different_key_is_not_equal(self):
        a = EqUser(name="alice")
        b = EqUser(name="bob")
        assert a != b

    def test_different_class_same_key_is_not_equal(self):
        assert EqUser(name="alice") != EqOtherUser(name="alice")

    def test_non_key_field_does_not_affect_equality(self):
        """Equality is by key identity, not by value."""
        a = EqUser(name="alice", email="a@example.com")
        b = EqUser(name="alice", email="different@example.com")
        assert a == b

    def test_comparison_to_non_model_is_false(self):
        assert EqUser(name="alice") != "EqUser:alice"
        assert EqUser(name="alice") != None  # noqa: E711


class TestUnsavedInstanceEquality:
    """#503: transient instances are equal only to themselves.

    The guard previously tested ``self._meta.fields.get(name)``, which returns
    the Field *descriptor* and is never None, so it never fired. Both branches
    then compared the same thing anyway, since __repr__ is built from db_key.
    """

    def test_distinct_unset_instances_are_not_equal(self):
        a, b = EqUser(), EqUser()
        assert (
            a.db_key.redis_key == b.db_key.redis_key
        ), "precondition: unset keys collapse onto the same db_key"
        assert a != b

    def test_unset_instance_equals_itself(self):
        a = EqUser()
        assert a == a

    def test_unset_is_not_equal_to_saved(self):
        saved = EqUser(name="alice")
        assert EqUser() != saved
        assert saved != EqUser()

    def test_partially_set_composite_key_still_compares_by_key(self):
        """A partial key still addresses one specific record, so it is not
        transient -- only an all-None key is a shared placeholder."""
        a = EqCompositeKey(org="acme")
        b = EqCompositeKey(org="acme")
        assert a == b
        assert a != EqCompositeKey(org="other")

    def test_fully_unset_composite_key_is_not_equal(self):
        a, b = EqCompositeKey(), EqCompositeKey()
        assert a != b
        assert a == a

    def test_fully_set_composite_key_compares_by_value(self):
        a = EqCompositeKey(org="acme", email="alice@example.com")
        b = EqCompositeKey(org="acme", email="alice@example.com")
        assert a == b

    def test_unset_instances_of_different_classes_are_not_equal(self):
        assert EqUser() != EqOtherUser()


class TestNullableKeyFieldEquality:
    """A saved None KeyField is a real key component, not an unset one.

    KeyField(null=True) stores None as part of the key, typically alongside
    an AutoKeyField that keeps it unique. Such instances are persisted and
    must still compare by db_key -- treating "any None KeyField" as transient
    would wrongly make them equal only to themselves.
    """

    def test_saved_null_key_instance_equals_reloaded_copy(self):
        anon = EqNullableKey.create(name="anonymous")
        reloaded = EqNullableKey.query.get(uuid=anon.uuid)

        assert anon.band is None and anon.role is None
        assert anon is not reloaded
        assert anon == reloaded

    def test_saved_null_key_instances_are_distinct_from_each_other(self):
        a = EqNullableKey.create(name="one")
        b = EqNullableKey.create(name="two")
        assert a != b

    def test_query_result_equality_with_null_key_filters(self):
        anon = EqNullableKey.create(name="anonymous")
        results = list(EqNullableKey.query.filter(band__isnull=True, role__isnull=True))
        assert results == [anon]


@pytest.fixture(autouse=True)
def cleanup():
    yield
    for model in (EqUser, EqCompositeKey, EqOtherUser, EqNullableKey):
        for instance in model.query.all():
            instance.delete()
