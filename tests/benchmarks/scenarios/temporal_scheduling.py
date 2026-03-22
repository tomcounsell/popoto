"""Temporal Scheduling scenario — cyclic decay and time-based retrieval.

Simulates an agent with recurring tasks that follow daily/weekly patterns.
Tests whether CyclicDecayField correctly surfaces items whose cycles
align with the current time context.

Ground truth: items whose cycle phase matches "now" should rank highest.
Query: top_by_decay with cycle-aware scoring.
"""

import time

from src import popoto
from src.popoto.fields.confidence_field import ConfidenceField
from src.popoto.fields.constants import TemporalPeriod
from src.popoto.fields.cyclic_decay_field import CyclicDecayField
from src.popoto.fields.decaying_sorted_field import DecayingSortedField
from src.popoto.fields.observation import ObservationProtocol
from src.popoto.redis_db import POPOTO_REDIS_DB

from ..overrides import apply_overrides
from .base import Scenario, ScenarioResult


def _build_model_class(overrides):
    """Build a ScheduledTask model class with optional field overrides."""
    decay_rate = overrides.get("decay_rate", 0.5)
    initial_confidence = overrides.get("initial_confidence", 0.5)

    class ScheduledTask(popoto.Model):
        name = popoto.UniqueKeyField()
        description = popoto.StringField(default="")
        priority = popoto.FloatField(default=0.5)
        schedule = CyclicDecayField(
            decay_rate=decay_rate,
            cycles=[(TemporalPeriod.WEEKLY, 5.0, 0)],
            pressure_rate=0.1,
        )
        certainty = ConfidenceField(initial_confidence=initial_confidence)

    return ScheduledTask


# Scheduled tasks: some have been acted on recently, others not
TASKS = [
    ("daily_standup", "Run daily standup meeting", 0.9),
    ("weekly_review", "Weekly code review session", 0.8),
    ("deploy_staging", "Deploy to staging environment", 0.75),
    ("backup_db", "Run database backup", 0.7),
    ("check_metrics", "Check system metrics dashboard", 0.65),
    ("update_deps", "Update dependencies", 0.5),
    ("clean_logs", "Clean old log files", 0.4),
    ("review_alerts", "Review monitoring alerts", 0.6),
]


class TemporalSchedulingScenario(Scenario):
    """Retrieve time-sensitive tasks based on cyclic decay patterns."""

    name = "temporal_scheduling"

    def setup(self):
        self._model_class = _build_model_class(self.overrides)
        self._instances = []
        self._recently_acted = set()

        for task_name, description, priority in TASKS:
            instance = self._model_class(
                name=f"{self._prefix}{task_name}",
                description=description,
                priority=priority,
            )
            instance.save()
            self._instances.append(instance)
            self._created_keys.append(instance.db_key.redis_key)

        # Simulate: first 3 tasks were acted on (recently done)
        # Remaining tasks were dismissed or deferred (need attention)
        with apply_overrides(self.overrides):
            for i, inst in enumerate(self._instances):
                key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                if i < 3:
                    # Recently acted — should rank lower (already done)
                    ObservationProtocol.on_context_used([inst], {key: "acted"})
                    self._recently_acted.add(key)
                elif i < 5:
                    # Deferred — pressure building
                    ObservationProtocol.on_context_used([inst], {key: "deferred"})

    def run(self) -> ScenarioResult:
        if not self._instances:
            return ScenarioResult(
                status="error",
                error_message="No instances created",
            )

        # Query via top_by_decay (time-weighted retrieval)
        with apply_overrides(self.overrides):
            try:
                results = self._model_class.query.top_by_decay("schedule", n=10)
            except Exception as e:
                return ScenarioResult(
                    status="error",
                    error_message=f"top_by_decay failed: {e}",
                )

        retrieved_ids = []
        for r in results:
            key = getattr(r, "_redis_key", None) or r.db_key.redis_key
            retrieved_ids.append(key)

        # Ground truth: tasks NOT recently acted on are "relevant"
        # (they need attention). High-priority non-acted tasks are most relevant.
        relevant_ids = set()
        relevance_scores = {}
        for inst in self._instances:
            key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
            if key not in self._recently_acted:
                relevant_ids.add(key)
                relevance_scores[key] = inst.priority or 0
            else:
                relevance_scores[key] = 0.0

        # Calibration: confidence for acted items should be higher
        predictions = []
        outcomes = []
        for inst in self._instances:
            try:
                conf = ConfidenceField.get_confidence(inst, "certainty")
                predictions.append(conf)
                key = getattr(inst, "_redis_key", None) or inst.db_key.redis_key
                # "Outcome" here = was it recently acted on (high confidence)
                outcomes.append(key in self._recently_acted)
            except Exception:
                pass

        return ScenarioResult(
            retrieved_ids=retrieved_ids,
            relevant_ids=relevant_ids,
            relevance_scores=relevance_scores,
            predictions=predictions,
            outcomes=outcomes,
            metadata={
                "n_total": len(self._instances),
                "n_recently_acted": len(self._recently_acted),
                "n_need_attention": len(relevant_ids),
            },
        )

    def teardown(self):
        if hasattr(self, "_model_class"):
            for inst in getattr(self, "_instances", []):
                try:
                    inst.delete()
                except Exception:
                    pass
            try:
                cursor = 0
                while True:
                    cursor, keys = POPOTO_REDIS_DB.scan(
                        cursor, match=f"*{self._prefix}*", count=200
                    )
                    if keys:
                        POPOTO_REDIS_DB.delete(*keys)
                    if cursor == 0:
                        break
            except Exception:
                pass
        super().teardown()
