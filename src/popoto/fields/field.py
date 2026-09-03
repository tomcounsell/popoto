"""
Popoto Field System - The Foundation of Redis ORM Type Safety

This module implements the core Field abstraction that enables Django-like model
definitions while mapping Python types to Redis storage. The Field class and its
metaclass FieldBase form the foundation upon which all specialized field types
(KeyField, SortedField, GeoField, etc.) are built.

Design Philosophy:
------------------
Popoto follows the principle of "convention over configuration" - fields work
sensibly by default but can be customized when needed. The design intentionally
mirrors Django's ORM to reduce cognitive load for developers familiar with that
ecosystem.

The metaclass pattern (FieldBase) ensures that each field type automatically
receives a unique identifier (field_class_key) used for Redis key namespacing.
This enables specialized fields to maintain their own secondary data structures
(sorted sets, geo indexes, etc.) without key collisions.

Extensibility:
--------------
New field behaviors are added through mixins (KeyFieldMixin, SortedFieldMixin,
etc.) rather than deep inheritance hierarchies. This composition-based approach
allows combining capabilities - for example, SortedKeyField combines both
key-based lookups and range queries by mixing SortedFieldMixin with KeyFieldMixin.

The hook methods (on_save, on_delete, format_value_pre_save, filter_query) provide
extension points for specialized fields to maintain secondary indexes or apply
custom serialization without modifying core model save/delete logic.

Example:
--------
    from popoto import Model, Field
    from popoto.fields.shortcuts import KeyField, SortedField

    class Product(Model):
        sku = KeyField()           # Part of Redis key, enables exact-match queries
        name = Field(type=str)     # Basic string field
        price = SortedField()      # Enables range queries like price__lte=9.99

See Also:
---------
- shortcuts.py: Convenience field types (IntField, StringField, AutoKeyField, etc.)
- key_field_mixin.py: Adds key-based indexing and lookup capabilities
- sorted_field_mixin.py: Adds range query support via Redis sorted sets
"""

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING
import redis

from ..exceptions import ModelException

import logging

from ..models.db_key import DB_key

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from ..models.base import Model

logger = logging.getLogger("POPOTO.field")

VALID_FIELD_TYPES = {
    int,
    float,
    Decimal,
    str,
    bool,
    bytes,
    list,
    dict,
    set,
    tuple,
    date,
    datetime,
    time,
}
"""
The set of Python types that can be stored in a basic Field.

These types are chosen because they can be reliably serialized to and from
Redis using msgpack encoding. Custom types require specialized Field subclasses
(e.g., GeoField for geographic coordinates, Relationship for model references).

Note: KeyFields have a more restricted type set (VALID_KEYFIELD_TYPES in
key_field_mixin.py) because key values must be safely representable as
Redis key segments (no colons, no complex nested structures).
"""


#: Index namespace -> owning class name. Two class names that fold to the
#: same namespace would share on-disk index keys; the metaclass refuses that.
_FIELD_CLASS_KEYS: dict[str, str] = {}


class FieldBase(type):
    """
    Metaclass for all Popoto Fields.

    This metaclass automatically assigns a unique `field_class_key` to each Field
    subclass upon creation. This key is used to namespace Redis data structures
    that specialized fields maintain alongside the main model data.

    Why a Metaclass?
    ----------------
    Field types need class-level identity before any instances exist. When a
    SortedField maintains a Redis sorted set index, that index key must include
    the field type to prevent collisions between different field types on the
    same model. The metaclass ensures this identity is established at class
    definition time, not instance creation time.

    Key Generation:
    ---------------
    The field_class_key follows the pattern: $<FieldTypeName>F
    For example:
    - Field -> $F (base field, rarely used directly)
    - SortedField -> $SortedF
    - GeoField -> $GeoF

    The $ prefix marks these as internal Popoto keys, distinguishing them from
    user model keys. The F suffix indicates "Field-related" data structure.
    """

    def __new__(cls, name, bases, attrs, **kwargs):
        # if not a Field, skip setup
        parents = [b for b in bases if isinstance(b, FieldBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

        new_class = super().__new__(cls, name, bases, attrs, **kwargs)
        if "field_class_key" not in attrs:
            # Index namespace derives from the class name. This uses
            # str.strip("Field"), which strips a character *set* rather than
            # a suffix (FloatField -> "$oatF"). That spelling is on disk for
            # every deployment, including downstream subclasses, so it is
            # frozen. What it must not do is let two class names fold onto
            # one namespace silently, so collisions are refused below; a
            # class that needs a specific namespace sets ``field_class_key``
            # explicitly.
            new_class.field_class_key = DB_key(f"${name.strip('Field')}F")
        key = str(getattr(new_class, "field_class_key"))
        owner = _FIELD_CLASS_KEYS.get(key)
        if owner is not None and owner != name:
            raise TypeError(
                f"Field class {name!r} derives index namespace {key!r}, which "
                f"{owner!r} already uses. Set an explicit "
                f"``field_class_key = DB_key('$...F')`` on {name!r}."
            )
        _FIELD_CLASS_KEYS[key] = name
        return new_class


class Field(metaclass=FieldBase):
    """
    Base class for all Popoto model fields.

    Field is the fundamental building block for defining model attributes that
    will be persisted to Redis. It handles type validation, null checking, and
    provides hooks for specialized field behaviors.

    Defines the value type, nullability, default, and validation logic.
    Specialized behaviors (key indexing, sorting, geo) are added via mixins
    or subclasses.

    Design Rationale:
    -----------------
    Fields serve dual purposes:

    1. Schema Definition: At class definition time (in the Model metaclass),
       Field instances describe what attributes a model has and their constraints.
       The ModelOptions class collects these fields and categorizes them (key fields,
       sorted fields, etc.).

    2. Value Handling: At runtime, fields validate values, format them for storage,
       and can maintain secondary data structures (via on_save/on_delete hooks).

    The class-level attributes (type, key, null, etc.) serve as documentation and
    defaults. Instance attributes set in __init__ override these for customization.

    Args:
        type: Python type for the field value (default ``str``).
        null: Allow ``None`` values (default ``True``).
        default: Default value for new instances.
        max_length: Maximum string length enforced on save (default 1024).

    Attributes:
        type: The Python type for values stored in this field. Defaults to str.
        key: If True, this field contributes to the model's Redis key. Use KeyField.
        unique: If True, values must be unique across all instances. Use UniqueKeyField.
        auto: If True, values are auto-generated (e.g., UUIDs). Use AutoKeyField.
        null: If True, None is a valid value. Defaults to True.
        max_length: Maximum string length (default 1024). Redis allows up to 512MB.
        default: Default value when none is provided. Defaults to empty string.
        sorted: If True, enables range queries. Use SortedField.

    Example:
        class Article(Model):
            title = Field(type=str, null=False, max_length=200)
            view_count = Field(type=int, default=0)
            content = Field(type=str, max_length=50000)
    """

    type: type = str
    key: bool = False
    unique: bool = False
    auto: bool = False
    null: bool = True
    value: str = None
    max_length: int = None
    default = ""
    sorted: bool = False
    indexed: bool = False

    # -------------------------------------------------------------------------
    # Export/import round-trip protocol (see export_state / import_state below)
    # -------------------------------------------------------------------------
    roundtrip_policy: str = "rebuild"
    """
    Declares how this field's auxiliary Redis state is treated by
    ``popoto.transfer`` export/import. One of:

    - ``"rebuild"`` (default) — the field maintains no independent state that
      export needs to capture; whatever ``on_save`` does on import is
      sufficient to fully reconstruct it. This is correct for the vast
      majority of fields (e.g. indexes, sorted sets, geo indexes) whose
      secondary structures are pure functions of the field's stored value.
    - ``"carry"`` — the field maintains state that is *not* derivable from the
      stored value alone (e.g. a running counter, a learned score). Fields
      declaring ``"carry"`` must implement ``export_state``/``import_state``
      to serialize and restore that state explicitly.
    - ``"partial"`` — some state is carried or rebuilt and some is knowingly
      not preserved. Fields declaring ``"partial"`` must also set
      ``roundtrip_note`` explaining what is lost and why (typically citing a
      tracking issue).

    Override this class attribute directly on a Field subclass when it
    maintains any secondary Redis structure via ``on_save`` that a plain
    re-save would not otherwise reconstruct byte-for-byte.
    """

    roundtrip_note: str = None
    """
    Human-readable explanation surfaced in the import report when
    ``roundtrip_policy == "partial"``. Should describe what state is not
    preserved and, where applicable, cite the tracking issue for future work
    (e.g. ``"history not carried, see #556"``). Left as ``None`` for
    ``"rebuild"``/``"carry"`` fields, which need no such caveat.
    """

    @classmethod
    def export_state(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        **kwargs,
    ) -> dict:
        """
        Return this field's auxiliary state for export, or ``None``.

        Called once per field per exported record by ``popoto.transfer``.
        Fields whose secondary Redis structures are fully derivable from
        ``field_value`` via ``on_save`` (``roundtrip_policy == "rebuild"``)
        should not override this — the base implementation returning
        ``None`` is correct and means "nothing extra to carry".

        Override this when ``roundtrip_policy`` is ``"carry"`` or
        ``"partial"`` and there is state to capture: read whatever secondary
        Redis structure this field maintains (e.g. via a companion hash key
        this field owns) and return it as a JSON-serializable dict. Return
        ``None`` if, for this particular instance, there is nothing to carry.

        Args:
            model_instance: The Model instance being exported.
            field_name: Name of this field on the model.
            field_value: The field's current value (as also captured by
                ``to_dict()``).
            **kwargs: Reserved for future extension.

        Returns:
            A JSON-serializable dict of auxiliary state, or ``None`` if this
            field has nothing beyond its plain value to carry. Base
            implementation always returns ``None``.
        """
        return None

    @classmethod
    def import_state(
        cls,
        model_instance: "Model",
        field_name: str,
        state: dict,
        **kwargs,
    ) -> None:
        """
        Restore this field's auxiliary state after import, or no-op.

        Called once per field per imported record by ``popoto.transfer``,
        after the instance has been constructed and saved (so any
        ``on_save``-rebuilt structure already exists and can be overwritten
        or supplemented). ``state`` is whatever ``export_state`` returned for
        this field on the exporting side, or ``None`` if nothing was carried.

        Override this when ``roundtrip_policy`` is ``"carry"`` or
        ``"partial"`` and ``export_state`` returns non-``None`` state: write
        it back into the same secondary Redis structure ``export_state``
        read it from. The base implementation is a no-op, which is correct
        whenever there is no carried state to restore.

        Args:
            model_instance: The Model instance that was just saved.
            field_name: Name of this field on the model.
            state: The dict previously returned by ``export_state`` for this
                field, or ``None``.
            **kwargs: Reserved for future extension.

        Returns:
            None.
        """
        return None

    def __init__(self, **kwargs):
        """
        Initialize a Field with optional configuration overrides.

        The initialization pattern uses a defaults dictionary that mixins can extend.
        This enables the mixin composition pattern: each mixin's __init__ calls
        super().__init__() and adds its own defaults to field_defaults, allowing
        kwargs to override any level of the hierarchy.

        Args:
            **kwargs: Configuration overrides. Common options include:
                - type: Python type for validation (default: str)
                - null: Allow None values (default: True)
                - default: Default value for new instances
                - max_length: Maximum string length (default: 1024)

        Raises:
            ModelException: If type is not in VALID_FIELD_TYPES for base Field.
                Specialized fields may have their own type restrictions.
        """
        self.field_defaults = {  # default
            "type": str,
            "key": False,
            "unique": False,
            "auto": False,
            "null": True,
            "value": None,
            "max_length": None,
            "default": None,
            "sorted": False,
            "indexed": False,
        }
        # set field_options, let kwargs override
        field_options = self.field_defaults.copy()
        for k, v in field_options.items():
            setattr(self, k, kwargs.get(k, v))
        if self.__class__ == Field and self.type not in VALID_FIELD_TYPES:
            raise ModelException(f"{self.type} is not a valid Field type")

    # -------------------------------------------------------------------------
    # Comparison operators for expression-based queries
    # -------------------------------------------------------------------------
    # These methods enable Python comparison syntax on Field class attributes:
    #   Model.query.filter(Model.price > 100)
    # instead of:
    #   Model.query.filter(price__gt=100)
    #
    # IMPORTANT: The `name` attribute is set by the Model metaclass when the
    # field is assigned to a model class. These operators rely on `self.name`
    # being available, which happens automatically for class-level Field access.
    # -------------------------------------------------------------------------

    def __eq__(self, value):
        """Create an equality expression or perform normal Python equality check.

        When comparing a Field to a non-Field value, creates an Expression for
        query filtering. When comparing two Fields, falls back to Python's
        normal object equality.

        Args:
            value: The value to compare against

        Returns:
            Expression if comparing to a non-Field value, bool otherwise

        Example:
            Model.status == "active"  # Returns Expression
            field1 == field2          # Returns bool (normal Python equality)
        """
        if isinstance(value, Field):
            return id(self) == id(value)  # Normal Python object equality
        from ..models.expressions import Expression

        return Expression(self.name, "__eq", value)

    def __ne__(self, value):
        """Create an inequality expression or perform normal Python inequality check.

        Args:
            value: The value to compare against

        Returns:
            Expression if comparing to a non-Field value, bool otherwise

        Example:
            Model.status != "inactive"  # Returns Expression
        """
        if isinstance(value, Field):
            return id(self) != id(value)  # Normal Python object inequality
        from ..models.expressions import Expression

        return Expression(self.name, "__ne", value)

    def __gt__(self, value):
        """Create a greater-than expression for query filtering.

        Args:
            value: The value to compare against

        Returns:
            Expression representing field > value

        Example:
            Model.price > 100  # Returns Expression(price__gt=100)
        """
        from ..models.expressions import Expression

        return Expression(self.name, "__gt", value)

    def __lt__(self, value):
        """Create a less-than expression for query filtering.

        Args:
            value: The value to compare against

        Returns:
            Expression representing field < value

        Example:
            Model.price < 50  # Returns Expression(price__lt=50)
        """
        from ..models.expressions import Expression

        return Expression(self.name, "__lt", value)

    def __ge__(self, value):
        """Create a greater-than-or-equal expression for query filtering.

        Args:
            value: The value to compare against

        Returns:
            Expression representing field >= value

        Example:
            Model.price >= 100  # Returns Expression(price__gte=100)
        """
        from ..models.expressions import Expression

        return Expression(self.name, "__gte", value)

    def __le__(self, value):
        """Create a less-than-or-equal expression for query filtering.

        Args:
            value: The value to compare against

        Returns:
            Expression representing field <= value

        Example:
            Model.price <= 50  # Returns Expression(price__lte=50)
        """
        from ..models.expressions import Expression

        return Expression(self.name, "__lte", value)

    def between(self, low, high):
        """Create a between expression for inclusive range query filtering.

        Returns an Expression that matches values where low <= field <= high.
        This is a convenience method equivalent to combining >= and <= operators.

        Args:
            low: The lower bound of the range (inclusive)
            high: The upper bound of the range (inclusive)

        Returns:
            Expression representing low <= field <= high

        Example:
            Model.price.between(10, 50)  # Returns Expression(price__between=(10, 50))
        """
        from ..models.expressions import Expression

        return Expression(self.name, "__between", (low, high))

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """
        Validate a value against field constraints.

        This is a classmethod because validation may need to be performed before
        a model instance exists (e.g., during query parameter validation). The
        field instance is passed explicitly rather than using self.

        Validation is intentionally lenient by default - it returns False with
        logged errors rather than raising exceptions. This allows batch operations
        to continue processing valid items while flagging invalid ones.

        Mixins override this method to add their own validation rules, calling
        super().is_valid() to chain validations. For example, SortedFieldMixin
        adds numeric type checking.

        Args:
            field: The Field instance defining constraints
            value: The value to validate
            null_check: If False, skip null validation (used during instance init
                when fields may not yet have values assigned)
            **kwargs: Reserved for mixin extensions

        Returns:
            True if value passes all validation rules, False otherwise.
            Validation failures are logged at ERROR level.
        """
        if not null_check and value is None:
            return True
        if field.null and value is None:
            return True
        elif value is None:
            logger.error(f"field {field} is null")
            return False
        elif not isinstance(value, field.type):
            logger.error(
                f"field {field} is type {field.type}. But value is {type(value)}"
            )
            return False
        if (
            field.max_length is not None
            and field.type is str
            and len(str(value)) > field.max_length
        ):
            logger.error(f"{field} value is greater than max_length={field.max_length}")
            return False
        return True

    def format_value_pre_save(self, field_value, **kwargs):
        """
        Transform a field value before Redis storage.

        This hook allows specialized fields to normalize or convert values just
        before they are serialized. It runs after validation, so the value is
        guaranteed to be valid according to is_valid().

        Design Note:
        ------------
        This is an instance method (not classmethod) because the transformation
        may depend on field configuration (e.g., a DecimalField might need to
        know the precision setting from its instance).

        Override Examples:
        ------------------
        - SortedFieldMixin: Converts Decimal to float for Redis sorted set scores
        - A hypothetical EncryptedField: Could encrypt the value here

        Args:
            field_value: The validated value to transform
            **kwargs: Additional keyword arguments accepted for forward
                compatibility (e.g., skip_auto_now used by SortedFieldMixin).

        Returns:
            The transformed value ready for serialization. Base implementation
            returns the value unchanged.
        """
        return field_value

    @classmethod
    def get_special_use_field_db_key(cls, model: "Model", *field_names) -> DB_key:
        """
        Generate a namespaced Redis key for field-specific data structures.

        Specialized fields often maintain secondary data structures alongside the
        main model hash. For example:
        - KeyFieldMixin maintains sets for each unique value (enabling exact-match queries)
        - SortedFieldMixin maintains sorted sets (enabling range queries)
        - GeoField maintains geo indexes (enabling radius searches)

        This method generates a unique key prefix that prevents collisions between
        these structures across different field types and models.

        Key Structure:
        --------------
        The generated key follows the pattern:
            $<FieldType>F:<ModelName>:<field_name>

        For example, a SortedField named 'price' on a Product model:
            $SortedF:Product:price

        This key might then be extended by the mixin (e.g., SortedFieldMixin adds
        partition field values for sharded sorted sets).

        Args:
            model: The Model class or instance (provides _meta.db_class_key)
            *field_names: Field name(s) to include in the key. Typically just one,
                but some advanced patterns may need multiple.

        Returns:
            A DB_key instance representing the namespaced key prefix.

        Note:
            Child classes implementing multiple Redis structures per field will
            need to extend this key further to distinguish between structures.
        """
        return DB_key(cls.field_class_key, model._meta.db_class_key, *field_names)

    @classmethod
    def pre_save_validate(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        **kwargs,
    ) -> None:
        """Raise to abort a save before ANY write is issued or queued.

        Runs from ONE site in ``Model.save()`` -- after the ``pre_save`` gate and
        before the partial/full save split -- so it covers all four save arms,
        including the two external-pipeline arms that return before either eager
        indexed-field loop is reached. That is what distinguishes it from
        :meth:`on_save`, which on the internal-pipeline arms has already let
        every ``IndexedFieldMixin`` field commit its hash value and index entry,
        and on the external-pipeline arms runs only as a queued command.

        On a partial save the dispatch is scoped to ``update_fields``, so a save
        of an unrelated column cannot trip a validation this field owns.

        Default: no-op. Implement it only for a check that must precede every
        write on the model, not merely every write this field owns.

        Args:
            model_instance: The Model instance being saved.
            field_name: Name of this field on the model.
            field_value: Current value of the field (may be None).
            **kwargs: Additional context, forwarded from ``Model.save()``.

        Returns:
            ``None``. Signal a refusal by raising.
        """
        return None

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Hook called when a model instance is saved, for each field.

        This is the primary extension point for specialized fields that maintain
        secondary data structures. The Model.save() method iterates through all
        fields and calls their on_save() hooks, allowing each field type to update
        its indexes.

        Pipeline Support:
        -----------------
        The optional pipeline parameter enables atomic batch operations. When
        provided, implementations should add their Redis commands to the pipeline
        rather than executing immediately. This is critical for consistency -
        the main model data and all secondary indexes are saved in a single
        Redis transaction.

        Implementation Pattern:
        -----------------------
        Mixins override this to add their index updates:
        - KeyFieldMixin: Adds model key to a set keyed by field value
        - SortedFieldMixin: Adds model key to a sorted set with field value as score
        - GeoField: Adds model key to a geo index

        Each mixin should call super().on_save() to allow chaining, though the
        base implementation is a no-op.

        Args:
            model_instance: The Model instance being saved
            field_name: Name of this field on the model
            field_value: Current value of the field (may be None)
            pipeline: Optional Redis pipeline for batched operations
            **kwargs: Additional context (e.g., ignore_errors)

        Returns:
            The pipeline if provided, otherwise None. Mixins that add commands
            to the pipeline must return it for chaining.
        """
        # todo: create choice Sets of instance keys for fields using choices option
        # if model_instance._meta.fields[field_name].choices:
        #     # this will not work! how to edit, delete, prevent overwrite and duplicates?
        #     field_value_b = cls.encode(field_value)
        #     if pipeline:
        #         return pipeline.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)
        #     else:
        #         from ..redis_db import POPOTO_REDIS_DB
        #         return POPOTO_REDIS_DB.set(cls.get_special_use_field_db_key(model_instance, field_name), field_value_b)

        return pipeline if pipeline else None

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value,
        pipeline=None,
        **kwargs,
    ):
        """
        Hook called when a model instance is deleted, for each field.

        Mirrors on_save() but for cleanup operations. Specialized fields use this
        to remove entries from their secondary data structures when a model is
        deleted, maintaining index consistency.

        Important Timing Note:
        ----------------------
        This hook is also called during Model.save() when a model's key changes
        (i.e., when a KeyField value is modified). In this case, on_delete() is
        called for the old key values before on_save() is called for the new ones.
        This ensures the old index entries are cleaned up.

        Implementation Pattern:
        -----------------------
        Mixins override this to remove their index entries:
        - KeyFieldMixin: Removes model key from the set for the old value
        - SortedFieldMixin: Removes model key from the sorted set
        - GeoField: Removes model key from the geo index

        Args:
            model_instance: The Model instance being deleted
            field_name: Name of this field on the model
            field_value: Current value of the field (may be None)
            pipeline: Optional Redis pipeline for batched operations
            **kwargs: Additional context

        Returns:
            The pipeline if provided, otherwise None.
        """
        return pipeline if pipeline else None

    def get_filter_query_params(self, field_name: str) -> set:
        """
        Declare which query filter parameters this field supports.

        The Query system uses this method to validate filter() arguments and route
        them to the appropriate field's filter_query() method. Each field type
        advertises what query operations it supports.

        Design Pattern:
        ---------------
        This method returns the set of valid parameter names for Query.filter().
        The naming convention follows Django's double-underscore pattern:
        - "field_name" - exact match
        - "field_name__gt" - greater than
        - "field_name__contains" - substring match
        - etc.

        Mixins extend this by calling super().get_filter_query_params() and adding
        their supported parameters via set union.

        Examples by Field Type:
        -----------------------
        - Base Field: Returns empty set (no filtering on plain fields)
        - KeyFieldMixin: {field_name, field_name__isnull, field_name__contains,
                         field_name__startswith, field_name__endswith, field_name__in}
        - SortedFieldMixin: {field_name, field_name__gt, field_name__gte,
                            field_name__lt, field_name__lte}

        Args:
            field_name: The name of this field on the model. Parameter names are
                derived from this (e.g., "price" -> "price__gte").

        Returns:
            Set of valid query parameter strings for this field.
        """
        return set()

    @classmethod
    def filter_query(cls, model: "Model", field_name: str, **query_params) -> set:
        """
        Execute a filter query and return matching model keys.

        This is the query execution counterpart to get_filter_query_params().
        After the Query class validates that filter parameters are supported,
        it delegates to this method to actually perform the Redis lookup.

        Why Return Keys (Not Instances)?
        ---------------------------------
        Returning Redis keys rather than model instances enables efficient
        query composition. Multiple filter conditions produce sets of keys that
        can be intersected (AND) or unioned (OR) before any model data is fetched.
        This is a deliberate trade-off: more complex query logic in exchange for
        minimal Redis round-trips.

        Implementation by Field Type:
        -----------------------------
        - KeyFieldMixin: Uses Redis SMEMBERS on value-indexed sets, or KEYS
          pattern matching for wildcard queries
        - SortedFieldMixin: Uses Redis ZRANGEBYSCORE for range queries
        - GeoField: Uses Redis GEORADIUS for geographic queries

        Args:
            model: The Model class to query against
            field_name: Name of the field being filtered
            **query_params: Filter parameters (e.g., price__gte=10.0)

        Returns:
            Set of Redis keys (as bytes) for matching model instances.

        Raises:
            QueryException: Base Field does not support filtering. This ensures
                users explicitly choose queryable field types (KeyField, SortedField)
                rather than accidentally expecting queries on plain fields.
        """
        from ..models.query import QueryException

        raise QueryException(
            "Query filter not allowed on base Field. Consider using a KeyField"
        )
