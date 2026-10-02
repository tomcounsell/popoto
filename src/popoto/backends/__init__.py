"""Storage backend seam (#631).

Every persistence operation the agent-memory field layer performs is named here
once, as a method on the :class:`Backend` protocol, so that a second
implementation can run the same *semantics* -- decay ranking, supersession,
atomic index swaps -- on a store that is not Redis. :mod:`popoto.backends.redis`
is the Redis implementation (today's code, moved); :mod:`popoto.backends.postgres`
is the proof-of-concept target and, in WS0, a stub.

Reading convention (from ``docs/plans/sdlc-631.md``):

* *index* (``idx``) is an opaque string the field layer already computes --
  ``DB_key(...).redis_key``, e.g. ``Memory:_score:acme``. The protocol never
  parses it. On Redis it is the key verbatim; on Postgres it is one ``idx text``
  column.
* *uow* is the unit-of-work handle from :meth:`Backend.begin`. ``None`` means
  "execute now". Every method that takes ``uow`` queues its commands on it and
  returns ``None`` when one is given, because the reply only exists once
  :meth:`UnitOfWork.commit` runs. On Redis the unit of work *is* the
  ``GuardedPipeline`` (architect decision 1), so an external caller passing a
  real pipeline into ``save(pipeline=...)`` keeps working unchanged.

Backend selection and the stale-client trap
-------------------------------------------
Selection is **lazy**: nothing is chosen at import. The first
:func:`get_backend` call reads ``POSTGRES_URL``, tries ``import psycopg``, and
binds :class:`~popoto.backends.postgres.PostgresBackend` if both hold, else
:class:`~popoto.backends.redis.RedisBackend`. The result is cached in the
module global ``_BACKEND`` until :func:`set_backend` replaces it or
``set_backend(None)`` resets it. This is deliberately unlike ``REDIS_URL``,
which ``redis_db`` reads at import: ``backends`` is imported by the field
layer, and an import-time Postgres connection would make ``import popoto`` dial
a second database. A downstream user with ``REDIS_URL`` and no
``POSTGRES_URL`` sees no change.

This module never imports ``POPOTO_REDIS_DB`` by name, only the
``get_REDIS_DB``/``run_lua`` accessors (CLAUDE.md, #655), and
``src/popoto/__init__.py`` exports :func:`get_backend` and :func:`set_backend`
as functions, never ``_BACKEND`` -- a package-level copy of the cache would be a
snapshot that ``set_backend`` could never update (#651, one layer up).
"""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Iterator, Literal, Mapping, Protocol, Sequence

__all__ = ["Backend", "UnitOfWork", "get_backend", "set_backend"]


class UnitOfWork(Protocol):
    """A transaction handle returned by :meth:`Backend.begin`.

    On Redis this *is* the ``GuardedPipeline``: ``commit()`` is ``execute()``
    and returns the per-operation replies in queue order. On Postgres it is a
    transaction whose "validation phase then mutation phase" ordering becomes a
    real rollback. Usable as a context manager either way.
    """

    def commit(self) -> list[Any]: ...

    def __enter__(self) -> Any: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any: ...


class Backend(Protocol):
    """The 44 storage operations behind the agent-memory field layer.

    Grouped as the plan enumerates them: A unit of work (2), B records (9),
    C atomic increment (1), D side maps (4), E set indexes (7), F sorted
    indexes (7), G atomic swaps (4), H decay and confidence (2), I validity (5),
    J orphan purge and maintenance (3). Each docstring names the Lua script or
    raw commands the method replaces on Redis.

    The plan froze WS0 at 42. Two were added by ``feature/backend-seam-protocol-1``
    because WS1a could not route ``models/base.py``'s maintenance paths without
    them: :meth:`records_exist` (B) and :meth:`drop_index` (J). Both are marked
    ``protocol-1`` in their docstrings.
    """

    # -- A. Unit of work ---------------------------------------------------

    def begin(self) -> UnitOfWork:
        """Open a unit of work. Replaces ``get_REDIS_DB().pipeline()``."""
        ...

    def native(self) -> Any:
        """The raw client, for out-of-scope mixin code only (the escape hatch).

        Every call site carries a one-line comment naming the out-of-scope
        feature it serves, and each WS1 PR body ledgers them. On Redis this is
        the ``GuardedRedis`` client; on Postgres it raises
        ``NotImplementedError("<feature> is Redis-only in the backend-seam POC")``.
        """
        ...

    # -- B. Records ---------------------------------------------------------

    def save_record(
        self,
        key: str,
        fields: Mapping[Any, bytes],
        *,
        class_set: str,
        obsolete_key: str | None = None,
        ttl: int | None = None,
        expire_at: float | None = None,
        numeric: Mapping[str, float] | None = None,
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Write one record's encoded fields and register it in its class set.

        Replaces ``HSET key mapping`` + ``EXPIRE``/``EXPIREAT`` + ``SADD
        class_set key`` + the obsolete-key ``SREM``/``DEL`` on key migration
        (``Model.save``). ``fields`` are the per-field msgpack bytes
        ``encode_popoto_model_obj`` produces, keyed as it keys them; the
        backend treats them as opaque. ``numeric`` is the decoded float value
        of every ``IntField``/``FloatField``/``DecimalField`` so Postgres can
        keep a typed column; Redis ignores it. ``expire_at`` is epoch seconds.
        """
        ...

    def load_record(self, key: str) -> dict[Any, bytes] | None:
        """Replaces ``HGETALL`` (``Query.get``, ``load_raw_hash``, ``_hydrate``).

        ``None`` when the record does not exist. Field names come back exactly
        as stored (``bytes`` on Redis) so ``decode_popoto_model_hashmap`` sees
        what it sees today.
        """
        ...

    def load_records(self, keys: Sequence[str]) -> list[dict[Any, bytes] | None]:
        """Replaces pipelined ``HGETALL`` (``get_many_objects``, ``async_get_many``,
        ``top_by_decay``). One entry per key, ``None`` for a missing record."""
        ...

    def load_fields(self, key: str, names: Sequence[str]) -> list[bytes | None]:
        """Replaces ``HGET``/``HMGET`` (``Model.load_fields``, the ``values=`` path).

        One name issues ``HGET``, more issue ``HMGET`` -- a wire-parity
        requirement the original carries, not an optimization.
        """
        ...

    def record_exists(self, key: str) -> bool:
        """Replaces ``EXISTS`` (``Model.exists``, ``delete``, ``update_confidence``,
        orphan checks)."""
        ...

    def records_exist(self, keys: Sequence[str]) -> list[bool]:
        """Replaces pipelined ``EXISTS`` over a batch of keys (``check_indexes``,
        ``clean_indexes``, ``_classify_class_set_orphans``): one entry per key,
        in order. One round trip per batch where a loop of
        :meth:`record_exists` would be one per key -- the maintenance paths
        batch 1000 keys at a time, so this is a protocol method and not a
        convenience. ``protocol-1`` addition.
        """
        ...

    def delete_record(
        self, key: str, *, class_set: str, uow: UnitOfWork | None = None
    ) -> Any:
        """Replaces ``DEL key`` + ``SREM class_set key`` (``Model.delete``).

        Executed now, returns whether the record existed (the ``DEL`` reply as
        a bool); on a ``uow`` the commands are queued and ``None`` is returned.
        """
        ...

    def list_keys(self, class_set: str) -> set[str]:
        """Replaces ``SMEMBERS class_set`` (``Query.keys``, ``Query.all``)."""
        ...

    def count_records(self, class_set: str) -> int:
        """Replaces ``SCARD class_set`` (``Query.count``)."""
        ...

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
        """Replaces the inline Lua in ``Model.atomic_increment`` (``HGET``,
        msgpack-decode, add, re-encode, ``HSET``).

        The ``Decimal`` tagged-dict envelope stays inside the Redis backend.
        The companion ``ZINCRBY`` on the field's sorted index is
        :meth:`sorted_increment`, called by ``base.py`` after this.
        """
        ...

    # -- D. Side maps --------------------------------------------------------
    # One family serves composite unique indexes (``Meta.indexes``), confidence
    # payloads (``ConfidenceField``) and supersession chain links
    # (``chain_fwd``/``chain_rev``). On Postgres: ``popoto_map(idx, member,
    # value bytea)``.

    def map_get(self, idx: str, member: str) -> bytes | None:
        """Replaces ``HGET idx member``."""
        ...

    def map_set(
        self,
        idx: str,
        member: str,
        value: bytes,
        *,
        only_if_absent: bool = False,
        uow: UnitOfWork | None = None,
    ) -> bool | None:
        """Replaces ``HSET idx member value`` (``HSETNX`` when ``only_if_absent``).

        Executed now, returns whether a new entry was created."""
        ...

    def map_delete(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> int | None:
        """Replaces ``HDEL idx member``."""
        ...

    def map_scan(self, idx: str, pattern: str = "*") -> dict[str, bytes]:
        """Replaces the ``HSCAN idx MATCH pattern`` loop
        (``ConfidenceField.get_confidence_filtered``)."""
        ...

    # -- E. Set indexes ------------------------------------------------------

    def index_add(self, idx: str, member: str, *, uow: UnitOfWork | None = None) -> Any:
        """Replaces ``SADD idx member`` (``KeyFieldMixin.on_save``)."""
        ...

    def index_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        """Replaces ``SREM idx member`` (``KeyFieldMixin.on_save``/``on_delete``)."""
        ...

    def index_members(self, idx: str) -> set[str]:
        """Replaces ``SMEMBERS idx`` (exact-match and ``__isnull`` filters, the
        external-pipeline uniqueness pre-check)."""
        ...

    def index_union(self, idxs: Sequence[str]) -> set[str]:
        """Replaces ``SUNION`` (``__in`` filters, ``TagField __any``)."""
        ...

    def index_intersection(self, idxs: Sequence[str]) -> set[str]:
        """Replaces ``SINTER`` (``TagField __all``)."""
        ...

    def scan_index_names(self, pattern: str) -> list[str]:
        """Replaces ``scan_keys`` over index prefixes (``__startswith``,
        ``__endswith``, ``__isnull=False``)."""
        ...

    def scan_record_keys(self, pattern: str) -> list[str]:
        """Replaces ``scan_keys`` + ``TYPE`` filter (``_scan_hash_keys``,
        ``Query.keys(catchall=True)``, ``check_indexes``): only hash-typed keys
        survive, so a side key sharing the glob never reaches ``HGETALL``."""
        ...

    # -- F. Sorted indexes ---------------------------------------------------

    def sorted_add(
        self, idx: str, member: str, score: float, *, uow: UnitOfWork | None = None
    ) -> Any:
        """Replaces ``ZADD idx score member`` (``SortedFieldMixin.on_save``,
        ``Model.touch``)."""
        ...

    def sorted_remove(
        self, idx: str, member: str, *, uow: UnitOfWork | None = None
    ) -> Any:
        """Replaces ``ZREM idx member`` (``SortedFieldMixin.on_delete``)."""
        ...

    def sorted_score(self, idx: str, member: str) -> float | None:
        """Replaces ``ZSCORE idx member`` (``SortedFieldMixin.score``)."""
        ...

    def sorted_count(self, idx: str) -> int:
        """Replaces ``ZCARD idx`` (#634 ``SortedFieldMixin.count``)."""
        ...

    def sorted_members(
        self, idx: str, start: int = 0, stop: int = -1, *, reverse: bool = False
    ) -> list[str]:
        """Replaces ``ZRANGE``/``ZREVRANGE idx start stop`` (#634
        ``SortedFieldMixin.members``, ``filter_query(__current=...)``)."""
        ...

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
        """Replaces ``ZRANGEBYSCORE``/``ZREVRANGEBYSCORE`` including the ``(``
        exclusive-bound strings and ``-inf``/``+inf`` (``SortedFieldMixin.filter_query``).

        Bounds are floats plus inclusivity flags; ``float("inf")`` is legal.
        The Redis wire format is this backend's business, not a caller's.
        """
        ...

    def sorted_increment(
        self, idx: str, member: str, delta: float, *, uow: UnitOfWork | None = None
    ) -> float | None:
        """Replaces ``ZINCRBY idx delta member`` (``Model.atomic_increment``)."""
        ...

    # -- G. Atomic swaps -----------------------------------------------------
    # The ``$IdxPtr:``/``$TagPtr:`` side keys, their pre-#540 variants and the
    # pre-#476 in-hash pointer are Redis key-layout migration state. The Redis
    # backend derives all three from ``(record_key, field)``; Postgres needs
    # none, since the index row *is* the pointer.

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
        """Replaces ``INDEX_SWAP_LUA`` (``IndexedFieldMixin.on_save``).

        Raises ``ModelException`` on the ``POPOTO_UNIQUE_CONFLICT`` error
        reply. The external-pipeline uniqueness pre-check stays in the mixin as
        :meth:`index_members`.
        """
        ...

    def drop_index_entry(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idx: str,
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Replaces ``IndexedFieldMixin.on_delete``: ``GET ptr`` -> ``SREM`` ->
        ``DEL ptr, old_ptr``, falling back to ``fallback_idx`` (the
        field-value-derived index) when no pointer exists."""
        ...

    def swap_tags(
        self,
        record_key: str,
        field: str,
        new_idxs: Sequence[str],
        value: bytes,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Replaces ``TAG_SWAP_LUA`` (``TagFieldMixin.on_save``)."""
        ...

    def drop_tag_entries(
        self,
        record_key: str,
        field: str,
        *,
        fallback_idxs: Sequence[str],
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Replaces ``TagFieldMixin.on_delete``: ``SMEMBERS ptr`` -> ``SREM``
        each -> ``DEL ptr, old_ptr``, falling back to ``fallback_idxs``."""
        ...

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
        """Replaces ``DECAY_SCORE_LUA`` and the preceding ``ZCARD`` when
        ``limit is None`` (``DecayingSortedField.rank_decayed``).

        ``confidence`` is ``(hash_idx, s, c0)``; ``validity`` is
        ``(invalid_idx, valid_idx, as_of)``. The backend renders ``as_of`` with
        ``repr(float)`` so the range bound is bit-exact.

        WS0 returns the script's raw flat reply ``[member, score, ...]``
        undecoded (architect decision 4); WS1d may change it to typed pairs.
        """
        ...

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
        """Replaces ``CAPPED_BAYESIAN_UPDATE_LUA`` (``ConfidenceField.update_confidence``).

        ``require_record`` is the record key whose existence gates the update:
        on a ``uow`` it becomes the script's ``KEYS[2]`` guard; executed now it
        is an ``EXISTS`` round trip and an absent record returns ``None``.
        Returns ``(confidence, evidence_count, corroborations, contradictions)``.
        """
        ...

    # -- I. Validity ---------------------------------------------------------
    # ``model_prefix`` is the opaque ``$ValidityF:<Model>`` namespace and
    # ``field`` the field name; the six interval keys are derived from the pair.

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
        """Replaces ``SUPERSEDE_LUA`` + ``_LUA_ERROR_MAP``
        (``ValidityField.execute_supersede``).

        Raises ``ValidityMemberAbsentError`` / ``ValidityCloseBeforeStartError``
        / ``ValidityValidFromConflictError`` directly. Returns the closed
        member key, or ``None`` when nothing was closed.
        """
        ...

    def interval_of(
        self, valid_idx: str, invalid_idx: str, member: str
    ) -> tuple[float | None, float | None]:
        """Replaces the ``ZSCORE`` pair (``is_valid_at``, ``get_valid_from``,
        ``pre_save_validate``, ``chain``, ``_walk_links``). ``+inf`` is
        returned as ``float("inf")``."""
        ...

    def interval_members(
        self,
        valid_idx: str,
        invalid_idx: str,
        as_of: float,
        *,
        select: Literal["valid", "excluded"],
    ) -> set[str]:
        """Replaces the two ``ZRANGEBYSCORE`` calls in ``resolve_valid_keys``
        (intersection) and ``resolve_excluded_keys`` (union)."""
        ...

    def drop_validity(
        self,
        model_prefix: str,
        field: str,
        member: str,
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Replaces ``ValidityField.on_delete``: 3x ``ZREM`` + 2x ``HDEL`` +
        ``DEL`` of every open pointer naming ``member`` (today's
        ``find_open_pointers_for_member`` scan, done by the backend because the
        digest is opaque and there is no record -> digest reverse lookup)."""
        ...

    def open_pointer(self, model_prefix: str, field: str, digest: str) -> str | None:
        """Replaces ``GET {prefix}:open:{digest}``."""
        ...

    # -- J. Orphan purge and maintenance ------------------------------------

    def purge_orphan(
        self,
        record_key: str,
        refs: Sequence[tuple[str, Literal["sorted", "set"]]],
        *,
        uow: UnitOfWork | None = None,
    ) -> int | None:
        """Replaces ``PURGE_ORPHAN_LUA`` (``Model._purge_orphan_keys``): the
        ``'z'``/``'s'`` kind flags become the literal. Removes ``record_key``
        from each index in ``refs`` only if its record is still gone."""
        ...

    def scan_index_members(
        self, idx: str, kind: Literal["sorted", "set"]
    ) -> Iterator[str]:
        """Replaces ``SSCAN``/``ZSCAN`` (``check_indexes``, ``clean_indexes``,
        ``rebuild_indexes``)."""
        ...

    def drop_index(
        self,
        idx: str,
        kind: Literal["sorted", "set", "map"],
        *,
        uow: UnitOfWork | None = None,
    ) -> Any:
        """Replaces ``DEL idx`` on a whole index (``rebuild_indexes`` step 1,
        which drops every secondary index -- class set, key-field sets, sorted
        indexes, composite maps -- before reconstructing it from the records).

        ``kind`` names the table the index lives in on Postgres; Redis ignores
        it, since the key is the index whatever its type. ``protocol-1``
        addition.
        """
        ...


# -- Selection -----------------------------------------------------------------

_BACKEND: Backend | None = None


def _psycopg_importable() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    return True


def _select_backend() -> Backend:
    url = os.environ.get("POSTGRES_URL")
    if url and _psycopg_importable():
        from .postgres import PostgresBackend

        return PostgresBackend(url)
    from .redis import RedisBackend

    return RedisBackend()


def get_backend() -> Backend:
    """Return the process-wide backend, selecting it on first call.

    ``POSTGRES_URL`` set and ``psycopg`` importable selects
    :class:`~popoto.backends.postgres.PostgresBackend`; anything else selects
    :class:`~popoto.backends.redis.RedisBackend`. The choice is cached until
    :func:`set_backend` changes it. Never called at import time -- see the
    module docstring for why.
    """
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = _select_backend()
    return _BACKEND


def set_backend(backend: Backend | None) -> None:
    """Replace the cached backend, or reset the cache with ``None``.

    ``set_backend(None)`` makes the next :func:`get_backend` re-run selection;
    the WS2 conformance fixture uses it per session.
    """
    global _BACKEND
    _BACKEND = backend
