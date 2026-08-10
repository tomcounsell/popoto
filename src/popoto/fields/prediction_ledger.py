"""PredictionLedgerMixin — Outcome tracking with auto-resolution.

Provides a mixin for recording predictions before acting and resolving them
against actual outcomes. High prediction errors feed back into ConfidenceField
to reduce trust in bad knowledge. Auto-resolution via ObservationProtocol
handles the common case where outcomes are inferred from behavior.

Redis Key Patterns:
    - $PL:{ClassName}:meta:{pk} — hash storing prediction metadata (msgpack)
    - $PL:{ClassName}:errors:{partition} — sorted set of PKs scored by |error|

Example:
    from popoto import Model, UniqueKeyField, StringField
    from popoto.fields.prediction_ledger import PredictionLedgerMixin
    from popoto.fields.confidence_field import ConfidenceField

    class Memory(PredictionLedgerMixin, Model):
        key = UniqueKeyField()
        content = StringField()
        certainty = ConfidenceField()

        _pl_partition = "default"

    memory = Memory.create(key="fact1", content="sky is blue")
    PredictionLedgerMixin.record_prediction(memory, predicted={"relevance": 0.9})
    PredictionLedgerMixin.resolve_prediction(memory, actual={"relevance": 0.3})
"""

import datetime
import logging
import statistics
import time

import msgpack

from ..redis_db import POPOTO_REDIS_DB
from .constants import Defaults

logger = logging.getLogger("POPOTO.PredictionLedger")

# Lua script: atomic prediction resolution.
# KEYS[1] = meta hash key ($PL:{ClassName}:meta:{pk})
# KEYS[2] = error sorted set key ($PL:{ClassName}:errors:{partition})
# ARGV[1] = member key (redis_key of the model instance)
# ARGV[2] = prediction_error (float)
# ARGV[3] = resolution_mode ("explicit" or "observed")
# ARGV[4] = resolved_at (timestamp)
#
# Returns: 1 if resolved, 0 if already resolved or no prediction exists
RESOLVE_PREDICTION_LUA = """
local meta_key = KEYS[1]
local error_key = KEYS[2]
local member = ARGV[1]
local prediction_error = tonumber(ARGV[2])
local resolution_mode = ARGV[3]
local resolved_at = ARGV[4]

-- Read existing prediction data
local raw = redis.call('HGET', meta_key, member)
if not raw then
    return 0
end

local ok, data = pcall(cmsgpack.unpack, raw)
if not ok or type(data) ~= 'table' then
    return 0
end

-- Check if already resolved
if data['resolved'] then
    return 0
end

-- Update prediction data
data['resolved'] = true
data['prediction_error'] = prediction_error
data['resolution_mode'] = resolution_mode
data['resolved_at'] = resolved_at

-- Write back
redis.call('HSET', meta_key, member, cmsgpack.pack(data))

-- ZADD error to sorted set (score = absolute error)
redis.call('ZADD', error_key, math.abs(prediction_error), member)

return 1
"""


class PredictionLedgerMixin:
    """Mixin that adds prediction recording, resolution, and error tracking.

    Add as a base class alongside Model:
        class MyModel(PredictionLedgerMixin, Model):
            _pl_partition = "default"

    Class Attributes (resolution order):
        _pl_partition: Partition key for error sorted set. Default "default".
          Plain class attribute on the mixin; subclasses override by assigning
          a different value.
        _pl_confidence_error_threshold, _pl_confidence_low_signal,
        _pl_auto_resolve_errors:
          - Subclasses may override by assigning a plain class attribute
            (subclass-dict-first lookup shadows the parent property).
          - Otherwise the mixin reads from ``Defaults.*`` at attribute-access
            time so runtime overrides via ``apply_overrides`` are observed.

    Note: Attributes prefixed with underscore to avoid conflict with
    Popoto's ModelBase metaclass, which requires public attributes to be Fields.
    """

    _pl_partition: str = "default"

    # Export/import: prediction metadata and the error sorted set accumulate
    # via record_prediction()/resolve() calls over the model's lifetime --
    # they are a function of prediction/resolution history, not of any
    # single record's stored field values, so they cannot be rebuilt or
    # meaningfully carried by a per-record export/import. Not addressed in
    # v1 -- see #556.
    roundtrip_policy: str = "partial"
    roundtrip_note: str = (
        "Prediction/resolution ledger history not carried or rebuilt by "
        "import; see #556"
    )

    # Runtime-lookup properties — read Defaults.* at attribute-access time
    # so that apply_overrides() patches of Defaults are observed. A subclass
    # may shadow any of these with a plain class attribute; subclass-dict-
    # first lookup in ``__getattribute__`` means the plain attribute wins
    # over the parent property without any descriptor trickery required.
    @property
    def _pl_confidence_error_threshold(self):
        return Defaults.PL_CONFIDENCE_ERROR_THRESHOLD

    @property
    def _pl_confidence_low_signal(self):
        return Defaults.PL_CONFIDENCE_LOW_SIGNAL

    @property
    def _pl_auto_resolve_errors(self):
        # Rebuilt per-call so overrides of the individual PL_AUTO_RESOLVE_*
        # constants take effect without a cache invalidation step.
        return {
            "acted": Defaults.PL_AUTO_RESOLVE_ACTED,
            "dismissed": Defaults.PL_AUTO_RESOLVE_DISMISSED,
            "contradicted": Defaults.PL_AUTO_RESOLVE_CONTRADICTED,
            "used": Defaults.PL_AUTO_RESOLVE_USED,
        }

    @staticmethod
    def _meta_key(instance):
        """Build the Redis key for prediction metadata hash.

        Pattern: $PL:{ClassName}:meta:{pk}

        Args:
            instance: A Model instance.

        Returns:
            str: Redis key for the prediction metadata hash.
        """
        class_name = type(instance).__name__
        pk = instance.db_key.redis_key
        return f"$PL:{class_name}:meta:{pk}"

    @staticmethod
    def _error_key(model_class, partition="default"):
        """Build the Redis key for the error sorted set.

        Pattern: $PL:{ClassName}:errors:{partition}

        Args:
            model_class: The Model class (or instance).
            partition: Partition key. Default "default".

        Returns:
            str: Redis key for the error sorted set.
        """
        class_name = (
            model_class.__name__
            if isinstance(model_class, type)
            else type(model_class).__name__
        )
        return f"$PL:{class_name}:errors:{partition}"

    @staticmethod
    def compute_prediction_error(predicted, actual):
        """Compute prediction error between predicted and actual dicts.

        For numeric values: |predicted - actual| / max(|predicted|, |actual|, 1)
        For string values: 0.0 if equal, 1.0 if different
        For missing keys: 1.0 error per missing key
        Overall error: mean across all keys.

        This method can be overridden on subclasses for custom error metrics.

        Args:
            predicted: Dict of predicted values.
            actual: Dict of actual values.

        Returns:
            float: Mean prediction error in [0, 1].
        """
        all_keys = set(list(predicted.keys()) + list(actual.keys()))
        if not all_keys:
            return 0.0

        errors = []
        for key in all_keys:
            if key not in predicted or key not in actual:
                errors.append(1.0)
                continue

            p_val = predicted[key]
            a_val = actual[key]

            if isinstance(p_val, (int, float)) and isinstance(a_val, (int, float)):
                denominator = max(abs(p_val), abs(a_val), 1)
                errors.append(abs(p_val - a_val) / denominator)
            elif isinstance(p_val, str) and isinstance(a_val, str):
                errors.append(0.0 if p_val == a_val else 1.0)
            else:
                # Mixed types or unsupported — treat as full error
                errors.append(0.0 if p_val == a_val else 1.0)

        return sum(errors) / len(errors)

    @classmethod
    def record_prediction(cls, instance, predicted, pipeline=None):
        """Record a prediction for a model instance.

        Stores prediction metadata in a Redis hash. The prediction can later
        be resolved with resolve_prediction() or auto_resolve().

        Args:
            instance: A saved Model instance.
            predicted: Dict of predicted values. Must not be None.
            pipeline: Optional Redis pipeline for batch operations.

        Raises:
            TypeError: If instance is unsaved (no redis_key).
            ValueError: If predicted is None.
        """
        if predicted is None:
            raise ValueError("predicted must not be None")

        try:
            member_key = instance.db_key.redis_key
        except Exception:
            raise TypeError("record_prediction() requires a saved model instance")

        if not POPOTO_REDIS_DB.exists(member_key):
            raise TypeError("record_prediction() requires a saved model instance")

        meta_key = cls._meta_key(instance)
        data = {
            "predicted": predicted,
            "resolved": False,
            "resolution_mode": None,
            "prediction_error": None,
            "resolved_at": None,
            "recorded_at": time.time(),
        }

        db = pipeline if pipeline is not None else POPOTO_REDIS_DB
        db.hset(meta_key, member_key, msgpack.packb(data))

    @classmethod
    def resolve_prediction(cls, instance, actual, pipeline=None):
        """Resolve a prediction with actual outcome values.

        Atomically reads the prediction, computes error, marks resolved,
        and ZADDs error to the error sorted set via Lua script.

        Args:
            instance: A saved Model instance with a recorded prediction.
            actual: Dict of actual values. Must not be None.
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            float or None: The prediction error, or None if no prediction
                exists or already resolved.

        Raises:
            TypeError: If instance is unsaved.
            ValueError: If actual is None.
        """
        if actual is None:
            raise ValueError("actual must not be None")

        try:
            member_key = instance.db_key.redis_key
        except Exception:
            raise TypeError("resolve_prediction() requires a saved model instance")

        if not POPOTO_REDIS_DB.exists(member_key):
            raise TypeError("resolve_prediction() requires a saved model instance")

        # Read current prediction to compute error
        meta_key = cls._meta_key(instance)
        raw = POPOTO_REDIS_DB.hget(meta_key, member_key)
        if raw is None:
            return None

        data = msgpack.unpackb(raw, raw=False)
        if data.get("resolved"):
            return None

        predicted = data.get("predicted", {})
        prediction_error = cls.compute_prediction_error(predicted, actual)

        # Get partition from instance class or default
        partition = getattr(instance, "_pl_partition", "default")
        error_key = cls._error_key(instance, partition)
        resolved_at = str(time.time())

        # Atomic resolution via Lua
        result = POPOTO_REDIS_DB.eval(
            RESOLVE_PREDICTION_LUA,
            2,  # number of KEYS
            meta_key,
            error_key,
            member_key,
            str(prediction_error),
            "explicit",
            resolved_at,
        )

        if result == 0:
            return None

        # Confidence feedback
        cls._apply_confidence_feedback(instance, prediction_error)

        # EventStream logging
        cls._log_resolution_event(instance, prediction_error, "explicit", pipeline)

        return prediction_error

    @classmethod
    def auto_resolve(cls, instance, outcome, pipeline=None):
        """Auto-resolve a prediction based on an ObservationProtocol outcome.

        Maps the outcome string to a prediction error value using the
        _pl_auto_resolve_errors class attribute, then resolves.

        Args:
            instance: A saved Model instance with a recorded prediction.
            outcome: One of "acted", "dismissed", "contradicted", "used".
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            float or None: The prediction error, or None if no prediction
                exists or already resolved.

        Raises:
            ValueError: If outcome is not a valid auto-resolve outcome.
        """
        error_map = getattr(
            instance,
            "_pl_auto_resolve_errors",
            {"acted": 0.1, "dismissed": 0.5, "contradicted": 0.9, "used": 0.3},
        )
        if outcome not in error_map:
            raise ValueError(
                f"Invalid outcome '{outcome}' for auto_resolve. "
                f"Valid outcomes: {sorted(error_map.keys())}"
            )

        try:
            member_key = instance.db_key.redis_key
        except Exception:
            return None

        if not POPOTO_REDIS_DB.exists(member_key):
            return None

        # Check if prediction exists and is unresolved
        meta_key = cls._meta_key(instance)
        raw = POPOTO_REDIS_DB.hget(meta_key, member_key)
        if raw is None:
            return None

        data = msgpack.unpackb(raw, raw=False)
        if data.get("resolved"):
            return None

        prediction_error = error_map[outcome]
        partition = getattr(instance, "_pl_partition", "default")
        error_key = cls._error_key(instance, partition)
        resolved_at = str(time.time())

        # Atomic resolution via Lua
        result = POPOTO_REDIS_DB.eval(
            RESOLVE_PREDICTION_LUA,
            2,  # number of KEYS
            meta_key,
            error_key,
            member_key,
            str(prediction_error),
            "observed",
            resolved_at,
        )

        if result == 0:
            return None

        # Confidence feedback
        cls._apply_confidence_feedback(instance, prediction_error)

        # EventStream logging
        cls._log_resolution_event(instance, prediction_error, "observed", pipeline)

        return prediction_error

    @classmethod
    def get_prediction_data(cls, instance):
        """Read current prediction metadata for an instance.

        Args:
            instance: A saved Model instance.

        Returns:
            dict or None: Prediction metadata dict, or None if no prediction.
        """
        try:
            member_key = instance.db_key.redis_key
        except Exception:
            return None

        meta_key = cls._meta_key(instance)
        raw = POPOTO_REDIS_DB.hget(meta_key, member_key)
        if raw is None:
            return None

        return msgpack.unpackb(raw, raw=False)

    @classmethod
    def get_highest_errors(cls, model_class, partition="default", limit=10):
        """Query instances with the highest prediction errors.

        Args:
            model_class: The Model class to query.
            partition: Partition key. Default "default".
            limit: Max results to return. Default 10.

        Returns:
            list: List of (member_key_str, error_float) tuples, ordered by
                descending error.
        """
        error_key = cls._error_key(model_class, partition)
        results = POPOTO_REDIS_DB.zrevrange(error_key, 0, limit - 1, withscores=True)
        return [
            (m.decode() if isinstance(m, bytes) else m, score) for m, score in results
        ]

    # ------------------------------------------------------------------
    # error_summary — grouped prediction-error aggregation (#352)
    # ------------------------------------------------------------------

    #: Built-in group_by bucketers. Each takes the msgpack-decoded meta dict
    #: and returns a hashable label. Unknown string names raise ValueError.
    _BUILTIN_GROUP_BY = {
        "hour",
        "weekday",
        "day",
    }

    @staticmethod
    def _apply_builtin_bucketer(name, data):
        """Apply a built-in bucketer to a single decoded meta dict.

        ``resolved_at`` is stored as ``str(time.time())`` (see
        prediction_ledger.py resolve_prediction() / auto_resolve()), so we
        must coerce to float. Entries with no / zero / non-numeric
        resolved_at return ``None`` so the caller can skip them.

        Built-in names (see ``_BUILTIN_GROUP_BY``):
            - ``"hour"`` -> int 0..23
            - ``"weekday"`` -> int 0..6 (Monday=0)
            - ``"day"`` -> str YYYY-MM-DD (ISO date)
        """
        raw_ts = data.get("resolved_at")
        try:
            ts = float(raw_ts or 0.0)
        except (TypeError, ValueError):
            return None
        if ts <= 0:
            return None
        dt = datetime.datetime.fromtimestamp(ts)
        if name == "hour":
            return dt.hour
        if name == "weekday":
            return dt.weekday()
        if name == "day":
            return dt.date().isoformat()
        return None

    @staticmethod
    def _stats_for(errors):
        """Compute summary stats for a list of absolute errors."""
        n = len(errors)
        if n == 0:
            return {
                "count": 0,
                "mean": 0.0,
                "stddev": 0.0,
                "p50": 0.0,
                "p90": 0.0,
                "p99": 0.0,
                "max": 0.0,
            }
        mean = statistics.fmean(errors)
        # stddev requires n >= 2; for n==1 return 0.0
        if n >= 2:
            try:
                stddev = statistics.pstdev(errors)
            except statistics.StatisticsError:
                stddev = 0.0
        else:
            stddev = 0.0
        sorted_errors = sorted(errors)

        def _percentile(pct):
            if n == 1:
                return sorted_errors[0]
            # Simple nearest-rank percentile on the sorted list.
            idx = min(n - 1, max(0, int(round((pct / 100.0) * (n - 1)))))
            return sorted_errors[idx]

        return {
            "count": n,
            "mean": mean,
            "stddev": stddev,
            "p50": _percentile(50),
            "p90": _percentile(90),
            "p99": _percentile(99),
            "max": sorted_errors[-1],
        }

    @classmethod
    def error_summary(
        cls,
        model_class,
        partition="default",
        group_by=None,
        limit=100,
    ):
        """Aggregate prediction errors across instances with optional grouping.

        Reads up to ``limit`` members from the error sorted set (top-|error|)
        plus their per-instance meta via a pipelined batch of HGET calls
        against ``$PL:{ClassName}:meta:{pk}`` hashes. The per-instance
        meta dict stores ``prediction_error`` and ``resolved_at``.

        Args:
            model_class: The Model class (same contract as
                ``get_highest_errors``).
            partition: Partition key. Default ``"default"``.
            group_by: One of:

                * ``None`` -> ungrouped; returns ``{"__all__": stats}``.
                * A callable ``(member_key, error) -> label``. The label
                  must be hashable; non-hashable labels raise ``TypeError``.
                * One of the built-in bucketer name strings: ``"hour"``,
                  ``"weekday"``, ``"day"``. Unknown strings raise
                  ``ValueError`` listing the known bucketers.
            limit: Max members to sample. Default 100. Pass a larger
                number for broader coverage; ``error_summary`` is an
                eventually-consistent sampling function, not an exhaustive
                scan.

        Returns:
            dict: ``{group_label: stats_dict}`` where ``stats_dict`` has
            keys ``count``, ``mean``, ``stddev``, ``p50``, ``p90``,
            ``p99``, ``max``. For ungrouped, the key is ``"__all__"``.

        Notes:
            * Corrupt msgpack entries are logged at warning and skipped.
            * When the error set is empty, returns ``{"__all__": {...}}``
              with ``count=0`` (no raise on empty inputs).
            * Not a cross-instance snapshot: pipelined HGETs are NOT
              transactional, so a resolution landing mid-batch may be
              observed for some instances and not others.
        """
        # Validate group_by eagerly so callers get a clear error before
        # we even hit Redis.
        if isinstance(group_by, str) and group_by not in cls._BUILTIN_GROUP_BY:
            raise ValueError(
                f"Unknown built-in group_by '{group_by}'. "
                f"Known bucketers: {sorted(cls._BUILTIN_GROUP_BY)}."
            )

        error_key = cls._error_key(model_class, partition)

        # Optional large-set warning per plan Risk 4.
        try:
            cardinality = POPOTO_REDIS_DB.zcard(error_key)
            if cardinality > 10_000 and limit < 0:
                logger.warning(
                    "error_summary over %d errors with unbounded limit — "
                    "this is a sampling function, not an exhaustive scan",
                    cardinality,
                )
        except Exception:
            pass

        # ZRANGE-by-|error| descending. Use ZREVRANGE to get the top-N.
        if limit <= 0:
            raw_results = []
        else:
            raw_results = POPOTO_REDIS_DB.zrevrange(
                error_key, 0, limit - 1, withscores=True
            )

        if not raw_results:
            # When group_by is provided, an empty set produces no groups — return {}
            # rather than {"__all__": ...}, which would be semantically misleading
            # (the caller expects per-group keys, not the ungrouped sentinel).
            if group_by is not None:
                return {}
            return {"__all__": cls._stats_for([])}

        # Decode members to strings and build a list of (member_key, error)
        decoded = []
        for m, score in raw_results:
            if isinstance(m, bytes):
                m = m.decode()
            try:
                decoded.append((m, float(score)))
            except (TypeError, ValueError):
                continue

        class_name = (
            model_class.__name__
            if isinstance(model_class, type)
            else type(model_class).__name__
        )

        # Pipelined HGETs. Each PL instance has its own meta hash keyed by
        # $PL:{ClassName}:meta:{pk}, so we do one HGET per member — NOT a
        # single HMGET over one hash (there is no such shared hash).
        pipe = POPOTO_REDIS_DB.pipeline()
        for member_key, _ in decoded:
            meta_key = f"$PL:{class_name}:meta:{member_key}"
            pipe.hget(meta_key, member_key)
        raw_meta = pipe.execute()

        # Decode msgpack; log-and-skip corrupt entries.
        decoded_meta = []
        for (member_key, err), raw in zip(decoded, raw_meta):
            if raw is None:
                decoded_meta.append((member_key, err, None))
                continue
            try:
                data = msgpack.unpackb(raw, raw=False)
            except Exception as e:
                logger.warning(
                    "error_summary: corrupt msgpack for %s — skipping (%s)",
                    member_key,
                    e,
                )
                continue
            decoded_meta.append((member_key, err, data))

        # Group and compute.
        if group_by is None:
            errors_only = [err for _, err, _ in decoded_meta]
            return {"__all__": cls._stats_for(errors_only)}

        groups = {}
        if callable(group_by):
            for member_key, err, _data in decoded_meta:
                try:
                    label = group_by(member_key, err)
                except Exception as e:
                    logger.warning(
                        "error_summary: group_by callable raised for %s — "
                        "skipping (%s)",
                        member_key,
                        e,
                    )
                    continue
                groups.setdefault(label, []).append(err)
        else:
            # group_by is a built-in bucketer name (validated above).
            for member_key, err, data in decoded_meta:
                if data is None:
                    # No meta read -> we cannot apply any time-based bucket.
                    continue
                label = cls._apply_builtin_bucketer(group_by, data)
                if label is None:
                    continue
                groups.setdefault(label, []).append(err)

        return {label: cls._stats_for(errs) for label, errs in groups.items()}

    @classmethod
    def _apply_confidence_feedback(cls, instance, prediction_error):
        """If error exceeds threshold and model has ConfidenceField, reduce confidence.

        Args:
            instance: A Model instance.
            prediction_error: The computed prediction error.
        """
        threshold = getattr(instance, "_pl_confidence_error_threshold", 0.7)
        low_signal = getattr(instance, "_pl_confidence_low_signal", 0.2)

        if prediction_error <= threshold:
            return

        from .confidence_field import ConfidenceField

        for field_name, field in instance._meta.fields.items():
            if isinstance(field, ConfidenceField):
                try:
                    ConfidenceField.update_confidence(
                        instance, field_name, signal=low_signal
                    )
                except (TypeError, ValueError):
                    pass  # Graceful degradation for unsaved instances

    @classmethod
    def _log_resolution_event(cls, instance, prediction_error, mode, pipeline=None):
        """If model uses EventStreamMixin, log the resolution event.

        Args:
            instance: A Model instance.
            prediction_error: The computed prediction error.
            mode: Resolution mode ("explicit" or "observed").
            pipeline: Optional Redis pipeline.
        """
        from .event_stream import EventStreamMixin

        if isinstance(instance, EventStreamMixin):
            try:
                instance._xadd_event(
                    op="prediction_resolved",
                    extra_fields={
                        "prediction_error": str(prediction_error),
                        "resolution_mode": mode,
                    },
                    pipeline=pipeline,
                )
            except Exception:
                logger.warning(
                    "PredictionLedger event logging failed for %s",
                    type(instance).__name__,
                )
