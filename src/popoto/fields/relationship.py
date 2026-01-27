"""
Relationship field for creating foreign-key-like references between Popoto models.

This module implements the relationship system that allows Popoto models to reference
other models, similar to Django's ForeignKey. The design philosophy centers on using
Redis Sets to maintain bidirectional indexes, enabling efficient queries in both
directions (e.g., "find all Memberships for a Person" or "find the Person for a Membership").

Key Design Decisions:
    1. **Set-Based Indexing**: Rather than storing just the foreign key value, Popoto
       maintains Redis Sets that index the relationship. When you save a Membership
       with person=alice, Popoto adds the Membership's db_key to a Set keyed by
       Alice's db_key. This enables O(1) lookups of "all objects pointing to Alice".

    2. **Lazy Loading**: Related model instances are stored as db_key strings and
       loaded on demand, avoiding expensive eager loading of entire object graphs.

    3. **Django-Style Query Syntax**: Supports double-underscore traversal for
       filtering through relationships (e.g., `Membership.query.filter(person__name="Alice")`).

    4. **Explicit Model Declaration**: Unlike Django's string-based lazy references,
       Popoto requires the related model class to be passed directly. This simplifies
       the implementation but requires careful import ordering.

Example:
    class Person(Model):
        name = KeyField()

    class Membership(Model):
        person = Relationship(model=Person)
        role = Field(type=str)

    alice = Person.create(name="Alice")
    m = Membership.create(person=alice, role="admin")

    # Query through relationship
    memberships = Membership.query.filter(person=alice)
    memberships = Membership.query.filter(person__name="Alice")
"""
import redis
from .field import Field
import logging

from ..models.db_key import DB_key
from ..models.query import QueryException
from ..redis_db import POPOTO_REDIS_DB

logger = logging.getLogger("POPOTO.Relationship")


class Relationship(Field):
    """
    A field that stores references to other model instances, analogous to a ForeignKey.

    The first positional argument is the related Model class. Internally the
    relationship is stored as the related instance's ``redis_key`` string,
    which is lazily loaded back into a full model instance on access. This
    prevents infinite recursion with circular references.

    The Relationship field enables models to reference other models while maintaining
    queryable indexes. When a model instance with a Relationship is saved, Popoto
    automatically maintains a Redis Set that tracks which instances point to each
    related object.

    This bidirectional indexing is the key innovation: traditional key-value stores
    only allow lookup by key, but Popoto's relationship Sets enable efficient
    reverse lookups ("find all X that reference Y") without scanning all records.

    A field value can be one of three types at runtime:

    * ``Model`` instance - fully loaded relationship.
    * ``str`` - a redis_key (lazy-loaded, not yet resolved).
    * ``None`` - no relationship set.

    Attributes:
        model: The related Model class that this field references. Must be a concrete
            Model subclass, not a string reference.
        many: Reserved for future many-to-many support. Currently not fully implemented.
        null: Whether the relationship can be None. Defaults to True, allowing
            optional relationships.

    Redis Data Structure:
        For each (Model, field_name, related_instance) combination, Popoto maintains
        a Set at key: `$RelationshipF:ModelClass:field_name:related_db_key`
        This Set contains the db_keys of all instances that reference the related object.

    Example:
        class Author(Model):
            name = KeyField()

        class Book(Model):
            title = KeyField()
            author = Relationship(model=Author)

        tolkien = Author.create(name="Tolkien")
        Book.create(title="The Hobbit", author=tolkien)
        Book.create(title="LOTR", author=tolkien)

        # Efficient lookup: "all books by Tolkien"
        books = Book.query.filter(author=tolkien)  # Returns both books

    Note:
        The `type` attribute is set to Model (the base class), not the specific
        related model. This is because type checking happens at the Field base
        class level and needs to accept any Model subclass.
    """

    type: "Model" = None
    model: "Model" = None
    many: bool = False
    null: bool = True

    def __init__(self, **kwargs):
        """
        Initialize a Relationship field with the specified related model.

        The initialization defers the Model import to avoid circular dependencies,
        since both Model and Relationship need to reference each other. This is
        a common pattern in ORM design where fields and models are tightly coupled.

        Args:
            **kwargs: Field configuration options including:
                - model: The Model class this relationship points to (required for queries)
                - many: Boolean for many-to-many relationships (future feature)
                - null: Whether None is allowed (default True)
        """
        super().__init__(**kwargs)
        from ..models.base import Model

        relationship_field_defaults = {
            "type": Model,
            "model": None,
            "many": False,
            "null": True,
        }
        self.field_defaults.update(relationship_field_defaults)
        # set field options, let kwargs override
        for k, v in relationship_field_defaults.items():
            setattr(self, k, kwargs.get(k, v))

    def get_filter_query_params(self, field_name) -> set:
        """
        Build the set of valid query parameters for filtering on this relationship.

        This method enables Django-style double-underscore query syntax by traversing
        into the related model and collecting its filterable fields. For example, if
        a Book has an `author` Relationship to Author, and Author has a `name` field,
        this method makes `author__name` a valid filter parameter.

        The implementation deliberately avoids recursive relationship traversal to
        prevent infinite loops (e.g., Person -> Friend -> Person) and to keep query
        compilation tractable.

        Args:
            field_name: The name of this Relationship field on the parent model.

        Returns:
            A set of valid query parameter strings, including:
            - The field name itself (for direct model instance filtering)
            - Chained parameters like `field_name__related_field` for each
              filterable field on the related model
        """
        related_field_filter_query_params = set()
        for related_field_name, related_field in self.model._meta.fields.items():
            if isinstance(related_field, Relationship):
                continue  # not ready for recursive compilation
            for related_query_param in related_field.get_filter_query_params(
                related_field_name
            ):
                related_field_filter_query_params.add(
                    f"{field_name}__{related_query_param}"
                )

        return (
            super()
            .get_filter_query_params(field_name)
            .union(
                {
                    f"{field_name}",
                }
            )
            .union(related_field_filter_query_params)
        )

    @classmethod
    def on_save(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: "Model | str | None",
        pipeline=None,
        **kwargs,
    ):
        """
        Maintain the relationship index Set when a model instance is saved.

        This hook is called automatically during model save operations. It updates
        the Redis Set that indexes which instances point to each related object.
        This is the core mechanism that enables efficient reverse lookups.

        The index key follows the pattern:
            `$RelationshipF:ModelClass:field_name:related_db_key`

        For example, saving a Membership with person=alice would add the
        Membership's db_key to the Set at `$RelationshipF:Membership:person:Person:Alice`.

        Args:
            model_instance: The model instance being saved.
            field_name: The name of the Relationship field.
            field_value: The related Model instance (or None).
            pipeline: Optional Redis pipeline for batched operations.
            **kwargs: Additional arguments (unused, for extensibility).

        Returns:
            The pipeline (for chaining) or None if no pipeline was provided.

        Note:
            When field_value is None, the instance is removed from the index Set.
            This handles the case where a relationship is being cleared.
        """
        from ..models.base import Model

        # Handle different field_value types:
        # - None: relationship was never set or being cleared
        # - Model instance: fully loaded relationship
        # - str: lazy-loaded relationship (redis_key string due to circular reference protection)
        if field_value is None:
            related_db_key = "None"
        elif isinstance(field_value, Model):
            related_db_key = field_value.db_key
        elif isinstance(field_value, str):
            # field_value is the redis_key string (lazy-loaded but never accessed)
            # Expecting format "ClassName:key_value"
            if ":" not in field_value:
                logger.error(
                    f"Invalid redis_key format for {field_name}: {field_value}. Expected 'ClassName:key_value'"
                )
                return pipeline if pipeline else None
            # Parse the redis_key string into DB_key components
            related_db_key = DB_key.from_redis_key(field_value)
        else:
            # Unknown type, log and return without action
            logger.warning(
                f"Unexpected field_value type in on_save: {type(field_value)} for {field_name}"
            )
            return pipeline if pipeline else None

        # on a one-to-many, save the set of many with the related instance
        # add this instance's id to a relationship set based on the related model
        # example: "$RelationshipF:Membership:person:person_db_key"
        relationship_set_db_key = DB_key(
            cls.get_special_use_field_db_key(model_instance, field_name),
            related_db_key,
        )

        if field_value is None:
            if pipeline:
                return pipeline.srem(
                    relationship_set_db_key.redis_key, model_instance.db_key.redis_key
                )
            else:
                return POPOTO_REDIS_DB.srem(
                    relationship_set_db_key.redis_key, model_instance.db_key.redis_key
                )
        else:
            if pipeline:
                return pipeline.sadd(
                    relationship_set_db_key.redis_key, model_instance.db_key.redis_key
                )
            else:
                return POPOTO_REDIS_DB.sadd(
                    relationship_set_db_key.redis_key, model_instance.db_key.redis_key
                )

    @classmethod
    def on_delete(
        cls,
        model_instance: "Model",
        field_name: str,
        field_value: "Model | str | None",
        pipeline: redis.client.Pipeline = None,
        **kwargs,
    ):
        """
        Clean up the relationship index Set when a model instance is deleted.

        This hook ensures referential integrity by removing the deleted instance
        from the relationship index. Without this cleanup, queries would return
        stale references to deleted objects.

        Args:
            model_instance: The model instance being deleted.
            field_name: The name of the Relationship field.
            field_value: The related Model instance (or None).
            pipeline: Optional Redis pipeline for batched operations.
            **kwargs: Additional arguments (unused, for extensibility).

        Returns:
            The pipeline (for chaining) or the result of SREM if no pipeline.

        Warning:
            There is a known edge case where lazy-loaded relationships may not
            be fully hydrated at delete time. If the relationship was accessed
            through a chain (e.g., person.friend.friend), the field_value may
            be a key string rather than a Model instance, causing this method
            to fail when accessing field_value.db_key.
        """
        # todo: it's possible this instance is not fully loaded or has been changed.
        #  Need to reload from db before deleting
        #  Example: on person.friend.delete() the person.friend.friend will have field_value as a keystring
        #  It will not be a model instance, so this method will fail on field_value.db_key below
        from ..models.base import Model

        # Handle different field_value types:
        # - None: relationship was never set
        # - Model instance: fully loaded relationship
        # - str: lazy-loaded relationship (redis_key string due to circular reference protection)
        if field_value is None:
            related_db_key = "None"
        elif isinstance(field_value, Model):
            related_db_key = field_value.db_key
        elif isinstance(field_value, str):
            # field_value is the redis_key string (lazy-loaded but never accessed)
            # Expecting format "ClassName:key_value"
            if ":" not in field_value:
                logger.error(
                    f"Invalid redis_key format for {field_name}: {field_value}. Expected 'ClassName:key_value'"
                )
                return pipeline if pipeline else None
            # Parse the redis_key string into DB_key components
            related_db_key = DB_key.from_redis_key(field_value)
        else:
            # Unknown type, log and return without action
            logger.warning(
                f"Unexpected field_value type in on_delete: {type(field_value)} for {field_name}"
            )
            return pipeline if pipeline else None

        relationship_set_db_key = DB_key(
            cls.get_special_use_field_db_key(model_instance, field_name),
            related_db_key,
        )
        # Use saved_redis_key if provided, otherwise fall back to current db_key
        member_key = kwargs.get("saved_redis_key", model_instance.db_key.redis_key)
        if pipeline:
            return pipeline.srem(relationship_set_db_key.redis_key, member_key)
        else:
            return POPOTO_REDIS_DB.srem(relationship_set_db_key.redis_key, member_key)

    @classmethod
    def filter_query(cls, model: "Model", field_name: str, **query_params) -> set:
        """
        Execute a filter query on a Relationship field, returning matching db_keys.

        This method handles two types of queries:

        1. **Direct Instance Filtering** (`field_name=instance`):
           Looks up the relationship index Set directly. For example,
           `Membership.query.filter(person=alice)` retrieves the Set at
           `$RelationshipF:Membership:person:Person:Alice`.

        2. **Chained Field Filtering** (`field_name__related_field=value`):
           First queries the related model to find matching instances, then
           looks up each of their relationship index Sets. For example,
           `Membership.query.filter(person__name="Alice")` first finds all
           Persons named Alice, then retrieves all Memberships pointing to them.

        The chained approach supports recursive traversal through multiple
        relationships, though this can become expensive for deep chains.

        Args:
            model: The Model class being queried.
            field_name: The name of the Relationship field.
            **query_params: Filter parameters (e.g., person=alice or person__name="Alice").

        Returns:
            A set of db_key bytes for matching model instances. Returns an empty
            set if no matches are found.

        Raises:
            QueryException: If filtering directly on the field with a non-Model value.

        Performance Note:
            Direct instance filtering is O(1) as it's a single Set lookup.
            Chained filtering requires querying the related model first, then
            performing N Set lookups where N is the number of matching related
            instances. Results are intersected if multiple filters are applied.
        """
        from ..models.base import Model

        keys_lists_to_intersect = list()
        pipeline = POPOTO_REDIS_DB.pipeline()

        for query_param, query_value in query_params.items():

            if query_param == f"{field_name}":
                if not isinstance(query_value, Model):
                    raise QueryException(
                        f"Query filter on Relationship expects model instance. Instead, got {query_value}"
                    )

                relationship_set_db_key = DB_key(
                    cls.get_special_use_field_db_key(model, field_name),
                    query_value.db_key,
                )
                keys_lists_to_intersect.append(
                    POPOTO_REDIS_DB.smembers(relationship_set_db_key.redis_key)
                )

            elif query_param.startswith(f"{field_name}__"):
                field = model._meta.fields[field_name]
                relationship_field_name = query_param.strip(f"{field_name}__").split(
                    "__"
                )[0]

                relationship_field_values = field.model._meta.fields[
                    relationship_field_name
                ].filter_query(
                    model=field.model,
                    field_name=relationship_field_name,
                    **{query_param.strip(f"{field_name}__"): query_value},
                )
                # note: this will be recursive if references another relationship and so forth

                for relationship_field_value in relationship_field_values:
                    relationship_set_db_key = DB_key(
                        cls.get_special_use_field_db_key(model, field_name),
                        relationship_field_value.db_key,
                    )
                    pipeline.smembers(relationship_set_db_key.redis_key)

        keys_lists_to_intersect += pipeline.execute()
        logger.debug(keys_lists_to_intersect)
        if len(keys_lists_to_intersect):
            return set.intersection(
                *[set(key_list) for key_list in keys_lists_to_intersect]
            )
        return set()
