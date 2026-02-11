"""Tests for Model.to_dict() serialization method."""

import pytest
from popoto import Model, Field, KeyField, SortedField, Relationship
from popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Test models
# ---------------------------------------------------------------------------


class SimpleModel(Model):
    name = KeyField()
    value = Field(type=str, null=True)


class Author(Model):
    name = KeyField()
    bio = Field(type=str, null=True)


class Book(Model):
    title = KeyField()
    pages = Field(type=int, null=True)
    author = Relationship(model=Author)


class PersonA(Model):
    name = KeyField()


class PersonB(Model):
    name = KeyField()


# Add cross-referencing relationships after class definition
PersonA.friend = Relationship(model=PersonB, null=True)
PersonA._meta.add_field("friend", PersonA.friend)

PersonB.friend = Relationship(model=PersonA, null=True)
PersonB._meta.add_field("friend", PersonB.friend)


class ScoredItem(Model):
    label = KeyField()
    score = SortedField()


# Chain models: C -> B -> A (no circular ref)
class ChainA(Model):
    name = KeyField()


class ChainB(Model):
    name = KeyField()
    ref = Relationship(model=ChainA, null=True)


class ChainC(Model):
    name = KeyField()
    ref = Relationship(model=ChainB, null=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MODEL_CLASSES = [
    SimpleModel,
    Author,
    Book,
    PersonA,
    PersonB,
    ScoredItem,
    ChainA,
    ChainB,
    ChainC,
]


def _cleanup():
    """Clean up Redis test data for all test model classes."""
    for cls in MODEL_CLASSES:
        pattern = f"{cls.__name__}:*"
        for key in POPOTO_REDIS_DB.keys(pattern):
            POPOTO_REDIS_DB.delete(key)
        # Clean class sets
        class_set_key = f"$Class:{cls.__name__}"
        POPOTO_REDIS_DB.delete(class_set_key)
    # Clean relationship index keys
    for pattern in ["$RelationshipF:*"]:
        for key in POPOTO_REDIS_DB.keys(pattern):
            POPOTO_REDIS_DB.delete(key)


@pytest.fixture(autouse=True)
def cleanup():
    """Clean up test data before and after each test."""
    _cleanup()
    yield
    _cleanup()


# ---------------------------------------------------------------------------
# Basic serialization
# ---------------------------------------------------------------------------


class TestBasicToDict:
    def test_simple_model_all_fields(self):
        """to_dict() returns all explicit fields."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict()
        assert d == {"name": "hello", "value": "world"}

    def test_simple_model_null_field(self):
        """Null field values appear as None in dict."""
        obj = SimpleModel.create(name="hello")
        d = obj.to_dict()
        assert d == {"name": "hello", "value": None}

    def test_sorted_field_included(self):
        """SortedField values appear in to_dict output."""
        obj = ScoredItem.create(label="item1", score=42.5)
        d = obj.to_dict()
        assert d["label"] == "item1"
        assert d["score"] == 42.5

    def test_returns_dict_type(self):
        """Return type is a plain dict."""
        obj = SimpleModel.create(name="test", value="v")
        result = obj.to_dict()
        assert type(result) is dict

    def test_hidden_fields_excluded(self):
        """Hidden fields (underscore-prefixed) are not in output."""
        obj = SimpleModel.create(name="test", value="v")
        d = obj.to_dict()
        for key in d:
            assert not key.startswith("_"), f"Hidden field {key} should not appear"


# ---------------------------------------------------------------------------
# Include / exclude filtering
# ---------------------------------------------------------------------------


class TestIncludeExclude:
    def test_include_single_field(self):
        """include limits output to specified fields."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(include={"name"})
        assert d == {"name": "hello"}

    def test_include_as_list(self):
        """include accepts a list as well as a set."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(include=["name"])
        assert d == {"name": "hello"}

    def test_exclude_single_field(self):
        """exclude removes specified fields from output."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(exclude={"value"})
        assert d == {"name": "hello"}

    def test_exclude_as_list(self):
        """exclude accepts a list as well as a set."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(exclude=["value"])
        assert d == {"name": "hello"}

    def test_include_nonexistent_field(self):
        """Including a nonexistent field simply omits it (no error)."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(include={"name", "nonexistent"})
        assert d == {"name": "hello"}

    def test_exclude_nonexistent_field(self):
        """Excluding a nonexistent field has no effect."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(exclude={"nonexistent"})
        assert d == {"name": "hello", "value": "world"}

    def test_include_relationship_field(self):
        """include can include a relationship field."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict(include={"title", "author"})
        assert "title" in d
        assert "author" in d
        assert "pages" not in d

    def test_exclude_relationship_field(self):
        """exclude can exclude a relationship field."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict(exclude={"author"})
        assert "author" not in d
        assert d == {"title": "Hobbit", "pages": 310}


# ---------------------------------------------------------------------------
# Relationship handling (default shallow mode)
# ---------------------------------------------------------------------------


class TestRelationshipShallow:
    def test_relationship_as_redis_key(self):
        """By default, relationships are serialized as redis_key strings."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict()
        assert d["title"] == "Hobbit"
        assert d["pages"] == 310
        # The relationship should be a redis_key string
        assert isinstance(d["author"], str)
        assert "Tolkien" in d["author"]

    def test_null_relationship(self):
        """Null relationships appear as None."""
        book = Book.create(title="Orphan", pages=100, author=None)
        d = book.to_dict()
        assert d["author"] is None


# ---------------------------------------------------------------------------
# Relationship expansion (relationships=True)
# ---------------------------------------------------------------------------


class TestRelationshipExpansion:
    def test_expand_relationship(self):
        """relationships=True produces nested dict for related instance."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict(relationships=True)
        assert isinstance(d["author"], dict)
        assert d["author"]["name"] == "Tolkien"
        assert d["author"]["bio"] == "Fantasy author"

    def test_expand_null_relationship(self):
        """Null relationships remain None even with relationships=True."""
        book = Book.create(title="Orphan", pages=100, author=None)
        d = book.to_dict(relationships=True)
        assert d["author"] is None

    def test_circular_reference_protection(self):
        """Circular references fall back to redis_key string."""
        alice = PersonA.create(name="alice", friend=None)
        bob = PersonB.create(name="bob", friend=alice)
        # Update alice to point to bob (circular)
        alice.friend = bob
        alice.save()
        # Reload to get fresh state
        alice = PersonA.query.get(name="alice")

        d = alice.to_dict(relationships=True)
        # alice -> bob should be expanded
        assert isinstance(d["friend"], dict)
        assert d["friend"]["name"] == "bob"
        # bob -> alice should be a redis_key string (circular ref)
        friend_of_friend = d["friend"]["friend"]
        assert isinstance(friend_of_friend, str)
        assert "alice" in friend_of_friend


# ---------------------------------------------------------------------------
# max_depth limiting
# ---------------------------------------------------------------------------


class TestMaxDepth:
    def test_max_depth_zero(self):
        """max_depth=0 prevents any relationship expansion."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict(relationships=True, max_depth=0)
        # Should be redis_key string, not expanded
        assert isinstance(d["author"], str)

    def test_max_depth_one_chain(self):
        """max_depth=1 expands one level then falls back to redis_key."""
        a = ChainA.create(name="alpha")
        b = ChainB.create(name="beta", ref=a)
        c = ChainC.create(name="gamma", ref=b)

        d = c.to_dict(relationships=True, max_depth=1)
        # gamma -> beta should be expanded (depth 1)
        assert isinstance(d["ref"], dict)
        assert d["ref"]["name"] == "beta"
        # beta -> alpha should be redis_key string (depth exhausted)
        assert isinstance(d["ref"]["ref"], str)
        assert "alpha" in d["ref"]["ref"]

    def test_max_depth_two_chain(self):
        """max_depth=2 expands two levels in a chain."""
        a = ChainA.create(name="alpha")
        b = ChainB.create(name="beta", ref=a)
        c = ChainC.create(name="gamma", ref=b)

        d = c.to_dict(relationships=True, max_depth=2)
        # gamma -> beta should be expanded
        assert isinstance(d["ref"], dict)
        assert d["ref"]["name"] == "beta"
        # beta -> alpha should also be expanded (depth 2 allows it)
        assert isinstance(d["ref"]["ref"], dict)
        assert d["ref"]["ref"]["name"] == "alpha"

    def test_max_depth_none_is_unlimited(self):
        """max_depth=None (default) allows unlimited expansion."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        d = book.to_dict(relationships=True, max_depth=None)
        assert isinstance(d["author"], dict)
        assert d["author"]["name"] == "Tolkien"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_unsaved_instance(self):
        """to_dict works on an unsaved instance."""
        obj = SimpleModel(name="unsaved", value="data")
        d = obj.to_dict()
        assert d == {"name": "unsaved", "value": "data"}

    def test_empty_include_returns_empty_dict(self):
        """An empty include set produces an empty dict."""
        obj = SimpleModel.create(name="hello", value="world")
        d = obj.to_dict(include=set())
        assert d == {}

    def test_include_and_exclude_combined(self):
        """When both include and exclude are given, both are applied."""
        author = Author.create(name="Tolkien", bio="Fantasy author")
        book = Book.create(title="Hobbit", pages=310, author=author)
        # Include title and pages, then exclude pages
        d = book.to_dict(include={"title", "pages"}, exclude={"pages"})
        assert d == {"title": "Hobbit"}

    def test_to_dict_does_not_modify_instance(self):
        """Calling to_dict does not alter the model instance."""
        obj = SimpleModel.create(name="hello", value="world")
        original_name = obj.name
        original_value = obj.value
        obj.to_dict()
        assert obj.name == original_name
        assert obj.value == original_value

    def test_to_dict_after_reload(self):
        """to_dict works correctly on instances loaded from Redis."""
        SimpleModel.create(name="reloaded", value="data")
        loaded = SimpleModel.query.get(name="reloaded")
        d = loaded.to_dict()
        assert d == {"name": "reloaded", "value": "data"}
