"""Constant override injection for benchmark parameter sweeps.

Provides a context manager that applies overrides using the appropriate
injection pattern for each constant category, and restores originals on exit.

Dual-patch strategy: overrides are applied to both the centralized
``Defaults`` class and the module-level aliases that functions read at
runtime. This ensures overrides take effect regardless of whether code
reads ``Defaults.X`` or the bare module-level name.
"""

import contextlib
from typing import Any, Dict, Generator

from src.popoto.fields.constants import Defaults

import src.popoto.fields.observation as observation_mod
import src.popoto.recipes.policy_cache as policy_cache_mod
import src.popoto.recipes.context_assembler as context_assembler_mod

# Registry mapping constant names to (module, attribute_name) for module-level constants.
# Each entry also has a corresponding attribute on the Defaults class.
MODULE_CONSTANTS = {
    # ObservationProtocol (observation.py)
    "ACTED_CONFIDENCE_SIGNAL": (observation_mod, "ACTED_CONFIDENCE_SIGNAL"),
    "CONTRADICTED_CONFIDENCE_SIGNAL": (
        observation_mod,
        "CONTRADICTED_CONFIDENCE_SIGNAL",
    ),
    "ACTED_CYCLE_STRENGTHEN_FACTOR": (
        observation_mod,
        "ACTED_CYCLE_STRENGTHEN_FACTOR",
    ),
    "DISMISSED_CYCLE_WEAKEN_FACTOR": (
        observation_mod,
        "DISMISSED_CYCLE_WEAKEN_FACTOR",
    ),
    "CONTRADICTED_CYCLE_WEAKEN_FACTOR": (
        observation_mod,
        "CONTRADICTED_CYCLE_WEAKEN_FACTOR",
    ),
    "AUTO_DISCHARGE_CONFIDENCE_THRESHOLD": (
        observation_mod,
        "AUTO_DISCHARGE_CONFIDENCE_THRESHOLD",
    ),
    # PolicyCache (policy_cache.py)
    "MIN_EVENTS_FOR_CRYSTALLIZATION": (
        policy_cache_mod,
        "MIN_EVENTS_FOR_CRYSTALLIZATION",
    ),
    "WILSON_CI_THRESHOLD": (policy_cache_mod, "WILSON_CI_THRESHOLD"),
    "TD_ALPHA": (policy_cache_mod, "TD_ALPHA"),
    "TD_GAMMA": (policy_cache_mod, "TD_GAMMA"),
    "CHI_SQUARED_P_THRESHOLD": (policy_cache_mod, "CHI_SQUARED_P_THRESHOLD"),
    "INITIAL_CYCLE_AMPLITUDE": (policy_cache_mod, "INITIAL_CYCLE_AMPLITUDE"),
    # ContextAssembler (context_assembler.py)
    "COMPETITIVE_SUPPRESSION_SIGNAL": (
        context_assembler_mod,
        "COMPETITIVE_SUPPRESSION_SIGNAL",
    ),
    "DEFAULT_SURFACING_THRESHOLD": (
        context_assembler_mod,
        "DEFAULT_SURFACING_THRESHOLD",
    ),
}

# Valid ranges for boundary checking
VALID_RANGES = {
    "decay_rate": (0.0, 2.0, False, True),  # (min, max, include_min, include_max)
    "initial_confidence": (0.0, 1.0, True, True),
    "_wf_min_threshold": (0.0, 1.0, True, False),
    "_wf_priority_threshold": (0.0, 1.0, True, True),
    "ACTED_CONFIDENCE_SIGNAL": (0.0, 1.0, True, True),
    "CONTRADICTED_CONFIDENCE_SIGNAL": (0.0, 1.0, True, True),
    "ACTED_CYCLE_STRENGTHEN_FACTOR": (0.0, 5.0, False, True),
    "DISMISSED_CYCLE_WEAKEN_FACTOR": (0.0, 5.0, False, True),
    "CONTRADICTED_CYCLE_WEAKEN_FACTOR": (0.0, 5.0, False, True),
    "AUTO_DISCHARGE_CONFIDENCE_THRESHOLD": (0.0, 1.0, True, True),
    "decay_factor": (0.0, 1.0, False, True),
    "initial_weight": (0.0, 1.0, False, True),
    "delta": (0.0, 1.0, False, True),
    "decay_per_hop": (0.0, 1.0, False, True),
    "MIN_EVENTS_FOR_CRYSTALLIZATION": (1, 100, True, True),
    "TD_ALPHA": (0.0, 1.0, False, True),
    "TD_GAMMA": (0.0, 1.0, True, False),
    "WILSON_CI_THRESHOLD": (0.0, 1.0, False, False),
    "COMPETITIVE_SUPPRESSION_SIGNAL": (0.0, 1.0, True, True),
    "DEFAULT_SURFACING_THRESHOLD": (0.0, 1.0, True, True),
}


def is_degenerate(name: str, value: float) -> bool:
    """Check if a parameter value is at a degenerate boundary.

    Returns True if the value is at or beyond the excluded boundary
    of its valid range.
    """
    if name not in VALID_RANGES:
        return False
    vmin, vmax, inc_min, inc_max = VALID_RANGES[name]
    if not inc_min and value <= vmin:
        return True
    if not inc_max and value >= vmax:
        return True
    return False


@contextlib.contextmanager
def apply_overrides(overrides: Dict[str, Any]) -> Generator[None, None, None]:
    """Context manager to apply constant overrides and restore on exit.

    Dual-patch strategy:
    - Module-level constants: setattr on the module AND on Defaults
    - Field constructor kwargs / class attributes: stored in overrides
      dict for the scenario to use

    Patching both Defaults and the module alias ensures overrides work
    regardless of whether code reads ``Defaults.X`` (e.g. new field
    instances created mid-test) or the bare module-level name (e.g.
    existing functions that read ``ACTED_CONFIDENCE_SIGNAL`` directly).

    Args:
        overrides: Mapping of constant name to override value.

    Yields:
        None. Constants are patched for the duration.
    """
    originals_module = {}
    originals_defaults = {}

    try:
        for name, value in overrides.items():
            if name in MODULE_CONSTANTS:
                # Patch module-level alias
                mod, attr = MODULE_CONSTANTS[name]
                originals_module[(mod, attr)] = getattr(mod, attr)
                setattr(mod, attr, value)

                # Patch Defaults class (same attr name)
                if hasattr(Defaults, name):
                    originals_defaults[name] = getattr(Defaults, name)
                    setattr(Defaults, name, value)
            elif hasattr(Defaults, name.upper()):
                # Handle constants that only exist on Defaults (not in MODULE_CONSTANTS)
                defaults_attr = name.upper()
                originals_defaults[defaults_attr] = getattr(Defaults, defaults_attr)
                setattr(Defaults, defaults_attr, value)
        yield
    finally:
        for (mod, attr), original in originals_module.items():
            setattr(mod, attr, original)
        for attr, original in originals_defaults.items():
            setattr(Defaults, attr, original)
