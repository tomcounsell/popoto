"""
Test callable defaults for Field.

GitHub Issue #25: Field `default` can be a callable

This test verifies that fields can accept callable defaults like uuid.uuid4 or dict,
which are called for each new instance rather than shared across all instances.
"""

import sys
import os
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

from src import popoto


def test_callable_uuid_default():
    """Each instance should get a unique UUID when using uuid.uuid4 as default."""

    class UUIDModel(popoto.Model):
        unique_id = popoto.KeyField(default=uuid.uuid4)
        name = popoto.Field()

    instance1 = UUIDModel(name="test1")
    instance2 = UUIDModel(name="test2")

    assert instance1.unique_id != instance2.unique_id, "UUIDs should be different"
    # Note: KeyField converts to str, so the UUID is stored as string
    assert isinstance(instance1.unique_id, str), "Should be a string (KeyField type)"
    # Verify it's a valid UUID string
    uuid.UUID(instance1.unique_id)  # Will raise ValueError if invalid

    # Cleanup - delete any saved instances
    for item in UUIDModel.query.all():
        item.delete()


def test_callable_dict_default():
    """Each instance should get its own dict when using dict as default."""

    class DictDefaultModel(popoto.Model):
        data = popoto.DictField(default=dict)

    instance1 = DictDefaultModel()
    instance2 = DictDefaultModel()

    # Modify one instance's dict
    instance1.data["key"] = "value"

    assert instance2.data == {}, "Instance 2 should have empty dict (not shared)"
    assert instance1.data == {"key": "value"}, "Instance 1 should have the value"

    # Cleanup
    for item in DictDefaultModel.query.all():
        item.delete()


def test_callable_list_default():
    """Each instance should get its own list when using list as default."""

    class ListDefaultModel(popoto.Model):
        items = popoto.ListField(default=list)

    instance1 = ListDefaultModel()
    instance2 = ListDefaultModel()

    instance1.items.append("item1")

    assert instance2.items == [], "Instance 2 should have empty list (not shared)"
    assert instance1.items == ["item1"], "Instance 1 should have the item"

    # Cleanup
    for item in ListDefaultModel.query.all():
        item.delete()


def test_static_default_still_works():
    """Non-callable defaults should still work as expected."""

    class StaticDefaultModel(popoto.Model):
        value = popoto.Field(default="static_value")

    instance = StaticDefaultModel()
    assert instance.value == "static_value", "Static default should work"

    # Cleanup
    for item in StaticDefaultModel.query.all():
        item.delete()


def test_save_and_load_with_callable_default():
    """Saving and loading models with callable defaults should work correctly."""

    class UUIDModel(popoto.Model):
        unique_id = popoto.KeyField(default=uuid.uuid4)
        name = popoto.Field()

    # Create and save
    instance1 = UUIDModel(name="test1")
    original_uuid = instance1.unique_id
    instance1.save()

    # Load back
    loaded = UUIDModel.query.get(unique_id=str(original_uuid))
    assert str(loaded.unique_id) == str(original_uuid), "UUID should match after loading"
    assert loaded.name == "test1", "Name should match after loading"

    # Cleanup
    for item in UUIDModel.query.all():
        item.delete()


def test_lambda_callable_default():
    """Lambda functions should work as callable defaults."""

    counter = [0]  # Use list to allow mutation in lambda

    def get_next_id():
        counter[0] += 1
        return counter[0]

    class CounterModel(popoto.Model):
        seq_id = popoto.IntField(default=get_next_id)

    instance1 = CounterModel()
    instance2 = CounterModel()
    instance3 = CounterModel()

    assert instance1.seq_id == 1
    assert instance2.seq_id == 2
    assert instance3.seq_id == 3

    # Cleanup
    for item in CounterModel.query.all():
        item.delete()


if __name__ == "__main__":
    test_callable_uuid_default()
    print("test_callable_uuid_default passed")

    test_callable_dict_default()
    print("test_callable_dict_default passed")

    test_callable_list_default()
    print("test_callable_list_default passed")

    test_static_default_still_works()
    print("test_static_default_still_works passed")

    test_save_and_load_with_callable_default()
    print("test_save_and_load_with_callable_default passed")

    test_lambda_callable_default()
    print("test_lambda_callable_default passed")

    print("\nAll tests passed!")
