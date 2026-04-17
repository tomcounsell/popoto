"""Constants for Popoto agent-memory primitives.

Provides:
- ``Defaults``: Central registry of tunable behavioral constants (Category 1).
  Override any constant before model definition or at runtime.
- ``TemporalPeriod``: Named constants for standard cycle periods in seconds.
- ``InteractionWeight``: Weight constants for source/role-based importance scoring.

Example:
    from popoto.fields.constants import Defaults, TemporalPeriod

    # Override a default before creating models
    Defaults.DECAY_RATE = 0.3

    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
    )
"""


class Defaults:
    """Central registry of tunable behavioral constants (Category 1).

    Override any constant before model definition or at runtime::

        from popoto.fields.constants import Defaults
        Defaults.DECAY_RATE = 0.3

    Constants are grouped by the primitive that owns them. Primitives
    read from ``Defaults`` at import time (module-level aliases) or at
    runtime (field kwargs / method params with ``None`` sentinel).

    Explicit kwargs always win: ``DecayingSortedField(decay_rate=0.7)``
    ignores ``Defaults.DECAY_RATE``.
    """

    # Sweep evidence for each numeric default is tagged inline. The
    # reference sweep is
    # ``tests/benchmarks/results/sweep_20260417_141047.json`` (26 Tier 1-3
    # constants, 8 family + 10 generic scenarios per constant). Variance is
    # max(nDCG@5) - min(nDCG@5) across the swept values. Constants with
    # variance <= 0.05 after the 2026-04-17 ground-truth decoupling are
    # marked "empirically inert" — the family scenarios don't exercise
    # their code paths enough to move the sensitivity signal. Inert
    # constants are kept at their prior values rather than removed; a
    # follow-up can scope deeper scenarios for them.

    # -- DecayingSortedField --------------------------------------------------
    DECAY_RATE = 0.1  # best from sweep 2026-04-17, variance=0.067, prior=0.5

    # -- ConfidenceField ------------------------------------------------------
    INITIAL_CONFIDENCE = 0.5  # empirically inert (sweep 2026-04-17, variance=0.0011)

    # -- ObservationProtocol (fields/observation.py) --------------------------
    ACTED_CONFIDENCE_SIGNAL = 0.9  # sweep 2026-04-17 variance=0.020 (borderline); best in-range was 0.1 but 0.9 better reflects the "strong positive" semantics and per-scenario effect is small
    CONTRADICTED_CONFIDENCE_SIGNAL = 0.1  # sweep 2026-04-17 variance=0.021 (borderline); best in-range was 0.9 (inverse of default — within noise, kept at 0.1 for compat)
    ACTED_CYCLE_STRENGTHEN_FACTOR = (
        1.2  # empirically inert (sweep 2026-04-17, variance=0.0)
    )
    DISMISSED_CYCLE_WEAKEN_FACTOR = (
        0.8  # empirically inert (sweep 2026-04-17, variance=0.0)
    )
    CONTRADICTED_CYCLE_WEAKEN_FACTOR = (
        0.5  # empirically inert (sweep 2026-04-17, variance=0.0)
    )
    AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = (
        0.1  # empirically inert (sweep 2026-04-17, variance=0.0)
    )

    # -- WriteFilterMixin (fields/write_filter.py) ----------------------------
    WF_MIN_THRESHOLD = 0.1  # best from sweep 2026-04-17, variance=0.068, prior=0.2
    WF_PRIORITY_THRESHOLD = (
        0.7  # not swept separately (Tier 1 covers WF_MIN); kept at prior
    )

    # -- CoOccurrenceField (fields/co_occurrence_field.py) --------------------
    CO_OCCURRENCE_DECAY_FACTOR = 0.95  # empirically inert (sweep 2026-04-17, variance=0.0) — family scenario never calls weaken_all()
    CO_OCCURRENCE_INITIAL_WEIGHT = 0.1  # sweep 2026-04-17 variance=0.144; best 0.01 but curve has noise cliff, 0.1 is safer default for new users
    CO_OCCURRENCE_DECAY_PER_HOP = 0.5  # best from sweep 2026-04-17, variance=0.112, prior=0.5 (already optimal, smooth peak at 0.5)

    # -- PredictionLedgerMixin (fields/prediction_ledger.py) ------------------
    PL_CONFIDENCE_ERROR_THRESHOLD = 0.7  # empirically inert (sweep 2026-04-17, variance=0.0) — no family scenario exercises prediction ledger
    PL_CONFIDENCE_LOW_SIGNAL = 0.2  # empirically inert (sweep 2026-04-17, variance=0.0)
    PL_AUTO_RESOLVE_ACTED = 0.1  # empirically inert (sweep 2026-04-17, variance=0.0)
    PL_AUTO_RESOLVE_DISMISSED = (
        0.5  # empirically inert (sweep 2026-04-17, variance=0.0)
    )
    PL_AUTO_RESOLVE_CONTRADICTED = (
        0.9  # empirically inert (sweep 2026-04-17, variance=0.0)
    )

    # -- PolicyCache (recipes/policy_cache.py) --------------------------------
    MIN_EVENTS_FOR_CRYSTALLIZATION = 3  # empirically inert (sweep 2026-04-17, variance=0.0) — no family scenario exercises policy cache
    WILSON_CI_THRESHOLD = 0.6  # empirically inert (sweep 2026-04-17, variance=0.0)
    TD_ALPHA = 0.1  # empirically inert (sweep 2026-04-17, variance=0.0)
    TD_GAMMA = 0.95  # empirically inert (sweep 2026-04-17, variance=0.0)
    CHI_SQUARED_P_THRESHOLD = 0.05  # empirically inert (sweep 2026-04-17, variance=0.0)
    INITIAL_CYCLE_AMPLITUDE = 0.5  # empirically inert (sweep 2026-04-17, variance=0.0)

    # -- ContextAssembler (recipes/context_assembler.py) ----------------------
    COMPETITIVE_SUPPRESSION_SIGNAL = (
        0.3  # empirically inert (sweep 2026-04-17, variance=0.0)
    )
    DEFAULT_SURFACING_THRESHOLD = (
        0.5  # empirically inert (sweep 2026-04-17, variance=0.0)
    )


class TemporalPeriod:
    """Named constants for common temporal cycle periods in seconds.

    These values represent the approximate duration of each period:
        DAILY     = 86,400 seconds (24 hours)
        WEEKLY    = 604,800 seconds (7 days)
        MONTHLY   = 2,592,000 seconds (30 days)
        QUARTERLY = 7,776,000 seconds (90 days)
        YEARLY    = 31,536,000 seconds (365 days)
    """

    DAILY = 86_400
    WEEKLY = 604_800
    MONTHLY = 2_592_000
    QUARTERLY = 7_776_000
    YEARLY = 31_536_000


class InteractionWeight:
    """Weight constants for source/role-based importance scoring.

    Two axes combined by addition:
    - Source axis: what kind of entity (HUMAN, AGENT, SYSTEM)
    - Role axis: authority level (EXECUTIVE, MANAGER, PEER, SUBORDINATE)

    With decay_rate=0.5, effective lifetime ~ score^2 days.
    """

    HUMAN = 6.0
    AGENT = 1.0
    SYSTEM = 0.2

    EXECUTIVE = 44.0
    MANAGER = 16.0
    PEER = 6.0
    SUBORDINATE = 1.0

    @staticmethod
    def combine(source: float, role: float) -> float:
        """Return the combined weight for a source/role pair.

        The two axes are additive: a HUMAN (6.0) PEER (6.0) interaction
        yields weight 12.0, while an AGENT (1.0) SUBORDINATE (1.0)
        interaction yields weight 2.0.
        """
        return source + role
