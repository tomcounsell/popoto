"""
Expression-Based Query Support for Popoto Redis ORM.

This module provides the Expression class that enables intuitive Python comparison
syntax for building queries. Instead of using Django-style keyword arguments,
users can write natural Python comparisons directly on Field objects.

Design Philosophy:
-----------------
Expression-based queries provide a more Pythonic alternative to keyword argument
filters. Both approaches are valid and can be mixed:

    # Keyword argument style (existing)
    Restaurant.query.filter(rating__gt=4.0, status="active")

    # Expression style (new)
    Restaurant.query.filter(Restaurant.rating > 4.0, Restaurant.status == "active")

    # Mixed style
    Restaurant.query.filter(Restaurant.rating > 4.0, status="active")

Expressions support combination using & (AND) and | (OR) operators, creating
compound queries that are converted to the underlying filter kwargs format.

Implementation Notes:
--------------------
The Expression class works by:
1. Field comparison operators (__gt__, __lt__, etc.) return Expression instances
2. Expressions store the field name, operator, and comparison value
3. When passed to filter(), expressions are converted to kwargs format
4. Combined expressions (using &/|) create nested structures that resolve to
   multiple filter operations with set intersection/union

Example:
--------
    # Single expression
    expr = Restaurant.rating > 4.0
    # Becomes: {"rating__gt": 4.0}

    # Combined expressions
    expr = (Restaurant.rating > 4.0) & (Restaurant.status == "active")
    # Becomes intersection of two filter results

See Also:
---------
- `popoto.fields.field.Field` - Comparison operators defined here
- `popoto.models.query.Query.filter` - Handles Expression objects in args
"""


class Expression:
    """Represents a field comparison expression for query building.

    An Expression captures a single comparison operation between a field and
    a value. It stores the field name, comparison operator, and value, which
    can later be converted to the kwargs format used by Query.filter().

    Expressions are created automatically when using comparison operators on
    Field instances (when accessed as class attributes, not instance values).

    Attributes:
        field_name: The name of the field being compared
        operator: The comparison operator ('__eq', '__gt', '__lt', '__gte', '__lte', '__ne')
        value: The value being compared against

    Example:
        # Created via Field comparison operators
        expr = MyModel.price > 100  # Expression(field_name='price', operator='__gt', value=100)

        # Convert to kwargs for filter()
        expr.to_kwargs()  # {'price__gt': 100}

        # Combine expressions
        combined = (MyModel.price > 100) & (MyModel.active == True)
    """

    def __init__(self, field_name: str, operator: str, value):
        """Initialize an Expression.

        Args:
            field_name: The name of the field (e.g., 'price', 'status')
            operator: The comparison operator string ('__eq', '__gt', '__lt',
                     '__gte', '__lte', '__ne')
            value: The value to compare against
        """
        self.field_name = field_name
        self.operator = operator
        self.value = value

    def __and__(self, other: "Expression") -> "CombinedExpression":
        """Combine this expression with another using AND logic.

        Args:
            other: Another Expression or CombinedExpression

        Returns:
            A CombinedExpression that represents the AND of both expressions

        Example:
            (Model.price > 100) & (Model.active == True)
        """
        return CombinedExpression(self, other, connector="AND")

    def __or__(self, other: "Expression") -> "CombinedExpression":
        """Combine this expression with another using OR logic.

        Args:
            other: Another Expression or CombinedExpression

        Returns:
            A CombinedExpression that represents the OR of both expressions

        Example:
            (Model.status == "active") | (Model.status == "pending")
        """
        return CombinedExpression(self, other, connector="OR")

    def __repr__(self) -> str:
        """Return a readable representation of this expression."""
        return f"Expression({self.field_name}{self.operator}={self.value!r})"

    def to_kwargs(self) -> dict:
        """Convert this expression to filter kwargs format.

        Returns:
            A dictionary suitable for passing to Query.filter() as kwargs.
            For equality checks, returns {field_name: value}.
            For other operators, returns {field_name + operator: value}.

        Example:
            Expression('price', '__gt', 100).to_kwargs()  # {'price__gt': 100}
            Expression('status', '__eq', 'active').to_kwargs()  # {'status': 'active'}
        """
        if self.operator == "__eq":
            return {self.field_name: self.value}
        else:
            return {f"{self.field_name}{self.operator}": self.value}

    def to_q(self):
        """Convert this expression to a Q object.

        Returns:
            A Q object representing this expression.

        Example:
            Expression('price', '__gt', 100).to_q()  # Q(price__gt=100)
        """
        from .q import Q

        return Q(**self.to_kwargs())


class CombinedExpression:
    """Represents multiple expressions combined with AND or OR logic.

    CombinedExpressions are created when using & or | operators between
    Expression objects. They can be nested to create complex query logic.

    When evaluated, AND combinations perform set intersection on results,
    while OR combinations perform set union.

    Attributes:
        left: The left-hand Expression or CombinedExpression
        right: The right-hand Expression or CombinedExpression
        connector: Either "AND" or "OR"

    Example:
        # AND combination
        expr = (Model.price > 100) & (Model.active == True)

        # OR combination
        expr = (Model.status == "active") | (Model.status == "pending")

        # Nested combination
        expr = ((Model.price > 100) & (Model.active == True)) | (Model.featured == True)
    """

    def __init__(self, left, right, connector: str = "AND"):
        """Initialize a CombinedExpression.

        Args:
            left: The left-hand Expression or CombinedExpression
            right: The right-hand Expression or CombinedExpression
            connector: The logical connector, either "AND" or "OR"
        """
        self.left = left
        self.right = right
        self.connector = connector

    def __and__(self, other) -> "CombinedExpression":
        """Combine this expression with another using AND logic."""
        return CombinedExpression(self, other, connector="AND")

    def __or__(self, other) -> "CombinedExpression":
        """Combine this expression with another using OR logic."""
        return CombinedExpression(self, other, connector="OR")

    def __repr__(self) -> str:
        """Return a readable representation of this combined expression."""
        return f"({self.left!r} {self.connector} {self.right!r})"

    def get_all_expressions(self) -> list:
        """Recursively collect all leaf Expression objects.

        Returns:
            A list of all Expression objects in this tree.
        """
        expressions = []

        if isinstance(self.left, Expression):
            expressions.append(self.left)
        elif isinstance(self.left, CombinedExpression):
            expressions.extend(self.left.get_all_expressions())

        if isinstance(self.right, Expression):
            expressions.append(self.right)
        elif isinstance(self.right, CombinedExpression):
            expressions.extend(self.right.get_all_expressions())

        return expressions

    def to_q(self):
        """Convert this combined expression to a Q object.

        Recursively converts the expression tree to Q objects combined
        with the appropriate AND/OR operators.

        Returns:
            A Q object representing this combined expression.

        Example:
            ((Model.price > 100) & (Model.active == True)).to_q()
            # Returns: Q(price__gt=100) & Q(active=True)
        """

        # Convert left side
        if isinstance(self.left, Expression):
            left_q = self.left.to_q()
        else:  # CombinedExpression
            left_q = self.left.to_q()

        # Convert right side
        if isinstance(self.right, Expression):
            right_q = self.right.to_q()
        else:  # CombinedExpression
            right_q = self.right.to_q()

        # Combine with appropriate operator
        if self.connector == "AND":
            return left_q & right_q
        else:  # OR
            return left_q | right_q
