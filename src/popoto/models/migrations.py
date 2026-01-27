"""
Migration utilities for Popoto Redis ORM.

This module provides patterns and utilities for migrating data between different
model schemas in Redis. Unlike traditional SQL ORMs where migrations alter table
structure, Redis migrations involve transforming key patterns and data formats
since Redis is schemaless.

Design Philosophy
-----------------
Popoto takes a pragmatic approach to migrations: rather than providing a complex
migration framework with version tracking (like Django or Alembic), it offers
simple patterns that developers can adapt to their specific needs. This reflects
the reality that Redis schema changes are typically:

1. Key pattern changes (e.g., adding new KeyFields)
2. Field additions (usually backward-compatible due to msgpack encoding)
3. Field removals (also backward-compatible)
4. Model renames (require key pattern changes)

The example below demonstrates the canonical migration pattern for renaming keys
when model structure changes. Developers should copy and adapt this pattern rather
than using an automated migration system.

Why No Automatic Migrations?
----------------------------
Unlike SQL databases, Redis doesn't enforce schema. A Popoto model is simply a
Python class that knows how to serialize/deserialize data and generate consistent
key patterns. This means:

- Adding new fields with defaults requires no migration
- Removing fields requires no migration (old data is simply ignored)
- Only key pattern changes require explicit data migration

Example Migration: Adding a KeyField
------------------------------------
When adding a new KeyField to an existing model, the Redis key pattern changes,
requiring data migration::

    from popoto.redis_db import POPOTO_REDIS_DB
    from popoto.models.base import Model
    from popoto.fields.field import Field
    from popoto.fields.shortcuts import KeyField

    # Define the old model structure (for reading existing data)
    class OldModel(Model):
        key = KeyField()
        value = Field()

    # Define the new model structure (for writing migrated data)
    class NewModel(Model):
        key = KeyField()
        key2 = KeyField()  # New key field changes the Redis key pattern
        value = Field()

    # Batch migration using Redis pipeline for performance
    pipeline = POPOTO_REDIS_DB.pipeline()
    redis_keys = OldModel.query.keys()

    for i, old_redis_key in enumerate(redis_keys):
        # Load the old record
        old_record = OldModel.query.get(redis_key=old_redis_key)
        old_record.delete()

        # Create new key pattern (actual save happens via pipeline.rename)
        new_redis_key = NewModel(**{
            field_name: getattr(old_record, field_name)
            for field_name in old_record._meta.fields.keys()
        }).db_key.redis_key

        pipeline.rename(old_redis_key, new_redis_key)

        # Execute in batches to avoid memory issues with large datasets
        if i % 500 == 0:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()

    pipeline.execute()

Performance Considerations
--------------------------
- Always use Redis pipelines for batch operations
- Process in chunks (500-1000 keys) to balance memory usage and round trips
- Consider running migrations during low-traffic periods
- For very large datasets, consider using Redis SCAN instead of keys()

See Also
--------
- :mod:`popoto.models.db_key` - How Redis keys are generated from KeyFields
- :mod:`popoto.models.query` - The Query.keys() method used in migrations
- :mod:`popoto.redis_db` - Pipeline and connection management
"""
