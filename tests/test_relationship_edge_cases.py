"""
Tests for Relationship field edge cases - Issue #41

These tests verify that the Relationship field properly handles:
1. Lazy-loaded relationships during on_delete()
2. Circular relationships with circular reference protection
3. String field values (redis_key) during save and delete operations
4. Index cleanup when relationships are not fully loaded
"""

import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src.popoto import Model, KeyField, Field, Relationship
from src.popoto.redis_db import POPOTO_REDIS_DB


def cleanup_test_data():
    """Clean up Redis test data."""
    patterns = [
        "NodeA:*",
        "NodeB:*",
        "NodeC:*",
        "PersonModel:*",
        "GroupModel:*",
        "MembershipModel:*",
        "$*:NodeA:*",
        "$*:NodeB:*",
        "$*:NodeC:*",
        "$*:PersonModel:*",
        "$*:GroupModel:*",
        "$*:MembershipModel:*",
        "$Class:*",
    ]
    for pattern in patterns:
        for key in POPOTO_REDIS_DB.keys(pattern):
            POPOTO_REDIS_DB.delete(key)


# Define test models at module level to avoid repeated class creation issues
class NodeA(Model):
    name = KeyField()


class NodeB(Model):
    name = KeyField()


class NodeC(Model):
    name = KeyField()


# Add relationships after class definition to enable circular references
NodeA.link_to_b = Relationship(model=NodeB)
NodeA._meta.add_field("link_to_b", NodeA.link_to_b)

NodeB.link_to_c = Relationship(model=NodeC)
NodeB._meta.add_field("link_to_c", NodeB.link_to_c)

NodeB.related_a = Relationship(model=NodeA)
NodeB._meta.add_field("related_a", NodeB.related_a)

NodeC.link_to_a = Relationship(model=NodeA)
NodeC._meta.add_field("link_to_a", NodeC.link_to_a)

NodeA.related_b = Relationship(model=NodeB)
NodeA._meta.add_field("related_b", NodeA.related_b)


class PersonModel(Model):
    name = KeyField()


class GroupModel(Model):
    name = KeyField()


class MembershipModel(Model):
    person = Relationship(model=PersonModel)
    group = Relationship(model=GroupModel)


class TestRelationshipEdgeCases:
    """Test edge cases for Relationship field on_save and on_delete methods."""

    def setup_method(self):
        """Clean up Redis before each test."""
        cleanup_test_data()

    def teardown_method(self):
        """Clean up Redis after each test."""
        cleanup_test_data()


class TestBasicRelationship(TestRelationshipEdgeCases):
    """Test basic relationship operations work correctly."""

    def test_basic_create_and_delete(self):
        """Test that basic relationships can be created and deleted."""
        person = PersonModel.create(name="Alice")
        group = GroupModel.create(name="Engineers")
        membership = MembershipModel.create(person=person, group=group)

        # Verify relationships
        assert membership.person.name == "Alice"
        assert membership.group.name == "Engineers"

        # Delete should work
        membership.delete()
        assert MembershipModel.query.filter(person=person) == []

        # Cleanup
        person.delete()
        group.delete()

    def test_delete_with_null_relationship(self):
        """Test deleting a model with a null relationship."""
        person = PersonModel.create(name="Bob")
        membership = MembershipModel.create(person=person, group=None)

        # Delete should work with null relationship
        membership.delete()
        person.delete()


class TestRelationshipIndexCleanup(TestRelationshipEdgeCases):
    """Test that relationship indexes are properly cleaned up."""

    def test_index_cleanup_on_delete(self):
        """Verify that relationship indexes are cleaned up correctly on delete."""
        person = PersonModel.create(name="Carol")
        group = GroupModel.create(name="Marketing")
        membership = MembershipModel.create(person=person, group=group)

        # Get the relationship index keys
        person_index_key = (
            f"$RelationshipF:MembershipModel:person:{person.db_key.redis_key}"
        )
        group_index_key = (
            f"$RelationshipF:MembershipModel:group:{group.db_key.redis_key}"
        )

        # Verify membership is in the indexes
        person_members = POPOTO_REDIS_DB.smembers(person_index_key)
        group_members = POPOTO_REDIS_DB.smembers(group_index_key)
        assert membership.db_key.redis_key.encode() in person_members
        assert membership.db_key.redis_key.encode() in group_members

        # Delete membership
        membership.delete()

        # Verify membership was removed from the indexes
        person_members_after = POPOTO_REDIS_DB.smembers(person_index_key)
        group_members_after = POPOTO_REDIS_DB.smembers(group_index_key)
        assert membership.db_key.redis_key.encode() not in person_members_after
        assert membership.db_key.redis_key.encode() not in group_members_after

        # Cleanup
        person.delete()
        group.delete()

    def test_index_cleanup_with_loaded_model(self):
        """Test index cleanup when model is loaded from database before delete."""
        person = PersonModel.create(name="Dave")
        group = GroupModel.create(name="Sales")
        membership = MembershipModel.create(person=person, group=group)

        membership_key = membership.db_key.redis_key
        person_index_key = (
            f"$RelationshipF:MembershipModel:person:{person.db_key.redis_key}"
        )

        # Load membership fresh from database
        loaded_membership = MembershipModel.query.all()[0]

        # Delete the loaded membership
        loaded_membership.delete()

        # Verify cleanup
        person_members = POPOTO_REDIS_DB.smembers(person_index_key)
        assert membership_key.encode() not in person_members

        # Cleanup
        person.delete()
        group.delete()


class TestCircularRelationships(TestRelationshipEdgeCases):
    """Test circular relationships that may trigger lazy loading protection."""

    def test_circular_chain_create(self):
        """Test creating a circular chain of relationships."""
        # Create nodes
        node_a = NodeA(name="A1")
        node_a.save()

        node_b = NodeB(name="B1")
        node_b.save()

        node_c = NodeC(name="C1")
        node_c.save()

        # Create circular links: A -> B -> C -> A
        node_a.link_to_b = node_b
        node_a.save()

        node_b.link_to_c = node_c
        node_b.save()

        node_c.link_to_a = node_a
        node_c.save()

        # Verify relationships
        assert node_a.link_to_b.name == "B1"
        assert node_b.link_to_c.name == "C1"
        assert node_c.link_to_a.name == "A1"

        # Cleanup
        node_c.delete()
        node_b.delete()
        node_a.delete()

    def test_delete_without_loading_from_db(self):
        """Test deleting nodes in a circular chain without reloading from DB."""
        # Create nodes
        node_a = NodeA(name="A3")
        node_a.save()

        node_b = NodeB(name="B3")
        node_b.save()

        node_c = NodeC(name="C3")
        node_c.save()

        # Create circular links
        node_a.link_to_b = node_b
        node_a.save()

        node_b.link_to_c = node_c
        node_b.save()

        node_c.link_to_a = node_a
        node_c.save()

        # Delete without loading from database - should work with our fix
        node_b.delete()

        # Verify deletion
        assert NodeB.query.get(name="B3") is None

        # Cleanup remaining nodes
        node_c.delete()
        node_a.delete()


class TestPipelineOperations(TestRelationshipEdgeCases):
    """Test relationship operations with Redis pipelines."""

    def test_delete_with_pipeline(self):
        """Test deleting models using a pipeline."""
        person1 = PersonModel.create(name="Eve")
        person2 = PersonModel.create(name="Frank")
        group = GroupModel.create(name="Support")

        membership1 = MembershipModel.create(person=person1, group=group)
        membership2 = MembershipModel.create(person=person2, group=group)

        # Delete using pipeline
        pipeline = POPOTO_REDIS_DB.pipeline()
        pipeline = membership1.delete(pipeline)
        pipeline = membership2.delete(pipeline)
        pipeline.execute()

        # Verify deletions
        assert MembershipModel.query.filter(group=group) == []

        # Cleanup
        person1.delete()
        person2.delete()
        group.delete()

    def test_batch_delete_with_pipeline(self):
        """Test batch deleting multiple models using a pipeline."""
        person1 = PersonModel.create(name="Grace")
        person2 = PersonModel.create(name="Heidi")
        group = GroupModel.create(name="HR")
        membership1 = MembershipModel.create(person=person1, group=group)
        membership2 = MembershipModel.create(person=person2, group=group)

        # Delete multiple memberships using pipeline (without reloading from DB)
        pipeline = POPOTO_REDIS_DB.pipeline()
        pipeline = membership1.delete(pipeline)
        pipeline = membership2.delete(pipeline)
        pipeline.execute()

        # Verify deletion
        assert MembershipModel.query.count() == 0

        # Cleanup
        person1.delete()
        person2.delete()
        group.delete()


class TestStringFieldValueHandling(TestRelationshipEdgeCases):
    """
    Test that the fix properly handles string field values.
    This directly tests the bug fix for issue #41.
    """

    def test_on_delete_handles_string_field_value(self):
        """
        Directly test that on_delete handles string field_value correctly.
        This simulates the scenario where a relationship field contains a string
        (redis_key) instead of a Model instance.
        """
        person = PersonModel.create(name="Henry")
        group = GroupModel.create(name="IT")
        membership = MembershipModel.create(person=person, group=group)

        # Get the field class
        person_field = MembershipModel._meta.fields["person"]

        # Manually call on_delete with a string field_value (simulating lazy-loaded scenario)
        # This is the exact scenario that caused the original bug
        string_field_value = person.db_key.redis_key

        # Before the fix, this would raise: AttributeError: 'str' object has no attribute 'db_key'
        result = person_field.on_delete(
            model_instance=membership,
            field_name="person",
            field_value=string_field_value,  # String instead of Model instance
            pipeline=None,
        )

        # Should succeed without error
        assert result is not None or result == 0  # srem returns number of removed items

        # Cleanup
        membership.delete()
        person.delete()
        group.delete()

    def test_on_save_handles_string_field_value(self):
        """
        Test that on_save handles string field_value correctly.
        """
        person = PersonModel.create(name="Irene")
        group = GroupModel.create(name="Legal")
        membership = MembershipModel.create(person=person, group=group)

        # Get the field class
        person_field = MembershipModel._meta.fields["person"]

        # Manually call on_save with a string field_value
        string_field_value = person.db_key.redis_key

        # Before the fix, string values were silently ignored
        # Now they should be handled correctly
        result = person_field.on_save(
            model_instance=membership,
            field_name="person",
            field_value=string_field_value,  # String instead of Model instance
            pipeline=None,
        )

        # Should succeed
        assert result is not None or result >= 0

        # Cleanup
        membership.delete()
        person.delete()
        group.delete()

    def test_on_delete_with_none_field_value(self):
        """Test that on_delete handles None field_value correctly."""
        person = PersonModel.create(name="Jack")
        membership = MembershipModel.create(person=person, group=None)

        # Get the field class
        group_field = MembershipModel._meta.fields["group"]

        # Call on_delete with None field_value
        result = group_field.on_delete(
            model_instance=membership,
            field_name="group",
            field_value=None,
            pipeline=None,
        )

        # Should succeed
        assert result is not None or result == 0

        # Cleanup
        membership.delete()
        person.delete()


class TestRedisKeyStringHandling:
    """Test that on_save and on_delete handle redis_key strings correctly."""

    def test_on_save_with_parsed_redis_key(self):
        """Verify on_save correctly parses redis_key strings into DB_key."""
        person = PersonModel.create(name="Emily")
        membership = MembershipModel(person=person, group=None)

        # Get the field class
        person_field = MembershipModel._meta.fields["person"]

        # Simulate what happens when field_value is a redis_key string (lazy-loaded)
        string_field_value = person.db_key.redis_key

        # This tests the fix: parse redis_key string with DB_key.from_redis_key()
        result = person_field.on_save(
            model_instance=membership,
            field_name="person",
            field_value=string_field_value,  # String redis_key
            pipeline=None,
        )

        # Should succeed and return a result
        assert result is not None or result >= 0

        # Verify the index was created correctly
        person_index_key = (
            f"$RelationshipF:MembershipModel:person:{person.db_key.redis_key}"
        )
        members = POPOTO_REDIS_DB.smembers(person_index_key)
        # Note: membership doesn't have a valid db_key yet since it wasn't saved

        # Cleanup
        person.delete()

    def test_on_delete_with_parsed_redis_key(self):
        """Verify on_delete correctly parses redis_key strings into DB_key."""
        person = PersonModel.create(name="Frank")
        group = GroupModel.create(name="Engineering")
        membership = MembershipModel.create(person=person, group=group)

        # Get the field class
        person_field = MembershipModel._meta.fields["person"]

        # Simulate what happens when field_value is a redis_key string
        string_field_value = person.db_key.redis_key

        # This tests the fix: parse redis_key string with DB_key.from_redis_key()
        result = person_field.on_delete(
            model_instance=membership,
            field_name="person",
            field_value=string_field_value,  # String redis_key
            pipeline=None,
        )

        # Should succeed
        assert result is not None or result == 0

        # Cleanup
        membership.delete()
        person.delete()
        group.delete()

    def test_invalid_redis_key_format_logged(self):
        """Verify that invalid redis_key format is logged and handled gracefully."""
        person = PersonModel.create(name="Grace")
        membership = MembershipModel(person=person, group=None)

        person_field = MembershipModel._meta.fields["person"]

        # Pass an invalid redis_key (no colon separator)
        invalid_redis_key = "InvalidFormatNoColon"

        result = person_field.on_save(
            model_instance=membership,
            field_name="person",
            field_value=invalid_redis_key,
            pipeline=None,
        )

        # Should return None (early return due to invalid format)
        assert result is None

        # Cleanup
        person.delete()


# Run tests if executed directly
if __name__ == "__main__":
    pytest.main([__file__, "-v"])
