"""EventStreamMixin — append-only mutation log via Redis Streams.

This module provides a mixin class that automatically appends to a Redis Stream
on every save() or delete() call. It captures model class, primary key,
operation type, timestamp, and configurable metadata fields.

On a Postgres-bound model (#759 M5) the stream is the backend's events table,
not Redis: the entry is appended in the save's (or delete's) own transaction,
so the record and its mutation entry commit or roll back together, with the
same stream keys, ids and fields. Read either with :meth:`stream_range` /
:meth:`stream_revrange`, or ``popoto.streams.stream_client(model)``, which is
the Redis client on Redis and the backend's redis-py-shaped stream store on
Postgres.

Design:
    - _xadd_mutation() is called by base.py after successful save/delete
    - _xadd_event() is a public method for non-save operations (e.g.,
      ConfidenceField.update_confidence, CoOccurrenceField.strengthen)
    - Stream entries use approximate MAXLEN trimming to bound memory
    - Streams are partitionable by a configurable key field

Redis Key Patterns:
    - stream:{stream_name} — default stream key
    - stream:{stream_name}:{partition_value} — partitioned stream key

Stream Entry Fields (all strings per Redis Streams spec):
    - model: Model class name
    - pk: Redis key of the instance
    - op: One of "create", "update", "delete" (or custom for _xadd_event)
    - ts: Unix timestamp string
    - changed_fields: Comma-separated list of updated fields (empty string for full saves)
    - Plus any fields named in _stream_metadata_fields

Example:
    class Memory(EventStreamMixin, Model):
        key = UniqueKeyField()
        content = StringField()
        source = StringField()

        _stream_name = "memory_mutations"
        _stream_metadata_fields = ("source",)

    memory = Memory(key="fact1", content="hello", source="user")
    memory.save()  # XADD to stream:memory_mutations

    # Partitioned streams:
    class PartitionedMemory(EventStreamMixin, Model):
        key = UniqueKeyField()
        tenant = StringField()

        _stream_name = "mutations"
        _stream_partition_field = "tenant"

    m = PartitionedMemory(key="x", tenant="acme")
    m.save()  # XADD to stream:mutations:acme
"""

import logging
import time
import weakref

from typing import Any, Optional

from ..exceptions import ModelException
from ..models.canonical_key import canonical_key_str
from ..redis_db import get_REDIS_DB

logger = logging.getLogger("POPOTO.EventStream")

#: Every class that composes the mixin, so a ``StreamConsumer`` given only a
#: stream key can find which backend holds it (``popoto.streams``).
STREAM_MODELS: "weakref.WeakSet[type]" = weakref.WeakSet()


class EventStreamMixin:
    """Mixin that appends mutation entries to a Redis Stream on save/delete.

    Add as a base class alongside Model:
        class MyModel(EventStreamMixin, Model):
            _stream_name = "my_mutations"

    Class Attributes:
        _stream_name: Name for the Redis Stream. Default "mutations".
        _stream_partition_field: Optional field name to partition streams by.
            When set, the stream key includes the field's value.
        _stream_max_length: Approximate max entries in the stream. Default 10000.
        _stream_metadata_fields: Tuple of field names whose values are included
            in stream entries as additional key-value pairs.

    Note: Attributes prefixed with underscore to avoid conflict with
    Popoto's ModelBase metaclass, which requires public attributes to be Fields.
    """

    _stream_name: str = "mutations"
    _stream_partition_field: Optional[str] = None
    _stream_max_length: int = 10000
    _stream_metadata_fields: tuple = ()

    # Export/import: the mutation stream is deliberately NOT carried, and
    # #556 settled that as a permanent contract rather than pending work.
    # The stream is a property of the *source deployment's history* -- every
    # save and delete that ever ran there -- not a fact about any record in
    # the export. Its entries are shared across records, its IDs are
    # source-clock timestamps that XADD requires to be monotonically
    # increasing against whatever the destination stream already holds, and
    # its consumer groups carry per-group delivery state. Carrying it would
    # either collide with the destination's own history or invent one; the
    # honest contract is that an import starts a fresh mutation history whose
    # first entry is the import itself.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "Mutation stream history is not carried: it records the source "
        "deployment's save/delete history, not the state of any exported "
        "record. The destination begins a fresh stream. Permanent contract, "
        "not pending work."
    )

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        STREAM_MODELS.add(cls)

    # -- reading (backend-neutral) -----------------------------------------------

    @classmethod
    def stream_client(cls) -> Any:
        """The client that holds this model's stream: the Redis client on a
        Redis-bound model, the backend's redis-py-shaped stream store on a
        Postgres-bound one. Same methods, same replies."""
        from ..streams import stream_client

        return stream_client(cls)

    @classmethod
    def _stream_key_for(cls, partition: Any = None) -> str:
        if not cls._stream_name:
            raise ModelException(f"{cls.__name__} has empty _stream_name")
        key = f"stream:{cls._stream_name}"
        if partition is not None:
            key = f"{key}:{canonical_key_str(partition)}"
        return key

    @classmethod
    def stream_range(
        cls,
        min: Any = "-",
        max: Any = "+",
        count: Optional[int] = None,
        partition: Any = None,
    ) -> list[Any]:
        """``XRANGE`` over this model's stream (``partition``: the
        partition field's value, for a partitioned stream): a list of
        ``(id, {field: value})`` in id order, ``bytes`` throughout."""
        return cls.stream_client().xrange(
            cls._stream_key_for(partition), min=min, max=max, count=count
        )

    @classmethod
    def stream_revrange(
        cls,
        max: Any = "+",
        min: Any = "-",
        count: Optional[int] = None,
        partition: Any = None,
    ) -> list[Any]:
        """``XREVRANGE`` over this model's stream: newest first."""
        return cls.stream_client().xrevrange(
            cls._stream_key_for(partition), max=max, min=min, count=count
        )

    @classmethod
    def stream_len(cls, partition: Any = None) -> int:
        """``XLEN`` of this model's stream."""
        return int(cls.stream_client().xlen(cls._stream_key_for(partition)))

    # -- writing ----------------------------------------------------------------

    def _get_stream_key(self):
        """Build the Redis Stream key for this instance.

        Returns:
            str: Stream key like "stream:{name}" or "stream:{name}:{partition}".

        Raises:
            ModelException: If _stream_name is empty or _stream_partition_field
                references a non-existent field.
        """
        if not self._stream_name:
            raise ModelException(f"{type(self).__name__} has empty _stream_name")

        base_key = f"stream:{self._stream_name}"

        if self._stream_partition_field:
            # Validate the partition field exists
            if not hasattr(self, self._stream_partition_field):
                raise ModelException(
                    f"{type(self).__name__}._stream_partition_field "
                    f"'{self._stream_partition_field}' does not exist on the model"
                )
            partition_value = getattr(self, self._stream_partition_field)
            if partition_value is not None:
                base_key = f"{base_key}:{canonical_key_str(partition_value)}"

        return base_key

    def _build_stream_entry(self, op, update_fields=None, extra_fields=None):
        """Build the field mapping for a stream entry.

        Args:
            op: Operation type string (e.g., "create", "update", "delete").
            update_fields: Optional list of field names that were updated.
            extra_fields: Optional dict of additional fields to include.

        Returns:
            dict: Mapping of string keys to string values for XADD.
        """
        redis_key = getattr(self, "_redis_key", None) or ""
        try:
            if not redis_key:
                redis_key = self.db_key.redis_key
        except Exception:
            redis_key = ""

        entry = {
            "model": type(self).__name__,
            "pk": str(redis_key),
            "op": str(op),
            "ts": str(time.time()),
            "changed_fields": ",".join(update_fields) if update_fields else "",
        }

        # Add metadata fields
        for field_name in self._stream_metadata_fields:
            value = getattr(self, field_name, None)
            entry[field_name] = str(value) if value is not None else ""

        # Add extra fields
        if extra_fields:
            for k, v in extra_fields.items():
                entry[str(k)] = str(v)

        return entry

    def _xadd_mutation(self, op, pipeline=None, update_fields=None):
        """Append a mutation entry to the Redis Stream.

        Called internally by base.py after successful save() or delete().

        Args:
            op: Operation type ("create", "update", or "delete").
            pipeline: Optional Redis pipeline to queue the XADD onto.
            update_fields: Optional list of field names that were updated.
        """
        try:
            stream_key = self._get_stream_key()
            entry = self._build_stream_entry(op, update_fields=update_fields)

            if pipeline:
                pipeline.xadd(
                    stream_key,
                    entry,
                    maxlen=self._stream_max_length,
                    approximate=True,
                )
            else:
                get_REDIS_DB().xadd(
                    stream_key,
                    entry,
                    maxlen=self._stream_max_length,
                    approximate=True,
                )
        except Exception as e:
            if pipeline:
                # In pipeline mode, re-raise since the caller expects
                # atomicity — if the pipeline fails, both data and
                # stream entry fail together (which is correct).
                raise
            # Non-pipeline mode: log and swallow — save must succeed
            logger.warning(
                "EventStreamMixin XADD failed for %s: %s",
                type(self).__name__,
                e,
            )

    def _stream_append(
        self,
        op: str,
        update_fields: Optional[Any] = None,
        extra_fields: Optional[Any] = None,
    ) -> Any:
        """This instance's entry as a backend append (#759 M5): the stream
        key, the fields ``XADD`` would send, and the configured ``MAXLEN``.
        Raises what building the key or entry raises."""
        from ..backends.postgres.events import StreamAppend

        return StreamAppend.of(
            self._get_stream_key(),
            self._build_stream_entry(
                op, update_fields=update_fields, extra_fields=extra_fields
            ),
            maxlen=self._stream_max_length,
            approximate=True,
        )

    def _native_stream_entries(
        self, op: str, update_fields: Optional[Any] = None, strict: bool = False
    ) -> list[Any]:
        """The entries a save on a non-Redis backend appends in its own
        transaction (``Model.save`` hands them to ``backend.save``). Errors
        follow the Redis paths: ``strict`` (a ``pipeline=`` was given)
        re-raises, as the queued ``XADD`` does; otherwise the failure is
        logged and the save goes ahead without an entry."""
        try:
            return [self._stream_append(op, update_fields=update_fields)]
        except Exception as e:
            if strict:
                raise
            logger.warning(
                "EventStreamMixin XADD failed for %s: %s",
                type(self).__name__,
                e,
            )
            return []

    def _xadd_event(self, op, extra_fields=None, pipeline=None):
        """Append a custom event entry to the Redis Stream.

        Public method for non-save operations (e.g., ConfidenceField.update_confidence,
        CoOccurrenceField.strengthen) that bypass Model.save() and write to Redis
        directly.

        Args:
            op: Operation type string (e.g., "confidence_update", "strengthen").
            extra_fields: Optional dict of additional fields to include in the entry.
            pipeline: Optional Redis pipeline to queue the XADD onto.
        """
        from ..backends.routing import non_redis_backend

        backend = non_redis_backend(self)
        if backend is not None:
            # #759 M5: the backend's stream, inside the caller's unit of work
            # when one is passed (appended just before its COMMIT).
            from ..backends import UnitOfWork

            try:
                backend.stream_append(
                    self._stream_append(op, extra_fields=extra_fields),
                    uow=pipeline if isinstance(pipeline, UnitOfWork) else None,
                )
            except Exception as e:
                if pipeline:
                    raise
                logger.warning(
                    "EventStreamMixin _xadd_event failed for %s: %s",
                    type(self).__name__,
                    e,
                )
            return
        try:
            stream_key = self._get_stream_key()
            entry = self._build_stream_entry(op, extra_fields=extra_fields)

            if pipeline:
                pipeline.xadd(
                    stream_key,
                    entry,
                    maxlen=self._stream_max_length,
                    approximate=True,
                )
            else:
                get_REDIS_DB().xadd(
                    stream_key,
                    entry,
                    maxlen=self._stream_max_length,
                    approximate=True,
                )
        except Exception as e:
            if pipeline:
                raise
            logger.warning(
                "EventStreamMixin _xadd_event failed for %s: %s",
                type(self).__name__,
                e,
            )
