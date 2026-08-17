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
            # Derived contraction-invariant cap, not a swept constant (#416);
            # referenced only via Defaults.CO_OCCURRENCE_WEIGHT_CAP.
            "CO_OCCURRENCE_WEIGHT_CAP",
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
            # Confidence-driven forgetting + tombstone retention (#491) —
            # resolved via MemoryLifecycle._LIFECYCLE_ATTRS, same as above.
            "LIFECYCLE_FORGET_CONFIDENCE_CEILING",
            "LIFECYCLE_FORGET_MIN_EVIDENCE",
            "LIFECYCLE_TOMBSTONE_RETENTION_LIMIT",
            # Confidence-modulated decay (#491) — read directly from Defaults
            # in the decay query path; no module-level alias exists, so not in
            # MODULE_CONSTANTS.
            "DECAY_CONFIDENCE_MODULATION_STRENGTH",
            "DECAY_CONFIDENCE_MODULATION_ENABLED",
            # Extraction (extraction/, #461) — read directly from Defaults
            # (popoto.extraction.claude, popoto.recipes.subconscious_memory);
            # no module-level alias exists, so not in MODULE_CONSTANTS.
            "EXTRACTION_DEFAULT_IMPORTANCE",
            "EXTRACTION_DEFAULT_CONFIDENCE",
            "EXTRACTION_ENTITY_PAIR_LINK_WEIGHT",
            "EXTRACTION_MAX_ENTITIES_PER_FACT",
            # TagField scoping kill switch (#492) — read directly from Defaults
            # in the assembler tag-scoping path (context_assembler.py); no
            # module-level alias exists, so not in MODULE_CONSTANTS.
            "TAG_SCOPING_ENABLED",
            # Sorted-range bound over-fetch margin — read directly from
            # Defaults in the query path (models/query.py); no module-level
            # alias exists, so not in MODULE_CONSTANTS.
            "SORTED_PUSHDOWN_OVERFETCH_MARGIN",
            # datetime KeyField identity kill switch (#537/#538) — a
            # deploy-level switch rather than a swept constant, read directly
            # from Defaults in models/canonical_key.py; no module-level alias
            # exists, so not in MODULE_CONSTANTS.
            "DATETIME_KEY_LEGACY",
            # ValidityField gating (#580) — read directly from Defaults at call
            # time by decaying_sorted_field.validity_gate_args and
            # ContextAssembler._resolve_excluded_keys; VALIDITY_OPEN_SENTINEL
            # likewise in validity_field.py. A module-level alias is deliberately
            # NOT created for the kill switch: an alias bound at import time
            # would defeat the whole point of a runtime-flippable deploy switch
            # for adopters who cannot edit model code. So not in
            # MODULE_CONSTANTS, exactly like TAG_SCOPING_ENABLED above.
            # (apply_overrides still reaches both via its
            # Defaults.<NAME_UPPER> fallback, so benchmark sweeps can flip them
            # without any registry entry.)
            "VALIDITY_GATING_ENABLED",
            "VALIDITY_OPEN_SENTINEL",
            # Never-record firewall (#561) — all read directly from Defaults
            # at scan time in privacy/never_record.py; no module-level alias
            # exists, so none are in MODULE_CONSTANTS. They are also
            # deliberately NOT sweepable: the firewall's thresholds are a
            # security posture, not a retrieval-quality dial, and there is no
            # nDCG signal that would make a sweep meaningful for them.
            "NR_ENTROPY_MIN_TOKEN_LEN",
            "NR_ENTROPY_MIN_BITS",
            "NR_ASSIGNMENT_MIN_VALUE_LEN",
            "NR_TOMBSTONE_LOG_MAX",
            # Deploy-level kill switch, like DATETIME_KEY_LEGACY above.
            "NEVER_RECORD_ENABLED",
        }

        expected_in_module = defaults_attrs - field_kwargs_and_class_attrs
        missing = expected_in_module - module_constant_names
        assert not missing, (
            f"Defaults attributes not in MODULE_CONSTANTS: {missing}. "
            f"Either add them to MODULE_CONSTANTS or to field_kwargs_and_class_attrs."
        )
