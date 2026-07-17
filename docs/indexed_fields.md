# Indexed Fields

Indexed fields provide secondary indexing for non-key fields, enabling efficient
exact-match queries without making the field part of the model's Redis key (identity).

## The Problem

In Popoto, `KeyField` conflates two concerns:

1. **Identity** -- the field's value forms part of the Redis storage key
2. **Indexing** -- the field is queryable via `filter()`

This means that to query on a field like `email` or `status`, you must make it a
`KeyField`, which changes the model's Redis key structure. If you later rename an
email, the entire Redis key changes, potentially orphaning references.

## The Solution

`IndexedField` and `UniqueField` decouple querying from identity. They maintain
Redis Set indexes (identical to KeyField's indexing mechanism) but do not participate
in the Redis key.

```python
from popoto import Model, AutoKeyField, IndexedField, UniqueField, Field

class User(Model):
    user_id = AutoKeyField()
    email = UniqueField(type=str)       # indexed + unique, NOT part of key
    status = IndexedField(type=str)     # indexed, NOT part of key
    name = Field(type=str)              # not indexed, not part of key
```

The Redis key for this model is based solely on `user_id` (e.g.,
`User:a1b2c3d4...`). The `email` and `status` fields are indexed separately,
enabling queries like:

```python
User.query.filter(email="alice@example.com")
User.query.filter(status="active")
User.query.filter(status__in=["active", "pending"])
```

## Field(indexed=True)

You can also enable indexing on a plain `Field` by passing `indexed=True`:

```python
class Product(Model):
    sku = AutoKeyField()
    category = Field(type=str, indexed=True)          # indexed, not unique
    barcode = Field(type=str, indexed=True, unique=True)  # indexed + unique
```

This is functionally identical to using `IndexedField` and `UniqueField` shortcuts.

## IndexedField

`IndexedField` is a shortcut for `Field(indexed=True)`. It creates a non-key field
with Set-based secondary indexing.

```python
from popoto import Model, AutoKeyField, IndexedField

class Order(Model):
    order_id = AutoKeyField()
    status = IndexedField(type=str)
    region = IndexedField(type=str, null=True)
```

### Supported Query Lookups

IndexedField supports the same lookups as KeyField:

| Lookup | Description | Redis Operation |
|--------|-------------|-----------------|
| `status=` | Exact match | SMEMBERS |
| `status__in=` | Match any value in list | SUNION |
| `status__isnull=` | Null / non-null check | SMEMBERS / SCAN |
| `status__startswith=` | Prefix match | SCAN + SMEMBERS |
| `status__endswith=` | Suffix match | SCAN + SMEMBERS |

```python
# Exact match
Order.query.filter(status="shipped")

# IN query -- efficient server-side SUNION
Order.query.filter(status__in=["pending", "processing"])

# Null check
Order.query.filter(region__isnull=True)

# Pattern matching (uses SCAN, slower on large datasets)
Order.query.filter(region__startswith="US-")
```

### Combining with Other Filters

Indexed field filters compose with all other filter types using AND logic:

```python
from popoto import SortedField

class Product(Model):
    product_id = AutoKeyField()
    category = IndexedField(type=str)
    price = SortedField(type=float)

# Combine indexed field filter with sorted field range query
affordable_electronics = Product.query.filter(
    category="electronics",
    price__lte=50.0,
)
```

## UniqueField

`UniqueField` is a shortcut for `Field(indexed=True, unique=True)`. It adds a
per-value uniqueness constraint on top of the secondary index.

```python
from popoto import Model, AutoKeyField, UniqueField, Field

class User(Model):
    user_id = AutoKeyField()
    email = UniqueField(type=str)
    name = Field(type=str)

user1 = User.create(email="alice@example.com", name="Alice")

# Attempting a duplicate email raises ModelException
try:
    user2 = User.create(email="alice@example.com", name="Not Alice")
except Exception as e:
    print(e)
    # => Uniqueness violation on User.email: value 'alice@example.com' is already taken
```

### Constraints

- `UniqueField` cannot be null (`null=False` is enforced)
- Setting `unique=False` raises `ModelException`
- Setting `null=True` raises `ModelException`

### Concurrency Guarantee

`UniqueField` enforces uniqueness inside an atomic server-side Lua script
(`INDEX_SWAP_LUA`). Under concurrent cross-process writes, the script runs as a
single Redis command: it reads the current index pointer, performs the uniqueness
check, and moves the record to the new Set — all without a race window.

When a caller provides an external Redis pipeline, a best-effort SMEMBERS pre-check
is made before queuing the EVAL. The authoritative guarantee is still enforced at
`pipeline.execute()` time when the Lua script runs on the server; the pre-check only
surfaces conflicts earlier for the common case.

## How It Works

### Index Key Pattern

Indexed fields maintain Redis Sets following this pattern:

```
$IndexF:ModelName:field_name:value -> Set of redis_keys
$UniquF:ModelName:field_name:value -> Set of redis_keys (UniqueField)
```

For example, a `User` model with `status = IndexedField(type=str)`:

```
$IndexF:User:status:active    -> {User:abc123, User:def456}
$IndexF:User:status:inactive  -> {User:ghi789}
```

This mirrors the `$KeyF` pattern used by KeyField.

### Save Behavior

When a model instance is saved, an atomic server-side Lua script (`INDEX_SWAP_LUA`)
handles the index update in a single Redis command:

1. Reads a server-authoritative pointer to identify which Set the record currently
   belongs to
2. If `unique=True`, scans the target Set for any member other than the current record
   and raises `ModelException` immediately on conflict
3. Atomically removes the record from the old Set, adds it to the new Set, updates the
   pointer, and writes the field bytes — no race window between any of these steps

This eliminates the stale-snapshot SREM+SADD race that existed in earlier versions.

For `Model.save()` calls that don't pass an external pipeline (the common case,
including `Model.create()`), the uniqueness EVAL for `IndexedField`/`UniqueField`
values runs *before* any other write for that record is issued. A genuine uniqueness
conflict therefore raises `ModelException` without leaving any trace — no orphaned
hash, no class-set membership, nothing to clean up.

### Operator Note

The server-authoritative pointer is stored in a standalone Redis key (a plain
string, `GET`/`SET`), **not** as a field inside the model hash. `redis-cli HGETALL
<Model:key>` shows only your declared fields — no extra bookkeeping field. The
pointer key itself follows the pattern:

```
<Model:key>\x00idxptr\x00<field_name>  ->  $IndexF:ModelName:field_name:current_value
```

- Do NOT edit or delete it manually. Removing it reopens the index-stranding race until
  the record's next save.
- It is invisible to the Python model API: `filter()`, attribute access, and
  `is_valid()` all ignore it (it isn't part of the model hash at all).
- The `$IndexF:` and `$UniquF:` Set schema itself is unchanged from earlier versions.

!!! note "Versions prior to this fix (1.8.0, see issue #476)"
    An earlier 1.8.0 revision stored this pointer as a `{field_name}\x00idxset`
    field **inside** the model hash. That is forward-incompatible: any decoder
    that unconditionally `msgpack.unpackb()`s every hash field (all pre-1.8.0
    releases) crashes with `msgpack.exceptions.ExtraData` on such records.
    Current code never writes that in-hash field. Records written by the
    affected 1.8.0 revision are read as a migration fallback and self-heal
    (the field is scrubbed and the pointer moves to the side key) the next
    time they're saved. A full clean cut-over is optional:

    ```python
    User.rebuild_indexes()   # optional — not required for correctness
    ```

### Delete Behavior

When a model instance is deleted, the field hooks that clean up secondary indexes
(including reading the pointer above) run *before* the model hash itself is
physically removed, so the correct current Set is always found — the instance's
Redis key is then removed from that index Set, and the pointer key is cleaned up.

## Comparison Table

| Feature | Field | IndexedField | UniqueField | KeyField | UniqueKeyField |
|---------|-------|-------------|-------------|----------|----------------|
| Part of Redis key | No | No | No | Yes | Yes |
| Queryable via filter() | No | Yes | Yes | Yes | Yes |
| Exact match | No | Yes | Yes | Yes | Yes |
| `__in` lookup | No | Yes | Yes | Yes | Yes |
| `__startswith` / `__endswith` | No | Yes | Yes | Yes | Yes |
| `__isnull` lookup | No | Yes | Yes | Yes | Yes |
| `__contains` lookup | No | No | No | Yes | No |
| Uniqueness enforced | No | No | Yes | No | Yes |
| Can be null | Yes | Yes | No | Yes | No |

## Immutable Keys and Key Migration

KeyField values are immutable by default after the initial save. Changing a KeyField
value and calling `save()` raises `KeyMutationError`:

```python
from popoto import KeyMutationError

instance = MyModel.query.get(name="old_name")
instance.name = "new_name"

try:
    instance.save()
except KeyMutationError as e:
    print(e)
    # => KeyField 'name' changed from 'old_name' to 'new_name'. Use save(migrate_key=True).
```

To intentionally migrate a key, pass `migrate_key=True`:

```python
instance.name = "new_name"
instance.save(migrate_key=True)  # Succeeds, old key is cleaned up
```

The `migrate_key=True` flag handles the full migration: deleting the old Redis hash,
removing old index entries (sorted sets, geo sets, unique constraints, indexed fields),
and creating the new key with all indexes pointing to it.

!!! warning
    Key migration changes the Redis key (identity) of the instance. Any external
    references to the old key will break. Use this intentionally, not accidentally.
