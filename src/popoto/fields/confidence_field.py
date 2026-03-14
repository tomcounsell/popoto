"""ConfidenceField — Bayesian confidence tracking for memory models.

Stores Bayesian confidence metadata in a companion Redis hash.
NOT a SortedField — it's a data field tracking:
    {confidence, evidence_count, corroborations, contradictions}

Companion Redis hash key pattern:
    $ConfidencF:{Model}:{field}:data

Each member in the hash maps a model instance's Redis key to a msgpack-encoded
dict of confidence metadata. Updates are atomic via Lua script.

The Bayesian update formula:
    new_confidence = prior + (signal - prior) / sqrt(evidence_count + 1)

Where signal is a float 0-1 representing corroboration (>0.5) or
contradiction (<0.5).

Example:
    class Memory(Model):
        key = AutoKeyField()
        content = Field(type=str)
        confidence = ConfidenceField(initial_confidence=0.5)

    memory = Memory.create(content="Earth is round")
    memory.update_confidence("confidence", signal=0.9)  # corroborate
    memory.get_confidence("confidence")  # ~0.9
"""

import logging

import msgpack
import redis

from ..redis_db import POPOTO_REDIS_DB
from .field import Field

logger = logging.getLogger("POPOTO.ConfidenceField")

# Atomic Lua script for Bayesian confidence update.
#
# KEYS[1] = companion hash key
# ARGV[1] = member key (Redis key of the model instance)
# ARGV[2] = signal (float 0-1)
# ARGV[3] = initial_confidence (default to use if no data exists)
#
# Logic: Read current data from hash -> apply Bayesian update ->
# increment counters -> write back -> return updated data.
BAYESIAN_UPDATE_LUA = """
local hash_key = KEYS[1]
local member_key = ARGV[1]
local signal = tonumber(ARGV[2])
local initial_confidence = tonumber(ARGV[3])

-- Read current data
local raw = redis.call('HGET', hash_key, member_key)
local confidence, evidence_count, corroborations, contradictions

if raw then
    local ok, data = pcall(cmsgpack.unpack, raw)
    if ok and type(data) == 'table' then
        confidence = data['confidence'] or initial_confidence
        evidence_count = data['evidence_count'] or 0
        corroborations = data['corroborations'] or 0
        contradictions = data['contradictions'] or 0
    else
        confidence = initial_confidence
        evidence_count = 0
        corroborations = 0
        contradictions = 0
    end
else
    confidence = initial_confidence
    evidence_count = 0
    corroborations = 0
    contradictions = 0
end

-- Bayesian update: new = prior + (signal - prior) / sqrt(evidence_count + 1)
local new_confidence = confidence + (signal - confidence) / math.sqrt(evidence_count + 1)

-- Clamp to [0, 1]
if new_confidence < 0 then new_confidence = 0 end
if new_confidence > 1 then new_confidence = 1 end

-- Increment evidence count
evidence_count = evidence_count + 1

-- Increment corroborations or contradictions
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
redis.call('HSET', hash_key, member_key, cmsgpack.pack(updated))

-- Return as flat array for easy parsing
return {tostring(new_confidence), tostring(evidence_count),
        tostring(corroborations), tostring(contradictions)}
"""


class ConfidenceField(Field):
    """A Field that tracks Bayesian confidence metadata in a companion Redis hash.

    Stores ``{confidence, evidence_count, corroborations, contradictions}``
    per model instance. Updates are atomic via Lua script using the formula:

        new_confidence = prior + (signal - prior) / sqrt(evidence_count + 1)

    This gives decreasing update magnitude as evidence accumulates (precision
    growth), while still allowing new evidence to shift the score.

    Args:
        initial_confidence: Starting confidence value (0-1). Default 0.5.
        **kwargs: Passed to parent Field.

    Example:
        class Memory(Model):
            key = AutoKeyField()
            content = Field(type=str)
            confidence = ConfidenceField(initial_confidence=0.5)

        m = Memory.create(content="fact")
        m.update_confidence("confidence", signal=0.9)
        m.get_confidence("confidence")
    """

    def __init__(self, initial_confidence=0.5, **kwargs):
        self.initial_confidence = initial_confidence
        kwargs.setdefault("type", float)
        kwargs.setdefault("null", True)
        kwargs.setdefault("default", None)
        super().__init__(**kwargs)

    def _get_data_hash_key(self, model_instance, field_name):
        """Build Redis key for confidence companion hash.

        Pattern: $ConfidencF:{Model}:{field}:data

        Args:
            model_instance: A Model instance.
            field_name: The field name on the model.

        Returns:
            str: Redis key string for the companion hash.
        """
        base_key = self.get_special_use_field_db_key(model_instance, field_name)
        return base_key.redis_key + ":data"

    @classmethod
    def update(cls, model_instance, field_name, signal, pipeline=None):
        """Atomically update Bayesian confidence for a model instance.

        Executes a Lua script that reads current confidence data,
        applies the Bayesian update formula, and writes back atomically.

        Args:
            model_instance: A saved Model instance.
            field_name: Name of the ConfidenceField on the model.
            signal: Float 0-1. Values >= 0.5 are corroborations,
                values < 0.5 are contradictions.
            pipeline: Optional Redis pipeline. When provided, the Lua
                script runs immediately (not deferred) but subsequent
                auto-discharge operations use the pipeline.

        Returns:
            dict: Updated confidence data with keys: confidence,
                evidence_count, corroborations, contradictions.
        """
        field = model_instance._meta.fields[field_name]
        if not isinstance(field, ConfidenceField):
            raise TypeError(
                f"ConfidenceField.update() requires a ConfidenceField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        member_key = (
            model_instance._redis_key
            if hasattr(model_instance, "_redis_key") and model_instance._redis_key
            else model_instance.db_key.redis_key
        )
        hash_key = field._get_data_hash_key(model_instance, field_name)

        # Lua scripts must run on the real connection, not a pipeline
        result = POPOTO_REDIS_DB.eval(
            BAYESIAN_UPDATE_LUA,
            1,
            hash_key,
            member_key,
            str(signal),
            str(field.initial_confidence),
        )

        # Parse result: [confidence, evidence_count, corroborations, contradictions]
        data = {
            "confidence": float(result[0]),
            "evidence_count": int(float(result[1])),
            "corroborations": int(float(result[2])),
            "contradictions": int(float(result[3])),
        }

        return data

    @classmethod
    def get_confidence_data(cls, model_instance, field_name):
        """Read confidence metadata from companion hash.

        Args:
            model_instance: A Model instance (saved or with redis key).
            field_name: Name of the ConfidenceField.

        Returns:
            dict or None: Confidence data dict with keys: confidence,
                evidence_count, corroborations, contradictions.
                Returns None if no data exists.
        """
        field = model_instance._meta.fields[field_name]
        if not isinstance(field, ConfidenceField):
            raise TypeError(
                f"get_confidence_data() requires a ConfidenceField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        member_key = (
            model_instance._redis_key
            if hasattr(model_instance, "_redis_key") and model_instance._redis_key
            else model_instance.db_key.redis_key
        )
        hash_key = field._get_data_hash_key(model_instance, field_name)

        raw = POPOTO_REDIS_DB.hget(hash_key, member_key)
        if not raw:
            return None

        data = msgpack.unpackb(raw, raw=False)
        return data

    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        """Initialize companion hash with initial_confidence if not exists.

        On first save, writes the initial confidence data dict.
        On subsequent saves, preserves existing confidence data.

        Args:
            model_instance: The Model instance being saved.
            field_name: The field name.
            field_value: The field value being saved.
            pipeline: Optional Redis pipeline.
            **kwargs: Additional keyword arguments.

        Returns:
            Result from parent on_save.
        """
        result = super().on_save(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )

        field = model_instance._meta.fields[field_name]
        if not isinstance(field, ConfidenceField):
            return result

        member_key = model_instance.db_key.redis_key
        hash_key = field._get_data_hash_key(model_instance, field_name)

        # Only initialize if no data exists yet
        existing = POPOTO_REDIS_DB.hget(hash_key, member_key)
        if not existing:
            initial_data = {
                "confidence": field.initial_confidence,
                "evidence_count": 0,
                "corroborations": 0,
                "contradictions": 0,
            }
            db = (
                pipeline
                if isinstance(pipeline, redis.client.Pipeline)
                else POPOTO_REDIS_DB
            )
            db.hset(hash_key, member_key, msgpack.packb(initial_data))

        return result

    @classmethod
    def on_delete(
        cls, model_instance, field_name, field_value, pipeline=None, **kwargs
    ):
        """Clean up companion hash entries on delete.

        Args:
            model_instance: The Model instance being deleted.
            field_name: The field name.
            field_value: The field value.
            pipeline: Optional Redis pipeline.
            **kwargs: Additional keyword arguments.

        Returns:
            Result from parent on_delete.
        """
        field = model_instance._meta.fields[field_name]

        if isinstance(field, ConfidenceField):
            member_key = (
                kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
            )
            hash_key = field._get_data_hash_key(model_instance, field_name)

            db = (
                pipeline
                if isinstance(pipeline, redis.client.Pipeline)
                else POPOTO_REDIS_DB
            )
            db.hdel(hash_key, member_key)

        return super().on_delete(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )
