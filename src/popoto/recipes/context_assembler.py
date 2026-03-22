"""ContextAssembler — Retrieval-to-injection bridge with token budgets.

A capstone recipe composing all shipped Popoto memory primitives into a
single ``assemble()`` call. Orchestrates pull-path (query-driven) and
push-path (proactive surfacing) retrieval, applies token budgets, and
formats output for LLM context injection.

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
import time
from dataclasses import dataclass, field

from ..fields.co_occurrence_field import CoOccurrenceField
from ..fields.confidence_field import ConfidenceField
from ..fields.constants import Defaults
from ..fields.cyclic_decay_field import CyclicDecayField
from ..fields.existence_filter import ExistenceFilter
from ..fields.observation import ObservationProtocol
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

    def assemble(self, query_cues=None, agent_id=None, partition_filters=None):
        """Execute the full retrieval pipeline.

        Args:
            query_cues: Optional dict of query cues (e.g., {"topic": "deploy"}).
                If None, pull path is skipped.
            agent_id: Optional agent ID for partition filtering. Added to
                partition_filters as {"agent_id": agent_id}.
            partition_filters: Optional dict of partition key-value pairs
                for filtering queries.

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

        return AssemblyResult(
            records=selected,
            proactive=proactive,
            formatted=formatted,
            metadata={
                "pull_count": len([r for r in selected if _get_key(r) in pull_keys]),
                "push_count": len(proactive),
                "token_count": total_tokens,
                "timing_ms": timing_ms,
                "total_candidates": len(merged),
            },
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
