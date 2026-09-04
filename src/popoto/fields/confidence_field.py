"""ConfidenceField — capped-evidence confidence tracking with entrainment.

Maintains a confidence score per member, updated atomically via Lua script
using a capped-evidence update rule (issue #407). While effective evidence
(real observations plus a prior pseudo-count of 1) is at or below the
``evidence_cap``, each update computes the exact running mean over
{prior, signals...} — the result is order-invariant. Once effective
evidence reaches the cap, the gain freezes at 1/(cap+1), giving
fixed-gain exponential forgetting with an effective memory window of
cap+1 observations: new evidence keeps a constant influence instead of
vanishing as history grows.

Companion Redis hash stores per-member confidence metadata:
    - $ConfidencF:{Model}:{field}:data — msgpack {confidence, evidence_count,
      corroborations, contradictions}

Partitioning:
    When ``partition_by`` is specified, the companion hash is split into
    per-partition hashes, avoiding O(N) HGETALL on the global hash:

        ConfidenceField(partition_by='project')
        # Hash key: $ConfidencF:{Model}:{field}:data:{project_value}

    This mirrors SortedField's partition_by API. Queries on partitioned
    ConfidenceFields must include the partition field value(s).

Example:
    class Memory(Model):
        key = UniqueKeyField()
        project = KeyField()
        content = StringField()
        certainty = ConfidenceField(initial_confidence=0.5, partition_by='project')

    memory = Memory.create(key="fact1", project="atlas", content="The sky is blue")
    ConfidenceField.update_confidence(memory, "certainty", signal=0.9)
    print(ConfidenceField.get_confidence(memory, "certainty"))
"""

import logging

import msgpack
import redis

from ..exceptions import ModelException
from ..models.canonical_key import canonical_key_str
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB, run_lua
from .constants import Defaults
from .field import Field

logger = logging.getLogger("POPOTO.ConfidenceField")

# Lua script: atomic capped-evidence update of the confidence companion hash.
# While n_eff = min(evidence_count + prior_weight, cap) is below the cap, the
# update is an exact running mean over {prior, signals...} (order-invariant).
# At the cap, the gain freezes at 1/(cap+1): fixed-gain exponential
# forgetting with an effective memory window of cap+1 observations.
# KEYS[1] = companion hash key
# ARGV[1] = member key (redis_key of the model instance)
# ARGV[2] = signal (float 0-1)
# ARGV[3] = initial_confidence (default for missing data)
# ARGV[4] = evidence_cap
CAPPED_BAYESIAN_UPDATE_LUA = """
local hash_key = KEYS[1]
local member = ARGV[1]
local signal = tonumber(ARGV[2])
local initial_confidence = tonumber(ARGV[3])

-- KEYS[2] (optional): the member's own hash. When given, an update for a
-- record that no longer exists is a no-op, which lets callers batch many
-- updates into one pipeline without a preceding EXISTS round trip each.
if KEYS[2] and redis.call('EXISTS', KEYS[2]) == 0 then
    return nil
end

-- Read existing data
local raw = redis.call('HGET', hash_key, member)
local confidence = initial_confidence
local evidence_count = 0
local corroborations = 0
local contradictions = 0

if raw then
    local ok, data = pcall(cmsgpack.unpack, raw)
    if ok and type(data) == 'table' then
        confidence = data['confidence'] or data[1] or initial_confidence
        evidence_count = data['evidence_count'] or data[2] or 0
        corroborations = data['corroborations'] or data[3] or 0
        contradictions = data['contradictions'] or data[4] or 0
    end
end

-- Capped-evidence update: running mean while effective evidence <= cap,
-- fixed-gain exponential forgetting (window cap+1) beyond it.
-- ARGV[4] = evidence_cap (the only new ARGV)
local prior_weight = 1  -- internal constant; not user config (issue #407 decision Q2)
local cap = tonumber(ARGV[4])
local n_eff = math.min(evidence_count + prior_weight, cap)
local new_confidence = confidence + (signal - confidence) / (n_eff + 1)

-- Clamp to [0, 1]
new_confidence = math.max(0, math.min(1, new_confidence))

-- Update counters
evidence_count = evidence_count + 1
if signal >= 0.5 then
    corroborations = corroborations + 1
else
    contradictions = contradictions + 1
end

-- Pack and store
local updated = {
    confidence = new_confidence,
    evidence_count = evidence_count,
    corroborations = corroborations,
    contradictions = contradictions
}
redis.call('HSET', hash_key, member, cmsgpack.pack(updated))

-- Return new values
return {tostring(new_confidence), tostring(evidence_count), tostring(corroborations), tostring(contradictions)}
"""


class ConfidenceField(Field):
    """A Field that tracks confidence metadata in a companion Redis hash.

    Not a SortedField — confidence is metadata, not a ranking dimension.
    Stores {confidence, evidence_count, corroborations, contradictions} per member.

    Updates use a capped-evidence rule (issue #407): an order-invariant
    running mean over {prior, signals...} while effective evidence is at
    or below ``evidence_cap``, then fixed-gain exponential forgetting
    (memory window cap+1) beyond it.

    Args:
        initial_confidence: Starting confidence for new members. Default 0.5.
        evidence_cap: Cap on effective evidence (memory-window length).
            Must be an int >= 1. Default ``Defaults.CONFIDENCE_EVIDENCE_CAP``
            (20). Validated at model-class definition time.
        partition_by: Optional field name or tuple of field names to partition
            the companion hash. When set, each unique combination of partition
            field values gets its own Redis hash, reducing the cost of reads
            from O(total_members) to O(partition_members). Mirrors SortedField's
            partition_by API.

    Example:
        class Memory(Model):
            key = UniqueKeyField()
            content = StringField()
            certainty = ConfidenceField(initial_confidence=0.5)

        memory = Memory.create(key="fact1", content="The sky is blue")
        ConfidenceField.update_confidence(memory, "certainty", signal=0.9)
        print(ConfidenceField.get_confidence(memory, "certainty"))

    Partitioned example:
        class Memory(Model):
            key = UniqueKeyField()
            project = KeyField()
            content = StringField()
            certainty = ConfidenceField(initial_confidence=0.5, partition_by='project')

        memory = Memory.create(key="fact1", project="atlas", content="...")
        ConfidenceField.update_confidence(memory, "certainty", signal=0.9)
    """

    # Export/import: the companion hash holds evidence_count, corroborations
    # and contradictions, which exist nowhere else -- only ``confidence`` is
    # mirrored onto the model attribute. A plain re-save reseeds the hash with
    # ``initial_confidence`` via HSETNX (see on_save), discarding all four
    # values, so they must be carried explicitly.
    roundtrip_policy: str = "carry"

    @classmethod
    def export_state(cls, model_instance, field_name, field_value, **kwargs):
        """Export the companion-hash confidence metadata for one instance.

        Reads ``{confidence, evidence_count, corroborations, contradictions}``
        from the field's own companion hash (partition-aware via
        :meth:`get_data_hash_key`).

        Returns:
            dict of the four metadata values, or ``None`` when this instance
            has no companion-hash entry (never saved, or already deleted).
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            return None

        data_hash_key = field.get_data_hash_key(model_instance, field_name)
        member_key = model_instance.db_key.redis_key
        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        if not raw:
            return None

        try:
            data = msgpack.unpackb(raw, raw=False)
        except Exception:
            logger.warning(
                f"Could not decode confidence data for {member_key} "
                f"in {data_hash_key}; skipping export of {field_name}"
            )
            return None
        if not isinstance(data, dict):
            return None

        return {
            "confidence": float(data.get("confidence", field.initial_confidence)),
            "evidence_count": int(data.get("evidence_count", 0) or 0),
            "corroborations": int(data.get("corroborations", 0) or 0),
            "contradictions": int(data.get("contradictions", 0) or 0),
        }

    @classmethod
    def import_state(cls, model_instance, field_name, state, **kwargs):
        """Restore companion-hash confidence metadata after import.

        Written with a raw ``HSET`` of msgpack -- mirroring what ``on_save``
        does -- because there is no public setter (``update_confidence`` is
        signal-only and would apply the capped-evidence update rule rather
        than restoring exact values).

        This deliberately OVERWRITES: ``on_save`` has already run by the time
        the transfer driver calls this, and it seeds the entry with
        ``field.initial_confidence`` via HSETNX. An HSETNX here would be a
        no-op and the exported values would be silently lost.
        """
        if not state:
            return None

        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            return None

        data_hash_key = field.get_data_hash_key(model_instance, field_name)
        member_key = model_instance.db_key.redis_key
        data = {
            "confidence": float(state.get("confidence", field.initial_confidence)),
            "evidence_count": int(state.get("evidence_count", 0) or 0),
            "corroborations": int(state.get("corroborations", 0) or 0),
            "contradictions": int(state.get("contradictions", 0) or 0),
        }
        POPOTO_REDIS_DB.hset(data_hash_key, member_key, msgpack.packb(data))
        return None

    def __init__(self, **kwargs):
        initial_confidence = kwargs.pop("initial_confidence", None)
        self.initial_confidence = (
            initial_confidence
            if initial_confidence is not None
            else Defaults.INITIAL_CONFIDENCE
        )

        if not 0 <= self.initial_confidence <= 1:
            raise ModelException(
                f"initial_confidence must be between 0 and 1 "
                f"(got {self.initial_confidence})"
            )

        # evidence_cap: memory-window length / epistemics knob (issue #407).
        # Validated here so misconfiguration fails at model-class definition
        # time (field construction), not at first update.
        evidence_cap = kwargs.pop("evidence_cap", None)
        if evidence_cap is None:
            evidence_cap = Defaults.CONFIDENCE_EVIDENCE_CAP
        # bool is a subclass of int — reject it explicitly
        if isinstance(evidence_cap, bool) or not isinstance(evidence_cap, int):
            raise ModelException(
                f"evidence_cap must be an int >= 1 (got {evidence_cap!r})"
            )
        if evidence_cap < 1:
            raise ModelException(
                f"evidence_cap must be an int >= 1 (got {evidence_cap})"
            )
        self.evidence_cap = evidence_cap

        # Handle partition_by parameter
        partition_by = kwargs.pop("partition_by", tuple())
        if isinstance(partition_by, str):
            partition_by = (partition_by,)
        elif partition_by and not isinstance(partition_by, tuple):
            raise ModelException("partition_by must be str or tuple of str field names")
        self.partition_by = partition_by

        # ConfidenceField stores float confidence values
        # Default to initial_confidence so instance.field returns
        # the configured value rather than None (fixes #281)
        kwargs.setdefault("type", float)
        kwargs.setdefault("default", self.initial_confidence)
        kwargs.setdefault("null", True)
        super().__init__(**kwargs)

    def get_data_hash_key(self, model_instance, field_name):
        """Build the Redis key for the confidence companion hash.

        Public API for external callers that need to perform direct Redis
        operations on the companion hash (e.g., bulk reads, migrations,
        or monitoring). Prefer the higher-level class methods
        (get_confidence, update_confidence) for normal operations.

        When partition_by is set and a model instance is provided,
        appends partition field values to the key.

        Pattern (unpartitioned): $ConfidencF:{Model}:{field}:data
        Pattern (partitioned):   $ConfidencF:{Model}:{field}:data:{partition_val1}:{partition_val2}
        """
        base_key = self.get_special_use_field_db_key(model_instance, field_name)
        key = base_key.redis_key + ":data"

        if self.partition_by:
            for partition_field_name in self.partition_by:
                val = getattr(model_instance, partition_field_name, None)
                if val is not None:
                    key += f":{canonical_key_str(val)}"
        return key

    def get_data_hash_key_from_values(
        self, model_class, field_name, **partition_values
    ):
        """Build the companion hash key from explicit partition values.

        Public API for query paths and external callers that have partition
        field values but not a model instance (e.g., custom query builders,
        bulk operations scoped to a partition).

        Args:
            model_class: The Model class.
            field_name: Name of the ConfidenceField.
            **partition_values: Mapping of partition field names to values.

        Returns:
            str: The Redis hash key.

        Raises:
            QueryException: If a required partition field is missing.
        """
        base_key = self.get_special_use_field_db_key(model_class, field_name)
        key = base_key.redis_key + ":data"

        if self.partition_by:
            for pf in self.partition_by:
                val = partition_values.get(pf)
                if val is None:
                    raise QueryException(
                        f"ConfidenceField '{field_name}' is partitioned by "
                        f"{', '.join(self.partition_by)}. "
                        f"Query must include filter(s) for: {pf}"
                    )
                key += f":{canonical_key_str(val)}"
        return key

    def get_old_data_hash_key(self, model_instance, field_name):
        """Build the companion hash key using saved (old) partition field values.

        Used during on_save/on_delete to find the old partition hash when
        a partition key has changed. External callers performing custom
        partition migrations may also need this to locate stale hash entries.

        Returns:
            str or None: The old hash key, or None if no saved values exist.
        """
        saved = getattr(model_instance, "_saved_field_values", None)
        if not saved:
            return None

        base_key = self.get_special_use_field_db_key(model_instance, field_name)
        key = base_key.redis_key + ":data"

        if self.partition_by:
            for pf in self.partition_by:
                val = saved.get(pf)
                if val is not None:
                    key += f":{canonical_key_str(val)}"
        return key

    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        """Initialize companion hash with initial_confidence on first save.

        For partitioned fields, detects partition key changes and moves
        the entry from the old partition hash to the new one. Also handles
        key migration (migrate_key=True) where on_delete preserves old
        confidence data on the instance for restoration here.
        """
        result = super().on_save(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )

        field = model_instance._meta.fields[field_name]
        if not isinstance(field, ConfidenceField):
            return result

        member_key = model_instance.db_key.redis_key
        data_hash_key = field.get_data_hash_key(model_instance, field_name)

        db = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )

        # Check if on_delete preserved migration data (key migration scenario)
        migration_data = getattr(model_instance, "_confidence_migration_data", {})
        if field_name in migration_data:
            old_raw = migration_data.pop(field_name)
            db.hset(data_hash_key, member_key, old_raw)
            # Clean up the temp attribute if empty
            if not migration_data:
                try:
                    del model_instance._confidence_migration_data
                except AttributeError:
                    pass
            return result

        # Handle partition key change without key migration (partition field
        # changed but redis key stayed the same, e.g. partition field is a
        # regular Field, not a KeyField)
        if field.partition_by and hasattr(model_instance, "_saved_field_values"):
            saved = model_instance._saved_field_values
            if saved:
                partition_changed = any(
                    saved.get(pf) is not None
                    and saved.get(pf) != getattr(model_instance, pf, None)
                    for pf in field.partition_by
                )
                if partition_changed:
                    old_hash_key = field.get_old_data_hash_key(
                        model_instance, field_name
                    )
                    if old_hash_key and old_hash_key != data_hash_key:
                        # Read old data directly from Redis
                        old_raw = POPOTO_REDIS_DB.hget(old_hash_key, member_key)
                        if old_raw:
                            db.hset(data_hash_key, member_key, old_raw)
                            db.hdel(old_hash_key, member_key)
                            return result

        # Initialize with HSETNX (atomic set-if-not-exists, no race condition)
        initial_data = {
            "confidence": field.initial_confidence,
            "evidence_count": 0,
            "corroborations": 0,
            "contradictions": 0,
        }
        db.hsetnx(data_hash_key, member_key, msgpack.packb(initial_data))

        return result

    @classmethod
    def on_delete(
        cls, model_instance, field_name, field_value, pipeline=None, **kwargs
    ):
        """Remove companion hash entry on delete.

        When saved_redis_key is provided (key migration), preserves the raw
        confidence data on the model instance so on_save can restore it into
        the new partition hash without data loss.
        """
        field = model_instance._meta.fields[field_name]

        if isinstance(field, ConfidenceField):
            member_key = (
                kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
            )

            # Build hash key from saved partition values (old partition)
            if field.partition_by and kwargs.get("saved_redis_key"):
                old_hash_key = field.get_old_data_hash_key(model_instance, field_name)
                data_hash_key = old_hash_key or field.get_data_hash_key(
                    model_instance, field_name
                )
            else:
                data_hash_key = field.get_data_hash_key(model_instance, field_name)

            # During key migration (saved_redis_key present), preserve confidence
            # data so on_save can restore it in the new partition hash.
            if kwargs.get("saved_redis_key"):
                old_raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
                if old_raw:
                    if not hasattr(model_instance, "_confidence_migration_data"):
                        model_instance._confidence_migration_data = {}
                    model_instance._confidence_migration_data[field_name] = old_raw

            db = (
                pipeline
                if isinstance(pipeline, redis.client.Pipeline)
                else POPOTO_REDIS_DB
            )
            db.hdel(data_hash_key, member_key)

        return super().on_delete(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )

    @classmethod
    def update_confidence(cls, model_instance, field_name, signal, pipeline=None):
        """Update confidence for a member using the capped-evidence rule.

        While effective evidence (real observations plus a prior
        pseudo-count of 1) is at or below the field's ``evidence_cap``,
        the update computes the exact running mean over
        {prior, signals...} — order-invariant, converging on the mean of
        the observed signals. Once effective evidence reaches the cap,
        the gain freezes at 1/(cap+1): fixed-gain exponential forgetting
        with an effective memory window of cap+1 observations, so new
        evidence keeps a constant influence.

        Warning:
            All processes updating the same companion hash entry must
            configure identical ``evidence_cap`` values. The cap is
            passed per-call, not stored in the hash, so divergent caps
            across processes produce silently inconsistent gain
            schedules with no runtime detection.

        Note: When ``pipeline`` is given the update is queued on it and
        this method returns ``None``; the instance attribute is not
        synced, and a record that no longer exists is skipped server-side
        instead of raising. Without a pipeline the update runs at once.

        Args:
            model_instance: The Model instance to update.
            field_name: Name of the ConfidenceField on the model.
            signal: Float 0-1. Values >= 0.5 corroborate, < 0.5 contradict.
            pipeline: Optional Redis pipeline to queue the update on.

        Returns:
            float: The new confidence value, or ``None`` when queued on a
            pipeline.

        Raises:
            TypeError: If field is not a ConfidenceField or model is unsaved.
            ValueError: If signal is not between 0 and 1.
        """
        if signal is None:
            raise TypeError("signal must be a number, not None")

        signal = float(signal)
        if not 0 <= signal <= 1:
            raise ValueError(f"signal must be between 0 and 1 (got {signal})")

        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            raise TypeError(f"{field_name} is not a ConfidenceField")

        # Check model is saved
        try:
            member_key = model_instance.db_key.redis_key
        except Exception:
            raise TypeError("update_confidence() requires a saved model instance")

        data_hash_key = field.get_data_hash_key(model_instance, field_name)

        if pipeline is not None:
            # Batched: the Lua EXISTS guard on KEYS[2] replaces the
            # round-trip check below, so N suppressions cost one execute().
            run_lua(
                pipeline,
                CAPPED_BAYESIAN_UPDATE_LUA,
                2,
                data_hash_key,
                member_key,
                member_key,
                str(signal),
                str(field.initial_confidence),
                str(field.evidence_cap),
            )
            return None

        if not POPOTO_REDIS_DB.exists(member_key):
            raise TypeError("update_confidence() requires a saved model instance")

        result = run_lua(
            POPOTO_REDIS_DB,
            CAPPED_BAYESIAN_UPDATE_LUA,
            1,  # number of KEYS
            data_hash_key,
            member_key,
            str(signal),
            str(field.initial_confidence),
            str(field.evidence_cap),
        )

        new_confidence = float(result[0])

        # Sync the instance attribute so instance.field returns
        # the updated confidence (fixes #281)
        setattr(model_instance, field_name, new_confidence)

        # EventStreamMixin: log confidence update event
        from .event_stream import EventStreamMixin

        if isinstance(model_instance, EventStreamMixin):
            model_instance._xadd_event(
                op="confidence_update",
                extra_fields={
                    "field": field_name,
                    "signal": str(signal),
                    "new_confidence": str(new_confidence),
                },
            )

        return new_confidence

    @classmethod
    def get_confidence(cls, model_instance, field_name):
        """Get the current confidence value for a member.

        Args:
            model_instance: The Model instance.
            field_name: Name of the ConfidenceField.

        Returns:
            float: Current confidence value, or initial_confidence if no data.
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            raise TypeError(f"{field_name} is not a ConfidenceField")

        member_key = model_instance.db_key.redis_key
        data_hash_key = field.get_data_hash_key(model_instance, field_name)

        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        if raw is None:
            return field.initial_confidence

        data = msgpack.unpackb(raw, raw=False)
        return data.get("confidence", field.initial_confidence)

    @classmethod
    def get_confidence_data(cls, model_instance, field_name):
        """Get all confidence metadata for a member.

        Args:
            model_instance: The Model instance.
            field_name: Name of the ConfidenceField.

        Returns:
            dict: {confidence, evidence_count, corroborations, contradictions}
                  or defaults if no data exists.
        """
        field = model_instance._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            raise TypeError(f"{field_name} is not a ConfidenceField")

        member_key = model_instance.db_key.redis_key
        data_hash_key = field.get_data_hash_key(model_instance, field_name)

        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        if raw is None:
            return {
                "confidence": field.initial_confidence,
                "evidence_count": 0,
                "corroborations": 0,
                "contradictions": 0,
            }

        return msgpack.unpackb(raw, raw=False)

    @classmethod
    def get_confidence_filtered(cls, model_class, field_name, pattern="*"):
        """Get confidence data for members matching an HSCAN pattern.

        Useful for filtered reads on unpartitioned hashes without loading
        all entries into memory.

        Args:
            model_class: The Model class.
            field_name: Name of the ConfidenceField.
            pattern: Redis MATCH pattern for HSCAN (default '*' matches all).

        Returns:
            dict: {member_key: {confidence, evidence_count, ...}} for matches.
        """
        field = model_class._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            raise TypeError(f"{field_name} is not a ConfidenceField")

        base_key = field.get_special_use_field_db_key(model_class, field_name)
        data_hash_key = base_key.redis_key + ":data"

        result = {}
        cursor = 0
        while True:
            cursor, data = POPOTO_REDIS_DB.hscan(
                data_hash_key, cursor=cursor, match=pattern, count=100
            )
            for member_key, raw_value in data.items():
                if isinstance(member_key, bytes):
                    member_key = member_key.decode()
                try:
                    result[member_key] = msgpack.unpackb(raw_value, raw=False)
                except Exception:
                    logger.warning(
                        "Failed to unpack confidence data for %s", member_key
                    )
            if cursor == 0:
                break
        return result

    @classmethod
    def migrate_to_partitioned(cls, model_class, field_name, dry_run=False):
        """Migrate existing unpartitioned hash data into partitioned hashes.

        Reads all entries from the single companion hash, loads each model
        instance from Redis to determine its partition field values, and
        writes each entry to the appropriate partitioned hash.

        Args:
            model_class: The Model class with the ConfidenceField.
            field_name: Name of the ConfidenceField to migrate.
            dry_run: If True, report what would happen without modifying data.

        Returns:
            dict: Migration report with counts per partition and any errors.

        Raises:
            ModelException: If the field has no partition_by configured.
        """
        from ..models.encoding import decode_popoto_model_hashmap

        field = model_class._meta.fields.get(field_name)
        if not isinstance(field, ConfidenceField):
            raise ModelException(f"{field_name} is not a ConfidenceField")

        if not field.partition_by:
            raise ModelException(
                f"ConfidenceField '{field_name}' has no partition_by configured. "
                f"Nothing to migrate."
            )

        # Read the unpartitioned hash
        base_key = field.get_special_use_field_db_key(model_class, field_name)
        unpartitioned_key = base_key.redis_key + ":data"

        all_data = POPOTO_REDIS_DB.hgetall(unpartitioned_key)
        if not all_data:
            return {"total": 0, "migrated": 0, "errors": [], "partitions": {}}

        report = {
            "total": len(all_data),
            "migrated": 0,
            "errors": [],
            "partitions": {},
        }

        for member_key_raw, raw_value in all_data.items():
            member_key = (
                member_key_raw.decode()
                if isinstance(member_key_raw, bytes)
                else member_key_raw
            )

            # Load the model instance from Redis to read partition field values
            try:
                redis_hash = POPOTO_REDIS_DB.hgetall(member_key)
                if not redis_hash:
                    report["errors"].append(
                        {
                            "member_key": member_key,
                            "error": "Instance not found in Redis",
                        }
                    )
                    continue
                instance = decode_popoto_model_hashmap(
                    model_class, redis_hash, source_redis_key=member_key
                )
                if instance is None:
                    report["errors"].append(
                        {"member_key": member_key, "error": "Failed to decode instance"}
                    )
                    continue
            except Exception as e:
                report["errors"].append({"member_key": member_key, "error": str(e)})
                continue

            # Build the partitioned hash key
            partitioned_key = field.get_data_hash_key(instance, field_name)
            partition_label = (
                partitioned_key.split(":data:")[-1]
                if ":data:" in partitioned_key
                else "default"
            )

            if partition_label not in report["partitions"]:
                report["partitions"][partition_label] = 0
            report["partitions"][partition_label] += 1

            if not dry_run:
                POPOTO_REDIS_DB.hset(partitioned_key, member_key, raw_value)
                report["migrated"] += 1

        # Delete the old unpartitioned hash after successful migration
        if not dry_run and report["migrated"] == report["total"] - len(
            report["errors"]
        ):
            if report["migrated"] > 0:
                POPOTO_REDIS_DB.delete(unpartitioned_key)

        return report
