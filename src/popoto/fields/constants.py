"""Temporal constants for CyclicDecayField cycle definitions.

Provides named constants for standard cycle periods in seconds,
used as the ``period`` parameter in CyclicDecayField cycle tuples.

Example:
    from popoto.fields.constants import TemporalPeriod

    relevance = CyclicDecayField(
        decay_rate=0.5,
        cycles=[(TemporalPeriod.QUARTERLY, 5.0, 0)],
    )
"""


class TemporalPeriod:
    """Named constants for common temporal cycle periods in seconds.

    These values represent the approximate duration of each period:
        DAILY     = 86,400 seconds (24 hours)
        WEEKLY    = 604,800 seconds (7 days)
        MONTHLY   = 2,592,000 seconds (30 days)
        QUARTERLY = 7,776,000 seconds (90 days)
        YEARLY    = 31,536,000 seconds (365 days)
    """

    DAILY = 86_400
    WEEKLY = 604_800
    MONTHLY = 2_592_000
    QUARTERLY = 7_776_000
    YEARLY = 31_536_000


class InteractionWeight:
    """Weight constants for source/role-based importance scoring.

    Two axes combined by addition:
    - Source axis: what kind of entity (HUMAN, AGENT, SYSTEM)
    - Role axis: authority level (EXECUTIVE, MANAGER, PEER, SUBORDINATE)

    With decay_rate=0.5, effective lifetime ~ score^2 days.
    """

    HUMAN = 6.0
    AGENT = 1.0
    SYSTEM = 0.2

    EXECUTIVE = 44.0
    MANAGER = 16.0
    PEER = 6.0
    SUBORDINATE = 1.0

    @staticmethod
    def combine(source: float, role: float) -> float:
        """Return the combined weight for a source/role pair.

        The two axes are additive: a HUMAN (6.0) PEER (6.0) interaction
        yields weight 12.0, while an AGENT (1.0) SUBORDINATE (1.0)
        interaction yields weight 2.0.
        """
        return source + role
