"""Constant override injection for benchmark parameter sweeps.

Provides a context manager that applies overrides using the appropriate
injection pattern for each constant category, and restores originals on exit.
"""

import contextlib
from typing import Any, Dict, Generator

import src.popoto.fields.observation as observation_mod
import src.popoto.recipes.policy_cache as policy_cache_mod
import src.popoto.recipes.context_assembler as context_assembler_mod

# Registry mapping constant names to (module, attribute_name) for module-level constants
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

    Handles three injection patterns:
    - Module-level constants: setattr on the module
    - Field constructor kwargs: stored in overrides dict for scenario to use
    - Class attributes: stored in overrides dict for scenario to use

    Args:
        overrides: Mapping of constant name to override value.

    Yields:
        None. Module-level constants are patched for the duration.
    """
    originals = {}

    try:
        for name, value in overrides.items():
            if name in MODULE_CONSTANTS:
                mod, attr = MODULE_CONSTANTS[name]
                originals[(mod, attr)] = getattr(mod, attr)
                setattr(mod, attr, value)
        yield
    finally:
        for (mod, attr), original in originals.items():
            setattr(mod, attr, original)
