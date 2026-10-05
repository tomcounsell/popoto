"""ObservationProtocol + RecallProposal — Outcome-driven memory effects.

Provides lifecycle hooks for passive behavioral inference on memory models.
The application layer reports how the agent used retrieved memories; the ORM
applies effects atomically.

Three hooks:
    - ``on_read(instance)``: Fire when query hydrates an instance.
      Delegates to AccessTrackerMixin staging.
    - ``on_surfaced(instances, reason)``: Fire when proactive system pushes
      memories into agent context. Creates RecallProposal entries.
    - ``on_context_used(instances, outcome_map)``: Fire when application
      reports how the agent responded. Applies effects based on outcome.

Five outcomes:
    - ``acted``: Memory content appeared in agent's response. Strengthen.
    - ``dismissed``: Agent explicitly ignored/rejected. Weaken.
    - ``deferred``: Agent didn't address it. No effects, pressure builds.
    - ``contradicted``: Agent explicitly contradicted. Aggressively weaken.
    - ``used``: Agent consumed the memory (read + reasoned) but did not
      act on it in the response. Confirms the staged read and auto-resolves
      predictions with a moderate error, but does NOT touch ConfidenceField,
      CyclicDecayField, or DecayingSortedField. Observable distinction from
      ``deferred``: ``used`` records a confirmed-read trace, ``deferred``
      discards staged reads.

See Also:
    For the effects-per-field matrix (what each outcome does to ConfidenceField,
    CyclicDecayField, DecayingSortedField, AccessTracker, PredictionLedger), see
    the "Effects Matrix" section of docs/features/observation-protocol.md.

RecallProposal:
    Internal ORM infrastructure for tracking proactively surfaced memories.
    Redis ZSET keyed by model class and partition, scored by surfaced_at.
    TTL-based expiration (default 1 hour).

Example:
    from popoto import ObservationProtocol, RecallProposal

    # After agent processes memories:
    outcome_map = {
        memory1.db_key.redis_key: "acted",
        memory2.db_key.redis_key: "dismissed",
    }
    ObservationProtocol.on_context_used(memories, outcome_map)
"""

import logging
import time
from typing import Any

from ..backends.routing import non_redis_backend
from ..redis_db import get_REDIS_DB
from .constants import Defaults

logger = logging.getLogger("POPOTO.ObservationProtocol")

VALID_OUTCOMES = {"acted", "dismissed", "deferred", "contradicted", "used"}

# ---------------------------------------------------------------------------
# Tuning Constants — initialized from Defaults (validated via sweep #234)
# Functions reference these bare names; the benchmark harness patches both
# Defaults and these module-level aliases.
# ---------------------------------------------------------------------------

ACTED_CONFIDENCE_SIGNAL = Defaults.ACTED_CONFIDENCE_SIGNAL
"""Confidence signal sent to ConfidenceField on 'acted' outcome.
Higher values corroborate the memory more strongly.
Optimal range: [0.5, 1.0]. Insensitive within this range (nDCG stable)."""

CONTRADICTED_CONFIDENCE_SIGNAL = Defaults.CONTRADICTED_CONFIDENCE_SIGNAL
"""Confidence signal sent to ConfidenceField on 'contradicted' outcome.
Lower values contradict the memory more aggressively.
Optimal range: [0.05, 0.3]. Insensitive within this range (nDCG stable)."""

ACTED_CYCLE_STRENGTHEN_FACTOR = Defaults.ACTED_CYCLE_STRENGTHEN_FACTOR
"""CyclicDecayField amplification factor on 'acted' outcome.
CLIFF EFFECT: values < 1.0 cause a 23% nDCG drop in temporal scheduling.
Must be >= 1.0. Optimal range: [1.0, 2.0]. Default 1.2 is safe."""

DISMISSED_CYCLE_WEAKEN_FACTOR = Defaults.DISMISSED_CYCLE_WEAKEN_FACTOR
"""CyclicDecayField damping factor on 'dismissed' outcome.
Values < 1.0 weaken the cycle amplitude.
Optimal range: [0.3, 1.0]. Insensitive within this range."""

CONTRADICTED_CYCLE_WEAKEN_FACTOR = Defaults.CONTRADICTED_CYCLE_WEAKEN_FACTOR
"""CyclicDecayField aggressive damping factor on 'contradicted' outcome.
Values < 1.0 weaken the cycle amplitude; lower = more aggressive.
Optimal range: [0.3, 0.8]. Insensitive within this range."""

AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = Defaults.AUTO_DISCHARGE_CONFIDENCE_THRESHOLD
"""Below this confidence level, pressure is auto-resolved on contradicted
outcome. Very low confidence records should not continue building pressure.
Optimal range: [0.05, 0.3]. Insensitive within this range."""

CONFIDENCE_EPSILON = Defaults.CONFIDENCE_EPSILON
"""Internal float-boundary tolerance for the auto-discharge comparison.
Confidence values within this epsilon of the threshold are NOT below it.
Not user config."""


class ObservationProtocol:
    """Lifecycle hooks for passive behavioral inference on memory models.

    All methods are static — the protocol is a stateless coordinator that
    dispatches effects based on outcome type.
    """

    @staticmethod
    def on_read(instance, pipeline=None):
        """Fire when query hydrates an instance. Delegates to AccessTrackerMixin staging.

        If the instance's model uses AccessTrackerMixin, this calls
        ``instance.on_read()``. Otherwise it's a no-op.

        Args:
            instance: A Model instance that was just read from Redis.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if hasattr(instance, "on_read") and callable(instance.on_read):
            instance.on_read(pipeline=pipeline)

    @staticmethod
    def on_surfaced(instances, reason="proactive", partition=None, pipeline=None):
        """Fire when proactive system pushes memories into agent context.

        Creates RecallProposal entries for tracking. Side-effect-free on
        the memories themselves.

        Args:
            instances: List of Model instances being surfaced.
            reason: Why the memories were surfaced. Default "proactive".
            partition: Optional partition key for multi-agent setups.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if not instances:
            return
        RecallProposal.create_batch(
            instances, reason=reason, partition=partition, pipeline=pipeline
        )

    @staticmethod
    def on_context_used(instances, outcome_map, pipeline=None):
        """Fire when application reports how agent responded to surfaced memories.

        For each instance, looks up its outcome in outcome_map and applies
        the corresponding effects atomically.

        Args:
            instances: List of Model instances that were in the agent's context.
            outcome_map: Dict mapping instance Redis keys (str) to outcome
                strings: "acted", "used", "dismissed", "deferred",
                "contradicted". Instances not in the map default to "deferred".
            pipeline: Optional Redis pipeline for batch operations.

        Note:
            This method validates ``outcome_map`` strictly against
            ``VALID_OUTCOMES``. Application-specific outcomes (e.g. a custom
            ``"echoed"`` label) must be coerced to one of the five valid
            values before calling, otherwise a ``ValueError`` is raised.
            See ``docs/features/observation-protocol.md`` (where the
            protocol lives) for guidance on mapping bespoke outcomes into
            the canonical vocabulary.

        Signalling a correction with ``contradicted``:
            ``outcome_map`` is ``{redis_key: outcome}`` and has no slot for a
            second instance, so a caller that knows *what* corrected a memory
            names it by setting the private ``_superseded_by`` attribute on the
            contradicted instance before reporting the outcome::

                stale._superseded_by = corrected
                ObservationProtocol.on_context_used(
                    [stale], {stale.db_key.redis_key: "contradicted"}
                )

            On a model declaring a ValidityField this closes ``stale``'s
            validity interval and writes the supersession edge to ``corrected``
            (issue #580). Without the attribute — and on every model with no
            ValidityField — ``contradicted`` behaves exactly as before. See
            ``_apply_supersession``.

        Unsaved instances:
            An unsaved member of ``instances`` is skipped, for every outcome.
            Its field effects raise internally and are swallowed, it
            contributes no commands, and the remaining members of the batch
            still get their full effects — one unsaved instance never aborts
            the batch (issue #583). The model methods themselves
            (``touch``, ``confirm_access``, ``strengthen_cycle``,
            ``weaken_cycle``, ``resolve_pressure``) are unchanged and still
            raise ``TypeError`` for direct callers; the degradation belongs
            to this protocol layer only.

        Raises:
            ValueError: If any outcome string is not a valid outcome. Being
                unsaved is never a reason this method raises.
        """
        if not instances:
            return

        # Validate all outcomes upfront
        for pk, outcome in outcome_map.items():
            if outcome not in VALID_OUTCOMES:
                raise ValueError(
                    f"Invalid outcome '{outcome}' for key '{pk}'. "
                    f"Valid outcomes: {sorted(VALID_OUTCOMES)}"
                )

        backend_items = [i for i in instances if non_redis_backend(i) is not None]
        if backend_items:
            _on_context_used_on_backend(backend_items, outcome_map, pipeline)
            instances = [i for i in instances if non_redis_backend(i) is None]

        for instance in instances:
            pk = _get_instance_key(instance)
            outcome = outcome_map.get(pk, "deferred")
            _apply_outcome(instance, outcome, pipeline=pipeline)

            # Resolve any pending proposal for this instance
            RecallProposal.resolve(instance, outcome, pipeline=pipeline)


def _on_context_used_on_backend(
    instances: Any, outcome_map: Any, pipeline: Any = None
) -> None:
    """``on_context_used`` for records stored outside Redis (#759 M2a).

    All five outcomes, for every instance, in **one transaction per
    backend**: each model's rows are locked ``FOR UPDATE`` in ``_pk`` order
    first, then the effects run in that order -- ``touch`` for ``acted``, the
    staged reads confirmed (``acted``, ``used``) or discarded (the rest), the
    confidence signal (``acted``, ``contradicted``), and the proposal resolved.
    A deadlock or serialization failure retries the whole unit and then
    raises ``BackendRetryableError``. A backend unit of work passed as
    ``pipeline`` is used as the transaction instead (no retry).

    Unsaved instances degrade exactly as on Redis: their effects are skipped
    and the rest of the batch still lands.
    """
    from ..batch import unit_of

    groups: dict[int, Any] = {}
    for instance in instances:
        backend = non_redis_backend(instance)
        groups.setdefault(id(backend), (backend, {}))[1].setdefault(
            type(instance), []
        ).append(instance)

    for backend, by_model in groups.values():

        def work(tx: Any, by_model: Any = by_model, backend: Any = backend) -> None:
            from ..backends import RecordId

            for model_cls in sorted(by_model, key=lambda m: m._meta.model_name):
                keyed = sorted(
                    ((_get_instance_key(i), i) for i in by_model[model_cls]),
                    key=lambda kv: kv[0].encode("utf-8", "surrogateescape"),
                )
                # A contradicted record's successor is locked with the batch:
                # its supersession takes the successor's record-key lock, and
                # taken afterwards that could fall out of _pk byte order (a
                # concurrent multi-record writer holding it would wait on the
                # batch). Same model only -- a chain never crosses models.
                lock_keys = {k for k, _ in keyed}
                for pk, instance in keyed:
                    if outcome_map.get(pk, "deferred") != "contradicted":
                        continue
                    successor = getattr(instance, "_superseded_by", None)
                    if isinstance(successor, model_cls):
                        try:
                            lock_keys.add(_get_instance_key(successor))
                        except Exception:  # unresolvable: it degrades later
                            pass
                backend.field_call(
                    model_cls._meta.spec,
                    "_observe",
                    "lock",
                    [
                        RecordId.from_key(model_cls._meta.model_name, k)
                        for k in sorted(lock_keys)
                    ],
                    uow=tx,
                )
                for pk, instance in keyed:
                    outcome = outcome_map.get(pk, "deferred")
                    _apply_outcome_on_backend(instance, outcome, tx)
                    RecallProposal.resolve(instance, outcome, pipeline=tx)

        some_model = next(iter(by_model))
        # A transaction() unit of work, or a popoto.batch()'s (#759 M5).
        backend.field_call(
            some_model._meta.spec,
            "_observe",
            "atomically",
            work,
            uow=unit_of(pipeline, backend),
        )


def _apply_outcome_on_backend(instance: Any, outcome: str, uow: Any) -> None:
    """The effects matrix for one instance on a non-Redis backend, inside
    ``uow``, in the order the Redis ``_apply_*`` functions apply them:

    * ``acted``: ``touch`` every decay clock, confirm the staged reads,
      ``strengthen_cycle`` (and ``resolve_pressure`` when the field has a
      pressure rate) on every ``CyclicDecayField``, the confidence signal,
      then ``auto_resolve`` of a ``PredictionLedgerMixin`` prediction.
    * ``used``: confirm the staged reads, ``auto_resolve``.
    * ``dismissed``: discard the staged reads, ``weaken_cycle``,
      ``auto_resolve``.
    * ``deferred``: discard the staged reads.
    * ``contradicted``: discard the staged reads, the aggressive
      ``weaken_cycle``, the confidence signal, ``auto_resolve``, the
      pressure auto-discharge (when the confidence, read inside this
      transaction, is now clearly below the threshold), then the
      supersession of a ``ValidityField``.

    Every write runs in the batch's transaction: the cycles, pressure and
    ledger writes (#759 M5) take the record-key lock the batch already holds,
    and the ledger's confidence feedback runs on the same connection, so no
    effect can wait on the batch's own row lock. The batch's ``(model,
    field)`` lock for a ``ValidityField`` (#759 M3) was taken before its row
    locks."""
    from .confidence_field import ConfidenceField
    from .cyclic_decay_field import CyclicDecayField
    from .decaying_sorted_field import DecayingSortedField
    from .prediction_ledger import PredictionLedgerMixin

    fields = instance._meta.fields
    tracked = hasattr(instance, "confirm_access") and callable(instance.confirm_access)
    cyclic = [(n, f) for n, f in fields.items() if isinstance(f, CyclicDecayField)]

    if outcome == "acted":
        for field_name, field in fields.items():
            if isinstance(field, DecayingSortedField):
                try:
                    instance.touch(field_name, pipeline=uow)
                except (TypeError, ValueError):
                    pass
    if outcome in ("acted", "used"):
        if tracked:
            try:
                instance.confirm_access(pipeline=uow)
            except (TypeError, ValueError):
                pass
    elif hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=uow)

    for field_name, field in cyclic:
        try:
            if outcome == "acted":
                instance.strengthen_cycle(
                    field_name, factor=ACTED_CYCLE_STRENGTHEN_FACTOR, pipeline=uow
                )
                if field.pressure_rate > 0:
                    instance.resolve_pressure(field_name, pipeline=uow)
            elif outcome in ("dismissed", "contradicted"):
                factor = (
                    DISMISSED_CYCLE_WEAKEN_FACTOR
                    if outcome == "dismissed"
                    else CONTRADICTED_CYCLE_WEAKEN_FACTOR
                )
                instance.weaken_cycle(field_name, factor=factor, pipeline=uow)
        except (TypeError, ValueError):
            pass  # Graceful degradation for unsaved instances

    signal = {
        "acted": ACTED_CONFIDENCE_SIGNAL,
        "contradicted": CONTRADICTED_CONFIDENCE_SIGNAL,
    }.get(outcome)
    if signal is not None:
        for field_name, field in fields.items():
            if isinstance(field, ConfidenceField):
                try:
                    ConfidenceField.update_confidence(
                        instance, field_name, signal=signal, pipeline=uow
                    )
                except (TypeError, ValueError):
                    pass

    if outcome != "deferred" and isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, outcome, pipeline=uow)
        except (TypeError, ValueError):
            pass  # Graceful degradation

    if outcome == "contradicted":
        _auto_discharge_on_backend(instance, cyclic, uow)
        _apply_supersession(instance, uow)


def _auto_discharge_on_backend(instance: Any, cyclic: Any, uow: Any) -> None:
    """``_apply_contradicted``'s pressure auto-discharge, reading the
    confidence inside ``uow`` -- the value this batch's updates left, as the
    Redis path reads it right after its own immediate updates."""
    from ..backends import record_id
    from .confidence_field import ConfidenceField

    backend = non_redis_backend(instance)
    if not cyclic or backend is None:
        return
    for field_name, field in instance._meta.fields.items():
        if not isinstance(field, ConfidenceField):
            continue
        try:
            state = backend.field_call(
                instance._meta.spec,
                field_name,
                "state",
                record_id(instance, key=_get_instance_key(instance)),
                uow=uow,
            )
            conf = field.initial_confidence if state is None else state["confidence"]
            if conf < AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - CONFIDENCE_EPSILON:
                for cdf_name, cdf_field in cyclic:
                    if cdf_field.pressure_rate > 0:
                        instance.resolve_pressure(cdf_name, pipeline=uow)
        except (TypeError, ValueError, AttributeError):
            pass


def _get_instance_key(instance):
    """Get the Redis key for a model instance.

    Args:
        instance: A Model instance.

    Returns:
        str: The Redis key string.
    """
    if hasattr(instance, "_redis_key") and instance._redis_key:
        return instance._redis_key
    return instance.db_key.redis_key


def _apply_outcome(instance, outcome, pipeline=None, superseded_by=None):
    """Apply effects for a single outcome on a single instance.

    Creates an internal pipeline for atomicity when no pipeline is provided.

    Args:
        instance: A Model instance.
        outcome: One of "acted", "used", "dismissed", "deferred",
            "contradicted".
        pipeline: Optional Redis pipeline for batch operations.
        superseded_by: Optional Model instance carrying the corrected claim.
            Only meaningful for the "contradicted" outcome, where it is
            recorded as a supersession edge on models that declare a
            ValidityField. Ignored otherwise.
    """
    # Use internal pipeline for atomicity if none provided
    use_internal_pipeline = pipeline is None
    if use_internal_pipeline:
        pipeline = get_REDIS_DB().pipeline()

    if outcome == "acted":
        _apply_acted(instance, pipeline)
    elif outcome == "dismissed":
        _apply_dismissed(instance, pipeline)
    elif outcome == "deferred":
        _apply_deferred(instance, pipeline)
    elif outcome == "contradicted":
        _apply_contradicted(instance, pipeline, superseded_by=superseded_by)
    elif outcome == "used":
        _apply_used(instance, pipeline)

    if use_internal_pipeline:
        pipeline.execute()


def _apply_acted(instance, pipeline):
    """Acted: touch decay clock, confirm reads, strengthen cycles, discharge pressure.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    from .decaying_sorted_field import DecayingSortedField
    from .cyclic_decay_field import CyclicDecayField

    # Touch all DecayingSortedFields (refreshes decay clock)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, DecayingSortedField):
            try:
                instance.touch(field_name, pipeline=pipeline)
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Confirm staged reads (AccessTrackerMixin)
    if hasattr(instance, "confirm_access") and callable(instance.confirm_access):
        try:
            instance.confirm_access(pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation for unsaved instances

    # Strengthen cycles and resolve pressure (CyclicDecayField)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            try:
                instance.strengthen_cycle(
                    field_name, factor=ACTED_CYCLE_STRENGTHEN_FACTOR, pipeline=pipeline
                )
                if field.pressure_rate > 0:
                    instance.resolve_pressure(field_name, pipeline=pipeline)
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Corroborate confidence (ConfidenceField)
    from .confidence_field import ConfidenceField

    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                ConfidenceField.update_confidence(
                    instance, field_name, signal=ACTED_CONFIDENCE_SIGNAL
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "acted", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


def _apply_dismissed(instance, pipeline):
    """Dismissed: discard staged reads, weaken cycles.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    from .cyclic_decay_field import CyclicDecayField

    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)

    # Weaken cycles
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            try:
                instance.weaken_cycle(
                    field_name, factor=DISMISSED_CYCLE_WEAKEN_FACTOR, pipeline=pipeline
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "dismissed", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


def _apply_deferred(instance, pipeline):
    """Deferred: discard staged reads, no other effects. Pressure keeps building.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)


def _apply_contradicted(instance, pipeline, superseded_by=None):
    """Contradicted: discard staged reads, aggressively weaken cycles.

    Also records *what* contradicted the memory, when that is knowable: on a
    model declaring a ValidityField, and given the instance carrying the
    corrected claim, the record's validity interval is closed and a
    supersession edge is written (issue #580). Contradiction stops being a
    scalar nudge and becomes provenance.

    The correcting instance is supplied either as ``superseded_by`` or, for
    callers that only reach this through ``on_context_used``, as the private
    ``instance._superseded_by`` attribute. Both are optional: with neither
    present — and on every model without a ValidityField, which is every
    shipped model today — this is a strict no-op and the pre-#580 effects are
    unchanged.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
        superseded_by: Optional Model instance that supersedes ``instance``.
    """
    from .cyclic_decay_field import CyclicDecayField

    # Discard staged reads
    if hasattr(instance, "discard_staged_access") and callable(
        instance.discard_staged_access
    ):
        instance.discard_staged_access(pipeline=pipeline)

    # Aggressively weaken cycles (factor=0.5 vs 0.8 for dismissed)
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, CyclicDecayField):
            try:
                instance.weaken_cycle(
                    field_name,
                    factor=CONTRADICTED_CYCLE_WEAKEN_FACTOR,
                    pipeline=pipeline,
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Contradict confidence (ConfidenceField)
    from .confidence_field import ConfidenceField

    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                ConfidenceField.update_confidence(
                    instance, field_name, signal=CONTRADICTED_CONFIDENCE_SIGNAL
                )
            except (TypeError, ValueError):
                pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(
                instance, "contradicted", pipeline=pipeline
            )
        except (TypeError, ValueError):
            pass  # Graceful degradation

    # Auto-discharge: when confidence is clearly below the threshold, resolve
    # pressure on CyclicDecayFields. Values within float epsilon of the
    # threshold are NOT below it (fixes the 0.09999999999999998 artifact
    # that auto-discharged on the first contradiction).
    for field_name, field in instance._meta.fields.items():
        if isinstance(field, ConfidenceField):
            try:
                conf = ConfidenceField.get_confidence(instance, field_name)
                if conf < AUTO_DISCHARGE_CONFIDENCE_THRESHOLD - CONFIDENCE_EPSILON:
                    for cdf_name, cdf_field in instance._meta.fields.items():
                        if (
                            isinstance(cdf_field, CyclicDecayField)
                            and cdf_field.pressure_rate > 0
                        ):
                            instance.resolve_pressure(cdf_name, pipeline=pipeline)
            except (TypeError, ValueError, AttributeError):
                pass

    # Record the supersession edge (ValidityField, issue #580). Strictly
    # additive: no ValidityField on the model, or no correcting instance, means
    # nothing runs at all.
    _apply_supersession(instance, pipeline, superseded_by)


def _apply_supersession(
    instance: Any, pipeline: Any, superseded_by: Any = None
) -> None:
    """Close a contradicted record's interval and chain it to its correction.

    No-op unless the model declares a ValidityField *and* the correcting
    instance is known. Delegates to ``SupersessionProtocol.invalidate``, which
    performs interval closure and both chain links in a single EVAL, so a
    half-linked chain is unobservable.

    How the correcting record reaches here
    --------------------------------------
    ``ObservationProtocol.on_context_used`` takes an ``outcome_map`` shaped
    ``{redis_key: outcome}``. That mapping has no slot for a second instance,
    so the public call cannot carry the correction — the only route through
    the protocol is to tag the contradicted instance before reporting it::

        stale._superseded_by = corrected          # the signal
        ObservationProtocol.on_context_used(
            [stale], {stale.db_key.redis_key: "contradicted"}
        )

    ``instance._superseded_by`` is therefore the documented public mechanism,
    not an internal accident; the ``superseded_by=`` parameter is the plumbing
    it resolves to, and is also usable by direct callers of
    ``_apply_contradicted`` (widening ``outcome_map``'s value shape to a
    ``(outcome, instance)`` tuple would be a breaking change to a shipped
    signature, so it was rejected).

    Args:
        instance: The contradicted Model instance.
        pipeline: Redis pipeline for batched operations.
        superseded_by: The correcting Model instance, or None. Falls back to
            ``instance._superseded_by`` when not passed explicitly — which is
            the route every ``on_context_used`` caller takes.
    """
    from .supersession import SupersessionProtocol
    from .validity_field import ValidityField

    successor = superseded_by
    if successor is None:
        successor = getattr(instance, "_superseded_by", None)
    if successor is None:
        return

    fields = getattr(getattr(instance, "_meta", None), "fields", None) or {}
    if not any(isinstance(field, ValidityField) for field in fields.values()):
        return

    # The EXISTS probe #588 removed from ``_member_key`` survives here, and only
    # here. This path is a *signal*: ``on_context_used`` reports outcomes for a
    # batch of memories and must not raise because one of them was never saved,
    # and in pipeline mode a script error at EXEC would abort effects already
    # queued for the rest of the batch.
    #
    # The probe is safe on this path and nowhere else, for a reason worth
    # writing down: the observation path never has a same-pipeline successor.
    # Both records are, by construction, already-saved memories the agent was
    # shown. The TOCTOU window that makes a client-side probe wrong in the
    # general case does not exist here.
    for role, obj in (("instance", instance), ("successor", successor)):
        key = getattr(getattr(obj, "db_key", None), "redis_key", None)
        backend = non_redis_backend(obj) if key else None
        if backend is not None:
            # #759 M3: the same probe through the record's own backend.
            from ..backends import RecordId

            (found,) = backend.exists(
                obj._meta.spec, [RecordId.from_key(obj._meta.model_name, key)]
            )
            if found:
                continue
            logger.debug("supersession: %s %r not persisted, degrading", role, key)
            return
        if not key or not get_REDIS_DB().exists(key):
            logger.debug("supersession: %s %r not persisted, degrading", role, key)
            return

    try:
        SupersessionProtocol.invalidate(
            instance, superseded_by=successor, pipeline=pipeline
        )
    except (TypeError, ValueError):
        # Second layer, kept deliberately: the new typed validity errors
        # subclass ValueError (#588 D4), so a race that hard-deletes a record
        # between the probe above and the write still degrades rather than
        # escaping a telemetry callback.
        pass


def _apply_used(instance, pipeline):
    """Used: confirm staged reads, auto-resolve predictions. No confidence,
    no cycle, no decay effects.

    Semantic: the agent consumed the memory (read + reasoned) but did not
    act on it in the response. Observably stronger than ``deferred``
    (which discards staged reads) because it commits the AccessTracker
    staged-read to a confirmed trace. Observably weaker than ``acted``
    (which corroborates confidence and strengthens cycles).

    Effects:
        * ``confirm_access(pipeline=pipeline)`` when AccessTrackerMixin is
          present — commits the staged read as a confirmed read.
        * ``PredictionLedgerMixin.auto_resolve(instance, "used")`` — maps
          to ``Defaults.PL_AUTO_RESOLVE_USED`` (0.3, moderate error).
        * Does NOT touch ConfidenceField, CyclicDecayField, or
          DecayingSortedField.

    Args:
        instance: A Model instance.
        pipeline: Redis pipeline for batched operations.
    """
    # Confirm staged reads (AccessTrackerMixin) — this is the key behavioral
    # distinction from `_apply_deferred`, which discards staged reads.
    if hasattr(instance, "confirm_access") and callable(instance.confirm_access):
        try:
            instance.confirm_access(pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation for unsaved instances

    # Auto-resolve predictions (PredictionLedgerMixin)
    from .prediction_ledger import PredictionLedgerMixin

    if isinstance(instance, PredictionLedgerMixin):
        try:
            PredictionLedgerMixin.auto_resolve(instance, "used", pipeline=pipeline)
        except (TypeError, ValueError):
            pass  # Graceful degradation


def _uow_of(pipeline: Any, backend: Any) -> Any:
    """A backend unit of work passed as ``pipeline=``, or the one a
    ``popoto.batch()`` opens on ``backend`` (#759 M5). A plain Redis pipeline
    cannot carry a Postgres write: the write runs at once."""
    from ..batch import unit_of

    return unit_of(pipeline, backend)


class RecallProposal:
    """Internal tracking for proactively surfaced memories.

    Key pattern: $RP:{ClassName}:pending:{partition} -> ZSET scored by surfaced_at
    Statuses: pending -> acted | used | dismissed | deferred | contradicted | expired
    TTL: default 3600s (1 hour). Unresolved proposals treated as deferred.

    This is internal ORM infrastructure, not a user-facing Model.
    """

    DEFAULT_TTL = 3600

    @classmethod
    def _pending_key(cls, model_class, partition=None):
        """Build the Redis key for pending proposals.

        Args:
            model_class: The Model class (or instance's class).
            partition: Optional partition key.

        Returns:
            str: Redis key like '$RP:ClassName:pending:partition'
        """
        class_name = (
            model_class.__name__
            if isinstance(model_class, type)
            else type(model_class).__name__
        )
        part = partition or "default"
        return f"$RP:{class_name}:pending:{part}"

    @classmethod
    def create_batch(cls, instances, reason="proactive", partition=None, pipeline=None):
        """Create pending proposals for a batch of instances.

        Args:
            instances: List of Model instances being surfaced.
            reason: Why the memories were surfaced.
            partition: Optional partition key.
            pipeline: Optional Redis pipeline for batch operations.
        """
        if not instances:
            return

        now = time.time()
        model_class = type(instances[0])
        backend = non_redis_backend(model_class)
        if backend is not None:
            backend.field_call(
                model_class._meta.spec,
                "_recall",
                "add",
                partition or "default",
                [_get_instance_key(i) for i in instances],
                now,
                uow=_uow_of(pipeline, backend),
            )
            return
        key = cls._pending_key(model_class, partition)

        db = pipeline if pipeline is not None else get_REDIS_DB()

        # ZADD each instance with score=now
        members = {}
        for instance in instances:
            member_key = _get_instance_key(instance)
            # Store member key with reason as the value scored by time
            members[member_key] = now

        if members:
            db.zadd(key, members)

    @classmethod
    def resolve(cls, instance, outcome, partition=None, pipeline=None):
        """Remove a resolved proposal from the pending set.

        Idempotent — returns 0 if already removed (e.g., by expiration).

        Args:
            instance: The Model instance whose proposal to resolve.
            outcome: The outcome string (for logging).
            partition: Optional partition key.
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            int: Number of members removed (0 or 1).
        """
        model_class = type(instance)
        backend = non_redis_backend(model_class)
        if backend is not None:
            removed = backend.field_call(
                model_class._meta.spec,
                "_recall",
                "remove",
                partition or "default",
                _get_instance_key(instance),
                uow=_uow_of(pipeline, backend),
            )
            return pipeline if pipeline is not None else removed
        key = cls._pending_key(model_class, partition)
        member_key = _get_instance_key(instance)

        if pipeline is not None:
            pipeline.zrem(key, member_key)
            return pipeline
        else:
            return get_REDIS_DB().zrem(key, member_key)

    @classmethod
    def expire_stale(cls, model_class, partition=None, ttl=None, pipeline=None):
        """Remove proposals older than TTL. Returns expired member keys.

        Args:
            model_class: The Model class to check.
            partition: Optional partition key.
            ttl: TTL in seconds. Default DEFAULT_TTL (3600).
            pipeline: Optional Redis pipeline for batch operations.

        Returns:
            list: List of expired member key strings.
        """
        if ttl is None:
            ttl = cls.DEFAULT_TTL

        backend = non_redis_backend(model_class)
        if backend is not None:
            return backend.field_call(
                model_class._meta.spec,
                "_recall",
                "expire",
                partition or "default",
                time.time() - ttl,
                uow=_uow_of(pipeline, backend),
            )
        key = cls._pending_key(model_class, partition)
        cutoff = time.time() - ttl

        # Get members with scores below cutoff (older than TTL)
        expired = get_REDIS_DB().zrangebyscore(key, "-inf", cutoff)

        if expired:
            if pipeline is not None:
                pipeline.zremrangebyscore(key, "-inf", cutoff)
            else:
                get_REDIS_DB().zremrangebyscore(key, "-inf", cutoff)

        # Decode bytes to strings
        return [m.decode() if isinstance(m, bytes) else m for m in expired]

    @classmethod
    def get_pending(cls, model_class, partition=None):
        """Return all pending proposals as (member_key, surfaced_at) pairs.

        Args:
            model_class: The Model class to check.
            partition: Optional partition key.

        Returns:
            list: List of (member_key_str, surfaced_at_float) tuples.
        """
        backend = non_redis_backend(model_class)
        if backend is not None:
            return backend.field_call(
                model_class._meta.spec, "_recall", "pending", partition or "default"
            )
        key = cls._pending_key(model_class, partition)
        results = get_REDIS_DB().zrange(key, 0, -1, withscores=True)
        return [
            (m.decode() if isinstance(m, bytes) else m, score) for m, score in results
        ]
