#!/usr/bin/env python
"""Record the exact Redis wire traffic of the validity surface (#759 M3).

``scripts/trace_redis_wire.py`` traces the M1 model/query surface; this
script reuses its RESP hooks, frozen clock and ``uuid4`` and normaliser, and
traces what M3 touched on the Redis side: ``ValidityField`` (save, the
declared-``valid_from`` refusal, ``execute_supersede`` in every mode, the
resolvers, ``filter(validity__*)``, ``export_state``/``import_state``, the
pointer scan, delete) and ``SupersessionProtocol`` (``supersede``,
``invalidate``, ``save_and_*`` on owned and caller pipelines, ``chain`` and
its one-hop walks, every typed error), the gated ``top_by_decay`` and
``composite_score``, and ``ObservationProtocol``'s ``contradicted``
supersession. M3 routes each of those through ``non_redis_backend`` first,
which must issue no Redis command; this trace is how that is proved.

Usage -- trace a base tree and the working tree with *this* script, compare::

    git archive origin/main src | tar -x -C /tmp/base
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 PYTHONPATH=/tmp/base/src \\
        python scripts/trace_validity_wire.py > base.trace
    REDIS_URL=redis://localhost:6379/11 PYTHONHASHSEED=0 \\
        python scripts/trace_validity_wire.py > head.trace
    cmp base.trace head.trace

Refuses database 0, like the script it builds on.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import trace_redis_wire as wire  # noqa: E402  (binds REDIS_URL, hooks, clock)

import popoto  # noqa: E402
from popoto import (  # noqa: E402
    ConfidenceField,
    DecayingSortedField,
    ObservationProtocol,
    SupersessionProtocol,
    ValidityField,
)
from popoto.redis_db import get_REDIS_DB  # noqa: E402

T0 = 1_760_000_000.0


class TrFact(popoto.Model):
    name = popoto.KeyField()
    label = popoto.IndexedField(type=str, default="")
    importance = popoto.FloatField(default=1.0)
    relevance = DecayingSortedField(base_score_field="importance")
    certainty = ConfidenceField()
    validity = ValidityField()


MODELS = (TrFact,)
SCENARIOS: list[tuple[str, Callable[[], Any]]] = []


def scenario(fn: Callable[[], Any]) -> Callable[[], Any]:
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _clear() -> None:
    client = get_REDIS_DB()
    for model in MODELS:
        for key in client.scan_iter(match=f"*{model.__name__}*", count=1000):
            client.delete(key)


def _fact(name: str, **kwargs: Any) -> TrFact:
    record = TrFact(name=name, **kwargs)
    record.save()
    return record


def _get(name: str) -> Any:
    return TrFact.query.get(name=name)


IDENTITY = SupersessionProtocol.identity_key("user_42", "plan")


@scenario
def save_opens_an_interval():
    return [_fact("v1"), _fact("v2", validity=T0 - 50.0)]


@scenario
def resave_and_declared_conflict():
    record = _get("v2")
    record.importance = 2.0
    first = record.save()
    record.validity = T0 - 99.0
    return [first, record.save()]


@scenario
def supersede_by_identity():
    return [
        SupersessionProtocol.supersede(_get("v1"), identity_key=IDENTITY),
        SupersessionProtocol.supersede(_fact("v3"), identity_key=IDENTITY),
        SupersessionProtocol.supersede(
            _fact("v4"), identity_key=("user_42", "plan"), at=T0
        ),
    ]


@scenario
def supersede_errors():
    out = []
    for fn in (
        lambda: SupersessionProtocol.supersede(
            TrFact(name="ghost"), identity_key=IDENTITY
        ),
        lambda: SupersessionProtocol.invalidate(_get("v4"), at=T0 - 1e6),
        lambda: ValidityField.execute_supersede(
            TrFact,
            "validity",
            new_member="TrFact:v4",
            mode="invalidate",
            old_member="TrFact:gone",
        ),
        lambda: ValidityField.execute_supersede(
            TrFact,
            "validity",
            new_member="TrFact:v4",
            mode="open",
            valid_from=1.5,
            assert_valid_from=True,
        ),
        lambda: ValidityField.execute_supersede(TrFact, "validity", mode="bogus"),
    ):
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001 - recorded
            out.append(f"{type(exc).__name__}: {exc}")
    return out


@scenario
def invalidate_shapes():
    old, new = _fact("i1"), _fact("i2")
    return [
        SupersessionProtocol.invalidate(old, superseded_by=new, at=T0),
        SupersessionProtocol.invalidate(old),
        SupersessionProtocol.invalidate(new),
    ]


@scenario
def execute_supersede_on_a_pipeline():
    pipe = get_REDIS_DB().pipeline()
    result = ValidityField.execute_supersede(
        TrFact,
        "validity",
        new_member="TrFact:v3",
        mode="supersede",
        old_member="TrFact:v2",
        close_at=T0,
        pipeline=pipe,
    )
    return [result is pipe, pipe.execute()]


@scenario
def save_and_supersede_shapes():
    owned = SupersessionProtocol.save_and_supersede(
        TrFact(name="s1"), identity_key=IDENTITY, at=T0
    )
    pipe = get_REDIS_DB().pipeline()
    queued = SupersessionProtocol.save_and_supersede(
        TrFact(name="s2"), identity_key=IDENTITY, pipeline=pipe
    )
    executed = pipe.execute()
    closing = SupersessionProtocol.save_and_invalidate(
        TrFact(name="s3"), closes=_get("s2"), at=T0
    )
    try:
        refused = SupersessionProtocol.save_and_invalidate(
            TrFact(name="s4"), closes=_get("s3"), at=T0 - 1e6
        )
    except Exception as exc:  # noqa: BLE001 - recorded
        refused = f"{type(exc).__name__}: {exc}"
    return [
        owned.closed_key,
        queued.closed_key,
        queued.close_index,
        executed,
        closing.closed_key,
        refused,
    ]


@scenario
def chain_and_walks():
    out = []
    for name in ("v1", "v3", "s3", "i2"):
        record = _get(name)
        out.append(
            [
                [r.name for r in SupersessionProtocol.chain(record)],
                getattr(SupersessionProtocol.superseded_by(record), "name", None),
                getattr(SupersessionProtocol.supersedes(record), "name", None),
            ]
        )
    out.append(SupersessionProtocol.chain(TrFact(name="unsaved")))
    return out


@scenario
def resolvers_and_reads():
    member = "TrFact:v3"
    return [
        ValidityField.resolve_excluded_keys(TrFact, "validity"),
        ValidityField.resolve_excluded_keys(TrFact, "validity", as_of=T0 - 10.0),
        ValidityField.resolve_valid_keys(TrFact, "validity"),
        ValidityField.resolve_valid_keys(TrFact, "validity", as_of=float("inf")),
        ValidityField.is_valid_at(TrFact, "validity", member),
        ValidityField.get_valid_from(TrFact, "validity", member),
        ValidityField.find_open_pointers_for_member(TrFact, "validity", "TrFact:s3"),
        ValidityField.filter_query(TrFact, "validity", validity__current=True),
    ]


@scenario
def validity_filters():
    out = [
        sorted(r.name for r in TrFact.query.filter(validity__current=True)),
        sorted(r.name for r in TrFact.query.filter(validity__current=False)),
        sorted(r.name for r in TrFact.query.filter(validity__as_of=T0 - 10.0)),
    ]
    try:
        out.append(TrFact.query.filter(validity__current="yes").all())
    except ValueError as exc:
        out.append(f"ValueError: {exc}")
    return out


@scenario
def gated_rankings():
    return [
        TrFact.query.top_by_decay(n=10),
        TrFact.query.top_by_decay(n=10, as_of=T0 - 10.0),
        TrFact.query.composite_score({"relevance": 0.5, "certainty": 0.5}, limit=10),
        TrFact.query.composite_score(
            {"relevance": 1.0}, limit=10, as_of=T0 - 10.0
        ),
    ]


@scenario
def export_import_state():
    record = _get("s3")
    state = ValidityField.export_state(record, "validity", None)
    ValidityField.import_state(record, "validity", state)
    return state


@scenario
def contradicted_supersession():
    stale, fresh = _fact("o1"), _fact("o2")
    stale._superseded_by = fresh
    ObservationProtocol.on_context_used(
        [stale], {stale.db_key.redis_key: "contradicted"}
    )
    ghost_target = _fact("o3")
    ghost_target._superseded_by = TrFact(name="o-ghost")
    ObservationProtocol.on_context_used(
        [ghost_target], {ghost_target.db_key.redis_key: "contradicted"}
    )
    return [
        ValidityField.is_valid_at(TrFact, "validity", "TrFact:o1"),
        ValidityField.is_valid_at(TrFact, "validity", "TrFact:o3"),
    ]


@scenario
def delete_with_pointers():
    return [_get("s3").delete(), _get("v1").delete(), TrFact.delete_all()]


def main() -> None:
    _clear()
    out = sys.stdout
    for name, fn in SCENARIOS:
        wire._TRACE = []
        try:
            result = wire._norm(fn())
        except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
            result = f"!! {type(exc).__name__}: {exc}"
        commands, wire._TRACE = wire._TRACE, None
        out.write(f"=== {name} ({len(commands)} commands)\n")
        for argv in commands:
            out.write("  " + " ".join(repr(tok) for tok in argv) + "\n")
        out.write(f"  -> {result}\n")
    _clear()


if __name__ == "__main__":
    main()
