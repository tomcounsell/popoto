"""EventStreamMixin — append-only mutation log via Redis Streams.

This module provides a mixin class that automatically appends to a Redis Stream
on every save() or delete() call. It captures model class, primary key,
operation type, timestamp, and configurable metadata fields.

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

from typing import Optional

from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.EventStream")


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

    # Export/import: the Redis Stream is an append-only mutation log built
    # incrementally by every save/delete over the model's lifetime. There is
    # no single-record snapshot that reconstructs prior stream entries, and
    # replaying export/import would fabricate a mutation history that never
    # happened. Not addressed in v1 -- see #556.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "Mutation stream history not carried or rebuilt by import; see #556"
    )

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
                base_key = f"{base_key}:{partition_value}"

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
                POPOTO_REDIS_DB.xadd(
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
                POPOTO_REDIS_DB.xadd(
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
