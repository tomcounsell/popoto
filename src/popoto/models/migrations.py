"""
Migration Cookbook for Popoto Redis ORM
=======================================

A comprehensive guide to migrating data when your Popoto model schema
changes. Covers 18 use cases organized by tier, from common field changes
to full index rebuilds.

.. contents:: Table of Contents
   :local:
   :depth: 2


Design Philosophy
-----------------

Popoto takes a pragmatic approach to migrations. Redis is schemaless, so
there are no ALTER TABLE equivalents. Instead, Popoto stores each model
instance as a msgpack-encoded Redis hash at a key like
``ClassName:key1_value:key2_value``. Migrations therefore involve:

- **Field data**: Adding, removing, or transforming hash fields inside
  msgpack-encoded values.
- **Key patterns**: Changing the Redis key itself when KeyFields change.
- **Secondary indexes**: Rebuilding sorted sets, unique indexes, and geo
  indexes that live alongside the primary data.

Because Redis has no transactional DDL, all migrations in this guide
follow the **expand-migrate-contract** pattern for crash safety.


Crash-Safe Pattern: Expand-Migrate-Contract
--------------------------------------------

Every migration should be structured in three phases so that every
intermediate state is valid and the system can recover from a crash at
any point:

1. **Expand** -- Add the new field or structure alongside the old one.
   This is purely additive; nothing breaks if you stop here.
2. **Migrate** -- Backfill data, rebuild indexes, or transform values.
   This step is idempotent; running it twice produces the same result.
3. **Contract** -- Remove the old field, index, or key pattern. This is
   purely destructive cleanup; the old field IS the backup until you
   reach this step.

Example of the pattern applied to renaming a field::

    # Phase 1: EXPAND -- add new_name field to model, deploy code that
    #          reads from new_name with fallback to old_name.
    # Phase 2: MIGRATE -- copy old_name -> new_name for all records.
    # Phase 3: CONTRACT -- remove old_name from model, deploy code that
    #          only reads new_name, then clean up orphaned data.


Three-Tier Save Pattern
------------------------

Popoto provides three levels of persistence, similar to Django:

1. **Full save** -- ``instance.save()`` validates all fields, fires
   ``auto_now`` hooks, and updates all secondary indexes.
2. **Partial save** -- ``instance.save(update_fields=["name", "email"])``
   validates and persists only the listed fields. ``auto_now`` fields
   are NOT automatically included (following Django convention; you must
   list them explicitly).
3. **Raw update** -- ``Model.raw_update(redis_keys, field=value)``
   writes msgpack-encoded values directly to the hash without loading
   the model, skipping all hooks and validation.

Each migration recipe below notes which save tier is appropriate.


Imports Used in This Guide
---------------------------

All examples assume the following imports::

    from popoto.redis_db import POPOTO_REDIS_DB
    from popoto.models.base import Model
    from popoto.fields.field import Field
    from popoto.fields.shortcuts import (
        KeyField, SortedField, AutoKeyField, UniqueKeyField,
        IntField, FloatField, StringField,
    )


.. _tier1:

==================================
Tier 1: Common Migrations
==================================

These are the migrations most projects will encounter. They involve
adding, removing, or modifying fields without changing key patterns.


1. Add a Field
~~~~~~~~~~~~~~

**Situation**: You add a new field to your model class.

**What happens**: Nothing breaks. Existing records in Redis simply lack
the new hash field. When Popoto deserializes an old record, the missing
field returns ``None`` (or the field's ``default`` if set).

**Action required**: None for basic cases. If you need every record to
have a concrete value, see recipe 2 (Add a Field with Backfill).

::

    # Before
    class Product(Model):
        sku = UniqueKeyField()
        name = Field()

    # After -- just add the field
    class Product(Model):
        sku = UniqueKeyField()
        name = Field()
        description = Field(default="")  # old records return ""

    # Verify: load an old record
    product = Product.query.get(sku="ABC-123")
    print(product.description)  # "" (the default)


2. Add a Field with Backfill
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You need every existing record to physically contain the
new field's value in Redis -- for example, a ``SortedField`` that must
appear in its sorted set index, or an ``auto_now``-style timestamp.

**Action required**: Load every record and re-save it. Be aware that
``auto_now`` fields fire on every ``save()``, which may overwrite
timestamps you want to preserve.

::

    class Product(Model):
        sku = UniqueKeyField()
        name = Field()
        description = Field(default="No description provided.")

    # Backfill all records so the field is physically present in Redis
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None:
            product.save(pipeline=pipeline)

        # Execute in batches to control memory
        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()

    pipeline.execute()

To preserve timestamps when backfilling, use ``update_fields`` to skip
``auto_now`` hooks on fields you did not list::

    product.save(update_fields=["description"])
    #   -> only writes "description" to the hash
    #   -> auto_now fields are NOT auto-included (Django convention)
    #   -> explicitly list auto_now fields if you want them updated

    product.save(skip_auto_now=True)
    #   -> saves all fields but skips auto_now hooks


3. Remove a Field
~~~~~~~~~~~~~~~~~~

**Situation**: You remove a field from your model class.

**What happens**: The field definition is gone, but existing Redis
hashes still contain the old field's data as an orphaned hash key.
Additionally, any secondary indexes (sorted sets, unique keys) for that
field become orphaned.

**Action required**: Clean up orphaned data and indexes. The orphaned
data does not cause errors (Popoto ignores unknown hash fields on load),
but it wastes memory.

::

    # Before
    class Product(Model):
        sku = UniqueKeyField()
        name = Field()
        legacy_code = Field()        # removing this
        priority = SortedField()     # removing this too

    # After
    class Product(Model):
        sku = UniqueKeyField()
        name = Field()

    # --- Cleanup orphaned hash fields ---
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Product.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        # Remove the orphaned field from the hash
        pipeline.hdel(redis_key, "legacy_code")
        pipeline.hdel(redis_key, "priority")

        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()

    # --- Cleanup orphaned sorted set index ---
    # SortedField indexes follow the pattern: ClassName:_field_name
    POPOTO_REDIS_DB.delete("Product:_priority")

    # --- Cleanup orphaned unique indexes (if field was UniqueKeyField) ---
    # Unique indexes follow: ClassName:field_name:value
    # Use SCAN to find and delete them
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match="Product:legacy_code:*", count=500
        )
        if keys:
            POPOTO_REDIS_DB.delete(*keys)
        if cursor == 0:
            break


4. Rename a Field
~~~~~~~~~~~~~~~~~~

**Situation**: You rename a field (e.g., ``desc`` -> ``description``).

**What happens**: Old records have the value stored under the old hash
key name. The new field name returns ``None`` until data is migrated.

**Action required**: Copy the value from the old hash key to the new
one, then delete the old key. If the field had secondary indexes, those
must be rebuilt under the new name.

::

    # Before
    class Product(Model):
        sku = UniqueKeyField()
        desc = Field()

    # After
    class Product(Model):
        sku = UniqueKeyField()
        description = Field()

    # --- Migrate hash field names ---
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Product.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")

        # Read old field value, write to new field name, delete old
        old_value = POPOTO_REDIS_DB.hget(redis_key, "desc")
        if old_value is not None:
            pipeline.hset(redis_key, "description", old_value)
            pipeline.hdel(redis_key, "desc")

        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()

    # --- If the field was a SortedField, rename the sorted set ---
    if POPOTO_REDIS_DB.exists("Product:_desc"):
        POPOTO_REDIS_DB.rename("Product:_desc", "Product:_description")

Alternative: Load-delete-recreate pattern (simpler but slower)::

    # This pattern is simpler but involves full serialization round-trips
    for redis_key in Product.query.keys():
        raw_hash = POPOTO_REDIS_DB.hgetall(redis_key)
        if b"desc" in raw_hash:
            # Decode field names
            data = {
                k.decode("utf-8"): v for k, v in raw_hash.items()
            }
            # Rename the key in the dict
            data["description"] = data.pop("desc")
            # Delete and recreate
            POPOTO_REDIS_DB.delete(redis_key)
            if isinstance(redis_key, bytes):
                redis_key = redis_key.decode("utf-8")
            POPOTO_REDIS_DB.hset(redis_key, mapping={
                k.encode("utf-8"): v for k, v in data.items()
            })


5. Add a SortedField Index
~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You promote a plain ``Field`` to a ``SortedField``, or
add a new ``SortedField``. Existing records have no entries in the
sorted set index.

**Action required**: Rebuild the sorted set index from existing data.
Use ``rebuild_indexes()`` for a one-liner, or the manual pattern below
for finer control.

::

    # Before
    class Product(Model):
        sku = UniqueKeyField()
        price = FloatField()

    # After -- price is now a SortedField for range queries
    class Product(Model):
        sku = UniqueKeyField()
        price = SortedField(type=float)

    # One-liner: rebuild all indexes (sorted sets, class set, etc.)
    Product.rebuild_indexes()

Manual alternative for finer control::

    # SortedField index key: ClassName:_field_name
    sorted_set_key = "Product:_price"
    pipeline = POPOTO_REDIS_DB.pipeline()

    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None and product.price is not None:
            # Add to sorted set: score=price, member=redis_key
            pipeline.zadd(sorted_set_key, {product.db_key.redis_key: product.price})

        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()

For partitioned sorted fields (``partition_by``), each partition gets
its own sorted set::

    class Product(Model):
        sku = UniqueKeyField()
        category = KeyField()
        price = SortedField(type=float, partition_by="category")

    # Each category gets its own sorted set:
    # Product:_price:electronics, Product:_price:clothing, etc.
    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None and product.price is not None:
            partition_key = f"Product:_price:{product.category}"
            POPOTO_REDIS_DB.zadd(
                partition_key,
                {product.db_key.redis_key: product.price},
            )


6. Change partition_by
~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You change the ``partition_by`` argument on a
``SortedField``, which changes how sorted sets are keyed.

**What happens**: Old sorted sets are keyed by the old partition value.
Queries using the new partition scheme will miss existing data.

**Action required**: Delete old sorted sets and rebuild with new
partitioning.

::

    # Before: price partitioned by category
    class Product(Model):
        sku = UniqueKeyField()
        category = KeyField()
        region = KeyField()
        price = SortedField(type=float, partition_by="category")

    # After: price partitioned by region
    class Product(Model):
        sku = UniqueKeyField()
        category = KeyField()
        region = KeyField()
        price = SortedField(type=float, partition_by="region")

    # --- Step 1: Delete old partitioned sorted sets ---
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match="Product:_price:*", count=500
        )
        if keys:
            POPOTO_REDIS_DB.delete(*keys)
        if cursor == 0:
            break

    # --- Step 2: Rebuild with new partition scheme ---
    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None and product.price is not None:
            partition_key = f"Product:_price:{product.region}"
            POPOTO_REDIS_DB.zadd(
                partition_key,
                {product.db_key.redis_key: product.price},
            )


.. _tier2:

==================================
Tier 2: Structural Migrations
==================================

These migrations change the Redis key pattern itself, meaning every
record must be moved to a new key. They require more care because the
key is the record's identity in Redis.


7. Add a KeyField
~~~~~~~~~~~~~~~~~~

**Situation**: You add a new ``KeyField`` to an existing model. The
Redis key pattern changes from ``ClassName:old_key`` to
``ClassName:new_key:old_key`` (keys are sorted alphabetically).

**Action required**: Rename every Redis key from the old pattern to the
new pattern, and update all secondary index references.

::

    from popoto.redis_db import POPOTO_REDIS_DB
    from popoto.models.base import Model
    from popoto.fields.field import Field
    from popoto.fields.shortcuts import KeyField

    # Before
    class UserSession(Model):
        session_id = KeyField()
        data = Field()

    # After -- adding user_id as a KeyField
    class UserSession(Model):
        session_id = KeyField()
        user_id = KeyField()  # new key field
        data = Field()

    # Redis key changes: "UserSession:sess_abc" -> "UserSession:sess_abc:user_123"
    # (KeyFields are sorted alphabetically: session_id, user_id)

    # --- Migrate keys using pipeline.rename ---
    pipeline = POPOTO_REDIS_DB.pipeline()

    # Use SCAN to find old keys (they still use the old pattern)
    cursor = 0
    old_keys = []
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match="UserSession:*", count=500
        )
        old_keys.extend(keys)
        if cursor == 0:
            break

    for i, old_redis_key in enumerate(old_keys):
        if isinstance(old_redis_key, bytes):
            old_redis_key = old_redis_key.decode("utf-8")

        # Load old record to get field values
        old_data = POPOTO_REDIS_DB.hgetall(old_redis_key)
        if not old_data:
            continue

        # Determine the new key field value.
        # You must decide what value the new KeyField gets for old records.
        # Options: derive from existing data, set a default, or look up externally.
        new_user_id = "unknown"  # replace with your logic

        # Construct new key (fields sorted alphabetically: session_id, user_id)
        old_session_id = old_redis_key.split(":")[-1]
        new_redis_key = f"UserSession:{old_session_id}:{new_user_id}"

        # Add new field to the hash data
        import msgpack
        pipeline.hset(old_redis_key, "user_id".encode("utf-8"),
                       msgpack.packb(new_user_id))

        # Rename the key atomically
        pipeline.rename(old_redis_key, new_redis_key)

        # Update the class set (tracks all instance keys)
        pipeline.srem("$Class:UserSession", old_redis_key)
        pipeline.sadd("$Class:UserSession", new_redis_key)

        if i % 500 == 0 and i > 0:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()

    pipeline.execute()

    # --- Rebuild any sorted set indexes (members reference the old key) ---
    # See recipe 15 for full index rebuild.


8. Remove a KeyField
~~~~~~~~~~~~~~~~~~~~~

**Situation**: You remove a ``KeyField`` from a model. The Redis key
pattern becomes shorter.

**Action required**: Similar to adding a KeyField -- rename every Redis
key and update index references.

.. warning::

    Removing a KeyField may cause key collisions if multiple old records
    map to the same new key. Check for collisions before migrating.

::

    # Before
    class UserSession(Model):
        session_id = KeyField()
        user_id = KeyField()
        data = Field()

    # After -- removing user_id from keys
    class UserSession(Model):
        session_id = KeyField()
        user_id = Field()  # demoted from KeyField to plain Field
        data = Field()

    # Redis key changes: "UserSession:sess_abc:user_123" -> "UserSession:sess_abc"

    # --- Step 1: Check for collisions ---
    seen_keys = {}
    for redis_key in POPOTO_REDIS_DB.smembers("$Class:UserSession"):
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        parts = redis_key.split(":")
        # Old pattern: UserSession:session_id:user_id
        # New pattern: UserSession:session_id
        new_key = f"{parts[0]}:{parts[1]}"
        if new_key in seen_keys:
            print(f"COLLISION: {redis_key} and {seen_keys[new_key]} "
                  f"both map to {new_key}")
            # Handle collision: merge data, pick winner, or abort
        seen_keys[new_key] = redis_key

    # --- Step 2: Rename keys (only if no collisions) ---
    pipeline = POPOTO_REDIS_DB.pipeline()
    for old_redis_key, new_redis_key in [
        (v, k) for k, v in seen_keys.items()
    ]:
        pipeline.rename(old_redis_key, new_redis_key)
        pipeline.srem("$Class:UserSession", old_redis_key)
        pipeline.sadd("$Class:UserSession", new_redis_key)
    pipeline.execute()

    # Rebuild secondary indexes (see recipe 15)


9. Rename Model Class
~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You rename the model class (e.g., ``User`` ->
``Account``). Every Redis key, sorted set, class set, and unique index
uses the old class name as prefix.

**Action required**: Rename all keys, sorted sets, and class set.

::

    # Before: class User(Model):
    # After:  class Account(Model):

    OLD_NAME = "User"
    NEW_NAME = "Account"

    # --- Step 1: Rename instance keys ---
    pipeline = POPOTO_REDIS_DB.pipeline()
    old_class_set = f"$Class:{OLD_NAME}"
    new_class_set = f"$Class:{NEW_NAME}"

    members = POPOTO_REDIS_DB.smembers(old_class_set)
    for i, old_key in enumerate(members):
        if isinstance(old_key, bytes):
            old_key = old_key.decode("utf-8")

        # Replace class name prefix
        new_key = NEW_NAME + old_key[len(OLD_NAME):]

        pipeline.rename(old_key, new_key)
        pipeline.srem(old_class_set, old_key)
        pipeline.sadd(new_class_set, new_key)

        if i % 500 == 0 and i > 0:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()

    # --- Step 2: Delete old class set ---
    POPOTO_REDIS_DB.delete(old_class_set)

    # --- Step 3: Rename sorted set indexes ---
    # Pattern: ClassName:_field_name
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match=f"{OLD_NAME}:_*", count=500
        )
        for old_key in keys:
            if isinstance(old_key, bytes):
                old_key = old_key.decode("utf-8")
            new_key = NEW_NAME + old_key[len(OLD_NAME):]
            POPOTO_REDIS_DB.rename(old_key, new_key)

            # Also update the members inside the sorted set
            # (they reference old instance keys)
            members_with_scores = POPOTO_REDIS_DB.zrange(
                new_key, 0, -1, withscores=True
            )
            pipe = POPOTO_REDIS_DB.pipeline()
            for member, score in members_with_scores:
                if isinstance(member, bytes):
                    member = member.decode("utf-8")
                if member.startswith(OLD_NAME):
                    new_member = NEW_NAME + member[len(OLD_NAME):]
                    pipe.zrem(new_key, member)
                    pipe.zadd(new_key, {new_member: score})
            pipe.execute()

        if cursor == 0:
            break

    # --- Step 4: Rename unique indexes ---
    # Pattern: ClassName:field_name:value
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match=f"{OLD_NAME}:*", count=500
        )
        for old_key in keys:
            if isinstance(old_key, bytes):
                old_key = old_key.decode("utf-8")
            # Skip instance keys (already handled) and sorted sets (handled above)
            if old_key.startswith(f"{NEW_NAME}:"):
                continue
            new_key = NEW_NAME + old_key[len(OLD_NAME):]
            POPOTO_REDIS_DB.rename(old_key, new_key)
        if cursor == 0:
            break


10. Change Field Type (e.g., Field -> SortedField)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You promote a plain ``Field`` to a ``SortedField`` or
change a field's type. Two cases:

**Case A**: Same field name, just adding an index.
Only the sorted set index needs to be built. See recipe 5.

**Case B**: Different field name (additive-first pattern).
Use expand-migrate-contract.

::

    # Case A: Same name, just promoted to SortedField
    # Before: price = FloatField()
    # After:  price = SortedField(type=float)
    # -> Follow recipe 5 (Add a SortedField Index)

    # Case B: Different name (expand-migrate-contract)
    # Before
    class Product(Model):
        sku = UniqueKeyField()
        price = FloatField()

    # --- Phase 1: EXPAND --- add new field alongside old
    class Product(Model):
        sku = UniqueKeyField()
        price = FloatField()  # keep old field
        sorted_price = SortedField(type=float)  # new indexed field

    # --- Phase 2: MIGRATE --- copy data from old to new
    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None and product.price is not None:
            product.sorted_price = product.price
            product.save()

    # --- Phase 3: CONTRACT --- remove old field
    class Product(Model):
        sku = UniqueKeyField()
        sorted_price = SortedField(type=float)

    # Clean up orphaned "price" hash field
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Product.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        pipeline.hdel(redis_key, "price")

        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()


.. _tier3:

==================================
Tier 3: Data Transformations
==================================

These migrations change the values stored in fields rather than the
schema structure itself.


11. Transform Field Values (e.g., Normalize Emails)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You need to normalize or transform all values for a
field across every record (e.g., lowercase all emails).

**Action required**: Load each record, transform the value, and re-save.
For bulk operations that skip hooks, use ``raw_update()``.

::

    class User(Model):
        email = UniqueKeyField()
        name = Field()

    # --- Normalize emails (full save, triggers all hooks) ---
    for redis_key in User.query.keys():
        user = User.query.get(redis_key=redis_key)
        if user is not None:
            normalized = user.email.strip().lower()
            if normalized != user.email:
                # Since email is a KeyField, changing it changes the
                # Redis key. Popoto handles the old key cleanup on save.
                user.email = normalized
                user.save()

Raw update version (skips hooks and validation, faster for bulk ops)::

    keys = User.query.keys()
    User.raw_update(keys, email="normalized@example.com")

    # For per-record transforms, iterate manually:
    import msgpack
    for redis_key in User.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        old_email = msgpack.unpackb(POPOTO_REDIS_DB.hget(redis_key, "email"))
        normalized = old_email.strip().lower()
        if normalized != old_email:
            User.raw_update([redis_key], email=normalized)


12. Split a Field (e.g., name -> first_name + last_name)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You split one field into two (or more) fields.

**Action required**: Use expand-migrate-contract. Read the old value,
split it, write the new values, then clean up.

::

    # Before
    class Contact(Model):
        contact_id = AutoKeyField()
        name = Field()

    # --- Phase 1: EXPAND --- add new fields alongside old
    class Contact(Model):
        contact_id = AutoKeyField()
        name = Field()            # keep until migration complete
        first_name = Field()
        last_name = Field()

    # --- Phase 2: MIGRATE --- split old value into new fields
    for redis_key in Contact.query.keys():
        contact = Contact.query.get(redis_key=redis_key)
        if contact is not None and contact.name:
            parts = contact.name.split(" ", 1)
            contact.first_name = parts[0]
            contact.last_name = parts[1] if len(parts) > 1 else ""
            contact.save()

    # --- Phase 3: CONTRACT --- remove old field from model
    class Contact(Model):
        contact_id = AutoKeyField()
        first_name = Field()
        last_name = Field()

    # Clean up orphaned "name" hash field
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Contact.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        pipeline.hdel(redis_key, "name")
        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()


13. Merge Fields (e.g., first_name + last_name -> display_name)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You merge two fields into one.

**Action required**: Same expand-migrate-contract pattern as splitting,
but in reverse.

::

    # Before
    class Contact(Model):
        contact_id = AutoKeyField()
        first_name = Field()
        last_name = Field()

    # --- Phase 1: EXPAND --- add merged field
    class Contact(Model):
        contact_id = AutoKeyField()
        first_name = Field()  # keep until migration complete
        last_name = Field()   # keep until migration complete
        display_name = Field()

    # --- Phase 2: MIGRATE --- populate merged field
    for redis_key in Contact.query.keys():
        contact = Contact.query.get(redis_key=redis_key)
        if contact is not None:
            first = contact.first_name or ""
            last = contact.last_name or ""
            contact.display_name = f"{first} {last}".strip()
            contact.save()

    # --- Phase 3: CONTRACT --- remove old fields
    class Contact(Model):
        contact_id = AutoKeyField()
        display_name = Field()

    # Clean up orphaned hash fields
    pipeline = POPOTO_REDIS_DB.pipeline()
    for redis_key in Contact.query.keys():
        if isinstance(redis_key, bytes):
            redis_key = redis_key.decode("utf-8")
        pipeline.hdel(redis_key, "first_name")
        pipeline.hdel(redis_key, "last_name")
        if pipeline.command_stack_size >= 500:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()


14. Change Field Encoding
~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You change how a field's data is stored -- for example,
switching a field type from ``str`` to ``int``, or updating serialization
format.

**Action required**: Re-load and re-save every record. The save path
will re-encode using the new type/format.

::

    # Before: priority stored as string ("1", "2", "3")
    class Task(Model):
        task_id = AutoKeyField()
        priority = Field()  # str

    # After: priority stored as int (1, 2, 3)
    class Task(Model):
        task_id = AutoKeyField()
        priority = IntField()

    # --- Re-encode all records ---
    for redis_key in Task.query.keys():
        task = Task.query.get(redis_key=redis_key)
        if task is not None:
            # The old string value needs to be cast to int
            if isinstance(task.priority, str):
                task.priority = int(task.priority)
            elif task.priority is None:
                task.priority = 0  # or a sensible default
            task.save()


.. _tier4:

==================================
Tier 4: Index Maintenance
==================================

These recipes address index rebuilds and repairs without changing the
model schema itself.


15. Rebuild All Indexes
~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: Indexes have drifted from the data -- perhaps due to a
crash during save, a manual Redis edit, or a migration that updated data
without updating indexes.

**Action required**: Clear and rebuild all secondary indexes for a model.

::

    class Product(Model):
        sku = UniqueKeyField()
        category = KeyField()
        price = SortedField(type=float)

    # One-liner: clears and rebuilds all indexes
    count = Product.rebuild_indexes()
    print(f"Rebuilt indexes for {count} Product records")

    # With smaller batches for memory-constrained environments
    count = Product.rebuild_indexes(batch_size=100)

    # Async version
    count = await Product.async_rebuild_indexes()

``rebuild_indexes()`` handles the full process automatically:

1. Deletes all secondary index keys (sorted sets, class set, geo
   indexes, composite indexes)
2. SCANs all instance keys matching ``ClassName:*``
3. Loads each instance and re-runs ``on_save()`` hooks via pipeline
4. Re-adds each instance key to the class set


16. Add Compound Index
~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You need to add a compound (multi-field) index to enable
efficient filtering on a combination of fields.

**Action required**: Build the compound index from existing data.

.. note::

    Compound indexes use Popoto's ``Meta.indexes`` configuration.
    The rebuild follows the same pattern as recipe 15 -- re-saving
    all records triggers the ``on_save()`` hooks that maintain indexes.

::

    class Product(Model):
        sku = UniqueKeyField()
        category = KeyField()
        brand = KeyField()
        price = SortedField(type=float)

        class Meta:
            indexes = [
                (("category", "brand"), False),  # non-unique compound index
            ]

    # After adding the Meta.indexes entry, rebuild by re-saving all records
    pipeline = POPOTO_REDIS_DB.pipeline()
    count = 0
    for redis_key in Product.query.keys():
        product = Product.query.get(redis_key=redis_key)
        if product is not None:
            product.save(pipeline=pipeline)
            count += 1

        if count % 500 == 0 and count > 0:
            pipeline.execute()
            pipeline = POPOTO_REDIS_DB.pipeline()
    pipeline.execute()
    print(f"Built compound index for {count} Product records")


17. Remove Compound Index
~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: You remove a compound index from ``Meta.indexes``.

**What happens**: The index Redis keys become orphaned.

**Action required**: Delete the orphaned index keys.

::

    # Compound index keys follow the pattern: $Index:ClassName:field1:field2
    # Delete the index set
    index_key = "$Index:Product:category:brand"
    POPOTO_REDIS_DB.delete(index_key)

    # If there are multiple compound indexes to remove, use SCAN
    cursor = 0
    while True:
        cursor, keys = POPOTO_REDIS_DB.scan(
            cursor, match="$Index:Product:*", count=500
        )
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8")
            # Only delete indexes you want to remove
            if key == "$Index:Product:category:brand":
                POPOTO_REDIS_DB.delete(key)
        if cursor == 0:
            break


18. Fix Corrupted Indexes
~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: Indexes are out of sync with the actual data. Sorted set
members reference keys that no longer exist, or the class set contains
stale entries.

**Action required**: Full index rebuild (same as recipe 15). For a
lighter check, use the ``clean`` parameter on ``query.keys()``.

::

    # --- Quick fix: use query.keys(clean=True) ---
    # This removes dangling references from the class set and field
    # indexes. It checks each referenced key and removes entries where
    # the key no longer exists in Redis.
    Product.query.keys(clean=True)

    # --- Full fix: complete index rebuild ---
    # Follow recipe 15 (Rebuild All Indexes) for a thorough rebuild.
    # This is the nuclear option that guarantees index consistency.


19. Migrate datetime KeyField Rows to Canonical Keys
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: A model declares ``KeyField(type=datetime)`` and holds rows
written before 1.9.0.

**What happens**: Before 1.9.0 a row's Redis key came from ``str(value)``,
which carries the UTC offset when a datetime is aware and omits it when it is
naive. Identity therefore depended on how the value happened to decode rather
than on the instant it denotes. From 1.9.0 the key is the *instant*: UTC
normalized, fixed width, ``2026-08-07T05:00:00.123456Z``. The stored value
still keeps the offset the caller supplied -- key and value are deliberately
different projections.

Two consequences to read before upgrading:

- **Key bytes move.** Keys written by >= 1.9.0 for ``KeyField(type=datetime)``
  values are not found by < 1.9.0. Roll readers forward before writers.
- **Three representations now address one row.** An aware ``12:00+07:00``, its
  UTC equivalent ``05:00Z``, and the naive ``05:00`` collapse onto one key. If
  a deployment treated a naive value and an aware UTC value as two distinct
  rows, they are now one. ``audit_datetime_keys()`` reports every such pair
  before anything moves. (This is not new semantics so much as newly consistent
  ones: ``SortedField`` has scored those three identically since #519.)

**Action required**: Audit, resolve, migrate, re-verify. Set the kill switch
first so the fleet keeps writing 1.8.2 key bytes while readers roll forward.

::

    # 1. Deploy 1.9.0 fleet-wide with the kill switch set. Key bytes are
    #    byte-identical to 1.8.2, so nothing moves yet.
    #
    #    POPOTO_DATETIME_KEY_LEGACY=1
    #
    #    Equivalently, in code before any model is defined:
    #    from popoto.fields.constants import Defaults
    #    Defaults.DATETIME_KEY_LEGACY = True

    # 2. Audit. Read-only -- safe against a live keyspace.
    print(Event.audit_datetime_keys())

    # Datetime KeyField audit: Event.at
    #   scanned:     3
    #   canonical:   0
    #   migratable:  1
    #   collisions:  1
    #   unparsable:  0
    #
    #   COLLISION -> Event:2026-08-07T05{&#58;}00{&#58;}00.123456Z
    #     Event:2026/-08/-07 12{&#58;}00{&#58;}00.123456+07{&#58;}00  (legacy-offset)
    #       * note = 'original'
    #     Event:2026/-08/-07 05{&#58;}00{&#58;}00.123456  (legacy-naive)
    #       * note = 'the copy that drifted'
    #     copies DIFFER on: note. Keeping either loses data; reconcile by hand.

    # 3. Resolve any collision by hand -- see recipe 20. The migration
    #    refuses to run at all while one exists.

    # 4. Preview. dry_run=True is the default, so this writes nothing.
    print(Event.migrate_datetime_keys())

    # 5. Apply. Quiesce writers first, or leave the kill switch set fleet-wide
    #    so no live writer is deriving the new form while this produces it.
    print(Event.migrate_datetime_keys(dry_run=False))

    # 6. Re-verify, then lift the kill switch fleet-wide.
    assert Event.audit_datetime_keys().is_clean

The migration is idempotent and resumable: a row already on its canonical key
is skipped, so re-running after a crash costs one scan and changes nothing. It
uses ``RENAMENX`` per row so an unexpected collision fails loudly rather than
clobbering, fixes ``$Class:`` membership, moves side keys whose names embed the
hash key, and runs one ``rebuild_indexes()`` at the end.

A model with no ``KeyField(type=datetime)`` needs none of this: the audit
reports "not applicable" and the kill switch is irrelevant.

**Three things this does not do.** It does not rewrite ``Relationship`` values
on other models that point at a renamed key -- it refuses to run when any
exist, and ``allow_inbound_relationships=True`` acknowledges rather than
repairs them, because a Relationship value is indistinguishable from an
ordinary string field's content and a blind rewrite could corrupt unrelated
data. It does not move a ``BM25Field``, ``EmbeddingField``,
``ConfidenceField``, ``CoOccurrenceField``, or ``ContentField``'s per-instance
data either -- each keys its own Redis structures (or, for ``EmbeddingField``,
filesystem paths) off the instance's redis_key through a path this migration
does not know how to rename, so it refuses to run when the model declares any
of them, and ``allow_orphaned_per_instance_fields=True`` acknowledges rather
than repairs that too. And it never resolves a duplicate pair; that is
recipe 20.

Note that ``rebuild_indexes()`` alone does **not** repair any of this. It
rebuilds indexes from the hashes that exist, and for a row whose stored key
disagrees with its derived key it would write an index entry naming a key with
no hash behind it. From 1.9.0 it skips such rows, counts them on
``result.diverged_count``, and logs a warning pointing here.


20. Repair Rows Duplicated Before 1.9.0
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Situation**: ``audit_datetime_keys()`` reports a COLLISION -- two hashes that
belong on one canonical key.

**What happens**: Before 1.8.2 an aware datetime KeyField value reloaded naive.
So saving an aware value stored it under ``...12:00:00+07:00``, reloading gave
back a naive value, and saving again derived ``...12:00:00`` -- a *different*
key. A second hash was written and the first orphaned. No explicit edit of the
key field was needed; any ``save()`` did it, so a model with a periodic re-save
accumulated orphans for as long as it ran.

1.8.2 stopped new duplicates and repaired none. Both hashes are real records
with real field values, and because writes could land on either after they
diverged, **neither copy is knowably the right one**.

**Action required**: Decide, by hand, which copy survives. There is
deliberately no ``strategy=`` argument: last-write-wins, newest-by-timestamp
and field-wise merge are each convenient and each occasionally destroy the
wrong record, and no default is right often enough to be safe when the library
cannot see what the fields mean.

::

    audit = Event.audit_datetime_keys()

    for collision in audit.collisions:
        print(collision.canonical_redis_key)
        print(collision.differing_fields)   # empty set == copies agree
        for redis_key, fields in collision.contents.items():
            print(redis_key, fields)

Three ways to choose, in rough order of how often they are right:

1. **Copies are identical** (``differing_fields == set()``). The choice is
   free. Delete either hash and re-run the migration.
2. **Copies differ and one is clearly authoritative** -- for example the one
   whose ``updated_at`` is newer, or the one your application has been reading.
   Delete the other.
3. **Copies differ and both hold wanted data.** Merge by hand into whichever
   hash you keep, then delete the other. Only you can say which field wins.

::

    # Having chosen, delete the loser and drop it from the class set.
    loser = "Event:2026/-08/-07 05{&#58;}00{&#58;}00.123456"
    POPOTO_REDIS_DB.delete(loser)
    POPOTO_REDIS_DB.srem(Event._meta.db_class_set_key.redis_key, loser)

    # The collision is gone, so the migration will now run.
    print(Event.migrate_datetime_keys(dry_run=False))
    assert Event.audit_datetime_keys().is_clean

Take a ``DUMP`` of both hashes before deleting either. The audit is read-only
and re-runnable, but a delete is not reversible.


Performance Considerations
---------------------------

- **Always use Redis pipelines** for batch operations. Each pipeline
  batch should contain 500-1000 commands to balance memory and round
  trips.
- **Process in chunks** using the ``pipeline.command_stack_size`` check
  or a simple counter.
- **Run migrations during low-traffic periods**. Migrations that call
  ``save()`` on every record will trigger all hooks and index updates.
- **Use SCAN instead of KEYS** for finding Redis keys. The ``KEYS``
  command blocks Redis for large databases. ``query.keys()`` uses the
  class set (O(N) SMEMBERS), which is preferable when the set is
  accurate. Use SCAN when the class set might be stale.
- **Test on a copy first**. Use ``DUMP`` / ``RESTORE`` or a Redis
  snapshot to test your migration script on a copy of production data.


API Reference
--------------

``Model.save(update_fields=[...])``
    Partial save. Only writes the listed fields to the Redis hash.
    Validates only listed fields. ``auto_now`` fields are NOT
    auto-included; list them explicitly if desired.

``Model.save(skip_auto_now=True)``
    Full save that skips ``auto_now`` field hooks. Useful for backfills
    where you want to preserve original timestamps.

``Model.raw_update(redis_keys, batch_size=1000, **field_values)``
    Write field values directly to Redis hashes without loading the
    model. Accepts Python values and msgpack-encodes them internally.
    Skips all hooks and validation. Returns number of keys updated.

``Model.rebuild_indexes(batch_size=1000)``
    Clear and rebuild all secondary indexes (sorted sets, unique
    indexes, class set) by re-saving every instance. Idempotent.
    Returns a ``RebuildIndexesResult`` -- an ``int`` equal to the number
    of instances indexed, carrying ``.diverged_keys`` and
    ``.diverged_count`` for rows skipped because their stored key
    disagrees with their derived key (see recipe 19).

``Model.audit_datetime_keys()``
    Read-only. Reports ``KeyField(type=datetime)`` rows not on their
    canonical key, and duplicate pairs from before 1.9.0, with a
    per-field diff of both hashes. ``print()`` it. See recipe 19.

``Model.migrate_datetime_keys(dry_run=True, allow_inbound_relationships=False, allow_orphaned_per_instance_fields=False)``
    Move datetime-keyed rows onto their canonical key. Dry run by
    default. Idempotent and resumable. Refuses the whole run on a
    duplicate pair, an inbound ``Relationship`` reference, or a
    ``BM25Field``/``EmbeddingField``/``ConfidenceField``/
    ``CoOccurrenceField``/``ContentField`` declared on the model (its
    per-instance data is keyed off the instance's redis_key and would be
    orphaned by a rename). See recipes 19 and 20.

``await Model.async_rebuild_indexes(batch_size=1000)``
    Async wrapper using ``asyncio.to_thread(Model.rebuild_indexes)``.


See Also
---------

- :mod:`popoto.models.db_key` -- How Redis keys are generated from KeyFields
- :mod:`popoto.models.query` -- The ``Query.keys()`` method used in migrations
- :mod:`popoto.models.encoding` -- Msgpack serialization and custom type handling
- :mod:`popoto.redis_db` -- Pipeline and connection management
"""
