"""
Unique Field Mixin for Popoto Redis ORM.

This module provides the UniqueFieldMixin class, which enforces uniqueness constraints
on field values within a Popoto model. It is designed to be used as a mixin with
KeyField to create fields that require globally unique values across all instances
of a model.

Design Philosophy
-----------------
In relational databases, unique constraints are typically enforced at the database
level with UNIQUE indexes. Redis, being a key-value store, does not have native
unique constraint enforcement. This mixin bridges that gap by ensuring that any
field marked as unique will reject duplicate values at the application layer.

The mixin follows Python's cooperative multiple inheritance pattern, using super()
to work seamlessly with Field and other mixins in the inheritance chain. This allows
UniqueFieldMixin to be combined with KeyFieldMixin to create UniqueKeyField, which
provides both indexing capabilities and uniqueness guarantees.

System Integration
------------------
This mixin works in conjunction with:
- KeyFieldMixin: Provides the indexing infrastructure that UniqueFieldMixin relies on
- Field: The base class that provides core field behavior
- UniqueKeyField (shortcuts.py): The primary consumer of this mixin's behavior

Note: In the current implementation, UniqueKeyField in shortcuts.py directly sets
unique=True rather than using this mixin. This mixin exists as a composable building
block for custom field types that need uniqueness constraints.

Example Usage
-------------
    from popoto import Model, UniqueKeyField

    class User(Model):
        # Email must be unique across all User instances
        email = UniqueKeyField(type=str)
        username = UniqueKeyField(type=str, max_length=30)

    # This will succeed
    user1 = User(email="alice@example.com", username="alice")
    user1.save()

    # This would fail validation due to duplicate email
    user2 = User(email="alice@example.com", username="bob")
"""
import logging

logger = logging.getLogger("POPOTO.KeyFieldMixin")


class UniqueFieldMixin:
    """
    Mixin that enforces uniqueness constraints on field values.

    This mixin guarantees that no two model instances can share the same value
    for a field marked as unique. It is intended to be combined with KeyFieldMixin
    (via UniqueKeyField) to create fields that are both indexed and unique.

    The uniqueness constraint is enforced at two levels:
    1. At field definition time: The mixin ensures unique=True cannot be overridden
    2. At query/save time: Inherited behavior from KeyFieldMixin maintains Redis
       sets that track which values exist, enabling duplicate detection

    Attributes
    ----------
    unique : bool
        Always True for this mixin. Attempting to set unique=False will raise
        a ModelException. This attribute signals to the validation and indexing
        system that duplicate values should be rejected.

    Design Decision
    ---------------
    The decision to make unique=True immutable (raising an exception if set to
    False) ensures that developers cannot accidentally create a "UniqueKeyField"
    that isn't actually unique. This follows the principle of making invalid
    states unrepresentable.
    """

    unique: bool = True

    def __init__(self, **kwargs):
        """
        Initialize the unique field mixin with uniqueness enforcement.

        This method participates in Python's cooperative multiple inheritance,
        calling super().__init__() to ensure all mixins in the inheritance chain
        are properly initialized. It sets the unique=True default and validates
        that the uniqueness constraint is not being disabled.

        Parameters
        ----------
        **kwargs : dict
            Field configuration options. The 'unique' key, if provided, must be
            True or omitted. Passing unique=False will raise a ModelException.

        Raises
        ------
        ModelException
            If unique=False is explicitly passed, since a UniqueField must
            enforce uniqueness by definition.

        Notes
        -----
        The field_defaults dictionary is updated to include unique=True, ensuring
        that this setting propagates correctly through the field system's
        introspection and serialization mechanisms.
        """
        super().__init__(**kwargs)
        uniquekeyfield_defaults = {
            "unique": True,
        }
        self.field_defaults.update(uniquekeyfield_defaults)
        # set field options, let kwargs override
        for k, v in uniquekeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

        if not kwargs.get("unique", True):
            from ..models.base import ModelException

            raise ModelException("UniqueKey field MUST be unique")
