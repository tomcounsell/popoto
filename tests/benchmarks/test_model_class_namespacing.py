"""Construction-invariant tests for per-item benchmark model classes (#701).

Every per-item factory in the benchmark harness must build its Model class
with ``type()`` so the metaclass captures ``_meta.db_class_key`` (and the
downstream ``db_class_set_key``) at class-creation time. A post-hoc
``cls.__name__ = ...`` rename never reaches ``_meta`` -- see
``src/popoto/models/base.py``'s ``ModelOptions.__init__`` (lines 189-191),
which is captured once from the ``class`` statement's name and never
revisited.

These tests assert the *invariant* every factory must uphold, parametrized
over each factory using the exact prefix expression its own real caller
supplies (critique C4) -- not a hand-picked literal that happens to be
collision-free. Any glob pattern used here is derived independently; none of
these tests import ``teardown()``'s own pattern constant (critique C1).
"""

import uuid

import pytest

from src import popoto
from src.popoto.fields.write_filter import WriteFilterMixin
from src.popoto.redis_db import get_REDIS_DB


def _real_uuid_prefix() -> str:
    """Matches the real caller expression used by association_recall.py
    and test_confidence_gate_refusal.py (``uuid.uuid4().hex[:8]``)."""
    return uuid.uuid4().hex[:8]


def _real_sanitized_prefix() -> str:
    """Matches the real caller expression used by RecipeScenario /
    ExternalScenario -- a colon-bearing UUID-derived prefix, sanitized the
    same way each factory's own body sanitizes it."""
    return f"bench:{uuid.uuid4().hex[:8]}:"


def _build_external_lexical():
    from tests.benchmarks.scenarios.external_base import _build_external_model_class

    prefix = _real_uuid_prefix()  # external_base.setup() uses uuid4().hex[:8] raw
    return _build_external_model_class(prefix, with_bm25=True, with_embedding=False)


def _build_external_hybrid():
    from tests.benchmarks.scenarios.external_base import _build_external_model_class

    prefix = _real_uuid_prefix()
    return _build_external_model_class(prefix, with_bm25=True, with_embedding=True)


def _build_external_vector():
    from tests.benchmarks.scenarios.external_base import _build_external_model_class

    prefix = _real_uuid_prefix()
    return _build_external_model_class(prefix, with_bm25=False, with_embedding=True)


def _build_external_validity():
    from tests.benchmarks.scenarios.external_base import _build_external_model_class

    prefix = _real_uuid_prefix()
    return _build_external_model_class(prefix, with_validity=True)


def _build_graph():
    from tests.benchmarks.scenarios.external_base import _build_graph_model_class

    prefix = _real_uuid_prefix()
    return _build_graph_model_class(prefix)


def _build_recipe():
    from tests.benchmarks.scenarios.recipe_base import _build_recipe_model_class

    # RecipeScenario.__init__ builds self._prefix as f"bench_{self._prefix}"
    # from Scenario.__init__'s own f"bench:{uuid4().hex[:8]}:" -- the factory
    # itself sanitizes with .replace(":", "").replace("-", "")[:8].
    prefix = _real_sanitized_prefix()
    return _build_recipe_model_class(prefix, overrides={})


def _build_association():
    from tests.benchmarks.association_recall import _build_model

    prefix = _real_uuid_prefix()  # run_trial() uses uuid4().hex[:8] raw
    return _build_model(prefix, with_cooccur=True)


def _build_refusal():
    from tests.benchmarks.test_confidence_gate_refusal import _build_refusal_model

    prefix = _real_uuid_prefix()  # the real caller uses uuid4().hex[:8] raw
    return _build_refusal_model(prefix)


FACTORIES = {
    "external_lexical": _build_external_lexical,
    "external_hybrid": _build_external_hybrid,
    "external_vector": _build_external_vector,
    "external_validity": _build_external_validity,
    "graph": _build_graph,
    "recipe": _build_recipe,
    "association": _build_association,
    "refusal": _build_refusal,
}

EXPECTED_FIELDS = {
    "external_lexical": {
        "turn_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "content_index",
    },
    "external_hybrid": {
        "turn_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "content_index",
        "embedding",
    },
    "external_vector": {
        "turn_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "embedding",
    },
    "external_validity": {
        "turn_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "content_index",
        "validity",
    },
    "graph": {
        "turn_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "content_index",
        "associations",
        "prev_turn",
    },
    "recipe": {
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
    },
    "association": {
        "mem_id",
        "agent_id",
        "content",
        "importance",
        "relevance",
        "certainty",
        "content_index",
        "associations",
        "related",
    },
    "refusal": {
        "turn_id",
        "agent_id",
        "content",
        "certainty",
        "content_index",
    },
}


def _cleanup(cls):
    """Best-effort key cleanup for a per-item class built in a test."""
    class_name = cls.__name__
    try:
        cursor = 0
        while True:
            cursor, keys = get_REDIS_DB().scan(
                cursor, match=f"*{class_name}*", count=200
            )
            if keys:
                get_REDIS_DB().delete(*keys)
            if cursor == 0:
                break
    except Exception:
        pass


@pytest.mark.parametrize("factory_name", sorted(FACTORIES))
class TestPerItemClassNamespacing:
    """Every per-item factory must own its Redis namespace from birth."""

    def test_db_class_key_matches_class_name(self, factory_name):
        cls = FACTORIES[factory_name]()
        try:
            assert cls._meta.db_class_key.redis_key == cls.__name__
        finally:
            _cleanup(cls)

    def test_name_carries_the_per_item_prefix(self, factory_name):
        # Build twice with independent prefixes; the class name must differ,
        # proving the name is not a fixed constant that ignores its prefix
        # argument (which would defeat #701's whole premise).
        cls_a = FACTORIES[factory_name]()
        cls_b = FACTORIES[factory_name]()
        try:
            assert cls_a.__name__ != cls_b.__name__
            assert cls_a._meta.db_class_key.redis_key == cls_a.__name__
            assert cls_b._meta.db_class_key.redis_key == cls_b.__name__
        finally:
            _cleanup(cls_a)
            _cleanup(cls_b)

    def test_db_class_key_has_no_colon(self, factory_name):
        cls = FACTORIES[factory_name]()
        try:
            assert ":" not in cls._meta.db_class_key.redis_key
        finally:
            _cleanup(cls)

    def test_declared_fields_match_reference_set(self, factory_name):
        """Guards against a dropped/renamed attribute during the type()
        conversion (Risk 3) -- compares against a hand-verified reference
        set, not merely "no exception was raised"."""
        cls = FACTORIES[factory_name]()
        try:
            assert set(cls._meta.fields.keys()) == EXPECTED_FIELDS[factory_name]
        finally:
            _cleanup(cls)


def test_recipe_class_keeps_write_filter_mixin():
    from tests.benchmarks.scenarios.recipe_base import _build_recipe_model_class

    cls = _build_recipe_model_class(_real_sanitized_prefix(), overrides={})
    try:
        assert WriteFilterMixin in cls.__mro__
        assert hasattr(cls, "compute_filter_score")
    finally:
        _cleanup(cls)


def test_graph_class_self_referential_relationship_survives():
    from src.popoto.fields.relationship import Relationship
    from tests.benchmarks.scenarios.external_base import _build_graph_model_class

    cls = _build_graph_model_class(_real_uuid_prefix())
    try:
        assert "prev_turn" in cls._meta.fields
        assert isinstance(cls._meta.fields["prev_turn"], Relationship)
        assert cls._meta.fields["prev_turn"].model is cls
    finally:
        _cleanup(cls)


def test_association_class_self_referential_relationship_survives():
    from src.popoto.fields.relationship import Relationship
    from tests.benchmarks.association_recall import _build_model

    cls = _build_model(_real_uuid_prefix(), with_cooccur=True)
    try:
        assert "related" in cls._meta.fields
        assert isinstance(cls._meta.fields["related"], Relationship)
        assert cls._meta.fields["related"].model is cls
    finally:
        _cleanup(cls)


@pytest.mark.parametrize("factory_name", sorted(FACTORIES))
def test_two_instances_from_same_factory_have_disjoint_keyspaces(factory_name):
    """Two classes from the same factory, built with different prefixes,
    must produce disjoint key sets once populated -- and neither key set may
    contain the pre-#701 shared base-class name. This is the direct
    behavioral proof of the fix: cross-item contamination is what #701
    reports as the defect."""
    cls_a = FACTORIES[factory_name]()
    cls_b = FACTORIES[factory_name]()
    try:
        kwargs = {"content": "hello world"}
        if "agent_id" in cls_a._meta.fields:
            kwargs["agent_id"] = "agent-a"
        instance_a = cls_a(**kwargs)
        instance_a.save()

        kwargs_b = {"content": "hello world"}
        if "agent_id" in cls_b._meta.fields:
            kwargs_b["agent_id"] = "agent-b"
        instance_b = cls_b(**kwargs_b)
        instance_b.save()

        def _scan(match):
            cursor = 0
            found: list = []
            while True:
                cursor, keys = get_REDIS_DB().scan(cursor, match=match, count=200)
                found.extend(keys)
                if cursor == 0:
                    break
            return set(found)

        keys_a = _scan(f"*{cls_a.__name__}*")
        keys_b = _scan(f"*{cls_b.__name__}*")

        assert keys_a, "expected at least one key written for instance_a"
        assert keys_b, "expected at least one key written for instance_b"
        assert keys_a.isdisjoint(keys_b)

        for k in keys_a | keys_b:
            key_str = k if isinstance(k, str) else k.decode()
            assert "ExternalBenchmarkMemory" not in key_str
    finally:
        _cleanup(cls_a)
        _cleanup(cls_b)
