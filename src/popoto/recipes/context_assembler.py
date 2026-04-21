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
import math
import statistics
import time
from dataclasses import dataclass, field

from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
from ..fields.cyclic_decay_field import CyclicDecayField
from ..fields.decaying_sorted_field import DecayingSortedField
from ..fields.existence_filter import ExistenceFilter
from ..fields.observation import ObservationProtocol
from ..fields.sorted_field_mixin import SortedFieldMixin
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


# FOK formula weights — hard-coded per design spec (see plan
# "Rabbit Holes": adjusting these is out-of-scope).
FOK_WEIGHT_CUE_FAMILIARITY = 0.4
FOK_WEIGHT_PARTIAL_RETRIEVAL = 0.4
FOK_WEIGHT_SUBTHRESHOLD_ACTIVATION = 0.2


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


def format_structured(records) -> str:
    """Format records as JSON array."""
    dicts = [_record_to_dict(r) for r in records]
    return json.dumps(dicts, default=str, indent=2)


def format_xml(records) -> str:
    """Format records as XML tags."""
    lines = ["<records>"]
    for record in records:
        d = _record_to_dict(record)
        lines.append("  <record>")
        for key, val in d.items():
            escaped = (
                str(val).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            lines.append(f"    <{key}>{escaped}</{key}>")
        lines.append("  </record>")
    lines.append("</records>")
    return "\n".join(lines)


def format_natural(records) -> str:
    """Format records as natural language summary."""
    if not records:
        return ""
    parts = []
    for i, record in enumerate(records, 1):
        d = _record_to_dict(record)
        fields_str = ", ".join(f"{k}: {v}" for k, v in d.items() if v is not None)
        parts.append(f"{i}. {fields_str}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# ContextAssembler
# ---------------------------------------------------------------------------


class ContextAssembler:
    """Orchestrates memory retrieval into a single assemble() call.

    Combines pull-path (query-driven via CompositeScoreQuery) and push-path
    (proactive via CyclicDecayField) retrieval, applies token budgets, and
    formats output for LLM context injection.

    Args:
        model_class: Popoto Model class to query.
        score_weights: Dict mapping field names to weights for
            CompositeScoreQuery (e.g., {"relevance": 0.6, "confidence": 0.3}).
        max_items: Maximum records to return. Default 10.
        max_tokens: Optional soft token budget. Records are dropped to fit.
        surfacing_threshold: Minimum score for push-path records. Default 0.5.
        propagation_depth: BFS depth for CoOccurrence. Default 2.
        output_format: "structured" (JSON), "xml", or "natural". Default "structured".
        token_counter: Optional callable(record) -> int. Default: len(str(r)) // 4.
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
    ):
        self.model_class = model_class
        self.score_weights = score_weights
        self.max_items = max_items
        self.max_tokens = max_tokens
        self.surfacing_threshold = surfacing_threshold
        self.propagation_depth = propagation_depth
        self.output_format = output_format
        self._token_counter = token_counter or (lambda r: len(str(r)) // 4)

        # Detect field capabilities on model
        self._existence_filter = None
        self._co_occurrence_field = None
        self._co_occurrence_field_name = None
        self._cyclic_decay_field_name = None
        self._confidence_field_name = None
        self._decaying_sorted_field_name = None

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

    def assemble(
        self,
        query_cues=None,
        agent_id=None,
        partition_filters=None,
        assess_quality=False,
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

        Returns:
            AssemblyResult with records, proactive, formatted, and metadata.
        """
        t0 = time.time()

        # Build partition filters
        filters = dict(partition_filters or {})
        if agent_id is not None:
            filters["agent_id"] = agent_id

        pull_records = []
        push_records = []
        all_pull_candidates = []  # For competitive suppression

        # --- Pull path ---
        if query_cues:
            pull_records, all_pull_candidates = self._pull_path(query_cues, filters)

        # --- Push path ---
        if self._cyclic_decay_field_name is not None:
            push_records = self._push_path(filters)

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

        # max_tokens cap
        total_tokens = 0
        if self.max_tokens is not None:
            budget_selected = []
            for record in selected:
                try:
                    tokens = self._token_counter(record)
                except Exception:
                    tokens = len(str(record)) // 4
                    logger.warning("Token counter failed, falling back to heuristic")
                if total_tokens + tokens > self.max_tokens and budget_selected:
                    break
                total_tokens += tokens
                budget_selected.append(record)
            selected = budget_selected
        else:
            for record in selected:
                try:
                    total_tokens += self._token_counter(record)
                except Exception:
                    total_tokens += len(str(record)) // 4

        # Identify proactive records in final selection
        proactive = [r for r in selected if _get_key(r) in push_keys]

        # --- Post-retrieval effects ---
        self._post_effects(
            selected, pull_keys, push_keys, all_pull_candidates, agent_id
        )

        # --- Format ---
        formatter = {
            "structured": format_structured,
            "xml": format_xml,
            "natural": format_natural,
        }.get(self.output_format, format_structured)

        formatted = formatter(selected)

        timing_ms = round((time.time() - t0) * 1000, 2)

        metadata = {
            "pull_count": len([r for r in selected if _get_key(r) in pull_keys]),
            "push_count": len(proactive),
            "token_count": total_tokens,
            "timing_ms": timing_ms,
            "total_candidates": len(merged),
        }

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

        return AssemblyResult(
            records=selected,
            proactive=proactive,
            formatted=formatted,
            metadata=metadata,
        )

    def _pull_path(self, query_cues, filters):
        """Execute pull-path retrieval.

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
                limit=self.max_items * 2,
                co_occurrence_boost=co_occurrence_boost,
            )
        except Exception as e:
            logger.warning("CompositeScoreQuery failed: %s", e)
            return [], []

        if not candidates:
            return [], []

        # CoOccurrence propagation to discover associated records
        if self._co_occurrence_field is not None and candidates:
            seed_pks = [_get_key(c) for c in candidates[: self.max_items]]
            try:
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
                        limit=self.max_items * 2,
                        co_occurrence_boost=propagated,
                    )
            except Exception as e:
                logger.warning("CoOccurrence propagation failed: %s", e)

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
                limit=self.max_items,
                min_score=(
                    self.surfacing_threshold if self.surfacing_threshold > 0 else None
                ),
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
        """Compute FOK cue_familiarity component for a single cue value.

        When the model has no ExistenceFilter, fall back to a neutral
        ``0.5`` (we cannot prove absence, we cannot prove presence).
        When present and ``might_exist`` returns True, ``1.0``; when
        present and False, ``0.0``.
        """
        if self._existence_filter is None:
            return 0.5
        try:
            present = self._existence_filter.might_exist(
                self.model_class, str(cue_value)
            )
        except Exception as e:
            logger.warning("cue_familiarity check failed for %r: %s", cue_value, e)
            return 0.5
        return 1.0 if present else 0.0

    def _compute_fok(self, query_cues, pull_candidates):
        """Compute FOK score and per-cue breakdown.

        Returns a tuple ``(fok_score, per_cue_map)``.

        Formula (per design spec, hard-coded):
            fok = mean_over_cues(
                0.4 * cue_familiarity
                + 0.4 * partial_retrieval_count
                + 0.2 * subthreshold_activation
            )

        * ``cue_familiarity``: 1.0 if ExistenceFilter says might_exist,
          0.0 if definitely_missing, 0.5 if no ExistenceFilter on model.
        * ``partial_retrieval_count``: ``min(len(candidates), max_items) /
          max_items`` — did we surface enough raw candidates to have
          something to pick from?
        * ``subthreshold_activation``: fraction of candidates with score
          strictly between 0 and the surfacing threshold — "almost
          remembered" content. Computed over pull candidates using the
          composite score proxy (see ``_score_proxy_for_records``).

        Edge cases:
        * Empty ``query_cues`` -> ``fok_score = 0.0``, empty per-cue map,
          with a logged warning. Do not raise.
        * ``pull_candidates`` is empty -> partial_retrieval and subthreshold
          both contribute 0.
        """
        if not query_cues:
            logger.warning("_compute_fok called with empty query_cues")
            return 0.0, {}

        n_candidates = len(pull_candidates)
        max_items = max(self.max_items, 1)
        partial_retrieval = min(n_candidates, max_items) / max_items

        # Subthreshold activation: pull candidates with score below the
        # surfacing_threshold but > 0. Uses the composite-score proxy.
        subthreshold_frac = 0.0
        if n_candidates > 0 and self.score_weights:
            try:
                proxy_scores = self._score_proxy_for_records(pull_candidates)
                below_threshold = sum(
                    1 for s in proxy_scores.values() if 0 < s < self.surfacing_threshold
                )
                subthreshold_frac = below_threshold / max(n_candidates, 1)
            except Exception as e:
                logger.warning("Subthreshold activation computation failed: %s", e)
                subthreshold_frac = 0.0

        per_cue = {}
        total = 0.0
        for cue_value in query_cues.values():
            familiarity = self._cue_familiarity(cue_value)
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

    def _score_proxy_for_records(self, records):
        """Build a per-record composite-score proxy via pipelined ZSCORE.

        Returns ``{redis_key: composite_score}``. The proxy mirrors
        composite_score's ZUNIONSTORE aggregation: for each weighted field
        in ``self.score_weights``, read the sorted set score for this
        record and add ``weight * score`` to the running total. Fields
        that are not sorted-set-backed (e.g., ConfidenceField) are read
        via their companion hash instead.

        This is a read-only helper; it does NOT mutate any Redis state
        and does NOT recreate temp keys. It is meant for quality probes
        where we need per-record scores without the overhead (and side
        effects) of a full composite_score call.
        """
        if not records:
            return {}

        # We compute composite per record using only SortedFieldMixin-backed
        # index fields. ConfidenceField / WriteFilter priority / AccessTracker
        # are supported by composite_score proper via companion hashes; for a
        # cheap proxy we restrict to the sorted-field path, which covers the
        # common cases (DecayingSortedField, CyclicDecayField, SortedField).
        sorted_field_names = []
        for field_name in self.score_weights:
            try:
                f = self.model_class._meta.fields.get(field_name)
            except Exception:
                f = None
            if f is not None and isinstance(f, SortedFieldMixin):
                sorted_field_names.append(field_name)

        if not sorted_field_names:
            # No sorted-field indexes -> no proxy scores.
            return {r_key: 0.0 for r_key in (_get_key(r) for r in records)}

        # Build ZSET key per field (resolves partition_by via the instance).
        per_field_keys = {}
        for field_name in sorted_field_names:
            f = self.model_class._meta.fields[field_name]
            per_field_keys[field_name] = []
            for record in records:
                try:
                    key = f.get_special_use_field_db_key(record, field_name).redis_key
                except Exception:
                    key = None
                per_field_keys[field_name].append(key)

        # Pipeline all ZSCORE calls.
        pipe = POPOTO_REDIS_DB.pipeline()
        for field_name in sorted_field_names:
            zset_keys = per_field_keys[field_name]
            for record, zset_key in zip(records, zset_keys):
                if zset_key is None:
                    pipe.zscore("", "")  # placeholder that returns None
                    continue
                pipe.zscore(zset_key, _get_key(record))
        raw = pipe.execute()

        # Re-aggregate per record.
        scores = {_get_key(r): 0.0 for r in records}
        idx = 0
        for field_name in sorted_field_names:
            weight = float(self.score_weights.get(field_name, 0.0))
            for record in records:
                r_key = _get_key(record)
                val = raw[idx]
                idx += 1
                if val is not None:
                    try:
                        scores[r_key] += weight * float(val)
                    except (TypeError, ValueError):
                        pass
        return scores

    def _compute_score_spread(self, records):
        """Coefficient of variation of composite-score proxy across records.

        Returns ``(score_spread, distribution_list)``. Falls back to
        ``(0.0, [])`` on empty input or when ``abs(mean) < 1e-9``.
        """
        if not records:
            return 0.0, []
        try:
            proxy = self._score_proxy_for_records(records)
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
            # stddev/mean is undefined when mean is zero (empty or all-zero).
            return 0.0, scores
        try:
            stddev = statistics.pstdev(scores)
        except statistics.StatisticsError:
            return 0.0, scores
        return stddev / mean, scores

    def _avg_confidence(self, records):
        """Mean ConfidenceField.get_confidence() across records.

        Returns ``1.0`` (neutral) when the model has no ConfidenceField,
        matching "no evidence against the retrieval." Per-record exceptions
        are logged and the record contributes the field's
        ``initial_confidence`` as a sentinel rather than aborting the
        whole metric.
        """
        if not records or not self._confidence_field_name:
            return 1.0
        field = self.model_class._meta.fields.get(self._confidence_field_name)
        initial = getattr(field, "initial_confidence", 0.5)

        total = 0.0
        count = 0
        for record in records:
            try:
                val = ConfidenceField.get_confidence(
                    record, self._confidence_field_name
                )
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

    def _staleness_ratio(self, records):
        """Fraction of records whose DecayingSortedField score is below
        the field's decay threshold.

        Returns ``0.0`` when the model has no DecayingSortedField (we
        cannot measure staleness without a decay signal). The "decay
        threshold" is interpreted as ``self.surfacing_threshold`` — the
        same threshold used to filter push-path records. A record whose
        decayed score is below this threshold is considered "stale"
        for the purposes of the retrieval quality signal.
        """
        if not records or not self._decaying_sorted_field_name:
            return 0.0
        field_name = self._decaying_sorted_field_name
        try:
            decayed_scores = self._score_proxy_for_records(records)
        except Exception as e:
            logger.warning("_staleness_ratio proxy failed: %s", e)
            return 0.0
        # Use only the decaying field's contribution. The proxy already
        # weights by score_weights. If DecayingSortedField isn't in the
        # configured weights, we cannot judge staleness.
        if field_name not in self.score_weights:
            return 0.0
        # When there's only one weighted field, proxy == weighted score; when
        # mixed, the other weighted contributions inflate the score. Use
        # direct ZSCORE for the decaying field to get a clean read.
        f = self.model_class._meta.fields[field_name]
        pipe = POPOTO_REDIS_DB.pipeline()
        for record in records:
            try:
                zkey = f.get_special_use_field_db_key(record, field_name).redis_key
                pipe.zscore(zkey, _get_key(record))
            except Exception:
                pipe.zscore("", "")  # placeholder None
        raw = pipe.execute()

        stale_count = 0
        for val in raw:
            if val is None:
                stale_count += 1
                continue
            try:
                if float(val) < self.surfacing_threshold:
                    stale_count += 1
            except (TypeError, ValueError):
                stale_count += 1
        return stale_count / len(records)

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
