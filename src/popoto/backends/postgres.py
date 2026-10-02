"""Postgres implementation of the storage backend seam (#631).

WS3a implements family B (records), C (atomic increment) and J's
``purge_orphan``, plus the unit of work they need. WS3b implements D (side
maps), E (set indexes), F (sorted indexes) and the rest of J
(``scan_index_members``, ``drop_index``). WS3c implements G (the atomic
index and tag swaps). WS3e implements I (validity intervals and
supersession). Every other method still raises
:class:`NotImplementedError` naming itself; WS3d fills them in family by
family behind the WS2 conformance harness.

``psycopg`` is deliberately **not** imported at module scope: selection in
:func:`popoto.backends.get_backend` must be able to import this module without
the driver, and importing ``popoto`` must never dial Postgres. The import sits
inside :meth:`PostgresBackend._connection`, the one place a connection opens.

Schema
------
Generic tables (issue #631's POC decision), created with ``CREATE TABLE IF NOT
EXISTS`` on the first connection a backend instance opens, into whatever
schema the URL's ``search_path`` names (the conformance harness hands every
instance a ``popoto_test_<hex>`` schema that way; the harness refuses
``public`` before any statement runs). The block runs inside one transaction
under ``pg_advisory_xact_lock(631, hashtext(current_schema()))``, because
``IF NOT EXISTS`` is not race-safe: two sessions that both find a table
absent both try to create it, and the loser fails on the catalog's unique
index (``UniqueViolation: pg_type_typname_nsp_index``; PR #737 review). With
the lock, concurrent first connections queue and every later one finds the
tables there. The two-argument lock form is a separate key space from the
one-argument ``hashtext(key)`` locks the record writers take, so no record
key can collide with it. Should the block fail, the connection it ran on is
closed before the error propagates, so the next call retries from scratch.

* ``popoto_record(key, field bytea, value bytea)`` -- **one row per field**,
  not the issue's ``data jsonb``: the field layer hands :meth:`save_record`
  per-field *msgpack bytes* keyed by *bytes* field names (plan finding 2, WS0
  deviation 1), and ``jsonb`` cannot hold either. Per-field rows give the
  ``HSET`` merge semantics for free -- a save touches only the fields it names
  -- so :meth:`save_record` is one ``INSERT ... ON CONFLICT DO UPDATE`` over
  ``unnest`` and :meth:`load_record` is one ``SELECT``. ``field`` is ``bytea``
  rather than ``text`` so a name containing ``\\x00`` (popoto's legacy
  in-hash pointer fields) round-trips and ``HGETALL``'s ``bytes`` keys come
  back as they went in.
* ``popoto_numeric(key, field text, value double precision)`` -- the numeric
  side-map (architect decision 2): the decoded value of every numeric field,
  written from :meth:`save_record`'s ``numeric`` argument and refreshed by
  :meth:`increment_field`, so WS3d's ``decayed_rank`` can ``ORDER BY`` a typed
  column instead of a msgpack blob.
* ``popoto_set(idx, member)`` and ``popoto_sorted(idx, member, score)`` --
  the set and sorted indexes (families E and F, the class set
  :meth:`save_record` registers in, and what :meth:`purge_orphan` prunes).
  The one-``idx``-column layout is the plan's convention (an index name is
  opaque to the protocol, so the issue's ``(model, field)`` pair collapses
  into it). ``popoto_sorted`` carries a ``(idx, score)`` index for the range
  reads.
* ``popoto_map(idx, member, value bytea)`` -- the side maps (family D):
  composite unique indexes, confidence payloads and supersession chain links,
  one table for all three as the plan says.
* ``popoto_pointer(key, field, idx)`` -- which set index(es) a record is in
  for one field (family G): one row for an indexed/unique field, one per tag
  for a tag field. See "Atomic swaps".

Atomic swaps
------------
``INDEX_SWAP_LUA`` and ``TAG_SWAP_LUA`` each need to know which index a
record *was* in for a field before they can move it, and on Redis that is the
``$IdxPtr:`` / ``$TagPtr:`` side key. The plan's "the index row is the
pointer" holds only in reverse: ``popoto_set(idx, member)`` answers "who is
in this index", not "which index is this record in for this field", and an
index name is opaque to the protocol, so the reverse lookup needs a row of
its own -- ``popoto_pointer``, keyed ``(key, field)``. It is the side key as
a table, nothing more: never a field of ``popoto_record`` (the pre-#476
in-hash scheme the Lua scrubs), so :meth:`load_record` returns exactly what
``HGETALL`` does. Of the Lua's two migration fallbacks only the second has a
Postgres shape: no record written by this backend can carry a pre-#540 side
key, but a record *imported* with the pre-#476 ``{field}\\x00idxset`` field
can, so :meth:`swap_index` adopts and scrubs it as the script does.

Each swap is one transaction whose statements follow the script's phases.
*Validation* -- the pointer read, the idempotent re-save check and the
uniqueness check -- is reads only, and a conflict raises ``ModelException``
with the Redis backend's exact wording before any write, so the transaction
(on the ``uow=`` path, the whole queue) rolls back untouched: the "validation
phase then mutation phase" comment in the Lua is a real rollback here.
*Mutation* -- leave the old index, join the new, repoint, write the field
bytes -- is all-or-nothing. The one visible difference is the migration
scrub: the Lua ``HDEL``\\s the adopted in-hash pointer before it checks
uniqueness, so on Redis a conflicting save still scrubs it, and here the
rollback keeps it for the next save to scrub.

A uniqueness check is a read-modify-write on rows that may not exist yet --
two instances claiming one value for two *different* records each read an
empty index, and ``FOR UPDATE`` has nothing to lock -- so :meth:`swap_index`
with ``unique=True`` takes the advisory lock on the target index as well as
on the record key (both through :func:`_lock_record_keys`, so in one global
order). The second claimant then waits, re-reads under the lock, sees the
first's committed row and raises: exactly one success and one conflict, as
Redis's single thread guarantees. The remaining swaps lock the record key
only, which serialises them against :meth:`save_record` / :meth:`delete_record`
on the same record.

Sorted-set semantics on a table
-------------------------------
Redis orders a sorted set by score and breaks ties by member, comparing the
member *bytes* (``memcmp``), in both directions. Postgres's default collation
is locale-aware and does not agree (``'B'`` sorts between ``'a'`` and ``'b'``
in ``en_US``), so every ordered read says ``ORDER BY score, member COLLATE
"C"`` -- byte order for UTF-8 text -- and the reverse reads flip both keys.
The ``(`` exclusive-bound strings and ``-inf``/``+inf`` the Redis backend
renders (WS0 deviation 12) have no Postgres rendering at all: bounds are
compared as ``double precision``, which holds ``±Infinity`` natively, with
``>=``/``>`` and ``<=``/``<`` chosen by the inclusivity flags. ``ZRANGE``'s
index arithmetic (negative indices count from the end, the stop is
inclusive, an out-of-range window is empty) is done in SQL over a
``row_number()`` window so one statement sees one snapshot. ``ZADD``'s reply
-- 1 for a new member, 0 for a score update -- is the upsert's
``RETURNING (xmax = 0)``; ``ZINCRBY`` is one ``ON CONFLICT DO UPDATE SET
score = score + delta``, which locks the row it updates, so no advisory
lock is needed for any index operation: each is one statement. A ``NaN``
score (``ZADD``) or a ``NaN`` result (``ZINCRBY`` of ``inf`` by ``-inf``)
raises ``ValueError`` with Redis's wording where Redis replies with an
error; the increment's transaction rolls the row back.

Scans (``scan_index_names``, ``scan_record_keys``, ``map_scan``) translate the
Redis glob to a POSIX regular expression (:func:`_glob_to_regex`) and push it
down as ``~``. On Redis ``SCAN MATCH`` sees every key of every type;
:meth:`scan_index_names` here enumerates the three index tables only, so a
*record* whose key happens to match an index glob is never returned (on Redis
it would be, and ``rebuild_indexes`` would ``DEL`` it). ``map_scan``'s
``count`` is ``HSCAN``'s batch hint and is ignored: there is no cursor.

Connections
-----------
One ``psycopg`` connection per backend instance, opened lazily on first use
in autocommit mode: every executed-now method runs inside its own
``conn.transaction()`` block, so a multi-statement operation such as
:meth:`save_record` is atomic the way its Redis ``MULTI`` pipeline is, and a
:class:`PostgresUnitOfWork` runs its whole queue inside one. The plan allows
the Postgres backend to hold a connection because there is no rebind protocol
to honour (``set_REDIS_DB_settings`` is a Redis concern). :meth:`close`
releases it; a ``weakref.finalize`` closes it when the instance is collected,
which is what keeps the conformance fixture -- a fresh instance per test,
never closed -- from leaking a connection per test. The connection is not
shared across threads: psycopg serialises calls on it, but two threads'
transactions would interleave.

Locking
-------
A transaction alone is not the Redis single thread: two *instances* (two
connections) can interleave at statement granularity, and a read-modify-write
on a row that does not exist yet has nothing for ``FOR UPDATE`` to lock. So
every operation that writes a record key -- :meth:`save_record`,
:meth:`delete_record`, :meth:`increment_field` and :meth:`purge_orphan` --
takes ``pg_advisory_xact_lock(hashtext(key))`` as its *first* statement
(:func:`_lock_record_keys`), held to the end of the transaction. On the
``uow=`` path that is the unit of work's transaction, taken at ``commit()``
rather than when the method was called. Without the lock on the writers, an
increment on an absent field could read "no row", lose to a concurrent
``save_record`` committing ``n = 100`` on another instance, and upsert
``0 + 1`` over it: a stored ``1`` that no serial order produces (PR #737
review, B1). The one operation that names two keys, a rename through
``obsolete_key``, locks both ascending by lock id so two instances renaming in
opposite directions take them in the same order.

Validity and supersession
-------------------------
On Redis a ``ValidityField`` is three ZSETs (``valid_from``, ``invalid_at``,
``ingested_at``; score ``+inf`` means "still open"), two HASHes (the chain
links) and one STRING per identity digest (the open pointer), all named from
the ``$ValidityF:<Model>`` prefix by ``_validity_keys`` (WS0 deviation 10,
imported from the Redis backend so the derivation is byte-equal by
construction). Here the intervals are rows of ``popoto_sorted`` under those
same three names and the links rows of ``popoto_map`` under the two chain
names -- **not** a dedicated interval table, because the field layer reads
them through the generic families: ``filter(validity__current=False)`` is
``sorted_members`` on the ``invalid_at`` / ``valid_from`` names and
``SupersessionProtocol.chain`` is ``map_get`` on the chain names (WS1e), so an
interval kept anywhere else would be invisible to both. The open pointers are
the one new table, ``popoto_open_ptr(prefix, digest, member)`` with an index
on ``(prefix, member)`` for :meth:`drop_validity`'s "every pointer naming this
member" delete (deviation 3). ``+inf`` is ``'infinity'::float8``, which
compares as the Lua's ``math.huge`` does: ``<= as_of`` is false for every
finite ``as_of`` and true for ``as_of = inf``.

:meth:`supersede` is one call of the PL/pgSQL function ``popoto_supersede``
(``SCHEMA_DDL``), ``SUPERSEDE_LUA`` phase for phase: resolve the incumbent
from the pointer, the ``#588`` membership guards, the idempotency and
close-before-start checks, the asserted-``valid_from`` check, then the close,
the two links, the NX open and the repoint. Each ``error_reply`` token is a
``RAISE EXCEPTION USING ERRCODE = 'P0631'`` whose ``MESSAGE`` is the same
token line, and the backend hands that line to ``validity_field``'s
``map_lua_error`` -- the same function the Redis backend calls -- so the
typed exception and its text are identical on both backends. Inside the
function the Redis single thread is ``pg_advisory_xact_lock`` on the pointer
key, the successor and the asserted incumbent (same key space and order as
the record locks) plus ``SELECT ... FOR UPDATE`` on the incumbent's
``invalid_at`` row, so two connections closing one incumbent serialise and
the second finds it closed, the idempotent no-op it is on Redis. On the
``uow=`` path the function runs inside the unit of work's transaction, which
is what makes the same-transaction successor of #588 visible to the guard
for free; it also means the *typed* exception is raised from ``commit()``
here, where the Redis pipeline surfaces a raw ``ResponseError`` for the
caller to remap. ``now`` is the caller's clock (deviation 2): the backend
fills every defaulted instant from it and never from ``clock_timestamp()``.

The atomic-increment envelope
-----------------------------
``ATOMIC_INCREMENT_LUA`` reads the field, ``cmsgpack.unpack``s it, adds the
delta in Lua's double arithmetic, re-packs and returns ``tostring(new)``.
:meth:`increment_field` reproduces that *bit for bit* so the two backends
store and return the same thing: Lua 5.1's ``tostring`` is ``%.14g``
(:func:`_lua_tostring`), its truthiness and ``tonumber`` rules decide what
counts as the current value (:func:`_current_value`), and ``cmsgpack`` packs
a Lua number as a msgpack integer when it is integral, else as a float32 when
that is lossless and a float64 otherwise (:func:`_cmsgpack_pack_number`). The
``Decimal`` envelope is the same tagged map the Lua builds,
``{"__Decimal__": True, "as_encodable": "%.14g"}``. Behaviour on an absent
record or field is the Lua's: the current value is ``0``, and the write
creates the field (and so the record) without touching any class set.
"""

from __future__ import annotations

import math
import re
import struct
import weakref
from decimal import Decimal
from typing import Any, Callable, Iterator, Literal, Mapping, Sequence

import msgpack

from ..exceptions import ModelException
from ..redis_db import ENCODING
from . import UnitOfWork
from .redis import _validity_keys

__all__ = ["PostgresBackend", "PostgresUnitOfWork", "SCHEMA_DDL"]

#: The two-argument advisory lock the DDL bootstrap takes, paired with
#: ``hashtext(current_schema())`` so bootstraps of different schemas never
#: queue on each other. A separate key space from the one-argument
#: ``hashtext(key)`` record locks (see "Schema" in the module docstring).
DDL_LOCK_CLASS = 631

#: Executed in order, once per connection, before any other statement.
SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS popoto_record (
        key   text  NOT NULL,
        field bytea NOT NULL,
        value bytea NOT NULL,
        PRIMARY KEY (key, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_numeric (
        key   text NOT NULL,
        field text NOT NULL,
        value double precision NOT NULL,
        PRIMARY KEY (key, field)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_set (
        idx    text NOT NULL,
        member text NOT NULL,
        PRIMARY KEY (idx, member)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_sorted (
        idx    text NOT NULL,
        member text NOT NULL,
        score  double precision NOT NULL,
        PRIMARY KEY (idx, member)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS popoto_sorted_idx_score
        ON popoto_sorted (idx, score)
    """,
    """
    CREATE TABLE IF NOT EXISTS popoto_map (
        idx    text  NOT NULL,
        member text  NOT NULL,
        value  bytea NOT NULL,
        PRIMARY KEY (idx, member)
    )
    """,
    # -- WS3c (family G, atomic swaps): the index pointer ----------------------
    # Which set index(es) a record currently belongs to for one field: one row
    # per (record, field) for an indexed/unique field, one per tag for a tag
    # field. The Postgres shape of the ``$IdxPtr:`` / ``$TagPtr:`` side keys
    # (see "Atomic swaps" in the module docstring).
    """
    CREATE TABLE IF NOT EXISTS popoto_pointer (
        key   text NOT NULL,
        field text NOT NULL,
        idx   text NOT NULL,
        PRIMARY KEY (key, field, idx)
    )
    """,
    # -- WS3e: validity and supersession (family I) ---------------------------
    # The validity intervals themselves live in ``popoto_sorted`` under the
    # three index names ``_validity_keys`` derives (``valid_from``,
    # ``invalid_at``, ``ingested_at``) and the chain links in ``popoto_map``
    # under the two chain names, exactly as they are three ZSETs and two
    # HASHes on Redis; see "Validity" in the module docstring for why a
    # dedicated interval table would break the field layer. The one new table
    # is the open-identity pointer (Redis: one STRING key per digest), and the
    # supersede itself is a PL/pgSQL function so its validation phase and
    # mutation phase are one statement inside the caller's transaction.
    """
    CREATE TABLE IF NOT EXISTS popoto_open_ptr (
        prefix text NOT NULL,
        digest text NOT NULL,
        member text NOT NULL,
        PRIMARY KEY (prefix, digest)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS popoto_open_ptr_prefix_member
        ON popoto_open_ptr (prefix, member)
    """,
    # ``SUPERSEDE_LUA`` (backends/redis.py) step by step. Every ``error_reply``
    # token becomes ``RAISE EXCEPTION USING ERRCODE = 'P0631'`` -- a custom
    # SQLSTATE in the PL/pgSQL class ``P0`` -- carrying the same token line as
    # ``MESSAGE`` (the two numbers of the valid-from conflict travel in
    # ``DETAIL`` so Python can render them with Lua's ``%.14g``), and
    # :meth:`PostgresBackend.supersede` hands that line to the field layer's
    # ``map_lua_error``, so ``_LUA_ERROR_MAP`` is the single source of the
    # token -> typed-exception mapping for both backends. Phase rule: no write
    # above the ``MUTATION PHASE`` comment, mirroring the Lua; here the
    # transaction would roll a torn write back anyway, but the ordering keeps
    # the two bodies reviewable side by side.
    """
    CREATE OR REPLACE FUNCTION popoto_supersede(
        vf_key      text,
        ia_key      text,
        ig_key      text,
        ptr_prefix  text,
        ptr_digest  text,
        fwd_key     text,
        rev_key     text,
        new_member  text,
        mode        text,
        old_member  text,
        valid_from  double precision,
        ingested_at double precision,
        close_at    double precision,
        vf_assert   boolean,
        lock_keys   text[]
    ) RETURNS text LANGUAGE plpgsql AS $popoto$
    DECLARE
        newm         text    := coalesce(new_member, '');
        oldm         text    := coalesce(old_member, '');
        -- old_member was supplied by the caller: an assertion, not a hint.
        asserted_old boolean := coalesce(old_member, '') <> '';
        will_close   boolean := false;
        closed       text    := '';
        pointed      text;
        old_ia       double precision;
        old_vf       double precision;
        stored_vf    double precision;
        new_ia       double precision;
    BEGIN
        -- The Redis single thread: every writer naming the same pointer or
        -- the same members queues here, locks taken in one global order (the
        -- same idiom and key space as the record writers' advisory locks).
        PERFORM pg_advisory_xact_lock(h)
          FROM (SELECT DISTINCT hashtext(k) AS h
                  FROM unnest(lock_keys) AS t(k)
                 ORDER BY h) AS locks;

        -- VALIDATION PHASE -- reads and RAISE only.

        IF mode <> 'open' THEN
            IF oldm = '' AND ptr_digest <> '' THEN
                SELECT p.member INTO pointed FROM popoto_open_ptr AS p
                 WHERE p.prefix = ptr_prefix AND p.digest = ptr_digest;
                IF pointed IS NOT NULL THEN
                    oldm := pointed;
                END IF;
            END IF;

            -- A caller-named successor must exist at the instant of the
            -- write (#588): inside the unit of work's transaction the
            -- successor's own save_record is already visible here.
            IF newm <> '' AND NOT EXISTS (
                SELECT 1 FROM popoto_record AS r WHERE r.key = newm
            ) THEN
                RAISE EXCEPTION USING ERRCODE = 'P0631',
                    MESSAGE = 'POPOTO_VALIDITY_MEMBER_ABSENT successor ' || newm;
            END IF;

            IF oldm <> '' AND NOT EXISTS (
                SELECT 1 FROM popoto_record AS r WHERE r.key = oldm
            ) THEN
                IF asserted_old THEN
                    RAISE EXCEPTION USING ERRCODE = 'P0631',
                        MESSAGE = 'POPOTO_VALIDITY_MEMBER_ABSENT incumbent ' || oldm;
                END IF;
                -- Resolved from the pointer: a hint, so a pointer left naming
                -- a hard-deleted record reads as "no incumbent".
                oldm := '';
            END IF;

            IF oldm <> '' AND oldm <> newm THEN
                -- FOR UPDATE: a second closer of the same incumbent waits
                -- here and then re-reads the committed close, so it is the
                -- idempotent no-op it would have been on Redis.
                SELECT s.score INTO old_ia FROM popoto_sorted AS s
                 WHERE s.idx = ia_key AND s.member = oldm FOR UPDATE;
                IF old_ia = 'infinity'::double precision THEN
                    SELECT s.score INTO old_vf FROM popoto_sorted AS s
                     WHERE s.idx = vf_key AND s.member = oldm;
                    IF old_vf IS NOT NULL AND close_at < old_vf THEN
                        RAISE EXCEPTION USING ERRCODE = 'P0631',
                            MESSAGE = 'POPOTO_VALIDITY_CLOSE_BEFORE_START';
                    END IF;
                    will_close := true;
                END IF;
            END IF;
        END IF;

        IF newm <> '' AND vf_assert THEN
            -- Valid-time has one writer: a disagreeing assertion is refused
            -- rather than lost to the NX insert below.
            SELECT s.score INTO stored_vf FROM popoto_sorted AS s
             WHERE s.idx = vf_key AND s.member = newm;
            IF stored_vf IS NOT NULL AND stored_vf <> valid_from THEN
                RAISE EXCEPTION USING ERRCODE = 'P0631',
                    MESSAGE = 'POPOTO_VALIDITY_VALID_FROM_CONFLICT',
                    DETAIL = stored_vf::text || ' ' || valid_from::text;
            END IF;
        END IF;

        -- MUTATION PHASE -- every check above has passed.

        IF will_close THEN
            INSERT INTO popoto_sorted (idx, member, score)
                 VALUES (ia_key, oldm, close_at)
            ON CONFLICT (idx, member) DO UPDATE SET score = EXCLUDED.score;
            closed := oldm;
            IF newm <> '' THEN
                IF fwd_key <> '' THEN
                    INSERT INTO popoto_map (idx, member, value)
                         VALUES (fwd_key, oldm, convert_to(newm, 'UTF8'))
                    ON CONFLICT (idx, member) DO UPDATE SET value = EXCLUDED.value;
                END IF;
                IF rev_key <> '' THEN
                    INSERT INTO popoto_map (idx, member, value)
                         VALUES (rev_key, newm, convert_to(oldm, 'UTF8'))
                    ON CONFLICT (idx, member) DO UPDATE SET value = EXCLUDED.value;
                END IF;
            END IF;
        END IF;

        IF newm <> '' THEN
            SELECT s.score INTO new_ia FROM popoto_sorted AS s
             WHERE s.idx = ia_key AND s.member = newm;
            IF new_ia IS NULL OR new_ia = 'infinity'::double precision THEN
                -- NX: a re-save never shifts an interval, and an already
                -- closed record is never resurrected (the guard above).
                INSERT INTO popoto_sorted (idx, member, score)
                     VALUES (vf_key, newm, valid_from)
                ON CONFLICT (idx, member) DO NOTHING;
                INSERT INTO popoto_sorted (idx, member, score)
                     VALUES (ig_key, newm, ingested_at)
                ON CONFLICT (idx, member) DO NOTHING;
                INSERT INTO popoto_sorted (idx, member, score)
                     VALUES (ia_key, newm, 'infinity'::double precision)
                ON CONFLICT (idx, member) DO NOTHING;
                IF ptr_digest <> '' THEN
                    INSERT INTO popoto_open_ptr (prefix, digest, member)
                         VALUES (ptr_prefix, ptr_digest, newm)
                    ON CONFLICT (prefix, digest) DO UPDATE SET member = EXCLUDED.member;
                END IF;
            END IF;
        END IF;

        RETURN closed;
    END
    $popoto$
    """,
)

#: The SQLSTATE ``popoto_supersede`` raises its ``POPOTO_VALIDITY_*`` replies
#: under: class ``P0`` (PL/pgSQL error) with a custom subcode, so the catch in
#: :meth:`PostgresBackend.supersede` is exact and any other error -- a bug in
#: the function, a lost connection -- propagates untouched.
SUPERSEDE_ERRCODE = "P0631"

#: The index tables, by the ``kind`` the protocol names them with.
_INDEX_TABLES: dict[str, str] = {
    "sorted": "popoto_sorted",
    "set": "popoto_set",
    "map": "popoto_map",
}

#: Lua 5.1's ``LUAI_NUMFMT``: what ``tostring(number)`` renders inside Redis.
_LUA_NUMBER_FORMAT = "%.14g"

#: An operation on a cursor; the unit of work queues these and runs them in
#: one transaction, the executed-now path runs one in its own.
Op = Callable[[Any], Any]


def _todo(name: str) -> NotImplementedError:
    return NotImplementedError(
        f"PostgresBackend.{name} is not implemented in the backend-seam POC "
        "(see docs/plans/sdlc-631.md WS3)"
    )


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:  # pragma: no cover - best effort at interpreter exit
        pass


def _field_bytes(name: Any) -> bytes:
    """Field names are ``bytes`` on the wire (WS0 deviation 1); a ``str`` is
    encoded the way redis-py would encode it."""
    if isinstance(name, bytes):
        return name
    return str(name).encode(ENCODING)


def _index_table(kind: str) -> str:
    try:
        return _INDEX_TABLES[kind]
    except KeyError:
        raise ValueError(
            f"index kind must be one of {sorted(_INDEX_TABLES)}, got {kind!r}"
        ) from None


def _glob_to_regex(pattern: str) -> str:
    """Translate a Redis glob (``SCAN MATCH`` / ``HSCAN MATCH``) to an anchored
    POSIX regular expression for Postgres's ``~``.

    Mirrors Redis's ``stringmatchlen``: ``*`` any run, ``?`` one character,
    ``\\x`` the literal ``x``, ``[...]`` a class with ``^`` negation, ``a-z``
    ranges and ``\\`` escapes, closed by ``]`` or by the end of the pattern. A
    class is emitted as an alternation of escaped characters rather than a
    bracket expression, so Postgres's bracket-escaping rules never apply;
    negation is a lookahead. Ranges are expanded (a code-point span over 256
    is refused, which no popoto pattern uses). Matching is case-sensitive and
    ``.`` spans newlines, as Redis's does.
    """
    out: list[str] = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            out.append(".*")
        elif ch == "?":
            out.append(".")
        elif ch == "\\" and i + 1 < n:
            i += 1
            out.append(re.escape(pattern[i]))
        elif ch == "[":
            i += 1
            negate = i < n and pattern[i] == "^"
            if negate:
                i += 1
            alternatives: list[str] = []
            while i < n and pattern[i] != "]":
                lo = pattern[i]
                if lo == "\\" and i + 1 < n:
                    # An escaped character is a literal, never a range start.
                    i += 1
                    alternatives.append(re.escape(pattern[i]))
                elif i + 2 < n and pattern[i + 1] == "-":
                    hi = pattern[i + 2]
                    i += 2
                    span = range(min(ord(lo), ord(hi)), max(ord(lo), ord(hi)) + 1)
                    if len(span) > 256:
                        raise ValueError(
                            f"glob range {lo}-{hi} spans more than 256 code points"
                        )
                    alternatives.extend(re.escape(chr(c)) for c in span)
                else:
                    alternatives.append(re.escape(lo))
                i += 1
            body = "|".join(alternatives)
            if negate:
                out.append(f"(?!(?:{body}))." if body else ".")
            else:
                out.append(f"(?:{body})" if body else "(?!.).")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    return "".join(out)


def _match_all(pattern: str) -> bool:
    """``*`` (or a run of them) matches every name: skip the regex."""
    return pattern != "" and set(pattern) == {"*"}


def _check_score(score: float, *, what: str = "value") -> float:
    """A ``NaN`` score is Redis's ``value is not a valid float`` error.

    ``-0.0`` becomes ``0.0`` (IEEE: ``-0.0 + 0.0 == +0.0``): ``ZADD``
    normalises the sign of zero and ``float8`` would keep it.
    """
    score = float(score)
    if math.isnan(score):
        raise ValueError(f"{what} is not a valid float")
    return score + 0.0


def _lock_record_keys(cur: Any, keys: Sequence[str]) -> None:
    """Take the per-key transaction advisory lock for every key in ``keys``,
    in one global order (see "Locking" in the module docstring).

    Runs as the *first* statement of every operation that writes a record
    key, so two instances' read-modify-write sequences on the same key are
    serialised the way Redis's single thread serialises them. Lock ids are
    ``hashtext(key)``; when an operation names more than one key (a rename
    with ``obsolete_key``) they are taken ascending by lock id, ``DISTINCT``
    so a hash collision locks once, so two instances renaming in opposite
    directions cannot deadlock on each other. The ordered subquery is the
    documented Postgres idiom for acquiring advisory locks in order: the
    volatile call is evaluated per row as the sorted rows stream.
    """
    cur.execute(
        "SELECT pg_advisory_xact_lock(h) FROM ("
        "  SELECT DISTINCT hashtext(k) AS h FROM unnest(%s::text[]) AS t(k)"
        "  ORDER BY h"
        ") AS locks",
        (list(keys),),
    )


# -- Swap helpers (family G) ---------------------------------------------------
# Each is one statement on the cursor of the swap's transaction. They mirror
# the Redis commands INDEX_SWAP_LUA / TAG_SWAP_LUA issue (SISMEMBER, SMEMBERS,
# SREM, SADD, SET/DEL of the pointer, HSET/HDEL of the field) one for one, so
# the swap bodies read like the scripts.


def _legacy_pointer_field(field: str) -> bytes:
    """The pre-#476 in-hash pointer field, ``{field}\\x00idxset``, as the
    ``bytea`` field name ``popoto_record`` stores it under."""
    return _field_bytes(f"{field}\x00idxset")


def _unique_conflict_message(record_key: str, field: str, new_idx: str) -> str:
    """Byte-identical to ``RedisBackend.swap_index``'s wording for the
    ``POPOTO_UNIQUE_CONFLICT`` reply, so the field layer's re-wrap (WS1c,
    deviation 6) sees the same object on both backends."""
    return (
        f"Uniqueness violation on {record_key}.{field}: the value "
        f"indexed at {new_idx!r} is already taken by another instance"
    )


def _pointer_one(cur: Any, record_key: str, field: str) -> str | None:
    """``GET $IdxPtr:...``: the one index the record is in for ``field``."""
    cur.execute(
        "SELECT idx FROM popoto_pointer WHERE key = %s AND field = %s "
        'ORDER BY idx COLLATE "C" LIMIT 1',
        (record_key, field),
    )
    row = cur.fetchone()
    return None if row is None else row[0]


def _pointer_all(cur: Any, record_key: str, field: str) -> set[str]:
    """``SMEMBERS $TagPtr:...``: every index the record is in for ``field``."""
    cur.execute(
        "SELECT idx FROM popoto_pointer WHERE key = %s AND field = %s",
        (record_key, field),
    )
    return {idx for (idx,) in cur.fetchall()}


def _set_pointer(cur: Any, record_key: str, field: str, idxs: Sequence[str]) -> None:
    """``SET`` (one index) / ``DEL`` + ``SADD`` each (tags): the pointer now
    names exactly ``idxs``."""
    cur.execute(
        "DELETE FROM popoto_pointer WHERE key = %s AND field = %s "
        "AND NOT (idx = ANY(%s))",
        (record_key, field, list(idxs)),
    )
    if idxs:
        cur.execute(
            "INSERT INTO popoto_pointer (key, field, idx) "
            "SELECT %s, %s, unnest(%s::text[]) ON CONFLICT DO NOTHING",
            (record_key, field, list(idxs)),
        )


def _clear_pointer(cur: Any, record_key: str, field: str) -> int:
    """``DEL ptr_key, old_ptr_key``: 1 when the pointer existed, else 0."""
    cur.execute(
        "DELETE FROM popoto_pointer WHERE key = %s AND field = %s",
        (record_key, field),
    )
    return 1 if cur.rowcount > 0 else 0


def _is_member(cur: Any, idx: str, member: str) -> bool:
    """``SISMEMBER``."""
    cur.execute(
        "SELECT 1 FROM popoto_set WHERE idx = %s AND member = %s", (idx, member)
    )
    return cur.fetchone() is not None


def _add_member(cur: Any, idx: str, member: str) -> int:
    """``SADD``: 1 when added, 0 when already present."""
    cur.execute(
        "INSERT INTO popoto_set (idx, member) VALUES (%s, %s) ON CONFLICT DO NOTHING",
        (idx, member),
    )
    return 1 if cur.rowcount > 0 else 0


def _remove_member(cur: Any, idx: str, member: str) -> int:
    """``SREM``: 1 when removed, 0 when it was not a member."""
    cur.execute("DELETE FROM popoto_set WHERE idx = %s AND member = %s", (idx, member))
    return 1 if cur.rowcount > 0 else 0


def _upsert_field(cur: Any, record_key: str, field: str, value: bytes) -> None:
    """``HSET model_key field new_bytes``: the field layer's packed value,
    byte for byte, creating the record when it does not exist yet (as the
    Lua's HSET does)."""
    cur.execute(
        "INSERT INTO popoto_record (key, field, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
        (record_key, _field_bytes(field), value),
    )


def _scrub_legacy_pointer(cur: Any, record_key: str, legacy_field: bytes) -> None:
    """``HDEL model_key {field}\\x00idxset``: the adopted pre-#476 pointer."""
    cur.execute(
        "DELETE FROM popoto_record WHERE key = %s AND field = %s",
        (record_key, legacy_field),
    )


class _ScriptReply(Exception):
    """A ``POPOTO_VALIDITY_*`` reply raised by ``popoto_supersede``, shaped as
    the Redis backend sees it: ``str()`` is the token line and nothing else.
    Handed to the field layer's ``map_lua_error`` (its ``_LUA_ERROR_MAP`` is
    the one token -> exception table for both backends)."""


def _supersede_reply(error: Any) -> str:
    """The Lua-identical token line for a ``SUPERSEDE_ERRCODE`` error.

    ``MESSAGE`` carries the token (and, for the member-absent reply, the role
    and key); the valid-from conflict's two numbers travel in ``DETAIL`` as
    Postgres ``float8`` text and are rendered here with Lua's ``%.14g``, since
    ``tostring(1759500000.123456)`` is ``1759500000.1235`` in the script and
    Postgres would print every digit.
    """
    primary = str(error.diag.message_primary or "")
    detail = error.diag.message_detail
    if detail:
        numbers = " ".join(_lua_tostring(float(part)) for part in detail.split())
        return f"{primary} {numbers}"
    return primary


# -- Lua parity helpers for increment_field ------------------------------------


def _lua_tostring(value: float) -> str:
    """``tostring(n)`` in Redis's Lua 5.1: ``%.14g``."""
    return _LUA_NUMBER_FORMAT % value


def _lua_tonumber(value: Any) -> float:
    """``tonumber(x)`` on what ``cmsgpack.unpack`` produced for the envelope's
    ``as_encodable``; a value Lua could not convert makes the script fail on
    ``nil + delta``, so this raises ``ValueError`` in the same cases."""
    if isinstance(value, bool):
        raise ValueError("tonumber(boolean) is nil")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, bytes):
        value = value.decode(ENCODING)
    if isinstance(value, str):
        return float(value)
    raise ValueError(f"tonumber({type(value).__name__}) is nil")


def _current_value(packed: bytes | None, is_decimal: bool) -> float:
    """The Lua's ``current_val``: ``0`` when the field is absent, the
    envelope's ``as_encodable`` for a Decimal increment on a tagged map, the
    number itself for a numeric payload, and ``0`` for anything else (a
    string, boolean, nil, list, or a map without the tag)."""
    if packed is None:
        return 0.0
    decoded = msgpack.unpackb(packed, raw=True, strict_map_key=False)
    if is_decimal and isinstance(decoded, dict):
        tagged = decoded.get(b"as_encodable")
        # Lua truthiness: only nil and false fail the ``and``.
        if tagged is not None and tagged is not False:
            return _lua_tonumber(tagged)
    if isinstance(decoded, (int, float)) and not isinstance(decoded, bool):
        return float(decoded)
    return 0.0


def _cmsgpack_pack_number(value: float) -> bytes:
    """Pack a Lua number as Redis's ``cmsgpack`` does: a msgpack integer when
    the double is integral and fits ``int64``, else ``float32`` when the
    narrowing is lossless, else ``float64``.

    One documented, un-emulated hole at exactly ``2**63``: cmsgpack decides
    "fits ``int64``" with a C ``(int64_t)d`` cast, which is undefined
    behaviour for ``9223372036854775808.0``. On aarch64 it saturates, so an
    arm64 Redis stores ``cf 7fffffffffffffff`` (``int64`` max); an x86-64
    Redis and this function store the float32 ``ca 5f000000``. The *returned*
    value (``%.14g``) is identical either way; only a later decode differs
    (``int`` ``2**63 - 1`` against ``float`` ``2**63``). Emulating one
    platform's UB would be wrong on the other, so the deviation is recorded
    here and in ``docs/features/postgres-backend.md`` instead.
    """
    if math.isfinite(value) and value.is_integer() and -(2**63) <= value < 2**63:
        return msgpack.packb(int(value))
    if not math.isnan(value):
        try:
            narrowed = struct.unpack(">f", struct.pack(">f", value))[0]
        except OverflowError:
            narrowed = None
        if narrowed == value:
            return struct.pack(">Bf", 0xCA, value)
    return struct.pack(">Bd", 0xCB, value)


def _pack_increment_result(new_val: float, is_decimal: bool) -> bytes:
    if is_decimal:
        return msgpack.packb(
            {"__Decimal__": True, "as_encodable": _lua_tostring(new_val)}
        )
    return _cmsgpack_pack_number(new_val)


# -- Unit of work ---------------------------------------------------------------


class PostgresUnitOfWork:
    """The Postgres :class:`~popoto.backends.UnitOfWork`: a queue of
    operations run inside one transaction on :meth:`commit`.

    Mirrors the Redis pipeline's shape rather than an open transaction:
    nothing reaches the server until ``commit()``, every ``uow=`` method
    returns ``None`` when queued (WS0 deviation 5), ``commit()`` returns one
    entry per queued *operation* (the value the executed-now call would have
    returned) and leaves the queue empty, so a second ``commit()`` returns
    ``[]`` as a re-executed pipeline does. Leaving the ``with`` block without
    committing discards the queue, as ``Pipeline.__exit__`` resets.

    Failure semantics differ: an operation that fails inside ``commit()``
    rolls back the *entire* queue (one transaction), whereas a Redis pipeline
    runs the remaining commands and commits them.
    """

    def __init__(self, backend: PostgresBackend) -> None:
        self._backend = backend
        self._ops: list[Op] = []

    def queue(self, op: Op) -> None:
        self._ops.append(op)

    def __len__(self) -> int:
        return len(self._ops)

    @property
    def command_stack(self) -> list[Op]:
        """The queue, under the name redis-py gives a pipeline's.

        ``SupersessionProtocol._save_and_close`` reads
        ``len(pipe.command_stack)`` for ``close_index`` -- the position of the
        queued supersede, whose reply it takes from ``commit()``'s result list
        -- and ``_validate_caller_pipeline`` requires the attribute. One entry
        per queued operation here is one entry per ``commit()`` result, so the
        position is exact.
        """
        return self._ops

    def commit(self) -> list[Any]:
        ops, self._ops = self._ops, []
        if not ops:
            return []
        return self._backend._run_all(ops)

    def reset(self) -> None:
        """Discard everything queued (the pipeline's ``reset()``)."""
        self._ops = []

    rollback = reset

    def __enter__(self) -> PostgresUnitOfWork:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.reset()


# -- Backend --------------------------------------------------------------------


class PostgresBackend:
    """The Postgres :class:`~popoto.backends.Backend`; see the module
    docstring for the schema, the connection policy, the increment envelope,
    the sorted-set semantics, the atomic swaps and the validity family. Family H
    still raise :class:`NotImplementedError`."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._conn: Any = None
        self._finalizer: Any = None

    # -- connection ----------------------------------------------------------

    def _connection(self) -> Any:
        """The instance's connection, opened (and the schema bootstrapped) on
        first use. The only place ``psycopg`` is imported."""
        if self._conn is None or self._conn.closed:
            import psycopg

            conn = psycopg.connect(self.url, autocommit=True)
            try:
                # One transaction, serialised per schema: ``IF NOT EXISTS`` is
                # not race-safe on its own (see "Schema" in the module doc).
                with conn.transaction():
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(%s, hashtext(current_schema()))",
                        (DDL_LOCK_CLASS,),
                    )
                    for statement in SCHEMA_DDL:
                        conn.execute(statement)
            except BaseException:
                _close_quietly(conn)
                raise
            self._finalizer = weakref.finalize(self, _close_quietly, conn)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        """Close the connection; the next call reopens one."""
        if self._finalizer is not None:
            self._finalizer.detach()
            self._finalizer = None
        if self._conn is not None:
            _close_quietly(self._conn)
            self._conn = None

    def _run_all(self, ops: Sequence[Op]) -> list[Any]:
        """Run ``ops`` in order inside one transaction."""
        conn = self._connection()
        with conn.transaction():
            with conn.cursor() as cur:
                return [op(cur) for op in ops]

    def _run(self, op: Op, uow: UnitOfWork | None) -> Any:
        """Execute ``op`` now in its own transaction, or queue it on ``uow``
        and return ``None``."""
        if uow is None:
            return self._run_all([op])[0]
        if not isinstance(uow, PostgresUnitOfWork):
            raise TypeError(
                "PostgresBackend methods take the unit of work from "
                f"PostgresBackend.begin(), not {type(uow).__name__}"
            )
        uow.queue(op)
        return None

    def _query(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any]
    ) -> list[tuple[Any, ...]]:
        """A read, outside any transaction block (autocommit)."""
        return list(self._connection().execute(sql, params).fetchall())

    # -- A. Unit of work ---------------------------------------------------

    def begin(self) -> UnitOfWork:
        return PostgresUnitOfWork(self)

    def native(self) -> Any:
        raise NotImplementedError(
            "native() is Redis-only in the backend-seam POC: the out-of-scope "
            "mixin code it serves has no Postgres path"
        )

    # -- B. Records ---------------------------------------------------------

    def save_record(
        self,
        key: str,
        fields: Mapping[Any, bytes],
        *,
        class_set: str | None = None,
        obsolete_key: str | None = None,
        ttl: int | None = None,
        expire_at: float | None = None,
        numeric: Mapping[str, float] | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        if ttl is not None or expire_at is not None:
            raise NotImplementedError(
                "record TTL (ttl=/expire_at=) is not implemented in the "
                "backend-seam POC: Postgres has no key expiry"
            )
        names = [_field_bytes(name) for name in fields]
        values = [bytes(value) for value in fields.values()]
        numeric_names = list(numeric) if numeric else []
        numeric_values = (
            [float(numeric[name]) for name in numeric_names] if numeric else []
        )

        locked = [key]
        if obsolete_key and obsolete_key != key:
            locked.append(obsolete_key)

        def op(cur: Any) -> Any:
            # First statement: serialise against increment_field / delete_record
            # on these keys (B1 in the #737 review). On the ``uow=`` path this
            # runs at commit(), inside the unit of work's transaction.
            _lock_record_keys(cur, locked)
            # Mirrors the Redis pipeline's reply: the HSET count of newly
            # created fields when any were given, else the SADD reply.
            reply = 0
            if names:
                rows = cur.execute(
                    "INSERT INTO popoto_record (key, field, value) "
                    "SELECT %s, f, v FROM unnest(%s::bytea[], %s::bytea[]) AS t(f, v) "
                    "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value "
                    "RETURNING (xmax = 0)",
                    (key, names, values),
                ).fetchall()
                reply = sum(1 for (inserted,) in rows if inserted)
            if numeric_names:
                cur.execute(
                    "INSERT INTO popoto_numeric (key, field, value) "
                    "SELECT %s, f, v FROM unnest(%s::text[], %s::float8[]) AS t(f, v) "
                    "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                    (key, numeric_names, numeric_values),
                )
            # protocol-2: ``class_set=None`` leaves the class set untouched --
            # no membership insert, no obsolete-member delete.
            if class_set is not None:
                cur.execute(
                    "INSERT INTO popoto_set (idx, member) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (class_set, key),
                )
                if not names:
                    reply = cur.rowcount
            if obsolete_key and obsolete_key != key:
                if class_set is not None:
                    cur.execute(
                        "DELETE FROM popoto_set WHERE idx = %s AND member = %s",
                        (class_set, obsolete_key),
                    )
                cur.execute("DELETE FROM popoto_record WHERE key = %s", (obsolete_key,))
                if not names and class_set is None:
                    # Redis's first queued reply is then the obsolete DEL.
                    reply = 1 if cur.rowcount > 0 else 0
                cur.execute(
                    "DELETE FROM popoto_numeric WHERE key = %s", (obsolete_key,)
                )
            return reply

        return self._run(op, uow)

    def set_expiry(
        self,
        key: str,
        *,
        ttl: int | None = None,
        expire_at: float | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        # protocol-2. Same scope line as save_record's ttl=/expire_at=: a
        # record without a TTL asks for nothing and gets ``None`` (Redis does
        # the same), so Model.save's partial path works here for Meta without
        # ``ttl``; one with a TTL is refused before anything is written.
        if ttl is None and expire_at is None:
            return None
        raise NotImplementedError(
            "record TTL (set_expiry) is not implemented in the backend-seam "
            "POC: Postgres has no key expiry"
        )

    def load_record(self, key: str) -> dict[Any, bytes] | None:
        rows = self._query(
            "SELECT field, value FROM popoto_record WHERE key = %s", (key,)
        )
        if not rows:
            return None
        return {bytes(field): bytes(value) for field, value in rows}

    def load_records(self, keys: Sequence[str]) -> list[dict[Any, bytes] | None]:
        if not keys:
            return []
        rows = self._query(
            "SELECT key, field, value FROM popoto_record WHERE key = ANY(%s)",
            (list(dict.fromkeys(keys)),),
        )
        found: dict[str, dict[Any, bytes]] = {}
        for key, field, value in rows:
            found.setdefault(key, {})[bytes(field)] = bytes(value)
        # A fresh dict per position, as a pipelined HGETALL replies.
        return [dict(found[key]) if key in found else None for key in keys]

    def load_fields(self, key: str, names: Sequence[str]) -> list[bytes | None]:
        if not names:
            raise ValueError("load_fields() requires at least one field name")
        wanted = [_field_bytes(name) for name in names]
        rows = self._query(
            "SELECT field, value FROM popoto_record "
            "WHERE key = %s AND field = ANY(%s)",
            (key, wanted),
        )
        got = {bytes(field): bytes(value) for field, value in rows}
        return [got.get(name) for name in wanted]

    def load_fields_many(
        self, keys: Sequence[str], names: Sequence[str]
    ) -> list[list[bytes | None]]:
        # protocol-3: one SELECT for the batch; a missing record is a row of
        # None per name, as a pipelined HMGET replies.
        if not names:
            raise ValueError("load_fields_many() requires at least one field name")
        if not keys:
            return []
        wanted = [_field_bytes(name) for name in names]
        rows = self._query(
            "SELECT key, field, value FROM popoto_record "
            "WHERE key = ANY(%s) AND field = ANY(%s)",
            (list(dict.fromkeys(keys)), wanted),
        )
        found: dict[str, dict[bytes, bytes]] = {}
        for key, field, value in rows:
            found.setdefault(key, {})[bytes(field)] = bytes(value)
        return [[found.get(key, {}).get(name) for name in wanted] for key in keys]

    def record_exists(self, key: str) -> bool:
        rows = self._query(
            "SELECT EXISTS (SELECT 1 FROM popoto_record WHERE key = %s)", (key,)
        )
        return bool(rows[0][0])

    def records_exist(self, keys: Sequence[str]) -> list[bool]:
        if not keys:
            return []
        rows = self._query(
            "SELECT DISTINCT key FROM popoto_record WHERE key = ANY(%s)",
            (list(keys),),
        )
        found = {row[0] for row in rows}
        return [key in found for key in keys]

    def delete_record(
        self, key: str, *, class_set: str, uow: UnitOfWork | None = None
    ) -> Any:
        def op(cur: Any) -> Any:
            _lock_record_keys(cur, [key])
            cur.execute("DELETE FROM popoto_record WHERE key = %s", (key,))
            existed = cur.rowcount > 0
            cur.execute("DELETE FROM popoto_numeric WHERE key = %s", (key,))
            cur.execute(
                "DELETE FROM popoto_set WHERE idx = %s AND member = %s",
                (class_set, key),
            )
            return existed

        return self._run(op, uow)

    def list_keys(self, class_set: str) -> set[str]:
        rows = self._query("SELECT member FROM popoto_set WHERE idx = %s", (class_set,))
        return {member for (member,) in rows}

    def count_records(self, class_set: str) -> int:
        rows = self._query(
            "SELECT count(*) FROM popoto_set WHERE idx = %s", (class_set,)
        )
        return int(rows[0][0])

    # -- C. Atomic increment -------------------------------------------------

    def increment_field(
        self,
        key: str,
        field: str,
        delta: int | float | Decimal,
        *,
        kind: Literal["int", "float", "decimal"],
        uow: UnitOfWork | None = None,
    ) -> int | float | Decimal | None:
        if kind not in ("int", "float", "decimal"):
            raise ValueError(
                f"increment_field kind must be int/float/decimal, got {kind!r}"
            )
        is_decimal = kind == "decimal"
        field_bytes = field.encode(ENCODING)
        # The Redis path sends ``str(delta)`` and the script ``tonumber``s it:
        # a double either way, so the arithmetic below is float arithmetic.
        delta_val = float(str(float(delta) if isinstance(delta, Decimal) else delta))

        def op(cur: Any) -> Any:
            # Serialise read-modify-write on this record (``FOR UPDATE`` cannot
            # lock a row that does not exist yet) -- against other increments
            # *and* against save_record / delete_record, which take the same
            # lock first.
            _lock_record_keys(cur, [key])
            row = cur.execute(
                "SELECT value FROM popoto_record WHERE key = %s AND field = %s "
                "FOR UPDATE",
                (key, field_bytes),
            ).fetchone()
            current = _current_value(bytes(row[0]) if row else None, is_decimal)
            new_val = current + delta_val
            cur.execute(
                "INSERT INTO popoto_record (key, field, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                (key, field_bytes, _pack_increment_result(new_val, is_decimal)),
            )
            cur.execute(
                "INSERT INTO popoto_numeric (key, field, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (key, field) DO UPDATE SET value = EXCLUDED.value",
                (key, field, new_val),
            )
            return _lua_tostring(new_val)

        result_str = self._run(op, uow)
        if result_str is None:
            return None
        if kind == "int":
            return int(float(result_str))
        if kind == "decimal":
            return Decimal(result_str)
        return float(result_str)

    # -- D. Side maps --------------------------------------------------------
    # One row per (idx, member) in ``popoto_map``; every write is one
    # statement, so no advisory lock is taken (see "Sorted-set semantics").

    def map_get(self, idx: str, member: str) -> bytes | None:
        rows = self._query(
            "SELECT value FROM popoto_map WHERE idx = %s AND member = %s",
            (idx, member),
        )
        return bytes(rows[0][0]) if rows else None

    def map_set(
        self,
        idx: str,
        member: str,
        value: bytes,
        *,
        only_if_absent: bool = False,
        uow: UnitOfWork | None = None,
    ) -> bool | None:
        payload = bytes(value)

        def op(cur: Any) -> Any:
            if only_if_absent:
                # HSETNX: 1 when the entry was created, 0 when it existed.
                cur.execute(
                    "INSERT INTO popoto_map (idx, member, value) VALUES (%s, %s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (idx, member, payload),
                )
                return cur.rowcount > 0
            # HSET: 1 for a new entry, 0 for an overwrite.
            row = cur.execute(
                "INSERT INTO popoto_map (idx, member, value) VALUES (%s, %s, %s) "
                "ON CONFLICT (idx, member) DO UPDATE SET value = EXCLUDED.value "
                "RETURNING (xmax = 0)",
                (idx, member, payload),
            ).fetchone()
            return bool(row[0])

        return self._run(op, uow)

    def map_delete(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> int | None:
        def op(cur: Any) -> Any:
            cur.execute(
                "DELETE FROM popoto_map WHERE idx = %s AND member = %s",
                (idx, member),
            )
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)

    def map_scan(
        self, idx: str, pattern: str = "*", count: int = 100
    ) -> dict[str, bytes]:
        # ``count`` is HSCAN's per-round-trip batch hint; there is no cursor
        # here, so it is accepted for signature parity and ignored.
        if _match_all(pattern):
            rows = self._query(
                "SELECT member, value FROM popoto_map WHERE idx = %s", (idx,)
            )
        else:
            rows = self._query(
                "SELECT member, value FROM popoto_map WHERE idx = %s AND member ~ %s",
                (idx, _glob_to_regex(pattern)),
            )
        return {member: bytes(value) for member, value in rows}

    # -- E. Set indexes ------------------------------------------------------

    def index_add(self, idx: str, member: str, *, uow: UnitOfWork | None = None) -> Any:
        def op(cur: Any) -> Any:
            # SADD's reply: the number of members actually added.
            cur.execute(
                "INSERT INTO popoto_set (idx, member) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (idx, member),
            )
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)

    def index_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        def op(cur: Any) -> Any:
            cur.execute(
                "DELETE FROM popoto_set WHERE idx = %s AND member = %s",
                (idx, member),
            )
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)

    def index_members(self, idx: str) -> set[str]:
        rows = self._query("SELECT member FROM popoto_set WHERE idx = %s", (idx,))
        return {member for (member,) in rows}

    def index_union(self, idxs: Sequence[str]) -> set[str]:
        if not idxs:
            return set()
        rows = self._query(
            "SELECT DISTINCT member FROM popoto_set WHERE idx = ANY(%s)",
            (list(idxs),),
        )
        return {member for (member,) in rows}

    def index_intersection(self, idxs: Sequence[str]) -> set[str]:
        if not idxs:
            return set()
        distinct = list(dict.fromkeys(idxs))
        # A member is in the intersection when it appears under every distinct
        # index named; a missing index contributes nothing, so the result is
        # empty, as SINTER with a missing key is.
        rows = self._query(
            "SELECT member FROM popoto_set WHERE idx = ANY(%s) "
            "GROUP BY member HAVING count(DISTINCT idx) = %s",
            (distinct, len(distinct)),
        )
        return {member for (member,) in rows}

    def scan_index_names(self, pattern: str) -> list[str]:
        # The three index tables only; a record key never matches here (see
        # "Sorted-set semantics" in the module docstring for the difference
        # from SCAN, which sees every key of every type).
        union = (
            "SELECT idx FROM popoto_set UNION SELECT idx FROM popoto_sorted "
            "UNION SELECT idx FROM popoto_map"
        )
        if _match_all(pattern):
            rows = self._query(f"SELECT idx FROM ({union}) AS names", ())
        else:
            rows = self._query(
                f"SELECT idx FROM ({union}) AS names WHERE idx ~ %s",
                (_glob_to_regex(pattern),),
            )
        return [idx for (idx,) in rows]

    def scan_record_keys(self, pattern: str) -> list[str]:
        # Only record keys live in ``popoto_record``, so the TYPE filter the
        # Redis backend applies after SCAN is the table itself.
        if _match_all(pattern):
            rows = self._query("SELECT DISTINCT key FROM popoto_record", ())
        else:
            rows = self._query(
                "SELECT DISTINCT key FROM popoto_record WHERE key ~ %s",
                (_glob_to_regex(pattern),),
            )
        return [key for (key,) in rows]

    # -- F. Sorted indexes ---------------------------------------------------
    # See "Sorted-set semantics on a table" in the module docstring for the
    # tie order, the bound comparison and the ZRANGE index arithmetic.

    def sorted_add(
        self, idx: str, member: str, score: float, *, uow: UnitOfWork | None = None
    ) -> Any:
        score = _check_score(score)

        def op(cur: Any) -> Any:
            # ZADD's reply: 1 for a new member, 0 when only the score changed.
            row = cur.execute(
                "INSERT INTO popoto_sorted (idx, member, score) VALUES (%s, %s, %s) "
                "ON CONFLICT (idx, member) DO UPDATE SET score = EXCLUDED.score "
                "RETURNING (xmax = 0)",
                (idx, member, score),
            ).fetchone()
            return 1 if row[0] else 0

        return self._run(op, uow)

    def sorted_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        def op(cur: Any) -> Any:
            cur.execute(
                "DELETE FROM popoto_sorted WHERE idx = %s AND member = %s",
                (idx, member),
            )
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)

    def sorted_score(self, idx: str, member: str) -> float | None:
        rows = self._query(
            "SELECT score FROM popoto_sorted WHERE idx = %s AND member = %s",
            (idx, member),
        )
        return float(rows[0][0]) if rows else None

    def sorted_count(self, idx: str) -> int:
        rows = self._query("SELECT count(*) FROM popoto_sorted WHERE idx = %s", (idx,))
        return int(rows[0][0])

    def sorted_members(
        self, idx: str, start: int = 0, stop: int = -1, *, reverse: bool = False
    ) -> list[str]:
        order = "DESC" if reverse else "ASC"
        # ZRANGE's window over the ranked members, resolved in SQL so the
        # count and the slice come from one snapshot: a negative index counts
        # from the end (clamped to 0 for ``start``), ``stop`` is inclusive,
        # and ``start > stop`` or ``start >= n`` is empty.
        rows = self._query(
            "SELECT member FROM ("
            "  SELECT member,"
            f'    row_number() OVER (ORDER BY score {order}, member COLLATE "C" {order})'
            "      - 1 AS rn,"
            "    count(*) OVER () AS n"
            "  FROM popoto_sorted WHERE idx = %(idx)s"
            ") AS ranked"
            " WHERE rn >= CASE WHEN %(start)s < 0"
            "               THEN greatest(n + %(start)s, 0) ELSE %(start)s END"
            "   AND rn <= CASE WHEN %(stop)s < 0 THEN n + %(stop)s ELSE %(stop)s END"
            " ORDER BY rn",
            {"idx": idx, "start": int(start), "stop": int(stop)},
        )
        return [member for (member,) in rows]

    def sorted_range(
        self,
        idx: str,
        lo: float,
        hi: float,
        *,
        lo_inclusive: bool = True,
        hi_inclusive: bool = True,
        reverse: bool = False,
        limit: int | None = None,
    ) -> list[str]:
        lo_op = ">=" if lo_inclusive else ">"
        hi_op = "<=" if hi_inclusive else "<"
        order = "DESC" if reverse else "ASC"
        sql = (
            "SELECT member FROM popoto_sorted "
            f"WHERE idx = %s AND score {lo_op} %s AND score {hi_op} %s "
            f'ORDER BY score {order}, member COLLATE "C" {order}'
        )
        params: list[Any] = [idx, float(lo), float(hi)]
        # Same rule as the Redis backend: only a positive int bounds the read.
        if isinstance(limit, int) and limit > 0:
            sql += " LIMIT %s"
            params.append(limit)
        return [member for (member,) in self._query(sql, params)]

    def sorted_increment(
        self, idx: str, member: str, delta: float, *, uow: UnitOfWork | None = None
    ) -> float | None:
        delta = _check_score(delta)

        def op(cur: Any) -> Any:
            # One upsert: ON CONFLICT DO UPDATE locks the row it reads, so two
            # instances incrementing the same member serialise on it.
            row = cur.execute(
                "INSERT INTO popoto_sorted (idx, member, score) VALUES (%s, %s, %s) "
                "ON CONFLICT (idx, member) DO UPDATE "
                "SET score = popoto_sorted.score + EXCLUDED.score "
                "RETURNING score",
                (idx, member, delta),
            ).fetchone()
            new_score = float(row[0])
            if math.isnan(new_score):
                # Redis refuses ``inf + -inf``; raising here rolls the row back.
                raise ValueError("resulting score is not a number (NaN)")
            return new_score

        return self._run(op, uow)

    # -- G. Atomic swaps -----------------------------------------------------

    def swap_index(
        self,
        record_key: str,
        field: str,
        new_idx: str,
        value: bytes,
        *,
        unique: bool,
        legacy_old_idx: str = "",
        uow: UnitOfWork | None = None,
    ) -> Any:
        legacy_ptr_field = _legacy_pointer_field(field)
        new_bytes = bytes(value)
        # The record key, plus the target index when the value must be unique:
        # the check-then-claim below is a read-modify-write on *rows that may
        # not exist*, which ``FOR UPDATE`` cannot lock, so two instances
        # claiming one value for two records serialise on the index's advisory
        # lock and the second sees the first's row (see "Atomic swaps").
        locked = [record_key, new_idx] if unique else [record_key]

        def op(cur: Any) -> Any:
            _lock_record_keys(cur, locked)

            # -- validation phase: reads only, in INDEX_SWAP_LUA's order -----
            # 1. The pointer. The Lua reads the side key, then the pre-#540
            #    side key (no Postgres shape: nothing predates this backend),
            #    then the pre-#476 in-hash field, which can exist here when a
            #    record was imported with it, so it is honoured and scrubbed.
            old_idx = _pointer_one(cur, record_key, field)
            scrub_legacy = False
            if old_idx is None:
                legacy = cur.execute(
                    "SELECT value FROM popoto_record WHERE key = %s AND field = %s",
                    (record_key, legacy_ptr_field),
                ).fetchone()
                if legacy is not None:
                    old_idx = bytes(legacy[0]).decode(ENCODING)
                    scrub_legacy = True

            # 2. Idempotent re-save: pointer already names the new index and
            #    the member is in it -- rewrite the field bytes and stop.
            if old_idx == new_idx and _is_member(cur, new_idx, record_key):
                if scrub_legacy:
                    _scrub_legacy_pointer(cur, record_key, legacy_ptr_field)
                _upsert_field(cur, record_key, field, new_bytes)
                return 1

            # 3. Uniqueness: any member of the new index other than self is a
            #    conflict. Raised before any write, so the transaction -- and
            #    on the uow path the whole queue -- rolls back untouched.
            if unique:
                cur.execute(
                    "SELECT 1 FROM popoto_set WHERE idx = %s AND member <> %s LIMIT 1",
                    (new_idx, record_key),
                )
                if cur.fetchone() is not None:
                    raise ModelException(
                        _unique_conflict_message(record_key, field, new_idx)
                    )

            # -- mutation phase, all-or-nothing --------------------------------
            if scrub_legacy:
                _scrub_legacy_pointer(cur, record_key, legacy_ptr_field)
            # 4. Leave the old index: the pointer's if it named one, else the
            #    field layer's legacy hint for records that predate pointers.
            if old_idx:
                if old_idx != new_idx:
                    _remove_member(cur, old_idx, record_key)
            elif legacy_old_idx and legacy_old_idx != new_idx:
                _remove_member(cur, legacy_old_idx, record_key)
            # 5. Join the new index, repoint, write the field bytes.
            _add_member(cur, new_idx, record_key)
            _set_pointer(cur, record_key, field, [new_idx])
            _upsert_field(cur, record_key, field, new_bytes)
            return 1

        return self._run(op, uow)

    def drop_index_entry(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idx: str,
        uow: UnitOfWork | None = None,
    ) -> Any:
        legacy_ptr_field = _legacy_pointer_field(field)

        def op(cur: Any) -> Any:
            # Reads the pointer, so it runs before delete_record in the same
            # unit of work -- Model.delete queues the hooks first (#476). On
            # Redis the pointer GET happens when the method is *called* and
            # only the SREM/DEL are queued; here the read is inside the
            # transaction, which is the stronger of the two.
            _lock_record_keys(cur, [record_key])
            idx = _pointer_one(cur, record_key, field)
            if idx is None:
                # Migration fallback: the pre-#476 in-hash pointer field.
                legacy = cur.execute(
                    "SELECT value FROM popoto_record WHERE key = %s AND field = %s",
                    (record_key, legacy_ptr_field),
                ).fetchone()
                if legacy is not None:
                    idx = bytes(legacy[0]).decode(ENCODING) or None
            if idx is None:
                # No pointer anywhere: the field-value-derived index.
                idx = fallback_idx
            # SREM's reply, then the pointer's DEL.
            removed = _remove_member(cur, idx, record_key)
            _clear_pointer(cur, record_key, field)
            return removed

        return self._run(op, uow)

    def swap_tags(
        self,
        record_key: str,
        field: str,
        new_idxs: Sequence[str],
        value: bytes,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        new_bytes = bytes(value)
        wanted = list(dict.fromkeys(new_idxs))  # set semantics, first-seen order

        def op(cur: Any) -> Any:
            _lock_record_keys(cur, [record_key])
            # TAG_SWAP_LUA: previous membership from the pointer (its pre-#540
            # fallback has no Postgres shape), then the diff.
            old = _pointer_all(cur, record_key, field)
            new = set(wanted)
            for idx in sorted(old - new):
                _remove_member(cur, idx, record_key)
            for idx in wanted:
                if idx not in old:
                    _add_member(cur, idx, record_key)
            _set_pointer(cur, record_key, field, wanted)
            _upsert_field(cur, record_key, field, new_bytes)
            return 1

        return self._run(op, uow)

    def drop_tag_entries(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idxs: Sequence[str],
        uow: UnitOfWork | None = None,
    ) -> Any:
        def op(cur: Any) -> Any:
            _lock_record_keys(cur, [record_key])
            idxs = sorted(_pointer_all(cur, record_key, field))
            # ``fallback_idxs`` is consulted only here, after the pointer read
            # came back empty, and never materialised up front: the field
            # layer hands in a lazy sequence whose first use normalises the
            # in-memory value and may raise (#744 review, B1), exactly as the
            # Redis backend's ``if not set_keys and fallback_idxs`` defers it.
            if not idxs and fallback_idxs:
                idxs = list(fallback_idxs)
            for idx in idxs:
                _remove_member(cur, idx, record_key)
            # DEL's reply for the pointer: 1 when it existed, else 0.
            return _clear_pointer(cur, record_key, field)

        return self._run(op, uow)

    # -- H. Decay and confidence ---------------------------------------------

    def decayed_rank(
        self,
        idx: str,
        *,
        now: float,
        decay_rate: float,
        limit: int | None,
        base_score_field: str = "",
        confidence: tuple[str, float, float] | None = None,
        validity: tuple[str, str, float] | None = None,
        pretrim_max_ratio: float,
    ) -> list[Any]:
        raise _todo("decayed_rank")

    def confidence_update(
        self,
        idx: str,
        member: str,
        signal: float,
        *,
        initial: float,
        cap: int,
        require_record: str | None = None,
        uow: UnitOfWork | None = None,
    ) -> tuple[float, int, int, int] | None:
        raise _todo("confidence_update")

    # -- I. Validity ---------------------------------------------------------

    def supersede(
        self,
        model_prefix: str,
        field: str,
        *,
        mode: str,
        new_member: str,
        old_member: str = "",
        now: float,
        valid_from: float | None,
        ingested_at: float | None,
        close_at: float | None,
        assert_valid_from: bool,
        pointer_digest: str | None,
        uow: UnitOfWork | None = None,
    ) -> str | None:
        keys = _validity_keys(model_prefix, field)
        prefix = f"{model_prefix}:{field}"
        digest = pointer_digest or ""
        pointer_key = f"{prefix}:open:{digest}" if digest else ""
        # Deviation 2: ``now`` is the caller's clock and the default for every
        # instant the script would fill from ARGV[2]; never clock_timestamp().
        clock = float(now)
        start = _check_score(clock if valid_from is None else valid_from)
        ingest = _check_score(clock if ingested_at is None else ingested_at)
        close = _check_score(clock if close_at is None else close_at)
        # Serialise with every writer naming this identity's pointer, this
        # successor or this (asserted) incumbent; an incumbent resolved from
        # the pointer inside the function is covered by the pointer lock and
        # by the ``FOR UPDATE`` on its interval row.
        lock_keys = [k for k in (pointer_key, new_member or "", old_member or "") if k]
        params = (
            keys["valid_from"],
            keys["invalid_at"],
            keys["ingested_at"],
            prefix,
            digest,
            keys["chain_fwd"],
            keys["chain_rev"],
            new_member or "",
            mode,
            old_member or "",
            start,
            ingest,
            close,
            bool(assert_valid_from),
            lock_keys,
        )

        def op(cur: Any) -> Any:
            try:
                row = cur.execute(
                    "SELECT popoto_supersede("
                    "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
                    "%s::text[])",
                    params,
                ).fetchone()
            except Exception as e:
                if getattr(e, "sqlstate", None) != SUPERSEDE_ERRCODE:
                    raise
                # Function-local for the same reason as the Redis backend's:
                # the field module imports ``backends.redis`` at module scope
                # for the script text, so a module-scope import here would be
                # a cycle. One mapper, one table, both backends.
                from ..fields.validity_field import map_lua_error

                raise map_lua_error(_ScriptReply(_supersede_reply(e))) from e
            closed = row[0] or ""
            return closed or None

        return self._run(op, uow)

    def interval_of(
        self, valid_idx: str, invalid_idx: str, member: str
    ) -> tuple[float | None, float | None]:
        rows = self._query(
            "SELECT idx, score FROM popoto_sorted "
            "WHERE member = %s AND idx = ANY(%s)",
            (member, [valid_idx, invalid_idx]),
        )
        scores = {idx: float(score) for idx, score in rows}
        return (scores.get(valid_idx), scores.get(invalid_idx))

    def interval_members(
        self,
        valid_idx: str,
        invalid_idx: str,
        as_of: float,
        *,
        select: Literal["valid", "excluded"],
    ) -> set[str]:
        t = float(as_of)
        if select == "valid":
            # valid_from <= t AND invalid_at > t: the intersection.
            rows = self._query(
                "SELECT member FROM popoto_sorted WHERE idx = %s AND score <= %s "
                "INTERSECT "
                "SELECT member FROM popoto_sorted WHERE idx = %s AND score > %s",
                (valid_idx, t, invalid_idx, t),
            )
        else:
            # invalid_at <= t (already closed; the +inf open sentinel never
            # matches a finite t) OR valid_from > t (not yet started): the
            # union. A member in neither index is in neither set -- absent
            # means *included* for every retrieval gate.
            rows = self._query(
                "SELECT member FROM popoto_sorted "
                "WHERE (idx = %s AND score <= %s) OR (idx = %s AND score > %s)",
                (invalid_idx, t, valid_idx, t),
            )
        return {member for (member,) in rows}

    def drop_validity(
        self,
        model_prefix: str,
        field: str,
        member: str,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        keys = _validity_keys(model_prefix, field)
        prefix = f"{model_prefix}:{field}"
        interval_idxs = [keys["valid_from"], keys["invalid_at"], keys["ingested_at"]]
        chain_idxs = [keys["chain_fwd"], keys["chain_rev"]]

        def op(cur: Any) -> Any:
            # The member's own lock: a supersede naming it waits, so the three
            # deletes below cannot interleave with its NX re-open.
            _lock_record_keys(cur, [member])
            cur.execute(
                "DELETE FROM popoto_sorted WHERE member = %s AND idx = ANY(%s)",
                (member, interval_idxs),
            )
            cur.execute(
                "DELETE FROM popoto_map WHERE member = %s AND idx = ANY(%s)",
                (member, chain_idxs),
            )
            # Deviation 3: every pointer naming the member, found by value --
            # the Redis backend's ``{prefix}:open:*`` scan as one DELETE.
            cur.execute(
                "DELETE FROM popoto_open_ptr WHERE prefix = %s AND member = %s",
                (prefix, member),
            )
            # The Redis reply is the last pointer DEL's (1) or 0 when there
            # was none to delete.
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)

    def open_pointer(self, model_prefix: str, field: str, digest: str) -> str | None:
        rows = self._query(
            "SELECT member FROM popoto_open_ptr WHERE prefix = %s AND digest = %s",
            (f"{model_prefix}:{field}", digest),
        )
        return str(rows[0][0]) if rows else None

    # -- J. Orphan purge and maintenance ------------------------------------

    def purge_orphan(
        self,
        record_key: str,
        refs: Sequence[tuple[str, Literal["sorted", "set"]]],
        *,
        uow: UnitOfWork | None = None,
    ) -> int | None:
        sorted_idxs = [idx for idx, kind in refs if kind == "sorted"]
        set_idxs = [idx for idx, kind in refs if kind != "sorted"]

        def op(cur: Any) -> Any:
            # The record key's advisory lock first, so a save_record of the
            # same key in flight on another instance commits before the gate
            # below reads (and the gate then sees the record and purges
            # nothing), rather than racing it. Then one statement, one
            # snapshot: the EXISTS gate and both DELETEs see the same state,
            # which is the Lua's "only if its record is still gone" rule.
            _lock_record_keys(cur, [record_key])
            row = cur.execute(
                "WITH gone AS ("
                "  SELECT NOT EXISTS (SELECT 1 FROM popoto_record WHERE key = %(key)s)"
                "  AS ok"
                "), sorted_removed AS ("
                "  DELETE FROM popoto_sorted"
                "  WHERE member = %(key)s AND idx = ANY(%(sorted)s)"
                "    AND (SELECT ok FROM gone)"
                "  RETURNING 1"
                "), set_removed AS ("
                "  DELETE FROM popoto_set"
                "  WHERE member = %(key)s AND idx = ANY(%(sets)s)"
                "    AND (SELECT ok FROM gone)"
                "  RETURNING 1"
                ") SELECT (SELECT count(*) FROM sorted_removed)"
                "       + (SELECT count(*) FROM set_removed)",
                {"key": record_key, "sorted": sorted_idxs, "sets": set_idxs},
            ).fetchone()
            return int(row[0])

        return self._run(op, uow)

    def scan_index_members(
        self, idx: str, kind: Literal["sorted", "set"]
    ) -> Iterator[str]:
        # A generator like the Redis SSCAN/ZSCAN loop: nothing runs until the
        # first ``next()``, and a missing index yields nothing.
        table = _index_table(kind)
        rows = self._query(f"SELECT member FROM {table} WHERE idx = %s", (idx,))
        for (member,) in rows:
            yield member

    def drop_index(
        self,
        idx: str,
        kind: Literal["sorted", "set", "map"],
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        table = _index_table(kind)

        def op(cur: Any) -> Any:
            # DEL's reply: 1 when the index existed (had any row), else 0.
            cur.execute(f"DELETE FROM {table} WHERE idx = %s", (idx,))
            return 1 if cur.rowcount > 0 else 0

        return self._run(op, uow)
