"""Label-blind supersession producer for the external benchmark ingest arm (#692).

The external harness (``external_base.py``) has, since it existed, opened every
memory record and never closed one: every write is a plain ``.save()``, which
``ValidityField.on_save`` (mode ``"open"``) registers with ``valid_from =
save_time`` and ``invalid_at = +inf``. With nothing ever calling
``supersede()``/``invalidate()``, V0's validity-gating exclusion set
(``ValidityField.resolve_excluded_keys``) is structurally always empty, so
gating is a subtractive no-op on this harness by construction (#580 shipped no
producer; `[EXTERNAL]` was an explicit deferral). This module is the first
producer: it wires two shipped popoto primitives, ``ValidityField`` and
``SupersessionProtocol``, into the harness so the gate has something to
subtract.

The design decision this module encodes (docs/plans/sdlc-692.md has the full
three-step argument): **the producer is label-blind.** The benchmark's scored
category (``question_type``, in particular ``"knowledge-update"``) and its
answer key (``relevant_ids``) are never inputs. ``identity_of`` takes exactly
one positional parameter — text — so neither can reach it, mirroring #514's
``collapse_to_ranking_unit`` gold-blindness construction. Driving the producer
on the scored label would make the resulting exclusion-set delta characterize
the annotation, not the system; it would also buy nothing, because
``question_type`` is annotated per *question*, not per *turn*, and does not
identify which of two sessions is the stale one.

Instead the producer uses a real ingest-visible signal the harness previously
discarded: LongMemEval-S's ``haystack_dates``, parsed into per-turn
``session_date`` by ``datasets/longmemeval_s.py``. That is exactly the signal
a deployed agent memory system has (every write carries a time) and it is
orthogonal to what the benchmark scores.

The identity rule below is the whole heuristic and the whole risk. It is
deliberately conservative (high precision, low recall): the gate is
subtractive, so a false-positive identity match deletes a live record and can
only cost recall, while under-matching just yields a smaller, still-honest
number. See docs/plans/sdlc-692.md for the full risk analysis.
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from src.popoto.fields.supersession import SupersedeDeclinedError
from src.popoto.fields.validity_field import (
    ValidityCloseBeforeStartError,
    ValidityMemberAbsentError,
)

logger = logging.getLogger("POPOTO.Benchmark.Supersession")

ARM_CHOICES = ("none", "content-identity")
"""Selectable ingest arms. ``none`` is byte-identical to the committed
baseline: no ``ValidityField`` is declared and no protocol call is ever made.
"""

# Pinned magic numbers in the CLAUDE.md sense — experimental tuning knobs for
# this harness-local heuristic, not library configuration. They intentionally
# do NOT live in ``popoto.fields.constants.Defaults``: promoting them would
# leak a benchmark artifact into the library's public surface and would trip
# ``tests/benchmarks/test_defaults_sync.py``'s registration gate for no
# benefit to any actual popoto adopter.
_STATE_VERBS = frozenset(
    {
        "work",
        "worked",
        "live",
        "lived",
        "moved",
        "own",
        "owned",
        "have",
        "had",
        "am",
        "was",
    }
)
_STATE_PREPOSITIONS = frozenset({"at", "in", "for", "on", "with"})

# First-person present/past state assertion: "I <verb> [<prep>]". Case-folded
# on the whole sentence before matching, so "I" and "i" are equivalent.
_IDENTITY_RE = re.compile(
    r"^i\s+(?P<verb>"
    + "|".join(sorted(_STATE_VERBS, key=len, reverse=True))
    + r")\b(?:\s+(?P<prep>"
    + "|".join(sorted(_STATE_PREPOSITIONS, key=len, reverse=True))
    + r")\b)?"
)

# Pinned stdlib-only sentence splitter — no new dependency. Splits on
# sentence-ending punctuation followed by whitespace; deliberately simple,
# since only the FIRST matching sentence of a unit is ever consulted.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

IdentityKey = Tuple[str, str]


def identity_of(unit_text: str) -> Optional[IdentityKey]:
    """Return a ``(subject, predicate)`` identity for ``unit_text``, or ``None``.

    Label-blind by construction: this function's signature is text and
    nothing else. The caller holds the answer key and ``question_type``; this
    signature makes it impossible for either to arrive here — the same
    construction #514's ``collapse_to_ranking_unit`` uses for the answer key.

    Rule (deliberately high-precision / low-recall): split ``unit_text`` into
    sentences with a pinned stdlib-only splitter. Take the FIRST sentence
    matching a leading first-person state-verb assertion (optionally followed
    by a pinned preposition) — at most one identity per unit, so a unit can
    join at most one supersession group. Everything else returns ``None``.

    The subject is always the literal ``"i"``: per-item namespaces are already
    agent-scoped (one ``agent_id`` per benchmark item), so no cross-speaker
    collision is possible within an item.

    Args:
        unit_text: The written unit's text (a turn, or an extracted fact).

    Returns:
        ``("i", "<verb>")`` or ``("i", "<verb>_<prep>")``, or ``None``.
    """
    if not unit_text or not unit_text.strip():
        return None
    for sentence in _SENTENCE_SPLIT_RE.split(unit_text.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        match = _IDENTITY_RE.match(sentence.casefold())
        if match is None:
            continue
        verb = match.group("verb")
        prep = match.group("prep")
        predicate = f"{verb}_{prep}" if prep else verb
        return ("i", predicate)
    return None


@dataclass
class SupersessionStats:
    """Per-run accounting for the ``content-identity`` producer (#692).

    ``producer_failures`` is printed in the aggregate report even when zero
    (Verification row 10) so a reader can distinguish "producer found
    nothing" from "producer errored on everything".
    """

    units_seen: int = 0
    units_with_identity: int = 0
    identity_groups: int = 0
    plain_writes: int = 0
    identity_writes: int = 0
    supersessions: int = 0
    failures: int = 0

    def to_dict(self) -> dict:
        return {
            "units_seen": self.units_seen,
            "units_with_identity": self.units_with_identity,
            "identity_groups": self.identity_groups,
            "plain_writes": self.plain_writes,
            "identity_writes": self.identity_writes,
            "n_supersessions": self.supersessions,
            "producer_failures": self.failures,
        }


def route_write(
    instance: Any,
    *,
    identity: Optional[IdentityKey],
    at: Optional[float],
    stats: "SupersessionStats",
) -> bool:
    """Write ``instance``, routing identity-bearing units through supersession.

    ``identity is None`` -> plain ``.save()``, never enters the validity axis.
    Otherwise every identity-bearing write — including the FIRST claim of a
    group — goes through ``SupersessionProtocol.save_and_supersede``. There is
    no branch on "is this the first": a plain ``.save()`` never registers an
    incumbent, so a first-claim-plain / second-claim-supersede sequence would
    close nothing and reproduce the exact defect #692 exists to fix, one level
    down (spike-2's most load-bearing finding). ``closed_key is None`` on the
    first call of a group is the expected, counted outcome.

    The ``try``/``except`` lives HERE, in the function that owns ``stats``, on
    purpose (critique R2-C2): if an exception is allowed to escape to the
    ingest loop's pre-existing handler, ``producer_failures`` would read 0
    under total producer failure, reintroducing exactly the
    "silently-failing producer masquerading as a producer that found nothing"
    defect this metric exists to prevent.

    Args:
        instance: The unsaved model instance carrying the new claim.
        identity: The identity key from ``identity_of``, or ``None``.
        at: The session's date (epoch float), or ``None``. Must be the
            session's own date, never wall-clock ``time.time()`` — passing now
            would open-and-close every interval within the same run second.
        stats: The run's ``SupersessionStats`` accumulator.

    Returns:
        ``True`` if the write succeeded, ``False`` if it was declined or
        failed.
    """
    stats.units_seen += 1
    if identity is None:
        stats.plain_writes += 1
        return bool(instance.save())

    stats.units_with_identity += 1
    from src.popoto.fields.supersession import SupersessionProtocol

    try:
        result = SupersessionProtocol.save_and_supersede(
            instance, identity_key=identity, at=at
        )
    except (
        SupersedeDeclinedError,
        ValidityMemberAbsentError,
        ValidityCloseBeforeStartError,
    ) as e:
        stats.failures += 1
        logger.warning("supersession producer failed: %s", e)
        return False

    stats.identity_writes += 1
    if result.closed_key:
        stats.supersessions += 1
    return True
