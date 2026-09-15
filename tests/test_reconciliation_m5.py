"""M5 reconciliation — equivalence classes over the provenance journal (#564).

Runs on the pytest plugin's isolated database (DB 15 by default, overridden per
worktree lane with ``POPOTO_TEST_DB``). The plugin's autouse flush runs before
every test, so this file owns no teardown beyond restoring ``Defaults``.

Tests cover the plan's verification table plus the behaviors the concern round
asked to be structural rather than aspirational:

- ``append_only``: reconciliation never mutates a persisted ``JournalEntry``,
  asserted by comparing the record's raw hash across a reconcile rather than by
  reading the implementation
- ``replay_rebuilds_index``: the merge log is the source of truth — the two
  index tables are deleted and recomputed from the annotations alone, byte for
  byte, and retracting a ``merge`` annotation removes the assignment it made
- ``no_m6_surfacing``: the module imports no surfacing path and defines no
  surfacing entry point; M5 groups and resolves, M6 decides what to show
- ``command_allowlist``: neither ``HSETNX`` nor ``SET ... NX`` appears in the
  module source, and no such command reaches the client during a reconcile —
  both withdrawn mitigations, replaced by the single-writer invariant
- ``membership_row_holds_no_claim_content``: no claim text reaches either
  reconciliation model's keyspace, which is what keeps ``hard_delete``'s
  documented scope sufficient
- ``hard_delete_cascades``: all four legs of :func:`erase_entry`
- ``single_writer_invariant``: a sibling committed earlier in the same pass is
  found by exact ``claim_slot`` equality, before any embedding work
- the deterministic tier firing with **zero** judge calls on a singleton-slot
  type, and the judge-call bound of 2x the shortlist cap
- abstention on a malformed reply, and a split symmetry probe becoming an
  explicit disjunction rather than a silent non-merge
- ``claim_type`` defaulting to ``None`` on capture and normalizing to ``note``
"""

import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest

from src.popoto.fields.validity_field import ValidityMemberAbsentError
from src.popoto.recipes.provenance_journal import (
    JournalEntry,
    ProvenanceJournal,
)
from src.popoto.recipes.reconciliation import (
    CLAIM_TYPES,
    CLAIM_TYPE_FAMILY,
    CONVENTION_BOOK_VERSION,
    DEFAULT_CLAIM_TYPE,
    PRECEDENCE_TIE,
    ClaimClass,
    ClaimMembership,
    claim_slot,
    erase_entry,
    judge_sameness,
    merge_log_entries,
    normalize_claim_type,
    reconcile_entry,
    replay,
    representative_for,
    resolve_precedence,
    shortlist_candidates,
)
from src.popoto.privacy.never_record import scan_never_record
from src.popoto.redis_db import get_REDIS_DB, scan_keys

#: The module object the imported functions actually close over.
#:
#: Resolved through ``__module__`` rather than written as a second import,
#: because ``import popoto`` and ``import src.popoto`` collapse onto one
#: canonical module: a ``from src.popoto.recipes import reconciliation``
#: alongside the ``from ...reconciliation import name`` above can hand back a
#: *different* module object, and monkeypatching the wrong one patches nothing
#: while the test still reads as green-adjacent (the patched name is simply
#: never called).
recon_module = sys.modules[reconcile_entry.__module__]
journal_module = sys.modules[JournalEntry.__module__]

AGENT = "agent-m5"

#: A uuid4 hex holding a Luhn-passing 13-digit run, from a random sample. Used
#: to make the ~0.66%-per-write firewall false positive deterministic; the
#: sibling constant in ``tests/test_provenance_journal.py`` pins the same
#: trigger at the journal layer.
LUHN_TRIPPING_HEX = "3ff8f2567646418588de71a311ae237a"

RECON_SOURCE_PATH = os.path.join(
    os.path.dirname(SCRIPT_DIR),
    "src",
    "popoto",
    "recipes",
    "reconciliation.py",
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Block:
    """One ``content`` block of an Anthropic-shaped reply."""

    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Reply:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, owner):
        self._owner = owner

    def create(self, **kwargs):
        payload = json.loads(kwargs["messages"][0]["content"])
        pair = (payload["claim_a"], payload["claim_b"])
        self._owner.calls.append(pair)
        answer = self._owner.answer(*pair)
        if answer is None:
            return _Reply("not json at all")
        return _Reply(json.dumps({"verdict": answer}))


class ScriptedJudge:
    """A judge client whose answer is a function of the ordered pair.

    Taking the *ordered* pair is what lets a test drive the symmetry probe:
    the forward ask and the swapped-order re-ask are distinguishable here in
    exactly the way they are to the real provider.
    """

    def __init__(self, answer):
        self._answer = answer
        self.calls = []
        self.messages = _Messages(self)

    def answer(self, claim_a, claim_b):
        return self._answer(claim_a, claim_b)


def always(verdict):
    return lambda a, b: verdict


class FakeProvider:
    """Embedding provider returning a vector keyed off the statement's words.

    Deliberately crude: similarity is word overlap, which is enough to order a
    shortlist deterministically without pulling a real model into the suite.
    """

    dimensions = 26

    def __init__(self):
        self.calls = 0

    def embed(self, texts, input_type=None):
        self.calls += 1
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for char in text.lower():
                index = ord(char) - 97
                if 0 <= index < self.dimensions:
                    vector[index] += 1.0
            vectors.append(vector)
        return vectors


class CommandSpy:
    """Records every command name the client is asked to execute."""

    def __init__(self, client):
        self._client = client
        self.commands = []
        self._original = client.execute_command

    def __enter__(self):
        def spy(*args, **kwargs):
            if args:
                self.commands.append(str(args[0]).upper())
            return self._original(*args, **kwargs)

        self._client.execute_command = spy
        return self

    def __exit__(self, *exc):
        self._client.execute_command = self._original
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def capture(statement, *, claim_type=None, subject="dana", at=None, stated=True):
    """Append one ``assert`` capture and return the entry."""
    return ProvenanceJournal.append(
        agent_id=AGENT,
        statement=statement,
        subjects=[subject] if subject else None,
        claim_type=claim_type,
        stated=stated,
        captured_at=at,
    ).entry


def index_snapshot():
    """Return the full index as a comparable structure."""
    memberships = {
        row.entry_redis_key: (row.class_id, row.claim_slot, row.disjunction_id)
        for row in ClaimMembership.query.filter(claim_slot__isnull=False)
    }
    return memberships


# ---------------------------------------------------------------------------
# Type vocabulary
# ---------------------------------------------------------------------------


def test_the_type_enum_is_frozen_and_every_type_has_a_family():
    """A type with no family would reach precedence with no ordering."""
    assert len(CLAIM_TYPES) == 7
    assert set(CLAIM_TYPE_FAMILY) == set(CLAIM_TYPES)
    assert CLAIM_TYPE_FAMILY["note"] == "rule_free"
    assert CLAIM_TYPE_FAMILY["deadline"] == "supersession"
    for stable in ("preference", "trait", "relationship", "goal", "procedure"):
        assert CLAIM_TYPE_FAMILY[stable] == "stable"


def test_unlabelled_and_unknown_types_normalize_to_the_catch_all():
    """``claim_type`` is write-once at capture, so pre-#564 entries are None."""
    assert normalize_claim_type(None) == DEFAULT_CLAIM_TYPE
    assert normalize_claim_type("") == DEFAULT_CLAIM_TYPE
    assert normalize_claim_type("not-a-type") == DEFAULT_CLAIM_TYPE
    assert normalize_claim_type("deadline") == "deadline"


def test_claim_type_defaults_null_and_round_trips():
    """Capture may omit the type; the field must accept and return ``None``."""
    plain = capture("dana likes tea")
    assert plain.claim_type is None
    stored = JournalEntry.query.get(redis_key=plain.pk)
    assert stored.claim_type is None

    typed = capture("dana prefers mornings", claim_type="preference")
    assert JournalEntry.query.get(redis_key=typed.pk).claim_type == "preference"


def test_claim_slot_is_a_one_way_digest():
    slot = claim_slot(AGENT, "dana", "preference")
    assert len(slot) == 32
    assert int(slot, 16) >= 0
    assert "dana" not in slot
    assert slot == claim_slot(AGENT, "dana", "preference")
    assert slot != claim_slot(AGENT, "dana", "deadline")
    assert slot != claim_slot("other", "dana", "preference")
    # An unrecognized type collapses onto the catch-all rather than minting a
    # slot no precedence row covers.
    assert claim_slot(AGENT, "dana", "bogus") == claim_slot(AGENT, "dana", "note")


# ---------------------------------------------------------------------------
# Precedence table
# ---------------------------------------------------------------------------


def test_rule_zero_beats_the_per_type_order():
    """A self-stated claim beats an inferred one, for every type."""
    inferred = capture("deadline friday", claim_type="deadline", at=200.0, stated=False)
    stated = capture("deadline monday", claim_type="deadline", at=100.0)
    winner, loser, basis = resolve_precedence(stated, inferred, "deadline")
    assert basis == "rule0_stated"
    assert winner.pk == stated.pk
    assert loser.pk == inferred.pk


def test_the_supersession_family_orders_by_recency():
    older = capture("deadline monday", claim_type="deadline", at=100.0)
    newer = capture("deadline friday", claim_type="deadline", at=200.0)
    winner, _loser, basis = resolve_precedence(newer, older, "deadline")
    assert basis == "recency"
    assert winner.pk == newer.pk


def test_the_stable_family_puts_confirmations_before_recency():
    """A claim corroborated many times must not lose to a single fresh mention."""
    confirmed = capture("dana prefers mornings", claim_type="preference", at=100.0)
    ProvenanceJournal.confirm(confirmed, agent_id=AGENT)
    ProvenanceJournal.confirm(confirmed, agent_id=AGENT)
    fresh = capture("dana prefers evenings", claim_type="preference", at=500.0)

    winner, loser, basis = resolve_precedence(fresh, confirmed, "preference")
    assert basis == "confirmations"
    assert winner.pk == confirmed.pk
    assert loser.pk == fresh.pk


def test_the_table_is_total_so_an_all_column_tie_is_a_tie_not_a_coin_flip():
    left = capture("deadline monday", claim_type="deadline", at=100.0)
    right = capture("deadline friday", claim_type="deadline", at=100.0)
    winner, loser, basis = resolve_precedence(left, right, "deadline")
    assert basis == PRECEDENCE_TIE
    assert winner is None and loser is None


def test_a_note_fires_no_rule():
    left = capture("random aside", claim_type="note", at=100.0)
    right = capture("another aside", claim_type="note", at=500.0)
    assert resolve_precedence(left, right, "note") == (None, None, PRECEDENCE_TIE)


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------


def test_a_blank_statement_costs_zero_judge_calls():
    judge = ScriptedJudge(always("same"))
    result = judge_sameness("", "dana prefers mornings", client=judge)
    assert result.abstained
    assert result.reason == "empty_statement"
    assert judge.calls == []


def test_a_malformed_reply_abstains_rather_than_guessing():
    judge = ScriptedJudge(lambda a, b: None)
    result = judge_sameness("a claim", "another claim", client=judge)
    assert result.abstained
    assert result.verdict is None
    assert result.reason == "llm_unavailable"
    assert len(judge.calls) == 1


def test_an_out_of_vocabulary_verdict_is_malformed():
    judge = ScriptedJudge(always("maybe"))
    assert judge_sameness("a claim", "b claim", client=judge).abstained


def test_a_raising_client_abstains():
    class Boom:
        class messages:
            @staticmethod
            def create(**kwargs):
                raise RuntimeError("unreachable")

    result = judge_sameness("a claim", "b claim", client=Boom())
    assert result.abstained and result.reason == "llm_unavailable"


def test_the_firewall_runs_before_the_call_and_no_text_is_transmitted():
    judge = ScriptedJudge(always("same"))
    secret = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    result = judge_sameness(secret, "dana prefers mornings", client=judge)
    assert result.abstained
    assert result.reason == "firewall_drop"
    assert judge.calls == []


def test_the_convention_book_version_is_pinned():
    """Replay pins the wording that produced a merge; a reword is a bump."""
    assert CONVENTION_BOOK_VERSION == "v1"


# ---------------------------------------------------------------------------
# The deterministic tier
# ---------------------------------------------------------------------------


def test_singleton_rule_zero_llm_calls():
    """A same-slot collision on a singleton-slot type is a conflict by
    definition, so the judge is never consulted."""
    judge = ScriptedJudge(always("same"))
    first = capture("deadline monday", claim_type="deadline", at=100.0)
    second = capture("deadline friday", claim_type="deadline", at=200.0)

    assert reconcile_entry(first, client=judge).action == "created"
    outcome = reconcile_entry(second, client=judge)

    assert outcome.action == "superseded"
    assert outcome.judge_calls == 0
    assert judge.calls == []


def test_deadline_supersession_closes_the_loser_through_the_journal():
    judge = ScriptedJudge(always("different"))
    older = capture("deadline monday", claim_type="deadline", at=100.0)
    newer = capture("deadline friday", claim_type="deadline", at=200.0)
    reconcile_entry(older, client=judge)
    outcome = reconcile_entry(newer, client=judge)

    assert outcome.action == "superseded"
    assert outcome.superseded_key == older.pk

    live = {e.pk for e in JournalEntry.query.filter(validity__current=True)}
    assert older.pk not in live
    assert newer.pk in live

    # Exactly one supersession mechanism: the journal's own, which appends a
    # ``supersede`` annotation and closes the interval in one MULTI/EXEC.
    kinds = {e.kind for e in ProvenanceJournal.annotations_for(older)}
    assert "supersede" in kinds


def test_a_precedence_tie_inside_a_slot_becomes_an_explicit_disjunction():
    judge = ScriptedJudge(always("same"))
    left = capture("deadline monday", claim_type="deadline", at=100.0)
    right = capture("deadline friday", claim_type="deadline", at=100.0)
    reconcile_entry(left, client=judge)
    outcome = reconcile_entry(right, client=judge)

    assert outcome.action == "disjoined"
    assert outcome.disjunction_id
    rows = {
        row.entry_redis_key: row
        for row in ClaimMembership.query.filter(class_id=outcome.class_id)
    }
    assert rows[left.pk].disjunction_id == outcome.disjunction_id
    assert rows[right.pk].disjunction_id == outcome.disjunction_id

    # No silent winner: both are still live.
    live = {e.pk for e in JournalEntry.query.filter(validity__current=True)}
    assert {left.pk, right.pk} <= live


# ---------------------------------------------------------------------------
# The judge path
# ---------------------------------------------------------------------------


def test_restatement_confirms_rather_than_superseding():
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture(
        "dana likes meetings early in the day", claim_type="preference", at=200.0
    )
    created = reconcile_entry(first, client=judge)
    joined = reconcile_entry(second, client=judge)

    assert joined.action == "joined"
    assert joined.class_id == created.class_id
    # Corroboration is an appended annotation, never a field bump.
    assert any(e.kind == "confirm" for e in ProvenanceJournal.annotations_for(first))
    # A restatement closes nobody's interval.
    live = {e.pk for e in JournalEntry.query.filter(validity__current=True)}
    assert {first.pk, second.pk} <= live


def test_a_clean_different_in_the_same_slot_fires_the_stable_type_rule():
    judge = ScriptedJudge(always("different"))
    incumbent = capture("dana prefers mornings", claim_type="preference", at=100.0)
    challenger = capture("dana prefers evenings", claim_type="preference", at=500.0)
    reconcile_entry(incumbent, client=judge)
    outcome = reconcile_entry(challenger, client=judge)

    assert outcome.action == "superseded"
    assert outcome.superseded_key == incumbent.pk


def test_split_verdict_disjoins():
    """A forward "same" the swapped-order probe contradicts is detected
    non-transitivity, and becomes explicit uncertainty rather than a join."""
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana is a morning person", claim_type="preference", at=200.0)

    def split(claim_a, claim_b):
        # "same" only when asked in the forward direction.
        return "same" if claim_a == second.statement else "different"

    judge = ScriptedJudge(split)
    reconcile_entry(first, client=ScriptedJudge(always("different")))
    outcome = reconcile_entry(second, client=judge)

    assert outcome.action == "disjoined"
    assert outcome.disjunction_id
    assert len(judge.calls) == 2
    # The join did not commit: the two remain in different classes.
    rows = {row.entry_redis_key: row for row in ClaimMembership.query.all()}
    assert rows[first.pk].class_id != rows[second.pk].class_id


def test_an_abstention_costs_one_extra_class_and_never_a_guessed_join():
    judge = ScriptedJudge(lambda a, b: None)
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana prefers tea", claim_type="preference", at=200.0)
    a = reconcile_entry(first, client=judge)
    b = reconcile_entry(second, client=judge)

    assert a.action == "created" and b.action == "created"
    assert a.class_id != b.class_id


def test_reconcile_is_idempotent_on_a_rerun():
    judge = ScriptedJudge(always("different"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    first = reconcile_entry(entry, client=judge)
    again = reconcile_entry(entry, client=judge)
    assert again.action == "noop"
    assert again.class_id == first.class_id
    assert again.judge_calls == 0


def test_the_judge_call_bound_is_twice_the_shortlist_cap():
    judge = ScriptedJudge(always("different"))
    for index in range(recon_module.M5_SHORTLIST_CAP + 4):
        entry = capture(
            f"dana claim number {index}",
            claim_type="preference",
            subject=f"subject-{index}",
            at=100.0 + index,
        )
        reconcile_entry(entry, client=judge)

    final = capture("dana has one more claim", claim_type="preference", at=900.0)
    outcome = reconcile_entry(final, client=judge)
    assert outcome.judge_calls <= 2 * recon_module.M5_SHORTLIST_CAP


# ---------------------------------------------------------------------------
# Shortlist
# ---------------------------------------------------------------------------


def test_single_writer_invariant_sibling_found_by_claim_slot_first(monkeypatch):
    """The exact slot-equality lookup happens *before* the embedding shortlist.

    Asserted by the arguments the shortlist receives: the slot class is already
    excluded by the time the shortlist is consulted, which is only possible if
    the slot lookup ran first. That ordering is what makes the single-writer
    invariant sufficient against the concurrent-join race -- a sibling
    committed earlier in the same sequential pass is found by index, not by
    similarity.
    """
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    created = reconcile_entry(first, client=ScriptedJudge(always("different")))

    seen = {}
    real = recon_module.shortlist_candidates

    def spy(entry, *, exclude_class_ids=(), provider=None):
        seen["excluded"] = list(exclude_class_ids)
        return real(entry, exclude_class_ids=exclude_class_ids, provider=provider)

    monkeypatch.setattr(recon_module, "shortlist_candidates", spy)

    second = capture("dana likes early meetings", claim_type="preference", at=200.0)
    outcome = reconcile_entry(second, client=judge)

    assert outcome.class_id == created.class_id
    assert created.class_id in seen["excluded"]


def test_the_shortlist_falls_back_to_a_bounded_index_scan_without_a_provider():
    judge = ScriptedJudge(always("different"))
    for index in range(recon_module.M5_SHORTLIST_CAP + 5):
        entry = capture(
            f"claim {index}",
            claim_type="preference",
            subject=f"s-{index}",
            at=100.0 + index,
        )
        reconcile_entry(entry, client=judge)

    fresh = capture("a brand new claim", claim_type="preference", at=900.0)
    candidates = shortlist_candidates(fresh, provider=None)
    assert len(candidates) <= recon_module.M5_SHORTLIST_CAP


def test_the_shortlist_ranks_by_similarity_and_caches_the_vector():
    provider = FakeProvider()
    judge = ScriptedJudge(always("different"))
    near = capture("dana prefers mornings", claim_type="preference", at=100.0)
    far = capture("zzz qqq xxx", claim_type="note", subject="other", at=110.0)
    reconcile_entry(near, client=judge, provider=provider)
    reconcile_entry(far, client=judge, provider=provider)

    probe = capture("dana prefers morning", claim_type="preference", at=200.0)
    ranked = shortlist_candidates(probe, provider=provider)
    assert ranked
    near_class = ClaimMembership.query.get(entry_redis_key=near.pk).class_id
    assert ranked[0] == near_class

    # The vector is cached in the reconciler's own hash, keyed by entry key.
    cached = get_REDIS_DB().hget(recon_module.EMBEDDING_CACHE_KEY, near.pk)
    assert cached
    assert len(json.loads(cached)) == provider.dimensions

    before = provider.calls
    shortlist_candidates(probe, provider=provider)
    assert provider.calls < before + 3  # cache hits, not a re-embed per class


# ---------------------------------------------------------------------------
# Representative selection
# ---------------------------------------------------------------------------


def test_the_representative_is_the_most_confirmed_live_member():
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(first, client=judge)
    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(second, client=judge)

    representative, uncertain = representative_for(outcome.class_id)
    assert representative is not None
    assert representative.pk == first.pk  # the join confirmed it
    assert uncertain is False


def test_selection_returns_the_uncertainty_flag_for_a_disjoined_class():
    """M5 returns the flag from its own selection call; a reader cannot be
    handed a silent winner, because there is not one to hand."""
    judge = ScriptedJudge(always("same"))
    left = capture("deadline monday", claim_type="deadline", at=100.0)
    right = capture("deadline friday", claim_type="deadline", at=100.0)
    reconcile_entry(left, client=judge)
    outcome = reconcile_entry(right, client=judge)

    _representative, uncertain = representative_for(outcome.class_id)
    assert uncertain is True


def test_a_superseded_member_is_excluded_from_representative_selection():
    judge = ScriptedJudge(always("different"))
    older = capture("deadline monday", claim_type="deadline", at=100.0)
    newer = capture("deadline friday", claim_type="deadline", at=200.0)
    reconcile_entry(older, client=judge)
    outcome = reconcile_entry(newer, client=judge)

    representative, _uncertain = representative_for(outcome.class_id)
    assert representative.pk == newer.pk


def test_an_empty_class_selects_nothing_rather_than_raising():
    assert representative_for("no-such-class") == (None, False)


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_append_only_reconcile_never_mutates_an_entry():
    """Compared as a raw hash, so this cannot pass by reading the code."""
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    reconcile_entry(first, client=judge)
    before = get_REDIS_DB().hgetall(first.pk)

    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(second, client=judge)

    assert get_REDIS_DB().hgetall(first.pk) == before


def test_class_membership_is_not_a_field_on_the_entry():
    """If it were, a relabel would be a re-save and the mixin would refuse."""
    declared = set(JournalEntry._meta.fields.keys())
    assert "class_id" not in declared
    assert "claim_slot" not in declared
    assert "disjunction_id" not in declared


# ---------------------------------------------------------------------------
# Privacy invariant
# ---------------------------------------------------------------------------


def test_membership_row_holds_no_claim_content():
    judge = ScriptedJudge(always("same"))
    secret_ish = "dana prefers mornings in Berlin"
    first = capture(secret_ish, claim_type="preference", at=100.0)
    reconcile_entry(first, client=judge)

    client = get_REDIS_DB()
    for pattern in ("ClaimMembership*", "ClaimClass*"):
        for key in scan_keys(pattern):
            name = key.decode() if isinstance(key, bytes) else key
            blob = repr(client.hgetall(name)) + repr(client.type(name))
            assert "Berlin" not in blob
            assert "mornings" not in blob
            assert "preference" not in blob

    row = ClaimMembership.query.get(entry_redis_key=first.pk)
    assert row.claim_slot == claim_slot(AGENT, "dana", "preference")
    assert "dana" not in row.claim_slot


# ---------------------------------------------------------------------------
# Erasure cascade
# ---------------------------------------------------------------------------


def test_hard_delete_cascades_through_every_leg():
    provider = FakeProvider()
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(first, client=judge, provider=provider)
    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(second, client=judge, provider=provider)
    # Prime the cache leg.
    shortlist_candidates(second, provider=provider)

    client = get_REDIS_DB()
    assert client.hget(recon_module.EMBEDDING_CACHE_KEY, first.pk)

    assert erase_entry(first) is True

    # 1. the record
    assert JournalEntry.query.get(redis_key=first.pk) is None
    # 2. the membership row
    assert ClaimMembership.query.get(entry_redis_key=first.pk) is None
    # 3. the cached embedding
    assert client.hget(recon_module.EMBEDDING_CACHE_KEY, first.pk) is None
    # 4. the class, recomputed rather than left pointing at an erased key
    row = ClaimClass.query.get(class_id=outcome.class_id)
    assert row is not None
    assert row.representative_key != first.pk
    assert row.member_count == 1


def test_erasing_the_last_member_drops_the_class_row():
    judge = ScriptedJudge(always("different"))
    only = capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(only, client=judge)
    assert ClaimClass.query.get(class_id=outcome.class_id) is not None

    erase_entry(only)
    assert ClaimClass.query.get(class_id=outcome.class_id) is None


# ---------------------------------------------------------------------------
# The merge log and replay
# ---------------------------------------------------------------------------


def test_merge_log_annotations_carry_the_pinned_payload_shape():
    """Payload shape, on the two outcomes this reconcile pair produces.

    Named for what it checks rather than for all seven ``ReconcileOutcome``
    actions: an always-"same" judge over two captures can only produce
    ``created`` then ``confirmed``, so a name claiming every outcome read as
    coverage the assertions never had. The other five are covered one test
    each -- ``joined`` and ``superseded`` by
    :func:`test_deadline_supersession_closes_the_loser_through_the_journal`,
    ``disjoined`` by :func:`test_split_verdict_disjoins`, ``noop`` by
    :func:`test_reconcile_is_idempotent_on_a_rerun`, and ``loser-absent`` by
    the test below.
    """
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(first, client=judge)
    reconcile_entry(second, client=judge)

    log = merge_log_entries(AGENT)
    assert len(log) == 2
    rationales = []
    for annotation in log:
        payload = json.loads(annotation.payload)
        assert payload["judge_version"] == CONVENTION_BOOK_VERSION
        assert payload["class_a"]
        assert payload["slot"]
        assert isinstance(payload["ts"], float)
        rationales.append(payload["rationale"])
    assert rationales == ["created", "confirmed"]


def test_a_loser_that_left_live_membership_records_loser_absent(monkeypatch):
    """The seventh outcome: the race window between shortlist and close.

    ``_supersede_loser`` catches ``ValidityMemberAbsentError`` and records
    ``loser-absent`` instead of crashing. The condition it detects is a
    *closed membership with the hash still present*, so it cannot be staged
    by deleting or by closing the loser first -- ``_live_members`` filters on
    validity, so a pre-closed loser is never shortlisted as an incumbent and
    the supersession branch is never reached. The window is genuinely between
    the shortlist read and the write, which under the single-writer invariant
    no test can open for real. So the journal call is made to raise once,
    which is exactly the state the real race hands it.
    """
    calls = []

    def absent(loser, **kwargs):
        calls.append(loser.pk)
        raise ValidityMemberAbsentError(f"member closed: {loser.pk}")

    judge = ScriptedJudge(always("different"))
    older = capture("deadline monday", claim_type="deadline", at=100.0)
    newer = capture("deadline friday", claim_type="deadline", at=200.0)
    reconcile_entry(older, client=judge)

    monkeypatch.setattr(
        recon_module.ProvenanceJournal, "supersede", staticmethod(absent)
    )
    outcome = reconcile_entry(newer, client=judge)

    assert calls == [older.pk]
    assert outcome.action == "loser-absent"
    assert outcome.superseded_key == older.pk

    # The winner stands, and the log names it as the annotation's target.
    absent_rows = [
        annotation
        for annotation in merge_log_entries(AGENT)
        if json.loads(annotation.payload)["rationale"] == "loser-absent"
    ]
    assert len(absent_rows) == 1
    payload = json.loads(absent_rows[0].payload)
    assert payload["class_a"] == outcome.class_id
    assert payload["other"] == older.pk
    assert absent_rows[0].target == newer._redis_key

    representative, _uncertain = representative_for(outcome.class_id)
    assert representative.pk == newer.pk


def test_a_luhn_tripping_class_id_does_not_block_the_merge_log(monkeypatch):
    """The CI failure that held this PR, deterministically.

    ``_append_merge_log`` used to write its JSON to ``statement``, which the
    never-record firewall scans; the Luhn rule matches any 13-19 digit run, and
    a uuid4 hex contains one ~0.23% of the time. With three or more hexes per
    payload that refused ~0.66% of merge-log writes with
    ``JournalBlockedError``, which is why the identical commit went green on
    CI's Redis job and red on Valkey. Pinning the id makes the 1-in-150 a
    certainty, so this fails every run if the payload ever moves back to a
    scanned field.
    """
    monkeypatch.setattr(recon_module, "_new_class_id", lambda: LUHN_TRIPPING_HEX)
    assert scan_never_record(LUHN_TRIPPING_HEX).blocked is True

    judge = ScriptedJudge(always("same"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(entry, client=judge)

    assert outcome.class_id == LUHN_TRIPPING_HEX
    log = merge_log_entries(AGENT)
    assert len(log) == 1
    assert json.loads(log[0].payload)["class_a"] == LUHN_TRIPPING_HEX
    assert log[0].statement == ""


def test_replay_rebuilds_index_from_the_merge_log_alone():
    judge = ScriptedJudge(always("same"))
    entries = [
        capture("dana prefers mornings", claim_type="preference", at=100.0),
        capture("dana likes early starts", claim_type="preference", at=200.0),
        capture("deadline friday", claim_type="deadline", at=300.0),
    ]
    for entry in entries:
        reconcile_entry(entry, client=judge)

    before = index_snapshot()
    assert before

    replayed = replay(AGENT, rebuild=True)
    assert replayed > 0
    assert index_snapshot() == before


def test_replay_rebuilds_the_index_for_a_disjoined_entry():
    """``always("same")`` only reaches ``created``/``confirmed``, so the test
    above cannot see this: a precedence tie writes a membership row and a
    ``disjoin`` annotation, and the replay ``disjoin`` branch only *repoints*
    rows it finds. Without a ``joined`` annotation naming each side, a
    from-genesis rebuild dropped both rows."""
    judge = ScriptedJudge(always("same"))
    left = capture("deadline monday", claim_type="deadline", at=100.0)
    right = capture("deadline friday", claim_type="deadline", at=100.0)
    reconcile_entry(left, client=judge)
    outcome = reconcile_entry(right, client=judge)
    assert outcome.action == "disjoined"

    before = index_snapshot()
    assert {left.pk, right.pk} <= set(before)

    replay(AGENT, rebuild=True)
    assert index_snapshot() == before


def test_replay_rebuilds_the_index_when_the_incumbent_wins_the_supersession():
    """``_supersede_loser`` logs against the *winner*, so on an incumbent win
    the reconciled entry is named by no outcome annotation at all. Its row is
    reconstructible only from the unconditional ``joined`` annotation."""
    judge = ScriptedJudge(always("same"))
    incumbent = capture("deadline friday", claim_type="deadline", at=200.0)
    challenger = capture("deadline monday", claim_type="deadline", at=100.0)
    reconcile_entry(incumbent, client=judge)
    outcome = reconcile_entry(challenger, client=judge)
    assert outcome.action == "superseded"
    assert outcome.superseded_key == challenger.pk

    before = index_snapshot()
    assert {incumbent.pk, challenger.pk} <= set(before)

    replay(AGENT, rebuild=True)
    assert index_snapshot() == before


def test_replay_rebuilds_the_index_for_a_probe_split_disjunction():
    """The third site that records a row outside the join path: a split verdict
    opens a fresh class for the entry before disjoining it."""
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana is a morning person", claim_type="preference", at=200.0)

    def split(claim_a, claim_b):
        return "same" if claim_a == second.statement else "different"

    reconcile_entry(first, client=ScriptedJudge(always("different")))
    outcome = reconcile_entry(second, client=ScriptedJudge(split))
    assert outcome.action == "disjoined"

    before = index_snapshot()
    assert {first.pk, second.pk} <= set(before)

    replay(AGENT, rebuild=True)
    assert index_snapshot() == before


def test_replay_reverses_a_retracted_merge():
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    created = reconcile_entry(first, client=judge)
    joined = reconcile_entry(second, client=judge)
    assert joined.class_id == created.class_id

    # Retract the annotation that recorded the join. Nothing is deleted: the
    # annotation's validity interval closes, so the live log no longer has it.
    join_log = [
        annotation
        for annotation in merge_log_entries(AGENT)
        if json.loads(annotation.payload)["rationale"] == "confirmed"
    ]
    assert len(join_log) == 1
    ProvenanceJournal.retract(join_log[0], agent_id=AGENT)

    replay(AGENT, rebuild=True)

    assert ClaimMembership.query.get(entry_redis_key=first.pk).class_id == (
        created.class_id
    )
    assert ClaimMembership.query.get(entry_redis_key=second.pk) is None


def test_replay_honours_the_watermark():
    judge = ScriptedJudge(always("different"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    reconcile_entry(first, client=judge)
    log = merge_log_entries(AGENT)
    watermark = float(getattr(log[0], recon_module.M5_REPLAY_WATERMARK_FIELD))

    # Nothing is strictly newer than the newest annotation.
    assert replay(AGENT, since=watermark) == 0
    # From genesis, everything replays.
    assert replay(AGENT, since=None) >= 1


def test_the_watermark_field_is_the_one_the_constant_names():
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    assert hasattr(entry, recon_module.M5_REPLAY_WATERMARK_FIELD)


# ---------------------------------------------------------------------------
# Withdrawn mitigations
# ---------------------------------------------------------------------------


def test_command_allowlist_no_hsetnx_or_set_nx_in_the_source():
    """Both mitigations were withdrawn; the single-writer invariant replaced
    them. A reintroduced advisory lock would be a silent scope change."""
    with open(RECON_SOURCE_PATH) as handle:
        source = handle.read()
    assert "HSETNX" not in source.upper().replace("HSETNX_NOT_USED", "")
    assert "hsetnx" not in source.lower()
    assert " nx=" not in source.lower()
    assert "setnx" not in source.lower()


def test_command_allowlist_no_such_command_reaches_the_client():
    judge = ScriptedJudge(always("same"))
    first = capture("dana prefers mornings", claim_type="preference", at=100.0)
    second = capture("dana likes early starts", claim_type="preference", at=200.0)
    reconcile_entry(first, client=judge)

    with CommandSpy(get_REDIS_DB()) as spy:
        reconcile_entry(second, client=judge)

    assert spy.commands  # the spy really saw traffic
    assert "HSETNX" not in spy.commands
    assert "SETNX" not in spy.commands


def test_no_redis_modules_are_used():
    """Everything must work on Redis *and* Valkey."""
    with open(RECON_SOURCE_PATH) as handle:
        source = handle.read().upper()
    for module_command in ("BF.", "CMS.", "TOPK.", "TDIGEST.", "FT."):
        assert module_command not in source


# ---------------------------------------------------------------------------
# Scope boundary
# ---------------------------------------------------------------------------


def test_no_m6_surfacing():
    """M5 groups and resolves; deciding what to show is M6's job.

    Asserted on the *import graph and the exported surface*, not on the word
    "surface" appearing in prose -- the read-surface docstrings legitimately
    use it, and a text match would fail for saying so.
    """
    with open(RECON_SOURCE_PATH) as handle:
        source = handle.read()
    assert "context_assembler" not in source
    assert "ContextAssembler" not in source
    assert "assemble" not in source.lower()
    for name in recon_module.__all__:
        assert "surfac" not in name.lower()
        assert "assemble" not in name.lower()


def test_the_read_surface_is_ordinary_orm_queries():
    judge = ScriptedJudge(always("different"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    outcome = reconcile_entry(entry, client=judge)

    classes = list(ClaimClass.query.filter(agent_id=AGENT))
    assert [row.class_id for row in classes] == [outcome.class_id]
    members = list(ClaimMembership.query.filter(class_id=outcome.class_id))
    assert [row.entry_redis_key for row in members] == [entry.pk]


def test_the_merge_kinds_are_registered_at_import():
    for kind in recon_module.MERGE_KINDS:
        assert kind in journal_module._REGISTERED_KINDS
        assert JournalEntry.kind_is_closing(kind) is False
        assert JournalEntry.kind_is_targetless(kind) is False


# ---------------------------------------------------------------------------
# The production trigger
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_stream_handler_reconciles_only_assert_captures():
    judge = ScriptedJudge(always("different"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    handler = recon_module.make_reconciliation_handler(AGENT, client=judge)

    await handler(
        [
            (
                "1-1",
                {
                    "op": "create",
                    "kind": "assert",
                    "agent_id": AGENT,
                    "pk": entry.pk,
                },
            )
        ]
    )
    assert ClaimMembership.query.get(entry_redis_key=entry.pk) is not None


@pytest.mark.asyncio
async def test_the_stream_handler_skips_its_own_annotations():
    judge = ScriptedJudge(always("different"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    handler = recon_module.make_reconciliation_handler(AGENT, client=judge)

    await handler(
        [("1-1", {"op": "create", "kind": "merge", "agent_id": AGENT, "pk": entry.pk})]
    )
    assert ClaimMembership.query.get(entry_redis_key=entry.pk) is None


@pytest.mark.asyncio
async def test_the_stream_handler_filters_by_agent():
    judge = ScriptedJudge(always("different"))
    entry = capture("dana prefers mornings", claim_type="preference", at=100.0)
    handler = recon_module.make_reconciliation_handler("someone-else", client=judge)

    await handler(
        [
            (
                "1-1",
                {
                    "op": "create",
                    "kind": "assert",
                    "agent_id": AGENT,
                    "pk": entry.pk,
                },
            )
        ]
    )
    assert ClaimMembership.query.get(entry_redis_key=entry.pk) is None


@pytest.mark.asyncio
async def test_an_unreadable_stream_entry_is_skipped_not_raised():
    handler = recon_module.make_reconciliation_handler(AGENT)
    await handler([("1-1", {"op": "create", "kind": "assert", "agent_id": AGENT})])
    await handler(
        [
            (
                "1-2",
                {
                    "op": "create",
                    "kind": "assert",
                    "agent_id": AGENT,
                    "pk": "JournalEntry:gone",
                },
            )
        ]
    )
    await handler([])


def test_the_consumer_is_wired_to_the_journal_stream():
    consumer = recon_module.reconciliation_consumer(
        agent_id=AGENT, consumer_name="worker-1"
    )
    assert consumer.stream_key == f"stream:{JournalEntry._stream_name}"
    assert consumer.stream_key == "stream:journal"


def test_reconcile_entry_is_documented_as_test_only():
    """The single-writer invariant is produced by the consumer being the sole
    writer, so the direct adapter must not read as a host-facing API."""
    doc = reconcile_entry.__doc__ or ""
    assert "test-only" in doc.lower()
    assert "not a production" in doc.lower()


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_mega_class_velocity_is_telemetry_and_never_a_gate(caplog):
    """A legitimately large class must not be blocked, so the threshold warns."""
    original = recon_module.MEGA_CLASS_VELOCITY_ALERT
    recon_module.MEGA_CLASS_VELOCITY_ALERT = 0
    try:
        judge = ScriptedJudge(always("same"))
        first = capture("dana prefers mornings", claim_type="preference", at=100.0)
        reconcile_entry(first, client=judge)
        second = capture("dana likes early starts", claim_type="preference", at=200.0)
        with caplog.at_level("WARNING", logger="POPOTO.Reconciliation"):
            outcome = reconcile_entry(second, client=judge)
    finally:
        recon_module.MEGA_CLASS_VELOCITY_ALERT = original

    assert outcome.action == "joined"  # the join stands
    assert any("mega-class velocity" in record.message for record in caplog.records)
