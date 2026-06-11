"""Constant override injection for benchmark parameter sweeps.

Provides a context manager that applies overrides using the appropriate
injection pattern for each constant category, and restores originals on exit.

Triple-patch strategy: overrides are applied to (a) the centralized
``Defaults`` class, (b) module-level aliases that functions read at
runtime, and (c) class attributes on mixins that cache ``Defaults.*``
values at class-body time. This ensures overrides take effect regardless
of whether code reads ``Defaults.X``, the bare module-level name, or a
cached mixin class attribute.

As of 2026-04-17, ``WriteFilterMixin`` and ``PredictionLedgerMixin`` now
read ``Defaults.*`` via properties at attribute-access time, so the
``CLASS_ATTR_CONSTANTS`` branch below is mostly a belt-and-suspenders
guard — but it remains authoritative and future-proof for any mixin
that still caches at class-body time or for subclasses that define
their own class-level attribute.
"""

import contextlib
import logging
from typing import Any, Dict, Generator

from src.popoto.fields.constants import Defaults

import src.popoto.fields.observation as observation_mod
import src.popoto.recipes.policy_cache as policy_cache_mod
import src.popoto.recipes.context_assembler as context_assembler_mod
from src.popoto.fields.prediction_ledger import PredictionLedgerMixin
from src.popoto.fields.write_filter import WriteFilterMixin

logger = logging.getLogger("POPOTO.Overrides")

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
    # Float-boundary tolerance for the auto-discharge comparison (#407).
    # Internal constant, but it has a module-level alias in observation.py,
    # so it is registered here for sync-test coverage and restore symmetry.
    "CONFIDENCE_EPSILON": (observation_mod, "CONFIDENCE_EPSILON"),
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

# Registry mapping override names to the Defaults.* attribute they patch.
# These constants do not have a module-level alias — they are read from
# ``Defaults`` directly, either via a runtime property (WriteFilterMixin,
# PredictionLedgerMixin after 2026-04-17 refactor) or as a class-body cached
# attribute on a mixin. Patching ``Defaults`` here covers both the runtime
# lookup path AND keeps restore symmetric.
#
# Maps override_name -> Defaults_attr_name. Both attr-style (``_wf_min_threshold``)
# and uppercase-style (``WF_MIN_THRESHOLD``) keys are supported so sweeps that
# use either naming convention resolve correctly.
CLASS_ATTR_CONSTANTS = {
    # WriteFilterMixin
    "_wf_min_threshold": "WF_MIN_THRESHOLD",
    "_wf_priority_threshold": "WF_PRIORITY_THRESHOLD",
    "WF_MIN_THRESHOLD": "WF_MIN_THRESHOLD",
    "WF_PRIORITY_THRESHOLD": "WF_PRIORITY_THRESHOLD",
    # PredictionLedgerMixin
    "PL_CONFIDENCE_ERROR_THRESHOLD": "PL_CONFIDENCE_ERROR_THRESHOLD",
    "PL_CONFIDENCE_LOW_SIGNAL": "PL_CONFIDENCE_LOW_SIGNAL",
    "PL_AUTO_RESOLVE_ACTED": "PL_AUTO_RESOLVE_ACTED",
    "PL_AUTO_RESOLVE_DISMISSED": "PL_AUTO_RESOLVE_DISMISSED",
    "PL_AUTO_RESOLVE_CONTRADICTED": "PL_AUTO_RESOLVE_CONTRADICTED",
    # DecayingSortedField / ConfidenceField / CoOccurrenceField — these
    # accept kwargs, but the family-factory scenarios (and Tier 1 sweeps)
    # pass the short constant names through ``self.overrides`` so field
    # constructors pick them up. Also patch Defaults so any runtime reads
    # of DECAY_RATE / INITIAL_CONFIDENCE / CO_OCCURRENCE_* observe the
    # override.
    "decay_rate": "DECAY_RATE",
    "DECAY_RATE": "DECAY_RATE",
    "initial_confidence": "INITIAL_CONFIDENCE",
    "INITIAL_CONFIDENCE": "INITIAL_CONFIDENCE",
    "decay_factor": "CO_OCCURRENCE_DECAY_FACTOR",
    "CO_OCCURRENCE_DECAY_FACTOR": "CO_OCCURRENCE_DECAY_FACTOR",
    "initial_weight": "CO_OCCURRENCE_INITIAL_WEIGHT",
    "CO_OCCURRENCE_INITIAL_WEIGHT": "CO_OCCURRENCE_INITIAL_WEIGHT",
    "decay_per_hop": "CO_OCCURRENCE_DECAY_PER_HOP",
    "CO_OCCURRENCE_DECAY_PER_HOP": "CO_OCCURRENCE_DECAY_PER_HOP",
    # Note: `delta` is a field-constructor kwarg for CyclicDecayField — it
    # has no corresponding Defaults.* constant. Included in the registry
    # as a no-op (None) marker so sweeps that reference it don't hit the
    # unknown-name path.
    "delta": None,
    # MemoryLifecycle (recipes/memory_lifecycle.py)
    # Patching Defaults.LIFECYCLE_* propagates to MemoryLifecycle class
    # attributes, which are initialized from Defaults at class-body time.
    # Runtime reads via lifecycle instance attributes also pick up the patch
    # because MemoryLifecycle reads Defaults at class-body time (same as
    # WriteFilterMixin's pre-2026-04-17 pattern).
    "LIFECYCLE_PROMOTION_ACCESS_COUNT": "LIFECYCLE_PROMOTION_ACCESS_COUNT",
    "LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD": "LIFECYCLE_PROMOTION_CONFIDENCE_THRESHOLD",
    "LIFECYCLE_PROMOTION_MIN_AGE_SECONDS": "LIFECYCLE_PROMOTION_MIN_AGE_SECONDS",
    "LIFECYCLE_FORGET_IMPORTANCE_FLOOR": "LIFECYCLE_FORGET_IMPORTANCE_FLOOR",
    "LIFECYCLE_FORGET_IDLE_SECONDS": "LIFECYCLE_FORGET_IDLE_SECONDS",
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
    # PredictionLedger
    "PL_CONFIDENCE_ERROR_THRESHOLD": (0.0, 1.0, False, True),
    "PL_CONFIDENCE_LOW_SIGNAL": (0.0, 1.0, True, True),
    "PL_AUTO_RESOLVE_ACTED": (0.0, 1.0, True, True),
    "PL_AUTO_RESOLVE_DISMISSED": (0.0, 1.0, True, True),
    "PL_AUTO_RESOLVE_CONTRADICTED": (0.0, 1.0, True, True),
    # PolicyCache (already in MODULE_CONSTANTS but missing valid ranges)
    "CHI_SQUARED_P_THRESHOLD": (0.001, 0.5, True, True),
    "INITIAL_CYCLE_AMPLITUDE": (0.0, 5.0, False, True),
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


def _resolve_defaults_attr(name: str) -> str | None:
    """Return the ``Defaults`` attribute name for an override key, or None.

    Resolution order:
    1. If the name is in ``MODULE_CONSTANTS``, the module-level attr name is
       also the Defaults attribute name (mirrored registry invariant).
    2. If the name is in ``CLASS_ATTR_CONSTANTS``, use the mapped name
       (which may be ``None`` for override keys that intentionally have
       no Defaults attribute — e.g. ``delta``).
    3. Fallback: ``name.upper()`` if it exists on ``Defaults`` (legacy
       discovery path retained so callers that pass uppercase
       Defaults-style names like ``DECAY_RATE`` still work).
    4. None if the name is unknown.
    """
    if name in MODULE_CONSTANTS:
        _mod, attr = MODULE_CONSTANTS[name]
        return attr
    if name in CLASS_ATTR_CONSTANTS:
        return CLASS_ATTR_CONSTANTS[name]
    if hasattr(Defaults, name.upper()):
        return name.upper()
    return None


def _is_known_override(name: str) -> bool:
    """True if *name* is registered in any override channel.

    Used by ``test_all_registered_names_dont_raise`` and as the gate for
    the unknown-name warning path in ``apply_overrides``.
    """
    if name in MODULE_CONSTANTS or name in CLASS_ATTR_CONSTANTS:
        return True
    return hasattr(Defaults, name.upper())


@contextlib.contextmanager
def apply_overrides(overrides: Dict[str, Any]) -> Generator[None, None, None]:
    """Context manager to apply constant overrides and restore on exit.

    Triple-patch strategy:
    - ``MODULE_CONSTANTS``: setattr on the module AND on ``Defaults``
      (so functions that import the name directly see the override, and
      code that reads ``Defaults.X`` also sees it).
    - ``CLASS_ATTR_CONSTANTS``: setattr on ``Defaults`` only. After the
      2026-04-17 refactor, ``WriteFilterMixin`` and
      ``PredictionLedgerMixin`` read ``Defaults.*`` at attribute-access
      time via properties, so patching ``Defaults`` is sufficient.
    - Field constructor kwargs / subclass class attributes: stored in the
      overrides dict for the scenario to read.

    Unknown override names (not in either registry and no matching
    ``Defaults.<NAME_UPPER>`` attribute) are logged as a warning and
    ignored. This preserves backward-compatibility with the prior
    silent-no-op behavior documented in 2026-04-17 critique C3. A future
    release may promote this to ``KeyError``; the transition behavior is
    covered by ``tests/benchmarks/test_overrides_reach.py``.

    Args:
        overrides: Mapping of constant name to override value.

    Yields:
        None. Constants are patched for the duration.
    """
    originals_module: Dict = {}
    originals_defaults: Dict[str, Any] = {}

    try:
        for name, value in overrides.items():
            patched = False

            if name in MODULE_CONSTANTS:
                # Patch module-level alias
                mod, attr = MODULE_CONSTANTS[name]
                originals_module[(mod, attr)] = getattr(mod, attr)
                setattr(mod, attr, value)
                patched = True

                # Patch Defaults class (same attr name)
                if hasattr(Defaults, attr) and attr not in originals_defaults:
                    originals_defaults[attr] = getattr(Defaults, attr)
                setattr(Defaults, attr, value)

            defaults_attr = _resolve_defaults_attr(name)
            if defaults_attr and hasattr(Defaults, defaults_attr):
                # Save the original once per Defaults attr (first-write wins).
                if defaults_attr not in originals_defaults:
                    originals_defaults[defaults_attr] = getattr(Defaults, defaults_attr)
                setattr(Defaults, defaults_attr, value)
                patched = True
            elif (
                defaults_attr is None
                and name in CLASS_ATTR_CONSTANTS
                and CLASS_ATTR_CONSTANTS[name] is None
            ):
                # Registered no-op (e.g. ``delta``) — value is consumed by the
                # scenario via the overrides dict itself.
                patched = True

            if not patched:
                logger.warning(
                    "apply_overrides: unknown override name %r — "
                    "no MODULE_CONSTANTS, CLASS_ATTR_CONSTANTS, or "
                    "Defaults.<NAME_UPPER> match. Ignored. A future "
                    "release may raise KeyError here.",
                    name,
                )

        yield
    finally:
        for (mod, attr), original in originals_module.items():
            setattr(mod, attr, original)
        for attr, original in originals_defaults.items():
            setattr(Defaults, attr, original)
