"""Regression tests for ContextAssembler token budget honesty (issue #408).

Trimmed from the June-2026 audit PoC. Locks in:

- Default counter measures serialized record CONTENT, not the Redis key
  (a 2,000-char record counts hundreds of tokens, not ~12).
- Budget adherence on the adversarial corpus (english/code/cjk/urls/emoji,
  plus a pre-escaped-backslash fixture for the spike's double-escape check):
  with a tiktoken-based counter per the docs contract, actual tiktoken
  tokens of ``result.formatted`` stay within budget +25% for every type.
- ``metadata["token_count"]`` accuracy within 25% of actual.
- Packing semantics: skip-not-break, first-record guarantee, and
  oversized-single-record overshoot VISIBILITY.
- Failure paths: raising/invalid token_counter falls back to the stdlib
  estimator with a diagnostic warning; old-contract ``callable(record)``
  counters trigger a construction-time DeprecationWarning.
- Golden composition identity per output format: the formatter output is
  exactly wrapper framing around the per-record serialized slices captured
  at count time (byte-identical across ``_post_effects``).

Accuracy and wrapper-residual assertions gate on
``pytest.importorskip("tiktoken")``; pure behavior tests run without it.
"""

import json
import logging
import os
import sys
import warnings

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402

from src.popoto.fields.confidence_field import ConfidenceField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.field import Field  # noqa: E402
from src.popoto.fields.shortcuts import AutoKeyField, KeyField  # noqa: E402
from src.popoto.models.base import Model  # noqa: E402

# Module-private helpers are imported explicitly so the golden composition
# identity and estimator assertions pin the real implementation.
from src.popoto.recipes.context_assembler import (  # noqa: E402
    ContextAssembler,
    _compose_structured,
    _estimate_tokens,
    _serialize_record,
    format_natural,
    format_structured,
    format_xml,
)
from src.popoto.redis_db import POPOTO_REDIS_DB  # noqa: E402

# ---------------------------------------------------------------------------
# Test Models
# ---------------------------------------------------------------------------


class BudgetMemory(Model):
    """Minimal memory model for token-budget regression tests."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")


class PostEffectBudgetMemory(Model):
    """Memory model with post-effect-active fields (on_read + competitive
    suppression via ConfidenceField) for the count-time/format-time
    byte-identity test."""

    memory_id = AutoKeyField()
    agent_id = KeyField()
    content = Field(type=str)
    relevance = DecayingSortedField(partition_by="agent_id")
    confidence = ConfidenceField(initial_confidence=0.5)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
#
# The plugin's per-test flushdb runs on the installed ``popoto`` package's
# connection; test modules import via ``src.popoto`` (a distinct module
# instance with its own connection), so — like tests/test_context_assembler.py
# — this file must clean its own model keys before and after each test.


def _clean_all():
    """Remove all keys for this file's test models (test-only, uses KEYS).

    The single ``*BudgetMemory*`` glob covers BudgetMemory and
    PostEffectBudgetMemory instance keys plus every field-companion key
    ($SortedF, $DecayingSortF, $ConfidencF, $KeyF, $Class, ...), all of which
    embed the model class name.
    """
    keys = POPOTO_REDIS_DB.keys("*BudgetMemory*")
    if keys:
        POPOTO_REDIS_DB.delete(*keys)


@pytest.fixture(autouse=True)
def clean_redis():
    """Clean Redis before and after each test."""
    _clean_all()
    yield
    _clean_all()


# ---------------------------------------------------------------------------
# Corpus fixtures (trimmed issue PoC: ~2,000 chars per record)
# ---------------------------------------------------------------------------

RECORD_CHARS = 2_000
CORPUS_SIZE = 20  # records per content type (Success Criterion 2)
CORPUS_MAX_ITEMS = 10
# Budget is set per content type to ~3.5x one record's measured tiktoken
# cost: structured JSON escapes non-ASCII to \uXXXX, so a 2,000-char cjk
# or emoji record alone costs 7k-13k tokens — a fixed budget below that
# would only exercise the first-record guarantee, not budget adherence.
CORPUS_BUDGET_RECORDS = 3.5
BUDGET_TOLERANCE = 0.25  # budget +25%, and token_count within 25% of actual


def _fill(unit: str, n: int = RECORD_CHARS) -> str:
    """Repeat ``unit`` to exactly ``n`` characters."""
    return (unit * (n // len(unit) + 1))[:n]


CORPUS = {
    "english": _fill(
        "The quick brown fox jumps over the lazy dog while reciting prose "
        "about memory systems and honest token budgets. "
    ),
    "code": _fill(
        "def assemble(self, query_cues=None):\n"
        "    return [r for r in records if r.score > 0.5]  # filter\n"
    ),
    "cjk": _fill("智能体的记忆系统需要诚实的令牌预算管理，否则上下文窗口将会溢出。"),
    "urls": _fill(
        "https://popoto.io/docs/features/context-assembler"
        "?ref=4f9a#token-budget-semantics "
    ),
    "emoji": _fill("🧠💾🤖✨🔥🎯🚀📚🧩⚡"),
    # Spike-1 double-escape check: content already containing literal
    # \uXXXX sequences serializes as \\uXXXX; the unanchored escape regex
    # still matches the trailing \uXXXX, so there is no systematic
    # undercount. This fixture locks that in.
    "pre_escaped": _fill(
        "literal \\u0041 and \\u00e9 escape sequences in payload text "
    ),
}


@pytest.fixture(scope="module")
def cl100k():
    """tiktoken cl100k_base encoding — accuracy tests only."""
    tiktoken = pytest.importorskip("tiktoken")
    return tiktoken.get_encoding("cl100k_base")


def _save_corpus(content: str, agent_id: str, n: int = CORPUS_SIZE):
    records = []
    for _ in range(n):
        m = BudgetMemory(agent_id=agent_id, content=content)
        m.save()
        records.append(m)
    return records


def _assemble_corpus(content_type: str, cl100k):
    """Save a 20-record corpus and assemble with a tiktoken counter per the
    fixed docs contract: ``token_counter=lambda text: len(enc.encode(text))``.

    Returns ``(result, actual, max_tokens)`` where ``actual`` is the real
    tiktoken token count of ``result.formatted`` and ``max_tokens`` is the
    per-content-type budget (~3.5 records' worth, so the budget engages —
    fewer than max_items admitted — yet single records always fit).
    """
    agent_id = f"corpus-{content_type}"
    records = _save_corpus(CORPUS[content_type], agent_id)
    per_record = len(cl100k.encode(_serialize_record(records[0], "structured")))
    max_tokens = int(CORPUS_BUDGET_RECORDS * per_record)
    assembler = ContextAssembler(
        model_class=BudgetMemory,
        score_weights={"relevance": 1.0},
        max_items=CORPUS_MAX_ITEMS,
        max_tokens=max_tokens,
        token_counter=lambda text: len(cl100k.encode(text)),
    )
    result = assembler.assemble(
        query_cues={"topic": "memory"},
        partition_filters={"agent_id": agent_id},
    )
    actual = len(cl100k.encode(result.formatted))
    return result, actual, max_tokens


# ---------------------------------------------------------------------------
# Default counter: content scale, not key scale
# ---------------------------------------------------------------------------


class TestDefaultCounter:
    def test_default_counter_measures_content_not_key(self):
        """A record with 2,000 chars of content counts hundreds of tokens —
        not the ~12 the old key-length heuristic (len(str(r)) // 4) gave."""
        m = BudgetMemory(agent_id="a1", content=CORPUS["english"])
        m.save()
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
        )
        tokens, serialized = assembler._count_record_tokens(m)
        assert tokens > 100  # hundreds, content scale
        # Explicitly dwarf the old broken heuristic (key length // 4 ~ 12).
        old_broken_count = len(str(m)) // 4
        assert tokens > 10 * old_broken_count
        # The counted string is the serialized record, content included.
        assert CORPUS["english"][:50] in serialized

    def test_default_counter_english_accuracy(self, cl100k):
        """Success Criterion 1: default estimate within ±25% of tiktoken
        cl100k_base over the serialized record output (spike: +20.3%;
        builder re-measured +3.1% — the bar is the ±25% band)."""
        m = BudgetMemory(agent_id="a2", content=CORPUS["english"])
        m.save()
        serialized = _serialize_record(m, "structured")
        est = _estimate_tokens(serialized)
        actual = len(cl100k.encode(serialized))
        assert abs(est - actual) / actual <= BUDGET_TOLERANCE

    def test_default_counter_pre_escaped_no_undercount(self, cl100k):
        """Spike-1 double-escape check: content with literal \\uXXXX
        sequences (serialized as \\\\uXXXX) must not be systematically
        undercounted — the safe direction is overestimation."""
        m = BudgetMemory(agent_id="a3", content=CORPUS["pre_escaped"])
        m.save()
        serialized = _serialize_record(m, "structured")
        est = _estimate_tokens(serialized)
        actual = len(cl100k.encode(serialized))
        # No undercount beyond tolerance (measured: +15.5% overestimate).
        assert est >= actual * (1 - BUDGET_TOLERANCE)
        assert abs(est - actual) / actual <= BUDGET_TOLERANCE


# ---------------------------------------------------------------------------
# Budget adherence on the corpus (Success Criteria 2 and 3)
# ---------------------------------------------------------------------------


class TestBudgetAdherence:
    @pytest.mark.parametrize("content_type", sorted(CORPUS))
    def test_formatted_output_within_budget(self, cl100k, content_type):
        """Actual tiktoken tokens of result.formatted stay within budget
        +25% for EVERY content type (the budget is ~3.5 records, so no
        single record exceeds it and the first-record guarantee cannot
        inflate this corpus)."""
        result, actual, max_tokens = _assemble_corpus(content_type, cl100k)
        assert len(result.records) >= 1
        # The budget actually engaged: fewer than max_items admitted.
        assert len(result.records) < CORPUS_MAX_ITEMS
        assert actual <= max_tokens * (1 + BUDGET_TOLERANCE)

    @pytest.mark.parametrize("content_type", sorted(CORPUS))
    def test_token_count_metadata_accuracy(self, cl100k, content_type):
        """metadata["token_count"] within 25% of actual tiktoken tokens of
        result.formatted (wrapper framing is the only excluded residual)."""
        result, actual, _ = _assemble_corpus(content_type, cl100k)
        token_count = result.metadata["token_count"]
        assert isinstance(token_count, int)
        assert actual > 0
        assert abs(token_count - actual) / actual <= BUDGET_TOLERANCE


# ---------------------------------------------------------------------------
# Packing semantics: skip-not-break, first-record guarantee, overshoot
# ---------------------------------------------------------------------------


class TestPackingSemantics:
    def test_skip_not_break_admits_later_fitting_record(self):
        """One oversized record ranked between small ones: the small record
        AFTER it is still admitted, and token_count reflects only admitted
        records (greedy first-fit, not a packing terminator)."""
        small1 = BudgetMemory(agent_id="a1", content="s" * 100)
        small1.save()
        big = BudgetMemory(agent_id="a1", content="B" * 8000)
        big.save()
        small2 = BudgetMemory(agent_id="a1", content="t" * 100)
        small2.save()

        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_items=10,
            max_tokens=50,
            # Deterministic size-keyed counter: small records cost 10,
            # the oversized record costs 10,000 (never fits).
            token_counter=lambda text: 10 if len(text) < 1000 else 10_000,
        )
        # Pin candidate rank order deterministically: [small1, big, small2].
        order = [small1, big, small2]
        assembler._pull_path = lambda cues, filters: (order, order)

        result = assembler.assemble(query_cues={"topic": "x"})

        admitted = [str(r) for r in result.records]
        assert admitted == [str(small1), str(small2)]
        assert str(big) not in admitted
        assert result.metadata["token_count"] == 20  # admitted records only

    def test_first_record_guarantee_never_zero_records(self):
        """Even when record 1 exceeds the budget, it is admitted — assemble()
        never returns zero records when candidates exist."""
        m = BudgetMemory(agent_id="solo", content=CORPUS["english"] * 4)
        m.save()
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_tokens=10,  # far below the record's real cost
        )
        result = assembler.assemble(
            query_cues={"topic": "x"},
            partition_filters={"agent_id": "solo"},
        )
        assert len(result.records) == 1

    def test_oversized_single_record_overshoot_is_visible(self):
        """When the first-record guarantee admits an oversized record, the
        overshoot is VISIBLE: metadata["token_count"] is the real
        (over-budget) count, not clamped to the budget."""
        m = BudgetMemory(agent_id="solo2", content=CORPUS["english"] * 4)
        m.save()

        counted = []

        def spy_counter(text):
            tokens = _estimate_tokens(text)
            counted.append(tokens)
            return tokens

        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_tokens=10,
            token_counter=spy_counter,
        )
        counted.clear()  # drop the construction-time probe call
        result = assembler.assemble(
            query_cues={"topic": "x"},
            partition_filters={"agent_id": "solo2"},
        )
        assert len(result.records) == 1
        assert counted, "spy counter never called during assemble()"
        assert result.metadata["token_count"] == counted[0]
        assert result.metadata["token_count"] > assembler.max_tokens


# ---------------------------------------------------------------------------
# End-to-end structural guard (the assertion that would have caught #408)
# ---------------------------------------------------------------------------


class TestStructuralGuard:
    def test_token_count_reflects_content_scale_not_key_scale(self):
        """Real Model, real assemble(): token_count must scale with the
        record's CONTENT, not its Redis key (str(record) is the key)."""
        content = _fill(
            "The quick brown fox jumps over the lazy dog while reciting "
            "prose about memory systems. ",
            n=8_000,
        )
        record = BudgetMemory(agent_id="guard", content=content)
        record.save()
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
        )
        result = assembler.assemble(
            query_cues={"topic": "memory"},
            partition_filters={"agent_id": "guard"},
        )
        assert len(result.records) == 1
        assert result.metadata["token_count"] >= 10 * len(str(record))


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def _save_one(agent_id="fp"):
    m = BudgetMemory(agent_id=agent_id, content=CORPUS["english"])
    m.save()
    return m


def _assert_fallback_diagnostics(caplog, exc_name):
    """The fallback warning is diagnostic: exception type, first-80-chars
    excerpt of the serialized text, and the contract statement."""
    messages = [
        rec.getMessage() for rec in caplog.records if rec.levelno >= logging.WARNING
    ]
    fallback = [m for m in messages if "falling back to _estimate_tokens" in m]
    assert fallback, f"no fallback warning logged; got: {messages}"
    msg = fallback[0]
    assert exc_name in msg
    assert "first 80 chars" in msg
    # The excerpt shows the serialized record string (structured format
    # opens with the field envelope), proving the counter received a str.
    assert '"memory_id"' in msg
    assert "callable(str) -> int" in msg


class TestFailurePaths:
    def test_raising_counter_falls_back_with_diagnostic_warning(self, caplog):
        """Budgeted branch: a raising token_counter falls back to
        _estimate_tokens(serialized) — token_count reflects content scale —
        and emits the diagnostic logger.warning."""

        def raising_counter(text):
            raise ValueError("boom")

        m = _save_one("fp-raise")
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_tokens=100_000,
            token_counter=raising_counter,
        )
        with caplog.at_level(logging.WARNING):
            result = assembler.assemble(
                query_cues={"topic": "x"},
                partition_filters={"agent_id": "fp-raise"},
            )
        assert len(result.records) == 1
        expected = _estimate_tokens(_serialize_record(m, "structured"))
        assert result.metadata["token_count"] == expected
        assert result.metadata["token_count"] > 100  # content scale
        _assert_fallback_diagnostics(caplog, "ValueError")

    def test_raising_counter_falls_back_on_unbudgeted_branch(self, caplog):
        """max_tokens is None accounting branch: same fallback + warning."""

        def raising_counter(text):
            raise ValueError("boom")

        _save_one("fp-raise-nb")
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            token_counter=raising_counter,  # max_tokens stays None
        )
        with caplog.at_level(logging.WARNING):
            result = assembler.assemble(
                query_cues={"topic": "x"},
                partition_filters={"agent_id": "fp-raise-nb"},
            )
        assert len(result.records) == 1
        assert result.metadata["token_count"] > 100  # content scale
        _assert_fallback_diagnostics(caplog, "ValueError")

    @pytest.mark.parametrize(
        "bad_return",
        [None, -5, 3.5, True],
        ids=["none", "negative", "float", "bool"],
    )
    def test_invalid_counter_return_falls_back(self, caplog, bad_return):
        """Counters returning None / negative / float / bool are not
        exceptions — the validation guard routes them into the same
        fallback, yielding a sane non-negative int token_count."""
        _save_one("fp-invalid")
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_tokens=100_000,
            token_counter=lambda text: bad_return,
        )
        with caplog.at_level(logging.WARNING):
            result = assembler.assemble(
                query_cues={"topic": "x"},
                partition_filters={"agent_id": "fp-invalid"},
            )
        token_count = result.metadata["token_count"]
        assert isinstance(token_count, int)
        assert not isinstance(token_count, bool)
        assert token_count > 100  # fallback measured the real content
        _assert_fallback_diagnostics(caplog, "TypeError")

    def test_old_contract_counter_warns_at_construction(self):
        """An old-contract callable(record) counter (attribute access on the
        record) triggers a DeprecationWarning at construction time."""
        with pytest.warns(DeprecationWarning, match="serialized record string"):
            ContextAssembler(
                model_class=BudgetMemory,
                score_weights={"relevance": 1.0},
                token_counter=lambda r: r.importance * 10,
            )

    def test_new_contract_counter_does_not_warn(self):
        """A new-contract callable(str) counter must NOT warn."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            ContextAssembler(
                model_class=BudgetMemory,
                score_weights={"relevance": 1.0},
                token_counter=lambda text: len(text) // 4,
            )

    def test_estimate_tokens_empty_string_is_zero(self):
        assert _estimate_tokens("") == 0

    def test_serialize_record_all_none_fields(self):
        """A record with all-None data fields serializes to a small valid
        envelope with a small nonzero estimate (None values are omitted;
        only the auto-generated key remains)."""
        m = BudgetMemory()  # unsaved: agent_id=None, content=None
        serialized = _serialize_record(m, "structured")
        parsed = json.loads(serialized)
        assert isinstance(parsed, dict)
        est = _estimate_tokens(serialized)
        assert 0 < est < 100
        # xml / natural do not crash on the same record
        assert "<record>" in _serialize_record(m, "xml")
        assert isinstance(_serialize_record(m, "natural"), str)

    def test_assemble_zero_candidates_empty_result(self):
        """Zero candidates: empty result with token_count == 0."""
        assembler = ContextAssembler(
            model_class=BudgetMemory,
            score_weights={"relevance": 1.0},
            max_tokens=4_000,
        )
        result = assembler.assemble()  # no cues, no push path
        assert result.records == []
        assert result.metadata["token_count"] == 0


# ---------------------------------------------------------------------------
# Golden composition: formatter == wrapper framing around per-record slices
# ---------------------------------------------------------------------------


def _golden_records():
    records = []
    for content in ("alpha content", "beta & <content>", "gamma " * 30):
        m = BudgetMemory(agent_id="golden", content=content)
        m.save()
        records.append(m)
    return records


class TestGoldenComposition:
    def test_structured_composition_identity(self):
        records = _golden_records()
        serialized = [_serialize_record(r, "structured") for r in records]
        assert format_structured(records) == "[\n" + ",\n".join(serialized) + "\n]"
        assert format_structured([]) == "[]"

    def test_xml_composition_identity(self):
        records = _golden_records()
        serialized = [_serialize_record(r, "xml") for r in records]
        assert (
            format_xml(records)
            == "<records>\n" + "\n".join(serialized) + "\n</records>"
        )
        assert format_xml([]) == "<records>\n</records>"

    def test_natural_composition_identity(self):
        """Natural format: the "N. " enumeration prefix is positional
        composition framing applied by the composer, not part of the
        per-record slice (numbering depends on final position after
        skip-not-break selection)."""
        records = _golden_records()
        serialized = [_serialize_record(r, "natural") for r in records]
        assert format_natural(records) == "\n".join(
            f"{i}. {s}" for i, s in enumerate(serialized, 1)
        )
        assert format_natural([]) == ""

    @pytest.mark.parametrize(
        "fmt,formatter",
        [
            ("structured", format_structured),
            ("xml", format_xml),
            ("natural", format_natural),
        ],
    )
    def test_wrapper_residual_under_20_tokens(self, cl100k, fmt, formatter):
        """The wrapper framing excluded from per-record counting is a fixed
        handful of tokens per assembly: < 20 tiktoken tokens per format."""
        records = _golden_records()
        serialized = [_serialize_record(r, fmt) for r in records]
        composed_tokens = len(cl100k.encode(formatter(records)))
        per_record_tokens = sum(len(cl100k.encode(s)) for s in serialized)
        residual = composed_tokens - per_record_tokens
        assert abs(residual) < 20

    def test_count_time_strings_compose_formatted_across_post_effects(self):
        """result.formatted is composed of the EXACT strings handed to the
        token counter at count time — byte-identical even though
        _post_effects (on_read, competitive suppression on the
        ConfidenceField model) runs between counting and formatting."""
        for i in range(5):
            m = PostEffectBudgetMemory(
                agent_id="pe", content=f"post effect record {i} " * 20
            )
            m.save()

        counted_strings = []

        def spy_counter(text):
            counted_strings.append(text)
            return _estimate_tokens(text)

        assembler = ContextAssembler(
            model_class=PostEffectBudgetMemory,
            score_weights={"relevance": 1.0},
            max_items=3,  # leaves non-selected candidates for suppression
            max_tokens=1_000_000,  # everything in the slice is admitted
            token_counter=spy_counter,
        )
        counted_strings.clear()  # drop the construction-time probe call
        result = assembler.assemble(
            query_cues={"topic": "post"},
            partition_filters={"agent_id": "pe"},
        )

        assert len(result.records) >= 1
        assert len(counted_strings) == len(result.records)
        # Byte-identity: formatted output IS the composition of count-time
        # strings (never a re-serialization after post-effects).
        assert result.formatted == _compose_structured(counted_strings)
