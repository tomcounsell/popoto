"""Popoto Recipes — Reference patterns composing multiple Popoto primitives.

Recipes are self-contained modules that demonstrate how to compose Popoto's
field types, mixins, and utilities into application-level patterns. They
are importable and usable, but designed primarily as reference implementations.
"""

from .adaptive_assembler import AdaptiveAssembler
from .context_assembler import AssemblyResult, ContextAssembler, RetrievalQuality
from .policy_cache import PolicyEntry, compute_fingerprint, update_q_value
from .subconscious_memory import SubconsciousMemory

__all__ = [
    "AdaptiveAssembler",
    "AssemblyResult",
    "ContextAssembler",
    "PolicyEntry",
    "RetrievalQuality",
    "SubconsciousMemory",
    "compute_fingerprint",
    "update_q_value",
]
