"""WriteFilterMixin — selective encoding gate for persistence.

This module provides a mixin class that gates save() calls based on a
scoring function. Low-value records are silently discarded; high-value
records are tagged in a priority sorted set for preferential retrieval.

Design:
    - compute_filter_score() is abstract (raises NotImplementedError)
    - Score < min_threshold (0.1, empirically tuned 2026-04-17) -> SkipSaveException -> save() returns without persisting
    - Score >= min_threshold and < priority_threshold (0.7) -> normal save
    - Score >= priority_threshold (0.7) -> normal save AND ZADD to priority sorted set

Redis Key Patterns:
    - $WF:{ClassName}:priority — sorted set of high-value record PKs scored by filter score

Example:
    class Memory(WriteFilterMixin, Model):
        key = UniqueKeyField()
        content = StringField()
        importance = FloatField(default=0.0)

        def compute_filter_score(self):
            return self.importance or 0.0

    memory = Memory(key="low", content="noise", importance=0.05)
    memory.save()  # silently discarded (0.05 < 0.1)

    memory = Memory(key="mid", content="useful", importance=0.5)
    memory.save()  # persisted normally

    memory = Memory(key="high", content="critical", importance=0.9)
    memory.save()  # persisted AND added to priority set
"""

import logging

from ..exceptions import SkipSaveException

# The accessor, not ``from ..redis_db import POPOTO_REDIS_DB``: that plain
# import captures a snapshot, and ``set_REDIS_DB_settings()`` rebinds
# ``redis_db``'s global without updating it, so the importer keeps issuing
# commands against the pre-reconfiguration client (#655).
from ..redis_db import get_REDIS_DB
from .constants import Defaults, _read_tombstone_prior_switch
from .tombstone_prior import TombstonePriorStore, penalty_for

logger = logging.getLogger("POPOTO.WriteFilter")

#: Sentinel distinguishing "not yet resolved" from a resolved ``None`` in the
#: per-class fingerprint-field cache. Without it, a model with no
#: ``ExistenceFilter`` would re-scan its field set on every single save.
_UNRESOLVED = object()


class WriteFilterMixin:
    """Mixin that gates save() based on a scoring function.

    Add as a base class alongside Model:
        class MyModel(WriteFilterMixin, Model):
            def compute_filter_score(self):
                return self.some_score_field or 0.0

    Class Attributes (resolution order):
        _wf_min_threshold (default 0.1, empirically tuned 2026-04-17):
          - Subclass may set it as a plain class attribute to override the
            default (e.g. ``_wf_min_threshold = 0.5``). Because Python's
            ``__getattribute__`` consults the subclass dict first, a plain
            subclass attribute shadows the parent property.
          - Otherwise the mixin's property returns ``Defaults.WF_MIN_THRESHOLD``
            **at access time** so that runtime overrides (e.g. from
            ``tests/benchmarks/overrides.apply_overrides``) take effect.
        _wf_priority_threshold (default 0.7): same semantics. Reads
            ``Defaults.WF_PRIORITY_THRESHOLD`` at access time.

    Note: Attributes prefixed with underscore to avoid conflict with
    Popoto's ModelBase metaclass, which requires public attributes to be Fields.
    """

    # Runtime-lookup properties — read Defaults.* at attribute-access time
    # so that apply_overrides() patches of Defaults are observed.
    # A subclass may shadow either property with a plain class attribute
    # (e.g. ``_wf_min_threshold = 0.5``); subclass-dict-first lookup in
    # ``__getattribute__`` means the plain attribute wins over the parent
    # property without any descriptor trickery required.
    @property
    def _wf_min_threshold(self):
        return Defaults.WF_MIN_THRESHOLD

    @property
    def _wf_priority_threshold(self):
        return Defaults.WF_PRIORITY_THRESHOLD

    # Export/import: the priority ZSET ($WF:{ClassName}:priority) is fully
    # recomputed from compute_filter_score() by _tag_priority() on every
    # save, so nothing needs to be carried across export/import. The gate
    # itself (_check_write_filter) is honored by default on import and can
    # be bypassed per-record via Model.save(skip_write_filter=True) -- a
    # driver-level flag, not carried state.
    roundtrip_policy: str = "rebuild"

    def compute_filter_score(self):
        """Compute the write filter score for this instance.

        Must be overridden by subclasses. Return a float between 0.0 and 1.0.

        Returns:
            float: Score indicating record importance.

        Raises:
            NotImplementedError: If not overridden by subclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} uses WriteFilterMixin but does not "
            f"implement compute_filter_score()"
        )

    def _wf_key(self, kind):
        """Build a write filter Redis key.

        Args:
            kind: Key type, currently only 'priority'

        Returns:
            str: Redis key like '$WF:ClassName:priority'
        """
        class_name = type(self).__name__
        return f"$WF:{class_name}:{kind}"

    def _check_write_filter(self):
        """Evaluate the write filter score and gate the save.

        Computes the score, caches it on self._write_filter_score,
        and raises SkipSaveException if below min_threshold.

        Returns:
            float: The computed score (also cached on self._write_filter_score)

        Raises:
            SkipSaveException: If score < _wf_min_threshold
        """
        score = self.compute_filter_score()

        # Handle None or non-numeric gracefully
        if score is None:
            score = 0.0
        try:
            score = float(score)
        except (TypeError, ValueError):
            score = 0.0

        # Negative prior (#494) applied HERE — after the None/non-numeric
        # normalization above, and before the threshold comparison below. The
        # order is load-bearing in both directions: normalizing first means a
        # non-numeric score is already 0.0 when the multiplier hits it (0.0 *
        # anything is 0.0, so behavior is unchanged), and comparing after means
        # the drawdown feeds the EXISTING gate rather than introducing a second
        # rejection path. A drawn-down score that falls under the threshold is
        # dropped by the same SkipSaveException as any other low score.
        score = self._apply_tombstone_prior(score)

        self._write_filter_score = score

        if score < self._wf_min_threshold:
            raise SkipSaveException(
                f"Score {score:.3f} below threshold {self._wf_min_threshold}"
            )

        return score

    @classmethod
    def _wf_fingerprint_field(cls):
        """Return this model's content-fingerprint field, or None.

        The auto-detect for the tombstone negative prior (#494). A model with
        no ``ExistenceFilter``, or one whose ``fingerprint_fn`` is unset, has no
        content identity — there is nothing a buried record could match against
        — so the write path skips the consult entirely and issues zero extra
        Redis commands. That is what makes "a deployment not using tombstones
        sees byte-identical write behavior" structurally true rather than
        dependent on a flag, while keeping the capability default-ON and
        auto-detected rather than opt-in.

        Cached in the class's own ``__dict__`` (never inherited from a parent,
        so subclasses resolve independently) because a model's field set does
        not change after definition and the scan should be paid once.
        """
        cached = cls.__dict__.get("_wf_fingerprint_field_cache", _UNRESOLVED)
        if cached is not _UNRESOLVED:
            return cached

        from .existence_filter import ExistenceFilter

        field = None
        meta = getattr(cls, "_meta", None)
        for candidate in getattr(meta, "fields", {}).values():
            if isinstance(candidate, ExistenceFilter):
                if getattr(candidate, "fingerprint_fn", None) is not None:
                    field = candidate
                break

        cls._wf_fingerprint_field_cache = field
        return field

    def _apply_tombstone_prior(self, score):
        """Draw ``score`` down if this record's content has been buried before.

        A tombstone (#491) is durable evidence that a kind of memory was
        learned to be worthless. This turns that evidence into a multiplicative
        penalty on the write-filter score, escalating with the number of times
        the same content fingerprint has been buried.

        Three short-circuits, in cost order, all returning the score untouched:
        the deploy-level kill switch; a model with no content fingerprint (no
        Redis command issued at all); and zero recorded burials.

        Never raises: any failure logs a warning and returns the unmodified
        score. A memory system whose save() dies because a bookkeeping hash is
        unreachable is worse than one that occasionally misses a drawdown.

        Args:
            score: The normalized write-filter score.

        Returns:
            float: The adjusted score, or ``score`` unchanged.
        """
        try:
            if not _read_tombstone_prior_switch():
                return score

            field = type(self)._wf_fingerprint_field()
            if field is None:
                return score

            from .existence_filter import _compute_fingerprint_impl

            fingerprint = _compute_fingerprint_impl(field, self)
            store = TombstonePriorStore(type(self))
            burials = store.burial_count(fingerprint)
            if burials <= 0:
                return score

            penalty = penalty_for(burials)
            adjusted = score * penalty
            store.note_penalty(score, adjusted)
            logger.debug(
                "tombstone prior: %s drawn down %.4f -> %.4f "
                "(burials=%d, penalty=%.4f)",
                type(self).__name__,
                score,
                adjusted,
                burials,
                penalty,
            )
            return adjusted
        except Exception as exc:
            logger.warning(
                "tombstone prior: consult failed for %s, admitting unchanged: %s",
                type(self).__name__,
                exc,
            )
            return score

    def _tag_priority(self, pipeline=None):
        """Add this instance to the priority sorted set if score >= priority_threshold.

        Called after a successful save. Uses the cached score from _check_write_filter().

        Args:
            pipeline: Optional Redis pipeline for batch operations.
        """
        score = getattr(self, "_write_filter_score", None)
        if score is None or score < self._wf_priority_threshold:
            return

        priority_key = self._wf_key("priority")
        redis_key = self._redis_key or self.db_key.redis_key

        if pipeline:
            pipeline.zadd(priority_key, {redis_key: score})
        else:
            get_REDIS_DB().zadd(priority_key, {redis_key: score})

    def _delete_write_filter_keys(self, pipeline=None):
        """Remove this instance from the priority sorted set.

        Called during model deletion to clean up.

        Args:
            pipeline: Optional Redis pipeline for batch operations.
        """
        priority_key = self._wf_key("priority")
        redis_key = self._redis_key or self.db_key.redis_key

        if pipeline:
            pipeline.zrem(priority_key, redis_key)
        else:
            get_REDIS_DB().zrem(priority_key, redis_key)
