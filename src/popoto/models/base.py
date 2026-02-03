"""Core model system for Popoto Redis ORM.

This module implements the Django-inspired ORM pattern for Redis, providing a
declarative way to define data models that persist to Redis as hash maps. The
design philosophy centers on three key principles:

1. **Familiarity**: Developers familiar with Django's ORM can immediately
   understand Popoto's syntax. Models are classes, fields are class attributes,
   and queries use filter/get patterns.

2. **Redis-Native**: Unlike SQL ORMs that abstract away the database, Popoto
   embraces Redis's strengths. KeyFields directly map to Redis key structure,
   SortedFields use Redis sorted sets, and GeoFields leverage Redis geospatial
   commands.

3. **Explicit Over Implicit**: Public model attributes must be Field instances.
   This prevents accidental data persistence and makes the schema self-documenting.

Architecture Overview:
    - ModelBase (metaclass): Intercepts class creation to process Field
      definitions and build ModelOptions metadata.
    - ModelOptions: Registry of field metadata enabling query optimization
      and key generation.
    - Model: Base class providing CRUD operations and validation.
    - Query: Attached to each Model class for Django-style querying.

Example:
    class User(Model):
        email = KeyField()  # Part of Redis key
        name = Field(type=str)
        score = SortedField()  # Enables range queries

    user = User(email="test@example.com", name="Test")
    user.save()

    # Query using Django-style syntax
    User.query.filter(score__gte=100)
"""

import logging
import asyncio
import sys
import functools

import redis

from .encoding import encode_popoto_model_obj, decode_lazy_field
from .db_key import DB_key
from .query import Query
from ..fields.auto_field_mixin import AutoFieldMixin
from ..fields.field import Field, VALID_FIELD_TYPES
from ..fields.key_field_mixin import KeyFieldMixin
from ..fields.sorted_field_mixin import SortedFieldMixin
from ..fields.geo_field import GeoField
from ..fields.relationship import Relationship
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.model_base")

# Python 3.8 compatibility for asyncio.to_thread()
if sys.version_info >= (3, 9):
    to_thread = asyncio.to_thread
else:
    # Backport for Python 3.8
    async def to_thread(func, *args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, functools.partial(func, *args, **kwargs)
        )


global RELATED_MODEL_LOAD_SEQUENCE
RELATED_MODEL_LOAD_SEQUENCE = set()


class ModelException(Exception):
    """Raised when a model operation fails (validation, save, unique constraint, etc.).

    Raised when:
        - Field names violate naming conventions (must start lowercase)
        - Reserved field names are used (limit, order_by, values)
        - Public attributes are not Field instances
        - Model validation fails during instantiation or save
        - TTL and expire_at are both set (mutually exclusive)

    This exception is intentionally broad to provide clear error messages
    during development. In production, consider catching specific cases.
    """

    pass


# Length of hex digest used for index hashes. 16 hex chars = 64 bits,
# sufficient for index key uniqueness within a single model's field combinations.
INDEX_HASH_LENGTH = 16


class ModelOptions:
    """Metadata container for a Model class, analogous to Django's Options.

    Tracks fields, key fields, sorted fields, geo fields, relationships,
    indexes, TTL, and default ordering. Created automatically by
    :class:`ModelBase` during class definition.

    ModelOptions serves as the central registry for all field metadata on a
    Model class. It is created during class definition by the ModelBase
    metaclass and attached as `Model._meta`.

    Design Rationale:
        Rather than repeatedly inspecting class attributes at runtime, all
        field information is collected once during class creation. This
        enables efficient query building and key generation without
        reflection overhead.

    The class tracks several field categories for specialized behavior:
        - key_field_names: Fields that comprise the Redis key (primary key)
        - auto_field_names: Fields with auto-generated values (e.g., UUIDs)
        - sorted_field_names: Fields backed by Redis sorted sets for range queries
        - geo_field_names: Fields using Redis geospatial indexes
        - relationship_field_names: Fields linking to other Model instances

    Attributes:
        model_name: The class name, used as prefix in Redis keys.
        db_class_key: Redis key prefix for this model type.
        db_class_set_key: Redis key for the set tracking all instances.
        explicit_fields: User-defined public fields (no underscore prefix).
        hidden_fields: Private fields (underscore prefix) still persisted.
        filter_query_params_by_field: Maps field names to valid query params.

    Example:
        class Product(Model):
            sku = KeyField()
            price = SortedField()

        Product._meta.key_field_names  # {'sku'}
        Product._meta.sorted_field_names  # {'price'}
    """

    def __init__(self, model_name):
        self.model_name = model_name
        self.db_class_key = DB_key(self.model_name)
        self.db_class_set_key = DB_key("$Class", self.db_class_key)

        self.hidden_fields = dict()
        self.explicit_fields = dict()
        self.key_field_names = set()
        self.auto_field_names = set()
        # self.list_field_names = set()
        # self.set_field_names = set()
        self.relationship_field_names = set()
        self.sorted_field_names = set()
        self.geo_field_names = set()
        # todo: should this be a dict of related objects or just a list of field names?
        # self.related_fields = {}  # model becomes graph node

        self.filter_query_params_by_field = dict()  # field_name: set(query_params,..)

        self.abstract = False
        self.unique_together = []
        self.index_together = []
        self.parents = []
        self.auto_created = False
        self.base_meta = None
        self.order_by = None  # Default ordering for queries
        self.ttl = None  # Default TTL in seconds for all instances
        self.indexes = ()  # Tuple of ((field_names,), is_unique) tuples

    def add_field(self, field_name: str, field: Field):
        """Register a field with this model's metadata.

        Called during class creation by ModelBase metaclass. Validates the
        field name against naming conventions and categorizes the field
        based on its mixins (KeyFieldMixin, SortedFieldMixin, etc.).

        This method enforces Popoto's "explicit schema" design: every
        persisted attribute must be a Field instance, preventing accidental
        data storage and making the model self-documenting.

        Args:
            field_name: The attribute name for this field.
            field: The Field instance to register.

        Raises:
            ModelException: If field_name doesn't start lowercase, uses a
                reserved name, or is already registered.

        Note:
            Fields starting with underscore are "hidden" - they persist to
            Redis but signal internal/computed data.
        """
        if not field_name[0] == "_" and not field_name[0].islower():
            raise ModelException(
                f"{field_name} field name must start with a lowercase letter."
            )
        elif field_name in ["limit", "order_by", "values"]:
            raise ModelException(
                f"{field_name} is a reserved field name. "
                f"See https://popoto.readthedocs.io/en/latest/fields/#reserved-field-names"
            )
        elif field_name.startswith("_") and field_name not in self.hidden_fields:
            self.hidden_fields[field_name] = field
        elif field_name not in self.explicit_fields:
            self.explicit_fields[field_name] = field
        else:
            raise ModelException(f"{field_name} is already a Field on the model")

        # Set the field name for expression-based queries
        field.name = field_name

        if isinstance(field, KeyFieldMixin):
            self.key_field_names.add(field_name)
        if isinstance(field, AutoFieldMixin):
            self.auto_field_names.add(field_name)
        if isinstance(field, SortedFieldMixin):
            self.sorted_field_names.add(field_name)
        if isinstance(field, GeoField):
            self.geo_field_names.add(field_name)
        # if isinstance(field, ListField):
        #     self.list_field_names.add(field_name)
        if isinstance(field, Relationship):
            self.relationship_field_names.add(field_name)

        self.filter_query_params_by_field[field_name] = field.get_filter_query_params(
            field_name
        )

    @property
    def fields(self) -> dict:
        """All registered fields, both public and hidden.

        Returns:
            Dict mapping field names to Field instances.
        """
        return {**self.explicit_fields, **self.hidden_fields}

    @property
    def field_names(self) -> list:
        """List of all field names on this model.

        Returns:
            List of field name strings.
        """
        return list(self.fields.keys())

    @property
    def db_key_length(self) -> int:
        """Number of segments in the Redis key.

        Redis keys follow the pattern: ClassName:key1:key2:...
        So length is 1 (class name) + number of KeyFields.

        Returns:
            Integer count of key segments.
        """
        return 1 + len(self.key_field_names)

    def get_db_key_index_position(self, field_name: str) -> int:
        """Get the position of a KeyField in the Redis key string.

        KeyFields are sorted alphabetically in the Redis key. This method
        returns the 1-based index (0 is the class name) for extracting
        field values from key strings without loading the full object.

        Args:
            field_name: Name of a KeyField on this model.

        Returns:
            Integer position in the colon-separated Redis key.

        Example:
            # For key "User:alice:123" with KeyFields email, user_id
            _meta.get_db_key_index_position('email')  # 1
            _meta.get_db_key_index_position('user_id')  # 2
        """
        return 1 + sorted(self.key_field_names).index(field_name)

    def get_index_key(self, field_names: tuple) -> str:
        """Generate Redis key for an index."""
        field_key = ":".join(field_names)
        return f"$Index:{self.model_name}:{field_key}"

    def compute_index_hash(self, model_instance, field_names: tuple) -> str:
        """Compute hash of field values for index uniqueness check.

        Returns None if any field value is None (NULL handling: multiple NULLs allowed).
        """
        import hashlib

        values = []
        for field_name in field_names:
            value = getattr(model_instance, field_name, None)
            if value is None:
                return None  # Don't index NULL values (allows multiple NULLs)
            values.append(str(value))
        combined = ":".join(values)
        return hashlib.sha256(combined.encode()).hexdigest()[:INDEX_HASH_LENGTH]

    def compute_index_hash_from_values(
        self, field_names: tuple, field_values: dict
    ) -> str:
        """Compute hash from a dict of field values (for cleanup of old values).

        Returns None if any field value is None.
        """
        import hashlib

        values = []
        for field_name in field_names:
            value = field_values.get(field_name)
            if value is None:
                return None
            values.append(str(value))
        combined = ":".join(values)
        return hashlib.sha256(combined.encode()).hexdigest()[:INDEX_HASH_LENGTH]


class ModelBase(type):
    """Metaclass for all Popoto Models.

    ModelBase intercepts class creation to transform Field class attributes
    into a structured metadata registry (ModelOptions). This follows the
    same pattern as Django's ModelBase metaclass.

    Key Responsibilities:
        1. Separate Field instances from methods and private attributes
        2. Build ModelOptions with categorized field metadata
        3. Attach Query interface as class attribute (Model.query)
        4. Enforce the "public attrs must be Fields" constraint

    Design Philosophy:
        By processing fields at class creation time rather than instance
        creation, we pay the introspection cost once. The resulting
        ModelOptions enables O(1) field lookups during save/load operations.

    The metaclass handles inheritance by checking for ModelBase parents,
    allowing the base Model class to skip field processing while all
    subclasses are fully configured.

    Example:
        class User(Model):  # ModelBase.__new__ is called here
            email = KeyField()
            name = Field()

        # After metaclass processing:
        # - User._meta contains ModelOptions with field registry
        # - User.query is a Query instance for this model
        # - User.objects aliases User.query (Django compatibility)
    """

    def __new__(cls, name, bases, attrs, **kwargs):

        # Initialization is only performed for a Model and its subclasses
        parents = [b for b in bases if isinstance(b, ModelBase)]
        if not parents:
            return super().__new__(cls, name, bases, attrs, **kwargs)

        # logger.debug({k: v for k, v in attrs.items() if not k.startswith('__')})
        module = attrs.pop("__module__")
        new_attrs = {"__module__": module}
        attr_meta = attrs.pop("Meta", None)
        options = ModelOptions(name)
        options.parents = parents

        for obj_name, obj in attrs.items():
            if obj_name.startswith("__"):
                # builtin or inherited private vars and methods
                new_attrs[obj_name] = obj

            elif isinstance(obj, Field):
                # save field instance
                # attr will be overwritten as a field.type
                # model will handle this and set default values
                obj.name = obj_name  # Set field name for expression-based queries
                options.add_field(obj_name, obj)
                # Keep Field as class attribute for expression queries (Model.field > value)
                new_attrs[obj_name] = obj

            elif callable(obj) or hasattr(obj, "__func__") or hasattr(obj, "__set__"):
                # a callable method or property
                new_attrs[obj_name] = obj

            elif obj_name.startswith("_"):
                # a private static attr not to be saved in the db
                new_attrs[obj_name] = obj

            else:
                raise ModelException(
                    f"public model attributes must inherit from class Field. "
                    f"Try using a private var (eg. _{obj_name})_"
                )

        # todo: handle multiple inheritance
        # for base in parents:
        #     for field_name, field in base.auto_fields.items():
        #         options.add_field(field_name, field)

        new_class = super().__new__(cls, name, bases, new_attrs)

        options.abstract = getattr(attr_meta, "abstract", False)
        options.order_by = getattr(attr_meta, "order_by", None)
        options.ttl = getattr(attr_meta, "ttl", None)
        options.indexes = getattr(attr_meta, "indexes", ())

        # Validate order_by field exists
        if options.order_by:
            field_name = options.order_by.lstrip("-")
            if field_name not in options.fields:
                raise ModelException(
                    f"Meta.order_by references '{field_name}' but this field does not exist on {name}"
                )

        # Validate ttl is a positive integer if provided
        if options.ttl is not None and (
            not isinstance(options.ttl, int) or options.ttl <= 0
        ):
            raise ModelException(
                f"Meta.ttl must be a positive integer (seconds), got {options.ttl}"
            )

        # Validate indexes structure
        if options.indexes:
            if not isinstance(options.indexes, (tuple, list)):
                raise ModelException(
                    f"Meta.indexes must be a tuple or list, got {type(options.indexes)}"
                )
            for index in options.indexes:
                if not isinstance(index, (tuple, list)) or len(index) != 2:
                    raise ModelException(
                        f"Each index must be a 2-tuple (field_names, is_unique), got {index}"
                    )
                field_names, is_unique = index
                if not isinstance(field_names, (tuple, list)):
                    raise ModelException(
                        f"Index field names must be a tuple/list, got {type(field_names)}"
                    )
                if not isinstance(is_unique, bool):
                    raise ModelException(
                        f"Index uniqueness flag must be boolean, got {type(is_unique)}"
                    )
                # Validate all field names exist
                for field_name in field_names:
                    if field_name not in options.fields:
                        raise ModelException(
                            f"Unknown field '{field_name}' in Meta.indexes for {name}"
                        )

        options.meta = attr_meta or getattr(new_class, "Meta", None)
        options.base_meta = getattr(new_class, "_meta", None)
        new_class._meta = options
        new_class.objects = new_class.query = Query(new_class)
        return new_class


class Model(metaclass=ModelBase):
    """Base class for all Popoto models providing Redis persistence.

    Define public attributes as :class:`~popoto.Field` instances. The model is
    persisted as a Redis hash at the key ``ClassName:key1:key2:...``.

    Model is the foundation of Popoto's ORM, combining declarative field
    definitions with Django-inspired CRUD operations. Each Model subclass
    maps to a collection of Redis hash maps, with instances identified by
    composite keys derived from KeyField values.

    Storage Architecture:
        - Each instance is stored as a Redis HSET (hash map)
        - The Redis key follows pattern: ClassName:keyfield1:keyfield2:...
        - A Redis SET tracks all keys for each model class (for .all() queries)
        - Specialized fields (Sorted, Geo) maintain secondary indexes

    Key Design Decisions:
        1. **Composite Keys**: Multiple KeyFields combine alphabetically,
           enabling natural multi-column primary keys.

        2. **Auto-Key Fallback**: Models without explicit KeyFields get an
           automatic UUID-based `_auto_key` field.

        3. **Pipeline Support**: All operations accept an optional Redis
           pipeline for batching multiple operations atomically.

        4. **Relationship Loading**: Related models are loaded eagerly with
           cycle detection to prevent infinite recursion.

    Class Attributes:
        query: A :class:`~popoto.models.query.Query` instance for this model.
        objects: Alias for query (Django compatibility).
        _meta: ModelOptions containing field metadata.

    Instance Attributes:
        _redis_key: The actual Redis key after save (may differ from db_key
            if KeyField values changed).
        _db_content: Cached serialized content from last save.
        obsolete_redis_key: Previous key if KeyFields changed (triggers
            delete of old key on save).

    Example:
        class Article(Model):
            slug = KeyField()
            title = Field(type=str)
            views = SortedField()

        # Create and save
        article = Article(slug="hello-world", title="Hello World")
        article.save()

        # Query
        Article.query.get(slug="hello-world")
        Article.query.filter(views__gte=100, order_by="-views")
    """

    query: Query

    def __init__(self, **kwargs):
        """Initialize a model instance with field values.

        Handles the complete initialization sequence:
            1. Apply any base parameters from kwargs
            2. Add auto-generated KeyField if no KeyFields defined
            3. Generate values for AutoFields (e.g., UUIDs)
            4. Set field defaults for unspecified fields
            5. Apply kwargs values over defaults
            6. Load related models (with cycle detection)
            7. Validate all field values

        Args:
            **kwargs: Field values to set on the instance. Keys should
                match field names defined on the model class.

        Raises:
            ModelException: If validation fails for any field value.

        Note:
            The instance is not saved to Redis during __init__. Call
            save() to persist, or use Model.create() for atomic
            create-and-save.
        """
        cls = self.__class__

        # allow init kwargs to set any base parameters
        self.__dict__.update(kwargs)

        # add auto KeyField if needed
        if not len(self._meta.key_field_names):
            from ..fields.shortcuts import AutoKeyField

            self._meta.add_field("_auto_key", AutoKeyField())

        # prep AutoKeys with new default ids
        for field in self._meta.fields.values():
            if hasattr(field, "auto") and field.auto:
                field.set_auto_key_value()

        # set defaults (support callable defaults like uuid.uuid4 or dict)
        for field_name, field in self._meta.fields.items():
            default_value = (
                field.default() if callable(field.default) else field.default
            )
            setattr(self, field_name, default_value)

        # set field values from init kwargs
        for field_name in self._meta.fields.keys() & kwargs.keys():
            setattr(self, field_name, kwargs.get(field_name))

        # load relationships
        if len(self._meta.relationship_field_names):
            global RELATED_MODEL_LOAD_SEQUENCE
            is_parent_model = len(RELATED_MODEL_LOAD_SEQUENCE) == 0
            for field_name in self._meta.relationship_field_names:
                if (
                    f"{self.__class__.__name__}.{field_name}"
                    in RELATED_MODEL_LOAD_SEQUENCE
                ):
                    continue
                RELATED_MODEL_LOAD_SEQUENCE.add(
                    f"{self.__class__.__name__}.{field_name}"
                )

                field_value = getattr(self, field_name)
                if isinstance(field_value, Model):
                    setattr(self, field_name, field_value)
                elif isinstance(field_value, str):
                    setattr(
                        self,
                        field_name,
                        self._meta.fields[field_name].model.query.get(
                            redis_key=field_value
                        ),
                    )

                # todo: lazy load the instance from the db
                elif not field_value:
                    setattr(self, field_name, None)
                else:
                    raise ModelException(
                        f"{field_name} expects model instance or redis_key"
                    )
            if is_parent_model:
                RELATED_MODEL_LOAD_SEQUENCE = set()

        # Set TTL from Meta.ttl as default if not already set via kwargs
        if not hasattr(self, "_ttl") or self._ttl is None:
            self._ttl = self._meta.ttl
        if not hasattr(self, "_expire_at"):
            self._expire_at = None  # Can be set per-instance as datetime

        # validate initial attributes
        if not self.is_valid(
            null_check=False
        ):  # exclude null, will validate null values on pre-save
            raise ModelException(f"Could not instantiate class {self}")

        self._redis_key = None
        # _db_key used by Redis cannot be known without performance cost
        # _db_key is predicted until synced during save() call
        if None not in [
            getattr(self, key_field_name)
            for key_field_name in self._meta.key_field_names
        ]:
            self._redis_key = self.db_key.redis_key
        self.obsolete_redis_key = (
            None  # to be used when db_key changes between loading and saving the object
        )
        self._db_content = dict()  # empty until synced during save() call
        self._saved_field_values = (
            dict()
        )  # stores field values at last save for proper on_delete cleanup

        # todo: create set of possible custom field keys

    @property
    def db_key(self) -> DB_key:
        """Compute the Redis key for this instance.

        The key structure is: ClassName:keyfield1_value:keyfield2_value:...

        KeyField values are sorted alphabetically by field name to ensure
        deterministic key generation. This means the order of KeyField
        definitions doesn't affect the key structure.

        Returns:
            DB_key instance that can be used as a Redis key string.

        Note:
            This property computes the key from current field values. If
            KeyField values change after loading, the computed key differs
            from _redis_key (the original storage location). The save()
            method handles this by deleting the obsolete key.

        Example:
            class User(Model):
                org = KeyField()
                email = KeyField()

            user = User(org="acme", email="alice@example.com")
            str(user.db_key)  # "User:alice@example.com:acme"
            # Note: 'email' comes before 'org' alphabetically
        """
        return DB_key(
            self._meta.db_class_key,
            [
                str(getattr(self, key_field_name, "None"))
                for key_field_name in sorted(self._meta.key_field_names)
            ],
        )

    def __repr__(self):
        """Return developer-friendly representation with Redis key."""
        return f"<{self.__class__.__name__} Popoto object at {self.db_key.redis_key}>"

    def __str__(self):
        """Return the Redis key as string representation."""
        return str(self.db_key)

    def __eq__(self, other):
        """Compare instances by their Redis key identity.

        Two instances are equal if they have the same class and the same
        Redis key (derived from KeyField values). This is identity equality,
        not value equality.

        Special Cases:
            - Instances with any None KeyField values are only equal to
              themselves (identity check via repr).
            - Different classes are never equal, even with same key structure.

        Args:
            other: Another object to compare against.

        Returns:
            True if same class and same db_key, False otherwise.

        Note:
            For full value comparison across all fields, compare the
            serialized field dictionaries directly rather than using ==.
        """
        try:
            if isinstance(other, self.__class__):
                # always False if if any KeyFields are None
                if (
                    None
                    in [
                        self._meta.fields.get(key_field_name)
                        for key_field_name in self._meta.key_field_names
                    ]
                ) or (
                    None
                    in [
                        other._meta.fields.get(key_field_name)
                        for key_field_name in other._meta.key_field_names
                    ]
                ):
                    return repr(self) == repr(other)
                return self.db_key == other.db_key
        except:
            return False
        else:
            return False

    def __getattribute__(self, name):
        """Get attribute with lazy field loading support.

        For lazily-loaded model instances (created via decode_popoto_model_hashmap
        with lazy=True), field values are decoded from msgpack on first access.
        This reduces deserialization overhead when only a subset of fields are used.

        The lazy loading mechanism:
            1. Check if instance has _lazy_fields (lazy-loaded from Redis)
            2. If the field hasn't been decoded yet, decode and cache it
            3. Return the cached decoded value

        Args:
            name: Attribute name to retrieve.

        Returns:
            The attribute value, decoded from msgpack if lazy-loaded.
        """
        # Use object.__getattribute__ to avoid recursion
        try:
            lazy_fields = object.__getattribute__(self, "_lazy_fields")
        except AttributeError:
            # Not a lazy instance, use normal attribute access
            return object.__getattribute__(self, name)

        # Check if this is a lazy field that needs decoding
        if name in lazy_fields:
            decoded_fields = object.__getattribute__(self, "_decoded_fields")
            if name not in decoded_fields:
                # Decode and cache the field value
                decoded_fields[name] = decode_lazy_field(lazy_fields[name])
            return decoded_fields[name]

        # For non-field attributes or already decoded fields, use normal access
        return object.__getattribute__(self, name)

    def __setattr__(self, name, value):
        """Set attribute with lazy field cache update.

        When setting a field value on a lazy-loaded instance, update the
        decoded fields cache to ensure consistency.

        Args:
            name: Attribute name to set.
            value: Value to assign.
        """
        # Check if this is a lazy instance and we're setting a lazy field
        try:
            lazy_fields = object.__getattribute__(self, "_lazy_fields")
            if name in lazy_fields:
                decoded_fields = object.__getattribute__(self, "_decoded_fields")
                decoded_fields[name] = value
                return
        except AttributeError:
            pass

        # Normal attribute setting
        object.__setattr__(self, name, value)

    # @property
    # def field_names(self):
    #     return [
    #         k for k, v in self.__dict__.items()
    #         if all([not k.startswith("_"), k + "_meta" in self.__dict__])
    #     ]

    def is_valid(self, null_check: bool = True) -> bool:
        """Validate all field values against their field constraints.

        Performs comprehensive validation including:
            - Type checking (coerces compatible types when possible)
            - Null/None checking for non-nullable fields
            - String max_length enforcement
            - Field-specific validation via Field.is_valid()
            - Mutual exclusion of ttl and expire_at

        Args:
            null_check: If False, skip null validation. Useful during
                initialization when required fields may not yet be set.

        Returns:
            True if all validations pass, False otherwise.

        Note:
            Validation errors are logged but not raised. Check logs for
            details when is_valid() returns False. This design allows
            batch validation without exception handling complexity.
        """

        # Check TTL/expire_at mutual exclusion (model-level, not per-field)
        if self._ttl and self._expire_at:
            raise ModelException("Can set either ttl and expire_at. Not both.")

        # Single pass over all fields with cached lookups
        for field_name, field in self._meta.fields.items():
            value = getattr(self, field_name)

            # Type coercion: convert compatible types before validation
            if value is not None and not isinstance(value, field.type):
                try:
                    if field.type in VALID_FIELD_TYPES:
                        coerced = field.type(value)
                        setattr(self, field_name, coerced)
                        value = coerced
                    if not isinstance(value, field.type):
                        raise TypeError(
                            f"Expected {field_name} to be type {field.type}. "
                            f"It is type {type(value)}"
                        )
                except TypeError as e:
                    logger.error(
                        f"{str(e)} \n Change the value or modify type on {self.__class__.__name__}.{field_name}"
                    )
                    return False

            # Field-level validation (handles null check, type check, max_length, etc.)
            field_class = field.__class__
            if not field_class.is_valid(field, value, null_check=null_check):
                logger.error(f"Validation on [{field_name}] Field failed")
                return False

        return True

    def pre_save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
        """Prepare instance for saving by validating and formatting fields.

        Called automatically by save(). Runs full validation and applies
        any field-specific formatting (via Field.format_value_pre_save).

        Args:
            pipeline: Optional Redis pipeline for batched operations.
            ignore_errors: If True, log validation errors instead of raising.
            **kwargs: Additional arguments passed to field formatters.

        Returns:
            The pipeline if provided, True on success, or False on
            validation failure with ignore_errors=True.

        Raises:
            ModelException: If validation fails and ignore_errors=False.
        """
        if not self.is_valid():
            error_message = "Model instance parameters invalid. Failed to save."
            if ignore_errors:
                logger.error(error_message)
            else:
                raise ModelException(error_message)
            return False

        # Check unique indexes
        for field_names, is_unique in self._meta.indexes:
            if not is_unique:
                continue  # Only check unique indexes

            index_key = self._meta.get_index_key(tuple(field_names))
            index_hash = self._meta.compute_index_hash(self, tuple(field_names))

            # Skip NULL values (multiple NULLs allowed per SQL standard)
            if index_hash is None:
                continue

            # Check if hash exists in Redis HASH
            existing_key = POPOTO_REDIS_DB.hget(index_key, index_hash)
            if existing_key:
                existing_key_str = (
                    existing_key.decode()
                    if isinstance(existing_key, bytes)
                    else existing_key
                )
                # Skip self if updating (same db_key)
                if self._redis_key and existing_key_str == self._redis_key:
                    continue
                if existing_key_str == self.db_key.redis_key:
                    continue

                field_values = [str(getattr(self, f)) for f in field_names]
                error_message = (
                    f"Unique index violation on {field_names}: "
                    f"({', '.join(field_values)}) already exists"
                )
                if ignore_errors:
                    logger.error(error_message)
                    return False
                else:
                    raise ModelException(error_message)

        # Check unique field constraints (individual fields with unique=True)
        # Uses SCARD + SISMEMBER instead of SMEMBERS for ~20x faster lookups
        for field_name, field in self._meta.fields.items():
            if not getattr(field, "unique", False):
                continue
            field_value = getattr(self, field_name)
            if field_value is None:
                continue
            unique_set_key = DB_key(
                field.get_special_use_field_db_key(self, field_name), field_value
            )
            set_size = POPOTO_REDIS_DB.scard(unique_set_key.redis_key)
            if set_size == 0:
                continue
            own_key = self.db_key.redis_key
            own_key_bytes = own_key.encode() if isinstance(own_key, str) else own_key
            is_self = POPOTO_REDIS_DB.sismember(unique_set_key.redis_key, own_key_bytes)
            if set_size > 1 or (set_size == 1 and not is_self):
                error_message = (
                    f"Unique constraint violated: {field_name}={field_value} "
                    f"already exists on another instance"
                )
                if ignore_errors:
                    logger.error(error_message)
                    return False
                else:
                    raise ModelException(error_message)

        # run any necessary formatting on field data before saving
        for field_name, field in self._meta.fields.items():
            setattr(
                self, field_name, field.format_value_pre_save(getattr(self, field_name))
            )
        return pipeline if pipeline else True

    def save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
        """Persist the model instance to Redis.

        Executes the complete save workflow:
            1. Validate and format field values (pre_save)
            2. Serialize instance to Redis hash map
            3. Store hash map with HSET command
            4. Add key to model's class set (for .all() queries)
            5. Handle key migration if KeyFields changed
            6. Trigger Field.on_save() hooks for secondary indexes

        Args:
            pipeline: Optional Redis pipeline for atomic batch operations.
                When provided, commands are queued but not executed - caller
                must call pipeline.execute().
            ignore_errors: If True, log validation errors and return False
                instead of raising ModelException.
            **kwargs: Passed to field on_save hooks.

        Returns:
            - If pipeline: The pipeline with queued commands
            - If no pipeline: Redis HSET response (number of fields set)
            - On error with ignore_errors: False

        Note:
            When KeyField values change between load and save, the old Redis
            key is automatically deleted. This enables "rename" operations
            while maintaining data integrity.

        Example:
            # Single save
            user.save()

            # Batched saves
            pipe = redis.pipeline()
            user1.save(pipeline=pipe)
            user2.save(pipeline=pipe)
            pipe.execute()
        """

        pipeline_or_success = self.pre_save(
            pipeline=pipeline, ignore_errors=ignore_errors, **kwargs
        )
        if not pipeline_or_success:
            return pipeline or False
        elif pipeline:
            pipeline = pipeline_or_success

        new_db_key = DB_key(self.db_key)  # todo: why have a new key??
        if self._redis_key != new_db_key.redis_key:
            self.obsolete_redis_key = self._redis_key

        # todo: implement and test tll, expire_at
        # ttl, expire_at = (ttl or self._ttl), (expire_at or self._expire_at)

        """
        1. save object as hashmap
        2. optionally set ttl, expire_at
        3. add to class set
        4. if obsolete key, delete and run field on_delete methods
        5. run field on_save methods
        6. save private version of compiled db key
        """

        hset_mapping = encode_popoto_model_obj(self)  # 1
        self._db_content = hset_mapping  # 1

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline = pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)  # 1
            if self._ttl is not None:
                pipeline = pipeline.expire(new_db_key.redis_key, self._ttl)  # 2
            elif self._expire_at is not None:
                pipeline = pipeline.expireat(
                    new_db_key.redis_key, int(self._expire_at.timestamp())
                )  # 2
            pipeline = pipeline.sadd(
                self._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3
            if (
                self.obsolete_redis_key
                and self.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in self._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = self._saved_field_values.get(
                        field_name, getattr(self, field_name)
                    )
                    pipeline = field.on_delete(  # 4
                        model_instance=self,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=pipeline,
                        saved_redis_key=self.obsolete_redis_key,
                        **kwargs,
                    )
                pipeline.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None
            for field_name, field in self._meta.fields.items():  # 5
                pipeline = field.on_save(  # 5
                    self,
                    field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=pipeline,
                    **kwargs,
                )
            # Manage indexes  # 6
            for field_names, is_unique in self._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = self._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if self._saved_field_values:
                    old_hash = self._meta.compute_index_hash_from_values(
                        field_names_tuple, self._saved_field_values
                    )
                    if old_hash:
                        pipeline = pipeline.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = self._meta.compute_index_hash(self, field_names_tuple)
                if new_hash:
                    pipeline = pipeline.hset(index_key, new_hash, new_db_key.redis_key)
            self._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            self._saved_field_values = {
                field_name: getattr(self, field_name)
                for field_name in self._meta.fields.keys()
            }
            return pipeline

        else:
            db_response = POPOTO_REDIS_DB.hset(
                new_db_key.redis_key, mapping=hset_mapping
            )  # 1
            if self._ttl is not None:
                POPOTO_REDIS_DB.expire(new_db_key.redis_key, self._ttl)  # 2
            elif self._expire_at is not None:
                POPOTO_REDIS_DB.expireat(
                    new_db_key.redis_key, int(self._expire_at.timestamp())
                )  # 2
            POPOTO_REDIS_DB.sadd(
                self._meta.db_class_set_key.redis_key, new_db_key.redis_key
            )  # 3

            if (
                self.obsolete_redis_key
                and self.obsolete_redis_key != new_db_key.redis_key
            ):  # 4
                for field_name, field in self._meta.fields.items():
                    # Use saved field values for cleanup to ensure correct Redis keys are removed
                    field_value = self._saved_field_values.get(
                        field_name, getattr(self, field_name)
                    )
                    field.on_delete(  # 4
                        model_instance=self,
                        field_name=field_name,
                        field_value=field_value,
                        pipeline=None,
                        saved_redis_key=self.obsolete_redis_key,
                        **kwargs,
                    )
                POPOTO_REDIS_DB.delete(self.obsolete_redis_key)  # 4
                self.obsolete_redis_key = None

            for field_name, field in self._meta.fields.items():  # 5
                field.on_save(  # 5
                    self,
                    field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=None,
                    **kwargs,
                )

            # Manage indexes  # 6
            for field_names, is_unique in self._meta.indexes:
                field_names_tuple = tuple(field_names)
                index_key = self._meta.get_index_key(field_names_tuple)
                # Remove old index entry if indexed fields changed
                if self._saved_field_values:
                    old_hash = self._meta.compute_index_hash_from_values(
                        field_names_tuple, self._saved_field_values
                    )
                    if old_hash:
                        POPOTO_REDIS_DB.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = self._meta.compute_index_hash(self, field_names_tuple)
                if new_hash:
                    POPOTO_REDIS_DB.hset(index_key, new_hash, new_db_key.redis_key)

            self._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            self._saved_field_values = {
                field_name: getattr(self, field_name)
                for field_name in self._meta.fields.keys()
            }
            return db_response

    @classmethod
    def create(cls, pipeline: redis.client.Pipeline = None, **kwargs):
        """Create a new instance, save it to Redis, and return it.

        Convenience method combining instantiation and save() in one call.
        Useful when you don't need to modify the instance before persisting.

        Args:
            pipeline: Optional Redis pipeline for batch operations.
            **kwargs: Field values passed to __init__.

        Returns:
            - If pipeline: The pipeline with queued commands
            - If no pipeline: The saved model instance

        Example:
            user = User.create(email="test@example.com", name="Test")
        """
        instance = cls(**kwargs)
        pipeline_or_db_response = instance.save(pipeline=pipeline)
        return pipeline_or_db_response if pipeline else instance

    @classmethod
    def load(cls, db_key: str = None, **kwargs):
        """Load an existing instance from Redis by *db_key* or field values.

        Provides two loading patterns:
            1. Direct key lookup: Pass db_key parameter
            2. KeyField lookup: Pass KeyField values as kwargs

        Args:
            db_key: Direct Redis key string to load.
            **kwargs: KeyField values to construct the lookup key.

        Returns:
            Model instance if found, None otherwise.

        Example:
            # By key
            user = User.load(db_key="User:test@example.com")

            # By KeyField values
            user = User.load(email="test@example.com")
        """
        return cls.query.get(db_key=db_key or cls(**kwargs).db_key)

    def delete(self, pipeline: redis.client.Pipeline = None, *args, **kwargs):
        """Delete this instance from Redis.

        Executes the complete deletion workflow:
            1. Delete the Redis hash map (HSET data)
            2. Remove key from model's class set
            3. Trigger Field.on_delete() hooks to clean secondary indexes
            4. Clear internal state (_db_content)

        Args:
            pipeline: Optional Redis pipeline for batch operations.
            **kwargs: Passed to field on_delete hooks.

        Returns:
            - If pipeline provided initially: The pipeline with queued commands
            - If no pipeline: Boolean indicating if object existed and was deleted

        Note:
            Field on_delete hooks are critical for maintaining index integrity.
            For example, SortedField removes the instance from its sorted set,
            and KeyField removes from its lookup set.

        Example:
            if user.delete():
                print("User was deleted")
            else:
                print("User did not exist")
        """
        delete_redis_key = self._redis_key or self.db_key.redis_key
        db_response = False

        if pipeline:
            pipeline = pipeline.delete(delete_redis_key)  # 1
        else:
            db_response = POPOTO_REDIS_DB.delete(delete_redis_key)  # 1
            pipeline = POPOTO_REDIS_DB.pipeline()

        pipeline = pipeline.srem(
            self._meta.db_class_set_key.redis_key, delete_redis_key
        )  # 2

        for field_name, field in self._meta.fields.items():  # 3
            # Use saved field values if available, otherwise fall back to current values
            # This ensures we clean up the correct Redis keys even if field values changed
            field_value = self._saved_field_values.get(
                field_name, getattr(self, field_name)
            )
            pipeline = field.on_delete(
                model_instance=self,
                field_name=field_name,
                field_value=field_value,
                pipeline=pipeline,
                saved_redis_key=delete_redis_key,
                **kwargs,
            )

        # Clean up indexes  # 4
        cleanup_values = self._saved_field_values or {
            field_name: getattr(self, field_name)
            for field_name in self._meta.fields.keys()
        }
        for field_names, is_unique in self._meta.indexes:
            field_names_tuple = tuple(field_names)
            index_key = self._meta.get_index_key(field_names_tuple)
            index_hash = self._meta.compute_index_hash_from_values(
                field_names_tuple, cleanup_values
            )
            if index_hash:
                pipeline = pipeline.hdel(index_key, index_hash)

        self._db_content = dict()  # 5
        self._saved_field_values = dict()  # 5

        if db_response is not False:
            pipeline.execute()
            return bool(db_response > 0)
        else:
            return pipeline

    @classmethod
    def get_info(cls) -> dict:
        """Return a dict with the model name, field names, and available query filters.

        Useful for debugging, documentation generation, and building
        dynamic query interfaces. Returns the model name, all field
        names, and all valid query filter parameters.

        Returns:
            Dict with keys:
                - name: Model class name
                - fields: List of all field names
                - query_filters: List of valid filter() parameter names

        Example:
            User.get_info()
            # {'name': 'User',
            #  'fields': ['email', 'name', 'score'],
            #  'query_filters': ['email', 'score__gte', 'score__lte']}
        """
        from itertools import chain

        query_filters = list(
            chain(
                *[
                    field.get_filter_query_params(field_name)
                    for field_name, field in cls._meta.fields.items()
                ]
            )
        )
        return {
            "name": cls.__name__,
            "fields": cls._meta.field_names,
            "query_filters": query_filters,
        }

    # Async methods

    async def async_save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        **kwargs,
    ):
        """Async version of save().

        Runs the synchronous save() method in a thread pool to avoid blocking
        the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            ignore_errors: If True, log errors instead of raising exceptions
            **kwargs: Additional arguments passed to save()

        Returns:
            Pipeline or db_response depending on whether pipeline was provided
        """
        return await to_thread(
            self.save, pipeline=pipeline, ignore_errors=ignore_errors, **kwargs
        )

    async def async_delete(
        self, pipeline: redis.client.Pipeline = None, *args, **kwargs
    ):
        """Async version of delete().

        Runs the synchronous delete() method in a thread pool to avoid blocking
        the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            *args: Additional positional arguments passed to delete()
            **kwargs: Additional keyword arguments passed to delete()

        Returns:
            Pipeline or boolean(object existed AND was deleted)
        """
        return await to_thread(self.delete, pipeline=pipeline, *args, **kwargs)

    @classmethod
    async def async_create(cls, pipeline: redis.client.Pipeline = None, **kwargs):
        """Async version of create().

        Creates a new model instance and saves it to Redis in a thread pool
        to avoid blocking the event loop.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            **kwargs: Field values for the new instance

        Returns:
            Pipeline or Model instance depending on whether pipeline was provided
        """
        return await to_thread(cls.create, pipeline=pipeline, **kwargs)

    @classmethod
    async def async_load(cls, db_key: str = None, **kwargs):
        """Async version of load().

        Loads a model instance from Redis by db_key or field values in a
        thread pool to avoid blocking the event loop.

        Args:
            db_key: Optional db_key string to load
            **kwargs: Field values to construct db_key if not provided

        Returns:
            Model instance or None if not found
        """
        return await to_thread(cls.load, db_key=db_key, **kwargs)
