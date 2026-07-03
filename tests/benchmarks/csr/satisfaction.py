"""Assertion engine for the deterministic CSR harness (issue #418).

Pure, deterministic predicates over an ordered ``list[planted_id]`` plus a
``planted_meta`` dict (``id -> {"timestamp": float, "topic": str, ...}``).
No Redis, no randomness, no wall clock — ``evaluate()`` on the same inputs
returns the same bool every run.

Assertion vocabulary (the five types named by #418 — deliberately closed;
see the plan's Rabbit Holes for why no combinators/regex/score predicates):

- :class:`InTopK` — a memory id appears within the first ``k`` ranked ids.
- :class:`RanksAbove` — one id precedes another (both must appear).
- :class:`NoneOlderThan` — no id in the top-k is older than a cutoff.
- :class:`CoversTopic` — every planted id with a topic appears in the top-k.
- :class:`Excludes` — a memory id does NOT appear in the top-k.

Edge semantics (pinned by ``test_satisfaction.py``):

- A referenced id missing from the ranked list fails ``InTopK``/``RanksAbove``
  deterministically (returns ``False``, never raises ``KeyError``).
- Empty ``ranked_ids``: ``InTopK``/``RanksAbove``/``CoversTopic`` -> ``False``
  (``CoversTopic`` vacuously ``True`` only when no planted id has the topic);
  ``Excludes``/``NoneOlderThan`` -> ``True`` (vacuously).
- ``k <= 0`` or ``k`` larger than the ranked list clamps to the available
  list length.
"""

from dataclasses import dataclass

from . import DEFAULT_TOP_K


def _effective_k(k, ranked_ids):
    """Clamp ``k`` to the available list length.

    ``k <= 0`` and ``k > len(ranked_ids)`` both clamp to ``len(ranked_ids)``
    (the whole list). This keeps boundary values deterministic instead of
    letting ``k=0`` silently produce an empty slice.
    """
    n = len(ranked_ids)
    if k <= 0 or k > n:
        return n
    return k


def _top_k(ranked_ids, k):
    """Return the top-k slice of ``ranked_ids`` under clamped-k semantics."""
    return ranked_ids[: _effective_k(k, ranked_ids)]


@dataclass(frozen=True)
class InTopK:
    """Pass iff ``memory_id`` appears within the first ``k`` ranked ids.

    Fails (``False``) when the id is absent from the ranked list entirely —
    a missing id is a deterministic failure, never an error.
    """

    memory_id: str
    k: int = DEFAULT_TOP_K

    def evaluate(self, ranked_ids, planted_meta):
        return self.memory_id in _top_k(ranked_ids, self.k)


@dataclass(frozen=True)
class RanksAbove:
    """Pass iff ``higher_id`` precedes ``lower_id`` in the ranked list.

    Both ids must appear in the ranked list; either one missing fails the
    assertion deterministically (returns ``False``, never ``KeyError``/
    ``ValueError``).
    """

    higher_id: str
    lower_id: str

    def evaluate(self, ranked_ids, planted_meta):
        if self.higher_id not in ranked_ids or self.lower_id not in ranked_ids:
            return False
        return ranked_ids.index(self.higher_id) < ranked_ids.index(self.lower_id)


@dataclass(frozen=True)
class NoneOlderThan:
    """Pass iff no id in the top-k has a planted timestamp older than cutoff.

    Timestamps are read from ``planted_meta`` (planted fixture values, never
    wall clock). An empty top-k passes vacuously. A ranked id absent from
    ``planted_meta`` cannot prove age and is skipped.
    """

    timestamp: float
    k: int = DEFAULT_TOP_K

    def evaluate(self, ranked_ids, planted_meta):
        for memory_id in _top_k(ranked_ids, self.k):
            meta = planted_meta.get(memory_id)
            if meta is None:
                continue
            if meta["timestamp"] < self.timestamp:
                return False
        return True


@dataclass(frozen=True)
class CoversTopic:
    """Pass iff every planted id with ``topic`` appears in the top-k.

    The planted ids carrying the topic are read from ``planted_meta``. If no
    planted id has the topic, the assertion passes vacuously (there is
    nothing to cover). An empty ranked list fails whenever at least one
    planted id carries the topic.
    """

    topic: str
    k: int = DEFAULT_TOP_K

    def evaluate(self, ranked_ids, planted_meta):
        topic_ids = {
            memory_id
            for memory_id, meta in planted_meta.items()
            if meta.get("topic") == self.topic
        }
        return topic_ids.issubset(set(_top_k(ranked_ids, self.k)))


@dataclass(frozen=True)
class Excludes:
    """Pass iff ``memory_id`` does NOT appear in the top-k.

    An empty ranked list passes vacuously (nothing is included, so
    everything is excluded).
    """

    memory_id: str
    k: int = DEFAULT_TOP_K

    def evaluate(self, ranked_ids, planted_meta):
        return self.memory_id not in _top_k(ranked_ids, self.k)


def evaluate_all(assertions, ranked_ids, planted_meta):
    """Evaluate every assertion against one ranked list.

    Args:
        assertions: List of assertion dataclass instances.
        ranked_ids: Ordered list of planted ids (best first).
        planted_meta: Dict ``id -> {"timestamp": ..., "topic": ..., ...}``.

    Returns:
        Tuple ``(passed, total, per_assertion)`` where ``per_assertion`` is a
        list of ``{"assertion": repr, "type": class name, "passed": bool}``
        dicts in assertion order.
    """
    per_assertion = []
    passed = 0
    for assertion in assertions:
        ok = bool(assertion.evaluate(ranked_ids, planted_meta))
        passed += ok
        per_assertion.append(
            {
                "assertion": repr(assertion),
                "type": type(assertion).__name__,
                "passed": ok,
            }
        )
    return passed, len(assertions), per_assertion
