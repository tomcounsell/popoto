"""Popoto Recipes — Reference patterns composing multiple Popoto primitives.

Recipes are self-contained modules that demonstrate how to compose Popoto's
field types, mixins, and utilities into application-level patterns. They
are importable and usable, but designed primarily as reference implementations.
"""

from .adaptive_assembler import AdaptiveAssembler
from .context_assembler import AssemblyResult, ContextAssembler, RetrievalQuality
from .default_memory import DefaultMemory
from .memory_lifecycle import LifecycleState, MemoryLifecycle
from .memory_telemetry import (
    AssemblyEvent,
    TelemetryAnalyzer,
    TelemetryRecorder,
    report_outcomes,
)
from .policy_cache import PolicyEntry, compute_fingerprint, update_q_value
from .subconscious_memory import SubconsciousMemory
from .trajectory_memory import TrajectoryMemory

__all__ = [
    "AdaptiveAssembler",
    "AssemblyEvent",
    "AssemblyResult",
    "ContextAssembler",
    "DefaultMemory",
    "LifecycleState",
    "MemoryLifecycle",
    "PolicyEntry",
    "RetrievalQuality",
    "SubconsciousMemory",
    "TelemetryAnalyzer",
    "TelemetryRecorder",
    "TrajectoryMemory",
    "compute_fingerprint",
    "report_outcomes",
    "update_q_value",
]
