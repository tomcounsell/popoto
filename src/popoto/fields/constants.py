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

import os

#: Environment values that read as "on" for a boolean deploy-level switch.
_TRUTHY = ("1", "true", "yes", "on")


def _read_legacy_datetime_key_switch() -> bool:
    """Read ``POPOTO_DATETIME_KEY_LEGACY`` from the environment (#537/#538)."""
    return os.environ.get("POPOTO_DATETIME_KEY_LEGACY", "").strip().lower() in _TRUTHY


def _read_journal_coupling_switch() -> bool:
    """Read ``POPOTO_JOURNAL_COUPLING_DISABLE`` from the environment (#560).

    Returns True when the provenance journal's validity coupling is ENABLED.
    Phrased as a disable so the default-on doctrine holds when it is unset,
    exactly like :func:`_read_never_record_switch`.
    """
    value = os.environ.get("POPOTO_JOURNAL_COUPLING_DISABLE", "").strip().lower()
    return value not in _TRUTHY


def _read_never_record_switch() -> bool:
    """Read ``POPOTO_NEVER_RECORD_DISABLE`` from the environment (#561).

    Returns True when the never-record firewall is ENABLED. The env var is
    phrased as a disable so the default-on doctrine holds when it is unset.
    """
    value = os.environ.get("POPOTO_NEVER_RECORD_DISABLE", "").strip().lower()
    return value not in _TRUTHY


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
    # Confidence-modulated decay (issue #491). Effective per-record rate is
    # decay_rate * 2^(s * 2 * (c0 - confidence)), so s is literally "doublings
    # of the decay rate at zero confidence". Not yet swept: 0.5 is the
    # literature-grounded midpoint of the 0.3-0.7 band recommended by spike-4
    # (Pavlik & Anderson 2005 strength-dependent decay; Duolingo half-life
    # regression), to be tuned against real dismissal data (#493) rather than
    # synthetic corpora. s = 0 makes modulation a bit-exact no-op.
    DECAY_CONFIDENCE_MODULATION_STRENGTH = 0.5
    # Deploy-level kill switch (issue #491 decision 4, 2026-07-27). Modulation
    # is default-ON via auto-detection, so a PyPI adopter whose ranking
    # regresses after `pip install -U` needs a disable that does not require
    # editing model definitions. False makes every path byte-identical to
    # pre-#491 behavior (equivalent to s = 0). Boolean, not swept.
    DECAY_CONFIDENCE_MODULATION_ENABLED = True

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

    # -- TagField / optional scoping (fields/tag_field.py, issue #492) ---------
    # Deploy-level kill switch for subconscious, retrieval-time tag scoping.
    # ContextAssembler auto-detects a TagField on the model and applies the
    # caller's tag constraints across all retrieval modes; this default-ON
    # behavior means a PyPI adopter cannot always edit model code to disable it.
    # Setting this False makes the assembler ignore tag constraints entirely, so
    # retrieval is byte-identical to a model without a TagField. Index
    # maintenance (per-tag Redis Sets) always runs for correctness, and explicit
    # `Model.query.filter(tags__all=...)` still works — this switch governs only
    # the subconscious assembler path, not deliberate queries. Boolean, not swept.
    TAG_SCOPING_ENABLED = True

    # -- ValidityField (fields/validity_field.py, issue #580) -----------------
    # Deploy-level kill switch for subconscious, retrieval-time validity gating.
    # A model that declares a ValidityField gets superseded records excluded from
    # default retrieval automatically (decay-Lua gate, composite mask, assembler
    # post-filter); this default-ON behavior means a PyPI adopter whose ranking
    # regresses after `pip install -U` cannot always edit model code to disable
    # it. Setting this False makes every retrieval path byte-identical to
    # pre-#580 behavior — as if the model had no ValidityField at all. Interval
    # and chain maintenance (the three ZSETs, two chain HASHes and the open
    # pointer) always runs for correctness, and deliberate queries
    # (`Model.query.filter(validity__current=True)` / `validity__as_of=t`) still
    # work — this switch governs only the subconscious gating path. Note the
    # blast radius of "on by default" is zero at merge: no shipped model
    # declares a ValidityField. Boolean, not swept.
    VALIDITY_GATING_ENABLED = True
    # Open-interval sentinel: the `invalid_at` score of a record that is still
    # true. `+inf` is native to Redis and Valkey sorted sets — ZADD stores it,
    # ZSCORE returns "inf", ZRANGEBYSCORE "(t" "+inf" includes it, and Lua 5.1's
    # tonumber() parses it — so an open interval needs no special-casing on any
    # read shape. Not a tunable: changing it would silently reclassify every
    # already-stored open record as closed.
    VALIDITY_OPEN_SENTINEL = float("inf")

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
    # Confidence-driven forgetting (issue #491). Closes the promote/forget
    # asymmetry: confidence could already promote a memory to permanence but
    # never hasten its removal.
    # Conservative by design — 0.3 sits well below INITIAL_CONFIDENCE (0.5), so
    # a record must have moved decisively negative rather than merely failing to
    # accumulate positive evidence, and below
    # LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD (0.6) so the forget and promote
    # bands cannot overlap.
    LIFECYCLE_FORGET_CONFIDENCE_CEILING = 0.3
    # Load-bearing guard: ConfidenceField starts at 0.5 and moves on every
    # signal, so without a minimum track record a single unlucky dismissal
    # could bury a memory. 5 observations is roughly a quarter of
    # CONFIDENCE_EVIDENCE_CAP (20) — enough for the running mean to reflect a
    # pattern rather than an accident.
    LIFECYCLE_FORGET_MIN_EVIDENCE = 5
    # Bounded tombstone retention (issue #491 Risk 7): forgetting tombstones
    # rather than deletes, so retention must be capped or tombstones outgrow
    # the records they replaced. Oldest age out past this count. 1000 keeps the
    # negative-evidence corpus meaningful for #494 while staying small next to
    # the 20k-record scale target. Each tombstone archives the record's full
    # payload (that archive is what makes restore() possible) plus a
    # fingerprint and death metadata, so retention has to be bounded rather
    # than assumed cheap.
    LIFECYCLE_TOMBSTONE_RETENTION_LIMIT = 1000

    # -- Sorted-range limit pushdown (models/query.py) -------------------------
    # Extra members requested beyond `limit` when a bound is pushed into a
    # sorted-set read. Index members whose backing hash is gone hydrate to
    # nothing, and under a bounded read those come straight off the result
    # count. The margin absorbs ordinary orphan density in the same round trip;
    # the unbounded re-read behind it is the correctness backstop, not the
    # common path. 8 covers the small top-N reads this path is built for
    # without meaningfully enlarging a 5-row query.
    SORTED_PUSHDOWN_OVERFETCH_MARGIN = 8

    # -- Extraction (extraction/) ---------------------------------------------
    # Experimental tuning constants for the pluggable LLM-extraction path
    # (popoto.extraction). Not yet swept -- initial values set by design,
    # per issue #461 / docs/plans/llm_memory_extraction_path.md.
    EXTRACTION_DEFAULT_IMPORTANCE = 0.5  # aligns with SubconsciousMemory.extract_memories()'s current flat importance default
    EXTRACTION_DEFAULT_CONFIDENCE = 0.7  # signal applied when a provider asserts a fact but returns no explicit confidence
    EXTRACTION_ENTITY_PAIR_LINK_WEIGHT = 0.1  # matches CO_OCCURRENCE_INITIAL_WEIGHT; must stay <= CO_OCCURRENCE_WEIGHT_CAP (1.0) or CoOccurrenceField.link() raises
    EXTRACTION_MAX_ENTITIES_PER_FACT = 12  # cap on deduped entities paired per fact; combinations grow O(n^2), so a malformed/adversarial extraction with many entities can't blow up co-occurrence writes

    # -- datetime KeyField identity (models/canonical_key.py, #537/#538) -------
    # Deploy-level kill switch, not a tuning constant. When True,
    # ``canonical_key_str`` falls back to ``str(value)`` for datetimes, which
    # reproduces 1.8.2 key bytes exactly. Default is False (canonicalization
    # ON) per the repo's default-on doctrine; the switch exists so an adopter
    # can roll readers forward to >= 1.9.0 *before* any key byte moves, then
    # run the migration, then lift it. Read from the environment at import so
    # it can be set without editing model code; assign it directly to override
    # at runtime. See migration cookbook recipe 19.
    DATETIME_KEY_LEGACY = _read_legacy_datetime_key_switch()

    # -- never-record firewall (privacy/never_record.py, #561) ----------------
    # Shortest whitespace token the entropy backstop will consider. Below 20
    # chars, ordinary base64-ish words (identifiers, hashes-of-hashes in
    # prose) dominate and the detector becomes noise rather than a backstop.
    NR_ENTROPY_MIN_TOKEN_LEN = 20
    # Shannon entropy in bits/char at or above which a credential-charset
    # token is treated as an unknown-prefix secret. Random base64 sits near
    # 5.5-6.0 and random hex near 4.0; English text over the same alphabet
    # sits well below 3.5. Not corpus-tuned -- deliberately conservative in
    # the over-blocking direction, per #561 ("over-blocking accepted").
    NR_ENTROPY_MIN_BITS = 3.5
    # Shortest value after ``password=``/``token:`` that counts as a secret.
    # Also the shortest URL-userinfo password.
    NR_ASSIGNMENT_MIN_VALUE_LEN = 6
    # Cap on the capped-LIST audit log of content-free drop tombstones. The
    # HASH counters are unbounded and authoritative; this list is a recent
    # window for eyeballing drop cadence.
    NR_TOMBSTONE_LOG_MAX = 1000
    # Deploy-level kill switch, not a tuning constant. False disables the
    # never-record firewall entirely. Default is enabled per the repo's
    # default-on doctrine; the switch exists because a PyPI adopter cannot
    # always edit model code to remove a mixin. Read from the environment at
    # import (``POPOTO_NEVER_RECORD_DISABLE``); assign directly to override
    # at runtime.
    NEVER_RECORD_ENABLED = _read_never_record_switch()

    # -- provenance journal (recipes/provenance_journal.py, #560) -------------
    # Deploy-level kill switch, not a tuning constant. When True (the default),
    # a ``supersede``/``retract`` annotation closes its target's validity
    # interval in the same MULTI/EXEC that appends the annotation, so the
    # target leaves ``validity__current`` membership immediately. When False,
    # the annotation entry is still appended and still carries its ``target``,
    # but the target's interval is left open: membership degrades to
    # "everything ever appended", which is pre-#560 behavior. The degraded mode
    # is observable without reading Redis --
    # ``AnnotationResult.target_closed`` is False and
    # ``AnnotationResult.coupling_enabled`` is False -- specifically so this
    # switch cannot reproduce #588's silent-no-op shape. Read from the
    # environment at import (``POPOTO_JOURNAL_COUPLING_DISABLE``) because a
    # PyPI adopter cannot always edit model code; assign directly to override
    # at runtime. Boolean, not swept.
    JOURNAL_VALIDITY_COUPLING_ENABLED = _read_journal_coupling_switch()
    # Core annotation-kind vocabulary for ``JournalEntry.kind``. Not a
    # tunable: changing it reclassifies already-stored entries. ``assert`` is
    # an original capture and carries no target; the other three are
    # annotations and each names exactly one target entry. Downstream modules
    # that need more kinds (M5 merge/equivalence, M7 queueing, M8 exposure)
    # extend via a ``JournalEntry`` subclass setting ``_journal_kinds``, which
    # is validated at class-definition time as a superset of these four rather
    # than by editing this tuple. Reader rule: an entry whose ``kind`` a reader
    # does not recognize is inert for membership -- never silently treated as
    # ``supersede`` or ``retract``.
    JOURNAL_KINDS = ("assert", "confirm", "supersede", "retract")


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
