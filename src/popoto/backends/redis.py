"""The Redis backend: today's model/query storage code, moved (#759 M1a).

Every method body below was *moved* from ``models/base.py`` or
``models/query.py`` -- not rewritten -- so the field hooks, index sets, Lua
scripts and the bytes Redis is sent are exactly what they were. Each method's
docstring names where its body came from; ``scripts/trace_redis_wire.py`` is
the command-level proof (a RESP-serializer trace of the M1 surface, compared
byte for byte against ``main``).

Two rules carried from the #631 POC:

* **No stored client.** Every operation calls ``get_REDIS_DB()`` when it runs
  (CLAUDE.md, #655), so a ``RedisBackend`` created before
  ``set_REDIS_DB_settings()`` or the pytest plugin's DB swap still talks to
  the current database.
* **Keys stay the caller's objects.** A :class:`~popoto.backends.types.RecordId`
  carries the key the caller held (``str`` or ``bytes``) as ``native``, and
  this backend sends that object, so a ``bytes`` key from ``Query.keys()`` is
  the same wire token -- and the same Python type in ``source_redis_key`` --
  that it always was (the #751 ``str``-boundary lesson).

Protocol groups D-H are not routed through the backend on Redis in M1a: the
field and model code keeps calling its own implementation directly. Calling
one of them here raises :class:`BackendCapabilityError` naming the API that
serves it.
"""

from __future__ import annotations

import contextlib
from decimal import Decimal as _Decimal
from typing import Any, Iterator, Mapping, Optional, Sequence, Union

import redis

from ..fields.indexed_field_mixin import IndexedFieldMixin
from ..models.db_key import DB_key
from ..models.encoding import decode_lazy_field, encode_popoto_model_obj
from ..redis_db import ENCODING, get_REDIS_DB, run_lua
from .types import (
    BackendCapabilityError,
    Capabilities,
    Expiry,
    ModelSpec,
    QueryPlan,
    RecordId,
    Row,
    SaveOutcome,
    UnitOfWork,
)

__all__ = ["RedisBackend", "RedisHashRow", "RedisIdRow", "RedisResultRow"]


REDIS_CAPABILITIES = Capabilities(
    backend="redis",
    groups=frozenset("ABCDEFGH"),
    field_kinds=frozenset({"*"}),
    key_migration=True,
    record_ttl=True,
)


# -- rows ---------------------------------------------------------------------


class RedisHashRow(Mapping[str, Any]):
    """A ``load`` row: the record's hash exactly as Redis returned it.

    ``raw`` is what ``HGETALL`` (or the ``HGET``/``HMGET`` projection) replied,
    undecoded; the query layer hands it to ``decode_popoto_model_hashmap``
    unchanged, which is what keeps hydration identical. The ``Mapping`` view
    decodes field values on access, for code that wants the protocol's decoded
    ``Row`` shape.
    """

    __slots__ = ("id", "raw")

    def __init__(self, id: RecordId, raw: Mapping[Any, Any]) -> None:
        self.id = id
        self.raw = raw

    def __getitem__(self, name: str) -> Any:
        if name == "_id":
            return self.id
        for key in (name, name.encode(ENCODING)):
            if key in self.raw:
                value = self.raw[key]
                return decode_lazy_field(value) if isinstance(value, bytes) else value
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        yield "_id"
        for key in self.raw:
            yield key.decode(ENCODING) if isinstance(key, bytes) else key

    def __len__(self) -> int:
        return 1 + len(self.raw)


class RedisIdRow(Mapping[str, Any]):
    """A ``select(project=())`` row: one class-set member, kept as Redis
    returned it (``bytes``). ``["_id"]`` builds the :class:`RecordId` lazily,
    so a large ``keys()`` pays for no decoding it does not use."""

    __slots__ = ("model", "key")

    def __init__(self, model: str, key: Any) -> None:
        self.model = model
        self.key = key

    def __getitem__(self, name: str) -> Any:
        if name == "_id":
            return RecordId.from_key(self.model, self.key)
        raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        yield "_id"

    def __len__(self) -> int:
        return 1


class RedisResultRow(Mapping[str, Any]):
    """A ``select`` row: the hydrated result today's filter code produced --
    a model instance, or a ``values=`` projection dict -- which the query layer
    returns unchanged. The ``Mapping`` view reads its fields; ``_id`` is the
    instance's key, or ``None`` for a projection dict (which has never carried
    its key on Redis)."""

    __slots__ = ("result",)

    def __init__(self, result: Any) -> None:
        self.result = result

    def _names(self) -> list[str]:
        if isinstance(self.result, dict):
            return list(self.result)
        return list(self.result._meta.fields)

    def __getitem__(self, name: str) -> Any:
        result = self.result
        if name == "_id":
            if isinstance(result, dict):
                return None
            key = getattr(result, "_redis_key", None) or result.db_key.redis_key
            return RecordId.from_key(result._meta.model_name, key)
        if name not in self._names():
            raise KeyError(name)
        return result[name] if isinstance(result, dict) else getattr(result, name)

    def __iter__(self) -> Iterator[str]:
        yield "_id"
        yield from self._names()

    def __len__(self) -> int:
        return 1 + len(self._names())


def _pipeline_of(uow: Optional[UnitOfWork]) -> Any:
    """The caller's pipeline inside a unit of work, or ``None``."""
    return uow.pipeline if uow is not None else None


class RedisBackend:
    """The Redis implementation of :class:`popoto.backends.Backend`.

    Stateless: it holds no client and no per-model state, so the one instance
    ``get_backend()`` caches is safe across DB swaps and threads.
    """

    name = "redis"

    # -- A. lifecycle ------------------------------------------------------

    def bind(self, spec: ModelSpec) -> Capabilities:
        """No-op on Redis: there is no schema, and the connection is the
        shared ``get_REDIS_DB()`` client. Returns the full capability set."""
        return REDIS_CAPABILITIES

    @contextlib.contextmanager
    def transaction(self) -> Iterator[UnitOfWork]:
        """A ``MULTI``/``EXEC`` pipeline (what ``popoto.batch()`` opens),
        executed on a clean exit and discarded on an exception. Not routed in
        M1a: ``batch()`` and the ``pipeline=`` kwargs keep their own path."""
        uow = UnitOfWork(get_REDIS_DB().pipeline(), backend="redis")
        try:
            yield uow
        except BaseException:
            uow.pipeline.reset()
            raise
        uow.commit()

    def close(self) -> None:
        """Nothing to close: the connection pool belongs to ``redis_db``."""

    # -- B. records --------------------------------------------------------

    def save(
        self,
        obj: Any,
        *,
        fields: Optional[Sequence[str]] = None,
        previous_id: Optional[RecordId] = None,
        expiry: Optional[Expiry] = None,
        uow: Optional[UnitOfWork] = None,
        ignore_errors: bool = False,
        **hook_kwargs: Any,
    ) -> SaveOutcome:
        """Moved from ``Model.save``: everything after the never-record
        firewall, the write filter, ``pre_save`` and ``pre_save_validate``,
        which stay in the model. The write-filter priority tag and the
        event-stream ``XADD`` also stay there, issued right after this returns
        -- the same position they had at the end of each path.

        ``HSET`` msgpack, ``$Class:`` ``SADD``, the field ``on_save`` hooks
        (indexed fields run eagerly on the internal path, #476), ``EXPIRE`` --
        in today's order on each of the four paths (full or partial, caller's
        pipeline or internal), including the partial path's "``SADD`` only on
        key migration" and "``EXPIRE`` after the hooks" (#735 review B1/B2).
        ``previous_id`` is informational here: the obsolete key is the
        instance's ``_redis_key``, as it always was. ``expiry`` must be
        ``None`` (the instance's own TTL settings); per-call expiry is M5.
        Options: ``ignore_errors`` and the hook ``**kwargs``.
        """
        if expiry is not None:
            raise BackendCapabilityError(
                "the backend save takes no per-call expiry; set Meta.ttl or the "
                "instance's _ttl/_expire_at"
            )
        update_fields = fields
        pipeline = _pipeline_of(uow)
        _validate_names = (
            update_fields if update_fields is not None else obj._meta.fields.keys()
        )
        new_db_key = DB_key(obj.db_key)  # todo: why have a new key??
        saved_id = RecordId.from_key(obj._meta.model_name, new_db_key.redis_key)

        if update_fields is not None:
            # Partial save path: only write listed fields to Redis
            from ..redis_db import ENCODING

            # Detect obsolete key: if any updated field is a KeyField,
            # the db_key may have changed. We need to clean up the old
            # key's hash, class set entry, and index entries.
            obsolete_key = None
            if obj._redis_key != new_db_key.redis_key:
                obsolete_key = obj._redis_key

            # Encode all fields, then filter to only update_fields.
            # Exclude IndexedFieldMixin fields — EVAL (INDEX_SWAP_LUA) owns their
            # hash writes atomically, so the plain HSET must not race with them.
            full_mapping = encode_popoto_model_obj(obj)
            # #573: post-encode, pre-write. The encode above is what creates
            # the quarantine on a lazy instance, and _validate_names is
            # update_fields here, so only a poisoned field that this partial
            # save would actually write blocks it.
            obj._raise_if_quarantine_blocks(_validate_names)
            update_field_names_bytes = {
                field_name.encode(ENCODING) for field_name in update_fields
            }
            indexed_field_names_bytes = {
                field_name.encode(ENCODING)
                for field_name, field in obj._meta.fields.items()
                if isinstance(field, IndexedFieldMixin)
            }
            hset_mapping = {
                k: v
                for k, v in full_mapping.items()
                if k in update_field_names_bytes and k not in indexed_field_names_bytes
            }

            if isinstance(pipeline, redis.client.Pipeline):
                if hset_mapping:
                    pipeline = pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)
                # else: EVAL-only path — indexed field EVALs write the hash fields
                # If db_key changed, clean up the obsolete key
                if obsolete_key and obsolete_key != new_db_key.redis_key:
                    # Remove old index entries using saved field values
                    for field_name, field in obj._meta.fields.items():
                        field_value = obj._saved_field_values.get(
                            field_name, getattr(obj, field_name)
                        )
                        pipeline = field.on_delete(
                            model_instance=obj,
                            field_name=field_name,
                            field_value=field_value,
                            pipeline=pipeline,
                            saved_redis_key=obsolete_key,
                            **hook_kwargs,
                        )
                    # Delete old hash and update class set
                    pipeline.delete(obsolete_key)
                    pipeline.srem(obj._meta.db_class_set_key.redis_key, obsolete_key)
                    pipeline.sadd(
                        obj._meta.db_class_set_key.redis_key,
                        new_db_key.redis_key,
                    )
                # Run on_save for listed fields (adds new index entries)
                for field_name in update_fields:
                    field = obj._meta.fields[field_name]
                    pipeline = field.on_save(
                        obj,
                        field_name=field_name,
                        field_value=getattr(obj, field_name),
                        ignore_errors=ignore_errors,
                        pipeline=pipeline,
                        **hook_kwargs,
                    )
                # Handle TTL/expire_at
                if obj._ttl is not None:
                    pipeline = pipeline.expire(new_db_key.redis_key, obj._ttl)
                elif obj._expire_at is not None:
                    pipeline = pipeline.expireat(
                        new_db_key.redis_key, int(obj._expire_at.timestamp())
                    )
                obj._redis_key = new_db_key.redis_key
                # Merge into saved_field_values (preserve existing, update listed)
                for field_name in update_fields:
                    obj._saved_field_values[field_name] = getattr(obj, field_name)
                uow.pipeline = pipeline  # type: ignore[union-attr]
                return SaveOutcome(id=saved_id, result=pipeline)
            else:
                # Run indexed/unique fields in update_fields EAGERLY (own
                # atomic EVAL, pipeline=None) before the internal pipeline
                # for everything else is built and executed — same #476
                # "unique-conflict window" fix as the full-save path below.
                _eager_indexed_update_fields = [
                    field_name
                    for field_name in update_fields
                    if isinstance(obj._meta.fields[field_name], IndexedFieldMixin)
                ]
                for field_name in _eager_indexed_update_fields:
                    field = obj._meta.fields[field_name]
                    field.on_save(
                        obj,
                        field_name=field_name,
                        field_value=getattr(obj, field_name),
                        ignore_errors=ignore_errors,
                        pipeline=None,
                        **hook_kwargs,
                    )

                # Use internal pipeline for atomic execution
                internal_pipeline = get_REDIS_DB().pipeline()
                if hset_mapping:
                    internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)
                # else: EVAL-only path — indexed field EVALs write the hash fields
                # If db_key changed, clean up the obsolete key
                if obsolete_key and obsolete_key != new_db_key.redis_key:
                    # Remove old index entries using saved field values
                    for field_name, field in obj._meta.fields.items():
                        field_value = obj._saved_field_values.get(
                            field_name, getattr(obj, field_name)
                        )
                        field.on_delete(
                            model_instance=obj,
                            field_name=field_name,
                            field_value=field_value,
                            pipeline=internal_pipeline,
                            saved_redis_key=obsolete_key,
                            **hook_kwargs,
                        )
                    # Delete old hash and update class set
                    internal_pipeline.delete(obsolete_key)
                    internal_pipeline.srem(
                        obj._meta.db_class_set_key.redis_key, obsolete_key
                    )
                    internal_pipeline.sadd(
                        obj._meta.db_class_set_key.redis_key,
                        new_db_key.redis_key,
                    )
                # Run on_save for listed fields (adds new index entries)
                for field_name in update_fields:
                    if field_name in _eager_indexed_update_fields:
                        continue  # already written + indexed atomically above
                    field = obj._meta.fields[field_name]
                    field.on_save(
                        obj,
                        field_name=field_name,
                        field_value=getattr(obj, field_name),
                        ignore_errors=ignore_errors,
                        pipeline=internal_pipeline,
                        **hook_kwargs,
                    )
                # Handle TTL/expire_at
                if obj._ttl is not None:
                    internal_pipeline.expire(new_db_key.redis_key, obj._ttl)
                elif obj._expire_at is not None:
                    internal_pipeline.expireat(
                        new_db_key.redis_key, int(obj._expire_at.timestamp())
                    )
                results = internal_pipeline.execute()
                # When hset_mapping is non-empty, results[0] is the HSET count.
                # When EVAL-only (all fields are indexed), hset_mapping is empty so
                # results[0] is the first queued pipeline op result (expire/sadd), still an int.
                db_response = results[0] if results else 0
                obj._is_persisted = True
                obj._redis_key = new_db_key.redis_key
                # Merge into saved_field_values (preserve existing, update listed)
                for field_name in update_fields:
                    obj._saved_field_values[field_name] = getattr(obj, field_name)
                return SaveOutcome(id=saved_id, result=db_response)

        # Full save path (existing behavior, unchanged)
        if obj._redis_key != new_db_key.redis_key:
            obj.obsolete_redis_key = obj._redis_key

        # todo: implement and test tll, expire_at
        # ttl, expire_at = (ttl or obj._ttl), (expire_at or obj._expire_at)

        """
        1. save object as hashmap
        2. optionally set ttl, expire_at
        3. add to class set
        4. if obsolete key, delete and run field on_delete methods
        5. run field on_save methods
        6. save private version of compiled db key
        """

        hset_mapping = encode_popoto_model_obj(obj)  # 1
        # #573: post-encode, pre-write. encode_popoto_model_obj's getattr loop
        # is the first access on a lazy instance, so the quarantine only
        # exists now; no Redis command has been issued yet.
        obj._raise_if_quarantine_blocks(_validate_names)
        obj._db_content = hset_mapping  # 1
        # Exclude IndexedFieldMixin fields — EVAL (INDEX_SWAP_LUA) owns their
        # hash writes atomically, so the plain HSET must not race with them.
        from ..redis_db import ENCODING as _ENCODING

        _indexed_field_names_bytes = {
            field_name.encode(_ENCODING)
            for field_name, field in obj._meta.fields.items()
            if isinstance(field, IndexedFieldMixin)
        }
        hset_mapping = {
            k: v for k, v in hset_mapping.items() if k not in _indexed_field_names_bytes
        }

        if isinstance(pipeline, redis.client.Pipeline):
            if hset_mapping:
                pipeline = pipeline.hset(
                    new_db_key.redis_key, mapping=hset_mapping
                )  # 1
            if obj._ttl is not None:
                pipeline = pipeline.expire(new_db_key.redis_key, obj._ttl)  # 2
            elif obj._expire_at is not None:
                pipeline = pipeline.expireat(
                    new_db_key.redis_key, int(obj._expire_at.timestamp())
                )  # 2
            pipeline = pipeline.sadd(
                obj._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3
            if (
                obj.obsolete_redis_key
                and obj.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in obj._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = obj._saved_field_values.get(
                        field_name, getattr(obj, field_name)
                    )
                    pipeline = field.on_delete(  # 4
                        model_instance=obj,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=pipeline,
                        saved_redis_key=obj.obsolete_redis_key,
                        **hook_kwargs,
                    )
                pipeline = pipeline.srem(
                    obj._meta.db_class_set_key.redis_key,
                    obj.obsolete_redis_key,
                )  # 4a - remove old key from class set
                pipeline.delete(obj.obsolete_redis_key)  # 4b
                obj.obsolete_redis_key = None
            for field_name, field in obj._meta.fields.items():  # 5
                pipeline = field.on_save(  # 5
                    obj,
                    field_name=field_name,
                    field_value=getattr(obj, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=pipeline,
                    **hook_kwargs,
                )
            # Manage indexes  # 6
            for field_names, is_unique in obj._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = obj._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if obj._saved_field_values:
                    old_hash = obj._meta.compute_index_hash_from_values(
                        field_names_tuple, obj._saved_field_values
                    )
                    if old_hash:
                        pipeline = pipeline.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = obj._meta.compute_index_hash(obj, field_names_tuple)
                if new_hash:
                    pipeline = pipeline.hset(index_key, new_hash, new_db_key.redis_key)
            obj._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            obj._saved_field_values = {
                field_name: getattr(obj, field_name)
                for field_name in obj._meta.fields.keys()
            }
            uow.pipeline = pipeline  # type: ignore[union-attr]
            return SaveOutcome(id=saved_id, result=pipeline)

        else:
            # Run indexed/unique field on_save() EAGERLY — each as its own
            # atomic Lua EVAL executed directly against POPOTO_REDIS_DB
            # (pipeline=None) — BEFORE the internal pipeline below is built
            # and executed. See #476 ("unique-conflict window"): Redis's
            # MULTI/EXEC does NOT roll back other queued commands when one
            # command in the transaction errors, so if these EVAL calls were
            # instead queued into internal_pipeline (as before), a uniqueness
            # conflict would still leave the base HSET / class-set SADD /
            # index bookkeeping committed, orphaning a hash with no index
            # entry pointing at it. Raising here, before internal_pipeline
            # even exists, guarantees nothing else for this save is written
            # when an indexed/unique field genuinely conflicts.
            _eager_indexed_fields = {
                field_name: field
                for field_name, field in obj._meta.fields.items()
                if isinstance(field, IndexedFieldMixin)
            }
            for field_name, field in _eager_indexed_fields.items():
                field.on_save(
                    obj,
                    field_name=field_name,
                    field_value=getattr(obj, field_name),
                    ignore_errors=ignore_errors,
                    pipeline=None,  # type: ignore[arg-type]  # eager path, #476
                    **hook_kwargs,
                )

            # Use internal pipeline for atomic execution of everything else
            internal_pipeline = get_REDIS_DB().pipeline()

            if hset_mapping:
                internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)  # 1
            if obj._ttl is not None:
                internal_pipeline.expire(new_db_key.redis_key, obj._ttl)  # 2
            elif obj._expire_at is not None:
                internal_pipeline.expireat(
                    new_db_key.redis_key, int(obj._expire_at.timestamp())
                )  # 2
            internal_pipeline.sadd(
                obj._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3

            if (
                obj.obsolete_redis_key
                and obj.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in obj._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = obj._saved_field_values.get(
                        field_name, getattr(obj, field_name)
                    )
                    field.on_delete(  # 4
                        model_instance=obj,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=internal_pipeline,
                        saved_redis_key=obj.obsolete_redis_key,
                        **hook_kwargs,
                    )
                internal_pipeline.srem(
                    obj._meta.db_class_set_key.redis_key,
                    obj.obsolete_redis_key,
                )  # 4a - remove old key from class set
                internal_pipeline.delete(obj.obsolete_redis_key)  # 4b
                obj.obsolete_redis_key = None

            for field_name, field in obj._meta.fields.items():  # 5
                if field_name in _eager_indexed_fields:
                    continue  # already written + indexed atomically above
                field.on_save(  # 5
                    obj,
                    field_name=field_name,
                    field_value=getattr(obj, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=internal_pipeline,
                    **hook_kwargs,
                )

            # Manage indexes  # 6
            for field_names, is_unique in obj._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = obj._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if obj._saved_field_values:
                    old_hash = obj._meta.compute_index_hash_from_values(
                        field_names_tuple, obj._saved_field_values
                    )
                    if old_hash:
                        internal_pipeline.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = obj._meta.compute_index_hash(obj, field_names_tuple)
                if new_hash:
                    internal_pipeline.hset(index_key, new_hash, new_db_key.redis_key)

            # pipeline.execute() is synchronous in redis-py: it blocks until
            # all responses are received, guaranteeing write visibility after return
            results = internal_pipeline.execute()
            # When hset_mapping is non-empty, results[0] is the HSET count.
            # When EVAL-only (all fields are indexed), hset_mapping is empty so
            # results[0] is the first queued pipeline op result (expire/sadd), still an int.
            db_response = results[0] if results else 0  # HSET result (backward compat)

            obj._is_persisted = True
            obj._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            obj._saved_field_values = {
                field_name: getattr(obj, field_name)
                for field_name in obj._meta.fields.keys()
            }
            return SaveOutcome(id=saved_id, result=db_response)

    def load(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        fields: Optional[Sequence[str]] = None,
        pipelined: bool = True,
        **options: Any,
    ) -> list[Optional[Row]]:
        """Moved from ``Query.get`` (bare ``HGETALL``), ``Query.get_many`` and
        ``Query.get_many_objects`` (pipelined ``HGETALL``, or ``HMGET`` for a
        ``values=`` projection) and ``Model.load_fields`` (bare ``HGET`` for
        one field, ``HMGET`` for several).

        ``pipelined=False`` is the single-record caller's shape and takes one
        id. A row is ``None`` where ``HGETALL`` replied with an empty hash.
        With ``fields=`` every id gets a row, as today: ``HMGET`` cannot tell
        a missing record from missing fields, and the projection decoder has
        always received the all-``None`` mapping.
        """
        if not pipelined:
            (rid,) = ids
            if fields is None:
                hashmap = get_REDIS_DB().hgetall(rid.key)
                return [RedisHashRow(rid, hashmap) if hashmap else None]
            names = list(fields)
            if len(names) == 1:
                raw_values: Any = [get_REDIS_DB().hget(rid.key, names[0])]
            else:
                raw_values = get_REDIS_DB().hmget(rid.key, names)
            return [RedisHashRow(rid, dict(zip(names, raw_values)))]

        pipeline = get_REDIS_DB().pipeline()
        if fields is not None:
            values = fields
            [pipeline.hmget(rid.key, values) for rid in ids]
            value_lists = pipeline.execute()
            return [
                RedisHashRow(
                    rid, {field_name: result[i] for i, field_name in enumerate(values)}
                )
                for rid, result in zip(ids, value_lists)
            ]
        [pipeline.hgetall(rid.key) for rid in ids]
        hashes_list = pipeline.execute()
        return [
            RedisHashRow(rid, hashmap) if hashmap else None
            for rid, hashmap in zip(ids, hashes_list)
        ]

    def delete(
        self,
        spec: ModelSpec,
        ids: Sequence[RecordId],
        *,
        uow: Optional[UnitOfWork] = None,
        objs: Optional[Sequence[Any]] = None,
        **hook_kwargs: Any,
    ) -> int:
        """Moved from ``Model.delete``: the existence check first, the field
        ``on_delete`` hooks while the hash still exists (#476), then ``DEL``
        and ``SREM``, ``Meta.indexes`` cleanup and the access-tracker,
        write-filter and event-stream keys, in one pipeline. Needs the
        instances (``objs``, aligned with ``ids``), because the hooks read
        their saved values. Returns how many existed; ``0`` when queued on
        ``uow``. Options: the hook ``**kwargs``.
        """
        if objs is None or len(objs) != len(ids):
            raise BackendCapabilityError(
                "RedisBackend.delete needs objs=, aligned with ids: the field "
                "on_delete hooks clean the indexes from each instance's saved "
                "values"
            )
        if len(ids) != 1:
            return sum(
                self.delete(spec, [rid], uow=uow, objs=[o], **hook_kwargs)
                for rid, o in zip(ids, objs)
            )
        obj = objs[0]
        delete_redis_key = ids[0].key
        pipeline = _pipeline_of(uow)

        if pipeline:
            result_pipeline = pipeline
            existed = None  # unknown/unused: caller executes and owns the result
        else:
            # #476: determine existence BEFORE any mutation, and run field
            # on_delete() hooks BEFORE the model hash is physically removed.
            # IndexedFieldMixin.on_delete() reads live pointer state (the
            # #476 side key, or — for records written before that fix
            # shipped — the legacy in-hash pointer field) to find the exact
            # index Set to SREM from. Deleting the hash first (the previous
            # order here) meant that read always came back empty for the
            # legacy in-hash pointer, silently falling back to a possibly
            # stale _saved_field_values snapshot and risking an orphaned
            # index member pointing at an already-deleted hash.
            existed = bool(get_REDIS_DB().exists(delete_redis_key))
            result_pipeline = get_REDIS_DB().pipeline()

        for field_name, field in obj._meta.fields.items():  # 3
            # Use saved field values if available, otherwise fall back to current values
            # This ensures we clean up the correct Redis keys even if field values changed
            field_value = obj._saved_field_values.get(
                field_name, getattr(obj, field_name)
            )
            result_pipeline = field.on_delete(
                model_instance=obj,
                field_name=field_name,
                field_value=field_value,
                pipeline=result_pipeline,
                saved_redis_key=delete_redis_key,
                **hook_kwargs,
            )

        result_pipeline = result_pipeline.delete(delete_redis_key)  # 1
        result_pipeline = result_pipeline.srem(
            obj._meta.db_class_set_key.redis_key, delete_redis_key
        )  # 2
        pipeline = result_pipeline

        # Clean up indexes  # 4
        cleanup_values = obj._saved_field_values or {
            field_name: getattr(obj, field_name)
            for field_name in obj._meta.fields.keys()
        }
        for field_names, is_unique in obj._meta.indexes:
            field_names_tuple = tuple(field_names)
            index_key = obj._meta.get_index_key(field_names_tuple)
            index_hash = obj._meta.compute_index_hash_from_values(
                field_names_tuple, cleanup_values
            )
            if index_hash:
                pipeline = pipeline.hdel(index_key, index_hash)

        # Clean up AccessTrackerMixin keys if applicable
        from ..fields.access_tracker import AccessTrackerMixin

        if isinstance(obj, AccessTrackerMixin):
            obj._delete_access_tracker_keys(pipeline=pipeline)

        # Clean up WriteFilterMixin keys if applicable
        from ..fields.write_filter import WriteFilterMixin

        if isinstance(obj, WriteFilterMixin):
            obj._delete_write_filter_keys(pipeline=pipeline)

        # EventStreamMixin: log delete mutation
        from ..fields.event_stream import EventStreamMixin

        if isinstance(obj, EventStreamMixin):
            obj._xadd_mutation("delete", pipeline=pipeline)

        obj._db_content = dict()  # 6
        obj._saved_field_values = dict()  # 6

        if existed is not None:
            pipeline.execute()
            return int(existed)
        else:
            uow.pipeline = pipeline  # type: ignore[union-attr]
            return 0

    def exists(self, spec: ModelSpec, ids: Sequence[RecordId]) -> list[bool]:
        """Moved from ``Model.exists``: one bare ``EXISTS`` for one id (today's
        shape); a pipelined ``EXISTS`` per id for several."""
        if len(ids) == 1:
            return [bool(get_REDIS_DB().exists(ids[0].key))]
        pipeline = get_REDIS_DB().pipeline()
        for rid in ids:
            pipeline.exists(rid.key)
        return [bool(n) for n in pipeline.execute()]

    def increment(
        self,
        spec: ModelSpec,
        id: RecordId,
        field: str,
        delta: Union[int, float],
        *,
        uow: Optional[UnitOfWork] = None,
        obj: Any = None,
        **options: Any,
    ) -> Any:
        """Moved from ``Model.atomic_increment``, after its validation (which
        stays in the model): the inline msgpack Lua, ``ZINCRBY`` for a sorted
        field, and the instance's in-memory value. Needs the instance
        (``obj``). With ``uow`` the commands are queued and the pipeline is
        returned, as today; otherwise the new value."""
        if obj is None:
            raise BackendCapabilityError(
                "RedisBackend.increment needs obj=: the instance's in-memory "
                "value and sorted index follow the increment"
            )
        field_name = field
        field_obj = obj._meta.fields[field_name]
        new_val: Union[int, float, _Decimal]
        redis_key = id.key
        pipeline = _pipeline_of(uow)
        field_name_bytes = field_name.encode(ENCODING)

        # Lua script that atomically reads, decodes msgpack, increments,
        # re-encodes, and writes back. Uses cmsgpack which is built into
        # Redis since version 2.6.
        #
        # KEYS[1] = redis hash key
        # ARGV[1] = field name (bytes)
        # ARGV[2] = delta value (string representation)
        # ARGV[3] = 1 if field is Decimal type (uses tagged dict encoding), 0 otherwise
        #
        # Returns the new numeric value as a string.
        lua_script = """
        local current_packed = redis.call('HGET', KEYS[1], ARGV[1])
        local current_val = 0
        local is_decimal = tonumber(ARGV[3])

        if current_packed then
            local decoded = cmsgpack.unpack(current_packed)
            if is_decimal == 1 and type(decoded) == 'table' and decoded['as_encodable'] then
                current_val = tonumber(decoded['as_encodable'])
            elseif type(decoded) == 'number' then
                current_val = decoded
            end
        end

        local delta = tonumber(ARGV[2])
        local new_val = current_val + delta

        if is_decimal == 1 then
            local encoded = cmsgpack.pack({['__Decimal__'] = true, ['as_encodable'] = tostring(new_val)})
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        else
            local encoded = cmsgpack.pack(new_val)
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        end

        return tostring(new_val)
        """

        is_decimal = 1 if field_obj.type is _Decimal else 0
        delta_str = str(float(delta) if isinstance(delta, _Decimal) else delta)

        if isinstance(pipeline, redis.client.Pipeline):
            # When using a pipeline, register the script and call it
            script = get_REDIS_DB().register_script(lua_script)
            pipeline = script(
                keys=[redis_key],
                args=[field_name_bytes, delta_str, is_decimal],
                client=pipeline,
            )

            # Update in-memory values optimistically
            current_val = getattr(obj, field_name) or field_obj.type()
            if field_obj.type is int:
                new_val = int(current_val) + int(delta)
            elif field_obj.type is _Decimal:
                new_val = _Decimal(str(current_val)) + _Decimal(str(delta))
            else:
                new_val = float(current_val) + float(delta)

            setattr(obj, field_name, new_val)
            if field_name in obj._saved_field_values or obj._saved_field_values:
                obj._saved_field_values[field_name] = new_val

            # Update sorted index if field is a SortedField
            if field_name in obj._meta.sorted_field_names:
                field_cls = field_obj.__class__
                sortedset_db_key = field_cls.get_partitioned_sortedset_db_key(
                    obj, field_name
                )
                score_delta = float(delta) if isinstance(delta, _Decimal) else delta
                pipeline = pipeline.zincrby(
                    sortedset_db_key.redis_key, score_delta, redis_key
                )

            uow.pipeline = pipeline  # type: ignore[union-attr]
            return pipeline
        else:
            # Execute the Lua script directly
            result_str = run_lua(
                get_REDIS_DB(),
                lua_script,
                1,
                redis_key,
                field_name_bytes,
                delta_str,
                is_decimal,
            )

            # Parse result and convert to field type
            if isinstance(result_str, bytes):
                result_str = result_str.decode(ENCODING)

            if field_obj.type is int:
                # Lua may return "15.0" for integer arithmetic; parse via float then int
                new_val = int(float(result_str))
            elif field_obj.type is _Decimal:
                new_val = _Decimal(result_str)
            else:
                new_val = float(result_str)

            # Update in-memory instance
            setattr(obj, field_name, new_val)
            if field_name in obj._saved_field_values or obj._saved_field_values:
                obj._saved_field_values[field_name] = new_val

            # Update sorted index if field is a SortedField
            if field_name in obj._meta.sorted_field_names:
                field_cls = field_obj.__class__
                sortedset_db_key = field_cls.get_partitioned_sortedset_db_key(
                    obj, field_name
                )
                score_delta = float(delta) if isinstance(delta, _Decimal) else delta
                get_REDIS_DB().zincrby(
                    sortedset_db_key.redis_key, score_delta, redis_key
                )

            return new_val

    # -- C. query ----------------------------------------------------------

    def select(self, spec: ModelSpec, plan: QueryPlan) -> list[Row]:
        """Moved from ``Query._execute_filter`` (key-set evaluation through
        each field's ``filter_query``, the sorted-range pushdown and its retry,
        hydration, client-side filters, geo distances, Python order/limit and
        ``on_read``) and from ``Query.keys()`` (``SMEMBERS`` of the class set,
        for ``project=()``).

        Replays ``plan.source`` -- the call as the public API received it --
        through that code; it never reads ``plan.where``. Rows are
        :class:`RedisResultRow` (hydrated results) or, for ``project=()``,
        :class:`RedisIdRow`.
        """
        call = plan.source
        if call is None:
            raise BackendCapabilityError(
                "RedisBackend.select replays the public query call; build the "
                "plan with source=QueryCall(...)"
            )
        query = call.query
        if call.kind == "keys":
            return [
                RedisIdRow(spec.name, key)
                for key in get_REDIS_DB().smembers(
                    query.model_class._meta.db_class_set_key.redis_key
                )
            ]
        results = self._filter(
            query,
            call.q_objects,
            call.options.get("_no_track", False),
            call.options.get("_allow_pushdown", True),
            dict(call.kwargs),
        )
        return [RedisResultRow(result) for result in results]

    def _filter(
        self,
        query: Any,
        q_objects: Any,
        _no_track: bool,
        _allow_pushdown: bool,
        kwargs: Any,
    ) -> list[Any]:
        """The ``Query._execute_filter`` body, verbatim but for ``self`` ->
        ``query`` and its one retry, which recurses here."""
        from ..models.query import Query, _fire_on_read, _PushdownState

        # Use _evaluate_filter_args if Q objects present, otherwise filter_for_keys_set
        if q_objects:
            query._pushdown_allowed = False
            # Build the carrier before evaluating, so any geo distances a
            # leaf's filter_query produces have somewhere to land (a fresh
            # carrier per call *is* the reset — no separate query._geo_* clear
            # is needed).
            state = _PushdownState()
            db_keys_set = query._evaluate_filter_args(q_objects, kwargs, state=state)
            # Q objects combine results from multiple filter_for_keys_set calls,
            # so _sorted_field_order is unreliable — clear it
            query._sorted_field_order = None
            query._sorted_field_name = None
            # Snapshot the seven non-geo fields AFTER the clear so the
            # "ordering is unreliable" decision travels in the carrier like
            # every other per-call fact. Fill `state` in place — it already
            # holds the geo distances the Q evaluation wrote into it, and a
            # bare no-arg snapshot here would silently drop them.
            query._snapshot_pushdown_state(into=state)
        else:
            # Arm, query and snapshot in one hop that contains no yield point,
            # so nothing below re-reads bookkeeping off the shared instance.
            db_keys_set, state = query._filter_keys_with_pushdown(
                _allow_pushdown, kwargs
            )
        if not len(db_keys_set):
            return []

        # Apply default order_by from Meta if not explicitly provided
        # but not when sorted field ordering is active (it's a smarter default)
        if (
            "order_by" not in kwargs
            and query.model_class._meta.order_by
            and not state.sorted_field_order
        ):
            kwargs["order_by"] = query.model_class._meta.order_by

        # Use sorted field order if available and no explicit order_by.
        # Typed Any because db_keys_set carries either the key set or this
        # ordered list from here on, and hydration accepts both; the carrier
        # types this precisely where the old getattr() erased it to Any.
        sorted_field_order: Any = state.sorted_field_order
        explicit_order_by = kwargs.get("order_by", None)
        # Meta.order_by is a default - sorted field order takes precedence over it
        if sorted_field_order and not explicit_order_by:
            db_keys_set = sorted_field_order  # Use ordered list instead of set

        # Bound the key list before hydration when the range read could not be
        # bounded itself. filter_for_keys_set has already intersected
        # _sorted_field_order down to the keys every other index agreed on, so
        # the AND happened without loading anything and slicing here is sound
        # even with other indexed filters in play. This is the cut that matters:
        # it takes hydration from every key in the partition to `limit` HGETALLs.
        # The Redis-side bound saves transferring the key list, a smaller win.
        db_keys_set = query._bound_keys_before_hydration(
            db_keys_set, q_objects, _allow_pushdown, kwargs, state=state
        )

        # A pending client-side filter must see every candidate before any
        # truncation: get_many_objects slices KeyField-ordered keys to `limit`
        # BEFORE hydration, which would cut rows the plain-field filter would
        # have kept -- filter(kind="x", note="hit", limit=2) on a model whose
        # Meta.order_by is a KeyField returned the first two keys, filtered
        # them all away, and answered [] although matches existed further down.
        # prepare_results re-applies the limit after the client filters run.
        client_filters_pending = bool(state.pending_client_filters)
        objects = Query.get_many_objects(
            query.model_class,
            db_keys_set,
            order_by_attr_name=kwargs.get("order_by", None),
            limit=None if client_filters_pending else kwargs.get("limit", None),
            values=kwargs.get("values", None),
        )

        if query._short_result_action(len(objects), _allow_pushdown, state):
            return self._filter(query, q_objects, _no_track, False, dict(kwargs))

        # Apply client-side filters for plain (unindexed) fields
        client_filters = state.pending_client_filters
        if client_filters:
            filtered = []
            for obj in objects:
                match = True
                for field_name, expected_value in client_filters.items():
                    if isinstance(obj, dict):
                        actual = obj.get(field_name)
                    else:
                        actual = getattr(obj, field_name, None)
                    if actual != expected_value:
                        match = False
                        break
                if match:
                    filtered.append(obj)
            objects = filtered

        # Attach geo distances to objects if available. Read off the carrier,
        # not query._geo_distances — the carrier is the one this call's own
        # key query wrote into, immune to another thread's reset/update.
        if state.geo_distances:
            # Normalize distance dict keys to strings for consistent lookup
            normalized_distances = {}
            for key, dist in state.geo_distances.items():
                if isinstance(key, bytes):
                    normalized_distances[key.decode()] = dist
                else:
                    normalized_distances[key] = dist

            for obj in objects:
                if isinstance(obj, dict):
                    # When values= is used, obj is a dict - skip distance attachment
                    continue
                redis_key = obj.db_key.redis_key
                if isinstance(redis_key, bytes):
                    redis_key = redis_key.decode()
                distance = normalized_distances.get(redis_key)
                if distance is not None:
                    obj._geo_distance = distance
                    obj._geo_distance_unit = state.geo_distance_unit

            # Sort by distance (ascending) to preserve geo-sorted order
            # Only sort model objects, not dicts
            model_objects = [o for o in objects if not isinstance(o, dict)]
            dict_objects = [o for o in objects if isinstance(o, dict)]
            model_objects.sort(key=lambda o: getattr(o, "_geo_distance", float("inf")))
            objects = model_objects + dict_objects

        results = query.prepare_results(objects, **kwargs)

        # Fire on_read for AccessTrackerMixin models (skip for value projections)
        if not _no_track and not kwargs.get("values"):
            model_results = [r for r in results if not isinstance(r, dict)]
            if model_results:
                _fire_on_read(query.model_class, model_results)

        return results

    def count(self, spec: ModelSpec, plan: QueryPlan) -> int:
        """Moved from ``Query.count``: ``SCARD`` of the class set with no
        filters, else the filter's key set (no pushdown), hydrating only when a
        client-side filter must be applied."""
        from ..models.query import Query

        call = plan.source
        if call is None:
            raise BackendCapabilityError(
                "RedisBackend.count replays the public query call; build the "
                "plan with source=QueryCall(...)"
            )
        query = call.query
        kwargs = dict(call.kwargs)
        if not len(kwargs):
            return int(
                get_REDIS_DB().scard(query.model_class._meta.db_class_set_key.redis_key)
                or 0
            )
        # allow_pushdown=False preserves today's behavior exactly: count() never
        # armed the pushdown and must not start, because a bound tally is wrong.
        db_keys, state = query._filter_keys_with_pushdown(False, kwargs)
        client_filters = state.pending_client_filters
        if client_filters:
            # Must load objects to apply client-side filters
            objects = Query.get_many_objects(query.model_class, db_keys)
            return sum(
                1
                for obj in objects
                if all(
                    getattr(obj, fname, None) == fval
                    for fname, fval in client_filters.items()
                )
            )
        return len(db_keys)

    # -- D-H: not routed on Redis in M1a -----------------------------------

    def _unrouted(self, method: str, served_by: str) -> BackendCapabilityError:
        return BackendCapabilityError(
            f"RedisBackend.{method} is not routed in #759 M1a: on Redis it is "
            f"still served directly by {served_by}"
        )

    def touch(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("touch", "Model.touch")

    def update_confidence(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("update_confidence", "ConfidenceField.update_confidence")

    def supersede(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("supersede", "SupersessionProtocol")

    def chain(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("chain", "SupersessionProtocol.chain")

    def rank_decayed(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("rank_decayed", "DecayingSortedField.rank_decayed")

    def rank_composite(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("rank_composite", "QueryBuilder.composite_score")

    def vector_search(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("vector_search", "EmbeddingField")

    def keyword_search(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("keyword_search", "BM25Field.search")

    def graph_update(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("graph_update", "CoOccurrenceField")

    def graph_expand(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("graph_expand", "CoOccurrenceField.propagate")

    def membership_add(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("membership_add", "ExistenceFilter/FrequencySketch")

    def membership_query(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("membership_query", "ExistenceFilter/FrequencySketch")

    def maintain(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted(
            "maintain", "Model.check_indexes/clean_indexes/rebuild_indexes"
        )

    def field_call(self, *a: Any, **kw: Any) -> Any:
        raise self._unrouted("field_call", "the field's own methods")
