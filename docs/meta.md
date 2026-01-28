# Model Meta Options

Model-specific configuration is defined in a special `Meta` inner class, following Django and Peewee ORM conventions. Once defined, access configuration via `ModelClass._meta`, not `ModelClass.Meta`.

## Available Options

### order_by

Define default ordering for query results.

```python
from popoto import Model, KeyField
from popoto.fields.shortcuts import SortedField

class Product(Model):
    name = KeyField()
    price = SortedField()

    class Meta:
        order_by = "price"  # Ascending order by default

# Queries automatically ordered by price
products = Product.query.all()
# Returns: [Product(price=10), Product(price=20), Product(price=30)]

# Descending order
class ProductDescending(Model):
    name = KeyField()
    price = SortedField()

    class Meta:
        order_by = "-price"  # Prefix with '-' for descending

# Override at query time
products = Product.query.all(order_by="-price")  # Descends despite Meta
products = Product.query.filter(name="Widget", order_by="name")  # Override with different field
```

**Features:**
- Supports ascending (`"fieldname"`) and descending (`"-fieldname"`)
- Works with `all()`, `filter()`, and `limit`
- Explicit `order_by` parameter overrides Meta default
- Field must exist or `ModelException` is raised at class definition

---

### ttl

Set Time-To-Live (TTL) for Redis keys in seconds. Models expire automatically after the specified duration.

```python
from popoto import Model, KeyField, Field

class CachedData(Model):
    key = KeyField()
    value = Field()

    class Meta:
        ttl = 3600  # Expires after 1 hour (3600 seconds)

# Instance automatically expires after 1 hour
data = CachedData.create(key="session", value="abc123")
```

**Features:**
- Sets Redis `EXPIRE` on save
- Instance-level override with `_ttl` attribute
- Alternative: use `_expire_at` for absolute timestamp expiration
- Validation: must be positive integer (seconds)

**Instance-Level Override:**

```python
class CachedData(Model):
    key = KeyField()
    value = Field()

    class Meta:
        ttl = 60  # Default: 60 seconds

# Override for specific instance
data = CachedData(key="important", value="data")
data._ttl = 3600  # This instance expires after 1 hour
data.save()

# Disable TTL for specific instance
permanent = CachedData(key="keep", value="forever")
permanent._ttl = None  # No expiration
permanent.save()
```

**Absolute Expiration Time:**

```python
from datetime import datetime, timedelta

data = CachedData(key="event", value="data")
data._expire_at = datetime.now() + timedelta(days=7)  # Expires in 7 days
data.save()
```

**How TTL Works:**
- On `save()`, Redis `EXPIRE` or `EXPIREAT` is called
- TTL is refreshed on every save (not just create)
- After expiration, Redis automatically deletes the key
- Queries will return `None` for expired objects

**Popoto Innovation:**
This is a Popoto-specific feature. Peewee ORM (SQL-based) doesn't have TTL support since SQL databases don't natively support key expiration. This showcases Popoto's Redis-native advantages.

---

### indexes

Multi-column indexes with uniqueness support (inspired by Peewee).

```python
from popoto import Model, KeyField, Field

class Transaction(Model):
    transaction_id = KeyField()
    from_account = Field()
    to_account = Field()
    amount = Field()

    class Meta:
        indexes = (
            # (field_names_tuple, is_unique_boolean)
            (('from_account', 'to_account'), True),   # Unique composite index
            (('to_account',), False),                  # Non-unique single-column index
        )

# Creating transactions
tx1 = Transaction.create(
    transaction_id="tx1",
    from_account="A",
    to_account="B",
    amount=100
)

# This will raise ModelException due to unique constraint
tx2 = Transaction.create(
    transaction_id="tx2",
    from_account="A",  # Same combination
    to_account="B",    # as tx1
    amount=200
)  # ❌ ModelException: Unique index violation
```

**Features:**
- Enforces unique constraints on field combinations
- Supports both unique (`True`) and non-unique (`False`) indexes
- Field names must exist or `ModelException` is raised at class definition
- Index entries are automatically managed on save and delete
- Updates properly handle changing indexed fields

**NULL Handling:**

Following SQL standard behavior, multiple `NULL` values are allowed in unique indexes:

```python
# Both instances can have NULL in the same indexed field
tx1 = Transaction.create(transaction_id="tx1", from_account=None, to_account="B", amount=100)
tx2 = Transaction.create(transaction_id="tx2", from_account=None, to_account="B", amount=200)
# ✅ No conflict - NULLs don't participate in uniqueness checks
```

**Update Handling:**

Updates that would create duplicates are rejected:

```python
tx1 = Transaction.create(
    transaction_id="tx1", from_account="A", to_account="B", amount=100
)
tx2 = Transaction.create(
    transaction_id="tx2", from_account="C", to_account="D", amount=200
)

# Try to change tx2 to match tx1's indexed fields
tx2.from_account = "A"
tx2.to_account = "B"
tx2.save()  # ❌ ModelException: Unique index violation

# But updating non-indexed fields works fine
tx1.amount = 150
tx1.save()  # ✅ OK - indexed fields unchanged
```

**Delete Handling:**

Deleted instances free up their index entries:

```python
tx1 = Transaction.create(
    transaction_id="tx1", from_account="A", to_account="B", amount=100
)
tx1.delete()

# Now another instance can use the same combination
tx2 = Transaction.create(
    transaction_id="tx2", from_account="A", to_account="B", amount=200
)  # ✅ OK - tx1's index entry was cleaned up
```

**Validation:**

Index structure is validated at class definition time:

```python
# These all raise ModelException immediately

class Bad1(Model):
    id = KeyField()
    class Meta:
        indexes = "invalid"  # ❌ Must be tuple/list

class Bad2(Model):
    id = KeyField()
    class Meta:
        indexes = (("field",),)  # ❌ Each index must be 2-tuple

class Bad3(Model):
    id = KeyField()
    class Meta:
        indexes = (("field", True),)  # ❌ Field names must be tuple/list

class Bad4(Model):
    id = KeyField()
    class Meta:
        indexes = ((("field",), "yes"),)  # ❌ is_unique must be boolean

class Bad5(Model):
    id = KeyField()
    class Meta:
        indexes = ((("nonexistent",), True),)  # ❌ Field must exist
```

**Redis Implementation:**

Indexes are stored as Redis HASHes:
- Key: `$Index:ClassName:field1:field2`
- Hash entries: `{hash_of_values: instance_db_key}`
- Efficient O(1) lookups with `HEXISTS`/`HGET`

---

## Complete Example

```python
from popoto import Model, KeyField, Field
from popoto.fields.shortcuts import SortedField

class Session(Model):
    session_id = KeyField()
    user_id = SortedField()
    created_at = SortedField()
    data = Field()

    class Meta:
        order_by = "-created_at"  # Newest sessions first
        ttl = 86400               # Expire after 24 hours

# Usage
session = Session.create(
    session_id="abc123",
    user_id="user_456",
    created_at=1234567890,
    data={"page": "home"}
)

# Query - automatically ordered by created_at descending
recent_sessions = Session.query.filter(user_id__gte=100)

# After 24 hours, session automatically expires from Redis
```

---

## Meta Validation

Meta options are validated at class definition time (not runtime):

```python
# This raises ModelException immediately
class BadModel(Model):
    name = KeyField()

    class Meta:
        order_by = "nonexistent_field"  # ❌ ModelException: field doesn't exist
        ttl = -1                         # ❌ ModelException: must be positive integer
```

---

## Accessing Meta Options

Always access via `_meta`, never via `Meta`:

```python
class Product(Model):
    name = KeyField()

    class Meta:
        order_by = "name"
        ttl = 3600

# ✅ Correct
print(Product._meta.order_by)  # "name"
print(Product._meta.ttl)        # 3600

# ❌ Wrong (Meta class is processed and not directly accessible)
# print(Product.Meta.order_by)  # May not work as expected
```

---

## Reference

Inspired by:
- [Django Model Meta options](https://docs.djangoproject.com/en/stable/ref/models/options/)
- [Peewee Model options](https://docs.peewee-orm.com/en/latest/peewee/models.html#model-options-and-table-metadata)
