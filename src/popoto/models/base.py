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
from asyncio import to_thread
from typing import TYPE_CHECKING, Optional, Tuple, Union

import redis

if TYPE_CHECKING:
    from redis.client import Pipeline

from .encoding import encode_popoto_model_obj, decode_lazy_field
from .db_key import DB_key
from .query import Query
from ..fields.auto_field_mixin import AutoFieldMixin
from ..fields.field import Field, VALID_FIELD_TYPES
from ..fields.indexed_field_mixin import IndexedFieldMixin
from ..fields.key_field_mixin import KeyFieldMixin
from ..fields.sorted_field_mixin import SortedFieldMixin
from ..fields.geo_field import GeoField
from ..fields.relationship import Relationship
from ..redis_db import POPOTO_REDIS_DB
from ..exceptions import ModelException, KeyMutationError, SkipSaveException

logger = logging.getLogger("POPOTO.model_base")


global RELATED_MODEL_LOAD_SEQUENCE
RELATED_MODEL_LOAD_SEQUENCE = set()


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
        self.indexed_field_names = set()
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
        if isinstance(field, IndexedFieldMixin):
            self.indexed_field_names.add(field_name)
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

        # Wrap capped ListField values in CappedListProxy
        from ..fields.shortcuts import ListField, CappedListProxy

        for field_name, field in self._meta.fields.items():
            if isinstance(field, ListField) and field._capped:
                current_value = getattr(self, field_name, None)
                if not isinstance(current_value, CappedListProxy):
                    proxy = CappedListProxy(
                        data=current_value if current_value else [],
                        model_instance=self,
                        field_name=field_name,
                        max_length=field.max_length,
                    )
                    setattr(self, field_name, proxy)

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
        self._is_persisted = False  # True after pipeline.execute() confirms write

        # todo: create set of possible custom field keys

    @property
    def pk(self) -> str:
        """The primary key string for this instance.

        Returns the cached Redis key if available, otherwise computes it
        from current KeyField values. Follows Django convention.

        Returns:
            The Redis key string identifying this instance.
        """
        return self._redis_key or self.db_key.redis_key

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
            if not isinstance(other, self.__class__):
                return False
            # Always False if any KeyFields are None - use repr comparison
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
        except (AttributeError, TypeError):
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
            # Skip coercion for Relationship fields — they handle their own
            # validation and legitimately hold str redis_key values when
            # lazy-loaded from Redis.
            from ..fields.relationship import Relationship

            from ..fields.shortcuts import CappedListProxy

            if (
                value is not None
                and not isinstance(value, field.type)
                and not isinstance(field, Relationship)
                and not isinstance(value, CappedListProxy)
            ):
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
        skip_auto_now: bool = False,
        update_fields: list = None,
        **kwargs,
    ):
        """Prepare instance for saving by validating and formatting fields.

        Called automatically by save(). Runs full validation and applies
        any field-specific formatting (via Field.format_value_pre_save).

        Args:
            pipeline: Optional Redis pipeline for batched operations.
            ignore_errors: If True, log validation errors instead of raising.
            skip_auto_now: If True, suppress auto_now timestamp updates.
            update_fields: Optional list of field names to validate/format.
                When provided, only listed fields are processed (partial save).
                When None, all fields are processed (full save).
            **kwargs: Additional arguments passed to field formatters.

        Returns:
            The pipeline if provided, True on success, or False on
            validation failure with ignore_errors=True.

        Raises:
            ModelException: If validation fails and ignore_errors=False,
                or if update_fields contains unknown field names.
        """
        if update_fields is not None:
            # Partial save: only validate and format listed fields
            for field_name in update_fields:
                if field_name not in self._meta.fields:
                    raise ModelException(
                        f"Unknown field '{field_name}' in update_fields"
                    )
                field = self._meta.fields[field_name]
                setattr(
                    self,
                    field_name,
                    field.format_value_pre_save(
                        getattr(self, field_name),
                        skip_auto_now=skip_auto_now,
                    ),
                )
            return pipeline if pipeline else True

        # Full save path (existing behavior, unchanged)
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
                self,
                field_name,
                field.format_value_pre_save(
                    getattr(self, field_name),
                    skip_auto_now=skip_auto_now,
                ),
            )
        return pipeline if pipeline else True

    def save(
        self,
        pipeline: "Pipeline" = None,
        ignore_errors: bool = False,
        skip_auto_now: bool = False,
        update_fields: list = None,
        migrate_key: bool = False,
        **kwargs,
    ) -> Union["Pipeline", int, bool]:
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
            skip_auto_now: If True, suppress auto_now timestamp updates.
            update_fields: Optional list of field names for partial save.
                When provided, only listed fields are serialized, validated,
                and indexed. None means full save (default behavior).
                Empty list is a no-op.
            migrate_key: If True, allow KeyField value changes (key migration).
                By default, changing a KeyField value after initial save raises
                KeyMutationError to prevent accidental identity changes.
            **kwargs: Passed to field on_save hooks.

        Returns:
            - If pipeline: The pipeline with queued commands
            - If no pipeline: Redis HSET response (number of fields set)
            - On error with ignore_errors: False

        Raises:
            KeyMutationError: If a KeyField value has changed since last save
                and migrate_key is not True.

        Note:
            When KeyField values change between load and save, the old Redis
            key is automatically deleted. This enables "rename" operations
            while maintaining data integrity.

            When no pipeline is provided, save() uses an internal pipeline
            and calls execute() before returning. This guarantees that all
            index writes are visible to subsequent filter() calls. When an
            external pipeline is provided, index writes are queued but NOT
            flushed -- the caller must call pipeline.execute() before
            querying, or filter() may miss the saved record.

        Example:
            # Single save (immediately queryable after return)
            user.save()

            # Batched saves (NOT queryable until execute)
            pipe = redis.pipeline()
            user1.save(pipeline=pipe)
            user2.save(pipeline=pipe)
            pipe.execute()  # indexes now visible to filter()

            # Partial save (only update specific fields)
            user.name = "new_name"
            user.save(update_fields=["name"])

            # Key migration (intentional identity change)
            instance.name = "new_key_value"
            instance.save(migrate_key=True)
        """

        # Handle update_fields empty list as no-op
        if update_fields is not None and len(update_fields) == 0:
            return pipeline or 0

        # Immutability guard: prevent accidental KeyField mutation
        # Only fires when _saved_field_values is populated (loaded from DB or
        # previously saved) and migrate_key is not explicitly True.
        if not migrate_key and self._saved_field_values:
            for key_field_name in self._meta.key_field_names:
                # Skip auto key fields -- they are set once and never change
                if key_field_name in self._meta.auto_field_names:
                    continue
                old_value = self._saved_field_values.get(key_field_name)
                new_value = getattr(self, key_field_name, None)
                if old_value is not None and old_value != new_value:
                    raise KeyMutationError(
                        f"Cannot change KeyField '{key_field_name}' from "
                        f"'{old_value}' to '{new_value}' without "
                        f"migrate_key=True. KeyField values form the model's "
                        f"Redis identity and cannot be changed accidentally."
                    )

        # WriteFilterMixin: check write filter before any save work
        from ..fields.write_filter import WriteFilterMixin

        if isinstance(self, WriteFilterMixin):
            try:
                self._check_write_filter()
            except SkipSaveException:
                return pipeline if pipeline else False

        # EventStreamMixin: detect create vs update before persistence
        # _db_content is {} on fresh instances, populated after first save
        from ..fields.event_stream import EventStreamMixin

        _is_create = isinstance(self, EventStreamMixin) and not self._db_content

        pipeline_or_success = self.pre_save(
            pipeline=pipeline,
            ignore_errors=ignore_errors,
            skip_auto_now=skip_auto_now,
            update_fields=update_fields,
            **kwargs,
        )
        if not pipeline_or_success:
            return pipeline or False
        elif pipeline:
            pipeline = pipeline_or_success

        new_db_key = DB_key(self.db_key)  # todo: why have a new key??

        if update_fields is not None:
            # Partial save path: only write listed fields to Redis
            from ..redis_db import ENCODING

            # Detect obsolete key: if any updated field is a KeyField,
            # the db_key may have changed. We need to clean up the old
            # key's hash, class set entry, and index entries.
            obsolete_key = None
            if self._redis_key != new_db_key.redis_key:
                obsolete_key = self._redis_key

            # Encode all fields, then filter to only update_fields
            full_mapping = encode_popoto_model_obj(self)
            update_field_names_bytes = {
                field_name.encode(ENCODING) for field_name in update_fields
            }
            hset_mapping = {
                k: v for k, v in full_mapping.items() if k in update_field_names_bytes
            }

            if isinstance(pipeline, redis.client.Pipeline):
                pipeline = pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)
                # If db_key changed, clean up the obsolete key
                if obsolete_key and obsolete_key != new_db_key.redis_key:
                    # Remove old index entries using saved field values
                    for field_name, field in self._meta.fields.items():
                        field_value = self._saved_field_values.get(
                            field_name, getattr(self, field_name)
                        )
                        pipeline = field.on_delete(
                            model_instance=self,
                            field_name=field_name,
                            field_value=field_value,
                            pipeline=pipeline,
                            saved_redis_key=obsolete_key,
                            **kwargs,
                        )
                    # Delete old hash and update class set
                    pipeline.delete(obsolete_key)
                    pipeline.srem(self._meta.db_class_set_key.redis_key, obsolete_key)
                    pipeline.sadd(
                        self._meta.db_class_set_key.redis_key,
                        new_db_key.redis_key,
                    )
                # Run on_save for listed fields (adds new index entries)
                for field_name in update_fields:
                    field = self._meta.fields[field_name]
                    pipeline = field.on_save(
                        self,
                        field_name=field_name,
                        field_value=getattr(self, field_name),
                        ignore_errors=ignore_errors,
                        pipeline=pipeline,
                        **kwargs,
                    )
                # Handle TTL/expire_at
                if self._ttl is not None:
                    pipeline = pipeline.expire(new_db_key.redis_key, self._ttl)
                elif self._expire_at is not None:
                    pipeline = pipeline.expireat(
                        new_db_key.redis_key, int(self._expire_at.timestamp())
                    )
                self._redis_key = new_db_key.redis_key
                # Merge into saved_field_values (preserve existing, update listed)
                for field_name in update_fields:
                    self._saved_field_values[field_name] = getattr(self, field_name)
                # WriteFilterMixin: tag priority after successful partial save
                if isinstance(self, WriteFilterMixin):
                    self._tag_priority(pipeline=pipeline)
                # EventStreamMixin: log mutation after successful partial save
                if isinstance(self, EventStreamMixin):
                    _op = "create" if _is_create else "update"
                    self._xadd_mutation(
                        _op, pipeline=pipeline, update_fields=update_fields
                    )
                return pipeline
            else:
                # Use internal pipeline for atomic execution
                internal_pipeline = POPOTO_REDIS_DB.pipeline()
                internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)
                # If db_key changed, clean up the obsolete key
                if obsolete_key and obsolete_key != new_db_key.redis_key:
                    # Remove old index entries using saved field values
                    for field_name, field in self._meta.fields.items():
                        field_value = self._saved_field_values.get(
                            field_name, getattr(self, field_name)
                        )
                        field.on_delete(
                            model_instance=self,
                            field_name=field_name,
                            field_value=field_value,
                            pipeline=internal_pipeline,
                            saved_redis_key=obsolete_key,
                            **kwargs,
                        )
                    # Delete old hash and update class set
                    internal_pipeline.delete(obsolete_key)
                    internal_pipeline.srem(
                        self._meta.db_class_set_key.redis_key, obsolete_key
                    )
                    internal_pipeline.sadd(
                        self._meta.db_class_set_key.redis_key,
                        new_db_key.redis_key,
                    )
                # Run on_save for listed fields (adds new index entries)
                for field_name in update_fields:
                    field = self._meta.fields[field_name]
                    field.on_save(
                        self,
                        field_name=field_name,
                        field_value=getattr(self, field_name),
                        ignore_errors=ignore_errors,
                        pipeline=internal_pipeline,
                        **kwargs,
                    )
                # Handle TTL/expire_at
                if self._ttl is not None:
                    internal_pipeline.expire(new_db_key.redis_key, self._ttl)
                elif self._expire_at is not None:
                    internal_pipeline.expireat(
                        new_db_key.redis_key, int(self._expire_at.timestamp())
                    )
                results = internal_pipeline.execute()
                db_response = results[0]  # HSET result
                self._is_persisted = True
                self._redis_key = new_db_key.redis_key
                # Merge into saved_field_values (preserve existing, update listed)
                for field_name in update_fields:
                    self._saved_field_values[field_name] = getattr(self, field_name)
                # WriteFilterMixin: tag priority after successful partial save
                if isinstance(self, WriteFilterMixin):
                    self._tag_priority()
                # EventStreamMixin: log mutation after successful partial save
                if isinstance(self, EventStreamMixin):
                    _op = "create" if _is_create else "update"
                    self._xadd_mutation(_op, update_fields=update_fields)
                return db_response

        # Full save path (existing behavior, unchanged)
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
                pipeline = pipeline.srem(
                    self._meta.db_class_set_key.redis_key,
                    self.obsolete_redis_key,
                )  # 4a - remove old key from class set
                pipeline.delete(self.obsolete_redis_key)  # 4b
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
            # WriteFilterMixin: tag priority after successful save
            if isinstance(self, WriteFilterMixin):
                self._tag_priority(pipeline=pipeline)
            # EventStreamMixin: log mutation after successful save
            if isinstance(self, EventStreamMixin):
                _op = "create" if _is_create else "update"
                self._xadd_mutation(_op, pipeline=pipeline)
            return pipeline

        else:
            # Use internal pipeline for atomic execution (all-or-nothing)
            internal_pipeline = POPOTO_REDIS_DB.pipeline()

            internal_pipeline.hset(new_db_key.redis_key, mapping=hset_mapping)  # 1
            if self._ttl is not None:
                internal_pipeline.expire(new_db_key.redis_key, self._ttl)  # 2
            elif self._expire_at is not None:
                internal_pipeline.expireat(
                    new_db_key.redis_key, int(self._expire_at.timestamp())
                )  # 2
            internal_pipeline.sadd(
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
                        pipeline=internal_pipeline,
                        saved_redis_key=self.obsolete_redis_key,
                        **kwargs,
                    )
                internal_pipeline.srem(
                    self._meta.db_class_set_key.redis_key,
                    self.obsolete_redis_key,
                )  # 4a - remove old key from class set
                internal_pipeline.delete(self.obsolete_redis_key)  # 4b
                self.obsolete_redis_key = None

            for field_name, field in self._meta.fields.items():  # 5
                field.on_save(  # 5
                    self,
                    field_name=field_name,
                    field_value=getattr(self, field_name),
                    # ttl=ttl, expire_at=expire_at,
                    ignore_errors=ignore_errors,
                    pipeline=internal_pipeline,
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
                        internal_pipeline.hdel(index_key, old_hash)
                # Add new index entry
                new_hash = self._meta.compute_index_hash(self, field_names_tuple)
                if new_hash:
                    internal_pipeline.hset(index_key, new_hash, new_db_key.redis_key)

            # pipeline.execute() is synchronous in redis-py: it blocks until
            # all responses are received, guaranteeing write visibility after return
            results = internal_pipeline.execute()
            db_response = results[0]  # HSET result (backward compat)

            self._is_persisted = True
            self._redis_key = new_db_key.redis_key  # 7
            # Store field values for proper cleanup on delete  # 8
            self._saved_field_values = {
                field_name: getattr(self, field_name)
                for field_name in self._meta.fields.keys()
            }
            # WriteFilterMixin: tag priority after successful save
            if isinstance(self, WriteFilterMixin):
                self._tag_priority()
            # EventStreamMixin: log mutation after successful save
            if isinstance(self, EventStreamMixin):
                _op = "create" if _is_create else "update"
                self._xadd_mutation(_op)
            return db_response

    @classmethod
    def create(
        cls,
        pipeline: "Pipeline" = None,
        **kwargs,
    ) -> Union["Pipeline", "Model"]:
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
    def get_or_create(cls, defaults: dict = None, **lookup) -> Tuple["Model", bool]:
        """
        Look up an object by lookup kwargs. If not found, create it with
        lookup + defaults.

        Args:
            defaults: Field values to use only when creating (not for lookup)
            **lookup: Field values to use for lookup AND creation

        Returns:
            (instance, created) tuple where created is True if object was created

        Example:
            user, created = User.get_or_create(
                email="alice@example.com",
                defaults={'name': 'Alice', 'role': 'member'}
            )
        """
        # 1. Try to get existing
        instance = cls.query.get(**lookup)
        if instance:
            return instance, False

        # 2. Create if missing
        create_kwargs = {**lookup}
        if defaults:
            create_kwargs.update(defaults)

        try:
            instance = cls.create(**create_kwargs)
            return instance, True
        except ModelException as e:
            # Race condition: another process created it between get and create
            # Retry get once
            if "unique" in str(e).lower() or "already exists" in str(e).lower():
                instance = cls.query.get(**lookup)
                if instance:
                    return instance, False
            raise  # Re-raise if not a uniqueness issue or retry failed

    @classmethod
    def update_or_create(cls, defaults: dict = None, **lookup) -> Tuple["Model", bool]:
        """
        Look up an object by lookup kwargs. If found, update it with defaults
        and save. If not found, create with lookup + defaults.

        Args:
            defaults: Field values to update (if exists) or use for creation
            **lookup: Field values to use for lookup AND creation

        Returns:
            (instance, created) tuple where created is True if object was created

        Example:
            tracker, created = Tracker.update_or_create(
                session_id=session_id,
                defaults={'last_seen': datetime.now()}
            )
        """
        defaults = defaults or {}

        # 1. Try to get existing
        instance = cls.query.get(**lookup)
        if instance:
            # 2a. Update existing with defaults
            for key, value in defaults.items():
                setattr(instance, key, value)
            instance.save()
            return instance, False

        # 2b. Create if missing
        create_kwargs = {**lookup, **defaults}

        try:
            instance = cls.create(**create_kwargs)
            return instance, True
        except ModelException as e:
            # Race condition: retry as get + update
            if "unique" in str(e).lower() or "already exists" in str(e).lower():
                instance = cls.query.get(**lookup)
                if instance:
                    for key, value in defaults.items():
                        setattr(instance, key, value)
                    instance.save()
                    return instance, False
            raise

    @classmethod
    def load(
        cls,
        db_key: str = None,
        **kwargs,
    ) -> Optional["Model"]:
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

    def delete(
        self,
        pipeline: "Pipeline" = None,
        *args,
        **kwargs,
    ) -> Union["Pipeline", bool]:
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

        # Clean up AccessTrackerMixin keys if applicable
        from ..fields.access_tracker import AccessTrackerMixin

        if isinstance(self, AccessTrackerMixin):
            self._delete_access_tracker_keys(pipeline=pipeline)

        # Clean up WriteFilterMixin keys if applicable
        from ..fields.write_filter import WriteFilterMixin

        if isinstance(self, WriteFilterMixin):
            self._delete_write_filter_keys(pipeline=pipeline)

        # EventStreamMixin: log delete mutation
        from ..fields.event_stream import EventStreamMixin

        if isinstance(self, EventStreamMixin):
            self._xadd_mutation("delete", pipeline=pipeline)

        self._db_content = dict()  # 6
        self._saved_field_values = dict()  # 6

        if db_response is not False:
            pipeline.execute()
            return bool(db_response > 0)
        else:
            return pipeline

    def atomic_increment(
        self,
        field_name: str,
        delta,
        pipeline: "Pipeline" = None,
    ):
        """Atomically increment a numeric field value in Redis.

        Uses a Lua script to read, decode (msgpack), increment, re-encode,
        and write back the field value in a single atomic Redis operation.
        This prevents lost updates from concurrent read-modify-write cycles.

        After the atomic Redis update, the in-memory instance attribute and
        _saved_field_values are updated to reflect the new value. If the
        field is a SortedField, its sorted set index score is also updated
        via ZINCRBY.

        Args:
            field_name: Name of the field to increment. Must be a numeric
                field (int, float, or Decimal type).
            delta: The amount to add. Use negative values to decrement.
                Must not be None. Type should be compatible with the field.
            pipeline: Optional Redis pipeline for batched operations. When
                provided, the Lua script and ZINCRBY are queued but not
                executed -- the caller must call pipeline.execute().

        Returns:
            The new field value after incrementing. Returns the same type
            as the field (int for IntField, float for FloatField, Decimal
            for DecimalField).

        Raises:
            TypeError: If the model has not been saved (no redis_key),
                if the field is not a numeric type, or if delta is None.
            AttributeError: If field_name does not exist on the model.

        Example:
            episode = Episode.query.get(id="abc")
            new_count = episode.atomic_increment('deviation_count', 1)
            # Atomically increments in Redis, returns 1, updates instance

            # Decrement
            new_count = episode.atomic_increment('deviation_count', -1)

            # Float fields
            new_score = episode.atomic_increment('score', 0.5)
        """
        from decimal import Decimal as _Decimal

        from ..redis_db import ENCODING

        # Validate field exists
        if field_name not in self._meta.fields:
            raise AttributeError(
                f"'{self.__class__.__name__}' has no field '{field_name}'"
            )

        # Validate model is saved (must have been persisted to Redis).
        # _db_content is populated only after save(); _redis_key is set in
        # __init__ when KeyField values are provided, so it's not reliable.
        if not self._db_content and not self._saved_field_values:
            raise TypeError(
                "Cannot call atomic_increment on an unsaved model instance. "
                "Save the model first."
            )
        redis_key = self._redis_key or self.db_key.redis_key

        # Validate delta is not None
        if delta is None:
            raise TypeError("delta must be a numeric value, not None")

        # Validate field is numeric
        field = self._meta.fields[field_name]
        numeric_types = (int, float, _Decimal)
        if field.type not in numeric_types:
            raise TypeError(
                f"atomic_increment requires a numeric field (int, float, Decimal). "
                f"'{field_name}' is type {field.type.__name__}"
            )

        field_name_bytes = field_name.encode(ENCODING)

        # Lua script that atomically reads, decodes msgpack, increments,
        # re-encodes, and writes back. Uses cmsgpack which is built into
        # Redis since version 2.6.
        #
        # KEYS[1] = redis hash key
        # ARGV[1] = field name (bytes)
        # ARGV[2] = delta value (string representation)
        # ARGV[3] = 1 if field is Decimal type (uses tagged dict encoding), 0 otherwise
        #
        # Returns the new numeric value as a string.
        lua_script = """
        local current_packed = redis.call('HGET', KEYS[1], ARGV[1])
        local current_val = 0
        local is_decimal = tonumber(ARGV[3])

        if current_packed then
            local decoded = cmsgpack.unpack(current_packed)
            if is_decimal == 1 and type(decoded) == 'table' and decoded['as_encodable'] then
                current_val = tonumber(decoded['as_encodable'])
            elseif type(decoded) == 'number' then
                current_val = decoded
            end
        end

        local delta = tonumber(ARGV[2])
        local new_val = current_val + delta

        if is_decimal == 1 then
            local encoded = cmsgpack.pack({['__Decimal__'] = true, ['as_encodable'] = tostring(new_val)})
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        else
            local encoded = cmsgpack.pack(new_val)
            redis.call('HSET', KEYS[1], ARGV[1], encoded)
        end

        return tostring(new_val)
        """

        is_decimal = 1 if field.type is _Decimal else 0
        delta_str = str(float(delta) if isinstance(delta, _Decimal) else delta)

        if isinstance(pipeline, redis.client.Pipeline):
            # When using a pipeline, register the script and call it
            script = POPOTO_REDIS_DB.register_script(lua_script)
            pipeline = script(
                keys=[redis_key],
                args=[field_name_bytes, delta_str, is_decimal],
                client=pipeline,
            )

            # Update in-memory values optimistically
            current_val = getattr(self, field_name) or field.type()
            if field.type is int:
                new_val = int(current_val) + int(delta)
            elif field.type is _Decimal:
                new_val = _Decimal(str(current_val)) + _Decimal(str(delta))
            else:
                new_val = float(current_val) + float(delta)

            setattr(self, field_name, new_val)
            if field_name in self._saved_field_values or self._saved_field_values:
                self._saved_field_values[field_name] = new_val

            # Update sorted index if field is a SortedField
            if field_name in self._meta.sorted_field_names:
                field_cls = field.__class__
                sortedset_db_key = field_cls.get_partitioned_sortedset_db_key(
                    self, field_name
                )
                score_delta = float(delta) if isinstance(delta, _Decimal) else delta
                pipeline = pipeline.zincrby(
                    sortedset_db_key.redis_key, score_delta, redis_key
                )

            return pipeline
        else:
            # Execute the Lua script directly
            result_str = POPOTO_REDIS_DB.eval(
                lua_script, 1, redis_key, field_name_bytes, delta_str, is_decimal
            )

            # Parse result and convert to field type
            if isinstance(result_str, bytes):
                result_str = result_str.decode(ENCODING)

            if field.type is int:
                # Lua may return "15.0" for integer arithmetic; parse via float then int
                new_val = int(float(result_str))
            elif field.type is _Decimal:
                new_val = _Decimal(result_str)
            else:
                new_val = float(result_str)

            # Update in-memory instance
            setattr(self, field_name, new_val)
            if field_name in self._saved_field_values or self._saved_field_values:
                self._saved_field_values[field_name] = new_val

            # Update sorted index if field is a SortedField
            if field_name in self._meta.sorted_field_names:
                field_cls = field.__class__
                sortedset_db_key = field_cls.get_partitioned_sortedset_db_key(
                    self, field_name
                )
                score_delta = float(delta) if isinstance(delta, _Decimal) else delta
                POPOTO_REDIS_DB.zincrby(
                    sortedset_db_key.redis_key, score_delta, redis_key
                )

            return new_val

    def touch(self, field_name, pipeline=None):
        """Update a DecayingSortedField's timestamp without a full save.

        Refreshes the decay clock by updating the sorted set score to
        the current timestamp. Does not modify the model hash.

        Args:
            field_name: Name of a DecayingSortedField on the model.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The new timestamp (float), or the pipeline if one was provided.

        Raises:
            TypeError: If model is unsaved or field is not a DecayingSortedField.
            AttributeError: If field_name does not exist.
        """
        from ..fields.decaying_sorted_field import DecayingSortedField

        if field_name not in self._meta.fields:
            raise AttributeError(
                f"'{self.__class__.__name__}' has no field '{field_name}'"
            )

        field = self._meta.fields[field_name]
        if not isinstance(field, DecayingSortedField):
            raise TypeError(
                f"touch() requires a DecayingSortedField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        if not self._db_content and not self._saved_field_values:
            raise TypeError(
                "Cannot call touch() on an unsaved model instance. "
                "Save the model first."
            )

        import time

        now = time.time()
        redis_key = self._redis_key or self.db_key.redis_key

        sortedset_db_key = field.__class__.get_partitioned_sortedset_db_key(
            self, field_name
        )

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline.zadd(sortedset_db_key.redis_key, {redis_key: now})
            setattr(self, field_name, now)
            if self._saved_field_values is not None:
                self._saved_field_values[field_name] = now
            return pipeline
        else:
            POPOTO_REDIS_DB.zadd(sortedset_db_key.redis_key, {redis_key: now})
            setattr(self, field_name, now)
            if self._saved_field_values is not None:
                self._saved_field_values[field_name] = now
            return now

    def resolve_pressure(self, field_name, pipeline=None):
        """Reset homeostatic pressure for a CyclicDecayField member.

        Discharges accumulated urgency by updating ``last_resolved`` to
        the current time in the pressure companion hash.

        Args:
            field_name: Name of a CyclicDecayField on the model.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The new last_resolved timestamp (float), or the pipeline
            if one was provided.

        Raises:
            TypeError: If model is unsaved, field is not a CyclicDecayField,
                or pressure_rate is 0.
            AttributeError: If field_name does not exist.
        """
        from ..fields.cyclic_decay_field import CyclicDecayField

        if field_name not in self._meta.fields:
            raise AttributeError(
                f"'{self.__class__.__name__}' has no field '{field_name}'"
            )

        field = self._meta.fields[field_name]
        if not isinstance(field, CyclicDecayField):
            raise TypeError(
                f"resolve_pressure() requires a CyclicDecayField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        if field.pressure_rate <= 0:
            raise TypeError(
                f"resolve_pressure() requires pressure_rate > 0. "
                f"'{field_name}' has pressure_rate={field.pressure_rate}"
            )

        if not self._db_content and not self._saved_field_values:
            raise TypeError(
                "Cannot call resolve_pressure() on an unsaved model instance. "
                "Save the model first."
            )

        import time
        import msgpack

        now = time.time()
        member_key = self._redis_key or self.db_key.redis_key
        pressure_hash_key = field._get_pressure_hash_key(self, field_name)

        pressure_data = {
            "rate": field.pressure_rate,
            "last_resolved": now,
        }
        packed = msgpack.packb(pressure_data)

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline.hset(pressure_hash_key, member_key, packed)
            return pipeline
        else:
            POPOTO_REDIS_DB.hset(pressure_hash_key, member_key, packed)
            return now

    def strengthen_cycle(self, field_name, factor=1.2, pipeline=None):
        """Multiply cycle amplitudes by factor (>1.0 strengthens).

        Reads current cycles from companion hash, multiplies each amplitude
        by factor, writes back. Amplitudes are clamped to [0.0, 100.0].

        Args:
            field_name: Name of a CyclicDecayField on the model.
            factor: Multiplier for amplitudes. Default 1.2.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The updated cycles list, or the pipeline if one was provided.

        Raises:
            TypeError: If model is unsaved or field is not a CyclicDecayField.
            AttributeError: If field_name does not exist.
        """
        return self._adjust_cycle_amplitudes(field_name, factor, pipeline)

    def weaken_cycle(self, field_name, factor=0.8, pipeline=None):
        """Multiply cycle amplitudes by factor (<1.0 weakens).

        Reads current cycles from companion hash, multiplies each amplitude
        by factor, writes back. Amplitudes below 0.01 are treated as zero.

        Args:
            field_name: Name of a CyclicDecayField on the model.
            factor: Multiplier for amplitudes. Default 0.8.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The updated cycles list, or the pipeline if one was provided.

        Raises:
            TypeError: If model is unsaved or field is not a CyclicDecayField.
            AttributeError: If field_name does not exist.
        """
        return self._adjust_cycle_amplitudes(field_name, factor, pipeline)

    def _adjust_cycle_amplitudes(self, field_name, factor, pipeline=None):
        """Internal: multiply all cycle amplitudes by factor with clamping.

        Shared implementation for strengthen_cycle and weaken_cycle.
        Amplitudes are clamped to [0.0, 100.0]. Values below 0.01
        are snapped to 0.0.

        Args:
            field_name: Name of a CyclicDecayField on the model.
            factor: Multiplier for amplitudes.
            pipeline: Optional Redis pipeline for batched operations.

        Returns:
            The updated cycles list, or the pipeline if one was provided.
        """
        from ..fields.cyclic_decay_field import CyclicDecayField

        if field_name not in self._meta.fields:
            raise AttributeError(
                f"'{self.__class__.__name__}' has no field '{field_name}'"
            )

        field = self._meta.fields[field_name]
        if not isinstance(field, CyclicDecayField):
            raise TypeError(
                f"strengthen_cycle()/weaken_cycle() requires a CyclicDecayField. "
                f"'{field_name}' is {type(field).__name__}"
            )

        if not self._db_content and not self._saved_field_values:
            raise TypeError(
                "Cannot adjust cycle amplitudes on an unsaved model instance. "
                "Save the model first."
            )

        import msgpack

        member_key = self._redis_key or self.db_key.redis_key
        cycles_hash_key = field._get_cycles_hash_key(self, field_name)

        # Read current cycles
        raw = POPOTO_REDIS_DB.hget(cycles_hash_key, member_key)
        if not raw:
            # No cycles stored — nothing to adjust
            if isinstance(pipeline, redis.client.Pipeline):
                return pipeline
            return []

        cycles = msgpack.unpackb(raw, raw=False)

        # Multiply each amplitude by factor, clamp to [0.0, 100.0]
        max_amplitude = 100.0
        min_threshold = 0.01
        for cycle in cycles:
            # cycle = [period, amplitude, phase]
            new_amp = cycle[1] * factor
            new_amp = max(0.0, min(new_amp, max_amplitude))
            if new_amp < min_threshold:
                new_amp = 0.0
            cycle[1] = new_amp

        packed = msgpack.packb(cycles)

        if isinstance(pipeline, redis.client.Pipeline):
            pipeline.hset(cycles_hash_key, member_key, packed)
            return pipeline
        else:
            POPOTO_REDIS_DB.hset(cycles_hash_key, member_key, packed)
            return cycles

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

    def to_dict(
        self,
        include=None,
        exclude=None,
        relationships=False,
        max_depth=None,
        _seen=None,
    ) -> dict:
        """Convert the model instance to a plain dictionary.

        Iterates over all explicit (public) fields and collects their current
        values via ``getattr()``.  Relationship fields receive special handling
        so that circular references are safely detected and the caller can
        choose between shallow (redis_key string) and deep (nested dict)
        representations.

        Args:
            include: Optional set or list of field names to include.  When
                provided, only these fields appear in the output.
            exclude: Optional set or list of field names to exclude.
            relationships: If ``True``, recursively call ``to_dict()`` on
                related Model instances to produce nested dicts.  Defaults
                to ``False``, which emits the redis_key string instead.
            max_depth: Maximum recursion depth for nested relationships.
                ``None`` means unlimited.  When the depth counter reaches
                ``0``, related instances fall back to their redis_key string
                regardless of the ``relationships`` flag.
            _seen: Internal set of redis_key strings used for circular
                reference detection.  Callers should not pass this directly.

        Returns:
            A dict mapping field names to their Python values.

        Examples:
            Basic usage -- all fields, relationships as redis_key strings::

                user = User(email="alice@example.com", name="Alice")
                user.to_dict()
                # {'email': 'alice@example.com', 'name': 'Alice'}

            With include/exclude filtering::

                user.to_dict(include={'email'})
                # {'email': 'alice@example.com'}

                user.to_dict(exclude={'name'})
                # {'email': 'alice@example.com'}

            Expanding relationships into nested dicts::

                class Author(Model):
                    name = KeyField()

                class Book(Model):
                    title = KeyField()
                    author = Relationship(model=Author)

                book.to_dict(relationships=True)
                # {'title': 'Hobbit', 'author': {'name': 'Tolkien'}}

            Circular reference protection::

                # When two models reference each other, the second occurrence
                # is serialised as the redis_key string to break the cycle.
                a.to_dict(relationships=True)
                # {'friend': {'friend': 'Person:a'}}
        """
        # Import Relationship at function level to avoid circular imports
        from ..fields.relationship import Relationship

        if include is not None:
            include = set(include)
        if exclude is not None:
            exclude = set(exclude)

        if _seen is None:
            _seen = set()

        # Register current instance to detect circular references
        current_key = self.db_key.redis_key
        _seen = _seen | {current_key}

        result = {}
        for field_name, field in self._meta.explicit_fields.items():
            # Apply include/exclude filtering
            if include is not None and field_name not in include:
                continue
            if exclude is not None and field_name in exclude:
                continue

            value = getattr(self, field_name)

            if isinstance(field, Relationship):
                if value is None:
                    result[field_name] = None
                elif isinstance(value, str):
                    # Lazy-loaded redis_key string
                    if relationships and (max_depth is None or max_depth > 0):
                        if value not in _seen:
                            # Try to load the related instance for expansion
                            related_instance = field.model.query.get(redis_key=value)
                            if related_instance is not None:
                                next_depth = (
                                    None if max_depth is None else max_depth - 1
                                )
                                result[field_name] = related_instance.to_dict(
                                    relationships=relationships,
                                    max_depth=next_depth,
                                    _seen=_seen,
                                )
                            else:
                                result[field_name] = value
                        else:
                            result[field_name] = value
                    else:
                        result[field_name] = value
                elif isinstance(value, Model):
                    related_key = value.db_key.redis_key
                    should_expand = (
                        relationships
                        and (max_depth is None or max_depth > 0)
                        and related_key not in _seen
                    )
                    if should_expand:
                        next_depth = None if max_depth is None else max_depth - 1
                        result[field_name] = value.to_dict(
                            relationships=relationships,
                            max_depth=next_depth,
                            _seen=_seen,
                        )
                    else:
                        result[field_name] = related_key
                else:
                    # Unexpected type -- include raw value
                    result[field_name] = value
            else:
                result[field_name] = value

        return result

    # Async methods using native redis.asyncio
    #
    # Note: async_save and async_delete use to_thread() because they involve
    # complex field hook operations (on_save, on_delete) that would require
    # updating all field classes. async_load uses native async for simple GET.

    async def async_save(
        self,
        pipeline: redis.client.Pipeline = None,
        ignore_errors: bool = False,
        skip_auto_now: bool = False,
        update_fields: list = None,
        migrate_key: bool = False,
        **kwargs,
    ):
        """Async version of save().

        Runs the synchronous save() method in a thread pool to avoid blocking
        the event loop.

        Note:
            Uses to_thread() rather than native async because save() involves
            complex field hook operations (on_save) across multiple field types.
            For most use cases, the thread pool overhead is negligible compared
            to network latency.

        Args:
            pipeline: Optional Redis pipeline for batching operations
            ignore_errors: If True, log errors instead of raising exceptions
            skip_auto_now: If True, suppress auto_now timestamp updates
            update_fields: Optional list of field names for partial save.
                When provided, only listed fields are serialized, validated,
                and indexed.
            migrate_key: If True, allow KeyField value changes (key migration).
            **kwargs: Additional arguments passed to save()

        Returns:
            Pipeline or db_response depending on whether pipeline was provided
        """
        return await to_thread(
            self.save,
            pipeline=pipeline,
            ignore_errors=ignore_errors,
            skip_auto_now=skip_auto_now,
            update_fields=update_fields,
            migrate_key=migrate_key,
            **kwargs,
        )

    async def async_delete(
        self, pipeline: redis.client.Pipeline = None, *args, **kwargs
    ):
        """Async version of delete().

        Runs the synchronous delete() method in a thread pool to avoid blocking
        the event loop.

        Note:
            Uses to_thread() rather than native async because delete() involves
            complex field hook operations (on_delete) across multiple field types.

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

        Note:
            Uses to_thread() because it internally calls save().

        Args:
            pipeline: Optional Redis pipeline for batching operations
            **kwargs: Field values for the new instance

        Returns:
            Pipeline or Model instance depending on whether pipeline was provided
        """
        return await to_thread(cls.create, pipeline=pipeline, **kwargs)

    @classmethod
    async def async_load(cls, db_key: str = None, **kwargs):
        """Async version of load() using native async Redis.

        Loads a model instance from Redis by db_key or field values using
        non-blocking I/O via redis.asyncio.

        Args:
            db_key: Optional db_key string to load
            **kwargs: Field values to construct db_key if not provided

        Returns:
            Model instance or None if not found
        """
        return await cls.query.async_get(db_key=db_key or cls(**kwargs).db_key)

    @classmethod
    async def async_get_or_create(
        cls, defaults: dict = None, **lookup
    ) -> Tuple["Model", bool]:
        """
        Async version of get_or_create.

        Look up an object by lookup kwargs. If not found, create it with
        lookup + defaults.

        Returns:
            (instance, created) tuple where created is True if object was created
        """
        return await to_thread(cls.get_or_create, defaults=defaults, **lookup)

    @classmethod
    async def async_update_or_create(
        cls, defaults: dict = None, **lookup
    ) -> Tuple["Model", bool]:
        """
        Async version of update_or_create.

        Look up an object by lookup kwargs. If found, update with defaults.
        If not found, create with lookup + defaults.

        Returns:
            (instance, created) tuple where created is True if object was created
        """
        return await to_thread(cls.update_or_create, defaults=defaults, **lookup)

    # Bulk operations

    @classmethod
    def bulk_create(cls, instances, batch_size: int = 1000):
        """Create multiple instances efficiently using Redis pipeline.

        Uses Redis pipelines to batch multiple save operations, significantly
        reducing network round-trips compared to individual save() calls.
        For very large datasets, operations are automatically batched to
        prevent excessive memory usage.

        Args:
            instances: List of model instances to create. Each instance should
                be a fully constructed Model object (not yet saved).
            batch_size: Number of operations per pipeline execution (default 1000).
                Lower values use less memory but require more round-trips.

        Returns:
            List of created instances (the same instances passed in, now saved).

        Example:
            restaurants = [
                Restaurant(name="A", rating=4.0),
                Restaurant(name="B", rating=4.5),
                Restaurant(name="C", rating=3.8),
            ]
            created = Restaurant.bulk_create(restaurants)

        Note:
            All instances must be of the same Model class (the class on which
            bulk_create is called). Validation runs on each instance during save.
        """
        if not instances:
            return []

        created = []
        pipeline = POPOTO_REDIS_DB.pipeline()
        count = 0

        for instance in instances:
            instance.save(pipeline=pipeline)
            created.append(instance)
            count += 1

            if count >= batch_size:
                pipeline.execute()
                pipeline = POPOTO_REDIS_DB.pipeline()
                count = 0

        if count > 0:
            pipeline.execute()

        return created

    @classmethod
    def bulk_update(cls, queryset_or_instances, batch_size: int = 1000, **updates):
        """Update multiple instances efficiently using Redis pipeline.

        Applies field updates to all instances matching the queryset or in the
        provided list. Uses Redis pipelines to batch operations for efficiency.

        Args:
            queryset_or_instances: Either a list of Model instances, or the result
                of a query.filter() call (list of instances).
            batch_size: Number of operations per pipeline execution (default 1000).
            **updates: Field name/value pairs to update on each instance.
                Must be valid field names defined on the Model.

        Returns:
            Number of updated instances.

        Raises:
            ModelException: If validation fails on any instance during update.

        Example:
            # Update from queryset
            count = Restaurant.bulk_update(
                Restaurant.query.filter(status="pending"),
                status="active"
            )

            # Update from list
            restaurants = [r1, r2, r3]
            count = Restaurant.bulk_update(restaurants, is_featured=True)

        Note:
            Each instance is fully validated before saving. If an instance
            fails validation, a ModelException is raised.
        """
        if not updates:
            return 0

        # Handle both queryset results (list) and plain lists
        if hasattr(queryset_or_instances, "__iter__"):
            instances = list(queryset_or_instances)
        else:
            instances = [queryset_or_instances]

        if not instances:
            return 0

        pipeline = POPOTO_REDIS_DB.pipeline()
        count = 0
        updated_count = 0

        for instance in instances:
            # Apply updates to the instance
            for field_name, value in updates.items():
                setattr(instance, field_name, value)

            instance.save(pipeline=pipeline)
            updated_count += 1
            count += 1

            if count >= batch_size:
                pipeline.execute()
                pipeline = POPOTO_REDIS_DB.pipeline()
                count = 0

        if count > 0:
            pipeline.execute()

        return updated_count

    @classmethod
    def bulk_delete(cls, queryset_or_instances, batch_size: int = 1000):
        """Delete multiple instances efficiently using Redis pipeline.

        Removes all instances matching the queryset or in the provided list
        from Redis. Uses pipelines for efficient batch deletion.

        Args:
            queryset_or_instances: Either a list of Model instances, or the result
                of a query.filter() call (list of instances).
            batch_size: Number of operations per pipeline execution (default 1000).

        Returns:
            Number of deleted instances.

        Example:
            # Delete from queryset
            count = Restaurant.bulk_delete(
                Restaurant.query.filter(status="inactive")
            )

            # Delete from list
            old_restaurants = [r1, r2, r3]
            count = Restaurant.bulk_delete(old_restaurants)

        Note:
            This method properly cleans up all associated indexes (sorted fields,
            geo fields, unique constraints, etc.) by calling delete() on each
            instance within the pipeline.
        """
        # Handle both queryset results (list) and plain lists
        if hasattr(queryset_or_instances, "__iter__"):
            instances = list(queryset_or_instances)
        else:
            instances = [queryset_or_instances]

        if not instances:
            return 0

        pipeline = POPOTO_REDIS_DB.pipeline()
        count = 0
        deleted_count = 0

        for instance in instances:
            instance.delete(pipeline=pipeline)
            deleted_count += 1
            count += 1

            if count >= batch_size:
                pipeline.execute()
                pipeline = POPOTO_REDIS_DB.pipeline()
                count = 0

        if count > 0:
            pipeline.execute()

        return deleted_count

    @classmethod
    def delete_all(cls, batch_size: int = 1000) -> int:
        """Delete all instances of this model, including all secondary indexes.

        This is a convenience wrapper around bulk_delete() that deletes every
        instance of the model. All secondary indexes (sorted fields, geo fields,
        unique constraints, etc.) are properly cleaned up.

        Args:
            batch_size: Number of instances to delete per pipeline batch.
                Default is 1000.

        Returns:
            Number of instances deleted.

        Example:
            # Delete all restaurants
            deleted = Restaurant.delete_all()
            print(f"Deleted {deleted} restaurants")

            # Clean up multiple models (delete referencing models first)
            for model in [Order, MenuItem, Restaurant]:
                model.delete_all()

        Note:
            When deleting models with Relationships, delete the referencing
            models before the referenced ones to avoid dangling references.
        """
        instances = list(cls.query.all())
        if not instances:
            return 0
        return cls.bulk_delete(instances, batch_size=batch_size)

    @classmethod
    async def async_delete_all(cls, batch_size: int = 1000) -> int:
        """Async version of delete_all().

        Deletes all instances in a thread pool to avoid blocking the event loop.

        Args:
            batch_size: Number of instances to delete per pipeline batch.

        Returns:
            Number of instances deleted.
        """
        return await to_thread(cls.delete_all, batch_size=batch_size)

    @classmethod
    async def async_bulk_create(cls, instances, batch_size: int = 1000):
        """Async version of bulk_create().

        Creates multiple instances using Redis pipeline in a thread pool
        to avoid blocking the event loop.

        Args:
            instances: List of model instances to create
            batch_size: Number of operations per pipeline execution

        Returns:
            List of created instances
        """
        return await to_thread(cls.bulk_create, instances, batch_size=batch_size)

    @classmethod
    async def async_bulk_update(
        cls, queryset_or_instances, batch_size: int = 1000, **updates
    ):
        """Async version of bulk_update().

        Updates multiple instances using Redis pipeline in a thread pool
        to avoid blocking the event loop.

        Args:
            queryset_or_instances: List of instances or queryset result
            batch_size: Number of operations per pipeline execution
            **updates: Field values to update

        Returns:
            Number of updated instances
        """
        return await to_thread(
            cls.bulk_update, queryset_or_instances, batch_size=batch_size, **updates
        )

    @classmethod
    async def async_bulk_delete(cls, queryset_or_instances, batch_size: int = 1000):
        """Async version of bulk_delete().

        Deletes multiple instances using Redis pipeline in a thread pool
        to avoid blocking the event loop.

        Args:
            queryset_or_instances: List of instances or queryset result
            batch_size: Number of operations per pipeline execution

        Returns:
            Number of deleted instances
        """
        return await to_thread(
            cls.bulk_delete, queryset_or_instances, batch_size=batch_size
        )

    # Index rebuild operations

    @classmethod
    def rebuild_indexes(cls, batch_size: int = 1000) -> int:
        """Delete all secondary indexes and reconstruct them from source hash data.

        This method is useful for repairing corrupted indexes, after bulk data
        imports that bypassed normal save() hooks, or when upgrading field types
        that change index structure.

        The rebuild process:
            1. Delete all secondary index keys (sorted sets, key field sets,
               geo indexes, class set, and composite indexes)
            2. SCAN all instance keys matching ClassName:*
            3. For each batch, load instances and re-run on_save() hooks
               via pipeline to reconstruct all indexes
            4. Re-add each instance key to the class set

        Args:
            batch_size: Number of instances to process per pipeline batch.
                Default is 1000. Lower values use less memory but require
                more round-trips.

        Returns:
            Number of instances processed.

        Example:
            # Rebuild all indexes for User model
            count = User.rebuild_indexes()
            print(f"Rebuilt indexes for {count} users")

            # With smaller batches for memory-constrained environments
            count = User.rebuild_indexes(batch_size=100)
        """
        from .encoding import decode_popoto_model_hashmap

        model_name = cls._meta.model_name

        # Step 1: Delete all secondary index keys

        # Delete class set
        POPOTO_REDIS_DB.delete(cls._meta.db_class_set_key.redis_key)

        # Delete sorted field indexes
        for field_name in cls._meta.sorted_field_names:
            field = cls._meta.fields[field_name]
            # Build the base sorted set key pattern
            base_key = field.get_special_use_field_db_key(cls, field_name)
            # Use SCAN to find all keys matching this pattern (handles partitioned fields)
            pattern = base_key.redis_key + "*"
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                POPOTO_REDIS_DB.delete(key)

        # Delete key field index sets
        for field_name in cls._meta.key_field_names:
            field = cls._meta.fields[field_name]
            # Skip auto fields - they don't maintain index sets
            if getattr(field, "auto", False):
                continue
            base_key = field.get_special_use_field_db_key(cls, field_name)
            pattern = base_key.redis_key + ":*"
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                POPOTO_REDIS_DB.delete(key)

        # Delete geo field indexes
        for field_name in cls._meta.geo_field_names:
            field = cls._meta.fields[field_name]
            geo_key = GeoField.get_geo_db_key(cls, field_name)
            POPOTO_REDIS_DB.delete(geo_key.redis_key)

        # Delete composite indexes
        for field_names, is_unique in cls._meta.indexes:
            index_key = cls._meta.get_index_key(tuple(field_names))
            POPOTO_REDIS_DB.delete(index_key)

        # Step 2: SCAN all instance keys and rebuild indexes
        instance_pattern = cls._meta.db_class_key.redis_key + ":*"
        count = 0
        pipeline = POPOTO_REDIS_DB.pipeline()
        batch_count = 0

        for redis_key in POPOTO_REDIS_DB.scan_iter(match=instance_pattern, count=1000):
            # Decode the key to a string
            if isinstance(redis_key, bytes):
                redis_key_str = redis_key.decode("utf-8")
            else:
                redis_key_str = redis_key

            # Filter out non-instance keys (e.g., keys with special prefixes
            # that happen to match the pattern). Instance keys should have
            # exactly the right number of segments.
            key_parts = redis_key_str.split(":")
            if len(key_parts) != cls._meta.db_key_length:
                continue

            # Load the raw hash from Redis
            redis_hash = POPOTO_REDIS_DB.hgetall(redis_key)
            if not redis_hash:
                continue

            # Decode into a model instance
            instance = decode_popoto_model_hashmap(cls, redis_hash)
            if instance is None:
                continue

            # Set the _redis_key so on_save hooks can use the correct key
            instance._redis_key = redis_key_str

            # Re-add to class set
            pipeline.sadd(cls._meta.db_class_set_key.redis_key, redis_key_str)

            # Run on_save for each field to rebuild indexes
            for field_name, field in cls._meta.fields.items():
                field.on_save(
                    instance,
                    field_name=field_name,
                    field_value=getattr(instance, field_name),
                    pipeline=pipeline,
                )

            # Rebuild composite indexes
            for field_names_tuple, is_unique in cls._meta.indexes:
                field_names_t = tuple(field_names_tuple)
                index_key = cls._meta.get_index_key(field_names_t)
                index_hash = cls._meta.compute_index_hash(instance, field_names_t)
                if index_hash:
                    pipeline.hset(index_key, index_hash, redis_key_str)

            count += 1
            batch_count += 1

            if batch_count >= batch_size:
                pipeline.execute()
                pipeline = POPOTO_REDIS_DB.pipeline()
                batch_count = 0

        # Execute any remaining commands in the pipeline
        if batch_count > 0:
            pipeline.execute()

        return count

    @classmethod
    def check_indexes(cls, batch_size: int = 1000) -> dict:
        """Read-only health check that counts orphaned index entries.

        Scans all five index types (class set, key fields, sorted fields,
        geo fields, composite indexes) and checks whether each referenced
        instance key still exists in Redis. Returns a structured dict with
        orphan counts per index type.

        This method makes zero writes to Redis. It is safe to call in
        production at any time. Note that counts are point-in-time snapshots;
        concurrent writes may cause minor discrepancies.

        Args:
            batch_size: Number of EXISTS commands per pipeline batch.
                Default is 1000. Lower values use less memory but require
                more round-trips.

        Returns:
            Dict with orphan counts per index type::

                {
                    'class_set': int,
                    'key_fields': {field_name: int, ...},
                    'sorted_fields': {field_name: int, ...},
                    'geo_fields': {field_name: int, ...},
                    'composite_indexes': {index_key: int, ...},
                    'total': int,
                }

        Example:
            result = User.check_indexes()
            if result['total'] > 0:
                print(f"Found {result['total']} orphaned index entries")
                User.rebuild_indexes()
        """

        def _count_orphans(keys_to_check: list) -> int:
            """Pipeline EXISTS in batches, return count of non-existent keys."""
            orphan_count = 0
            for i in range(0, len(keys_to_check), batch_size):
                batch = keys_to_check[i : i + batch_size]
                pipe = POPOTO_REDIS_DB.pipeline()
                for key in batch:
                    pipe.exists(key)
                results = pipe.execute()
                orphan_count += sum(1 for exists in results if not exists)
            return orphan_count

        def _scan_set_members(set_key: str) -> list:
            """SSCAN all members of a Redis set."""
            members = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.sscan(set_key, cursor, count=1000)
                members.extend(batch)
                if cursor == 0:
                    break
            return members

        def _scan_sorted_set_members(zset_key: str) -> list:
            """ZSCAN all members of a Redis sorted set."""
            members = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.zscan(zset_key, cursor, count=1000)
                members.extend(member for member, _score in batch)
                if cursor == 0:
                    break
            return members

        def _scan_hash_values(hash_key: str) -> list:
            """HSCAN all values of a Redis hash."""
            values = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.hscan(hash_key, cursor, count=1000)
                values.extend(batch.values())
                if cursor == 0:
                    break
            return values

        result = {
            "class_set": 0,
            "key_fields": {},
            "sorted_fields": {},
            "geo_fields": {},
            "composite_indexes": {},
            "total": 0,
        }

        # 1. Check class set
        class_set_key = cls._meta.db_class_set_key.redis_key
        members = _scan_set_members(class_set_key)
        if members:
            result["class_set"] = _count_orphans(members)

        # 2. Check key field sets
        for field_name in cls._meta.key_field_names:
            field = cls._meta.fields[field_name]
            # Skip auto fields - they don't maintain index sets
            if getattr(field, "auto", False):
                continue
            base_key = field.get_special_use_field_db_key(cls, field_name)
            pattern = base_key.redis_key + ":*"
            field_orphans = 0
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                members = _scan_set_members(key)
                if members:
                    field_orphans += _count_orphans(members)
            result["key_fields"][field_name] = field_orphans

        # 3. Check sorted field sets
        for field_name in cls._meta.sorted_field_names:
            field = cls._meta.fields[field_name]
            base_key = field.get_special_use_field_db_key(cls, field_name)
            pattern = base_key.redis_key + "*"
            field_orphans = 0
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                members = _scan_sorted_set_members(key)
                if members:
                    field_orphans += _count_orphans(members)
            result["sorted_fields"][field_name] = field_orphans

        # 4. Check geo fields
        for field_name in cls._meta.geo_field_names:
            geo_key = GeoField.get_geo_db_key(cls, field_name)
            members = _scan_sorted_set_members(geo_key.redis_key)
            field_orphans = 0
            if members:
                field_orphans = _count_orphans(members)
            result["geo_fields"][field_name] = field_orphans

        # 5. Check composite indexes
        for field_names, is_unique in cls._meta.indexes:
            index_key = cls._meta.get_index_key(tuple(field_names))
            values = _scan_hash_values(index_key)
            index_orphans = 0
            if values:
                index_orphans = _count_orphans(values)
            result["composite_indexes"][index_key] = index_orphans

        # Compute total
        result["total"] = (
            result["class_set"]
            + sum(result["key_fields"].values())
            + sum(result["sorted_fields"].values())
            + sum(result["geo_fields"].values())
            + sum(result["composite_indexes"].values())
        )

        return result

    @classmethod
    def clean_indexes(cls, batch_size: int = 1000) -> int:
        """Remove orphaned entries from all secondary indexes.

        Scans all five index types (class set, key fields, sorted fields,
        geo fields, composite indexes) and removes entries that reference
        instance keys which no longer exist in Redis. This is the write
        counterpart to check_indexes().

        Uses SCAN-based iteration (SSCAN, ZSCAN, HSCAN) instead of the
        KEYS command, making it safe to run on production databases.
        For best results, run during low-traffic periods to minimize
        the chance of race conditions with concurrent writes.

        Args:
            batch_size: Number of EXISTS/removal commands per pipeline
                batch. Default is 1000. Lower values use less memory
                but require more round-trips.

        Returns:
            Total number of orphaned index entries removed.

        Example:
            # Check first, then clean
            result = User.check_indexes()
            if result['total'] > 0:
                removed = User.clean_indexes()
                print(f"Removed {removed} orphaned index entries")
        """

        def _collect_orphans(keys_to_check: list) -> list:
            """Pipeline EXISTS in batches, return list of non-existent keys."""
            orphans = []
            for i in range(0, len(keys_to_check), batch_size):
                batch = keys_to_check[i : i + batch_size]
                pipe = POPOTO_REDIS_DB.pipeline()
                for key in batch:
                    pipe.exists(key)
                results = pipe.execute()
                for key, exists in zip(batch, results):
                    if not exists:
                        orphans.append(key)
            return orphans

        def _scan_set_members(set_key: str) -> list:
            """SSCAN all members of a Redis set."""
            members = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.sscan(set_key, cursor, count=1000)
                members.extend(batch)
                if cursor == 0:
                    break
            return members

        def _scan_sorted_set_members(zset_key: str) -> list:
            """ZSCAN all members of a Redis sorted set."""
            members = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.zscan(zset_key, cursor, count=1000)
                members.extend(member for member, _score in batch)
                if cursor == 0:
                    break
            return members

        def _scan_hash_entries(hash_key: str) -> list:
            """HSCAN all key-value pairs of a Redis hash."""
            entries = []
            cursor = 0
            while True:
                cursor, batch = POPOTO_REDIS_DB.hscan(hash_key, cursor, count=1000)
                entries.extend(batch.items())
                if cursor == 0:
                    break
            return entries

        removed = 0

        # 1. Clean class set
        class_set_key = cls._meta.db_class_set_key.redis_key
        members = _scan_set_members(class_set_key)
        if members:
            orphans = _collect_orphans(members)
            if orphans:
                pipe = POPOTO_REDIS_DB.pipeline()
                for orphan in orphans:
                    pipe.srem(class_set_key, orphan)
                pipe.execute()
                removed += len(orphans)

        # 2. Clean key field sets
        for field_name in cls._meta.key_field_names:
            field = cls._meta.fields[field_name]
            # Skip auto fields - they don't maintain index sets
            if getattr(field, "auto", False):
                continue
            base_key = field.get_special_use_field_db_key(cls, field_name)
            pattern = base_key.redis_key + ":*"
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                members = _scan_set_members(key)
                if members:
                    orphans = _collect_orphans(members)
                    if orphans:
                        pipe = POPOTO_REDIS_DB.pipeline()
                        for orphan in orphans:
                            pipe.srem(key, orphan)
                        pipe.execute()
                        removed += len(orphans)

        # 3. Clean sorted field sets
        for field_name in cls._meta.sorted_field_names:
            field = cls._meta.fields[field_name]
            base_key = field.get_special_use_field_db_key(cls, field_name)
            pattern = base_key.redis_key + "*"
            for key in POPOTO_REDIS_DB.scan_iter(match=pattern, count=1000):
                if isinstance(key, bytes):
                    key = key.decode("utf-8")
                members = _scan_sorted_set_members(key)
                if members:
                    orphans = _collect_orphans(members)
                    if orphans:
                        pipe = POPOTO_REDIS_DB.pipeline()
                        for orphan in orphans:
                            pipe.zrem(key, orphan)
                        pipe.execute()
                        removed += len(orphans)

        # 4. Clean geo fields
        for field_name in cls._meta.geo_field_names:
            geo_key = GeoField.get_geo_db_key(cls, field_name)
            members = _scan_sorted_set_members(geo_key.redis_key)
            if members:
                orphans = _collect_orphans(members)
                if orphans:
                    pipe = POPOTO_REDIS_DB.pipeline()
                    for orphan in orphans:
                        pipe.zrem(geo_key.redis_key, orphan)
                    pipe.execute()
                    removed += len(orphans)

        # 5. Clean composite indexes
        for field_names, is_unique in cls._meta.indexes:
            index_key = cls._meta.get_index_key(tuple(field_names))
            entries = _scan_hash_entries(index_key)
            if entries:
                # values are instance redis keys, keys are index hashes
                values_to_check = [value for _hash_field, value in entries]
                orphans = _collect_orphans(values_to_check)
                if orphans:
                    orphan_set = set(orphans)
                    pipe = POPOTO_REDIS_DB.pipeline()
                    for hash_field, value in entries:
                        if value in orphan_set:
                            pipe.hdel(index_key, hash_field)
                    pipe.execute()
                    removed += len(orphan_set)

        return removed

    @classmethod
    async def async_check_indexes(cls, batch_size: int = 1000) -> dict:
        """Async version of check_indexes().

        Runs the synchronous check_indexes() method in a thread pool
        to avoid blocking the event loop.

        Args:
            batch_size: Number of EXISTS commands per pipeline batch.

        Returns:
            Dict with orphan counts per index type (same as check_indexes).

        Example:
            result = await User.async_check_indexes()
            if result['total'] > 0:
                await User.async_rebuild_indexes()
        """
        return await to_thread(cls.check_indexes, batch_size=batch_size)

    @classmethod
    async def async_clean_indexes(cls, batch_size: int = 1000) -> int:
        """Async version of clean_indexes().

        Runs the synchronous clean_indexes() method in a thread pool
        to avoid blocking the event loop.

        Args:
            batch_size: Number of EXISTS/removal commands per pipeline
                batch.

        Returns:
            Total number of orphaned index entries removed.

        Example:
            result = await User.async_check_indexes()
            if result['total'] > 0:
                removed = await User.async_clean_indexes()
        """
        return await to_thread(cls.clean_indexes, batch_size=batch_size)

    @classmethod
    async def async_rebuild_indexes(cls, batch_size: int = 1000) -> int:
        """Async version of rebuild_indexes().

        Runs the synchronous rebuild_indexes() method in a thread pool
        to avoid blocking the event loop.

        Args:
            batch_size: Number of instances to process per pipeline batch.

        Returns:
            Number of instances processed.
        """
        return await to_thread(cls.rebuild_indexes, batch_size=batch_size)

    @classmethod
    def raw_update(
        cls, redis_keys: list, batch_size: int = 1000, **field_values
    ) -> int:
        """Perform direct HSET updates on Redis hashes without hooks or validation.

        This is a low-level bulk update method designed for data migrations and
        transformations. It writes field values directly to Redis using HSET,
        bypassing all ORM machinery (on_save hooks, sorted set index updates,
        auto_now fields, validation, and class set membership).

        Values are msgpack-encoded to match the format used by normal Model.save(),
        so data written by raw_update can be read back by normal Model.query.get().

        Because indexes are NOT updated, you should call rebuild_indexes() after
        raw_update if any SortedField or other indexed fields were modified.

        Args:
            redis_keys: List of Redis key strings to update. Each key should be
                a valid model instance key (e.g., "ClassName:key_value").
            batch_size: Number of HSET commands to pipeline before executing.
                Larger values use more memory but reduce round-trips. Default 1000.
            **field_values: Field name/value pairs to set. Field names must
                correspond to fields defined on the model. Values are encoded
                using the same msgpack encoding as Model.save().

        Returns:
            Number of keys that were updated.

        Example:
            # Update all instances' name field without triggering hooks
            keys = [inst.db_key.redis_key for inst in MyModel.query.all()]
            MyModel.raw_update(keys, name="new_value")

            # Rebuild indexes if sorted/indexed fields were changed
            MyModel.rebuild_indexes()
        """
        if not redis_keys:
            return 0

        from .encoding import TYPE_ENCODER_DECODERS
        from ..redis_db import ENCODING

        import msgpack

        # Pre-encode all field values once (same encoding for every key)
        encoded_mapping = {}
        for field_name, value in field_values.items():
            field = cls._meta.fields.get(field_name)
            field_name_bytes = str(field_name).encode(ENCODING)

            if (
                value is not None
                and field is not None
                and field.type in TYPE_ENCODER_DECODERS
            ):
                encoded_value = msgpack.packb(
                    TYPE_ENCODER_DECODERS[field.type].encoder(value)
                )
            else:
                encoded_value = msgpack.packb(value)

            encoded_mapping[field_name_bytes] = encoded_value

        # Pipeline HSET commands in batches
        count = 0
        pipeline = POPOTO_REDIS_DB.pipeline()
        for i, redis_key in enumerate(redis_keys, start=1):
            pipeline.hset(redis_key, mapping=encoded_mapping)
            count += 1

            if i % batch_size == 0:
                pipeline.execute()
                pipeline = POPOTO_REDIS_DB.pipeline()

        # Execute any remaining commands in the pipeline
        if count % batch_size != 0:
            pipeline.execute()

        return count
