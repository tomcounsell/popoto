"""Popoto Recipes — Reference patterns composing multiple Popoto primitives.

Recipes are self-contained modules that demonstrate how to compose Popoto's
field types, mixins, and utilities into application-level patterns. They
are importable and usable, but designed primarily as reference implementations.
"""

from .context_assembler import AssemblyResult, ContextAssembler
from .policy_cache import PolicyEntry, compute_fingerprint, update_q_value
from .subconscious_memory import SubconsciousMemory

__all__ = [
    "AssemblyResult",
    "ContextAssembler",
    "PolicyEntry",
    "SubconsciousMemory",
    "compute_fingerprint",
    "update_q_value",
]
