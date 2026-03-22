"""Base scenario class for benchmark evaluation.

Each scenario creates models, simulates a lifecycle, queries results,
and returns metrics. Scenarios are self-contained and idempotent.
"""

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from src.popoto.redis_db import POPOTO_REDIS_DB


@dataclass
class ScenarioResult:
    """Results from running a single scenario.

    Attributes:
        scenario_name: Name of the scenario that produced these results.
        retrieved_ids: Ordered list of retrieved item IDs from the query.
        relevant_ids: Set of ground-truth relevant item IDs.
        relevance_scores: Per-item relevance scores for nDCG computation.
        predictions: Predicted probabilities for calibration measurement.
        outcomes: Actual boolean outcomes for calibration measurement.
        metadata: Additional scenario-specific data.
        duration_ms: Wall-clock execution time in milliseconds.
        status: "ok", "skipped-degenerate", or "error".
        error_message: Description if status is not "ok".
    """

    scenario_name: str = ""
    retrieved_ids: List[str] = field(default_factory=list)
    relevant_ids: Set[str] = field(default_factory=set)
    relevance_scores: Dict[str, float] = field(default_factory=dict)
    predictions: List[float] = field(default_factory=list)
    outcomes: List[bool] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    duration_ms: float = 0.0
    status: str = "ok"
    error_message: str = ""


class Scenario(ABC):
    """Base class for benchmark scenarios.

    Subclasses implement setup(), run(), and teardown(). The execute()
    method handles the full lifecycle including timing and error handling.
    """

    name: str = "base"

    def __init__(self, overrides: Optional[Dict[str, Any]] = None):
        """Initialize with optional constant overrides.

        Args:
            overrides: Dict of constant_name -> value to apply during this run.
        """
        self.overrides = overrides or {}
        self._prefix = f"bench:{uuid.uuid4().hex[:8]}:"
        self._created_keys: List[str] = []

    @abstractmethod
    def setup(self) -> None:
        """Create model instances and test data in Redis."""
        ...

    @abstractmethod
    def run(self) -> ScenarioResult:
        """Execute queries and return results with ground truth."""
        ...

    def teardown(self) -> None:
        """Clean up Redis state. Default implementation deletes tracked keys."""
        if self._created_keys:
            try:
                POPOTO_REDIS_DB.delete(*self._created_keys)
            except Exception:
                pass
        # Also clean up by prefix
        try:
            cursor = 0
            while True:
                cursor, keys = POPOTO_REDIS_DB.scan(
                    cursor, match=f"{self._prefix}*", count=100
                )
                if keys:
                    POPOTO_REDIS_DB.delete(*keys)
                if cursor == 0:
                    break
        except Exception:
            pass

    def execute(self) -> ScenarioResult:
        """Run the full scenario lifecycle with timing and error handling.

        Returns:
            ScenarioResult with metrics and metadata.
        """
        start = time.monotonic()
        try:
            self.setup()
            result = self.run()
            result.scenario_name = self.name
            result.duration_ms = (time.monotonic() - start) * 1000
            return result
        except Exception as e:
            return ScenarioResult(
                scenario_name=self.name,
                status="error",
                error_message=str(e),
                duration_ms=(time.monotonic() - start) * 1000,
            )
        finally:
            self.teardown()
