"""Timestampable mixin for automatic creation and modification tracking.

This module provides a reusable mixin that adds automatic timestamp fields to
any Popoto model. It follows Django's convention of `auto_now_add` and `auto_now`
for tracking when records are created and modified, bringing familiar patterns
to Redis-backed models.

Design Philosophy:
    Timestamps are fundamental to most data models but tedious to implement
    repeatedly. By providing Timestampable as a mixin rather than baking
    timestamps into the base Model class, Popoto respects that not every
    use case needs this overhead (e.g., ephemeral cache entries, high-frequency
    time-series data where external timestamps exist).

    The mixin pattern also allows multiple mixins to be composed together,
    enabling a "pick what you need" approach to model capabilities.

Integration with Popoto:
    Timestampable inherits from Model, making it usable via multiple inheritance.
    When a class inherits from Timestampable, the `created_at` and `updated_at`
    fields are registered with ModelOptions just like any other field, and
    their values are automatically managed during save operations via
    DatetimeField's `format_value_pre_save` hook.

Example:
    from popoto import Model, Field
    from popoto.utils.mixins import Timestampable

    class Article(Timestampable):
        title = Field(type=str)
        content = Field(type=str)

    article = Article(title="Hello", content="World")
    article.save()

    print(article.created_at)  # datetime when first saved
    print(article.updated_at)  # datetime of most recent save

    article.content = "Updated content"
    article.save()
    # created_at unchanged, updated_at refreshed to current time

Note:
    The timestamps use `datetime.now()` without timezone awareness. For
    applications requiring timezone support, consider extending this mixin
    or implementing custom DatetimeField subclasses.
"""

from ...models.base import Model
from ...fields.datetime_field import DatetimeField


class Timestampable(Model):
    """Mixin that adds automatic created_at and updated_at timestamp fields.

    Timestampable is designed to be inherited alongside other Model classes
    using Python's multiple inheritance. It provides two datetime fields that
    are automatically populated during save operations:

    - `created_at`: Set once when the record is first saved (auto_now_add=True)
    - `updated_at`: Refreshed to current time on every save (auto_now=True)

    Design Rationale:
        This mixin embodies the DRY (Don't Repeat Yourself) principle. Rather
        than defining timestamp fields on every model that needs them, inherit
        from Timestampable to get consistent, well-tested timestamp behavior.

        The naming convention (`created_at`, `updated_at`) mirrors Django and
        Rails conventions, making Popoto models feel familiar to developers
        from those ecosystems.

    Trade-offs:
        - Adds two fields to every model that inherits this mixin, increasing
          storage per record slightly
        - `datetime.now()` is called during every save operation, adding minimal
          but non-zero overhead
        - Uses naive datetimes (no timezone), which may require conversion for
          distributed systems

    Example:
        # Basic usage with multiple inheritance
        class BlogPost(Timestampable):
            slug = KeyField()
            title = Field(type=str)

        # Created and updated timestamps are handled automatically
        post = BlogPost(slug="my-post", title="My Post")
        post.save()  # Both created_at and updated_at set

        post.title = "Updated Title"
        post.save()  # Only updated_at changes

        # Query by timestamp (requires SortedField for range queries)
        # For timestamp-based queries, consider adding a SortedField wrapper
    """

    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)
