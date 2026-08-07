"""Canonical string forms for values that become Redis key bytes.

Popoto derives a row's identity from its KeyField values: ``DB_key`` joins
them with colons and that string *is* the Redis key. Historically each value
was rendered with ``str()``, which works for ``str``/``int``/``Decimal`` but
is unstable for ``datetime``: ``str()`` carries the UTC offset when the value
is aware and omits it when the value is naive, so identity depended on how the
value happened to decode rather than on the instant it denotes.

That instability is the root of two defects (#537, #538). Before 1.8.2 an
aware value reloaded naive, so a re-save derived a *different* key, wrote a
second hash and orphaned the first. And it blocked #521's proposed
"assume UTC for legacy offset-free rows" change, because stamping an offset on
read shifts ``str()`` and therefore shifts the key.

``canonical_key_str`` fixes the projection nobody had touched. Identity becomes
a function of the instant; the *stored value* still keeps the caller's exact
offset (#521), because key and value are deliberately different projections.

Dispatch is on ``datetime.datetime`` only. Every other type -- including
``datetime.date`` and ``datetime.time`` -- returns ``str(value)``, byte-for-byte
what 1.8.2 produced. See :func:`canonical_key_str` for why the two siblings are
excluded rather than deferred.
"""

import datetime

from ..fields.constants import Defaults

#: Fixed-width UTC rendering. ``%f`` always emits six digits, unlike
#: ``isoformat()``, which drops the fractional part entirely when microseconds
#: are zero. Fixed width keeps lexicographic key order equal to chronological
#: order, which matters for ``scan_keys`` patterns and for anyone reading
#: ``redis-cli --scan`` output.
CANONICAL_DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"

#: Appended to mark the rendering as UTC. Also what makes the canonical shape
#: distinguishable from both legacy shapes by inspection alone: legacy keys are
#: space-separated (``2026-08-07 12:00:00.123456``, with or without a trailing
#: ``+00:00``), canonical keys are ``T``-separated and ``Z``-terminated. The
#: audit in ``datetime_key_migration`` classifies stored keys on this alone,
#: without loading a single hash.
CANONICAL_UTC_SUFFIX = "Z"


def canonical_key_str(value) -> str:
    """Render *value* as the bytes it contributes to a Redis key.

    For a ``datetime.datetime``: normalize to UTC and render
    ``%Y-%m-%dT%H:%M:%S.%f`` plus a literal ``Z``. An aware value is converted
    with ``astimezone``; a naive value is *assumed UTC* and converted with
    ``replace(tzinfo=utc)``, which is the same doctrine ``convert_to_numeric``
    has applied to sorted-set scores since #519 and that ``auto_now`` has
    applied to stamping since #421. Consequence, and it is the point: an aware
    ``12:00+07:00``, its UTC equivalent ``05:00+00:00``, and the naive
    ``05:00`` all render identically and therefore address one row.

    For every other value: ``str(value)``, byte-identical to 1.8.2. ``None``
    renders ``"None"``, which ``Model.db_key`` and ``_has_unstable_db_key``
    both depend on.

    ``datetime.date`` has no offset to lose. ``datetime.time`` is excluded for
    PR #532's reason: a naive ``time.isoformat()`` with microseconds is
    byte-identical to the legacy ``%H:%M:%S.%f`` form, so a legacy time cannot
    be told apart from a deliberately naive one, and an *aware* time's ``str()``
    already carries its offset stably. Neither type has the bug, so neither is
    a deferral.

    This runs strictly *before* ``DB_key.clean()``. The canonical form contains
    colons and hyphens, which ``clean()`` already escapes (#525); the two
    compose and this function must never be taught to escape anything itself.

    Setting ``POPOTO_DATETIME_KEY_LEGACY=1`` restores 1.8.2's ``str(value)``
    for datetimes without a model-code edit, so an adopter can roll readers
    forward before moving key bytes. See the migration cookbook, recipe 19.
    """
    if isinstance(value, datetime.datetime) and not Defaults.DATETIME_KEY_LEGACY:
        if value.tzinfo is None:
            as_utc = value.replace(tzinfo=datetime.timezone.utc)
        else:
            as_utc = value.astimezone(datetime.timezone.utc)
        return as_utc.strftime(CANONICAL_DATETIME_FORMAT) + CANONICAL_UTC_SUFFIX
    return str(value)
