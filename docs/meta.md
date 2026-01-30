# Model Meta Options

Every Popoto model can include a `Meta` inner class to configure model-level behavior like default ordering, automatic expiration, and composite indexes. This follows the same pattern you might know from Django or Peewee ORMs, making configuration explicit and centralized.

The `Meta` class is processed when your model is defined, and its options become available via `ModelClass._meta`. This separation keeps configuration distinct from your model's fields and methods.

## When to Use Meta Options

You should define a `Meta` class when you want to:

- **Set default ordering** for query results without specifying `order_by` every time
- **Automatically expire data** using Redis TTL (great for sessions, cache, or temporary data)
- **Enforce uniqueness** across multiple fields (composite unique constraints)

Without a `Meta` class, your models work fine—you just configure behavior at query time instead.

## Basic Example

Here's a simple model with default ordering configured:

```python
from popoto import Model, KeyField, SortedField

class Person(Model):
    name = KeyField()
    email = Field()
    age = SortedField(type=int)

    class Meta:
        order_by = "age"  # Always return people sorted by age
```

Now when you query for people, they come back ordered by age without you needing to specify it:

```python
Person.create(name="Alice", email="alice@example.com", age=30)
Person.create(name="Bob", email="bob@example.com", age=25)

people = Person.query.all()
print(people[0].name)
# => "Bob"
```

## Available Options

### order_by

The `order_by` option sets a default sort order for all queries. This is useful when you almost always want results in the same order—for example, showing newest notes first, or listing people alphabetically.

Without `order_by` in Meta, you'd need to specify `order_by` on every query. With it in Meta, the default is automatic, and you can still override it when needed.

```python
from popoto import Model, KeyField, Field, SortedField
from popoto.fields.relationship import Relationship
from popoto.fields.datetime import DatetimeField

class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person)
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"  # Newest notes first (descending)
```

The minus sign prefix (`-`) means descending order. Without it, results are ascending.

Now queries automatically return newest notes first:

```python
notes = Note.query.all()
# Returns notes ordered by created_at descending

recent = Note.query.filter(author=person)
# Still respects default order_by
```

You can override the default at query time if you need a different order for a specific query:

```python
notes = Note.query.all(order_by="title")
# Overrides Meta, sorts by title ascending instead
```

!!! note
    The field specified in `order_by` must exist on the model, or you'll get a `ModelException` when the class is defined.

### ttl

The `ttl` (time-to-live) option tells Redis to automatically delete model instances after a specified number of seconds. This is perfect for temporary data like user sessions, rate-limiting counters, or cached API responses.

When you save a model with a TTL, Popoto sets Redis's `EXPIRE` command on the key. After that time elapses, Redis automatically removes it—no cleanup code needed.

```python
from popoto import Model, KeyField, Field

class Person(Model):
    name = KeyField()
    email = Field()
    age = SortedField(type=int)

    class Meta:
        ttl = 3600  # Expire after 1 hour (3600 seconds)
```

Now every person instance expires one hour after being saved:

```python
person = Person.create(name="Charlie", email="charlie@example.com", age=28)
# After 3600 seconds, Redis automatically deletes this person
```

!!! tip
    TTL is a Redis-native feature that SQL databases don't have. This is one of the advantages of using Popoto with Redis for certain use cases.

#### Instance-Level Override

You can override the Meta TTL for specific instances using the `_ttl` attribute:

```python
person = Person(name="Diana", email="diana@example.com", age=35)
person._ttl = 7200  # This person expires after 2 hours instead
person.save()
```

To make a specific instance permanent (no expiration), set `_ttl` to `None`:

```python
permanent = Person(name="Eve", email="eve@example.com", age=40)
permanent._ttl = None  # Never expires
permanent.save()
```

#### Absolute Expiration Time

Instead of a relative TTL, you can set an absolute expiration timestamp with `_expire_at`:

```python
from datetime import datetime, timedelta

person = Person(name="Frank", email="frank@example.com", age=32)
person._expire_at = datetime.now() + timedelta(days=7)
person.save()
# Expires exactly 7 days from now
```

!!! warning
    TTL is refreshed every time you call `save()`, not just on creation. If you save an instance again, the expiration clock resets.

### indexes

The `indexes` option creates composite indexes—multi-field indexes that can enforce uniqueness across combinations of fields. This is useful when a single field isn't enough to guarantee uniqueness, but a combination should be unique.

For example, you might want to ensure that each person can only create one note with a given title, but different people can use the same title.

Indexes are specified as a tuple of tuples. Each inner tuple contains:

1. A tuple of field names to index together
2. A boolean indicating whether the combination must be unique

```python
from popoto import Model, KeyField, Field
from popoto.fields.relationship import Relationship
from popoto.fields.datetime import DatetimeField

class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person)
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        indexes = (
            (('author', 'title'), True),  # Each author can only have one note with a given title
        )
```

With this index in place, attempting to create a duplicate will raise an exception:

```python
alice = Person.create(name="Alice", email="alice@example.com", age=30)

note1 = Note.create(title="Ideas", content="First idea", author=alice)
# => Note saved successfully

note2 = Note.create(title="Ideas", content="Second idea", author=alice)
# => ModelException: Unique index violation
```

However, a different person can create a note with the same title:

```python
bob = Person.create(name="Bob", email="bob@example.com", age=25)

note3 = Note.create(title="Ideas", content="Bob's idea", author=bob)
# => Note saved successfully
```

#### Non-Unique Indexes

You can also create non-unique indexes for query performance by setting the second value to `False`:

```python
class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person)
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        indexes = (
            (('author', 'created_at'), False),  # Index for querying, but not unique
        )
```

#### NULL Handling

Following SQL standard behavior, `NULL` values don't participate in uniqueness checks. Multiple instances can have `NULL` in an indexed field:

```python
note1 = Note.create(title="Orphan1", content="No author", author=None)
note2 = Note.create(title="Orphan2", content="Also no author", author=None)
# => Both save successfully, even with the same author value (None)
```

#### Update and Delete Handling

Updates that would violate a unique index are rejected:

```python
alice = Person.create(name="Alice", email="alice@example.com", age=30)

note1 = Note.create(title="Ideas", content="First", author=alice)
note2 = Note.create(title="Plans", content="Second", author=alice)

note2.title = "Ideas"  # Try to use the same title as note1
note2.save()
# => ModelException: Unique index violation
```

When you delete an instance, its index entries are automatically cleaned up:

```python
note1.delete()

# Now another note can use the same title for this author
note3 = Note.create(title="Ideas", content="Third", author=alice)
# => Saves successfully
```

!!! note
    All field names in an index must exist on the model, or you'll get a `ModelException` when the class is defined.

## Complete Example

Here's a model that combines all three Meta options:

```python
from popoto import Model, KeyField, Field, SortedField
from popoto.fields.relationship import Relationship
from popoto.fields.datetime import DatetimeField

class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person)
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)
    priority = SortedField(type=int)

    class Meta:
        order_by = "-priority"           # High priority notes first
        ttl = 2592000                    # Expire after 30 days (30 * 24 * 60 * 60)
        indexes = (
            (('author', 'title'), True),  # Each author's note titles must be unique
        )
```

This model will:

- Return notes ordered by priority (highest first) by default
- Automatically expire notes after 30 days
- Prevent an author from creating two notes with the same title

## Accessing Meta Options

After your model is defined, access Meta options via `_meta`, not `Meta`:

```python
class Person(Model):
    name = KeyField()
    email = Field()
    age = SortedField(type=int)

    class Meta:
        order_by = "age"
        ttl = 3600

# Correct way to access
print(Person._meta.order_by)
# => "age"

print(Person._meta.ttl)
# => 3600
```

!!! warning
    Don't access `Person.Meta` directly—it's processed during class creation and may not be available as you expect.

## Meta Validation

All Meta options are validated when your model class is defined, not when you create instances. This means you'll get immediate feedback about configuration errors:

```python
# This raises ModelException immediately
class BadPerson(Model):
    name = KeyField()

    class Meta:
        order_by = "nonexistent_field"  # ModelException: field doesn't exist
        ttl = -1                         # ModelException: must be positive integer
```

## Reference

Popoto's Meta class is inspired by:

- [Django Model Meta options](https://docs.djangoproject.com/en/stable/ref/models/options/)
- [Peewee Model options](https://docs.peewee-orm.com/en/latest/peewee/models.html#model-options-and-table-metadata)
