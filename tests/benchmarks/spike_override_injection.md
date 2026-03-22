# Spike: Parameter Override Injection

## Summary

Validated override injection mechanisms for all ~25 Category 1 constants.
Three injection patterns exist; all are usable without source refactoring.

## Findings by Category

### Pattern A: Constructor kwargs (field-level)
Override at model definition time. Clean, type-safe.

| Constant | Location | Override |
|----------|----------|---------|
| `decay_rate` | DecayingSortedField | `DecayingSortedField(decay_rate=X)` |
| `initial_confidence` | ConfidenceField | `ConfidenceField(initial_confidence=X)` |
| `decay_factor` | CoOccurrenceField | `CoOccurrenceField(decay_factor=X)` |

### Pattern B: Class attributes (mixin-level)
Override via subclass attribute. Clean, inheritable.

| Constant | Location | Override |
|----------|----------|---------|
| `_wf_min_threshold` | WriteFilterMixin | `class M(WriteFilterMixin, Model): _wf_min_threshold = X` |
| `_wf_priority_threshold` | WriteFilterMixin | Same pattern |
| `_pl_confidence_error_threshold` | PredictionLedgerMixin | Same pattern |
| `_pl_confidence_low_signal` | PredictionLedgerMixin | Same pattern |
| `_pl_auto_resolve_errors` | PredictionLedgerMixin | Same pattern |

### Pattern C: Module-level constants
Override via monkeypatch (pytest `monkeypatch.setattr` or direct assignment).

| Constant | Module | Override |
|----------|--------|---------|
| `MIN_EVENTS_FOR_CRYSTALLIZATION` | `popoto.recipes.policy_cache` | `monkeypatch.setattr(pc, 'MIN_EVENTS_FOR_CRYSTALLIZATION', X)` |
| `WILSON_CI_THRESHOLD` | `popoto.recipes.policy_cache` | Same pattern |
| `TD_ALPHA` | `popoto.recipes.policy_cache` | Same pattern |
| `TD_GAMMA` | `popoto.recipes.policy_cache` | Same pattern |
| `CHI_SQUARED_P_THRESHOLD` | `popoto.recipes.policy_cache` | Same pattern |
| `INITIAL_CYCLE_AMPLITUDE` | `popoto.recipes.policy_cache` | Same pattern |
| `COMPETITIVE_SUPPRESSION_SIGNAL` | `popoto.recipes.context_assembler` | Same pattern |
| `DEFAULT_SURFACING_THRESHOLD` | `popoto.recipes.context_assembler` | Same pattern |

### Pattern D: Method parameter defaults (call-site)
Must be passed at each call site. The harness wraps these calls.

| Constant | Method | Override |
|----------|--------|---------|
| `initial_weight` | `CoOccurrenceField.link()` | Pass `initial_weight=X` |
| `delta` | `CoOccurrenceField.strengthen()` | Pass `delta=X` |
| `decay_per_hop` | `CoOccurrenceField.propagate()` | Pass `decay_per_hop=X` |

### Inline Literals (ObservationProtocol) -- REFACTORED

The following were inline literals in `observation.py` module-level functions.
Refactored to module-level constants so the harness can override them cleanly:

| Constant | Default | Function | New Module Constant |
|----------|---------|----------|-------------------|
| Acted confidence signal | 0.9 | `_apply_acted()` | `ACTED_CONFIDENCE_SIGNAL` |
| Contradicted confidence signal | 0.1 | `_apply_contradicted()` | `CONTRADICTED_CONFIDENCE_SIGNAL` |
| Acted cycle strengthen factor | 1.2 | `_apply_acted()` | `ACTED_CYCLE_STRENGTHEN_FACTOR` |
| Dismissed cycle weaken factor | 0.8 | `_apply_dismissed()` | `DISMISSED_CYCLE_WEAKEN_FACTOR` |
| Contradicted cycle weaken factor | 0.5 | `_apply_contradicted()` | `CONTRADICTED_CYCLE_WEAKEN_FACTOR` |
| Auto-discharge confidence threshold | 0.1 | `_apply_contradicted()` | `AUTO_DISCHARGE_CONFIDENCE_THRESHOLD` |

## Conclusion

All 25+ constants are overridable without invasive changes. The benchmark
harness will use a `ConstantOverrides` context manager that applies the
appropriate injection pattern for each constant category.
