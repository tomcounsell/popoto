"""TrajectoryMemory -- Fingerprint-keyed procedural pattern recipe.

Stores completed task trajectories, clusters them by structural fingerprint,
and recalls the canonical "what worked last time" sequence by fingerprint.
This is procedural memory: the unit of recall is *a sequence of actions
plus an outcome*, not a fact.

Architecture::

    record_episode(fingerprint, trajectory, outcome, partition)
        |
        v
    [episode_model.save() -- append-only]
        |
        v
    crystallize(partition)  (periodic)
        |
        +-- group episodes by fingerprint tuple
        +-- for each group with len >= cluster_threshold:
        |       upsert pattern_model record
        |       update_confidence(signal=success_rate)
        |       write canonical (modal) trajectory
        v
    recall(fingerprint, partition, limit)
        |
        +-- pattern_model.query.filter(**fingerprint, partition=partition)
        +-- composite_score({confidence, last_reinforced})
        v
    list[pattern_model], ranked

The recipe is generic over ``episode_model`` and ``pattern_model``. The
consumer brings two model classes satisfying documented field
requirements (mirrors how ``ContextAssembler`` is generic over
``model_class``). The recipe owns *only* the generic primitive
(fingerprint shape, episode schema, crystallization, recall).

Note on ``pattern.observe(success=...)`` ergonomics:
    The issue's API sketch refers to ``pattern.observe(success=...)``.
    The recipe owns the observation call site — consumers never call
    ``observe`` directly. Inside :meth:`crystallize`, the recipe calls
    ``ConfidenceField.update_confidence(pattern, confidence_field,
    signal=...)`` on the consumer's behalf.

Dependencies:
    ConfidenceField, DecayingSortedField, ListField, KeyField (Popoto core)
    ``composite_score()`` query method
    ``compute_fingerprint()`` from policy_cache (re-exported here for ergonomics)

Example:
    from popoto import (
        AutoKeyField, ConfidenceField, DecayingSortedField, KeyField,
        ListField, Model,
    )
    from popoto.recipes.trajectory_memory import TrajectoryMemory

    class CyclicEpisode(Model):
        episode_id = AutoKeyField()
        partition = KeyField()
        problem_topology = KeyField()
        affected_layer = KeyField()
        trajectory = ListField(default=[])
        outcome = ListField(default=[])  # dict-serialisable
        recorded_at = DecayingSortedField(partition_by="partition")

    class ProceduralPattern(Model):
        pattern_id = AutoKeyField()
        partition = KeyField()
        problem_topology = KeyField()
        affected_layer = KeyField()
        canonical_sequence = ListField(default=[])
        confidence = ConfidenceField(initial_confidence=0.5)
        last_reinforced = DecayingSortedField(partition_by="partition")

    tm = TrajectoryMemory(
        episode_model=CyclicEpisode,
        pattern_model=ProceduralPattern,
        fingerprint_fields=["problem_topology", "affected_layer"],
    )
    tm.record_episode(
        fingerprint={"problem_topology": "bug_fix", "affected_layer": "agent"},
        trajectory=["read_logs", "edit_file", "run_tests"],
        outcome={"success": True},
        partition="team-a",
    )
    # ... record more episodes ...
    tm.crystallize(partition="team-a")
    patterns = tm.recall(
        fingerprint={"problem_topology": "bug_fix", "affected_layer": "agent"},
        partition="team-a",
    )
"""

import logging
from collections import Counter, defaultdict

from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.shortcuts import KeyField, ListField
from .policy_cache import compute_fingerprint  # re-exported for ergonomics

__all__ = ["TrajectoryMemory", "compute_fingerprint"]

logger = logging.getLogger("POPOTO.TrajectoryMemory")


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_RECALL_LIMIT = 5
"""Default maximum number of patterns returned by :meth:`recall`."""

DEFAULT_SCORE_WEIGHTS = {"confidence": 0.6, "last_reinforced": 0.4}
"""Default composite-score weights. Confidence dominates; freshness
breaks ties between equally-confident patterns."""

DEFAULT_CONFIDENCE_FIELD = "confidence"
DEFAULT_RECENCY_FIELD = "last_reinforced"
DEFAULT_TRAJECTORY_FIELD = "trajectory"
DEFAULT_OUTCOME_FIELD = "outcome"
DEFAULT_CANONICAL_SEQUENCE_FIELD = "canonical_sequence"
DEFAULT_PARTITION_FIELD = "partition"
DEFAULT_EPISODE_RECENCY_FIELD = "recorded_at"


# ---------------------------------------------------------------------------
# TrajectoryMemory
# ---------------------------------------------------------------------------


class TrajectoryMemory:
    """Fingerprint-keyed procedural-pattern recipe.

    Generic over ``episode_model`` and ``pattern_model``. The recipe
    introspects the supplied model classes — it does not define them.

    Args:
        episode_model: A Popoto ``Model`` class storing raw trajectories.
            Required fields: each name in ``fingerprint_fields`` as a
            ``KeyField``; the trajectory list field; the outcome field; the
            partition ``KeyField``; an episode-recency
            ``DecayingSortedField``.
        pattern_model: A Popoto ``Model`` class storing crystallized
            patterns. Required fields: each name in ``fingerprint_fields``
            as a ``KeyField``; canonical-sequence ``ListField``;
            ``ConfidenceField``; ``DecayingSortedField`` (recency);
            partition ``KeyField``.
        fingerprint_fields: Ordered list of field names that together
            identify a unique pattern under a partition. Each must exist as
            a ``KeyField`` on both ``episode_model`` and ``pattern_model``.
        cluster_threshold: Minimum episodes-per-cluster before
            crystallization promotes the group into a pattern. Defaults to
            ``Defaults.TRAJECTORY_CLUSTER_THRESHOLD`` when ``None``.
        score_weights: ``composite_score`` weights for recall. Defaults to
            ``{"confidence": 0.6, "last_reinforced": 0.4}``.
        partition_field: Name of the partition ``KeyField`` on both models.
            Default ``"partition"``.
        confidence_field: Name of the ``ConfidenceField`` on
            ``pattern_model``. Default ``"confidence"``.
        recency_field: Name of the ``DecayingSortedField`` on
            ``pattern_model`` tracking last-reinforced time. Default
            ``"last_reinforced"``.
        trajectory_field: Name of the trajectory ``ListField`` on
            ``episode_model``. Default ``"trajectory"``.
        outcome_field: Name of the outcome field on ``episode_model``.
            Default ``"outcome"``.
        canonical_sequence_field: Name of the canonical-sequence
            ``ListField`` on ``pattern_model``. Default
            ``"canonical_sequence"``.
        episode_recency_field: Name of the ``DecayingSortedField`` (or
            equivalent sortable timestamp) on ``episode_model``. Default
            ``"recorded_at"``.

    Raises:
        TypeError: If either model is missing a required field or has a
            field of the wrong type.
    """

    def __init__(
        self,
        episode_model,
        pattern_model,
        fingerprint_fields,
        cluster_threshold=None,
        score_weights=None,
        partition_field=DEFAULT_PARTITION_FIELD,
        confidence_field=DEFAULT_CONFIDENCE_FIELD,
        recency_field=DEFAULT_RECENCY_FIELD,
        trajectory_field=DEFAULT_TRAJECTORY_FIELD,
        outcome_field=DEFAULT_OUTCOME_FIELD,
        canonical_sequence_field=DEFAULT_CANONICAL_SEQUENCE_FIELD,
        episode_recency_field=DEFAULT_EPISODE_RECENCY_FIELD,
    ):
        if not fingerprint_fields:
            raise TypeError(
                "fingerprint_fields must be a non-empty list of field names"
            )

        self.episode_model = episode_model
        self.pattern_model = pattern_model
        self.fingerprint_fields = list(fingerprint_fields)
        self.cluster_threshold = (
            cluster_threshold
            if cluster_threshold is not None
            else Defaults.TRAJECTORY_CLUSTER_THRESHOLD
        )
        self.score_weights = (
            dict(score_weights)
            if score_weights is not None
            else dict(DEFAULT_SCORE_WEIGHTS)
        )
        self.partition_field = partition_field
        self.confidence_field = confidence_field
        self.recency_field = recency_field
        self.trajectory_field = trajectory_field
        self.outcome_field = outcome_field
        self.canonical_sequence_field = canonical_sequence_field
        self.episode_recency_field = episode_recency_field

        self._validate_models()

    # ------------------------------------------------------------------
    # Model introspection
    # ------------------------------------------------------------------

    def _validate_models(self):
        """Validate required fields exist with the expected types.

        Raises ``TypeError`` listing every missing/wrong-typed field so the
        consumer gets a single descriptive error rather than discovering
        them one at a time inside :meth:`crystallize`.
        """
        errors = []

        pattern_fields = self.pattern_model._meta.fields
        episode_fields = self.episode_model._meta.fields

        # Pattern model checks
        for fp_field in self.fingerprint_fields:
            f = pattern_fields.get(fp_field)
            if f is None:
                errors.append(
                    f"pattern_model {self.pattern_model.__name__!s} is missing "
                    f"fingerprint field '{fp_field}'"
                )
            elif not isinstance(f, KeyField):
                errors.append(
                    f"pattern_model field '{fp_field}' must be a KeyField "
                    f"(got {type(f).__name__})"
                )

        partition_pattern = pattern_fields.get(self.partition_field)
        if partition_pattern is None:
            errors.append(
                f"pattern_model is missing partition field '{self.partition_field}'"
            )
        elif not isinstance(partition_pattern, KeyField):
            errors.append(
                f"pattern_model field '{self.partition_field}' must be a "
                f"KeyField (got {type(partition_pattern).__name__})"
            )

        conf_f = pattern_fields.get(self.confidence_field)
        if conf_f is None:
            errors.append(
                f"pattern_model is missing ConfidenceField '{self.confidence_field}'"
            )
        elif not isinstance(conf_f, ConfidenceField):
            errors.append(
                f"pattern_model field '{self.confidence_field}' must be a "
                f"ConfidenceField (got {type(conf_f).__name__})"
            )

        rec_f = pattern_fields.get(self.recency_field)
        if rec_f is None:
            errors.append(
                f"pattern_model is missing DecayingSortedField '{self.recency_field}'"
            )
        elif not isinstance(rec_f, DecayingSortedField):
            errors.append(
                f"pattern_model field '{self.recency_field}' must be a "
                f"DecayingSortedField (got {type(rec_f).__name__})"
            )

        canon_f = pattern_fields.get(self.canonical_sequence_field)
        if canon_f is None:
            errors.append(
                f"pattern_model is missing ListField '{self.canonical_sequence_field}'"
            )
        elif not isinstance(canon_f, ListField):
            errors.append(
                f"pattern_model field '{self.canonical_sequence_field}' must "
                f"be a ListField (got {type(canon_f).__name__})"
            )

        # Episode model checks
        for fp_field in self.fingerprint_fields:
            f = episode_fields.get(fp_field)
            if f is None:
                errors.append(
                    f"episode_model {self.episode_model.__name__!s} is missing "
                    f"fingerprint field '{fp_field}'"
                )
            elif not isinstance(f, KeyField):
                errors.append(
                    f"episode_model field '{fp_field}' must be a KeyField "
                    f"(got {type(f).__name__})"
                )

        partition_ep = episode_fields.get(self.partition_field)
        if partition_ep is None:
            errors.append(
                f"episode_model is missing partition field '{self.partition_field}'"
            )
        elif not isinstance(partition_ep, KeyField):
            errors.append(
                f"episode_model field '{self.partition_field}' must be a "
                f"KeyField (got {type(partition_ep).__name__})"
            )

        traj_f = episode_fields.get(self.trajectory_field)
        if traj_f is None:
            errors.append(
                f"episode_model is missing trajectory field '{self.trajectory_field}'"
            )
        elif not isinstance(traj_f, ListField):
            errors.append(
                f"episode_model field '{self.trajectory_field}' must be a "
                f"ListField (got {type(traj_f).__name__})"
            )

        if self.outcome_field not in episode_fields:
            errors.append(
                f"episode_model is missing outcome field '{self.outcome_field}'"
            )

        ep_recency_f = episode_fields.get(self.episode_recency_field)
        if ep_recency_f is None:
            errors.append(
                f"episode_model is missing episode-recency field "
                f"'{self.episode_recency_field}'"
            )

        if errors:
            raise TypeError(
                "TrajectoryMemory model validation failed:\n  - "
                + "\n  - ".join(errors)
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_episode(self, fingerprint, trajectory, outcome, partition):
        """Persist a single trajectory episode (write path).

        Args:
            fingerprint: Dict of fingerprint-field values. Must contain
                every name in ``fingerprint_fields``.
            trajectory: List of action identifiers (will be stored verbatim
                in the trajectory ``ListField``).
            outcome: Outcome payload (any value the outcome field accepts).
            partition: Partition value (the partition ``KeyField`` value).

        Returns:
            The saved ``episode_model`` instance.

        Raises:
            ValueError: If ``fingerprint`` is missing any required field.
        """
        missing = [k for k in self.fingerprint_fields if k not in fingerprint]
        if missing:
            raise ValueError(
                f"record_episode fingerprint is missing required field(s): {missing}"
            )

        kwargs = {
            self.partition_field: partition,
            self.trajectory_field: list(trajectory),
            self.outcome_field: outcome,
        }
        for fp_field in self.fingerprint_fields:
            kwargs[fp_field] = fingerprint[fp_field]

        episode = self.episode_model(**kwargs)
        episode.save()
        return episode

    def crystallize(self, partition):
        """Cluster recent episodes into canonical patterns.

        Loads every episode in ``partition``, groups them by their
        fingerprint tuple, and for each group at least
        ``cluster_threshold`` episodes large:

        * Upserts a ``pattern_model`` record keyed by ``(partition,
          *fingerprint_values)``.
        * On existing patterns, observes only episodes strictly newer
          than the pattern's recency score (the watermark filter below).
        * Recomputes the canonical sequence as the modal trajectory
          across the group, ties broken by the most recent episode.
        * Advances the pattern's recency field (``last_reinforced``) to
          the **max observed episode timestamp** (the watermark), saved
          with ``skip_auto_now=True`` — never to the wall-clock time of
          the save.

        Idempotence guarantee (and its limits):

        * **Sequential re-run idempotence** via watermark filtering:
          because the stored watermark equals the max *observed* episode
          timestamp and the filter is strict ``>``, re-running
          crystallize on an unchanged episode set observes nothing and
          produces zero confidence drift. An episode recorded between
          the episode query and the save (i.e. with a timestamp above
          the watermark but below wall-clock save time) is picked up by
          the *next* run rather than lost. No commutativity of the
          underlying confidence update is assumed or claimed.
        * **Concurrent crystallization of the same partition is NOT
          coordinated.** The recipe performs a read-modify-write with no
          locking, so two crystallizers racing on one partition can
          double-observe the same episodes. Run one crystallizer per
          partition.

        Watermark semantics to be aware of:

        * The recency field's stored score equals the max **observed
          episode** ``recorded_at``, which may predate the wall-clock
          crystallize call. A ZSCORE read of the recency index therefore
          means "newest episode processed", not "when crystallize last
          ran", and recency-weighted recall ranks patterns by *episode*
          time — batched/nightly crystallization makes patterns appear
          correspondingly older.
        * **Clock-sync assumption:** episode writers and the
          crystallizer are assumed to be within ordinary NTP sync. A
          writer whose clock lags the watermark can record an episode
          *below* it, which the strict ``>`` filter then skips forever.
        * Crystallize-triggered saves use ``skip_auto_now=True``, so any
          **other** ``auto_now`` field on ``pattern_model`` is frozen by
          (and never populated by) crystallize-triggered saves.

        Args:
            partition: Partition value to crystallize. Crystallization is
                scoped per partition.

        Returns:
            List of ``pattern_model`` instances that were created or
            reinforced by this call.
        """
        episodes = list(
            self.episode_model.query.filter(**{self.partition_field: partition}).all()
        )

        # Group by fingerprint tuple
        groups = defaultdict(list)
        for episode in episodes:
            key = tuple(getattr(episode, f) for f in self.fingerprint_fields)
            groups[key].append(episode)

        affected = []
        for fingerprint_tuple, group in groups.items():
            if len(group) < self.cluster_threshold:
                continue

            fingerprint = dict(zip(self.fingerprint_fields, fingerprint_tuple))

            # Look up existing pattern (unique under partition, by KeyFields)
            existing = list(
                self.pattern_model.query.filter(
                    **{self.partition_field: partition, **fingerprint}
                ).all()
            )

            canonical = self._canonical_sequence(group)

            if not existing:
                # New pattern (unsaved): observe every episode in the group
                pattern = self._create_pattern(
                    partition=partition,
                    fingerprint=fingerprint,
                    canonical=canonical,
                )
                observed_episodes = group
            else:
                pattern = existing[0]
                # Re-running idempotence: only observe episodes strictly
                # newer than the stored watermark (max observed episode
                # timestamp from the previous run).
                last_reinforced = self._episode_score(pattern, self.recency_field)
                observed_episodes = [
                    e
                    for e in group
                    if self._episode_score(e, self.episode_recency_field)
                    > last_reinforced
                ]
                if not observed_episodes:
                    # No new evidence — nothing to do.
                    continue

                # Recompute canonical sequence on the full group (deterministic).
                setattr(pattern, self.canonical_sequence_field, canonical)

            # Watermark: the max OBSERVED episode timestamp, not the
            # wall-clock save time. Saving the wall clock would let an
            # episode recorded between the query above and the save fall
            # below the stored score and be filtered out forever.
            watermark = max(
                self._episode_score(e, self.episode_recency_field)
                for e in observed_episodes
            )
            setattr(pattern, self.recency_field, watermark)

            if not existing:
                # Bootstrap save: ConfidenceField.update_confidence is an
                # atomic Lua read-modify-write keyed on the member's redis
                # key, and requires the instance to already exist in Redis.
                # skip_auto_now keeps the explicit watermark here too, so
                # the new-pattern path never carries wall-clock semantics.
                pattern.save(skip_auto_now=True)

            self._observe_episodes(pattern, observed_episodes)
            # Single authoritative save for both branches: persists the
            # watermark (skip_auto_now suppresses the DecayingSortedField's
            # auto_now wall-clock overwrite) and the canonical_sequence
            # update. Note: skip_auto_now also freezes any OTHER auto_now
            # field on pattern_model for crystallize-triggered saves.
            pattern.save(skip_auto_now=True)

            affected.append(pattern)

        return affected

    def recall(self, fingerprint, partition, limit=None, score_weights=None):
        """Recall ranked patterns for the given fingerprint (read path).

        Recall is keyed exact-match by fingerprint plus partition. Because
        fingerprint fields are ``KeyField`` s and the (partition,
        fingerprint) combination is unique under a partition, this is a
        small, bounded query. The composite-score ranking matters when the
        caller supplies a *partial* fingerprint (a subset of
        ``fingerprint_fields``) — in that case multiple patterns may
        match and are ranked by ``score_weights``.

        Args:
            fingerprint: Dict of fingerprint-field values. May be a subset
                of ``fingerprint_fields`` (partial-fingerprint recall).
                Empty dicts return ``[]`` — caller must commit to at least
                one fingerprint dimension to keep recall keyed rather than
                an unfiltered scan.
            partition: Partition value (exact-match filter).
            limit: Maximum patterns to return. Defaults to
                ``DEFAULT_RECALL_LIMIT`` (5). ``0`` or negative returns
                ``[]``.
            score_weights: Override the default composite-score weights for
                this call.

        Returns:
            List of ``pattern_model`` instances ranked by composite score
            (descending).

        Raises:
            ValueError: If ``fingerprint`` contains a key not in
                ``fingerprint_fields``.
        """
        if not fingerprint:
            return []
        if limit is None:
            limit = DEFAULT_RECALL_LIMIT
        if limit <= 0:
            return []

        # Validate fingerprint keys belong to fingerprint_fields
        unknown = [k for k in fingerprint if k not in self.fingerprint_fields]
        if unknown:
            raise ValueError(
                f"recall fingerprint has unknown field(s): {unknown}; "
                f"expected subset of {self.fingerprint_fields}"
            )

        weights = score_weights if score_weights is not None else self.score_weights

        # Drop any non-positive weight entries. At least one positive
        # weight is required for composite_score to run.
        positive_weights = {k: v for k, v in weights.items() if v > 0}
        if not positive_weights:
            return []

        # Resolve allowed redis_keys via the filter step. ``filter()`` walks
        # KeyField indexes and is cheap; we then restrict composite_score
        # to that allowed set via ``post_filter``. (composite_score itself
        # ranks via partition-scoped sorted sets and is unaware of
        # non-partition KeyField filters.)
        filters = {self.partition_field: partition, **fingerprint}
        allowed_keys = {
            inst.db_key.redis_key
            for inst in self.pattern_model.query.filter(**filters).all()
        }
        if not allowed_keys:
            return []

        def _post_filter(member, _score, _allowed=allowed_keys):
            return member in _allowed

        return self.pattern_model.query.filter(
            **{self.partition_field: partition}
        ).composite_score(
            indexes=positive_weights,
            limit=limit,
            post_filter=_post_filter,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _canonical_sequence(self, episodes):
        """Modal trajectory across the group, ties broken by recency.

        The trajectory is rendered as a tuple for hashability, then
        returned as a list. If multiple trajectories share the top
        frequency, the most-recent episode's trajectory wins (deterministic
        as long as episode timestamps are stable).
        """
        if not episodes:
            return []

        counts = Counter()
        # Track the most recent episode score per trajectory for tie-break.
        latest_score = {}
        for ep in episodes:
            traj = tuple(getattr(ep, self.trajectory_field) or [])
            counts[traj] += 1
            score = self._episode_score(ep, self.episode_recency_field) or 0.0
            if traj not in latest_score or score > latest_score[traj]:
                latest_score[traj] = score

        top_count = max(counts.values())
        contenders = [traj for traj, c in counts.items() if c == top_count]
        if len(contenders) == 1:
            winner = contenders[0]
        else:
            # Tie-break: most recent trajectory wins.
            winner = max(contenders, key=lambda t: latest_score.get(t, 0.0))

        return list(winner)

    def _create_pattern(self, partition, fingerprint, canonical):
        """Instantiate a new pattern record WITHOUT saving it.

        :meth:`crystallize` owns the save: it sets the recency watermark
        first and persists with ``save(skip_auto_now=True)``, so no
        auto_now wall-clock score is ever written for a new pattern.
        """
        kwargs = {
            self.partition_field: partition,
            self.canonical_sequence_field: list(canonical),
        }
        for k, v in fingerprint.items():
            kwargs[k] = v
        return self.pattern_model(**kwargs)

    def _observe_episodes(self, pattern, episodes):
        """Bayesian-update the pattern's confidence using each episode's
        outcome signal."""
        for ep in episodes:
            signal = self._episode_signal(ep)
            ConfidenceField.update_confidence(
                pattern,
                self.confidence_field,
                signal=signal,
            )

    def _episode_signal(self, episode):
        """Derive a 0..1 confidence signal from an episode's outcome.

        Default heuristic:
          * If outcome is a dict with a ``"success"`` key: True -> 0.9,
            False -> 0.1, float -> clamped to [0, 1].
          * If outcome is a numeric value: clamped to [0, 1].
          * Otherwise: 0.5 (neutral).

        Subclass to plug in a different mapping.
        """
        outcome = getattr(episode, self.outcome_field, None)
        if isinstance(outcome, dict) and "success" in outcome:
            val = outcome["success"]
            if isinstance(val, bool):
                return 0.9 if val else 0.1
            try:
                return max(0.0, min(1.0, float(val)))
            except (TypeError, ValueError):
                return 0.5
        if isinstance(outcome, (int, float)) and not isinstance(outcome, bool):
            return max(0.0, min(1.0, float(outcome)))
        return 0.5

    @staticmethod
    def _episode_score(instance, field_name):
        """Return the raw stored score for a DecayingSortedField on an
        instance, or 0 if it cannot be read.

        DecayingSortedField stores its score as the field attribute on a
        saved instance (set via ``auto_now``). This method is tolerant of
        missing values during testing or unsaved instances.
        """
        val = getattr(instance, field_name, None)
        if val is None:
            return 0.0
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0
