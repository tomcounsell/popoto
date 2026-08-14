"""Tests for TagField / TagFieldMixin — optional multi-value scoping (issue #492).

Covers:
- CRUD + index maintenance (create indexes, delete cleanup, re-save diff / no orphans)
- Untagged records live in the shared pool (unscoped queries return them)
- Filter lookups: __contains (membership), __any (OR/SUNION), __all (AND/SINTER)
- Composition with other filters (KeyField / IndexedField)
- Normalization: list/set/tuple accepted, dedup, deterministic order, invalid raises
- Valkey-safety: only plain Redis Set index keys are created
- ContextAssembler integration: tag scoping across modes, kill switch, no-TagField
- RLT micro-benchmark: bounded tag-filtered assembly overhead
"""

import time

import pytest

from popoto import (
    AutoKeyField,
    Field,
    IndexedField,
    KeyField,
    Model,
    ModelException,
    TagField,
)
from popoto.fields.constants import Defaults
from popoto.fields.decaying_sorted_field import DecayingSortedField
from popoto.fields.tag_field import TagFieldMixin
from popoto.recipes.context_assembler import ContextAssembler
from popoto.redis_db import POPOTO_REDIS_DB

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TaggedMemory(Model):
    key = AutoKeyField()
    content = Field(type=str, null=True)
    tags = TagField()


class ScopedMemory(Model):
    """TagField composed with a KeyField partition and an IndexedField."""

    key = AutoKeyField()
    project = KeyField()
    status = IndexedField(type=str, null=True)
    tags = TagField()


class AssemblerMemory(Model):
    """DecayingSortedField (composite mode) + TagField for assembler scoping."""

    key = AutoKeyField()
    content = Field(type=str)
    relevance = DecayingSortedField()
    tags = TagField()


# ---------------------------------------------------------------------------
# CRUD + index maintenance
# ---------------------------------------------------------------------------


class TestTagFieldCRUD:
    def setup_method(self):
        TaggedMemory.delete_all()

    def teardown_method(self):
        TaggedMemory.delete_all()

    def test_save_creates_per_tag_index(self):
        m = TaggedMemory.create(tags=["agent:valor", "project:popoto"])
        assert len(TaggedMemory.query.filter(tags__contains="agent:valor")) == 1
        assert (
            TaggedMemory.query.filter(tags__contains="project:popoto")[0].key == m.key
        )

    def test_untagged_lives_in_shared_pool(self):
        tagged = TaggedMemory.create(tags=["agent:valor"])
        untagged = TaggedMemory.create()
        # Unscoped query returns BOTH (shared pool includes untagged).
        keys = {r.key for r in TaggedMemory.query.all()}
        assert tagged.key in keys and untagged.key in keys
        # A tag filter returns only the tagged record.
        scoped = TaggedMemory.query.filter(tags__contains="agent:valor")
        assert len(scoped) == 1 and scoped[0].key == tagged.key

    def test_untagged_variants_all_empty(self):
        for value in (None, [], set(), tuple()):
            TaggedMemory.delete_all()
            m = TaggedMemory.create(tags=value)
            reloaded = TaggedMemory.query.get(key=m.key)
            assert reloaded.tags == []

    def test_delete_removes_from_all_tag_sets(self):
        m = TaggedMemory.create(tags=["a", "b", "c"])
        assert len(TaggedMemory.query.filter(tags__contains="b")) == 1
        # Pointer side key must exist BEFORE delete(), otherwise the
        # post-delete "doesn't exist" assertion below is vacuously true —
        # it would pass whether or not delete() actually cleaned anything up.
        ptr = TagFieldMixin._tag_pointer_side_key(m.db_key.redis_key, "tags")
        assert POPOTO_REDIS_DB.exists(ptr) == 1, "pointer side key was never created"
        m.delete()
        for tag in ("a", "b", "c"):
            assert len(TaggedMemory.query.filter(tags__contains=tag)) == 0
        # Pointer side key is cleaned up.
        assert POPOTO_REDIS_DB.exists(ptr) == 0

    def test_resave_diff_no_orphans(self):
        m = TaggedMemory.create(tags=["a", "b"])
        m.tags = ["b", "c"]
        m.save()
        # 'a' dropped, 'c' added, 'b' unchanged — no orphaned members.
        assert len(TaggedMemory.query.filter(tags__contains="a")) == 0
        assert len(TaggedMemory.query.filter(tags__contains="b")) == 1
        assert len(TaggedMemory.query.filter(tags__contains="c")) == 1

    def test_resave_to_untagged_clears_membership(self):
        m = TaggedMemory.create(tags=["a", "b"])
        m.tags = []
        m.save()
        assert len(TaggedMemory.query.filter(tags__contains="a")) == 0
        assert len(TaggedMemory.query.filter(tags__contains="b")) == 0
        # Still present in the shared pool.
        assert TaggedMemory.query.get(key=m.key).tags == []

    def test_multiple_records_share_a_tag(self):
        TaggedMemory.create(tags=["shared"])
        TaggedMemory.create(tags=["shared"])
        TaggedMemory.create(tags=["other"])
        assert len(TaggedMemory.query.filter(tags__contains="shared")) == 2

    def test_idempotent_resave(self):
        m = TaggedMemory.create(tags=["a", "b"])
        m.save()
        m.save()
        assert len(TaggedMemory.query.filter(tags__contains="a")) == 1
        assert len(TaggedMemory.query.filter(tags__all=["a", "b"])) == 1


# ---------------------------------------------------------------------------
# Filter semantics
# ---------------------------------------------------------------------------


class TestTagFieldFilter:
    def setup_method(self):
        TaggedMemory.delete_all()
        self.m_ab = TaggedMemory.create(tags=["agent:a", "project:x"])
        self.m_b = TaggedMemory.create(tags=["agent:b", "project:x"])
        self.m_c = TaggedMemory.create(tags=["agent:c"])
        self.m_none = TaggedMemory.create()

    def teardown_method(self):
        TaggedMemory.delete_all()

    def test_contains_membership(self):
        assert len(TaggedMemory.query.filter(tags__contains="project:x")) == 2
        assert len(TaggedMemory.query.filter(tags__contains="agent:c")) == 1
        assert len(TaggedMemory.query.filter(tags__contains="missing")) == 0

    def test_any_is_or(self):
        results = TaggedMemory.query.filter(tags__any=["agent:a", "agent:c"])
        assert {r.key for r in results} == {self.m_ab.key, self.m_c.key}

    def test_all_is_and(self):
        results = TaggedMemory.query.filter(tags__all=["agent:a", "project:x"])
        assert {r.key for r in results} == {self.m_ab.key}
        # No record carries both agent:a AND agent:b.
        assert len(TaggedMemory.query.filter(tags__all=["agent:a", "agent:b"])) == 0

    def test_empty_any_all_lists_are_safe(self):
        assert len(TaggedMemory.query.filter(tags__any=[])) == 0
        assert len(TaggedMemory.query.filter(tags__all=[])) == 0

    def test_absent_tag_param_is_unscoped(self):
        # No tag lookup at all — shared pool, including the untagged record.
        assert len(TaggedMemory.query.all()) == 4


class TestTagFieldComposition:
    def setup_method(self):
        ScopedMemory.delete_all()
        ScopedMemory.create(project="popoto", status="active", tags=["agent:valor"])
        ScopedMemory.create(project="popoto", status="stale", tags=["agent:valor"])
        ScopedMemory.create(project="other", status="active", tags=["agent:valor"])

    def teardown_method(self):
        ScopedMemory.delete_all()

    def test_tag_composes_with_keyfield_partition(self):
        results = ScopedMemory.query.filter(
            project="popoto", tags__contains="agent:valor"
        )
        assert len(results) == 2

    def test_tag_composes_with_indexed_and_key(self):
        results = ScopedMemory.query.filter(
            project="popoto", status="active", tags__contains="agent:valor"
        )
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Normalization + validation
# ---------------------------------------------------------------------------


class TestTagFieldNormalization:
    def setup_method(self):
        TaggedMemory.delete_all()

    def teardown_method(self):
        TaggedMemory.delete_all()

    def test_set_and_tuple_accepted(self):
        m_set = TaggedMemory.create(tags={"b", "a"})
        m_tuple = TaggedMemory.create(tags=("c", "d"))
        assert TaggedMemory.query.get(key=m_set.key).tags == ["a", "b"]
        assert TaggedMemory.query.get(key=m_tuple.key).tags == ["c", "d"]

    def test_dedup_and_sorted(self):
        m = TaggedMemory.create(tags=["b", "a", "b", "a"])
        assert TaggedMemory.query.get(key=m.key).tags == ["a", "b"]
        assert len(TaggedMemory.query.filter(tags__contains="a")) == 1

    def test_scalar_coercion_to_str(self):
        m = TaggedMemory.create(tags=[1, 2, 2])
        assert TaggedMemory.query.get(key=m.key).tags == ["1", "2"]

    def test_invalid_non_iterable_raises(self):
        # A non-iterable scalar cannot be coerced to a tag collection.
        with pytest.raises(ModelException):
            TaggedMemory.create(tags=123)

    def test_invalid_element_raises(self):
        with pytest.raises(ModelException):
            TaggedMemory.create(tags=["ok", {"nested": "dict"}])

    def test_bare_exact_match_filter_raises(self):
        # Bare filter(tags=[...]) is ambiguous on a multi-value field and must
        # raise rather than silently degrade to client-side list equality.
        from popoto.exceptions import QueryException

        TaggedMemory.create(tags=["a"])
        with pytest.raises(QueryException):
            list(TaggedMemory.query.filter(tags=["a"]))


# ---------------------------------------------------------------------------
# Valkey-safety
# ---------------------------------------------------------------------------


class TestTagFieldValkeySafety:
    def setup_method(self):
        TaggedMemory.delete_all()

    def teardown_method(self):
        TaggedMemory.delete_all()

    def test_index_key_is_a_plain_redis_set(self):
        TaggedMemory.create(tags=["agent:valor"])
        # Colon in the tag value is escaped by DB_key.clean.
        idx_key = "$TagF:TaggedMemory:tags:agent{&#58;}valor"
        assert POPOTO_REDIS_DB.type(idx_key) == b"set"
        assert POPOTO_REDIS_DB.scard(idx_key) == 1

    def test_pointer_side_key_is_a_set(self):
        m = TaggedMemory.create(tags=["a", "b"])
        ptr = TagFieldMixin._tag_pointer_side_key(m.db_key.redis_key, "tags")
        assert POPOTO_REDIS_DB.type(ptr) == b"set"
        assert POPOTO_REDIS_DB.scard(ptr) == 2


# ---------------------------------------------------------------------------
# ContextAssembler integration
# ---------------------------------------------------------------------------


class TestTagFieldAssembler:
    def setup_method(self):
        AssemblerMemory.delete_all()
        # Two agents' memories, same content cue.
        self.valor = AssemblerMemory.create(
            content="deploy runbook", relevance=1.0, tags=["agent:valor"]
        )
        self.other = AssemblerMemory.create(
            content="deploy runbook", relevance=1.0, tags=["agent:other"]
        )
        self.shared = AssemblerMemory.create(
            content="deploy runbook", relevance=1.0
        )  # untagged
        self.assembler = ContextAssembler(
            AssemblerMemory, score_weights={"relevance": 1.0}, max_items=10
        )

    def teardown_method(self):
        AssemblerMemory.delete_all()

    def _keys(self, result):
        return {getattr(r, "key", None) for r in result.records}

    def test_auto_detects_tag_field(self):
        assert self.assembler._tag_field_name == "tags"

    def test_tags_scope_retrieval(self):
        result = self.assembler.assemble(
            query_cues={"content": "deploy"}, tags=["agent:valor"]
        )
        keys = self._keys(result)
        assert self.valor.key in keys
        assert self.other.key not in keys

    def test_none_tags_is_unscoped_and_identical(self):
        a = self.assembler.assemble(query_cues={"content": "deploy"})
        b = self.assembler.assemble(query_cues={"content": "deploy"}, tags=None)
        assert self._keys(a) == self._keys(b)
        # Unscoped sees every agent + the untagged record.
        assert len(self._keys(a)) == 3

    def test_tag_match_any(self):
        result = self.assembler.assemble(
            query_cues={"content": "deploy"},
            tags=["agent:valor", "agent:other"],
            tag_match="any",
        )
        keys = self._keys(result)
        assert self.valor.key in keys and self.other.key in keys
        assert self.shared.key not in keys

    def test_default_tag_match_is_any(self):
        # Default (no tag_match) is OR/"any": multiple tags surface a memory
        # in EITHER scope (maintainer decision 2026-08-05, #492).
        result = self.assembler.assemble(
            query_cues={"content": "deploy"},
            tags=["agent:valor", "agent:other"],
        )
        keys = self._keys(result)
        assert self.valor.key in keys and self.other.key in keys
        assert self.shared.key not in keys

    def test_tag_match_all_intersects(self):
        # Explicit "all" is AND: two disjoint single-agent tags → no memory
        # carries both, so nothing scoped in.
        result = self.assembler.assemble(
            query_cues={"content": "deploy"},
            tags=["agent:valor", "agent:other"],
            tag_match="all",
        )
        assert self._keys(result) == set()

    def test_kill_switch_disables_scoping(self):
        original = Defaults.TAG_SCOPING_ENABLED
        Defaults.TAG_SCOPING_ENABLED = False
        try:
            result = self.assembler.assemble(
                query_cues={"content": "deploy"}, tags=["agent:valor"]
            )
            # Scoping ignored — all three returned.
            assert len(self._keys(result)) == 3
        finally:
            Defaults.TAG_SCOPING_ENABLED = original

    def test_tags_ignored_when_model_has_no_tagfield(self):
        class NoTagMemory(Model):
            key = AutoKeyField()
            content = Field(type=str)
            relevance = DecayingSortedField()

        NoTagMemory.delete_all()
        try:
            NoTagMemory.create(content="deploy runbook", relevance=1.0)
            asm = ContextAssembler(
                NoTagMemory, score_weights={"relevance": 1.0}, max_items=10
            )
            assert asm._tag_field_name is None
            # Passing tags must not raise and must not change results.
            with_tags = asm.assemble(
                query_cues={"content": "deploy"}, tags=["agent:valor"]
            )
            without = asm.assemble(query_cues={"content": "deploy"})
            assert len(with_tags.records) == len(without.records) == 1
        finally:
            NoTagMemory.delete_all()


# ---------------------------------------------------------------------------
# RLT micro-benchmark — bounded tag-filtered assembly overhead
# ---------------------------------------------------------------------------


class TestTagFieldBenchmark:
    def setup_method(self):
        AssemblerMemory.delete_all()
        for i in range(200):
            AssemblerMemory.create(
                content=f"deploy runbook {i}",
                relevance=1.0,
                tags=["agent:valor"] if i % 2 == 0 else ["agent:other"],
            )
        self.assembler = ContextAssembler(
            AssemblerMemory, score_weights={"relevance": 1.0}, max_items=10
        )

    def teardown_method(self):
        AssemblerMemory.delete_all()

    def test_tag_filtered_overhead_is_bounded(self):
        def _timed(**kw):
            t0 = time.perf_counter()
            self.assembler.assemble(query_cues={"content": "deploy"}, **kw)
            return time.perf_counter() - t0

        # Warm up (script cache, connection).
        _timed()
        base = min(_timed() for _ in range(5))
        scoped = min(_timed(tags=["agent:valor"]) for _ in range(5))

        # Tag pre-filter adds one SMEMBERS/SUNION/SINTER to the candidate stage.
        # Assert it is bounded — never more than ~3x the unfiltered assemble and
        # under a generous absolute ceiling. This is the reported RLT overhead.
        assert scoped < max(base * 3.0, base + 0.05)
        print(
            f"\n[RLT tag overhead] base={base*1000:.2f}ms "
            f"scoped={scoped*1000:.2f}ms ratio={scoped/base:.2f}x"
        )
