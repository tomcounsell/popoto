"""
Q Objects for Complex Query Expressions.

This module provides Django-style Q objects that enable complex query logic
with OR, AND, and NOT operators. Q objects can be combined using Python
operators to build expressive filter expressions.

Usage Examples:
--------------
    from popoto import Q

    # OR logic - find active OR premium users
    users = User.query.filter(Q(status="active") | Q(type="premium"))

    # AND logic (explicit) - same as passing multiple kwargs
    users = User.query.filter(Q(status="active") & Q(rating__gt=4.0))

    # Complex combinations - active OR premium, with high rating
    users = User.query.filter(
        (Q(status="active") | Q(type="premium")) & Q(rating__gt=3.0)
    )

    # Negation - find users who are NOT inactive
    users = User.query.filter(~Q(status="inactive"))

Design Notes:
------------
Q objects build an expression tree that is evaluated at query time. Each Q
object can be either:

1. A leaf node containing filter kwargs (e.g., Q(status="active"))
2. An internal node combining child Q objects with AND/OR connectors

The Query class evaluates Q expressions by recursively computing result sets:
- AND: set intersection of children's results
- OR: set union of children's results
- NOT: set difference from all keys

This approach leverages Redis's efficient set operations while providing
a familiar Django-like query interface.
"""


class Q:
    """Django-style Q object for complex query expressions.

    Q objects enable OR logic and complex query combinations that aren't
    possible with simple kwargs. They can be combined using Python operators:

    - ``|`` (OR): Union of results
    - ``&`` (AND): Intersection of results
    - ``~`` (NOT): Negation (exclusion from all results)

    Attributes:
        filters: Dict of filter kwargs for leaf nodes
        connector: 'AND' or 'OR' for combined Q objects
        children: List of child Q objects for combined expressions
        negated: Whether this Q object is negated

    Example:
        # Basic Q object
        q = Q(status="active")

        # Combined with OR
        q = Q(status="active") | Q(type="premium")

        # Negated
        q = ~Q(status="inactive")

        # Complex expression
        q = (Q(status="active") | Q(type="premium")) & ~Q(rating__lt=2.0)
    """

    AND = "AND"
    OR = "OR"

    def __init__(self, **kwargs):
        """Create a Q object with filter kwargs.

        Args:
            **kwargs: Filter parameters matching those supported by
                     Model.query.filter(). These become the leaf-level
                     filter criteria.

        Example:
            Q(status="active")
            Q(price__gte=10.0, price__lte=100.0)
            Q(category__in=["electronics", "books"])
        """
        self.filters = kwargs
        self.connector = self.AND
        self.children = []
        self.negated = False

    def __or__(self, other):
        """Combine two Q objects with OR logic.

        Creates a new Q object that matches if EITHER operand matches.
        The result is evaluated as a set union of matching keys.

        Args:
            other: Another Q object to OR with this one

        Returns:
            A new Q object representing (self OR other)

        Example:
            q = Q(status="active") | Q(type="premium")
        """
        if not isinstance(other, Q):
            raise TypeError(f"Cannot OR Q object with {type(other).__name__}")
        combined = Q()
        combined.connector = self.OR
        combined.children = [self, other]
        return combined

    def __and__(self, other):
        """Combine two Q objects with AND logic.

        Creates a new Q object that matches only if BOTH operands match.
        The result is evaluated as a set intersection of matching keys.

        Args:
            other: Another Q object to AND with this one

        Returns:
            A new Q object representing (self AND other)

        Example:
            q = Q(status="active") & Q(rating__gt=4.0)
        """
        if not isinstance(other, Q):
            raise TypeError(f"Cannot AND Q object with {type(other).__name__}")
        combined = Q()
        combined.connector = self.AND
        combined.children = [self, other]
        return combined

    def __invert__(self):
        """Negate a Q object.

        Creates a new Q object that matches everything EXCEPT what this
        Q object matches. The result is evaluated as a set difference
        from all model keys.

        Returns:
            A new Q object representing NOT(self)

        Example:
            q = ~Q(status="inactive")  # Matches everything except inactive
        """
        negated = Q()
        negated.filters = self.filters.copy()
        negated.connector = self.connector
        negated.children = self.children.copy()
        negated.negated = not self.negated
        return negated

    def __repr__(self):
        """Return a string representation of this Q object for debugging."""
        if self.children:
            connector_str = f" {self.connector} "
            children_repr = connector_str.join(repr(c) for c in self.children)
            result = f"({children_repr})"
        else:
            filters_str = ", ".join(f"{k}={v!r}" for k, v in self.filters.items())
            result = f"Q({filters_str})"

        if self.negated:
            result = f"~{result}"
        return result

    def is_leaf(self):
        """Check if this Q object is a leaf node (has filters, no children).

        Returns:
            True if this Q object has filter kwargs and no children.
        """
        return bool(self.filters) and not self.children

    def is_empty(self):
        """Check if this Q object has no filters and no children.

        Returns:
            True if this Q object is effectively a no-op.
        """
        return not self.filters and not self.children


def evaluate_q(query_instance, q_obj, all_keys=None, *, state=None):
    """Evaluate a Q object expression tree and return matching Redis keys.

    This function recursively evaluates Q object trees, computing result
    sets at each level using set operations:

    - Leaf nodes: Execute filter query and return matching keys
    - AND nodes: Return intersection of children's results
    - OR nodes: Return union of children's results
    - Negated nodes: Return difference from all_keys

    Args:
        query_instance: The Query instance to execute filters against
        q_obj: The Q object to evaluate
        all_keys: Set of all Redis keys for the model (used for negation).
                 If None, will be fetched when needed for NOT operations.
        state: Optional `_PushdownState` carrier. When given, leaf nodes
            route to `query_instance._filter_for_keys_set_with_state(state,
            ...)` so any geo distances a leaf's filter produces accumulate
            into the caller's carrier instead of a throwaway one. Threaded
            down every recursive call unchanged.

    Returns:
        Set of Redis keys matching the Q expression.

    Note:
        This function is called internally by Query.filter() when Q objects
        are passed as arguments. Users should not need to call it directly.
    """
    if q_obj.is_empty():
        # Empty Q() matches everything
        if all_keys is None:
            all_keys = set(query_instance.keys())
        return all_keys

    if q_obj.is_leaf():
        # Leaf node - execute the filter
        if state is not None:
            result = query_instance._filter_for_keys_set_with_state(
                state, **q_obj.filters
            )
        else:
            result = query_instance.filter_for_keys_set(**q_obj.filters)
    elif q_obj.children:
        # Internal node - combine children
        child_results = [
            evaluate_q(query_instance, child, all_keys, state=state)
            for child in q_obj.children
        ]

        if q_obj.connector == Q.AND:
            result = set.intersection(*child_results) if child_results else set()
        else:  # OR
            result = set.union(*child_results) if child_results else set()
    else:
        # Empty filters, no children - return all keys
        if all_keys is None:
            all_keys = set(query_instance.keys())
        result = all_keys

    # Apply negation if needed
    if q_obj.negated:
        if all_keys is None:
            all_keys = set(query_instance.keys())
        result = all_keys - result

    return result
