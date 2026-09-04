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

import logging
import os

logger = logging.getLogger("POPOTO.constants")

#: Environment values that read as "on" for a boolean deploy-level switch.
_TRUTHY = ("1", "true", "yes", "on")

#: Non-numeric environment values that read as "off". Kept separate from
#: :data:`_TRUTHY` because a value switch cannot reuse an *on* set.
_FALSY = ("off", "false", "no")

#: Raw environment values already warned about, so a malformed value that is
#: re-read on every save warns once per process rather than once per save.
_WARNED_BAD_ENV: set[str] = set()


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


def _read_decode_quarantine_switch() -> bool:
    """Read ``POPOTO_DECODE_QUARANTINE_DISABLE`` from the environment (#573).

    Returns True when decode quarantine is ENABLED — i.e. when an undecodable
    non-key field should be tolerated (raw bytes preserved, warning logged,
    field recorded in ``_corrupt_fields``) instead of raising. The env var is
    phrased as a *disable* so the default-on doctrine holds when it is unset.

    A ``_DISABLE`` switch is a two-state membership test: anything not in
    :data:`_TRUTHY` means "not disabled", which is the safe default. There is
    deliberately no malformed-value handling and :data:`_WARNED_BAD_ENV` is
    untouched.

    This is a call-time function and deliberately **not** a ``Defaults`` class
    attribute: the class body is evaluated at import, which would bind the
    value once and make a deploy-time flip (or a ``monkeypatch.setenv``)
    a no-op. The call-time precedent is
    :func:`_read_default_memory_max_records` above. It is only ever called
    from ``_decode_field_value``'s ``except`` branch, which is already off the
    healthy path, so the ``os.environ`` read costs a healthy row nothing.
    """
    value = os.environ.get("POPOTO_DECODE_QUARANTINE_DISABLE", "").strip().lower()
    return value not in _TRUTHY


def _read_m4_resolution_switch() -> bool:
    """Read ``POPOTO_M4_RESOLUTION_ENABLED`` from the environment (#563).

    Deploy-level kill switch for the reference-resolution stage, default
    ``True`` per the repo's default-on doctrine. Unlike
    :func:`_read_journal_coupling_switch` / :func:`_read_never_record_switch`,
    the env var name already reads as "enabled" rather than "disable", so no
    inversion is needed: unset or empty leaves the stage on, an explicit
    truthy value leaves it on, and anything else (including an explicit
    falsy value) turns it off.
    """
    value = os.environ.get("POPOTO_M4_RESOLUTION_ENABLED", "").strip().lower()
    if not value:
        return True
    return value in _TRUTHY


def _read_default_memory_max_records() -> int | None:
    """Cap on records **per ``agent_id``** kept by ``DefaultMemory``;
    ``0``/``off`` disables eviction.

    Reads ``POPOTO_DEFAULT_MEMORY_MAX_RECORDS`` (#596) — the deploy-level
    escape hatch for the eviction introduced in #594, for hook adopters who
    use ``DefaultMemory`` directly and cannot edit model code. The env var
    name omits ``PER_AGENT`` for table brevity, so the scope is stated here.

    Parse order, on the stripped/lowercased raw value:

    1. unset or empty → ``None`` ("no opinion"; the class attribute applies);
    2. ``int(raw)`` succeeds and is ``>= 0`` → that integer (``0`` disables
       eviction, ``1`` unambiguously means a cap of one record);
    3. ``int(raw)`` succeeds and is negative → malformed;
    4. value in :data:`_FALSY` → ``0`` (disabled);
    5. anything else → malformed.

    Malformed values warn once per distinct raw value (deduped through
    :data:`_WARNED_BAD_ENV`, since this runs on every save) and return
    ``None``. This never raises: eviction must never fail a save.

    :data:`_TRUTHY` is deliberately **not** consulted. It is an *on* set that
    cannot express this switch's disable words, and ``"1"`` is a member of it
    while also being a valid cap of one record — so integers are parsed first
    and ``_TRUTHY`` never applies.

    Return type is ``int | None`` and the two falsy values must not collapse:
    ``0`` means "explicitly disabled", ``None`` means "defer to the class
    attribute".

    This is a call-time function and deliberately **not** a ``Defaults`` class
    attribute (nor ``lru_cache``-wrapped): binding the value at import time
    would defeat a runtime-flippable deploy switch — the defect recorded for
    ``VALIDITY_GATING_ENABLED`` in ``tests/benchmarks/test_defaults_sync.py``.
    """
    raw = os.environ.get("POPOTO_DEFAULT_MEMORY_MAX_RECORDS", "").strip()
    if not raw:
        return None
    value = raw.lower()
    malformed = False
    try:
        parsed = int(value)
    except ValueError:
        if value in _FALSY:
            return 0
        malformed = True
    else:
        if parsed >= 0:
            return parsed
        malformed = True
    if malformed and raw not in _WARNED_BAD_ENV:
        _WARNED_BAD_ENV.add(raw)
        logger.warning(
            "POPOTO_DEFAULT_MEMORY_MAX_RECORDS=%r is not a non-negative "
            "integer or one of %s; ignoring it and using the model's "
            "_max_records_per_agent",
            raw,
            ", ".join(_FALSY),
        )
    return None


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

    # --- DefaultMemory eviction ---
    # Memories per agent partition kept by popoto.recipes.DefaultMemory. On
    # every save past the cap, the stalest records by decay timestamp are
    # deleted with full index cleanup. Nothing else on the default path
    # evicts, so without this the store grows one record per turn forever.
    # Not a tuning constant so much as a safety rail; raise it in a subclass
    # by overriding ``_max_records_per_agent``.
    DEFAULT_MEMORY_MAX_RECORDS_PER_AGENT = 1000

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
    # extend via ``JournalEntry.register_kind(name, targetless=, closing=)``,
    # which adds to the vocabulary in place rather than editing this tuple.
    # (Registration rather than a model subclass because Popoto's ModelBase
    # metaclass does not inherit Field attributes, so a JournalEntry subclass
    # has an empty field set.) Reader rule: an entry whose ``kind`` a reader
    # does not recognize is inert for membership -- never silently treated as
    # ``supersede`` or ``retract``.
    JOURNAL_KINDS = ("assert", "confirm", "supersede", "retract")

    # -- auditable extraction (extraction/decision_log.py, #562) --------------
    # Lifetime of the assembly claim one runner takes on a candidate before
    # writing to the provenance journal, in milliseconds. A **liveness** bound,
    # not a correctness one: long enough that the common case never expires
    # mid-flight, finite so a crashed runner's claim cannot wedge a candidate
    # forever. Correctness under a claim that does expire mid-flight comes from
    # the identity probe on the ``cand:`` subject tag, which makes the residual
    # race converge on the existing entry instead of duplicating it. Pinned
    # in-repo rather than exposed on ``AuditableExtractionConfig``, per the
    # magic-number rule.
    M3_ASSEMBLY_CLAIM_TTL_MS = 30_000

    # -- reference resolution (extraction/resolution.py, #563) --
    # Deploy-level kill switch, not a tuning constant. Default True per the
    # repo's default-on doctrine; a PyPI adopter who cannot edit model code
    # still needs a way to turn the stage off (e.g. no `anthropic` client
    # available). Read from the environment at import
    # (``POPOTO_M4_RESOLUTION_ENABLED``); assign directly to override at
    # runtime.
    M4_RESOLUTION_ENABLED = _read_m4_resolution_switch()
    # Window truncation bound 1 of 2, in turns. A turn count alone does not
    # bound prompt size, so it is paired with a char bound below; whichever
    # binds first truncates oldest-first (Decision 1).
    M4_WINDOW_MAX_TURNS = 8
    # Window truncation bound 2 of 2, in characters. A char bound alone can
    # slice a single turn in half, so it is paired with the turn-count bound
    # above (Decision 1).
    M4_WINDOW_MAX_CHARS = 4000
    # Cap on references returned per candidate. Re-validation rejects a reply
    # over this cap rather than truncating it, so a runaway model response
    # cannot silently balloon a single candidate's evidence.
    M4_MAX_REFERENCES_PER_CANDIDATE = 8
    # Lower bound on ``evidence_gap`` candidate referents. Below this there is
    # no genuine ambiguity to report -- a single candidate is a resolution,
    # not a gap.
    M4_EVIDENCE_GAP_MIN_CANDIDATES = 2
    # Upper bound on ``evidence_gap`` candidate referents. Above this the
    # model is listing possibilities rather than narrowing them, which is not
    # useful evidence for the one clarifying question the record carries.
    M4_EVIDENCE_GAP_MAX_CANDIDATES = 4
    # Max length of an ``assumed`` status's one-line assumption. Re-validation
    # enforces this (and no newlines) so the assumption stays a scannable
    # audit line, not free-form prose.
    M4_ASSUMPTION_MAX_CHARS = 200
    # Max length of an ``evidence_gap`` status's clarifying question, for the
    # same scannability reason as ``M4_ASSUMPTION_MAX_CHARS`` above.
    M4_QUESTION_MAX_CHARS = 200
    # Multiplicative term of the ``statement`` length bound relative to
    # ``verbatim`` (paired with ``M4_STATEMENT_MAX_GROWTH_CHARS`` below).
    # Re-validation enforces this so the model cannot turn a clause into a
    # paragraph of invention.
    M4_STATEMENT_MAX_GROWTH_FACTOR = 2.0
    # Additive term of the ``statement`` length bound relative to
    # ``verbatim``; see ``M4_STATEMENT_MAX_GROWTH_FACTOR`` above. The additive
    # term keeps very short verbatims from being bounded to near-zero growth.
    M4_STATEMENT_MAX_GROWTH_CHARS = 120
    # Temporal roles that emit ``valid_from`` (Decision 4). Onsets only, by
    # deliberate narrowing of the amendment that also named deadlines:
    # emitting a future deadline as ``valid_from`` would hide the obligation
    # from as-of retrieval until the deadline arrives, which is a data error.
    # A constant rather than a literal in the emission code so a maintainer
    # reversal ("deadlines also emit") is a one-tuple change, not a rewrite;
    # a parameterised test flips it to ("onset", "deadline") and asserts a
    # deadline reference then does emit.
    M4_VALID_FROM_ROLES = ("onset",)


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
