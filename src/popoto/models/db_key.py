"""
Redis Key Generation and Management for Popoto Models.

This module provides the DB_key class, which is the foundation of Popoto's
object identity system. Every Popoto model instance is uniquely identified
by a Redis key, and DB_key handles the construction, parsing, and escaping
of these keys.

Design Philosophy:
    Redis uses simple string keys, but Popoto models need hierarchical,
    structured identifiers that encode both the model type and the values
    of key fields. DB_key solves this by using a colon-delimited format:

        ClassName:key1_value:key2_value:...

    This design allows Redis SCAN and pattern matching to efficiently
    query all instances of a model class or filter by partial key values.

Key Escaping:
    Since colons are used as delimiters, any colon appearing in field values
    must be escaped. DB_key also escapes Redis glob pattern characters to
    prevent injection attacks or accidental pattern matching.

Integration:
    DB_key is used throughout Popoto:
    - Model.db_key property generates the key for persistence
    - Query.get() uses DB_key to look up specific instances
    - ModelOptions stores the class-level key prefix (db_class_key)
    - Field indexes reference objects by their DB_key
"""

from collections.abc import Iterable

from ..redis_db import POPOTO_REDIS_DB, ENCODING


class DB_key(list):
    """A Redis key represented as a list of colon-separated parts.

    The first element is the model class name, followed by the values of each
    ``KeyField`` in definition order. The string form ``ClassName:val1:val2``
    is used as the actual Redis key. Special characters are escaped via
    :meth:`clean` / :meth:`unclean`.

    DB_key extends list to hold the "partials" (segments) of a Redis key.
    When converted to a string, these partials are joined with colons and
    properly escaped to form a valid Redis key.

    This design allows keys to be constructed incrementally and composed
    from other DB_key instances, enabling patterns like:

        class_key = DB_key("User")
        instance_key = DB_key(class_key, user_id)  # "User:123"

    The list inheritance provides natural iteration over key segments,
    which is useful for extracting field values from stored keys.

    Examples:
        >>> key = DB_key("User", "john_doe")
        >>> str(key)
        'User:john_doe'

        >>> key = DB_key("Event", ["2024", "01", "15"])
        >>> str(key)
        'Event:2024:01:15'

        >>> key = DB_key("Config", "db:host")  # colon in value
        >>> str(key)
        'Config:db{&#58;}host'
    """

    def __init__(self, *key_partials):
        """
        Initialize a DB_key from one or more key segments.

        Accepts any combination of strings, DB_key instances, and iterables.
        Nested structures are automatically flattened, allowing flexible
        composition of keys from multiple sources.

        This flattening design supports the common pattern where a model's
        key is built from its class name (itself a DB_key) plus the values
        of its KeyFields (provided as a list).

        Args:
            *key_partials: Strings, DB_key instances, or iterables containing
                key segments. Nested iterables are flattened recursively.

        Examples:
            >>> DB_key("User", "alice")           # Two string partials
            >>> DB_key(["A", "B"], "C")           # Flattened to ["A", "B", "C"]
            >>> DB_key(other_db_key, field_val)  # Compose from existing key
        """

        def flatten(yet_flat):
            if isinstance(yet_flat, Iterable) and not isinstance(
                yet_flat, (str, bytes)
            ):
                for item in yet_flat:
                    yield from flatten(item)
            else:
                yield yet_flat

        super().__init__(flatten(key_partials))

    @classmethod
    def from_redis_key(cls, redis_key):
        """Parse a ``ClassName:val1:val2`` Redis key string into a DB_key.

        This factory method is the inverse of __str__. It parses a
        colon-delimited Redis key back into its component partials,
        unescaping any special characters that were escaped during
        key construction.

        This is essential for extracting field values from stored keys,
        particularly in Query operations that need to return KeyField
        values without fetching the full object from Redis.

        Args:
            redis_key: A Redis key as string or bytes (e.g., from SCAN).

        Returns:
            A new DB_key instance with unescaped partials.

        Examples:
            >>> key = DB_key.from_redis_key(b"User:john_doe")
            >>> key[0]
            'User'
            >>> key[1]
            'john_doe'
        """
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode(ENCODING)
        return cls([DB_key.unclean(partial) for partial in redis_key.split(":")])

    @classmethod
    def clean(cls, value: str) -> str:
        """Escape special Redis glob/key characters in *value*.

        Redis keys can contain any bytes, but Popoto uses colons as delimiters
        and must also prevent accidental glob pattern interpretation. This
        method escapes:
            - Forward slashes (/) -> doubled (//) as the escape character
            - Glob pattern chars ('?*^[]-) -> prefixed with /
            - Colons (:) -> HTML entity style ({&#58;})

        The colon escaping uses an HTML-entity-inspired format rather than
        the slash prefix to make colons visually distinct, since they are
        the most structurally important character to escape.

        Args:
            value: A raw string value to be used as a key segment.

        Returns:
            The escaped string safe for use in Redis keys.
        """
        value = value.replace("/", "//")
        for char in "'?*^[]-":
            value = value.replace(char, f"/{char}")
        value = value.replace(":", "{&#58;}")
        return value

    @classmethod
    def unclean(cls, value: str) -> str:
        """Reverse the escaping applied by :meth:`clean`.

        This is the inverse of clean(), used when parsing stored Redis keys
        back into their original field values. The unescaping order matters:
        colons first, then glob characters, then slashes last (since slashes
        are the escape character).

        Args:
            value: An escaped key segment from a Redis key.

        Returns:
            The original unescaped string value.
        """
        value = value.replace("{&#58;}", ":")
        for char in "'?*^[]-":
            value = value.replace(f"/{char}", char)
        value = value.replace(
            "//",
            "/",
        )
        return value

    def __str__(self):
        """
        Convert to a colon-delimited Redis key string.

        Joins all partials with colons, escaping each segment (unless it
        is already a DB_key, which is assumed to be pre-escaped). This
        produces the final string used as a Redis key.

        Returns:
            The complete Redis key string (e.g., "User:john_doe").
        """
        return ":".join(
            [
                (
                    str(partial)
                    if isinstance(partial, DB_key)
                    else self.clean(str(partial))
                )
                for partial in self
            ]
        )

    @property
    def redis_key(self):
        """The colon-joined string used as the actual Redis key.

        Alias for str(self) providing semantic clarity in Redis operations.

        While DB_key can be converted to a string directly, this property
        makes code more readable when the key is being used specifically
        for Redis commands.

        Returns:
            The Redis key string representation.
        """
        return str(self)

    def exists(self):
        """Return ``True`` if this key exists in Redis.

        Provides a convenient way to verify object existence without
        fetching the full data. Useful for validation before operations
        that assume an object exists.

        Returns:
            True if a Redis key with this value exists, False otherwise.
        """
        return True if POPOTO_REDIS_DB.exists(self.redis_key) > 0 else False

    def get_instance(self, model_class):
        """Load and return a model instance from Redis for this key.

        Fetches the hash stored at this key and deserializes it into
        a model instance. This method bridges the gap between a key
        (object identity) and the actual object (data).

        The model_class parameter is required because DB_key itself
        does not store type information beyond the class name string
        in the first segment.

        Args:
            model_class: The Popoto Model class to instantiate.

        Returns:
            A model instance populated with data from Redis, or None
            if the key does not exist.
        """
        redis_hash = POPOTO_REDIS_DB.hgetall(self.redis_key)
        from .encoding import decode_popoto_model_hashmap

        return decode_popoto_model_hashmap(model_class, redis_hash)
