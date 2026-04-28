#!/usr/bin/env python3
"""Apply all three documentation edits for companion hash key API docs."""

# ============================================================
# 1. api-reference.md
# ============================================================
path_api = "docs/api-reference.md"
with open(path_api, "r") as f:
    text = f.read()

# 1a. ConfidenceField companion key methods
confidence_block = r"""
#### ConfidenceField.get\_data\_hash\_key(instance, field\_name)

Build the Redis key for the confidence companion hash from a model instance. When
`partition_by` is set, appends partition field values to the key.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `ConfidenceField`. |

**Returns:** `str` -- Redis key for the companion hash.

**Key pattern (unpartitioned):** `$ConfidencF:{Model}:{field}:data`
**Key pattern (partitioned):** `$ConfidencF:{Model}:{field}:data:{partition_val}`

```python
field = Memory._options.fields["certainty"]
hash_key = field.get_data_hash_key(memory, "certainty")
# => "$ConfidencF:Memory:certainty:data"
```

#### ConfidenceField.get\_data\_hash\_key\_from\_values(model\_class, field\_name, \*\*partition\_values)

Build the companion hash key from explicit partition values, without needing a model
instance. Useful in query paths and bulk operations scoped to a partition.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `ConfidenceField`. |
| `**partition_values` | | Mapping of partition field names to values. |

**Returns:** `str` -- Redis key for the companion hash.

**Raises:** `QueryException` if a required partition field value is missing.

```python
field = Memory._options.fields["certainty"]
key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
# => "$ConfidencF:Memory:certainty:data:atlas"
```

#### ConfidenceField.get\_old\_data\_hash\_key(instance, field\_name)

Build the companion hash key using saved (pre-mutation) partition field values. Used
during `on_save`/`on_delete` to locate the old partition hash when a partition key has
changed. Also useful for custom partition migration logic.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `ConfidenceField`. |

**Returns:** `str` or `None` -- The old hash key, or `None` if no saved values exist.

"""

old1 = '**Raises:** `ModelException` if the field has no `partition_by` configured.\n\n#### ObservationProtocol entrainment'
new1 = '**Raises:** `ModelException` if the field has no `partition_by` configured.\n' + confidence_block + '#### ObservationProtocol entrainment'
assert old1 in text, f"Could not find ConfidenceField anchor in {path_api}"
text = text.replace(old1, new1, 1)

# 1b. CyclicDecayField companion key methods
cyclic_block = r"""
#### CyclicDecayField.get\_cycles\_hash\_key(instance, field\_name)

Build the Redis key for the cycles companion hash from a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |

**Returns:** `str` -- Redis key for the cycles companion hash.

**Key pattern:** `$CyclicDecayF:{Model}:{field}:{partitions}:cycles`

#### CyclicDecayField.get\_pressure\_hash\_key(instance, field\_name)

Build the Redis key for the pressure companion hash from a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `instance` | `Model` | A saved model instance. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |

**Returns:** `str` -- Redis key for the pressure companion hash.

**Key pattern:** `$CyclicDecayF:{Model}:{field}:{partitions}:pressure`

#### CyclicDecayField.get\_cycles\_hash\_key\_from\_parts(model\_class, field\_name, \*partition\_values)

Class method. Build the cycles hash key from a model class and explicit partition values,
without needing a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |
| `*partition_values` | | Positional partition field values. |

**Returns:** `str` -- Redis key for the cycles companion hash.

#### CyclicDecayField.get\_pressure\_hash\_key\_from\_parts(model\_class, field\_name, \*partition\_values)

Class method. Build the pressure hash key from a model class and explicit partition values,
without needing a model instance.

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |
| `field_name` | `str` | Name of the `CyclicDecayField`. |
| `*partition_values` | | Positional partition field values. |

**Returns:** `str` -- Redis key for the pressure companion hash.

"""

old2 = '| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values (inherited). |\n\n### AccessTrackerMixin'
new2 = '| `partition_by` | `str` or `tuple` | `()` | Partition the sorted set by key field values (inherited). |\n' + cyclic_block + '### AccessTrackerMixin'
assert old2 in text, f"Could not find CyclicDecayField anchor in {path_api}"
text = text.replace(old2, new2, 1)

# 1c. CoOccurrenceField section
cooc_block = r"""### CoOccurrenceField

```python
from popoto.fields.co_occurrence_field import CoOccurrenceField
```

A field that maintains a co-occurrence graph as Redis sorted sets. Each primary key gets
an edge sorted set tracking weighted links to other PKs. See
[CoOccurrenceField feature docs](features/co-occurrence-field.md) for usage examples.

#### CoOccurrenceField.get\_edge\_key(model\_class, pk)

Build the Redis key for a PK's edge sorted set. Use this for direct Redis access to
a specific node's edges (e.g., bulk edge inspection, custom graph queries, monitoring).

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class (or instance). |
| `pk` | `str` | The primary key string. |

**Returns:** `str` -- Redis key for this PK's edge sorted set.

**Key pattern:** `$CoOcF:{ClassName}:{field_name}:{pk}`

```python
field = Memory._options.fields["associations"]
edge_key = field.get_edge_key(Memory, "fact1")
# => "$CoOcF:Memory:associations:fact1"
```

#### CoOccurrenceField.get\_edge\_key\_prefix(model\_class)

Build the Redis key prefix for scanning or iterating over all edge sorted sets for a
field (e.g., graph analytics, bulk cleanup).

| Parameter | Type | Description |
|-----------|------|-------------|
| `model_class` | `Model` | The Model class. |

**Returns:** `str` -- Key prefix ending with colon.

**Key pattern:** `$CoOcF:{ClassName}:{field_name}:`

"""

old3 = '### InteractionWeight\n\n```python\nfrom popoto import InteractionWeight\n# or: from popoto.fields.constants import InteractionWeight'
new3 = cooc_block + '### InteractionWeight\n\n```python\nfrom popoto import InteractionWeight\n# or: from popoto.fields.constants import InteractionWeight'
assert old3 in text, f"Could not find InteractionWeight anchor in {path_api}"
text = text.replace(old3, new3, 1)

with open(path_api, "w") as f:
    f.write(text)
print("api-reference.md updated")

# ============================================================
# 2. confidence-field.md
# ============================================================
path_cf = "docs/features/confidence-field.md"
with open(path_cf, "r") as f:
    text2 = f.read()

example_block = '''### Inspecting Companion Hash Keys

Each `ConfidenceField` stores its Bayesian metadata in a companion Redis hash alongside
the main model hash. The public companion key methods let you build these Redis keys
for debugging, monitoring, or direct Redis inspection without reverse-engineering
suffix conventions.

```python
import redis
from popoto import Model, UniqueKeyField, StringField
from popoto.fields.confidence_field import ConfidenceField

class Memory(Model):
    key = UniqueKeyField()
    content = StringField()
    certainty = ConfidenceField(initial_confidence=0.5)

# Create and update a memory
memory = Memory.create(key="fact1", content="The sky is blue")
ConfidenceField.update_confidence(memory, "certainty", signal=0.9)

# Get the companion hash key for direct Redis inspection
field = Memory._options.fields["certainty"]
hash_key = field.get_data_hash_key(memory, "certainty")
print(hash_key)
# => "$ConfidencF:Memory:certainty:data"

# Inspect the raw companion hash in Redis
r = redis.from_url("redis://localhost:6379")
raw_data = r.hgetall(hash_key)
print(raw_data)
# Shows all members and their msgpack-encoded confidence metadata
```

When you do not have an instance loaded, use `get_data_hash_key_from_values` to build
the key from explicit values:

```python
# Build the key without loading a model instance
key = field.get_data_hash_key_from_values(Memory, "certainty")
# => "$ConfidencF:Memory:certainty:data"

# For partitioned fields, pass the partition values as keyword arguments
# key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
```

'''

old4 = '## Partitioned Reads'
new4 = example_block + '## Partitioned Reads'
assert old4 in text2, f"Could not find Partitioned Reads anchor in {path_cf}"
text2 = text2.replace(old4, new4, 1)

with open(path_cf, "w") as f:
    f.write(text2)
print("confidence-field.md updated")

# ============================================================
# 3. multi-tenancy.md
# ============================================================
path_mt = "docs/multi-tenancy.md"
with open(path_mt, "r") as f:
    text3 = f.read()

tenant_block = '''### Verifying tenant isolation

Use the companion key methods to confirm that each tenant's companion hash is stored
under a separate Redis key. This is useful for auditing, monitoring dashboards, or
integration tests that validate isolation.

```python
field = Memory._options.fields["certainty"]

# Build keys for each tenant without loading instances
atlas_key = field.get_data_hash_key_from_values(Memory, "certainty", project="atlas")
hermes_key = field.get_data_hash_key_from_values(Memory, "certainty", project="hermes")

print(atlas_key)   # => "$ConfidencF:Memory:certainty:data:atlas"
print(hermes_key)  # => "$ConfidencF:Memory:certainty:data:hermes"
assert atlas_key != hermes_key  # Companion hashes are fully isolated
```

You can also verify isolation for instance-based keys:

```python
memory_a = Memory.query.get(project="atlas", key="fact1")
memory_b = Memory.query.get(project="hermes", key="fact2")

key_a = field.get_data_hash_key(memory_a, "certainty")
key_b = field.get_data_hash_key(memory_b, "certainty")
assert key_a != key_b  # Each tenant's data lives in a separate hash
```

'''

old5 = '### Filtered reads without partitioning'
new5 = tenant_block + '### Filtered reads without partitioning'
assert old5 in text3, f"Could not find Filtered reads anchor in {path_mt}"
text3 = text3.replace(old5, new5, 1)

with open(path_mt, "w") as f:
    f.write(text3)
print("multi-tenancy.md updated")

print("\nAll three files updated successfully.")
