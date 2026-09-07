"""``rank_decayed``: the decay-ranking seam owned by the field (#648).

Until #648 every ``EVAL`` of a decay script was assembled by a caller, and
``recipes/context_assembler.py`` held its own copy of *both* KEYS layouts in one
function. The two layouts are mutually incompatible by design — confidence is
``KEYS[2]`` in ``DECAY_SCORE_LUA`` but ``KEYS[4]`` in ``CYCLIC_DECAY_LUA``,
because the cyclic fork binds cycles and pressure to 2 and 3 — and both scripts
carry a comment warning that reusing index 2 in the cyclic script would
``cmsgpack.unpack`` the cycles array as a confidence dict: a silent corrupt read
rather than a clean crash.

``DecayingSortedField.rank_decayed`` and the ``CyclicDecayField.rank_decayed``
override put each layout in the class that owns its script, so neither body
contains an index it must not use. These tests pin the properties that make the
seam safe to route the recipe through: the two scripts stay separated, the
``n=None`` cardinality path keeps the ``ZCARD``-then-``EVAL`` wire order (and
skips the ``EVAL`` entirely on an empty set), an explicit ``n`` costs no
``ZCARD``, the cyclic override drops the validity gate on purpose, and the
exclusion-set read stays subtractive rather than becoming a whitelist.
"""

import os
import sys
import time
import uuid

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto import redis_db  # noqa: E402
from src.popoto.fields import decaying_sorted_field, validity_field  # noqa: E402
from src.popoto.fields.cyclic_decay_field import CyclicDecayField  # noqa: E402
from src.popoto.fields.decaying_sorted_field import (  # noqa: E402
    DecayingSortedField,
)
from src.popoto.fields.validity_field import ValidityField  # noqa: E402

DAY = 86400.0


class RankedMemory(popoto.Model):
    key = popoto.KeyField()
    strength = popoto.FloatField(default=1.0)
    last_accessed = DecayingSortedField(decay_rate=0.5, base_score_field="strength")


class CyclicMemory(popoto.Model):
    key = popoto.KeyField()
    strength = popoto.FloatField(default=1.0)
    last_accessed = CyclicDecayField(decay_rate=0.5, base_score_field="strength")


class GatedMemory(popoto.Model):
    key = popoto.KeyField()
    validity = ValidityField()


def _zset_key(model_class, field_name):
    field = model_class._meta.fields[field_name]
    return field.get_sortedset_db_key(model_class, field_name).redis_key


def _aged(model_class, key, *, days, now, **kwargs):
    """Save a record and plant its decay timestamp ``days`` in the past.

    ``Model.touch()`` takes no timestamp, so the ZSET score is set directly —
    the same idiom ``tests/test_decaying_sorted_field.py`` uses to age records.
    """
    record = model_class(key=key, **kwargs)
    record.save()
    redis_db.POPOTO_REDIS_DB.zadd(
        _zset_key(model_class, "last_accessed"),
        {record.db_key.redis_key: now - days * DAY},
    )
    return record


def _capture(monkeypatch):
    """Record the command verb of every call issued by the code under test.

    The spy goes on the client object the *field modules* hold, not on
    ``redis_db.POPOTO_REDIS_DB``. Those modules bind the client at import time
    (``from ..redis_db import POPOTO_REDIS_DB``, the pattern the whole package
    uses), so a test that reconfigures the connection — ``test_connection.py``
    rebinds the module attribute to a fresh client — leaves the two references
    pointing at different objects. Patching the module attribute then spies on
    a client nobody calls and captures nothing, which is a silent pass on the
    empty list rather than a failure. Patching every distinct object keeps the
    capture correct under any suite ordering.
    """
    commands = []

    def spy_on(client):
        original = client.execute_command

        def spy(*args, **kwargs):
            commands.append(str(args[0]) if args else None)
            return original(*args, **kwargs)

        monkeypatch.setattr(client, "execute_command", spy)

    seen = []
    for client in (
        decaying_sorted_field.POPOTO_REDIS_DB,
        validity_field.POPOTO_REDIS_DB,
        redis_db.POPOTO_REDIS_DB,
    ):
        if not any(client is s for s in seen):
            seen.append(client)
            spy_on(client)
    return commands


def _decoded(reply):
    """The flat ``[member, score, ...]`` reply as a ``{member: score}`` dict."""
    out = {}
    for i in range(0, len(reply), 2):
        member = reply[i]
        if isinstance(member, bytes):
            member = member.decode()
        out[member] = float(reply[i + 1])
    return out


# ---------------------------------------------------------------------------
# The seam agrees with the caller it was extracted from
# ---------------------------------------------------------------------------


def test_rank_decayed_matches_top_by_decay():
    """The seam must score identically to the existing query path, or the
    metacognitive proxy silently disagrees with retrieval."""
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    for i in range(1, 4):
        _aged(RankedMemory, f"{tag}-{i}", days=i, now=now, strength=float(i))

    # ``top_by_decay`` reads the clock itself, so the two calls cannot share an
    # exact ``now``. Records are planted whole days apart, which makes the
    # *ordering* insensitive to the milliseconds between them — and ordering is
    # what the no-drift contract between proxy and query is about.
    ranked = _decoded(
        RankedMemory._meta.fields["last_accessed"].rank_decayed(
            _zset_key(RankedMemory, "last_accessed"), now=time.time()
        )
    )
    via_query = RankedMemory.query.top_by_decay("last_accessed", n=10)

    assert ranked, "seam returned nothing"
    ordered_by_seam = [k for k, _ in sorted(ranked.items(), key=lambda kv: -kv[1])]
    assert ordered_by_seam == [r.db_key.redis_key for r in via_query]


# ---------------------------------------------------------------------------
# n=None: the ZCARD lives inside, and an empty set costs no EVAL
# ---------------------------------------------------------------------------


def test_n_none_issues_zcard_before_eval(monkeypatch):
    """Wire order is ZCARD then EVAL. The recipe's captured sequence depends on
    it: the ZCARD it used to issue itself now happens here, in the same place."""
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    _aged(RankedMemory, f"{tag}-1", days=1, now=now)

    field = RankedMemory._meta.fields["last_accessed"]
    zkey = _zset_key(RankedMemory, "last_accessed")

    commands = _capture(monkeypatch)
    field.rank_decayed(zkey, now=now)

    verbs = [c.upper() for c in commands]
    assert "ZCARD" in verbs
    evals = [i for i, v in enumerate(verbs) if v in ("EVAL", "EVALSHA")]
    assert evals, "expected an EVAL"
    assert verbs.index("ZCARD") < evals[0]


def test_empty_zset_short_circuits_without_any_eval(monkeypatch):
    """``if not cardinality: continue`` in the recipe became a ``return []``
    here. An EVAL on an empty set would be a new command on the wire."""
    field = RankedMemory._meta.fields["last_accessed"]
    absent = f"$SortF:RankedMemory:last_accessed:{uuid.uuid4().hex}"

    commands = _capture(monkeypatch)
    result = field.rank_decayed(absent, now=time.time())

    assert result == []
    verbs = [c.upper() for c in commands]
    assert "EVAL" not in verbs and "EVALSHA" not in verbs


def test_explicit_n_issues_no_zcard(monkeypatch):
    """The query path passes its own limit and must not pay for a ZCARD."""
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    _aged(RankedMemory, f"{tag}-1", days=1, now=now)

    field = RankedMemory._meta.fields["last_accessed"]
    commands = _capture(monkeypatch)
    field.rank_decayed(_zset_key(RankedMemory, "last_accessed"), now=now, n=5)

    assert "ZCARD" not in [c.upper() for c in commands]


# ---------------------------------------------------------------------------
# The two layouts stay apart
# ---------------------------------------------------------------------------


def test_cyclic_override_is_a_distinct_implementation():
    """If the override were ever dropped, a CyclicDecayField would silently
    evaluate DECAY_SCORE_LUA against a ZSET whose companion hashes it never
    passes — the exact corruption the split prevents."""
    assert (
        CyclicDecayField.rank_decayed is not DecayingSortedField.rank_decayed
    ), "CyclicDecayField must override rank_decayed"


def test_cyclic_rank_decayed_returns_scores():
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    for i in range(1, 4):
        _aged(CyclicMemory, f"{tag}-{i}", days=i, now=now, strength=float(i))

    scored = _decoded(
        CyclicMemory._meta.fields["last_accessed"].rank_decayed(
            _zset_key(CyclicMemory, "last_accessed"), now=now
        )
    )
    assert len(scored) == 3
    assert all(v > 0 for v in scored.values())


def test_cyclic_override_ignores_the_validity_gate():
    """CYCLIC_DECAY_LUA has no validity gate: KEYS 1-4 are taken and its header
    forbids renumbering. The parameter exists so callers stay polymorphic, and
    dropping it is deliberate — pinned here so a future 'fix' is a visible
    decision rather than an accident. See TestCyclicDecayGatingGap."""
    tag = uuid.uuid4().hex[:8]
    now = time.time()
    for i in range(1, 4):
        _aged(CyclicMemory, f"{tag}-{i}", days=i, now=now, strength=float(i))

    field = CyclicMemory._meta.fields["last_accessed"]
    zkey = _zset_key(CyclicMemory, "last_accessed")

    ungated = _decoded(field.rank_decayed(zkey, now=now))
    with_gate_args = _decoded(
        field.rank_decayed(
            zkey,
            now=now,
            validity=("some:invalid_at", "some:valid_from", str(now)),
        )
    )
    assert ungated == with_gate_args


# ---------------------------------------------------------------------------
# The exclusion read stays subtractive
# ---------------------------------------------------------------------------


def test_resolve_excluded_keys_unions_closed_and_future():
    from src.popoto.fields.supersession import SupersessionProtocol

    tag = uuid.uuid4().hex[:8]
    closed = GatedMemory(key=f"{tag}-closed")
    closed.save()
    open_record = GatedMemory(key=f"{tag}-open")
    open_record.save()
    SupersessionProtocol.invalidate(closed)

    excluded = ValidityField.resolve_excluded_keys(GatedMemory, "validity")

    assert closed.db_key.redis_key in excluded
    assert open_record.db_key.redis_key not in excluded


def test_unmanaged_records_are_never_excluded():
    """The whole reason this is an exclusion set and not a whitelist: a record
    with no interval predates the field's adoption and must stay retrievable.
    ``resolve_valid_keys`` would omit it, which is a data-visibility
    regression, not a stricter gate."""
    tag = uuid.uuid4().hex[:8]
    unmanaged = RankedMemory(key=f"{tag}-unmanaged")
    unmanaged.save()

    excluded = ValidityField.resolve_excluded_keys(GatedMemory, "validity")

    assert unmanaged.db_key.redis_key not in excluded


def test_resolve_excluded_keys_issues_exactly_two_range_reads(monkeypatch):
    """Two ZRANGEBYSCOREs, in the order the assembler established. A third read
    or a reorder would show up in the base-vs-branch command capture."""
    commands = _capture(monkeypatch)
    ValidityField.resolve_excluded_keys(GatedMemory, "validity", as_of=time.time())

    ranges = [c for c in commands if c.upper() == "ZRANGEBYSCORE"]
    assert len(ranges) == 2


def test_excluded_keys_are_decoded_strings():
    """Consumers compare against ``record.db_key.redis_key``, which is a str."""
    from src.popoto.fields.supersession import SupersessionProtocol

    tag = uuid.uuid4().hex[:8]
    record = GatedMemory(key=f"{tag}-c")
    record.save()
    SupersessionProtocol.invalidate(record)

    excluded = ValidityField.resolve_excluded_keys(GatedMemory, "validity")
    assert excluded
    assert all(isinstance(k, str) for k in excluded)


# ---------------------------------------------------------------------------
# The recipe no longer holds any of this
# ---------------------------------------------------------------------------


def test_context_assembler_names_no_lua_and_opens_no_client():
    """The seven direct-Redis sites #648 removed, asserted as an inventory so a
    regression is caught at the import boundary rather than in a wire capture.

    Asserted on the module *namespace*, not its source text: the module still
    discusses ``DECAY_SCORE_LUA`` in prose (it explains which gating layer lives
    inside the script), and forbidding the words would forbid the explanation.
    What must not come back is the ability to *call* any of it.
    """
    import inspect

    from src.popoto.recipes import context_assembler

    for name in (
        "DECAY_SCORE_LUA",
        "CYCLIC_DECAY_LUA",
        "POPOTO_REDIS_DB",
        "run_lua",
    ):
        assert not hasattr(context_assembler, name), (
            f"context_assembler re-imported {name}; the recipe is supposed to "
            "reach Redis only through the field and model layers (#648)"
        )

    source = inspect.getsource(context_assembler)
    assert ".zrangebyscore(" not in source
    assert ".zcard(" not in source
    assert "POPOTO_REDIS_DB.pipeline(" not in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
