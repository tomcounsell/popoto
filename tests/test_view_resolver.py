"""Tests for the belief-sheet view resolver (issue #565).

Covers the pure resolution fold (no Redis), the reader-gate hook point in
``assemble()``, and resolver integration: V0 membership, pre-truncation
back-fill, fail-closed fault injection, per-entry staleness with no second
Redis pass, and the ``assemble()``-unchanged guard.
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest
from src import popoto
from src.popoto import SupersessionProtocol
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.tag_field import TagFieldMixin
from src.popoto.fields.validity_field import ValidityField
from src.popoto.recipes import context_assembler as context_assembler_module
from src.popoto.recipes.context_assembler import ContextAssembler
from src.popoto.recipes.view_resolver import (
    BeliefSheetResolver,
    resolve_entries,
    resolve_policy,
)


# ---------------------------------------------------------------------------
# Fakes (pure-fold tests touch no Redis)
# ---------------------------------------------------------------------------


class FakeRecord:
    """Minimal claim double: only the attrs the fold reads."""

    def __init__(
        self,
        key,
        statement="",
        kind=None,
        target=None,
        agent_id="a1",
        class_id=None,
        stated=True,
        captured_at=0.0,
    ):
        self._redis_key = key
        self.statement = statement
        self.kind = kind
        self.target = target
        self.agent_id = agent_id
        self.class_id = class_id
        self.stated = stated
        self.captured_at = captured_at


def _claim(key, statement="claim", **kwargs):
    return FakeRecord(key, statement=statement, kind="assert", **kwargs)


def _annotation(key, kind, target, statement="", **kwargs):
    return FakeRecord(
        key, statement=statement, kind=kind, target=target, **kwargs
    )


class ExplodingRecord:
    """Attribute access raises: the fold must survive, never crash."""

    _redis_key = "evil:key"

    def __getattr__(self, name):
        raise RuntimeError("corrupt")


class TestResolvePolicy:
    def test_none_selects_library_defaults(self):
        policy, warnings = resolve_policy(None)
        assert warnings == []
        assert policy["prefer"] == "self-stated"
        assert policy["gate_overfetch_multiplier"] >= 1
        assert policy["max_backfill_pulls"] >= 0

    def test_unknown_keys_ignored_with_warning(self):
        policy, warnings = resolve_policy({"nope": 1, "prefer": "recent"})
        assert policy["prefer"] == "recent"
        assert any("nope" in w for w in warnings)

    def test_bad_prefer_falls_back_with_warning(self):
        policy, warnings = resolve_policy({"prefer": "vibes"})
        assert policy["prefer"] == "self-stated"
        assert any("prefer" in w for w in warnings)


class TestPureFold:
    def test_retracted_entry_never_appears(self):
        loser = _claim("k:loser", "old news")
        retract = _annotation("k:retract", "retract", "k:loser")
        sheet = resolve_entries([loser], {"k:loser": [retract]}, None)
        assert sheet.claims == []
        assert sheet.metadata["counts"]["retracted_dropped"] == 1

    def test_superseded_collapses_to_winner_with_handles(self):
        loser = _claim("k:old", "launch is the 30th")
        winner = _annotation(
            "k:new", "supersede", "k:old", "launch is the 27th", stated=True
        )
        confirm = _annotation("k:c1", "confirm", "k:new")
        chains = {"k:old": [winner], "k:new": [confirm]}
        sheet = resolve_entries([loser], chains, None)
        assert [c.key for c in sheet.claims] == ["k:new"]
        claim = sheet.claims[0]
        assert claim.content == "launch is the 27th"
        assert claim.provenance["supersedes"] == ["k:old"]
        assert claim.provenance["superseded_by"] is None
        assert claim.provenance["confirmations"] == 1
        assert sheet.metadata["counts"]["superseded_collapsed"] == 1

    def test_confirmations_count_on_plain_claim(self):
        record = _claim("k:a", "steady")
        chains = {
            "k:a": [
                _annotation("k:c1", "confirm", "k:a"),
                _annotation("k:c2", "confirm", "k:a"),
            ]
        }
        sheet = resolve_entries([record], chains, None)
        assert sheet.claims[0].provenance["confirmations"] == 2

    def test_disjunct_pairs_surface_together(self):
        a = _claim("k:a", "maybe X", class_id="q1")
        b = _claim("k:b", "maybe Y", class_id="q1")
        sheet = resolve_entries([a, b], {}, None)
        assert [c.key for c in sheet.claims] == ["k:a", "k:b"]
        assert sheet.claims[0].provenance["disjunct_with"] == ["k:b"]
        assert sheet.claims[1].provenance["disjunct_with"] == ["k:a"]
        assert sheet.metadata["counts"]["disjunct_groups"] == 1

    def test_no_class_id_is_per_record(self):
        sheet = resolve_entries([_claim("k:a"), _claim("k:b")], {}, None)
        assert all(c.provenance["disjunct_with"] == [] for c in sheet.claims)

    def test_competing_supersessions_prefer_confirmed(self):
        loser = _claim("k:old", "old")
        w1 = _annotation("k:w1", "supersede", "k:old", "first fix")
        w2 = _annotation("k:w2", "supersede", "k:old", "second fix")
        chains = {
            "k:old": [w1, w2],
            "k:w2": [_annotation("k:c", "confirm", "k:w2")],
        }
        sheet = resolve_entries([loser], chains, {"prefer": "confirmed"})
        assert [c.key for c in sheet.claims] == ["k:w2"]

    def test_competing_supersessions_tie_is_unresolved(self):
        loser = _claim("k:old", "old")
        w1 = _annotation("k:w1", "supersede", "k:old", "fix one")
        w2 = _annotation("k:w2", "supersede", "k:old", "fix two")
        sheet = resolve_entries(
            [loser], {"k:old": [w1, w2]}, {"prefer": "confirmed"}
        )
        assert sheet.claims == []
        assert sheet.metadata["counts"]["unresolved"] == 1
        assert any("unresolved contradiction" in w for w in sheet.warnings)

    def test_dangling_target_flagged_never_crashes(self):
        corrupt = _annotation("k:bad", "supersede", None, "points nowhere")
        sheet = resolve_entries([corrupt], {}, None)
        assert sheet.claims == []
        assert any("unresolved contradiction" in w for w in sheet.warnings)

    def test_corrupt_chain_member_survived(self):
        record = _claim("k:a", "steady")
        sheet = resolve_entries([record], {"k:a": [ExplodingRecord()]}, None)
        assert [c.key for c in sheet.claims] == ["k:a"]

    def test_empty_candidate_set_is_empty_sheet(self):
        sheet = resolve_entries([], {}, None)
        assert sheet.claims == [] and sheet.warnings == []
        assert sheet.serialize()

    def test_determinism_replay_byte_identical(self):
        loser = _claim("k:old", "old")
        winner = _annotation("k:new", "supersede", "k:old", "new")
        a = _claim("k:a", "x", class_id="q")
        b = _claim("k:b", "y", class_id="q")
        records = [loser, a, b]
        chains = {
            "k:old": [winner, _annotation("k:c", "confirm", "k:old")],
            "k:new": [],
        }
        first = resolve_entries(records, chains, None).serialize()
        # Chain order shuffled: the deterministic sort must erase it.
        shuffled = {
            "k:old": list(reversed(chains["k:old"])),
            "k:new": [],
        }
        second = resolve_entries(records, shuffled, None).serialize()
        assert first == second

    def test_staleness_attached_from_mapping(self):
        record = _claim("k:a", "steady")
        sheet = resolve_entries(
            [record], {}, None, staleness_by_key={"k:a": (0.9, False)}
        )
        assert sheet.claims[0].staleness == 0.9
        assert sheet.claims[0].stale is False

    def test_handles_render_score_and_source(self):
        record = _claim("k:a", "steady")
        sheet = resolve_entries(
            [record],
            {},
            None,
            handles_by_key={"k:a": {"score": 1.5, "source": "pull"}},
        )
        assert sheet.claims[0].score == 1.5
        assert sheet.claims[0].source == "pull"


# ---------------------------------------------------------------------------
# Redis integration
# ---------------------------------------------------------------------------


class SheetMemory(popoto.Model):
    """Decaying + tag + validity: the full resolver seam."""

    memory_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.StringField(default="")
    relevance = DecayingSortedField(partition_by="agent_id")
    labels = popoto.TagField(null=True)
    validity = ValidityField()


class SheetNote(popoto.Model):
    """No DecayingSortedField: the staleness-unavailable path."""

    note_id = popoto.AutoKeyField()
    agent_id = popoto.KeyField()
    content = popoto.StringField(default="")
    score = popoto.SortedField(type=float, default=0.0)


class StubJournal:
    """Canned chains; counts reads for the budget spy."""

    def __init__(self, chains):
        self.chains = chains
        self.calls = 0

    def annotations_for(self, entry):
        self.calls += 1
        key = getattr(entry, "_redis_key", None) or str(entry)
        return list(self.chains.get(key, []))


def _save(model, **kwargs):
    instance = model(**kwargs)
    instance.save()
    return instance


def _resolver(model_class=SheetMemory, max_items=5, journal=None, **weights):
    inner = ContextAssembler(
        model_class=model_class,
        score_weights=weights or {"relevance": 1.0},
        max_items=max_items,
        retrieval_mode="composite",
    )
    kwargs = {} if journal is None else {"journal": journal}
    return BeliefSheetResolver(inner, **kwargs)


@pytest.fixture
def clean_store():
    for model in (SheetMemory, SheetNote):
        try:
            model.delete_all()
        except Exception:
            pass
    yield
    for model in (SheetMemory, SheetNote):
        try:
            model.delete_all()
        except Exception:
            pass


class TestReaderValidation:
    def test_reader_none_raises(self):
        with pytest.raises(ValueError):
            _resolver().resolve({"content": "x"}, reader=None)

    def test_reader_without_agent_raises(self):
        with pytest.raises(ValueError):
            _resolver().resolve({"content": "x"}, reader={"purpose": "answer"})


class TestAssemblerGate:
    def test_bare_assemble_has_no_gate_metadata(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="hello")
        inner = ContextAssembler(
            model_class=SheetMemory, score_weights={"relevance": 1.0}
        )
        first = inner.assemble(
            {"content": "hello"}, partition_filters={"agent_id": "a1"}
        )
        second = inner.assemble(
            {"content": "hello"}, partition_filters={"agent_id": "a1"}
        )
        assert first.formatted == second.formatted
        assert "reader_gate" not in first.metadata

    def test_raising_gate_denies_and_logs(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="hello")

        def boom(record):
            raise RuntimeError("gate outage")

        inner = ContextAssembler(
            model_class=SheetMemory, score_weights={"relevance": 1.0}
        )
        result = inner.assemble(
            {"content": "hello"},
            partition_filters={"agent_id": "a1"},
            record_gate=boom,
        )
        assert result.records == []
        assert result.metadata["reader_gate"]["gate_rejected"] == 1


class TestResolverIntegration:
    def test_v0_invalidated_record_absent(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="fresh")
        old = _save(SheetMemory, agent_id="a1", content="stale")
        SupersessionProtocol.invalidate(old)
        sheet = _resolver().resolve(
            {"content": "stale"}, reader={"agent_id": "a1"}
        )
        contents = {c.content for c in sheet.claims}
        assert "stale" not in contents
        assert "fresh" in contents

    def test_gate_prefilter_backfills_to_max_items(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="keep one", labels=["keep"])
        _save(SheetMemory, agent_id="a1", content="keep two", labels=["keep"])
        # Saved last (freshest under decay) so a post-truncation filter
        # would keep them and starve the sheet: only a pre-truncation
        # gate with back-fill returns both keeps.
        _save(SheetMemory, agent_id="a1", content="drop one", labels=["drop"])
        _save(SheetMemory, agent_id="a1", content="drop two", labels=["drop"])
        _save(SheetMemory, agent_id="a1", content="untagged")
        sheet = _resolver(max_items=2).resolve(
            {"content": "keep"},
            reader={"agent_id": "a1", "tags": ["keep"]},
        )
        assert len(sheet.claims) == 2
        assert {c.content for c in sheet.claims} == {"keep one", "keep two"}
        assert sheet.metadata["counts"]["gate_rejected"] >= 3

    def test_gate_fails_closed_on_redis_error(self, clean_store, monkeypatch):
        _save(SheetMemory, agent_id="a1", content="hello", labels=["keep"])

        def outage(tags, tag_match):
            raise ConnectionError("redis down")

        monkeypatch.setattr(ContextAssembler, "_resolve_tag_keys", outage)
        sheet = _resolver().resolve(
            {"content": "hello"},
            reader={"agent_id": "a1", "tags": ["keep"]},
        )
        assert sheet.claims == []
        assert any("failed closed" in w for w in sheet.warnings)

    def test_tagless_gate_adds_zero_tag_roundtrips(
        self, clean_store, monkeypatch
    ):
        _save(SheetMemory, agent_id="a1", content="hello", labels=["keep"])

        def forbidden(*args, **kwargs):
            raise AssertionError("tag membership must not be read tagless")

        monkeypatch.setattr(TagFieldMixin, "filter_query", forbidden)
        sheet = _resolver().resolve(
            {"content": "hello"}, reader={"agent_id": "a1"}
        )
        assert len(sheet.claims) == 1

    def test_tagged_gate_is_one_batched_read(
        self, clean_store, monkeypatch
    ):
        _save(SheetMemory, agent_id="a1", content="hello", labels=["keep"])
        calls = []
        original = TagFieldMixin.filter_query

        @classmethod
        def counting(cls, model, field_name, **kwargs):
            calls.append((field_name, kwargs))
            return original(model, field_name, **kwargs)

        monkeypatch.setattr(TagFieldMixin, "filter_query", counting)
        sheet = _resolver().resolve(
            {"content": "hello"},
            reader={"agent_id": "a1", "tags": ["keep"]},
        )
        assert len(sheet.claims) == 1
        # Exactly one batched membership read per resolve(): the gate
        # batch. The inner assemble runs unscoped on the resolver path,
        # so cooperative scoping never double-reads.
        assert len(calls) == 1

    def test_staleness_costs_no_second_pass(
        self, clean_store, monkeypatch
    ):
        for i in range(5):
            _save(SheetMemory, agent_id="a1", content=f"hello {i}")
        calls = []
        original = context_assembler_module._partition_scores_for_field

        def counting(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            context_assembler_module, "_partition_scores_for_field", counting
        )
        inner = ContextAssembler(
            model_class=SheetMemory,
            score_weights={"relevance": 1.0},
            max_items=5,
            retrieval_mode="composite",
        )
        # Current cost of quality + trace on bare assemble(): the trace
        # proxy pass plus the staleness-ratio pass.
        inner.assemble(
            {"content": "hello"},
            partition_filters={"agent_id": "a1"},
            assess_quality=True,
            emit_trace=True,
        )
        bare_calls = len(calls)
        before_resolve = len(calls)
        sheet = _resolver().resolve(
            {"content": "hello"}, reader={"agent_id": "a1"}
        )
        added = len(calls) - before_resolve
        # The resolver folds the trace-handles pass plus one per-record
        # staleness pass and no per-record loop: constant in K, and no
        # more than the bare quality+trace combo already pays.
        assert added <= 2
        assert added <= bare_calls
        assert sheet.claims[0].staleness is not None

    def test_no_decaying_field_warns_staleness_unavailable(
        self, clean_store, monkeypatch
    ):
        _save(SheetNote, agent_id="a1", content="plain", score=1.0)
        calls = []
        original = context_assembler_module._partition_scores_for_field

        def counting(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)

        monkeypatch.setattr(
            context_assembler_module, "_partition_scores_for_field", counting
        )
        resolver = _resolver(model_class=SheetNote, score=1.0)
        sheet = resolver.resolve(
            {"content": "plain"}, reader={"agent_id": "a1"}
        )
        # The single pass is the trace proxy over the plain sorted field;
        # the per-record staleness path adds nothing without a decaying
        # field — and the sheet says so instead of scoring zeroes.
        assert len(calls) == 1
        assert any("staleness unavailable" in w for w in sheet.warnings)
        assert sheet.claims and sheet.claims[0].stale is None

    def test_stub_journal_collapse_end_to_end(self, clean_store):
        loser = _save(SheetMemory, agent_id="a1", content="launch is the 30th")
        winner = FakeRecord(
            "stub:winner",
            statement="launch is the 27th",
            kind="supersede",
            target=loser.db_key.redis_key,
            stated=True,
        )
        journal = StubJournal({loser.db_key.redis_key: [winner]})
        sheet = _resolver(journal=journal).resolve(
            {"content": "launch"}, reader={"agent_id": "a1"}
        )
        assert journal.calls <= 5  # one chain read per admitted candidate, max
        keys = [c.key for c in sheet.claims]
        assert "stub:winner" in keys
        claim = next(c for c in sheet.claims if c.key == "stub:winner")
        assert claim.provenance["supersedes"] == [loser.db_key.redis_key]

    def test_stub_journal_retraction_drops(self, clean_store):
        loser = _save(SheetMemory, agent_id="a1", content="withdrawn")
        retract = FakeRecord(
            "stub:retract", kind="retract", target=loser.db_key.redis_key
        )
        journal = StubJournal({loser.db_key.redis_key: [retract]})
        sheet = _resolver(journal=journal).resolve(
            {"content": "withdrawn"}, reader={"agent_id": "a1"}
        )
        assert all(c.key != loser.db_key.redis_key for c in sheet.claims)

    def test_chain_budget_bounded_by_candidates(self, clean_store):
        first = _save(SheetMemory, agent_id="a1", content="one")
        second = _save(SheetMemory, agent_id="a1", content="two")
        journal = StubJournal(
            {
                first.db_key.redis_key: [],
                second.db_key.redis_key: [],
            }
        )
        sheet = _resolver(max_items=2, journal=journal).resolve(
            {"content": "one"}, reader={"agent_id": "a1"}
        )
        assert journal.calls <= 2

    def test_real_journal_wires_per_record_fallback(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="unjournaled")
        sheet = _resolver().resolve(
            {"content": "unjournaled"}, reader={"agent_id": "a1"}
        )
        assert len(sheet.claims) == 1
        assert sheet.claims[0].provenance["confirmations"] == 0

    def test_replay_is_byte_identical_with_pinned_now(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="steady")
        resolver = _resolver()
        reader = {"agent_id": "a1"}
        first = resolver.resolve(
            {"content": "steady"}, reader=reader, now=1_700_000_000.0
        ).serialize()
        second = resolver.resolve(
            {"content": "steady"}, reader=reader, now=1_700_000_000.0
        ).serialize()
        assert first == second

    def test_backfill_cap_reports_split_shortfall(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="hello", labels=["keep"])
        sheet = _resolver().resolve(
            {"content": "hello"},
            reader={"agent_id": "a1", "tags": ["nothing-matches"]},
            policy={"max_backfill_pulls": 0},
        )
        assert sheet.claims == []
        assert sheet.metadata["counts"]["assembles"] == 1
        assert any(
            "validity_excluded=0" in w and "gate_rejected=" in w
            for w in sheet.warnings
        )

    def test_unknown_policy_key_warns_alongside_claims(self, clean_store):
        _save(SheetMemory, agent_id="a1", content="hello")
        sheet = _resolver().resolve(
            {"content": "hello"},
            reader={"agent_id": "a1"},
            policy={"bogus": True},
        )
        assert len(sheet.claims) == 1
        assert any("bogus" in w for w in sheet.warnings)
