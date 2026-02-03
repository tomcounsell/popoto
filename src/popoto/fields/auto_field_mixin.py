"""
Auto Field Mixin for Popoto Redis ORM.

This module provides automatic UUID-based key generation for models that need
guaranteed uniqueness without requiring the developer to manage identifiers.

Design Philosophy:
------------------
In Redis, every stored object needs a unique key. Popoto's approach mirrors
relational database patterns where you might have a natural composite key
(like country + city) or a synthetic auto-incrementing ID. The AutoFieldMixin
solves the "synthetic ID" case for Redis.

The key insight is that UUID4 provides probabilistically unique identifiers
without requiring coordination (unlike auto-increment, which needs a central
counter). This makes it ideal for distributed systems and eliminates a
potential Redis bottleneck.

Integration with Popoto's Field System:
---------------------------------------
AutoFieldMixin is designed for multiple inheritance with Field via the
Method Resolution Order (MRO). It works with KeyFieldMixin and UniqueKeyField
to create the full AutoKeyField:

    class AutoKeyField(AutoFieldMixin, UniqueKeyField):
        ...

This layered approach allows each mixin to handle its specific concern:
- AutoFieldMixin: UUID generation and validation
- KeyFieldMixin: Index maintenance via Redis Sets
- UniqueKeyField: Uniqueness constraints

Automatic Model Enhancement:
----------------------------
When a model has no explicit KeyFields, Popoto automatically adds a hidden
`_auto_key` field of type AutoKeyField during model initialization. This
ensures every model can be uniquely identified and queried without forcing
developers to think about keys for simple use cases.

Example::

    # Explicit AutoKeyField
    class Article(popoto.Model):
        uuid = popoto.AutoKeyField()
        title = popoto.Field()

    # Implicit _auto_key (added automatically since no KeyFields defined)
    class LogEntry(popoto.Model):
        message = popoto.Field()
        # _auto_key is silently added

    # Both can be saved and retrieved:
    article = Article.create(title="Hello")
    print(article.uuid)  # e.g., "a1b2c3d4e5f6..."

    entry = LogEntry.create(message="Something happened")
    print(entry._auto_key)  # e.g., "f6e5d4c3b2a1..."
"""

import logging
import uuid

import redis

logger = logging.getLogger("POPOTO.field")


class AutoFieldMixin:
    """
    Mixin that provides automatic UUID-based value generation for key fields.

    This mixin adds auto-generation capabilities to any field, though it's
    typically used with KeyFieldMixin to create AutoKeyField. The auto-generated
    value becomes part of the Redis key, ensuring each model instance has a
    unique identifier.

    Why a Mixin?
    ------------
    Using a mixin rather than direct inheritance allows composition of field
    behaviors. A field can be both auto-generated AND sorted, or auto-generated
    AND part of a compound key. This flexibility mirrors Django's field design
    while adapting to Redis's key-value paradigm.

    Key Generation Strategy:
    ------------------------
    Uses UUID4 (random-based) rather than UUID1 (time-based) for several reasons:

    1. No MAC address leakage (privacy)
    2. No clock synchronization requirements (distributed-friendly)
    3. Sufficient uniqueness for typical use cases (122 bits of randomness)

    The default 32-character length uses the full UUID4 hex representation.
    Shorter lengths can be configured via `auto_uuid_length` for cases where
    collision probability is acceptable in exchange for shorter keys.

    Trade-offs:
    -----------
    - AutoKeyFields skip Redis Set indexing (see on_save) because their values
      are unique and not useful for category-based queries
    - Once generated, auto values should not be modified (not enforced, but
      changing them would create a new Redis key, orphaning the old data)

    Attributes:
        auto: Boolean flag indicating this field auto-generates its value.
              Always True for this mixin; exists for field introspection.
        auto_uuid_length: Length of the generated UUID string (default 32).
                          Shorter values increase collision probability.
        auto_id: Deprecated/unused attribute for potential future use.
    """

    # todo: add support for https://github.com/ai/nanoid

    auto: bool = True
    auto_uuid_length: int = 32
    auto_id: str = ""

    def __init__(self, **kwargs):
        """
        Initialize the auto-field mixin with UUID generation settings.

        Follows Popoto's field initialization pattern: call super().__init__
        first to let other mixins in the MRO initialize, then merge this
        mixin's defaults into field_defaults and apply any kwargs overrides.

        This ordering ensures that subclasses can override auto_uuid_length
        or other settings through kwargs while maintaining sensible defaults.

        Args:
            **kwargs: Field configuration options. Relevant options:
                - auto_uuid_length: Override the UUID length (default 32)
                - auto: Override auto-generation (rarely needed)
        """
        super().__init__(**kwargs)
        autokeyfield_defaults = {
            "auto": True,
            "auto_uuid_length": 32,
            "auto_id": "",
        }
        self.field_defaults.update(autokeyfield_defaults)
        # set field options, let kwargs override
        for k, v in autokeyfield_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    @classmethod
    def is_valid(cls, field, value, null_check=True, **kwargs) -> bool:
        """
        Validate that the auto-generated value has the expected length.

        This validation catches cases where a value was manually assigned
        with an incorrect length, which could indicate data corruption or
        a programming error. The length check happens before the parent
        class validation to fail fast on obviously invalid data.

        Args:
            field: The Field instance being validated.
            value: The value to validate (should be a UUID string or None).
            null_check: If True, null values will be checked against field.null.
            **kwargs: Additional validation context (passed to parent).

        Returns:
            True if valid, False otherwise. Logs an error on invalid length.
        """
        if value and len(value) != field.auto_uuid_length:
            logger.error(
                f"auto key value is length {len(value)}. It should be {field.auto_uuid_length}"
            )
            return False
        return super().is_valid(field, value, null_check=null_check, **kwargs)

    def get_new_auto_key_value(self):
        """
        Generate a new UUID4-based identifier string.

        Uses uuid.uuid4().hex to get a lowercase hexadecimal string without
        hyphens, then truncates to the configured length. The hex format
        ensures URL-safe and Redis-key-safe characters.

        Returns:
            A string of length `auto_uuid_length` containing hex characters.
        """
        return uuid.uuid4().hex[: self.auto_uuid_length]

    def set_auto_key_value(self, force: bool = False):
        """
        Set a new auto-generated value as this field's default.

        Called during model instantiation to prepare a fresh UUID for new
        instances. The value is stored as `self.default` so it gets applied
        when the model sets field defaults.

        This is called in Model.__init__ for all fields with `auto=True`,
        ensuring each new model instance starts with its own unique key.

        Args:
            force: If True, generate a new value even if auto=False.
                   Useful for programmatically resetting a field's identity.
        """
        if self.auto or force:
            self.default = self.get_new_auto_key_value()

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
        Handle post-save operations for auto-generated fields.

        Intentionally a no-op that returns the pipeline unchanged. This
        overrides KeyFieldMixin.on_save to prevent auto-fields from being
        added to Redis Sets for value-based indexing.

        Why skip indexing?
        ------------------
        Auto-generated values are unique and random, making Set-based
        indexing pointless: you would have one Set per unique value, each
        containing exactly one model instance. The storage cost would be
        significant with no query benefit.

        Instead, auto-key lookups go directly through the Redis key, which
        already contains the auto value (e.g., "Article:a1b2c3d4...").

        Args:
            model_instance: The model being saved.
            field_name: Name of this field on the model.
            field_value: The auto-generated value being saved.
            pipeline: Redis pipeline for batched operations, or None.
            **kwargs: Additional context (ignored).

        Returns:
            The pipeline unchanged, or None if no pipeline was provided.
        """
        return pipeline if pipeline else None
