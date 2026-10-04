"""Validity and supersession, two legs, one set of assertions (#759 M3).

Every test here runs on Redis and on Postgres with the same assertions:
Redis is the oracle (``SUPERSEDE_LUA``, ``DECAY_SCORE_LUA``'s gate, the
composite mask), and whatever it returns, stores or raises -- down to the text
of each typed exception -- is what the Postgres backend must return, store or
raise. These are the model-level re-expressions of the #631 POC's
``tests/conformance/test_validity.py`` (archive branch ``poc/backend-seam``):
the 17-row exclusion table (its ``TestValidityGate``), the four error replies
with byte-identical text, the open-pointer lifecycle, the ``a``/``ab`` prefix
case, #588's same-transaction successor, and the crossing-chains
interleaving that deadlocked the POC (#750 B1).

State is seeded and read through small leg helpers below: on Redis the six
``$ValidityF`` keys, on Postgres the record's interval columns and the pointer
table. Nothing touches Redis database 0 or Postgres schema ``public``.
"""

import os
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(SCRIPT_DIR))

import pytest  # noqa: E402
from src import popoto  # noqa: E402
from src.popoto import (  # noqa: E402
    SupersessionProtocol,
    ValidityCloseBeforeStartError,
    ValidityField,
    ValidityMemberAbsentError,
    ValidityValidFromConflictError,
)
from src.popoto.backends import RecordId  # noqa: E402
from src.popoto.backends.routing import non_redis_backend  # noqa: E402
from src.popoto.fields.decaying_sorted_field import DecayingSortedField  # noqa: E402
from src.popoto.fields.validity_field import (  # noqa: E402
    CLOSE_BEFORE_START_ERROR,
    MEMBER_ABSENT_ERROR,
    VALID_FROM_CONFLICT_ERROR,
    ValidityError,
    map_lua_error,
)
from src.popoto.redis_db import get_REDIS_DB  # noqa: E402

pytestmark = [pytest.mark.conformance, pytest.mark.usefixtures("backend")]

INF = float("inf")
NOW = time.time()
#: A close instant after every save a test makes (they open at save time).
LATER = NOW + 3600.0


class ParityClaim(popoto.Model):
    name = popoto.UniqueKeyField()
    relevance = DecayingSortedField()
    validity = ValidityField()


class ParityOther(popoto.Model):
    name = popoto.UniqueKeyField()
    validity = ValidityField()


MODELS = (ParityClaim, ParityOther)
FIELD = "validity"


def _wipe_redis():
    for model in MODELS:
        prefix = ValidityField.get_prefix_db_key(model, FIELD).redis_key
        keys = list(get_REDIS_DB().keys(f"{prefix}*"))
        if keys:
            get_REDIS_DB().delete(*keys)


@pytest.fixture(autouse=True)
def clean(backend):
    if backend.is_redis:
        for model in MODELS:
            model.delete_all()
        _wipe_redis()
    yield
    if backend.is_redis:
        for model in MODELS:
            model.delete_all()
        _wipe_redis()


# -- leg helpers -------------------------------------------------------------


def _save(name, model=ParityClaim, **kwargs):
    record = model(name=name, **kwargs)
    record.save()
    return record


def _key(record):
    return record.db_key.redis_key


def _set_interval(record, *, valid_from="keep", invalid_at="keep"):
    """Put ``record`` at an exact interval. ``None`` takes it out of that
    index (an absent end); ``"keep"`` leaves it."""
    model = type(record)
    backend = non_redis_backend(model)
    if backend is not None:
        sets, params = [], []
        for suffix, value in (("__valid_from", valid_from), ("__invalid_at", invalid_at)):
            if value == "keep":
                continue
            sets.append(f'"{FIELD}{suffix}" = %s')
            params.append(value)
        table = backend._table(model._meta.spec).qualified
        backend._run(
            f'UPDATE {table} SET {", ".join(sets)} WHERE "_pk" = %s',
            params + [_key(record)],
            write=True,
        )
        return
    keys = ValidityField.get_all_keys(model, FIELD)
    for index, value in (("valid_from", valid_from), ("invalid_at", invalid_at)):
        if value == "keep":
            continue
        if value is None:
            get_REDIS_DB().zrem(keys[index], _key(record))
        else:
            get_REDIS_DB().zadd(keys[index], {_key(record): value})


def _interval(record):
    model = type(record)
    backend = non_redis_backend(model)
    if backend is not None:
        state = backend.field_call(
            model._meta.spec,
            FIELD,
            "interval",
            RecordId.from_key(model._meta.model_name, _key(record)),
        )
        state = state or {}
        return state.get("valid_from"), state.get("invalid_at")
    keys = ValidityField.get_all_keys(model, FIELD)
    return (
        get_REDIS_DB().zscore(keys["valid_from"], _key(record)),
        get_REDIS_DB().zscore(keys["invalid_at"], _key(record)),
    )


def _links(record):
    """``(superseded_by, supersedes)`` as strings."""
    model = type(record)
    backend = non_redis_backend(model)
    if backend is not None:
        state = backend.field_call(
            model._meta.spec,
            FIELD,
            "interval",
            RecordId.from_key(model._meta.model_name, _key(record)),
        )
        state = state or {}
        return state.get("superseded_by"), state.get("supersedes")
    keys = ValidityField.get_all_keys(model, FIELD)
    out = []
    for index in ("chain_fwd", "chain_rev"):
        raw = get_REDIS_DB().hget(keys[index], _key(record))
        out.append(raw.decode() if isinstance(raw, bytes) else raw)
    return tuple(out)


def _pointer(digest, model=ParityClaim):
    backend = non_redis_backend(model)
    if backend is not None:
        return backend.field_call(model._meta.spec, FIELD, "pointer", digest)
    raw = get_REDIS_DB().get(ValidityField.get_open_pointer_key(model, FIELD, digest))
    return raw.decode() if isinstance(raw, bytes) else raw


def _typed(exc):
    return exc if isinstance(exc, ValidityError) else map_lua_error(exc)


def _names(records):
    return sorted(r.name for r in records)


# -- the 17-row exclusion table -------------------------------------------------

#: The #631 POC's ``GATE_CASES`` (``tests/conformance/test_decay.py`` on the
#: archive branch): ``(id, invalid_at, valid_from, as_of, included)``, with
#: ``None`` for an end absent from its index. ``invalid_at <= as_of OR
#: valid_from > as_of`` excludes; an absent end never excludes; ``+inf`` is
#: open, and closed only at ``as_of = +inf``.
GATE_CASES = [
    ("unmanaged", None, None, NOW, True),
    ("closed-before", NOW - 1, None, NOW, False),
    ("closed-exactly-at", NOW, None, NOW, False),
    ("closes-after", NOW + 1, None, NOW, True),
    ("open-inf", INF, None, NOW, True),
    ("starts-after", None, NOW + 1, NOW, False),
    ("starts-exactly-at", None, NOW, NOW, True),
    ("started-before", None, NOW - 1, NOW, True),
    ("open-and-started", INF, NOW - 5, NOW, True),
    ("closed-and-started", NOW - 5, NOW - 10, NOW, False),
    ("open-but-future", INF, NOW + 5, NOW, False),
    ("malformed-both-clauses", NOW - 5, NOW + 5, NOW, False),
    ("as-of-inf-closes-open", INF, None, INF, False),
    ("as-of-inf-future-start", None, NOW, INF, True),
    ("as-of-minus-inf-start", None, NOW, -INF, False),
    ("as-of-minus-inf-close", NOW, None, -INF, True),
    ("as-of-1e308", NOW, None, 1e308, False),
]


class TestExclusionRule:
    @pytest.mark.parametrize(
        "invalid_at,valid_from,as_of,included",
        [c[1:] for c in GATE_CASES],
        ids=[c[0] for c in GATE_CASES],
    )
    def test_every_gate_applies_the_rule(self, invalid_at, valid_from, as_of, included):
        """The same membership through all three gating layers: the decay
        ranking (``top_by_decay``), the composite mask, and
        ``resolve_excluded_keys``. An unmanaged control is never gated."""
        g = _save("g")
        control = _save("control")
        _set_interval(control, valid_from=None, invalid_at=None)
        _set_interval(g, valid_from=valid_from, invalid_at=invalid_at)

        ranked = _names(ParityClaim.query.top_by_decay(n=10, as_of=as_of))
        composite = _names(
            ParityClaim.query.composite_score(
                indexes={"relevance": 1.0}, limit=10, as_of=as_of
            )
        )
        excluded = ValidityField.resolve_excluded_keys(ParityClaim, FIELD, as_of=as_of)

        assert "control" in ranked and "control" in composite
        assert ("g" in ranked) is included
        assert ("g" in composite) is included
        assert (_key(g) in excluded) is (not included)
        assert _key(control) not in excluded

    @pytest.mark.parametrize(
        "invalid_at,valid_from,as_of,included",
        [c[1:] for c in GATE_CASES],
        ids=[c[0] for c in GATE_CASES],
    )
    def test_the_valid_set_needs_both_ends(
        self, invalid_at, valid_from, as_of, included
    ):
        """``filter(validity__as_of=t)`` and ``resolve_valid_keys`` are the
        whitelist: ``valid_from <= t AND invalid_at > t``, both present."""
        g = _save("g")
        _set_interval(g, valid_from=valid_from, invalid_at=invalid_at)
        valid = (
            valid_from is not None
            and invalid_at is not None
            and valid_from <= as_of < invalid_at
        )
        assert ("g" in _names(ParityClaim.query.filter(validity__as_of=as_of))) is valid
        keys = ValidityField.resolve_valid_keys(ParityClaim, FIELD, as_of=as_of)
        assert (_key(g) in keys) is valid

    def test_the_as_of_bound_is_bit_exact(self):
        """One ulp either side of a close: ``timestamptz`` would fold all
        three together, which is why the interval is ``double precision``."""
        import math

        as_of = 1_700_000_000.123456
        after, at, before = _save("after"), _save("at"), _save("before")
        _set_interval(after, valid_from=0.0, invalid_at=math.nextafter(as_of, INF))
        _set_interval(at, valid_from=0.0, invalid_at=as_of)
        _set_interval(before, valid_from=0.0, invalid_at=math.nextafter(as_of, -INF))
        assert _names(ParityClaim.query.top_by_decay(n=10, as_of=as_of)) == ["after"]
        assert _names(ParityClaim.query.filter(validity__as_of=as_of)) == ["after"]

    def test_a_nan_as_of_excludes_nothing_from_the_ranking(self):
        closed, future = _save("closed"), _save("future")
        _set_interval(closed, invalid_at=NOW - 1)
        _set_interval(future, valid_from=NOW + 1)
        ranked = _names(ParityClaim.query.top_by_decay(n=10, as_of=float("nan")))
        assert ranked == ["closed", "future"]


# -- the four error replies, byte for byte ------------------------------------


MEMBER_ABSENT_TEXT = (
    "ValidityField: a member named by this call does not exist at write "
    "time, so no interval, chain link, or pointer was written ({detail})"
)
CLOSE_BEFORE_START_TEXT = (
    "ValidityField: close-at precedes the record's own valid_from ({detail})"
)
VALID_FROM_CONFLICT_TEXT = (
    "ValidityField: the asserted valid_from disagrees with the start "
    "already stored for this record; valid-time has one writer, the field "
    "value at construction ({detail})"
)


class TestTypedErrors:
    def test_an_absent_successor(self):
        identity = SupersessionProtocol.identity_key("u", "plan")
        with pytest.raises(ValidityMemberAbsentError) as info:
            SupersessionProtocol.supersede(
                ParityClaim(name="ghost"), identity_key=identity
            )
        detail = f"{MEMBER_ABSENT_ERROR} successor ParityClaim:ghost"
        assert str(info.value) == MEMBER_ABSENT_TEXT.format(detail=detail)
        assert _pointer(identity) is None

    def test_an_absent_asserted_incumbent(self):
        new = _save("new")
        with pytest.raises(ValidityMemberAbsentError) as info:
            ValidityField.execute_supersede(
                ParityClaim,
                FIELD,
                new_member=_key(new),
                mode="invalidate",
                old_member="ParityClaim:gone",
            )
        detail = f"{MEMBER_ABSENT_ERROR} incumbent ParityClaim:gone"
        assert str(info.value) == MEMBER_ABSENT_TEXT.format(detail=detail)
        assert _links(new) == (None, None)

    def test_a_close_before_the_start(self):
        record = _save("a")
        valid_from, _ = _interval(record)
        with pytest.raises(ValidityCloseBeforeStartError) as info:
            SupersessionProtocol.invalidate(record, at=valid_from - 60)
        assert str(info.value) == CLOSE_BEFORE_START_TEXT.format(
            detail=CLOSE_BEFORE_START_ERROR
        )
        assert _interval(record) == (valid_from, INF)

    @pytest.mark.parametrize(
        "stored,requested,rendered",
        [
            (1759500000.123456, 100.5, "1759500000.1235 100.5"),
            (100.0, 0.3, "100 0.3"),
        ],
    )
    def test_a_disagreeing_asserted_valid_from(self, stored, requested, rendered):
        """The numbers are rendered as Lua's ``tostring`` prints them
        (``%.14g``), on both legs."""
        record = _save("a")
        _set_interval(record, valid_from=stored)
        with pytest.raises(ValidityValidFromConflictError) as info:
            ValidityField.execute_supersede(
                ParityClaim,
                FIELD,
                new_member=_key(record),
                mode="open",
                valid_from=requested,
                assert_valid_from=True,
            )
        detail = f"{VALID_FROM_CONFLICT_ERROR} {rendered}"
        assert str(info.value) == VALID_FROM_CONFLICT_TEXT.format(detail=detail)
        assert _interval(record)[0] == stored

    def test_every_typed_error_is_a_value_error(self):
        for exc in (
            ValidityMemberAbsentError,
            ValidityCloseBeforeStartError,
            ValidityValidFromConflictError,
        ):
            assert issubclass(exc, ValueError)


# -- supersede, mode by mode ----------------------------------------------------


class TestModes:
    def test_supersede_closes_chains_and_repoints(self):
        identity = SupersessionProtocol.identity_key("u", "plan")
        v1, v2, v3 = _save("v1"), _save("v2"), _save("v3")
        assert SupersessionProtocol.supersede(v1, identity_key=identity) is None
        assert SupersessionProtocol.supersede(v2, identity_key=identity, at=LATER) == (
            _key(v1)
        )
        assert SupersessionProtocol.supersede(
            v3, identity_key=identity, at=LATER + 1
        ) == _key(v2)
        assert _interval(v1)[1] == LATER and _interval(v2)[1] == LATER + 1
        assert _interval(v3)[1] == INF
        assert _links(v1) == (_key(v2), None)
        assert _links(v2) == (_key(v3), _key(v1))
        assert _links(v3) == (None, _key(v2))
        assert _pointer(identity) == _key(v3)
        for anchor in (v1, v2, v3):
            assert [r.name for r in SupersessionProtocol.chain(anchor)] == [
                "v1",
                "v2",
                "v3",
            ]

    def test_an_explicit_incumbent_beats_the_pointer(self):
        identity = SupersessionProtocol.identity_key("u", "plan")
        pointed, named, new = _save("pointed"), _save("named"), _save("new")
        SupersessionProtocol.supersede(pointed, identity_key=identity)
        closed = ValidityField.execute_supersede(
            ParityClaim,
            FIELD,
            new_member=_key(new),
            mode="supersede",
            old_member=_key(named),
            identity_digest=identity,
            close_at=LATER,
        )
        assert closed == _key(named)
        assert _interval(pointed)[1] == INF
        assert _pointer(identity) == _key(new)

    def test_closing_is_idempotent_and_never_forks(self):
        old, a, b = _save("old"), _save("a"), _save("b")
        first = SupersessionProtocol.invalidate(old, superseded_by=a, at=LATER)
        second = SupersessionProtocol.invalidate(old, superseded_by=b, at=LATER + 5)
        assert first == _key(old) and second is None
        assert _interval(old)[1] == LATER
        assert _links(old) == (_key(a), None)
        assert _links(b) == (None, None)
        assert _interval(b)[1] == INF

    def test_a_record_cannot_supersede_itself(self):
        identity = SupersessionProtocol.identity_key("u", "plan")
        a = _save("a")
        SupersessionProtocol.supersede(a, identity_key=identity)
        assert SupersessionProtocol.supersede(a, identity_key=identity) is None
        assert _interval(a)[1] == INF and _links(a) == (None, None)

    def test_an_incumbent_with_no_interval_is_not_closed(self):
        old, new = _save("old"), _save("new")
        _set_interval(old, valid_from=None, invalid_at=None)
        assert SupersessionProtocol.invalidate(old, superseded_by=new) is None
        assert _interval(old) == (None, None)
        assert _links(old) == (None, None)

    def test_closing_exactly_at_the_start_is_allowed(self):
        record = _save("a")
        start, _ = _interval(record)
        assert SupersessionProtocol.invalidate(record, at=start) == _key(record)
        assert _interval(record) == (start, start)

    def test_a_save_never_reopens_a_closed_record(self):
        record = _save("a")
        SupersessionProtocol.invalidate(record, at=LATER + 10)
        closed = _interval(record)
        record.save()
        assert _interval(record) == closed

    def test_a_save_fills_an_absent_end_and_keeps_the_rest(self):
        record = _save("a")
        start, _ = _interval(record)
        _set_interval(record, invalid_at=None)
        record.save()
        assert _interval(record) == (start, INF)

    def test_save_and_supersede_and_save_and_invalidate(self):
        identity = SupersessionProtocol.identity_key("u", "plan")
        first = SupersessionProtocol.save_and_supersede(
            ParityClaim(name="first"), identity_key=identity
        )
        assert first.closed_key is None
        second = SupersessionProtocol.save_and_supersede(
            ParityClaim(name="second"), identity_key=identity, at=LATER + 50
        )
        assert second.closed_key == "ParityClaim:first"
        third = SupersessionProtocol.save_and_invalidate(
            ParityClaim(name="third"), closes=second.instance, at=LATER + 60
        )
        assert third.closed_key == "ParityClaim:second"
        assert [r.name for r in SupersessionProtocol.chain(third.instance)] == [
            "first",
            "second",
            "third",
        ]


# -- the open pointer and record deletion ------------------------------------------


class TestOpenPointer:
    def test_lifecycle(self):
        identity = "d" * 16
        old, new = _save("old"), _save("new")
        assert _pointer(identity) is None
        ValidityField.execute_supersede(
            ParityClaim, FIELD, new_member=_key(old), identity_digest=identity
        )
        assert _pointer(identity) == _key(old)
        ValidityField.execute_supersede(
            ParityClaim,
            FIELD,
            new_member=_key(new),
            mode="supersede",
            identity_digest=identity,
            close_at=LATER + 50,
        )
        assert _pointer(identity) == _key(new)
        # A pure invalidate leaves the pointer naming the record it closed;
        # the next supersede on the identity reads it as already closed.
        ValidityField.execute_supersede(
            ParityClaim,
            FIELD,
            mode="invalidate",
            identity_digest=identity,
            close_at=LATER + 60,
        )
        assert _pointer(identity) == _key(new)
        assert _interval(new)[1] == LATER + 60
        new.delete()
        assert _pointer(identity) is None

    def test_one_record_open_under_two_identities(self):
        a = _save("a")
        for digest in ("1" * 16, "2" * 16):
            ValidityField.execute_supersede(
                ParityClaim, FIELD, new_member=_key(a), identity_digest=digest
            )
        assert _pointer("1" * 16) == _pointer("2" * 16) == _key(a)
        found = ValidityField.find_open_pointers_for_member(ParityClaim, FIELD, _key(a))
        assert sorted(found) == sorted(
            ValidityField.get_open_pointer_key(ParityClaim, FIELD, d)
            for d in ("1" * 16, "2" * 16)
        )
        a.delete()
        assert _pointer("1" * 16) is None and _pointer("2" * 16) is None

    def test_deleting_a_never_takes_ab_with_it(self):
        """#750's ``drop_validity`` prefix over-match: ``a`` is a prefix of
        ``ab``, and deleting ``a`` must leave ``ab``'s interval and pointer."""
        a, ab = _save("a"), _save("ab")
        ValidityField.execute_supersede(
            ParityClaim, FIELD, new_member=_key(a), identity_digest="a" * 16
        )
        ValidityField.execute_supersede(
            ParityClaim, FIELD, new_member=_key(ab), identity_digest="b" * 16
        )
        ab_interval = _interval(ab)
        a.delete()
        assert _pointer("a" * 16) is None
        assert _pointer("b" * 16) == _key(ab)
        assert _interval(ab) == ab_interval
        assert ab_interval[1] == INF

    def test_another_models_pointer_survives(self):
        digest = "c" * 16
        mine, theirs = _save("x"), _save("x", model=ParityOther)
        ValidityField.execute_supersede(
            ParityClaim, FIELD, new_member=_key(mine), identity_digest=digest
        )
        ValidityField.execute_supersede(
            ParityOther, FIELD, new_member=_key(theirs), identity_digest=digest
        )
        mine.delete()
        assert _pointer(digest) is None
        assert _pointer(digest, model=ParityOther) == _key(theirs)

    def test_a_deleted_neighbour_ends_the_chain(self):
        v1, v2, v3 = _save("v1"), _save("v2"), _save("v3")
        SupersessionProtocol.invalidate(v1, superseded_by=v2, at=LATER)
        SupersessionProtocol.invalidate(v2, superseded_by=v3, at=LATER + 1)
        v2.delete()
        assert [r.name for r in SupersessionProtocol.chain(v1)] == ["v1"]
        assert [r.name for r in SupersessionProtocol.chain(v3)] == ["v3"]
        assert SupersessionProtocol.superseded_by(v1) is None


# -- #588: a successor saved in the same unit of work ---------------------------


class _Unit:
    """One unit of work on either leg: a redis-py pipeline executed on exit,
    or a Postgres ``transaction()``."""

    def __init__(self, model):
        self.backend = non_redis_backend(model)

    def __enter__(self):
        if self.backend is None:
            self.pipe = get_REDIS_DB().pipeline()
            return self.pipe
        self.cm = self.backend.transaction()
        return self.cm.__enter__()

    def __exit__(self, exc_type, exc, tb):
        if self.backend is None:
            if exc_type is None:
                self.pipe.execute()
            else:
                self.pipe.reset()
            return False
        return self.cm.__exit__(exc_type, exc, tb)


class TestSameTransactionSuccessor:
    def test_a_successor_saved_in_the_same_unit_closes_the_incumbent(self):
        old = _save("old")
        new = ParityClaim(name="new")
        with _Unit(ParityClaim) as unit:
            new.save(pipeline=unit)
            SupersessionProtocol.invalidate(old, superseded_by=new, pipeline=unit)
        assert _interval(old)[1] != INF
        assert _links(old) == (_key(new), None)
        assert _links(new) == (None, _key(old))
        assert _names(ParityClaim.query.filter(validity__current=True)) == ["new"]

    def test_a_successor_not_in_the_unit_is_refused_and_nothing_applies(self):
        old = _save("old")
        with pytest.raises(Exception) as info:
            with _Unit(ParityClaim) as unit:
                _save("bystander").save(pipeline=unit)
                SupersessionProtocol.invalidate(
                    old, superseded_by=ParityClaim(name="ghost"), pipeline=unit
                )
        assert isinstance(_typed(info.value), ValidityMemberAbsentError)
        assert _interval(old)[1] == INF
        assert _links(old) == (None, None)


# -- the crossing chains (#750 B1) ------------------------------------------------


class TestCrossingChains:
    """``d1 -> X`` superseded by ``Y`` while ``d2 -> Y`` is superseded by
    ``X``: two pointer-resolved incumbents, each the other's successor. Redis
    runs the two scripts one after the other. The POC's Postgres function let
    both in at once and one died with ``DeadlockDetected``; here the ``(model,
    field)`` advisory lock and the ``_pk``-ordered row locks serialise them.

    On Postgres the interleaving is forced: a probe connection holds both
    incumbents' rows ``FOR UPDATE``, both writers are started and observed
    blocked in ``pg_stat_activity``, then the rows are released together.
    Redis's single thread cannot interleave, so its leg races the two calls.
    """

    def _outcome(self, x, y, results, at):
        assert sorted(results) == sorted([_key(x), _key(y)]), results
        assert _interval(x)[1] == at and _interval(y)[1] == at
        assert _links(x) == (_key(y), _key(y))
        assert _links(y) == (_key(x), _key(x))
        d1, d2 = _pointer("1" * 16), _pointer("2" * 16)
        assert d1 == d2 and d1 in (_key(x), _key(y))

    def _run(self, calls, *, hold=()):
        backend = non_redis_backend(ParityClaim)
        results = [None] * len(calls)
        errors = []
        probe = None
        if backend is not None and hold:
            import psycopg

            probe = psycopg.connect(backend.dsn)
            table = backend._table(ParityClaim._meta.spec).qualified
            probe.execute(
                f'SELECT 1 FROM {table} WHERE "_pk" = ANY(%s) FOR UPDATE',
                ([_key(r) for r in hold],),
            )
        barrier = threading.Barrier(len(calls))

        def run(i, call):
            try:
                if probe is None:
                    barrier.wait(timeout=10)
                results[i] = call()
            except BaseException as e:  # pragma: no cover - surfaced below
                errors.append(e)

        threads = [
            threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)
        ]
        try:
            for i, thread in enumerate(threads):
                thread.start()
                if probe is not None:
                    self._wait_for_blocked(probe, i + 1)
            if probe is not None:
                probe.rollback()
            for thread in threads:
                thread.join(timeout=30)
        finally:
            if probe is not None:
                probe.close()
        assert not errors, errors
        assert not any(t.is_alive() for t in threads)
        return results

    @staticmethod
    def _wait_for_blocked(probe, count):
        deadline = time.monotonic() + 10.0
        rows = []
        while time.monotonic() < deadline:
            probe.execute("SELECT pg_stat_clear_snapshot()")
            rows = probe.execute(
                "SELECT wait_event_type, query FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND datname = current_database()"
            ).fetchall()
            blocked = [
                r
                for r in rows
                if r[0] == "Lock"
                and ("pg_advisory_xact_lock" in (r[1] or "") or "FOR UPDATE" in (r[1] or ""))
            ]
            if len(blocked) >= count:
                return
            time.sleep(0.02)
        raise AssertionError(f"{count} writer(s) never blocked; activity: {rows}")

    def test_crossing_pointer_chains_both_complete_ten_times(self):
        backend = non_redis_backend(ParityClaim)
        for i in range(10):
            x, y = _save(f"x{i}"), _save(f"y{i}")
            for record, digest in ((x, "1" * 16), (y, "2" * 16)):
                ValidityField.execute_supersede(
                    ParityClaim, FIELD, new_member=_key(record), identity_digest=digest
                )
            at = time.time()
            calls = [
                lambda y=y, at=at: SupersessionProtocol.supersede(
                    y, identity_key="1" * 16, at=at
                ),
                lambda x=x, at=at: SupersessionProtocol.supersede(
                    x, identity_key="2" * 16, at=at
                ),
            ]
            results = self._run(calls, hold=(x, y) if backend is not None else ())
            self._outcome(x, y, results, at)
