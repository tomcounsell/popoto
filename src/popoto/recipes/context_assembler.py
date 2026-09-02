"""ContextAssembler — Retrieval-to-injection bridge with token budgets.

A capstone recipe composing all shipped Popoto memory primitives into a
single ``assemble()`` call. Orchestrates pull-path (query-driven) and
push-path (proactive surfacing) retrieval, applies token budgets, and
formats output for LLM context injection.

Metacognitive extensions (opt-in, off-by-default):

* ``RetrievalQuality`` dataclass surfaces avg confidence, score spread,
  feeling-of-knowing (FOK), and staleness for the retrieval.
* ``ContextAssembler.assess(query_cues)`` — pre-retrieval FOK probe.
* ``ContextAssembler.assemble(..., assess_quality=True)`` — attaches a
  ``RetrievalQuality`` to ``AssemblyResult.metadata["quality"]`` without
  changing the default behavior.

Pipeline:
    Pull path: ExistenceFilter pre-check → CompositeScoreQuery → CoOccurrence propagation
    Push path: CyclicDecayField temporal scan above surfacing threshold
    Merge: Deduplicate, re-rank, budget-select, post-effects, format

Synergy with Popoto Primitives:
    ┌────────────────────────┬───────────────────────────────────────┐
    │ Primitive              │ Role in ContextAssembler              │
    ├────────────────────────┼───────────────────────────────────────┤
    │ DecayingSortedField    │ Score index for CompositeScoreQuery   │
    │ CyclicDecayField       │ Push-path proactive surfacing         │
    │ ConfidenceField        │ Score index + competitive suppression │
    │ CoOccurrenceField      │ Pull-path candidate expansion         │
    │ ExistenceFilter        │ Pull-path pre-check (skip if absent)  │
    │ AccessTrackerMixin     │ on_read post-effect tracking          │
    │ ObservationProtocol    │ on_read / on_surfaced dispatch        │
    │ RecallProposal         │ Created for push-path records         │
    │ WriteFilterMixin       │ Priority score in composite           │
    │ EventStreamMixin       │ Mutation logging (via model save)     │
    │ PredictionLedgerMixin  │ Outcome tracking (via model save)     │
    │ CompositeScoreQuery    │ Multi-factor ranked retrieval         │
    └────────────────────────┴───────────────────────────────────────┘

Dependencies:
    All 12 shipped Popoto primitives (Steps 1-12 of the memory roadmap).
    No external dependencies beyond Popoto itself.

Example:
    from popoto.recipes.context_assembler import ContextAssembler

    assembler = ContextAssembler(
        model_class=Memory,
        score_weights={"relevance": 0.6, "confidence": 0.3},
        max_items=10,
        max_tokens=4000,
    )
    result = assembler.assemble(
        query_cues={"topic": "deployment"},
        agent_id="agent-1",
    )
    # result.records — selected instances
    # result.proactive — push-path subset
    # result.formatted — LLM-ready string
    # result.metadata — scores, timing, token counts
"""

import json
import logging
import re
import statistics
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, cast

from ..fields.bm25_field import BM25Field
from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
from ..fields.cyclic_decay_field import CyclicDecayField
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.embedding_field import EmbeddingField
from ..fields.existence_filter import ExistenceFilter
from ..fields.observation import ObservationProtocol
from ..fields.sorted_field_mixin import SortedFieldMixin
from ..fields.tag_field import TagFieldMixin
from ..fields.validity_field import ValidityField
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.ContextAssembler")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _get_key(instance) -> str:
    """Get the Redis key for a model instance.

    Popoto stores the key as ``_redis_key`` on hydrated instances.
    Falls back to ``db_key.redis_key`` for freshly-saved instances.
    """
    key = getattr(instance, "_redis_key", None)
    if not key:
        try:
            key = instance.db_key.redis_key
        except Exception:
            return str(id(instance))
    return key


# ---------------------------------------------------------------------------
# Tuning Constants — validated via parameter sweep (issue #234)
# ---------------------------------------------------------------------------

COMPETITIVE_SUPPRESSION_SIGNAL = Defaults.COMPETITIVE_SUPPRESSION_SIGNAL
"""Signal strength for competitive suppression of non-selected pull-path
candidates. Applied via ConfidenceField.update_confidence(). Values < 0.5
act as contradiction signals, mildly reducing future ranking.
Optimal range: [0.1, 0.7]. Insensitive to retrieval quality."""

DEFAULT_SURFACING_THRESHOLD = Defaults.DEFAULT_SURFACING_THRESHOLD
"""Minimum score for push-path records to be surfaced. Records from
CyclicDecayField scan below this threshold are filtered out.
Optimal range: [0.1, 0.9]. Insensitive to retrieval quality."""

DEFAULT_MAX_ITEMS = 10
"""Default maximum number of records returned by assemble()."""

DEFAULT_PROPAGATION_DEPTH = 2
"""Default BFS depth for CoOccurrence propagation."""

RRF_K = 60
"""RRF rank-fusion constant (Cormack et al. 2009). Do not expose as user config;
tuning experiments belong in a separate follow-up."""

HYBRID_CANDIDATE_MULTIPLIER = 5
"""candidate_limit = max_items * HYBRID_CANDIDATE_MULTIPLIER for per-signal
retrieval in the hybrid pull path before RRF fusion."""

EXCLUDE_HEADROOM_CAP = 200
"""Ceiling on the extra candidates fetched to see past an ``exclude_keys``
set. Every arm's exclusion filter runs after a bounded fetch, so suppression
without headroom silently starves retrieval: with the default max_items=5 the
composite arm fetches 10 candidates, and a 10-key exclusion set empties it.
:meth:`ContextAssembler._fetch_limit` adds the exclusion count back.

The cap bounds the widening. A session that has already surfaced hundreds of
distinct memories is better served by decay than by an ever-wider ZRANGE, and
degrading to fewer results is the right answer once nearly everything relevant
has been said. A magic number for tuning, not user config."""

EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD = 0.5
"""NOT a shipped default — used only by the benchmark/report script; needs
maintainer sign-off before becoming Defaults.CONFIDENCE_GATE_THRESHOLD or a
ctor default (see issue #463)."""


# ---------------------------------------------------------------------------
# Weighted / query-adaptive RRF fusion (issue #457) — experimental tuning
# constants, in-code only, never user config (magic-number doctrine).
#
# Unweighted RRF gave the dense (vector) arm equal say with BM25. On
# coreference-heavy multi-session dialogue (LoCoMo) the dense arm is
# net-harmful — its standalone recall is near-zero there (vector-only R@1
# ~0.05) yet it still casts full rank-votes, displacing correct BM25 hits
# (unweighted hybrid R@1 0.1667 << lexical 0.2986). On paraphrastic first-
# person recall (LongMemEval-S) the same dense arm is net-helpful (unweighted
# hybrid R@1 0.894 > lexical 0.856).
#
# The two query populations are cleanly separable by shape (measured over the
# full datasets): LoCoMo questions are third-person and NAME-anchored — 99.6%
# contain a proper-noun name, 0% are first-person — while LongMemEval-S
# questions are first-person self-recall — 95.8% contain "I/my/me", only 2.6%
# carry a name without a first-person pronoun. So the adaptive rule is: when a
# query is name/date/token-specific and NOT first-person (LoCoMo's shape),
# DROP the dense arm (vector weight 0) — this makes the hybrid RRF converge
# exactly to the lexical result, eliminating the regression. Otherwise
# (first-person / paraphrastic, LongMemEval's shape) keep unweighted RRF —
# the blend that produced the LongMemEval win. Cheap, deterministic, in-
# process — no ML, no new deps, Valkey-safe.
#
# NOTE: an earlier revision used a token-*fraction* specificity threshold plus
# a vector-lean regime that DOWN-weighted BM25. That misfired badly — a single
# name in a ~7-token question scores a ~0.14 fraction, so 75% of LoCoMo
# queries were misclassified paraphrastic and had their good BM25 arm
# down-weighted, making weighted hybrid (R@1 0.1556) *worse* than unweighted
# (0.1667). The name/first-person signal below replaces that fraction logic.
# ---------------------------------------------------------------------------

FUSION_WEIGHT_GRAPH = 1.0
"""Graph (co-occurrence) arm RRF weight. Fixed — not adapted by query shape;
only the keyword/vector balance is query-adaptive."""

FUSION_REGIME_KEYWORD_LEAN = {"keyword": 1.0, "vector": 0.0}
"""Weight regime for name/date/token-specific, non-first-person queries
(LoCoMo's shape). The dense arm's standalone recall is near-zero on these
queries yet it still casts full rank-votes under unweighted RRF, so it is
dropped entirely (vector weight 0). With the vector arm contributing zero to
every document's RRF score, the fused ranking converges to the lexical
(BM25 + graph) result — guaranteeing hybrid >= lexical on this query shape."""

FUSION_REGIME_NEUTRAL = {"keyword": 1.0, "vector": 1.0}
"""Weight regime for paraphrastic / first-person queries (LongMemEval-S's
shape). Equal weight == plain unweighted RRF — the exact blend that produced
the LongMemEval-S hybrid win (R@1 0.894 > lexical 0.856), so it is preserved
verbatim for that query shape."""

_FUSION_DATE_TOKEN_RE = re.compile(r"^\d{1,4}([/-]\d{1,2}){0,2}$")
_FUSION_QUOTED_RE = re.compile(r"\"[^\"]+\"|'[^']+'")

# First-person markers — presence signals paraphrastic self-recall
# (LongMemEval-S's shape), where the dense arm helps and must be kept.
_FUSION_FIRST_PERSON = frozenset(
    {
        "i",
        "me",
        "my",
        "mine",
        "myself",
        "i'm",
        "i've",
        "i'd",
        "i'll",
        "we",
        "us",
        "our",
        "ours",
    }
)

# Sentence-initial / interrogative capitalized words that are NOT proper-noun
# names — excluded so an interrogative appearing mid-query does not read as a
# name. (The sentence-initial token is already skipped positionally.)
_FUSION_STOP_CAP = frozenset(
    {
        "What",
        "When",
        "Where",
        "Who",
        "Whom",
        "Whose",
        "Why",
        "Which",
        "How",
        "Would",
        "Will",
        "Did",
        "Do",
        "Does",
        "Is",
        "Are",
        "Was",
        "Were",
        "Has",
        "Have",
        "Had",
        "Can",
        "Could",
        "Should",
        "The",
        "A",
        "An",
        "If",
        "In",
        "On",
        "At",
        "I",
    }
)


def _fusion_weights(query_text: str) -> dict:
    """Query-adaptive RRF weight regime for the keyword/vector arms (#457).

    Classifies the query by shape and returns the corresponding in-code
    weight regime:

    - **Name/date/token-specific AND not first-person** (LoCoMo's shape) ->
      ``FUSION_REGIME_KEYWORD_LEAN`` (vector weight 0). The dense arm is
      net-harmful on these queries, so it is dropped and the hybrid fusion
      converges to the lexical result.
    - **Otherwise** (first-person / paraphrastic, LongMemEval-S's shape) ->
      ``FUSION_REGIME_NEUTRAL`` (unweighted RRF), preserving the LongMemEval-S
      hybrid win.

    The specificity signal is (a) a proper-noun-like name (a capitalized,
    non-ALL-CAPS, non-interrogative token that is not sentence-initial),
    (b) a digit/date/year token, or (c) a quoted/exact-token substring. A
    first-person pronoun anywhere in the query ("I", "my", "me", ...) vetoes
    the keyword-lean classification, because first-person self-recall is
    LongMemEval-S's shape where the dense arm helps.

    Pure Python, deterministic, no ML, no server round-trip -- Valkey-safe.

    Args:
        query_text: The joined query-cue text passed to BM25/vector search.

    Returns:
        ``{"keyword": w, "vector": w}`` weight mapping suitable for merging
        into the ``weights`` dict passed to ``Query.fuse()``. The graph arm
        (``FUSION_WEIGHT_GRAPH``) is not query-adaptive and is applied
        separately by the caller.
    """
    tokens = query_text.split()
    if not tokens:
        return dict(FUSION_REGIME_NEUTRAL)

    # First-person self-recall (LongMemEval-S's shape) -> keep the dense arm.
    for tok in tokens:
        if tok.strip(".,!?;:\"'").lower() in _FUSION_FIRST_PERSON:
            return dict(FUSION_REGIME_NEUTRAL)

    # Lexical-specificity signal: any quoted span, digit/date token, or
    # proper-noun-like name marks the query as token-specific (LoCoMo's shape).
    if _FUSION_QUOTED_RE.search(query_text):
        return dict(FUSION_REGIME_KEYWORD_LEAN)

    for idx, tok in enumerate(tokens):
        bare = tok.strip(".,!?;:\"'")
        if not bare:
            continue
        if bare.isdigit() or _FUSION_DATE_TOKEN_RE.match(bare):
            return dict(FUSION_REGIME_KEYWORD_LEAN)
        if (
            idx > 0
            and bare[0].isupper()
            and not bare.isupper()
            and bare not in _FUSION_STOP_CAP
        ):
            # Capitalized, not sentence-initial, not an ALL-CAPS acronym, not
            # an interrogative — proper-noun-like (a person/place name).
            return dict(FUSION_REGIME_KEYWORD_LEAN)

    return dict(FUSION_REGIME_NEUTRAL)


# ---------------------------------------------------------------------------
# AssemblyResult
# ---------------------------------------------------------------------------


@dataclass
class AssemblyResult:
    """Return type for ContextAssembler.assemble().

    Attributes:
        records: All selected instances (pull + push, deduplicated).
        proactive: Push-path subset of records (proactively surfaced).
        formatted: LLM-ready formatted string (JSON, XML, or natural).
        metadata: Dict with scores, token_count, timing_ms, pull_count,
            push_count.
    """

    records: list = field(default_factory=list)
    proactive: list = field(default_factory=list)
    formatted: str = ""
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RetrievalQuality — Metacognitive layer
# ---------------------------------------------------------------------------


@dataclass
class RetrievalQuality:
    """Metacognitive signal describing retrieval trustworthiness.

    Surfaces four machine-readable metrics about a retrieval so an agent
    can decide whether to trust its context, retry with different cues,
    widen scope, or caveat its downstream answer. This is a purely
    *mechanical* signal — no LLM self-reporting — following the research
    finding that GPT-4's self-reported confidence reflects output structure
    rather than internal uncertainty.

    Attributes:
        avg_confidence: Mean of ``ConfidenceField.get_confidence()`` across
            selected records. ``1.0`` when the model has no ConfidenceField
            (no evidence against the retrieval).
        score_spread: Coefficient of variation (stddev / mean) of the
            per-record composite scores. High spread means one or two records
            dominate; low spread means results are roughly equivalent.
            Falls back to ``0.0`` when ``abs(mean) < 1e-9`` — stddev/mean is
            undefined when mean is zero.
        fok_score: Feeling-of-knowing — 0.4 * cue_familiarity + 0.4 *
            partial_retrieval_count + 0.2 * subthreshold_activation,
            averaged across query cues. ``0.0`` when no cues were provided.
        staleness_ratio: Fraction of selected records with
            DecayingSortedField score below the field's decay threshold.
            ``0.0`` when the model has no DecayingSortedField.
        score_distribution: Optional full list of per-record composite
            scores for histogram analysis; empty when unavailable.
        per_cue_fok: Optional dict mapping cue value -> dict with the
            three FOK components for that cue (for debugging).

    Example:
        quality = assembler.assess({"topic": "deploy"})
        if quality.fok_score < 0.3:
            # Skip the expensive retrieval; we don't know this domain
            return
        result = assembler.assemble({"topic": "deploy"}, assess_quality=True)
        if result.metadata["quality"].avg_confidence < 0.4:
            # Caveat the downstream response
            ...
    """

    avg_confidence: float = 0.0
    score_spread: float = 0.0
    fok_score: float = 0.0
    staleness_ratio: float = 0.0
    score_distribution: list = field(default_factory=list)
    per_cue_fok: dict = field(default_factory=dict)

    @classmethod
    def from_records(
        cls,
        records,
        query_cues=None,
        score_weights=None,
        max_items=DEFAULT_MAX_ITEMS,
        surfacing_threshold=DEFAULT_SURFACING_THRESHOLD,
    ) -> "RetrievalQuality":
        """Build a RetrievalQuality over an already-retrieved list of records.

        Intended for custom retrieval pipelines (BM25, RRF, hybrid) that
        want the metacognitive layer without adopting
        :class:`ContextAssembler`. All model capabilities (ConfidenceField,
        ExistenceFilter, DecayingSortedField) are introspected from
        ``records[0]._meta.fields``. Heterogeneous record lists are
        rejected with ``TypeError`` — score weights and capability field
        names are per-model-class, so a mixed list would silently produce
        incorrect FOK / score_spread / staleness_ratio values.

        Args:
            records: Non-empty list of Popoto Model instances of a single
                concrete class. When empty, returns a zero-valued
                :class:`RetrievalQuality`.
            query_cues: Optional dict of query cues — same shape as
                ``ContextAssembler.assess(query_cues=...)``. When falsy,
                ``fok_score`` is 0.0 and ``per_cue_fok`` is empty.
            score_weights: Optional dict mapping sorted-field names to
                weights. Used for ``score_spread`` and ``staleness_ratio``.
                When None, both default to 0.0 and ``score_distribution``
                is empty.
            max_items: Denominator for ``partial_retrieval_count`` in the
                FOK formula. Default matches ``ContextAssembler``.
            surfacing_threshold: Threshold for subthreshold_activation and
                staleness_ratio. Default matches ``ContextAssembler``.

        Returns:
            A :class:`RetrievalQuality` dataclass. Field semantics match
            the assembler path exactly — see class docstring.

        Raises:
            TypeError: If ``records`` contains instances of more than one
                concrete model class.

        Example:
            >>> from popoto import RetrievalQuality
            >>> records = my_bm25_pipeline(query)  # custom retrieval
            >>> quality = RetrievalQuality.from_records(
            ...     records,
            ...     query_cues={"topic": query},
            ...     score_weights={"relevance": 1.0},
            ... )
            >>> if quality.fok_score < 0.3:
            ...     return "low confidence retrieval"
        """
        # Empty list -> zero-valued quality, no warning.
        if not records:
            return cls()

        # Mixed-model guard (C4): fail loudly, not silently.
        distinct_types = {type(r) for r in records}
        if len(distinct_types) > 1:
            class_names = sorted(t.__name__ for t in distinct_types)
            raise TypeError(
                "RetrievalQuality.from_records requires a homogeneous list "
                f"of records; got {len(distinct_types)} distinct model "
                f"classes: {class_names}"
            )

        # All records are the same class — introspect once at the entry point.
        model_class = type(records[0])
        existence_filter = None
        confidence_field_name = None
        decaying_sorted_field_name = None
        for name, f in model_class._meta.fields.items():
            if isinstance(f, ExistenceFilter) and existence_filter is None:
                existence_filter = f
            if isinstance(f, ConfidenceField) and confidence_field_name is None:
                confidence_field_name = name
            if (
                isinstance(f, DecayingSortedField)
                and decaying_sorted_field_name is None
            ):
                decaying_sorted_field_name = name

        # Delegate numeric work to the pure module-level helpers (C2).
        avg_conf = _avg_confidence(
            records,
            confidence_field_name=confidence_field_name,
            model_class=model_class,
        )

        if score_weights is None:
            # Skip score_spread / staleness_ratio entirely.
            score_spread = 0.0
            distribution: list = []
            staleness = 0.0
        else:
            score_spread, distribution = _compute_score_spread(
                records,
                model_class=model_class,
                score_weights=score_weights,
            )
            staleness = _staleness_ratio(
                records,
                model_class=model_class,
                score_weights=score_weights,
                surfacing_threshold=surfacing_threshold,
                decaying_sorted_field_name=decaying_sorted_field_name,
            )

        if not query_cues:
            fok_score = 0.0
            per_cue: dict = {}
        else:
            # FOK needs pull-candidates; we have already-retrieved records,
            # so those are the candidates by construction.
            fok_score, per_cue = _compute_fok(
                query_cues,
                records,
                model_class=model_class,
                score_weights=score_weights or {},
                max_items=max_items,
                surfacing_threshold=surfacing_threshold,
                existence_filter=existence_filter,
            )

        return cls(
            avg_confidence=avg_conf,
            score_spread=score_spread,
            fok_score=fok_score,
            staleness_ratio=staleness,
            score_distribution=distribution,
            per_cue_fok=per_cue,
        )


# FOK formula weights — hard-coded per design spec (see plan
# "Rabbit Holes": adjusting these is out-of-scope).
FOK_WEIGHT_CUE_FAMILIARITY = 0.4
FOK_WEIGHT_PARTIAL_RETRIEVAL = 0.4
FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION = 0.2


# ---------------------------------------------------------------------------
# Module-level quality helpers (pure, keyword-only; issue #370 C2)
#
# These functions are the single source of truth for RetrievalQuality
# computation. Both the ContextAssembler instance methods and the public
# RetrievalQuality.from_records() classmethod introspect model capability
# fields ONCE at the entry point and then forward explicit kwargs into
# these helpers. Helpers do not introspect — that is the caller's job.
# ---------------------------------------------------------------------------


def _cue_familiarity(cue_value, *, existence_filter, model_class) -> float:
    """Return the FOK cue_familiarity component for a single cue value.

    ``existence_filter`` is the ``ExistenceFilter`` field (or ``None``).
    ``model_class`` is the concrete Popoto model — required because
    ``ExistenceFilter.might_exist`` is a method on the filter that takes
    the model class as the first argument.

    Returns ``0.5`` (neutral) when no ExistenceFilter is available OR the
    membership check fails. ``1.0`` when the bloom says might_exist,
    ``0.0`` when definitely_missing.
    """
    if existence_filter is None:
        return 0.5
    try:
        present = existence_filter.might_exist(model_class, str(cue_value))
    except Exception as e:
        logger.warning("cue_familiarity check failed for %r: %s", cue_value, e)
        return 0.5
    return 1.0 if present else 0.0


def _partition_scores_for_field(records, *, model_class, field_name, now=None):
    """Per-record relevance score read from the PARTITION-specific sorted set.

    Agent-memory sorted fields almost always declare ``partition_by``, so the
    real scores live in a partition-specific ZSET (``$SortF:Model:field:<part>``),
    not the base key. Reading the base key -- the historical behaviour of the
    shared proxy -- returned ``None`` for every partitioned record and silently
    collapsed every metacognitive signal to its degenerate value (issue #474).

    Scoring is field-type aware, because the two sorted-field families store
    *different quantities* as the ZSET score:

    * A plain :class:`SortedField` stores the field *value itself* as the
      score, so a raw pipelined ``ZSCORE`` on the partition key is the score.
    * A :class:`DecayingSortedField` (and its :class:`CyclicDecayField`
      subclass) stores the *last_updated timestamp* as the score, not
      relevance. A naive ``ZSCORE`` would surface ~1.7e9 timestamps and invert
      staleness. This reuses ``DECAY_SCORE_LUA`` to compute the power-law
      decayed relevance on the partition ZSET -- identical math to
      :meth:`Query.top_by_decay`, so the proxy and the query agree.

    Records whose member is absent from the partition ZSET, or whose partition
    field value is missing, map to ``None`` (never corrupting other records'
    scores). Read-only and Valkey-safe (``ZSCORE`` / read-only ``EVAL``; no
    ``ZUNIONSTORE``, no temp keys, no Redis modules).

    Returns:
        ``{record_key: score_or_None}`` for every record in ``records``.
    """
    f = model_class._meta.fields[field_name]

    # Resolve each record's partition-specific ZSET key up front. A record
    # whose partition value is missing raises QueryException in the key
    # builder -- it scores None and never perturbs the pipeline for the rest.
    key_for_record = {}
    for record in records:
        r_key = _get_key(record)
        try:
            key_for_record[r_key] = f.get_partitioned_sortedset_db_key(
                record, field_name
            ).redis_key
        except Exception:
            key_for_record[r_key] = None

    if isinstance(f, DecayingSortedField):
        return _decayed_partition_scores(
            records,
            field=f,
            key_for_record=key_for_record,
            now=now,
            model_class=model_class,
            field_name=field_name,
        )

    # Plain SortedField: the stored value IS the score.
    scores = {}
    ordered_keys = []
    pipe = POPOTO_REDIS_DB.pipeline()
    for record in records:
        r_key = _get_key(record)
        ordered_keys.append(r_key)
        zkey = key_for_record[r_key]
        if zkey is None:
            pipe.zscore("", "")  # placeholder keeps the pipeline plan aligned
        else:
            pipe.zscore(zkey, r_key)
    raw = pipe.execute()
    for r_key, val in zip(ordered_keys, raw):
        if val is None:
            scores[r_key] = None
        else:
            try:
                scores[r_key] = float(val)
            except (TypeError, ValueError):
                scores[r_key] = None
    return scores


def _decayed_partition_scores(
    records, *, field, key_for_record, now=None, model_class=None, field_name=None
):
    """Decayed relevance per record for a DecayingSortedField partition ZSET.

    Reuses the same decay scripts :meth:`Query.top_by_decay` runs, so the
    metacognitive proxy sees the same decayed relevance a query would surface,
    rather than the raw last_updated timestamp stored as the ZSET score:

    * :class:`DecayingSortedField` -> ``DECAY_SCORE_LUA`` (4 KEYS: the partition
      ZSET, the ConfidenceField ``:data`` hash (``""`` when modulation is off),
      and the ValidityField ``invalid_at`` / ``valid_from`` interval ZSETs
      (``""`` when the model has no validity axis — #580, plan D5)).
    * :class:`CyclicDecayField` -> ``CYCLIC_DECAY_LUA`` (4 KEYS: the partition
      ZSET, its ``:cycles`` / ``:pressure`` companion hashes, and the
      confidence ``:data`` hash), so cyclic cadence/pressure is honoured and
      the proxy agrees with ``top_by_decay`` for cyclic fields too.

    Confidence-modulated decay (#491) must be threaded here too, or the proxy
    would silently disagree with ``top_by_decay`` (the no-drift contract at
    ``test_proxy_matches_top_by_decay``). Unlike the query paths this one has
    records rather than filters, so each record's ``:data`` key is read off the
    instance; the script is evaluated once per distinct
    ``(partition ZSET, confidence hash)`` pair, which collapses to one EVAL per
    ZSET in the overwhelmingly common case where both share a partitioning.

    Read-only ``EVAL``, Valkey-safe.
    """
    from ..fields.cyclic_decay_field import CYCLIC_DECAY_LUA
    from ..fields.decaying_sorted_field import (
        DECAY_SCORE_LUA,
        MODULATION_DISABLED,
        confidence_modulation_args,
        validity_gate_args,
    )

    if now is None:
        now = time.time()

    # Validity gating (#580, plan D5). Resolves to ("", "", "") -- gate off,
    # byte-identical scores -- for every model without a ValidityField, and
    # whenever Defaults.VALIDITY_GATING_ENABLED is False. model_class is
    # optional on this path; validity_gate_args tolerates None.
    gate_invalid_key, gate_valid_key, gate_as_of = validity_gate_args(model_class)

    base_score_field = field.base_score_field or ""
    is_cyclic = isinstance(field, CyclicDecayField)

    # Per-record confidence modulation args. Resolves to MODULATION_DISABLED
    # (empty key, s="0") whenever modulation is off, which reproduces the
    # pre-#491 EVAL byte-for-byte apart from the inert trailing key/ARGV.
    conf_args_for_record = {}
    for r in records:
        r_key = _get_key(r)
        if model_class is None or field_name is None:
            conf_args_for_record[r_key] = MODULATION_DISABLED
            continue
        try:
            conf_args_for_record[r_key] = confidence_modulation_args(
                model_class, field, field_name, model_instance=r
            )
        except Exception as e:
            # The proxy is a best-effort metacognitive signal; degrade to
            # unmodulated decay rather than break assembly.
            logger.warning("confidence modulation unavailable for %s: %s", r_key, e)
            conf_args_for_record[r_key] = MODULATION_DISABLED

    # Distinct (partition ZSET, confidence args) pairs (typically one).
    eval_groups = {
        (key_for_record[_get_key(r)], conf_args_for_record[_get_key(r)])
        for r in records
        if key_for_record[_get_key(r)] is not None
    }

    # (zset_key, confidence_hash_key, member) -> decayed score
    decayed = {}
    for zkey, (conf_hash_key, conf_s, conf_c0) in eval_groups:
        try:
            cardinality = POPOTO_REDIS_DB.zcard(zkey)
            if not cardinality:
                continue
            if is_cyclic:
                # Companion hashes are the partition ZSET key plus a suffix
                # (CyclicDecayField.get_cycles/pressure_hash_key), so they are
                # derivable directly from the partition ZSET key. The
                # confidence hash is NOT -- it lives under its own
                # $ConfidencF: prefix -- so it is resolved Python-side above.
                result = POPOTO_REDIS_DB.eval(
                    CYCLIC_DECAY_LUA,
                    4,  # number of KEYS: zset + cycles + pressure + confidence
                    zkey,
                    zkey + ":cycles",
                    zkey + ":pressure",
                    conf_hash_key,
                    str(now),
                    str(field.decay_rate),
                    str(cardinality),  # return every member's decayed score
                    base_score_field,
                    conf_s,
                    conf_c0,
                )
            else:
                result = POPOTO_REDIS_DB.eval(
                    DECAY_SCORE_LUA,
                    # numkeys: zset + confidence (KEYS[2]) + invalid_at
                    # (KEYS[3]) + valid_from (KEYS[4]). Passing the validity
                    # keys without bumping this would shunt them into ARGV and
                    # silently corrupt base_score_field (plan Risk 1).
                    4,
                    zkey,
                    conf_hash_key,
                    gate_invalid_key,
                    gate_valid_key,
                    str(now),
                    str(field.decay_rate),
                    str(cardinality),  # return every member's decayed score
                    base_score_field,
                    conf_s,
                    conf_c0,
                    gate_as_of,  # ARGV[7]
                )
        except Exception as e:
            logger.warning("decayed partition score eval failed for %s: %s", zkey, e)
            continue
        # Flat array: [member1, score1, member2, score2, ...]
        for i in range(0, len(result), 2):
            member = result[i]
            if isinstance(member, bytes):
                member = member.decode()
            try:
                decayed[(zkey, conf_hash_key, member)] = float(result[i + 1])
            except (TypeError, ValueError):
                pass

    scores = {}
    for record in records:
        r_key = _get_key(record)
        zkey = key_for_record[r_key]
        conf_hash_key = conf_args_for_record[r_key][0]
        scores[r_key] = (
            decayed.get((zkey, conf_hash_key, r_key)) if zkey is not None else None
        )
    return scores


def _score_proxy_for_records(records, *, model_class, score_weights):
    """Partition-aware weighted score proxy for composite scores.

    Reads each record's per-field score from the *partition-specific* sorted
    set (see :func:`_partition_scores_for_field`), so partitioned agent-memory
    models get real per-record scores instead of the all-zero result the base
    key produced before #474. Non-partitioned models are unaffected:
    ``get_partitioned_sortedset_db_key`` returns the identical base key when
    ``partition_by`` is empty.

    Read-only and Valkey-safe (``ZSCORE`` / read-only ``EVAL``; no
    ``ZUNIONSTORE``, no temp keys). Only sorted-field-backed entries in
    ``score_weights`` contribute; other weights are ignored (ConfidenceField /
    WriteFilter are computed elsewhere in the quality signal).
    """
    if not records:
        return {}

    sorted_field_names = []
    for field_name in score_weights:
        try:
            f = model_class._meta.fields.get(field_name)
        except Exception:
            f = None
        if f is not None and isinstance(f, SortedFieldMixin):
            sorted_field_names.append(field_name)

    if not sorted_field_names:
        return {r_key: 0.0 for r_key in (_get_key(r) for r in records)}

    now = time.time()
    scores = {_get_key(r): 0.0 for r in records}
    for field_name in sorted_field_names:
        weight = float(score_weights.get(field_name, 0.0))
        field_scores = _partition_scores_for_field(
            records, model_class=model_class, field_name=field_name, now=now
        )
        for r_key, val in field_scores.items():
            if val is not None:
                scores[r_key] += weight * val
    return scores


def _compute_score_spread(records, *, model_class, score_weights):
    """Coefficient of variation of composite-score proxy across records.

    Returns ``(score_spread, distribution_list)``. Falls back to
    ``(0.0, [])`` on empty input or when ``abs(mean) < 1e-9``.
    """
    if not records:
        return 0.0, []
    try:
        proxy = _score_proxy_for_records(
            records, model_class=model_class, score_weights=score_weights
        )
    except Exception as e:
        logger.warning("_compute_score_spread proxy failed: %s", e)
        return 0.0, []
    scores = list(proxy.values())
    if not scores:
        return 0.0, []
    try:
        mean = statistics.fmean(scores)
    except statistics.StatisticsError:
        return 0.0, scores
    if abs(mean) < 1e-9:
        return 0.0, scores
    try:
        stddev = statistics.pstdev(scores)
    except statistics.StatisticsError:
        return 0.0, scores
    return stddev / mean, scores


def _avg_confidence(records, *, confidence_field_name, model_class):
    """Mean ConfidenceField.get_confidence() across records.

    Returns ``1.0`` (neutral) when the model has no ConfidenceField.
    Per-record exceptions log and fall back to ``initial_confidence``.
    """
    if not records or not confidence_field_name:
        return 1.0
    field_obj = model_class._meta.fields.get(confidence_field_name)
    initial = getattr(field_obj, "initial_confidence", 0.5)

    total = 0.0
    count = 0
    for record in records:
        try:
            val = ConfidenceField.get_confidence(record, confidence_field_name)
        except Exception as e:
            logger.warning(
                "get_confidence failed for %s: %s — using initial_confidence",
                _get_key(record),
                e,
            )
            val = initial
        total += float(val)
        count += 1
    if count == 0:
        return 1.0
    return total / count


def _compute_fok(
    query_cues,
    pull_candidates,
    *,
    model_class,
    score_weights,
    max_items,
    surfacing_threshold,
    existence_filter,
):
    """FOK score + per-cue breakdown.

    Mirrors the pre-refactor ``ContextAssembler._compute_fok`` exactly.
    Empty ``query_cues`` -> ``(0.0, {})`` with a warning.
    """
    if not query_cues:
        logger.warning("_compute_fok called with empty query_cues")
        return 0.0, {}

    n_candidates = len(pull_candidates)
    capped_max_items = max(max_items, 1)
    partial_retrieval = min(n_candidates, capped_max_items) / capped_max_items

    subthreshold_frac = 0.0
    if n_candidates > 0 and score_weights:
        try:
            proxy_scores = _score_proxy_for_records(
                pull_candidates,
                model_class=model_class,
                score_weights=score_weights,
            )
            below_threshold = sum(
                1 for s in proxy_scores.values() if 0 < s < surfacing_threshold
            )
            subthreshold_frac = below_threshold / max(n_candidates, 1)
        except Exception as e:
            logger.warning("Subthreshold activation computation failed: %s", e)
            subthreshold_frac = 0.0

    per_cue = {}
    total = 0.0
    for cue_value in query_cues.values():
        familiarity = _cue_familiarity(
            cue_value,
            existence_filter=existence_filter,
            model_class=model_class,
        )
        component_score = (
            FOK_WEIGHT_CUE_FAMILIARITY * familiarity
            + FOK_WEIGHT_PARTIAL_RETRIEVAL * partial_retrieval
            + FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION * subthreshold_frac
        )
        per_cue[str(cue_value)] = {
            "cue_familiarity": familiarity,
            "partial_retrieval_count": partial_retrieval,
            "subthreshold_activation": subthreshold_frac,
            "component_score": component_score,
        }
        total += component_score

    fok_score = total / len(query_cues) if query_cues else 0.0
    return fok_score, per_cue


def _staleness_ratio(
    records,
    *,
    model_class,
    score_weights,
    surfacing_threshold,
    decaying_sorted_field_name,
):
    """Fraction of records whose decayed DecayingSortedField relevance is below
    the surfacing threshold. ``0.0`` when the model has no DecayingSortedField
    or when that field is not in ``score_weights``.

    Reads the *partition-specific* decayed relevance via
    :func:`_partition_scores_for_field` (issue #474). Before the fix this read
    the non-partitioned base key -- ``None`` for every partitioned record --
    and reported ``1.0`` (all records "stale") regardless of freshness. It now
    compares each record's decayed relevance against ``surfacing_threshold``,
    so freshly-updated partitioned records are correctly not stale.
    """
    if not records or not decaying_sorted_field_name:
        return 0.0
    field_name = decaying_sorted_field_name
    if field_name not in score_weights:
        return 0.0
    try:
        field_scores = _partition_scores_for_field(
            records, model_class=model_class, field_name=field_name
        )
    except Exception as e:
        logger.warning("_staleness_ratio proxy failed: %s", e)
        return 0.0

    stale_count = 0
    for record in records:
        val = field_scores.get(_get_key(record))
        if val is None or val < surfacing_threshold:
            stale_count += 1
    return stale_count / len(records)


# ---------------------------------------------------------------------------
# Token Estimation (spike-1, plan: context_assembler_token_budget, issue #408)
# ---------------------------------------------------------------------------

# Experimental tuning constants for the default token estimator — NOT user
# config. Measured against tiktoken cl100k_base over the audit PoC corpus
# wrapped in the actual formatter envelope (spike-1, 2026-06-11). Resulting
# error per content type: english +20.3%, code +20.6%, cjk +4.5%,
# urls -15.0%, emoji -1.1% (overestimates are the safe direction for budget
# enforcement).
# Prose-like ASCII chars ([a-z] and whitespace) run ~5 chars per token.
LOW_CHARS_PER_TOKEN_WEIGHT = 0.2
# Digits/symbols/uppercase ASCII tokenize densely, ~1.2 chars per token.
OTHER_CHARS_PER_TOKEN_WEIGHT = 0.85
# Each \uXXXX escape (json.dumps ensure_ascii=True output) measures ~3.7
# cl100k tokens per 6-char escape sequence.
UESC_TOKEN_WEIGHT = 3.7
# Raw non-ASCII content (xml/natural formats do not escape) tokenizes at
# roughly 0.75 tokens per UTF-8 byte (BPE splits near byte boundaries).
NON_ASCII_BYTE_TOKEN_WEIGHT = 0.75

_UESC = re.compile(r"\\u[0-9a-fA-F]{4}")
_LOW = re.compile(r"[a-z\s]")


def _estimate_tokens(s: str) -> int:
    """Estimate the LLM token count of serialized record text (stdlib-only).

    Escape-aware character-class heuristic (spike-1): ``\\uXXXX`` escape
    sequences are counted as whole-escape units, remaining ASCII chars are
    split into prose-like vs dense classes, and raw non-ASCII chars are
    counted per UTF-8 byte. Each character contributes to exactly one term —
    non-ASCII chars counted in the byte term are excluded from the
    ``low``/``other`` ASCII counts.

    This is the default ``token_counter`` for :class:`ContextAssembler`. It
    receives the serialized per-record string (the exact slice the formatter
    emits), never the record object.
    """
    escapes = len(_UESC.findall(s))
    rest = _UESC.sub("", s)
    non_ascii_bytes = 0
    ascii_chars = []
    for c in rest:
        if ord(c) > 127:
            non_ascii_bytes += len(c.encode("utf-8"))
        else:
            ascii_chars.append(c)
    ascii_rest = "".join(ascii_chars)
    low = len(_LOW.findall(ascii_rest))
    other = len(ascii_rest) - low
    return round(
        LOW_CHARS_PER_TOKEN_WEIGHT * low
        + OTHER_CHARS_PER_TOKEN_WEIGHT * other
        + UESC_TOKEN_WEIGHT * escapes
        + NON_ASCII_BYTE_TOKEN_WEIGHT * non_ascii_bytes
    )


# ---------------------------------------------------------------------------
# Output Formatters
# ---------------------------------------------------------------------------


def _record_to_dict(record) -> dict:
    """Convert a model instance to a serializable dict."""
    if isinstance(record, dict):
        return record
    result = {}
    for name, f in record._meta.fields.items():
        try:
            val = getattr(record, name, None)
            if val is not None:
                # Ensure JSON-serializable
                json.dumps(val, default=str)
                result[name] = val
        except (TypeError, AttributeError):
            result[name] = str(val) if val is not None else None
    return result


def _resolve_content_text(record, content_field) -> str:
    """Return the memory text of ``record`` for the ``"content"`` format.

    Resolution order:

    1. ``content_field`` when given and the attribute holds a non-empty
       string (``ContentField`` lazily materializes to ``str`` on access,
       so filesystem-backed content works unchanged).
    2. A field literally named ``"content"``.
    3. The longest string value across the record's fields — a last-resort
       guess so an unconventional schema degrades to *something readable*
       rather than to an empty payload.

    Returns ``""`` when the record carries no string value at all. The
    caller (:func:`_compose_content`) drops empty slices, so a model with
    no text field yields an empty context block instead of raising.
    """
    if content_field:
        val = getattr(record, content_field, None)
        if isinstance(val, str) and val.strip():
            return val.strip()

    d = _record_to_dict(record)
    val = d.get("content")
    if isinstance(val, str) and val.strip():
        return val.strip()

    best = ""
    for v in d.values():
        if isinstance(v, str) and len(v.strip()) > len(best):
            best = v.strip()
    return best


def _serialize_record(record, output_format, content_field=None) -> str:
    """Serialize one record to the exact per-record slice the formatter emits.

    The compositional identity holds byte-for-byte per format::

        formatter(records) == wrapper_prefix
                              + sep.join(_serialize_record(r) for r in records)
                              + wrapper_suffix

    * ``"structured"`` (default): ``json.dumps(_record_to_dict(r),
      default=str, indent=2)`` re-indented to the JSON array's nesting level
      (two extra leading spaces per line).
    * ``"xml"``: the ``  <record>...</record>`` block including its two-space
      prefix.
    * ``"natural"``: the per-record ``key: value`` line WITHOUT the ``"N. "``
      enumeration prefix — numbering depends on final position after
      skip-not-break budget selection, so it is composition framing (applied
      by :func:`_compose_natural`), analogous to the wrapper framing of the
      other formats.
    * ``"content"``: the memory text alone, WITHOUT the ``"- "`` bullet
      prefix (framing, applied by :func:`_compose_content`). Carries no
      field names, no key values, and no scores — see
      :func:`_resolve_content_text` for how the text field is chosen and
      the ``"content"`` entry in the ``ContextAssembler`` ``output_format``
      docs for why that is the injection default.

    Wrapper framing (array brackets, ``<records>`` envelope, enumeration
    and bullet prefixes) is the only residual excluded from per-record token
    counting; it is a fixed handful of tokens per assembly, independent of
    record count or size.
    """
    if output_format == "content":
        return _resolve_content_text(record, content_field)
    d = _record_to_dict(record)
    if output_format == "xml":
        lines = ["  <record>"]
        for key, val in d.items():
            escaped = (
                str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            lines.append(f"    <{key}>{escaped}</{key}>")
        lines.append("  </record>")
        return "\n".join(lines)
    if output_format == "natural":
        return ", ".join(f"{k}: {v}" for k, v in d.items() if v is not None)
    # structured (default, and the fallback for unknown formats — must stay
    # consistent with the composition fallback in assemble())
    dumped = json.dumps(d, default=str, indent=2)
    return "\n".join("  " + line for line in dumped.split("\n"))


def _compose_structured(serialized) -> str:
    """Wrap pre-serialized structured record slices in the JSON array framing."""
    if not serialized:
        return "[]"
    return "[\n" + ",\n".join(serialized) + "\n]"


def _compose_xml(serialized) -> str:
    """Wrap pre-serialized xml record blocks in the ``<records>`` envelope."""
    if not serialized:
        return "<records>\n</records>"
    return "<records>\n" + "\n".join(serialized) + "\n</records>"


def _compose_natural(serialized) -> str:
    """Join pre-serialized natural lines, applying enumeration framing."""
    if not serialized:
        return ""
    return "\n".join(f"{i}. {s}" for i, s in enumerate(serialized, 1))


def _compose_content(serialized) -> str:
    """Join pre-serialized memory texts as a bullet list.

    Empty slices are dropped, so a record with no resolvable text
    contributes nothing rather than an orphan ``"- "`` bullet.
    """
    lines = [s for s in serialized if s]
    if not lines:
        return ""
    return "\n".join(f"- {s}" for s in lines)


def format_structured(records) -> str:
    """Format records as JSON array."""
    return _compose_structured([_serialize_record(r, "structured") for r in records])


def format_xml(records) -> str:
    """Format records as XML tags."""
    return _compose_xml([_serialize_record(r, "xml") for r in records])


def format_natural(records) -> str:
    """Format records as natural language summary."""
    return _compose_natural([_serialize_record(r, "natural") for r in records])


def format_content(records, content_field=None) -> str:
    """Format records as a content-only bullet list (no field names, no IDs).

    Args:
        records: Model instances to format.
        content_field: Name of the text field to read. ``None`` falls back
            to the resolution order in :func:`_resolve_content_text`.
    """
    return _compose_content(
        [_serialize_record(r, "content", content_field) for r in records]
    )


# ---------------------------------------------------------------------------
# ContextAssembler
# ---------------------------------------------------------------------------


class ContextAssembler:
    """Orchestrates memory retrieval into a single assemble() call.

    Combines pull-path (query-driven) and push-path (proactive via
    CyclicDecayField) retrieval, applies token budgets, and formats output
    for LLM context injection.

    The pull path supports two modes selected by ``retrieval_mode``:

    * ``"hybrid"`` — BM25 (lexical) + vector (semantic) signals fused via
      Reciprocal Rank Fusion (RRF, k=60) followed by optional CoOccurrence
      graph propagation. Requires ``BM25Field`` and ``EmbeddingField`` on the
      model.
    * ``"composite"`` — original CompositeScoreQuery weighted-sum (unchanged
      from pre-v1.7 behaviour). Requires ``score_weights``.
    * ``"auto"`` *(default)* — selects ``"hybrid"`` when both ``BM25Field``
      and ``EmbeddingField`` are detected on the model, ``"lexical"`` when
      only ``BM25Field`` is, otherwise falls back to ``"composite"``.

    .. warning::
       ``"auto"`` falling through to ``"composite"`` means retrieval is
       **query-blind**: ``query_cues`` are ignored and ranking comes from
       ``score_weights`` alone. That resolution logs a ``WARNING`` on the
       ``POPOTO.ContextAssembler`` logger naming the missing ``BM25Field``
       (issue #513). Declare a ``BM25Field`` (or import
       :class:`popoto.recipes.DefaultMemory`, which declares one) for
       query-sensitive retrieval, or pass ``retrieval_mode="composite"``
       explicitly to affirm the query-blind ranking and silence the warning.

    Args:
        model_class: Popoto Model class to query.
        score_weights: Dict mapping field names to weights for the composite
            pull path (e.g., ``{"relevance": 0.6, "confidence": 0.3}``).
            Ignored when the effective retrieval mode is ``"hybrid"``.
        max_items: Maximum records to return. Default 10.
        max_tokens: Optional enforced token budget over the serialized
            per-record output. Packing is greedy first-fit in rank order:
            a record that does not fit is skipped (not a packing
            terminator) and later smaller records may still be admitted.
            The first record is always admitted, so ``assemble()`` never
            returns zero records when candidates exist — a single
            oversized record can therefore overshoot the budget, and that
            overshoot is visible in ``metadata["token_count"]``. Wrapper
            framing (JSON array brackets, ``<records>`` envelope,
            enumeration prefixes) is excluded from counting; it is a fixed
            handful of tokens per assembly.
        surfacing_threshold: Minimum score for push-path records. Default 0.5.
        propagation_depth: BFS depth for CoOccurrence. Default 2.
        output_format: ``"structured"`` (JSON), ``"xml"``, ``"natural"``, or
            ``"content"``. Default ``"structured"``.

            ``"content"`` emits the memory text alone as a ``"- "`` bullet
            list: no field names, no ``memory_id``, no ``agent_id``, no raw
            epoch scores. It exists because the structured default carried
            ~2.8x the characters of the content it wrapped when injected
            into a system prompt (issue #513), and the identifiers it spent
            them on are not answerable by the model.
            :class:`~popoto.recipes.subconscious_memory.SubconsciousMemory`
            defaults to ``"content"``; ``ContextAssembler`` keeps
            ``"structured"`` so existing callers are unaffected.
        content_field: Name of the text field ``output_format="content"``
            reads. Default ``None`` — auto-detected from the model's
            ``BM25Field`` source, else a field named ``"content"``. Ignored
            by every other output format.
        token_counter: Optional ``callable(serialized_text: str) -> int``.
            Receives the exact serialized per-record string the formatter
            emits for the active ``output_format`` (never the record
            object) and must return a non-negative ``int``. Counters that
            raise, or return anything else, fall back to the stdlib
            estimator for that record (with a diagnostic warning).
            Old-contract ``callable(record)`` counters trigger a
            ``DeprecationWarning`` at construction. Default: a stdlib
            character-class heuristic over the serialized text
            (``_estimate_tokens``).
        retrieval_mode: ``"auto"`` (default), ``"hybrid"``, or
            ``"composite"``. See class docstring for semantics.
        confidence_gate_threshold: Optional confidence gate. When not
            ``None``, ``assemble()`` reads the rank-0 pull-path candidate's
            ``ConfidenceField`` value and compares it to this threshold. If
            the value is below the threshold the gate is "gated": in
            ``"refuse"`` mode all pull-path records are dropped before
            injection; in ``"flag"`` mode records are retained and only
            ``AssemblyResult.metadata["gate"]`` reports the decision. Requires
            the model to declare a ``ConfidenceField``. Default ``None``
            (gate disabled; no shipped default — see
            ``EXPERIMENTAL_CONFIDENCE_GATE_THRESHOLD`` and issue #463).
        confidence_gate_mode: ``"refuse"`` (default) or ``"flag"``. Only
            meaningful when ``confidence_gate_threshold`` is not ``None``.
        graph_traversal_relationship_fields: Optional list of
            self-referential ``Relationship`` field name(s) on
            ``model_class`` (i.e. the field's ``model`` is ``model_class``
            itself). When set, the existing CoOccurrence graph arm (both
            pull-path modes) is extended via
            :func:`popoto.recipes.graph_traversal.traverse` to also expand
            1-2 hops across those Relationship edges, with hop admission
            modulated by the model's ``ConfidenceField``/decaying-field
            state when present. Default ``None`` (disabled; behavior is
            identical to before #462). Entries that are not a valid
            self-referential ``Relationship`` field are ignored with a
            warning rather than raising. See issue #462.

    Raises:
        QueryException: If ``retrieval_mode="hybrid"`` is requested but the
            model lacks ``BM25Field`` or ``EmbeddingField``; if
            ``confidence_gate_threshold`` is set and ``confidence_gate_mode``
            is not one of ``{"refuse", "flag"}``; or if
            ``confidence_gate_threshold`` is set but the model lacks a
            ``ConfidenceField``.
    """

    def __init__(
        self,
        model_class,
        score_weights,
        max_items=DEFAULT_MAX_ITEMS,
        max_tokens=None,
        surfacing_threshold=DEFAULT_SURFACING_THRESHOLD,
        propagation_depth=DEFAULT_PROPAGATION_DEPTH,
        output_format="structured",
        token_counter=None,
        *,
        retrieval_mode: str = "auto",
        confidence_gate_threshold: float | None = None,
        confidence_gate_mode: str = "refuse",
        graph_traversal_relationship_fields: list | None = None,
        content_field: str | None = None,
    ):
        self.model_class = model_class
        self.score_weights = score_weights
        self.max_items = max_items
        self.max_tokens = max_tokens
        self.surfacing_threshold = surfacing_threshold
        self.propagation_depth = propagation_depth
        self.output_format = output_format
        self.confidence_gate_threshold = confidence_gate_threshold
        self.confidence_gate_mode = confidence_gate_mode
        self._graph_traversal_relationship_fields = (
            list(graph_traversal_relationship_fields)
            if graph_traversal_relationship_fields
            else None
        )
        if token_counter is None:
            self._token_counter = _estimate_tokens
        else:
            # Construction-time contract probe: token_counter receives the
            # serialized record STRING, not the record object. Old-contract
            # callable(record) counters typically raise TypeError or
            # AttributeError when handed a str — surface that loudly here
            # instead of silently degrading per-call in production.
            try:
                token_counter("popoto token_counter contract probe")
            except (TypeError, AttributeError):
                warnings.warn(
                    "token_counter now receives the serialized record string "
                    "(str), not the record object — update your counter",
                    DeprecationWarning,
                    stacklevel=2,
                )
            except Exception:
                # Other probe failures are not contract signals; per-call
                # validation in _count_record_tokens handles them.
                pass
            self._token_counter = token_counter

        # Detect field capabilities on model
        self._existence_filter = None
        self._co_occurrence_field = None
        self._co_occurrence_field_name = None
        self._cyclic_decay_field_name = None
        self._confidence_field_name = None
        self._decaying_sorted_field_name = None
        self._bm25_field = None
        self._bm25_field_name = None
        self._embedding_field = None
        self._embedding_field_name = None
        self._tag_field_name = None
        self._validity_field_name = None
        # Point-in-time epoch for the *current* assemble() call (issue #580).
        # Parked here rather than threaded through _pull_path / _push_path
        # because those seams are monkeypatched by existing callers with
        # two-argument stand-ins. Unconditionally (re)set at the top of every
        # assemble() and cleared before its return, so a value can never leak
        # into a later call even if one raises partway through.
        self._assembly_as_of: float | None = None
        self._exclude_headroom: int = 0

        for name, f in model_class._meta.fields.items():
            if isinstance(f, ExistenceFilter) and self._existence_filter is None:
                self._existence_filter = f
            if isinstance(f, CoOccurrenceField) and self._co_occurrence_field is None:
                self._co_occurrence_field = f
                self._co_occurrence_field_name = name
            if (
                isinstance(f, CyclicDecayField)
                and self._cyclic_decay_field_name is None
            ):
                self._cyclic_decay_field_name = name
            if isinstance(f, ConfidenceField) and self._confidence_field_name is None:
                self._confidence_field_name = name
            if (
                isinstance(f, DecayingSortedField)
                and self._decaying_sorted_field_name is None
            ):
                self._decaying_sorted_field_name = name
            if isinstance(f, BM25Field) and self._bm25_field is None:
                self._bm25_field = f
                self._bm25_field_name = name
            if isinstance(f, EmbeddingField) and self._embedding_field is None:
                self._embedding_field = f
                self._embedding_field_name = name
            if isinstance(f, TagFieldMixin) and self._tag_field_name is None:
                self._tag_field_name = name
            if isinstance(f, ValidityField) and self._validity_field_name is None:
                self._validity_field_name = name

        # Text field read by output_format="content". Explicit wins; then the
        # BM25Field's source (the field the model already declares as its
        # searchable text); then a field literally named "content". Left as
        # None when nothing matches — _resolve_content_text() then falls back
        # to the longest string value per record.
        self._content_field_name = content_field
        if self._content_field_name is None:
            if self._bm25_field is not None and getattr(
                self._bm25_field, "source", None
            ):
                self._content_field_name = self._bm25_field.source
            elif "content" in model_class._meta.fields:
                self._content_field_name = "content"

        # Confidence-gate construction-time validation (issue #463). The gate
        # is opt-in — no-op unless confidence_gate_threshold is set — but
        # once enabled it must be self-consistent (valid mode) and backed by
        # a ConfidenceField on the model, mirroring the hybrid-mode
        # validation block below.
        _VALID_GATE_MODES = {"refuse", "flag"}
        if self.confidence_gate_threshold is not None:
            if self.confidence_gate_mode not in _VALID_GATE_MODES:
                from ..exceptions import QueryException

                raise QueryException(
                    f"confidence_gate_mode={self.confidence_gate_mode!r} is not "
                    f"a recognised mode. Allowed values: "
                    f"{sorted(_VALID_GATE_MODES)}"
                )
            if self._confidence_field_name is None:
                from ..exceptions import QueryException

                raise QueryException(
                    f"confidence_gate_threshold requires ConfidenceField on "
                    f"{model_class.__name__}"
                )

        # Graph-traversal relationship expansion (issue #462) — opt-in,
        # off-by-default. Validated here (warn-and-disable, not raise) so a
        # misconfigured field name degrades to the pre-existing
        # CoOccurrence-only graph arm instead of breaking assemble().
        if self._graph_traversal_relationship_fields:
            from ..fields.relationship import Relationship

            valid_names = []
            for name in self._graph_traversal_relationship_fields:
                f = model_class._meta.fields.get(name)
                if isinstance(f, Relationship) and f.model is model_class:
                    valid_names.append(name)
                else:
                    logger.warning(
                        "ContextAssembler: graph_traversal_relationship_fields "
                        "entry %r is not a self-referential Relationship field "
                        "on %s — ignored",
                        name,
                        model_class.__name__,
                    )
            self._graph_traversal_relationship_fields = valid_names or None

        # Resolve effective retrieval mode.
        #
        # Emergent-mode behavior: under "auto", declaring or removing a
        # BM25Field or EmbeddingField on the model changes the retrieval mode
        # without any code change at the call site.
        #   - BM25Field + EmbeddingField  → "hybrid"
        #   - BM25Field only              → "lexical"  (query-sensitive via BM25
        #                                               + graph propagation, no
        #                                               embeddings)
        #   - neither                     → "composite" (query-blind)
        # Adding an EmbeddingField to a BM25-only model flips lexical → hybrid
        # automatically.
        _VALID_MODES = {"auto", "lexical", "hybrid", "composite"}
        if retrieval_mode not in _VALID_MODES:
            from ..exceptions import QueryException

            raise QueryException(
                f"retrieval_mode={retrieval_mode!r} is not a recognised mode. "
                f"Allowed values: {sorted(_VALID_MODES)}"
            )

        if retrieval_mode == "auto":
            if self._bm25_field is not None and self._embedding_field is not None:
                self._effective_mode = "hybrid"
            elif self._bm25_field is not None:
                # BM25-only: query-sensitive lexical retrieval (BM25 + graph, no embeddings)
                self._effective_mode = "lexical"
            else:
                # No BM25 and no embedding → composite (unchanged, incl. embedding-only)
                self._effective_mode = "composite"
                # Issue #513: this resolution used to be silent at every log
                # level, so a model that looked fine ranked the correct memory
                # below unrelated ones with no signal that query text was
                # being ignored. Warn only for the *implicit* path — an
                # explicit retrieval_mode="composite" is an informed choice
                # and stays quiet (it is also the documented way to silence
                # this).
                logger.warning(
                    "ContextAssembler: retrieval_mode='auto' resolved to "
                    "'composite' (QUERY-BLIND) because %s declares no "
                    "BM25Field%s. Query cues passed to assemble() are "
                    "IGNORED; records are ranked by score_weights alone. "
                    "Add a BM25Field to %s for query-sensitive retrieval, "
                    'e.g. content_bm25 = BM25Field(source="content"), or '
                    "import the batteries-included model: "
                    "from popoto.recipes import DefaultMemory. "
                    "Pass retrieval_mode='composite' to silence this "
                    "warning if query-blind ranking is intended. "
                    "See https://popoto.io/features/context-assembler/"
                    "#pull-path-modes-retrieval_mode",
                    model_class.__name__,
                    (
                        " (an EmbeddingField alone does not enable "
                        "query-sensitive retrieval through ContextAssembler)"
                        if self._embedding_field is not None
                        else ""
                    ),
                    model_class.__name__,
                )
        elif retrieval_mode == "hybrid":
            if self._bm25_field is None or self._embedding_field is None:
                from ..exceptions import QueryException

                missing = []
                if self._bm25_field is None:
                    missing.append("BM25Field")
                if self._embedding_field is None:
                    missing.append("EmbeddingField")
                raise QueryException(
                    f"retrieval_mode='hybrid' requires {' and '.join(missing)} "
                    f"on {model_class.__name__}"
                )
            self._effective_mode = "hybrid"
        elif retrieval_mode == "lexical":
            if self._bm25_field is None:
                from ..exceptions import QueryException

                raise QueryException(
                    f"retrieval_mode='lexical' requires BM25Field "
                    f"on {model_class.__name__}"
                )
            self._effective_mode = "lexical"
        else:
            self._effective_mode = "composite"

        if self._effective_mode == "hybrid" and score_weights:
            logger.debug(
                "ContextAssembler: retrieval_mode resolved to 'hybrid'; "
                "score_weights are ignored for the pull path"
            )

    def _resolve_tag_keys(self, tags, tag_match):
        """Resolve optional tag constraints to the set of allowed Redis keys.

        Auto-detects a TagField on the model (``self._tag_field_name``) and, when
        ``tags`` is provided and the deploy kill switch
        ``Defaults.TAG_SCOPING_ENABLED`` is on, evaluates the tag membership Sets
        directly (``__all`` → SINTER, ``__any`` → SUNION) and returns the matching
        instance keys as a ``set[str]``. This set is used to *post-filter* retrieved
        candidates across every mode, so scoping is uniform whether the pull path
        ran composite, hybrid, or lexical (``composite_score`` only honors a
        sorted-field partition, not an arbitrary field filter, so a candidate-set
        intersection is the only mode-agnostic seam).

        Returns ``None`` — meaning "no scoping, behavior identical to today" — when
        ``tags`` is empty, the model has no TagField, or the kill switch is off.
        Tag scoping is cooperative, not a security boundary: an unusable request
        degrades to unscoped retrieval rather than raising.
        """
        if not tags or self._tag_field_name is None:
            return None
        if not Defaults.TAG_SCOPING_ENABLED:
            return None
        lookup = "any" if tag_match == "any" else "all"
        param = f"{self._tag_field_name}__{lookup}"
        field_cls = self.model_class._meta.fields[self._tag_field_name].__class__
        matched = field_cls.filter_query(
            self.model_class, self._tag_field_name, **{param: list(tags)}
        )
        return {k.decode() if isinstance(k, bytes) else k for k in matched}

    @staticmethod
    def _scope_by_tags(records, allowed_keys):
        """Keep only records whose Redis key is in ``allowed_keys``.

        No-op passthrough when ``allowed_keys`` is None (no tag scoping requested).
        """
        if allowed_keys is None:
            return records
        return [r for r in records if _get_key(r) in allowed_keys]

    def _resolve_excluded_keys(self, as_of: float | None = None) -> set[str] | None:
        """Resolve validity gating to the set of Redis keys to DROP.

        Layer 3 of validity gating (issue #580, plan D6), and the only layer
        that reaches the ``fuse`` / BM25 / graph-propagation arms: those bypass
        the ``filters`` dict entirely, so neither the decay-Lua gate (layer 1,
        inside ``DECAY_SCORE_LUA``) nor the composite difference (layer 2,
        inside ``composite_score``) can see them. Exactly the shape tag scoping
        already established — a candidate-key post-filter is the only
        mode-agnostic seam.

        THIS RETURNS AN EXCLUSION SET, NOT A WHITELIST, and that is deliberate.
        All three gating layers must agree on the same rule: drop a record only
        when it is provably closed (``invalid_at <= as_of``) or provably not yet
        started (``valid_from > as_of``). A record with **no entry in either
        interval ZSET** is *unmanaged* and stays fully retrievable. Resolving
        the valid set instead — e.g. via ``ValidityField.resolve_valid_keys``,
        which intersects the two ranges — would silently hide every record that
        predates the day a ``ValidityField`` was added to an existing model,
        since none of those has an interval until it is next saved. That is a
        data-visibility regression, not a stricter gate. Do not "simplify" this
        into a valid-key intersection.

        Two read-only ``ZRANGEBYSCORE``s, run once per ``assemble()`` call.

        ``Defaults.VALIDITY_GATING_ENABLED`` is read **here, at call time**, not
        captured at import: the deploy-level kill switch has to take effect at
        runtime for adopters who cannot edit model code.

        Args:
            as_of: Epoch seconds to evaluate membership at. ``None`` = now.

        Returns:
            ``set[str]`` of record keys to drop, or ``None`` meaning "no
            scoping, behavior byte-identical to pre-#580" — returned when the
            model declares no ``ValidityField`` (which is every model that has
            not opted in) or when the kill switch is off. An empty set is NOT
            the same as ``None``; it means gating ran and excluded nothing.

        Note:
            This is a point-in-time snapshot of a live store. A supersession
            landing mid-``assemble()`` is not reflected until the next call —
            the same accepted property tag scoping has (plan Race 3).
        """
        if self._validity_field_name is None:
            return None
        if not Defaults.VALIDITY_GATING_ENABLED:
            return None

        t = time.time() if as_of is None else float(as_of)
        valid_from_key, invalid_at_key = ValidityField.get_interval_keys(
            self.model_class, self._validity_field_name
        )
        # invalid_at <= t: already closed. The +inf open sentinel never matches.
        closed = POPOTO_REDIS_DB.zrangebyscore(invalid_at_key, "-inf", t)
        # valid_from > t: not yet started.
        future = POPOTO_REDIS_DB.zrangebyscore(valid_from_key, f"({t}", "+inf")
        # redis-py types every command ``Awaitable[T] | T`` for both the sync
        # and async clients, so mypy cannot see that these are concrete lists
        # on the sync client we actually use. Same family as the existing
        # narrowing casts elsewhere in this module; see CLAUDE.md's note that
        # this error class is redis-py-version-dependent.
        closed_list = cast("list[Any]", closed)
        future_list = cast("list[Any]", future)
        return {
            k.decode() if isinstance(k, bytes) else k for k in closed_list + future_list
        }

    def _fetch_limit(self, base: int) -> int:
        """Widen a candidate fetch to make room for suppressed records.

        Every arm fetches a bounded candidate set (``max_items * 2``, or
        ``max_items * HYBRID_CANDIDATE_MULTIPLIER``) and the exclusion
        post-filter runs *after* that fetch. Without headroom, an exclusion
        set as large as the fetch consumes the entire candidate list and the
        arm returns nothing — so per-session suppression would turn "do not
        repeat a memory" into "go silent after two turns", which is the
        opposite of the intent.

        Adding the exclusion count restores the invariant that the arm can
        still surface ``base`` unsuppressed candidates. The headroom is capped
        so a long session cannot grow an unbounded ZRANGE: past
        ``EXCLUDE_HEADROOM_CAP`` the store is better served by decay than by
        an ever-wider scan, and the arm degrading to fewer results is the
        correct outcome when nearly everything relevant has already been said.
        """
        headroom = min(self._exclude_headroom, EXCLUDE_HEADROOM_CAP)
        return base + headroom

    @staticmethod
    def _scope_by_validity(
        records: list[Any], excluded_keys: set[str] | None
    ) -> list[Any]:
        """Drop records whose validity interval provably does not cover the as-of.

        No-op passthrough when ``excluded_keys`` is None (gating disabled, or the
        model has no ``ValidityField``). Records absent from ``excluded_keys``
        are kept — including unmanaged records with no interval at all, which is
        the whole point of taking an exclusion set rather than a whitelist. See
        :meth:`_resolve_excluded_keys`.
        """
        if excluded_keys is None:
            return records
        return [r for r in records if _get_key(r) not in excluded_keys]

    def assemble(
        self,
        query_cues=None,
        agent_id=None,
        partition_filters=None,
        assess_quality=False,
        emit_trace=False,
        tags=None,
        tag_match="any",
        *,
        as_of=None,
        exclude_keys=None,
    ):
        """Execute the full retrieval pipeline.

        Args:
            query_cues: Optional dict of query cues (e.g., {"topic": "deploy"}).
                If None, pull path is skipped.
            agent_id: Optional agent ID for partition filtering. Added to
                partition_filters as {"agent_id": agent_id}.
            partition_filters: Optional dict of partition key-value pairs
                for filtering queries.
            assess_quality: When True, compute a ``RetrievalQuality`` over
                the selected records and attach it to
                ``AssemblyResult.metadata["quality"]``. Default False;
                when False the result shape is bit-for-bit identical to
                the pre-metacognitive-layer behavior. Turning this on adds
                bounded overhead (one ``might_exist`` per cue plus one
                ``get_confidence`` per selected record).
            emit_trace: When True, attach ``metadata["trace"]`` — a list of
                ``{"key", "rank", "score", "source"}`` dicts describing the
                selected (injected) records in final rank order. ``score``
                is the injection-time composite/fused score proxy, captured
                *before* post-retrieval effects mutate decay clocks;
                ``source`` is ``"pull"`` or ``"push"``. Default False; when
                False the result is bit-for-bit identical to the
                pre-telemetry behavior. This is the instrumentation hook the
                telemetry recipe (issue #464) consumes — it uses only the
                read-only, pipelined score proxy (ZSCORE), so it is
                Valkey-safe and adds bounded overhead.

            tags: Optional list of tag values to scope retrieval (issue #492).
                Requires a TagField on the model (auto-detected). Applied as a
                pre-filter across ALL retrieval modes via the shared filter dict.
                When None/empty, or when the model has no TagField, or when
                ``Defaults.TAG_SCOPING_ENABLED`` is False, retrieval is
                byte-identical to the pre-#492 behavior. Tag scoping is
                cooperative, not a security boundary.
            tag_match: How to combine multiple ``tags`` — ``"all"`` (default,
                AND / SINTER: records carrying every listed tag) or ``"any"``
                (OR / SUNION: records carrying at least one). Ignored when
                ``tags`` is empty.
            as_of: Keyword-only. Epoch seconds for point-in-time retrieval
                (issue #580): ``assemble(as_of=t)`` returns what the agent
                believed at ``t``, superseded records included, while the
                default ``None`` means "now" and returns only currently-valid
                records. Requires a ``ValidityField`` on the model
                (auto-detected); a no-op passthrough without one, or when
                ``Defaults.VALIDITY_GATING_ENABLED`` is False. Validity is
                never expressed as a filter kwarg — a surviving filter param
                would disable sorted-range limit pushdown — so it is applied
                server-side in the decay/composite layers and as a
                candidate-key post-filter here.
            exclude_keys: Keyword-only. Iterable of record keys to drop from
                every retrieval arm, unioned with any validity exclusions.
                ``None`` or empty leaves behavior byte-identical.

                This exists for **per-session injection suppression**, the
                append-only way to bound what a subconscious memory layer
                costs against a provider's prompt cache. Injected context is
                appended to the model's prompt and stays resident for the rest
                of the session, so re-retrieving the same top-k every turn --
                which is what topically similar consecutive prompts produce --
                makes cumulative cache-read grow with the square of turn count.
                Passing the keys already injected this session declines to add
                them again. That is strictly append-only, so it costs nothing
                in cache terms, and it changes growth from quadratic in turns
                to linear in *distinct* memories, bounded by store size.

                Do not reach for the opposite fix. Pruning an already-injected
                block from the prompt is a mutation of sealed history, which
                invalidates every token behind it and costs more than leaving
                it in place.

                Suppression is not deletion: excluded records stay in the
                store, stay retrievable on a later call that does not exclude
                them, and are not marked as decayed, dismissed, or superseded.
                They are also dropped from the proactive/push arm, so a
                suppressed memory cannot re-enter by that route. Because
                filtering happens before selection, the budget backfills with
                the next-best candidates rather than returning short.

                See the ``exclude_keys`` discussion in the Prompt Cache
                Efficiency guide.

        Returns:
            AssemblyResult with records, proactive, formatted, and metadata.
        """
        t0 = time.time()

        # Build partition filters
        filters = dict(partition_filters or {})
        if agent_id is not None:
            filters["agent_id"] = agent_id

        # Optional tag scoping (issue #492): resolve the allowed-key set once and
        # post-filter retrieved candidates across every mode. ``None`` means no
        # scoping — every code path below is then byte-identical to pre-#492.
        allowed_tag_keys = self._resolve_tag_keys(tags, tag_match)

        # Validity gating (issue #580, plan D6 / layer 3). Snapshot the key set
        # once and post-filter every arm — the fuse/BM25/graph arms bypass
        # the filters dict, so the server-side layers cannot reach them. This is
        # an EXCLUSION set, matching layers 1 and 2: unmanaged records (no
        # interval at all) are kept. ``None`` means no gating; every path below
        # is then byte-identical to pre-#580.
        excluded_valid_keys = self._resolve_excluded_keys(as_of)

        # Caller-supplied suppression (per-session injection dedup) rides the
        # same exclusion seam. Union rather than replace: a record that is
        # invalid at ``as_of`` must stay dropped whether or not the caller
        # named it. ``None`` on both sides stays ``None`` so every arm below is
        # byte-identical to the pre-exclude_keys behavior — an empty set is
        # NOT the same as None here, exactly as in _resolve_excluded_keys.
        if exclude_keys:
            caller_excluded = {str(k) for k in exclude_keys}
            excluded_valid_keys = (
                caller_excluded
                if excluded_valid_keys is None
                else excluded_valid_keys | caller_excluded
            )

        self._assembly_as_of = as_of
        self._exclude_headroom = len(exclude_keys) if exclude_keys else 0

        pull_records = []
        push_records = []
        all_pull_candidates = []  # For competitive suppression

        # --- Pull path ---
        if query_cues:
            pull_records, all_pull_candidates = self._pull_path(query_cues, filters)
            pull_records = self._scope_by_tags(pull_records, allowed_tag_keys)
            pull_records = self._scope_by_validity(pull_records, excluded_valid_keys)
            all_pull_candidates = self._scope_by_tags(
                all_pull_candidates, allowed_tag_keys
            )
            all_pull_candidates = self._scope_by_validity(
                all_pull_candidates, excluded_valid_keys
            )

        # [CONFIDENCE GATE] Opt-in, off-by-default (issue #463). Reads the
        # rank-0 pull-path candidate's ConfidenceField value and compares it
        # to confidence_gate_threshold. Kept as a small, self-contained,
        # additive block that does not touch RRF/fusion logic — it is
        # mode-agnostic because ConfidenceField.get_confidence() always
        # returns a value in [0, 1] regardless of the underlying ranking
        # score scale (composite weighted-sum vs RRF-fused). suppression_candidates
        # starts as an alias of all_pull_candidates and is only ever
        # replaced (never mutated in place), so all_pull_candidates itself is
        # never touched here.
        suppression_candidates = all_pull_candidates
        gate_meta = None
        if self.confidence_gate_threshold is not None:
            if not pull_records:
                gate_meta = {
                    "applied": False,
                    "gate_score": None,
                    "threshold": self.confidence_gate_threshold,
                    "mode": self.confidence_gate_mode,
                    "gated": False,
                }
            else:
                try:
                    gate_score = float(
                        ConfidenceField.get_confidence(
                            pull_records[0], self._confidence_field_name
                        )
                    )
                except Exception as e:
                    # Fault-tolerant, matching the style of emit_trace /
                    # _compute_quality elsewhere in this method: a
                    # get_confidence() failure degrades to "gate not
                    # applied" rather than crashing assemble().
                    logger.warning("confidence gate get_confidence failed: %s", e)
                    gate_score = None

                if gate_score is None:
                    gate_meta = {
                        "applied": False,
                        "gate_score": None,
                        "threshold": self.confidence_gate_threshold,
                        "mode": self.confidence_gate_mode,
                        "gated": False,
                    }
                else:
                    gated = gate_score < self.confidence_gate_threshold
                    if gated and self.confidence_gate_mode == "refuse":
                        pull_records = []
                        # Don't punish other candidates in competitive
                        # suppression for a refusal that already happened:
                        # zero only the copy fed to _post_effects.
                        # all_pull_candidates itself is left untouched so
                        # _compute_quality's feeling-of-knowing (FoK) score
                        # still reflects that candidates WERE found, even
                        # though the assembler chose not to inject them.
                        suppression_candidates = []
                    gate_meta = {
                        "applied": True,
                        "gate_score": gate_score,
                        "threshold": self.confidence_gate_threshold,
                        "mode": self.confidence_gate_mode,
                        "gated": gated,
                    }

        # --- Push path ---
        if self._cyclic_decay_field_name is not None:
            push_records = self._push_path(filters)
            push_records = self._scope_by_tags(push_records, allowed_tag_keys)
            push_records = self._scope_by_validity(push_records, excluded_valid_keys)

        # --- Merge + deduplicate ---
        seen_keys = set()
        merged = []
        pull_keys = set()
        push_keys = set()

        for record in pull_records:
            rk = _get_key(record)
            if rk not in seen_keys:
                seen_keys.add(rk)
                merged.append(record)
                pull_keys.add(rk)

        for record in push_records:
            rk = _get_key(record)
            if rk not in seen_keys:
                seen_keys.add(rk)
                merged.append(record)
                push_keys.add(rk)

        # --- Budget selection ---
        # max_items cap
        selected = merged[: self.max_items]

        # max_tokens cap — greedy first-fit in rank order (skip, not break):
        # a record that does not fit is skipped and the loop continues, so
        # later smaller records may still be admitted. The first record is
        # always admitted (never-zero-records guarantee); a single oversized
        # record can overshoot the budget, visibly in metadata["token_count"].
        # The serialized strings captured here at count time are the exact
        # strings the formatter composes below — counting can never diverge
        # from emission, even if _post_effects mutates record state.
        total_tokens = 0
        selected_serialized = []
        if self.max_tokens is not None:
            budget_selected = []
            for record in selected:
                tokens, serialized = self._count_record_tokens(record)
                if budget_selected and total_tokens + tokens > self.max_tokens:
                    continue
                total_tokens += tokens
                budget_selected.append(record)
                selected_serialized.append(serialized)
            selected = budget_selected
        else:
            for record in selected:
                tokens, serialized = self._count_record_tokens(record)
                total_tokens += tokens
                selected_serialized.append(serialized)

        # Identify proactive records in final selection
        proactive = [r for r in selected if _get_key(r) in push_keys]

        # [TELEMETRY] Capture the injection trace BEFORE post-effects mutate
        # decay clocks, so the recorded score reflects the score the record
        # was actually ranked/injected on. Off-by-default (issue #464): when
        # emit_trace is False this block does no Redis work and the result is
        # bit-for-bit identical to the pre-telemetry behavior.
        trace = None
        if emit_trace:
            try:
                proxy = self._injection_scores(selected)
            except Exception as e:
                logger.warning("emit_trace score proxy failed: %s", e)
                proxy = {}
            trace = []
            for rank, record in enumerate(selected):
                rk = _get_key(record)
                trace.append(
                    {
                        "key": rk,
                        "rank": rank,
                        "score": proxy.get(rk, 0.0),
                        "source": "pull" if rk in pull_keys else "push",
                    }
                )

        # --- Post-retrieval effects ---
        # suppression_candidates (not all_pull_candidates) so a "refuse"
        # gate decision does not competitively suppress candidates for an
        # answer that was never injected.
        self._post_effects(
            selected, pull_keys, push_keys, suppression_candidates, agent_id
        )

        # --- Format ---
        # Compose from the count-time serialized strings (NOT a re-serialize
        # after _post_effects): what was counted is byte-for-byte what is
        # emitted, plus fixed wrapper framing.
        compose = {
            "structured": _compose_structured,
            "xml": _compose_xml,
            "natural": _compose_natural,
            "content": _compose_content,
        }.get(self.output_format, _compose_structured)

        formatted = compose(selected_serialized)

        timing_ms = round((time.time() - t0) * 1000, 2)

        metadata = {
            "pull_count": len([r for r in selected if _get_key(r) in pull_keys]),
            "push_count": len(proactive),
            "token_count": total_tokens,
            "timing_ms": timing_ms,
            "total_candidates": len(merged),
        }

        # [TELEMETRY] Attach the injection trace captured pre-post-effects.
        # Off-by-default so existing callers see identical metadata.
        if trace is not None:
            metadata["trace"] = trace

        # [CONFIDENCE GATE] Attach only when configured — callers who pass
        # no threshold get bit-for-bit identical metadata to pre-change
        # behavior (no "gate" key at all).
        if gate_meta is not None:
            metadata["gate"] = gate_meta

        # [METACOGNITIVE] Quality assessment — opt-in, off-by-default so existing
        # callers see bit-for-bit identical metadata.
        if assess_quality:
            try:
                metadata["quality"] = self._compute_quality(
                    selected=selected,
                    all_pull_candidates=all_pull_candidates,
                    query_cues=query_cues or {},
                )
            except Exception as e:
                logger.warning("_compute_quality failed: %s", e)
                metadata["quality"] = RetrievalQuality()

        self._assembly_as_of = None
        self._exclude_headroom = 0
        return AssemblyResult(
            records=selected,
            proactive=proactive,
            formatted=formatted,
            metadata=metadata,
        )

    def _injection_scores(self, records):
        """Composite-score proxy for the telemetry trace (#464).

        Reconciled onto the shared metacognitive
        :func:`_score_proxy_for_records` (#474), so the trace and the
        metacognitive layer share one partition- **and** decay-aware
        implementation -- eliminating the drift that produced #474. Scores are
        read from the *partition-specific* sorted set, and
        ``DecayingSortedField`` / ``CyclicDecayField`` timestamps are decayed
        into relevance (previously the trace emitted raw ~1.7e9 timestamps for
        decaying fields). Read-only and Valkey-safe.

        Only sorted-field-backed entries in ``score_weights`` contribute
        (ConfidenceField / WriteFilter scores are not persisted in a ZSET). In
        hybrid/lexical retrieval the fused RRF score is not persisted either, so
        the trace score reflects only ``score_weights`` sorted fields (0.0 when
        none) -- per-arm score decomposition is the documented fast-follow.

        Returns:
            ``{record_key: weighted_score}`` for every record in ``records``.
        """
        if not records or not self.score_weights:
            return {_get_key(r): 0.0 for r in records}
        return _score_proxy_for_records(
            records,
            model_class=self.model_class,
            score_weights=self.score_weights,
        )

    def _count_record_tokens(self, record):
        """Serialize ``record`` for the active output format and count tokens.

        Single counting path used by both the budgeted and unbudgeted
        branches of budget selection. The configured ``token_counter``
        receives the serialized string; its return value is validated (a
        non-negative ``int``, not a ``bool``). Any exception or invalid
        return falls back to :func:`_estimate_tokens` over the same string,
        with a diagnostic warning.

        Returns:
            Tuple ``(tokens, serialized)`` — the token count and the exact
            per-record string the formatter will emit for this record.
        """
        serialized = _serialize_record(
            record, self.output_format, self._content_field_name
        )
        try:
            tokens = self._token_counter(serialized)
            if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
                raise TypeError(
                    f"token_counter returned {tokens!r}; expected a non-negative int"
                )
        except Exception as e:
            logger.warning(
                "token_counter raised %s on serialized text (first 80 chars: "
                "%r); falling back to _estimate_tokens. Contract: "
                "callable(str) -> int.",
                type(e).__name__,
                serialized[:80],
            )
            tokens = _estimate_tokens(serialized)
        return tokens, serialized

    def _pull_path(self, query_cues, filters):
        """Dispatch pull-path retrieval based on ``self._effective_mode``.

        The point-in-time epoch is read off ``self._assembly_as_of`` (parked by
        ``assemble()`` for the duration of one call) rather than passed as a
        parameter: these internal seams are monkeypatched by existing tests with
        two-argument stand-ins, so widening their signatures would break
        callers this task does not own. It is threaded down to the
        ``composite_score`` calls so the server-side validity layers evaluate
        membership at the same instant the assembler's post-filter does —
        without it, ``assemble(as_of=t)`` could only ever narrow the
        *currently*-valid set and never reconstruct history (issue #580).

        Returns:
            Tuple of (selected_records, all_candidates).
        """
        if self._effective_mode == "hybrid":
            return self._pull_path_hybrid(query_cues, filters)
        elif self._effective_mode == "lexical":
            # Lexical mode (BM25 + graph, no embeddings) routes through the shared
            # hybrid body. Because _embedding_field is None in lexical mode, the
            # vector branch is gated off inside _pull_path_hybrid.
            return self._pull_path_hybrid(query_cues, filters)
        return self._pull_path_composite(query_cues, filters)

    def _pull_path_composite(self, query_cues, filters):
        """Execute pull-path retrieval via CompositeScoreQuery (original path).

        Returns:
            Tuple of (selected_records, all_candidates) where all_candidates
            includes records that may not make the final cut.
        """
        # ExistenceFilter pre-check
        if self._existence_filter is not None:
            all_missing = True
            for cue_value in query_cues.values():
                if not self._existence_filter.definitely_missing(
                    self.model_class, str(cue_value)
                ):
                    all_missing = False
                    break
            if all_missing:
                logger.debug(
                    "ExistenceFilter: all cues definitely missing, skipping pull"
                )
                return [], []

        # CoOccurrence boost (first pass without boost, then propagate)
        co_occurrence_boost = None

        try:
            # Initial composite score query
            query = self.model_class.query
            if filters:
                query = query.filter(**filters)

            candidates = query.composite_score(
                indexes=self.score_weights,
                limit=self._fetch_limit(self.max_items * 2),
                co_occurrence_boost=co_occurrence_boost,
                as_of=self._assembly_as_of,
            )
        except Exception as e:
            logger.warning("CompositeScoreQuery failed: %s", e)
            return [], []

        if not candidates:
            return [], []

        # CoOccurrence propagation (+ optional RelationshipField traversal,
        # #462) to discover associated records
        if (
            self._co_occurrence_field is not None
            or self._graph_traversal_relationship_fields
        ) and candidates:
            seed_pks = [_get_key(c) for c in candidates[: self.max_items]]
            try:
                if self._graph_traversal_relationship_fields:
                    from .graph_traversal import traverse as _graph_traverse

                    propagated = dict(
                        _graph_traverse(
                            self.model_class,
                            seed_pks,
                            co_occurrence_field=self._co_occurrence_field,
                            relationship_field_names=(
                                self._graph_traversal_relationship_fields
                            ),
                            depth=self.propagation_depth,
                            decay_per_hop=0.5,
                            threshold=0.01,
                            confidence_field_name=self._confidence_field_name,
                            decay_field_name=self._decaying_sorted_field_name,
                        )
                    )
                else:
                    propagated = self._co_occurrence_field.propagate(
                        self.model_class,
                        seed_pks,
                        depth=self.propagation_depth,
                        decay_per_hop=0.5,
                        threshold=0.01,
                    )
                if propagated:
                    # Re-run composite score with co-occurrence boost
                    query = self.model_class.query
                    if filters:
                        query = query.filter(**filters)
                    candidates = query.composite_score(
                        indexes=self.score_weights,
                        limit=self._fetch_limit(self.max_items * 2),
                        co_occurrence_boost=propagated,
                        as_of=self._assembly_as_of,
                    )
            except Exception as e:
                logger.warning("CoOccurrence propagation failed: %s", e)

        all_candidates = list(candidates)
        return candidates, all_candidates

    def _pull_path_hybrid(self, query_cues, filters):
        """Shared pull path for "hybrid" and "lexical" modes: BM25 + graph via RRF.

        In "hybrid" mode collects BM25 (lexical), vector (semantic), and graph
        signals then fuses them with ``QueryBuilder.fuse()`` (RRF, k=RRF_K).

        In "lexical" mode (BM25 + graph, no embeddings) the vector branch is
        skipped because ``self._embedding_field is None`` — no numpy required.

        Falls back to the composite path when both lexical and vector signals
        are empty (e.g. zero-BM25-hit queries degrade to composite, by design).

        Returns:
            Tuple of (selected_records, all_candidates).
        """
        query_text = " ".join(str(v) for v in query_cues.values())

        # ExistenceFilter pre-check (same short-circuit as composite path)
        if self._existence_filter is not None:
            all_missing = all(
                self._existence_filter.definitely_missing(self.model_class, str(v))
                for v in query_cues.values()
            )
            if all_missing:
                logger.debug(
                    "ExistenceFilter: all cues definitely missing, skipping hybrid pull"
                )
                return [], []

        candidate_limit = self._fetch_limit(self.max_items * HYBRID_CANDIDATE_MULTIPLIER)

        keyword_results: list = []
        vector_results: list = []
        graph_results: list = []

        # --- BM25 lexical retrieval ---
        # The BM25 index is corpus-wide, so the caller's scope (agent_id and
        # friends) has to be handed to search() rather than applied afterwards:
        # filtering a bounded fetch lets other agents' records crowd the window
        # and starve this one (#576).
        allowed_keys = None
        if filters:
            try:
                allowed_keys = self.model_class.query.filter_for_keys_set(**filters)
            except Exception as e:
                # Fail closed: an unscoped BM25 signal fused under a filtered
                # query is exactly the cross-agent leak this guards against.
                logger.warning(
                    "Could not resolve retrieval scope %s, skipping the lexical "
                    "signal: %s",
                    filters,
                    e,
                )
                allowed_keys = set()
        try:
            keyword_results = BM25Field.search(
                self.model_class,
                self._bm25_field_name,
                query_text,
                limit=candidate_limit,
                allowed_keys=allowed_keys,
            )
        except Exception as e:
            logger.warning("BM25 search failed in hybrid path: %s", e)

        # --- Vector semantic retrieval (only when an EmbeddingField is configured) ---
        # In "lexical" mode _embedding_field is None, so this branch is skipped
        # entirely — no numpy import, no embedding-provider call.
        if self._embedding_field is not None:
            try:
                from ..models.query import QueryBuilder as _QueryBuilder

                _q = self.model_class.query
                if filters:
                    _qb = _q.filter(**filters)
                else:
                    _qb = _QueryBuilder(_q)
                vector_results = _qb._get_vector_scores(
                    query_text, limit=candidate_limit
                )
            except Exception as e:
                logger.warning("Vector search failed in hybrid path: %s", e)

        if not keyword_results and not vector_results:
            logger.warning(
                "%s retrieval for %s collected no query signal — BM25 returned 0 "
                "hits%s (query has no lexical overlap OR the BM25 index is empty; "
                "re-save existing records to backfill the index — see the recipe "
                "reindex caveat). Falling back to composite (query-blind).",
                self._effective_mode,
                self.model_class.__name__,
                (
                    " and the vector search returned none"
                    if self._embedding_field is not None
                    else ""
                ),
            )
            return self._pull_path_composite(query_cues, filters)

        # --- Graph propagation (+ optional RelationshipField traversal,
        # #462), seeds from BM25 top results ---
        if (
            self._co_occurrence_field is not None
            or self._graph_traversal_relationship_fields
        ) and keyword_results:
            seed_pks = [k for k, _ in keyword_results[:5]]
            try:
                if self._graph_traversal_relationship_fields:
                    from .graph_traversal import traverse as _graph_traverse

                    graph_results = _graph_traverse(
                        self.model_class,
                        seed_pks,
                        co_occurrence_field=self._co_occurrence_field,
                        relationship_field_names=(
                            self._graph_traversal_relationship_fields
                        ),
                        depth=self.propagation_depth,
                        decay_per_hop=0.5,
                        threshold=0.01,
                        confidence_field_name=self._confidence_field_name,
                        decay_field_name=self._decaying_sorted_field_name,
                    )
                else:
                    propagated = self._co_occurrence_field.propagate(
                        self.model_class,
                        seed_pks,
                        depth=self.propagation_depth,
                        decay_per_hop=0.5,
                        threshold=0.01,
                    )
                    graph_results = list(propagated.items())
            except Exception as e:
                logger.warning("Graph propagation failed in hybrid path: %s", e)

        # --- RRF fusion ---
        fuse_kwargs: dict = {}
        if keyword_results:
            fuse_kwargs["keyword"] = keyword_results
        if vector_results:
            fuse_kwargs["vector"] = vector_results
        if graph_results:
            fuse_kwargs["graph"] = graph_results

        try:
            query = self.model_class.query
            if filters:
                query = query.filter(**filters)
            fusion_weights = _fusion_weights(query_text)
            fusion_weights["graph"] = FUSION_WEIGHT_GRAPH
            candidates = query.fuse(
                k=RRF_K,
                limit=self._fetch_limit(self.max_items * 2),
                weights=fusion_weights,
                **fuse_kwargs,
            )
        except Exception as e:
            logger.warning("RRF fusion failed, falling back to composite: %s", e)
            return self._pull_path_composite(query_cues, filters)

        all_candidates = list(candidates)
        return candidates, all_candidates

    def _push_path(self, filters):
        """Execute push-path retrieval via CyclicDecayField.

        Uses composite_score with min_score for threshold filtering instead
        of top_by_decay, since composite_score supports server-side score
        thresholds via ZREVRANGEBYSCORE.
        """
        try:
            query = self.model_class.query
            if filters:
                query = query.filter(**filters)

            # Build weights using only the CyclicDecayField for push-path scoring
            push_weights = {self._cyclic_decay_field_name: 1.0}

            results = query.composite_score(
                indexes=push_weights,
                limit=self._fetch_limit(self.max_items),
                min_score=(
                    self.surfacing_threshold if self.surfacing_threshold > 0 else None
                ),
                as_of=self._assembly_as_of,
            )
        except Exception as e:
            logger.warning("Push path failed: %s", e)
            return []

        if not results:
            logger.debug("Push path: 0 records above surfacing threshold")

        return results

    def _post_effects(
        self, selected, pull_keys, push_keys, all_pull_candidates, agent_id
    ):
        """Apply post-retrieval effects using Redis pipeline."""
        if not selected and not all_pull_candidates:
            return

        pipeline = POPOTO_REDIS_DB.pipeline()

        # on_read for pull-path selected records
        for record in selected:
            if _get_key(record) in pull_keys:
                ObservationProtocol.on_read(record, pipeline=pipeline)

        # on_surfaced for push-path selected records
        proactive_records = [r for r in selected if _get_key(r) in push_keys]
        if proactive_records:
            ObservationProtocol.on_surfaced(
                proactive_records,
                reason="proactive",
                partition=agent_id,
                pipeline=pipeline,
            )

        # Competitive suppression for non-selected pull candidates
        if self._confidence_field_name is not None:
            selected_keys = {_get_key(r) for r in selected}
            for candidate in all_pull_candidates:
                if _get_key(candidate) not in selected_keys:
                    try:
                        ConfidenceField.update_confidence(
                            candidate,
                            self._confidence_field_name,
                            signal=COMPETITIVE_SUPPRESSION_SIGNAL,
                        )
                    except (TypeError, ValueError):
                        pass  # Model may not have confidence on this instance

        try:
            pipeline.execute()
        except Exception as e:
            logger.warning("Post-effects pipeline failed: %s", e)

    # ------------------------------------------------------------------
    # Metacognitive layer: RetrievalQuality helpers + public assess()
    # ------------------------------------------------------------------

    def _cue_familiarity(self, cue_value) -> float:
        """Thin wrapper around the module-level :func:`_cue_familiarity`.

        Introspects ``self`` exactly once and forwards explicit kwargs so
        the pure helper is the single source of truth (issue #370 C2).
        """
        return _cue_familiarity(
            cue_value,
            existence_filter=self._existence_filter,
            model_class=self.model_class,
        )

    def _compute_fok(self, query_cues, pull_candidates):
        """Thin wrapper around the module-level :func:`_compute_fok`."""
        return _compute_fok(
            query_cues,
            pull_candidates,
            model_class=self.model_class,
            score_weights=self.score_weights,
            max_items=self.max_items,
            surfacing_threshold=self.surfacing_threshold,
            existence_filter=self._existence_filter,
        )

    def _score_proxy_for_records(self, records):
        """Thin wrapper around the module-level
        :func:`_score_proxy_for_records`.
        """
        return _score_proxy_for_records(
            records,
            model_class=self.model_class,
            score_weights=self.score_weights,
        )

    def _compute_score_spread(self, records):
        """Thin wrapper around the module-level
        :func:`_compute_score_spread`.
        """
        return _compute_score_spread(
            records,
            model_class=self.model_class,
            score_weights=self.score_weights,
        )

    def _avg_confidence(self, records):
        """Thin wrapper around the module-level :func:`_avg_confidence`."""
        return _avg_confidence(
            records,
            confidence_field_name=self._confidence_field_name,
            model_class=self.model_class,
        )

    def _staleness_ratio(self, records):
        """Thin wrapper around the module-level :func:`_staleness_ratio`."""
        return _staleness_ratio(
            records,
            model_class=self.model_class,
            score_weights=self.score_weights,
            surfacing_threshold=self.surfacing_threshold,
            decaying_sorted_field_name=self._decaying_sorted_field_name,
        )

    def _compute_quality(self, selected, all_pull_candidates, query_cues):
        """Assemble a RetrievalQuality over the selected records.

        Side-effect-free; reads from ConfidenceField, ExistenceFilter, and
        sorted-set indexes via their existing public APIs. Intended for
        calls at the end of ``assemble()`` (post selection) and from the
        standalone ``assess()`` probe.
        """
        avg_conf = self._avg_confidence(selected)
        score_spread, distribution = self._compute_score_spread(selected)
        fok_score, per_cue = self._compute_fok(query_cues, all_pull_candidates)
        staleness = self._staleness_ratio(selected)
        return RetrievalQuality(
            avg_confidence=avg_conf,
            score_spread=score_spread,
            fok_score=fok_score,
            staleness_ratio=staleness,
            score_distribution=distribution,
            per_cue_fok=per_cue,
        )

    def assess(self, query_cues=None, partition_filters=None, probe_limit=None):
        """Probe retrieval quality without running the full pipeline.

        Runs a cheap pre-retrieval check: ExistenceFilter lookups for
        cue_familiarity + a single low-limit composite_score probe to
        gather pull candidates for FOK computation. Does NOT run
        CoOccurrence propagation, does NOT run the push path, does NOT
        apply post-effects.

        Intended use: call ``assess()`` before ``assemble()`` to decide
        whether the full retrieval is worth the round-trip cost. When
        ``assess().fok_score < some_threshold``, the agent can skip the
        retrieval entirely and widen the cue or caveat its answer.

        Args:
            query_cues: Optional dict of query cues. When empty, all
                metrics default to 0.0 with a logged warning.
            partition_filters: Optional dict of partition filters. Same
                semantics as ``assemble()``.
            probe_limit: Optional cap on the number of candidates fetched
                for the probe. Defaults to ``self.max_items``.

        Returns:
            RetrievalQuality. ``selected`` is treated as empty; the quality
            reflects what's *available* for retrieval, not what was
            actually retrieved.
        """
        filters = dict(partition_filters or {})

        if not query_cues:
            logger.warning("assess() called with no query_cues")
            return RetrievalQuality()

        limit = probe_limit if probe_limit is not None else self.max_items
        probe_candidates = []

        # ExistenceFilter pre-check — same short-circuit as _pull_path.
        if self._existence_filter is not None:
            all_missing = True
            for cue_value in query_cues.values():
                if not self._existence_filter.definitely_missing(
                    self.model_class, str(cue_value)
                ):
                    all_missing = False
                    break
            if all_missing:
                # No probe — everything is definitely absent.
                fok_score, per_cue = self._compute_fok(query_cues, [])
                return RetrievalQuality(
                    avg_confidence=1.0 if not self._confidence_field_name else 0.5,
                    score_spread=0.0,
                    fok_score=fok_score,
                    staleness_ratio=0.0,
                    score_distribution=[],
                    per_cue_fok=per_cue,
                )

        try:
            query = self.model_class.query
            if filters:
                query = query.filter(**filters)
            probe_candidates = query.composite_score(
                indexes=self.score_weights,
                limit=limit,
            )
        except Exception as e:
            logger.warning("assess() composite_score probe failed: %s", e)
            probe_candidates = []

        avg_conf = self._avg_confidence(probe_candidates)
        score_spread, distribution = self._compute_score_spread(probe_candidates)
        fok_score, per_cue = self._compute_fok(query_cues, probe_candidates)
        staleness = self._staleness_ratio(probe_candidates)

        return RetrievalQuality(
            avg_confidence=avg_conf,
            score_spread=score_spread,
            fok_score=fok_score,
            staleness_ratio=staleness,
            score_distribution=distribution,
            per_cue_fok=per_cue,
        )
