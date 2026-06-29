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
    # ``tests/benchmarks/results/sweep_20260420_051055.json`` (26 Tier 1-3
    # constants, 8 family + 10 generic scenarios per constant, 7 family
    # scenarios including PredictionLedger / ContextAssembler /
    # PolicyCache added in issue #362). Variance is
    # max(nDCG@5) - min(nDCG@5) across the swept values. Constants with
    # variance <= 0.05 are marked "empirically inert" — the family
    # scenarios don't exercise their code paths enough to move the
    # sensitivity signal. Inert constants are kept at their prior values
    # rather than removed; a follow-up can scope deeper scenarios for
    # them.

    # -- DecayingSortedField --------------------------------------------------
    DECAY_RATE = 0.1  # best from sweep 2026-04-20, variance=0.067, prior=0.1 (stable)

    # -- ConfidenceField ------------------------------------------------------
    INITIAL_CONFIDENCE = 0.5  # empirically inert (sweep 2026-04-20, variance=0.0011)
    CONFIDENCE_EVIDENCE_CAP = 20  # deliberate user-facing config exception per issue #407 decision — a memory-window length / epistemics knob (how much history a belief retains), not an experimental tuning constant
    CONFIDENCE_EPSILON = 1e-9  # internal float-boundary tolerance for threshold comparisons, not user config

    # -- ObservationProtocol (fields/observation.py) --------------------------
    ACTED_CONFIDENCE_SIGNAL = 0.9  # sweep 2026-04-20 variance=0.030 (borderline); best in-range was 0.1 but 0.9 better reflects the "strong positive" semantics and per-scenario effect is small
    CONTRADICTED_CONFIDENCE_SIGNAL = 0.1  # sweep 2026-04-20 variance=0.030 (borderline); best in-range was 0.9 (inverse of default — within noise, kept at 0.1 for compat)
    ACTED_CYCLE_STRENGTHEN_FACTOR = (
        1.2  # empirically inert (sweep 2026-04-20, variance=0.0)
    )
    DISMISSED_CYCLE_WEAKEN_FACTOR = (
        0.8  # empirically inert (sweep 2026-04-20, variance=0.0)
    )
    CONTRADICTED_CYCLE_WEAKEN_FACTOR = (
        0.5  # empirically inert (sweep 2026-04-20, variance=0.0)
    )
    AUTO_DISCHARGE_CONFIDENCE_THRESHOLD = (
        0.1  # empirically inert (sweep 2026-04-20, variance=0.0)
    )

    # -- WriteFilterMixin (fields/write_filter.py) ----------------------------
    WF_MIN_THRESHOLD = (
        0.1  # best from sweep 2026-04-20, variance=0.068, prior=0.1 (stable)
    )
    WF_PRIORITY_THRESHOLD = (
        0.7  # not swept separately (Tier 1 covers WF_MIN); kept at prior
    )

    # -- CoOccurrenceField (fields/co_occurrence_field.py) --------------------
    CO_OCCURRENCE_DECAY_FACTOR = 0.95  # empirically inert (sweep 2026-04-20, variance=0.0) — family scenario never calls weaken_all()
    CO_OCCURRENCE_INITIAL_WEIGHT = 0.1  # sweep 2026-04-20 variance=0.144; best 0.01 but curve has noise cliff, 0.1 is safer default for new users
    CO_OCCURRENCE_DECAY_PER_HOP = 0.5  # best from sweep 2026-04-20, variance=0.112, prior=0.5 (stable, smooth peak at 0.5)
    # Upper bound on stored edge weights. Contraction invariant:
    #   cap * CO_OCCURRENCE_DECAY_PER_HOP < 1  ->  per-hop transfer < 1
    # so propagation decays rather than amplifies. The value 1.0 has
    # intentional headroom below the theoretical maximum
    # 1 / CO_OCCURRENCE_DECAY_PER_HOP = 2.0; a runtime guard in
    # CoOccurrenceField.propagate() backstops the invariant if either
    # constant is later changed.
    CO_OCCURRENCE_WEIGHT_CAP = 1.0

    # -- PredictionLedgerMixin (fields/prediction_ledger.py) ------------------
    # Issue #362 added PredictionLedgerFamilyScenario. PL_AUTO_RESOLVE_
    # CONTRADICTED shows a gate-crossing signal (variance 0.025 between
    # the [0.5, 0.7] plateau and the [0.8, 0.9, 0.95] plateau) but the
    # signal dilutes below the 0.05 sweep bar when averaged across the
    # family + generic scenario mix. PL_CONFIDENCE_ERROR_THRESHOLD shows
    # a similar 0.025 variance. The remaining three PL_* constants (ACTED /
    # DISMISSED / LOW_SIGNAL) are inert-by-design per plan Technical
    # Approach §2 — their sweep grids fall entirely below the default
    # confidence-error gate, so no auto-resolve transitions fire.
    PL_CONFIDENCE_ERROR_THRESHOLD = 0.7  # sweep 2026-04-20 variance=0.025 (borderline); PL family scenario shows gate-crossing signal but family-average dilutes below 0.05 bar. Kept at 0.7 (semantic "moderate error floor").
    PL_CONFIDENCE_LOW_SIGNAL = 0.2  # empirically inert (sweep 2026-04-20, variance=0.0) — fires only when error threshold is crossed; PL family scenario keeps threshold constant
    PL_AUTO_RESOLVE_ACTED = 0.1  # empirically inert (sweep 2026-04-20, variance=0.0) — sweep grid [0.05..0.5] all below default 0.7 gate; inert-by-design per plan Technical Approach §2
    PL_AUTO_RESOLVE_DISMISSED = 0.5  # empirically inert (sweep 2026-04-20, variance=0.0) — grid mostly below gate
    PL_AUTO_RESOLVE_CONTRADICTED = 0.9  # sweep 2026-04-20 variance=0.025 (borderline); gate-crossing plateau at 0.5/0.7 vs 0.8/0.9/0.95. Kept at 0.9 (semantic "strong negative").
    # Metacognitive layer (#352): "used" outcome means the agent consumed
    # the memory (read + reasoned) but didn't act on it. Error 0.3 is a
    # moderate placeholder — neither confirmed nor contradicted. Callers
    # wanting precise accounting should use resolve_prediction() explicitly
    # instead of relying on auto-resolve.
    PL_AUTO_RESOLVE_USED = 0.3

    # -- AdaptiveAssembler (recipes/adaptive_assembler.py, #352) --------------
    # Rolling-window size for the keep/revert loop. Smaller windows adapt
    # faster but noisier; larger windows converge more slowly but more
    # reliably. Autoresearch pattern uses ~20 samples per proposal.
    ADAPTIVE_QUALITY_WINDOW_SIZE = 20

    # -- PolicyCache (recipes/policy_cache.py) --------------------------------
    # Issue #362 added PolicyCacheFamilyScenario. WILSON_CI_THRESHOLD is
    # now sensitive (variance 0.130) with monotonic curve peaking at 0.7;
    # MIN_EVENTS_FOR_CRYSTALLIZATION is flat in the scenario's [1, 10]
    # sweep range because the group specs all satisfy min_events<=10 and
    # CI thresholds dominate the crystallized-set-membership signal.
    MIN_EVENTS_FOR_CRYSTALLIZATION = 3  # empirically inert (sweep 2026-04-20, variance=0.0) — PolicyCache family scenario is CI-dominated; min_events signal needs a broader group-size spread to emerge
    WILSON_CI_THRESHOLD = 0.6  # sweep 2026-04-20 variance=0.130 (sensitive), best 0.7 (nDCG 0.999 vs 0.972 at 0.6). Kept at 0.6 for semantic stability (60% lower-bound is a round threshold) and to avoid breaking callers that tune against the 0.6 baseline; the 0.027 ndcg gain is modest and downstream tests encode the 0.6 boundary (test_policy_cache.py::test_crystallization_from_events uses 8-success case with ci=0.676 that straddles 0.6 but falls below 0.7).
    TD_ALPHA = 0.1  # empirically inert (sweep 2026-04-20, variance=0.0)
    TD_GAMMA = 0.95  # empirically inert (sweep 2026-04-20, variance=0.0)
    CHI_SQUARED_P_THRESHOLD = 0.05  # empirically inert (sweep 2026-04-20, variance=0.0)
    INITIAL_CYCLE_AMPLITUDE = 0.5  # empirically inert (sweep 2026-04-20, variance=0.0)

    # -- TrajectoryMemory (recipes/trajectory_memory.py) ----------------------
    # Cluster threshold for crystallizing trajectory patterns. Episodes
    # sharing a fingerprint must reach this count before being promoted to a
    # canonical pattern. Higher values delay crystallization in favor of
    # stronger evidence; lower values produce patterns sooner from sparser
    # data. Not yet swept — initial value mirrors PolicyCache's
    # MIN_EVENTS_FOR_CRYSTALLIZATION (3) which is the closest analogue.
    TRAJECTORY_CLUSTER_THRESHOLD = 3

    # -- ContextAssembler (recipes/context_assembler.py) ----------------------
    # Issue #362 added ContextAssemblerFamilyScenario.
    # COMPETITIVE_SUPPRESSION_SIGNAL is now sensitive (variance 0.053) —
    # the [0.1, 0.2, 0.3, 0.5] plateau at nDCG 0.874 dips to 0.821 at 0.7
    # (signal crosses the contradiction/corroboration boundary).
    # DEFAULT_SURFACING_THRESHOLD remains inert because the scenario's
    # pull path dominates and the push path is never activated above
    # threshold.
    COMPETITIVE_SUPPRESSION_SIGNAL = 0.3  # best-plateau from sweep 2026-04-20, variance=0.053, prior=0.3 (on plateau [0.1..0.5]; kept at 0.3 for "mild contradiction" semantics)
    DEFAULT_SURFACING_THRESHOLD = 0.5  # empirically inert (sweep 2026-04-20, variance=0.0) — scenario's pull path never crosses the surfacing threshold

    # -- MemoryLifecycle (recipes/memory_lifecycle.py) -----------------------
    # Tier-transition thresholds for the episodic→semantic promotion policy.
    # These are tuning constants fed into the benchmarks/run_sweeps.py
    # TIER5_SWEEPS grid and tuned against the LoCoMo + LongMemEval-S harness
    # established in issue #394. Not yet swept; initial values set by design.
    LIFECYCLE_PROMOTION_ACCESS_COUNT = 3  # accesses before episodic→semantic eligible
    LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD = 0.6  # confidence floor for promotion
    LIFECYCLE_PROMOTION_MIN_AGE_SECONDS = (
        300.0  # 5 min — prevents burst-access promotion
    )
    LIFECYCLE_FORGET_IMPORTANCE_FLOOR = (
        0.1  # importance below this → eligible for forget
    )
    LIFECYCLE_FORGET_IDLE_SECONDS = 86400.0  # 24 h idle → eligible for forget


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
