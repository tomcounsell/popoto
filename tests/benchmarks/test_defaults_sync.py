"""Drift detection: verify module-level constant aliases match Defaults.

Every module-level constant in MODULE_CONSTANTS should have the same
value as its corresponding Defaults attribute.  This catches cases where
a module constant is updated but Defaults is not (or vice versa).
"""

import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest

from src.popoto.fields.constants import Defaults
from tests.benchmarks.overrides import MODULE_CONSTANTS


class TestDefaultsSync:
    """Verify every module-level alias matches its Defaults counterpart."""

    @pytest.mark.parametrize(
        "name,mod_attr",
        [(name, (mod, attr)) for name, (mod, attr) in MODULE_CONSTANTS.items()],
        ids=list(MODULE_CONSTANTS.keys()),
    )
    def test_module_alias_matches_defaults(self, name, mod_attr):
        mod, attr = mod_attr
        module_value = getattr(mod, attr)
        assert hasattr(Defaults, name), f"Defaults class is missing attribute '{name}'"
        defaults_value = getattr(Defaults, name)
        assert module_value == defaults_value, (
            f"Drift detected: {mod.__name__}.{attr} = {module_value} "
            f"but Defaults.{name} = {defaults_value}"
        )

    def test_all_defaults_covered_by_module_constants(self):
        """Every Defaults attribute that corresponds to a module constant
        should appear in MODULE_CONSTANTS."""
        # Get all uppercase attributes from Defaults (these are the constants)
        defaults_attrs = {
            name
            for name in dir(Defaults)
            if name.isupper() and not name.startswith("_")
        }
        module_constant_names = set(MODULE_CONSTANTS.keys())

        # Some Defaults attrs are field kwargs / class attrs, not module-level.
        # Those are expected to NOT be in MODULE_CONSTANTS.
        field_kwargs_and_class_attrs = {
            "DECAY_RATE",
            "INITIAL_CONFIDENCE",
            # ConfidenceField constructor kwarg default (evidence_cap, #407);
            # no module-level alias exists, so not in MODULE_CONSTANTS.
            "CONFIDENCE_EVIDENCE_CAP",
            "WF_MIN_THRESHOLD",
            "WF_PRIORITY_THRESHOLD",
            "CO_OCCURRENCE_DECAY_FACTOR",
            "CO_OCCURRENCE_INITIAL_WEIGHT",
            "CO_OCCURRENCE_DECAY_PER_HOP",
            "PL_CONFIDENCE_ERROR_THRESHOLD",
            "PL_CONFIDENCE_LOW_SIGNAL",
            "PL_AUTO_RESOLVE_ACTED",
            "PL_AUTO_RESOLVE_DISMISSED",
            "PL_AUTO_RESOLVE_CONTRADICTED",
            "PL_AUTO_RESOLVE_USED",
            "ADAPTIVE_QUALITY_WINDOW_SIZE",
            "TRAJECTORY_CLUSTER_THRESHOLD",
            # MemoryLifecycle class-level constants (recipes/memory_lifecycle.py)
            # Not module-level aliases — patched via CLASS_ATTR_CONSTANTS in overrides.py.
            "LIFECYCLE_PROMOTION_ACCESS_COUNT",
            "LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD",
            "LIFECYCLE_PROMOTION_MIN_AGE_SECONDS",
            "LIFECYCLE_FORGET_IMPORTANCE_FLOOR",
            "LIFECYCLE_FORGET_IDLE_SECONDS",
        }

        expected_in_module = defaults_attrs - field_kwargs_and_class_attrs
        missing = expected_in_module - module_constant_names
        assert not missing, (
            f"Defaults attributes not in MODULE_CONSTANTS: {missing}. "
            f"Either add them to MODULE_CONSTANTS or to field_kwargs_and_class_attrs."
        )
