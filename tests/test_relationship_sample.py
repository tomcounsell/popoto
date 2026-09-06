"""Tests for ``Relationship.sample_related_keys`` — issue #646 (#630 series).

The method is the bounded (``SRANDMEMBER``) counterpart to ``filter_query``'s
unbounded ``SMEMBERS`` read of the Relationship reverse index. It exists so
``recipes/graph_traversal`` can sample edges without rebuilding index keys by
hand; these tests pin the contract that recipe now depends on.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

from src.popoto import Field, KeyField, Model, Relationship
from src.popoto.models.db_key import DB_key


class RSNode(Model):
    key = KeyField()
    content = Field(type=str, default="")


RSNode.related = Relationship(model=RSNode, null=True)
RSNode._meta.add_field("related", RSNode.related)


@pytest.fixture
def hub():
    """A hub node with three distinct nodes pointing at it."""
    target = RSNode.create(key="rs-hub", content="hub")
    pointers = []
    for name in ("rs-p1", "rs-p2", "rs-p3"):
        node = RSNode.create(key=name)
        node.related = target
        node.save()
        pointers.append(node)
    yield target, pointers
    for node in pointers + [target]:
        node.delete()


class TestSampleRelatedKeys:
    def test_returns_pointing_members(self, hub):
        target, pointers = hub
        got = Relationship.sample_related_keys(
            RSNode, "related", target.db_key.redis_key, 10
        )
        assert set(got) == {p.db_key.redis_key for p in pointers}

    def test_members_are_str_not_bytes(self, hub):
        target, _ = hub
        got = Relationship.sample_related_keys(
            RSNode, "related", target.db_key.redis_key, 10
        )
        assert got
        assert all(isinstance(member, str) for member in got)

    def test_count_bounds_the_sample(self, hub):
        target, _ = hub
        got = Relationship.sample_related_keys(
            RSNode, "related", target.db_key.redis_key, 2
        )
        assert len(got) == 2

    def test_count_zero_returns_empty(self, hub):
        target, _ = hub
        assert (
            Relationship.sample_related_keys(
                RSNode, "related", target.db_key.redis_key, 0
            )
            == []
        )

    def test_missing_reverse_index_returns_empty_list(self):
        # Never saved, so no reverse index Set exists for it.
        got = Relationship.sample_related_keys(
            RSNode, "related", "RSNode:rs-never-written", 5
        )
        assert got == []

    def test_str_and_db_key_forms_agree(self, hub):
        """The colon trap: a redis_key str must be parsed, not re-escaped.

        ``DB_key("RSNode:rs-hub")`` escapes the colon and addresses a
        different key; ``DB_key.from_redis_key`` is the correct parse. Both
        supported argument forms must reach the same Set.
        """
        target, pointers = hub
        from_str = Relationship.sample_related_keys(
            RSNode, "related", target.db_key.redis_key, 10
        )
        from_db_key = Relationship.sample_related_keys(
            RSNode, "related", target.db_key, 10
        )
        assert set(from_str) == set(from_db_key)
        assert set(from_str) == {p.db_key.redis_key for p in pointers}

        # And the naive construction really does address elsewhere, which is
        # why the str arm cannot use it.
        assert DB_key(target.db_key.redis_key).redis_key != target.db_key.redis_key

    def test_no_exception_handling_of_its_own(self, hub, monkeypatch):
        """Failures propagate — degradation policy belongs to the caller."""
        from src.popoto.fields import relationship as relationship_module

        class Boom:
            def srandmember(self, *args, **kwargs):
                raise RuntimeError("redis down")

        monkeypatch.setattr(relationship_module, "POPOTO_REDIS_DB", Boom())
        target, _ = hub
        with pytest.raises(RuntimeError, match="redis down"):
            Relationship.sample_related_keys(
                RSNode, "related", target.db_key.redis_key, 5
            )
