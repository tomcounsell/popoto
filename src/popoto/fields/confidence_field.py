"""ConfidenceField — Bayesian certainty tracking with entrainment.

Maintains a Bayesian confidence score per member, updated atomically via
Lua script. Precision grows with sqrt(n), so early evidence has outsized
effect while established beliefs resist change.

Companion Redis hash stores per-member confidence metadata:
    - $ConfidencF:{Model}:{field}:data — msgpack {confidence, evidence_count,
      corroborations, contradictions}

Example:
    class Memory(Model):
        key = UniqueKeyField()
        content = StringField()
        certainty = ConfidenceField(initial_confidence=0.5)

    memory = Memory.create(key="fact1", content="The sky is blue")
    ConfidenceField.update_confidence(memory, "certainty", signal=0.9)
    print(ConfidenceField.get_confidence(memory, "certainty"))
"""

import logging

import msgpack
import redis

from ..exceptions import ModelException
from ..redis_db import POPOTO_REDIS_DB
from .field import Field

logger = logging.getLogger("POPOTO.ConfidenceField")

# Lua script: atomic Bayesian update of confidence companion hash.
# KEYS[1] = companion hash key
# ARGV[1] = member key (redis_key of the model instance)
# ARGV[2] = signal (float 0-1)
# ARGV[3] = initial_confidence (default for missing data)
BAYESIAN_UPDATE_LUA = """
local hash_key = KEYS[1]
local member = ARGV[1]
local signal = tonumber(ARGV[2])
local initial_confidence = tonumber(ARGV[3])

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

-- Bayesian update: precision grows with sqrt(n)
local new_confidence = confidence + (signal - confidence) / math.sqrt(evidence_count + 1)

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
    """A Field that tracks Bayesian confidence metadata in a companion Redis hash.

    Not a SortedField — confidence is metadata, not a ranking dimension.
    Stores {confidence, evidence_count, corroborations, contradictions} per member.

    Args:
        initial_confidence: Starting confidence for new members. Default 0.5.

    Example:
        class Memory(Model):
            key = UniqueKeyField()
            content = StringField()
            certainty = ConfidenceField(initial_confidence=0.5)

        memory = Memory.create(key="fact1", content="The sky is blue")
        ConfidenceField.update_confidence(memory, "certainty", signal=0.9)
        print(ConfidenceField.get_confidence(memory, "certainty"))
    """

    def __init__(self, **kwargs):
        self.initial_confidence = kwargs.pop("initial_confidence", 0.5)

        if not 0 <= self.initial_confidence <= 1:
            raise ModelException(
                f"initial_confidence must be between 0 and 1 "
                f"(got {self.initial_confidence})"
            )

        # ConfidenceField stores float confidence values
        kwargs.setdefault("type", float)
        kwargs.setdefault("default", None)
        kwargs.setdefault("null", True)
        super().__init__(**kwargs)

    def _get_data_hash_key(self, model_instance, field_name):
        """Build the Redis key for the confidence companion hash.

        Pattern: $ConfidencF:{Model}:{field}:data
        """
        base_key = self.get_special_use_field_db_key(model_instance, field_name)
        return base_key.redis_key + ":data"

    @classmethod
    def on_save(cls, model_instance, field_name, field_value, pipeline=None, **kwargs):
        """Initialize companion hash with initial_confidence on first save."""
        result = super().on_save(
            model_instance, field_name, field_value, pipeline=pipeline, **kwargs
        )

        field = model_instance._meta.fields[field_name]
        if not isinstance(field, ConfidenceField):
            return result

        member_key = model_instance.db_key.redis_key
        data_hash_key = field._get_data_hash_key(model_instance, field_name)

        # Initialize with HSETNX (atomic set-if-not-exists, no race condition)
        initial_data = {
            "confidence": field.initial_confidence,
            "evidence_count": 0,
            "corroborations": 0,
            "contradictions": 0,
        }
        db = (
            pipeline if isinstance(pipeline, redis.client.Pipeline) else POPOTO_REDIS_DB
        )
        db.hsetnx(data_hash_key, member_key, msgpack.packb(initial_data))

        return result

    @classmethod
    def on_delete(
        cls, model_instance, field_name, field_value, pipeline=None, **kwargs
    ):
        """Remove companion hash entry on delete."""
        field = model_instance._meta.fields[field_name]

        if isinstance(field, ConfidenceField):
            member_key = (
                kwargs.get("saved_redis_key") or model_instance.db_key.redis_key
            )
            data_hash_key = field._get_data_hash_key(model_instance, field_name)

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
        """Update confidence for a member using Bayesian formula.

        Note: Always executes immediately via Lua EVAL (not batched into
        pipeline) because the Lua script needs to read-modify-write atomically.
        The pipeline parameter is accepted for API consistency but unused.

        Args:
            model_instance: The Model instance to update.
            field_name: Name of the ConfidenceField on the model.
            signal: Float 0-1. Values >= 0.5 corroborate, < 0.5 contradict.
            pipeline: Optional Redis pipeline (unused — Lua EVAL is atomic).

        Returns:
            float: The new confidence value.

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

        if not POPOTO_REDIS_DB.exists(member_key):
            raise TypeError("update_confidence() requires a saved model instance")

        data_hash_key = field._get_data_hash_key(model_instance, field_name)

        result = POPOTO_REDIS_DB.eval(
            BAYESIAN_UPDATE_LUA,
            1,  # number of KEYS
            data_hash_key,
            member_key,
            str(signal),
            str(field.initial_confidence),
        )

        return float(result[0])

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
        data_hash_key = field._get_data_hash_key(model_instance, field_name)

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
        data_hash_key = field._get_data_hash_key(model_instance, field_name)

        raw = POPOTO_REDIS_DB.hget(data_hash_key, member_key)
        if raw is None:
            return {
                "confidence": field.initial_confidence,
                "evidence_count": 0,
                "corroborations": 0,
                "contradictions": 0,
            }

        return msgpack.unpackb(raw, raw=False)
