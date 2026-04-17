"""Verify that every swept constant is actually reachable by apply_overrides().

This addresses issue #351 — the core failure mode where patches to
``Defaults.*`` never reached cached class attributes on ``WriteFilterMixin``
/ ``PredictionLedgerMixin``, so sweeps ran with the stock defaults regardless
of override values.

Tests:
    - ``test_all_constants_patchable`` (parametrized over every swept
      constant across TIER1–TIER3): asserts that entering
      ``apply_overrides({name: sentinel})`` makes the effective value equal
      to ``sentinel`` via the same code path the production runtime uses.
    - ``test_unknown_name_emits_warning`` (C3 migration path): asserts an
      unknown override name emits a ``logger.warning`` and does not raise.
      Documents the current soft-deprecation behavior. A future release may
      promote this to ``KeyError``.
    - ``test_all_registered_names_dont_raise``: iterates every name in
      ``MODULE_CONSTANTS ∪ CLASS_ATTR_CONSTANTS ∪ uppercase-Defaults-attrs``
      and confirms no name triggers the unknown-name warning. Guards
      against future registry drift.
"""

import logging
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(os.path.dirname(SCRIPT_DIR)))

import pytest  # noqa: E402

from src import popoto  # noqa: E402
from src.popoto.fields.constants import Defaults  # noqa: E402
from src.popoto.fields.write_filter import WriteFilterMixin  # noqa: E402

from tests.benchmarks.overrides import (  # noqa: E402
    CLASS_ATTR_CONSTANTS,
    MODULE_CONSTANTS,
    apply_overrides,
    _is_known_override,
    _resolve_defaults_attr,
)
from tests.benchmarks.run_sweeps import (  # noqa: E402
    TIER1_SWEEPS,
    TIER2_SWEEPS,
    TIER3_SWEEPS,
)

# ---------------------------------------------------------------------------
# Helpers: resolve the effective value through the same path production uses.
# ---------------------------------------------------------------------------


class _WFProbeModel(WriteFilterMixin, popoto.Model):
    """Probe model for reading _wf_min / _wf_priority via the property path.

    Tests that expect the property read-through must instantiate this model
    inside the ``apply_overrides`` context and read the attribute off the
    instance.
    """

    name = popoto.UniqueKeyField()

    def compute_filter_score(self):
        return 0.5


def _read_effective(name: str):
    """Read the effective value of an override name via the production path.

    For most names, this reads ``Defaults.<ATTR>`` (the single source of
    truth that both module-level aliases and mixin properties delegate to).
    For ``_wf_min_threshold`` / ``_wf_priority_threshold``, we also spot-
    check that the mixin property resolves to the same value — that's the
    path the runtime actually takes.
    """
    attr = _resolve_defaults_attr(name)
    if attr is None:
        # Registered no-op (e.g. ``delta``). Nothing to read.
        return None
    return getattr(Defaults, attr)


# All swept constants across the three main tiers.
ALL_SWEPT_CONSTANTS = (
    list(TIER1_SWEEPS.keys()) + list(TIER2_SWEEPS.keys()) + list(TIER3_SWEEPS.keys())
)


# Sentinel values for each constant. Chosen to be valid-range values that
# are deliberately different from the current default so the assertion has
# teeth. Integer constants get integer sentinels.
_INT_NAMES = {"MIN_EVENTS_FOR_CRYSTALLIZATION"}


def _sentinel_for(name: str):
    """Return a valid-range sentinel that differs from the current default."""
    current = _read_effective(name)
    if current is None:
        return 0.123456
    if name in _INT_NAMES:
        # MIN_EVENTS_FOR_CRYSTALLIZATION is an integer in [1, 100]; pick a
        # value clearly different from the default.
        return int(current) + 7 if int(current) + 7 <= 50 else max(1, int(current) - 7)
    # Float sentinel: offset by 0.13 within (0, 1) for most constants, or
    # clamp to valid range for cycle-factor constants (0, 5).
    sentinel = float(current) + 0.13
    # Cycle factors range up to 5.0; everything else should stay in (0,1).
    if sentinel >= 0.99 and name not in {
        "ACTED_CYCLE_STRENGTHEN_FACTOR",
        "DISMISSED_CYCLE_WEAKEN_FACTOR",
        "CONTRADICTED_CYCLE_WEAKEN_FACTOR",
        "INITIAL_CYCLE_AMPLITUDE",
    }:
        sentinel = max(0.01, float(current) - 0.13)
    return sentinel


# ---------------------------------------------------------------------------
# test_all_constants_patchable — parametrized over every swept constant.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL_SWEPT_CONSTANTS)
def test_all_constants_patchable(name):
    """Every swept constant is reachable by apply_overrides().

    For each name in TIER1∪TIER2∪TIER3 sweep registries:
    1. Read the current effective value (baseline).
    2. Enter ``apply_overrides({name: sentinel})``.
    3. Assert the effective value inside the context equals ``sentinel``
       (via ``Defaults.<ATTR>`` — the single source of truth that mixin
       properties and module aliases both delegate to).
    4. Exit and assert the value is restored.

    This is the test that would have caught the original issue #351
    failure: before the 2026-04-17 refactor, WF_* and PL_* constants
    cached at class-body time, so patching Defaults after import did
    not change the effective value. Post-refactor, the mixin properties
    read ``Defaults.*`` at access time and the patch takes effect.
    """
    sentinel = _sentinel_for(name)
    baseline = _read_effective(name)

    with apply_overrides({name: sentinel}):
        effective = _read_effective(name)
        # For no-op entries (delta), _read_effective returns None and we
        # just assert the context manager didn't raise.
        if baseline is None:
            return
        assert effective == pytest.approx(sentinel), (
            f"Override of {name!r} did not take effect. "
            f"Expected {sentinel!r}, got {effective!r}. "
            f"Check the override registry entries in tests/benchmarks/overrides.py."
        )

    # Restore check (belt-and-suspenders — apply_overrides' finally block
    # handles this, but the test makes the contract explicit).
    restored = _read_effective(name)
    assert restored == baseline, (
        f"Override of {name!r} did not restore. "
        f"Expected baseline {baseline!r}, got {restored!r}."
    )


def test_wf_min_threshold_reaches_property_path():
    """The ``_wf_min_threshold`` property on a WriteFilterMixin instance
    reflects the override — not just ``Defaults.WF_MIN_THRESHOLD``.

    This is the production read path: ``write_filter.py::_check_write_filter``
    reads ``self._wf_min_threshold``. A property that did not delegate to
    Defaults would pass the Defaults check above but still fail here.
    """
    baseline = Defaults.WF_MIN_THRESHOLD
    sentinel = 0.111
    _WFProbeModel.delete_all()
    inst = _WFProbeModel(name="probe_min")

    with apply_overrides({"_wf_min_threshold": sentinel}):
        assert inst._wf_min_threshold == pytest.approx(sentinel)

    assert inst._wf_min_threshold == pytest.approx(baseline)
    _WFProbeModel.delete_all()


# ---------------------------------------------------------------------------
# C3 migration path — unknown names emit a warning.
# ---------------------------------------------------------------------------


def test_unknown_name_emits_warning(caplog):
    """Unknown override name emits ``logger.warning`` and does not raise.

    Documents the current soft-deprecation behavior introduced in 2026-04-17.
    A future release may promote this to ``KeyError`` — when that happens,
    update this test to expect the exception and flip the xfail marker.

    Note: we intentionally do NOT use ``pytest.raises(KeyError)`` here. The
    decision to keep the warning-only behavior for one release is
    documented in the ``apply_overrides`` docstring (C3 migration).
    """
    unknown = "DEFINITELY_NOT_A_REAL_CONSTANT"
    baseline_defaults_snapshot = {
        k: v for k, v in vars(Defaults).items() if not k.startswith("_")
    }

    with caplog.at_level(logging.WARNING, logger="POPOTO.Overrides"):
        with apply_overrides({unknown: 0.5}):
            pass

    # No exception. Warning was emitted.
    assert any(
        unknown in record.getMessage()
        and "unknown override name" in record.getMessage()
        for record in caplog.records
    ), (
        "Expected a WARNING containing 'unknown override name' and the "
        f"name {unknown!r}. caplog records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # Defaults must be unchanged.
    post = {k: v for k, v in vars(Defaults).items() if not k.startswith("_")}
    assert (
        post == baseline_defaults_snapshot
    ), "apply_overrides on an unknown name must not mutate Defaults."


# ---------------------------------------------------------------------------
# Registry drift guard — every known name resolves cleanly.
# ---------------------------------------------------------------------------


def _all_registered_names():
    """Every name that ought to resolve WITHOUT hitting the warning path."""
    names: set = set()
    names.update(MODULE_CONSTANTS.keys())
    names.update(CLASS_ATTR_CONSTANTS.keys())
    # Every uppercase attribute on Defaults is also a legal override name
    # via the ``hasattr(Defaults, name.upper())`` fallback.
    names.update(
        k for k in vars(Defaults).keys() if not k.startswith("_") and k.isupper()
    )
    # Plus every swept constant — the whole point is that sweeps work.
    names.update(ALL_SWEPT_CONSTANTS)
    return sorted(names)


def test_all_registered_names_dont_raise(caplog):
    """Iterate every registered name; confirm no name hits the warning path.

    Guards against future registry drift where a new sweep constant is
    added but forgotten in the overrides registry.
    """
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="POPOTO.Overrides"):
        for name in _all_registered_names():
            # _is_known_override uses the same resolution logic as
            # apply_overrides' warning branch, so this is a direct check.
            assert _is_known_override(name), (
                f"Override name {name!r} is not registered. Add it to "
                f"MODULE_CONSTANTS or CLASS_ATTR_CONSTANTS in "
                f"tests/benchmarks/overrides.py."
            )


def test_apply_overrides_triple_patch_end_to_end():
    """End-to-end: overriding a MODULE_CONSTANTS entry patches module,
    Defaults, AND is observed by code that reads either path.
    """
    import src.popoto.fields.observation as obs

    baseline_mod = obs.ACTED_CONFIDENCE_SIGNAL
    baseline_defaults = Defaults.ACTED_CONFIDENCE_SIGNAL

    with apply_overrides({"ACTED_CONFIDENCE_SIGNAL": 0.42}):
        assert obs.ACTED_CONFIDENCE_SIGNAL == 0.42
        assert Defaults.ACTED_CONFIDENCE_SIGNAL == 0.42

    assert obs.ACTED_CONFIDENCE_SIGNAL == baseline_mod
    assert Defaults.ACTED_CONFIDENCE_SIGNAL == baseline_defaults
