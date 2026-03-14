"""WriteFilterMixin — selective encoding gate for persistence.

This module provides a mixin class that gates save() calls based on a
scoring function. Low-value records are silently discarded; high-value
records are tagged in a priority sorted set for preferential retrieval.

Design:
    - compute_filter_score() is abstract (raises NotImplementedError)
    - Score < min_threshold (0.2) -> SkipSaveException -> save() returns without persisting
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

    memory = Memory(key="low", content="noise", importance=0.1)
    memory.save()  # silently discarded (0.1 < 0.2)

    memory = Memory(key="mid", content="useful", importance=0.5)
    memory.save()  # persisted normally

    memory = Memory(key="high", content="critical", importance=0.9)
    memory.save()  # persisted AND added to priority set
"""

import logging

from ..exceptions import SkipSaveException
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.WriteFilter")


class WriteFilterMixin:
    """Mixin that gates save() based on a scoring function.

    Add as a base class alongside Model:
        class MyModel(WriteFilterMixin, Model):
            def compute_filter_score(self):
                return self.some_score_field or 0.0

    Class Attributes:
        _wf_min_threshold: Minimum score to persist. Default 0.2.
        _wf_priority_threshold: Score to tag as priority. Default 0.7.

    Note: Attributes prefixed with underscore to avoid conflict with
    Popoto's ModelBase metaclass, which requires public attributes to be Fields.
    """

    _wf_min_threshold = 0.2
    _wf_priority_threshold = 0.7

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

        self._write_filter_score = score

        if score < self._wf_min_threshold:
            raise SkipSaveException(
                f"Score {score:.3f} below threshold {self._wf_min_threshold}"
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
            POPOTO_REDIS_DB.zadd(priority_key, {redis_key: score})

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
            POPOTO_REDIS_DB.zrem(priority_key, redis_key)
