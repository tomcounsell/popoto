"""Safety utilities for values crossing the Python-to-Redis/Lua boundary.

Lua's ``tonumber("None")`` returns ``nil`` and causes crashes.
Redis LIMIT clauses expect integers, not ``float('inf')``.
This module provides ``_to_redis_limit()`` to convert Python unlimited
representations to safe bounded integers before they reach Redis.
"""

import logging

_observe_logger = logging.getLogger("popoto.observe.redis_limits")

# Absolute maximum that Redis can safely handle in LIMIT/score contexts.
_REDIS_SAFE_MAX = 999_999_999


def _to_redis_limit(value, default=_REDIS_SAFE_MAX):
    """Convert Python unlimited representations to safe Redis integers.

    Args:
        value: An int, float, or None. None and float('inf') are treated
            as "unlimited" and replaced with *default*.
        default: Safe integer ceiling. Default 999_999_999.

    Returns:
        int: A bounded integer safe for Redis LIMIT clauses and Lua
            ``tonumber()`` calls.
    """
    if value is None or value == float("inf"):
        _observe_logger.debug(
            "redis_limit_converted value=%s default=%s", value, default
        )
        return int(default)
    return int(min(value, default))
