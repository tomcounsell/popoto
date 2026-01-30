# Relationships

The `Relationship` field creates references between model instances, similar to foreign keys in SQL databases. Unlike SQL, Redis does not support JOIN operations, so relationships in Popoto work differently — they store references (Redis keys) that are lazy-loaded when accessed.

This page explains how to create, query, and traverse relationships. If your application requires complex relational queries with joins and aggregations, consider whether Redis is the right fit for your use case.

## Basic Usage

A `Relationship` field stores a reference to another model instance. The simplest relationship links one model to another.

```python
from popoto import Model, KeyField, Field
from popoto.fields.relationship import Relationship

class Person(Model):
    name = KeyField()
    email = Field()

class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person)
```

When you create a `Note`, you pass a `Person` instance to the `author` field.

```python
sally = Person.create(name="Sally", email="sally@example.com")
note = Note.create(
    title="Meeting Notes",
    content="Discussed Q1 roadmap",
    author=sally
)

print(note.author.name)
# => "Sally"
```

The `author` field now holds a reference to Sally. When you access `note.author`, Popoto loads the full `Person` instance from Redis.

## How Relationships Differ from SQL

In SQL databases, relationships use foreign keys and JOINs to combine data from multiple tables in a single query. Redis has no built-in JOIN operation, so Popoto uses a different approach:

1. **Storage**: The relationship field stores the related model's Redis key (e.g., `"Person:Sally"`) as a string.
2. **Loading**: When you access the field, Popoto fetches the related instance from Redis on demand.
3. **Querying**: You can filter by related fields using double-underscore notation, but you cannot join models in a single query.

This means querying relationships requires traversing references explicitly, often using list comprehensions or loops. If your application needs complex multi-table joins, SQL may be a better fit than Redis.

## Creating Relationships

Pass a model instance when creating or updating a relationship field.

```python
sally = Person.create(name="Sally", email="sally@example.com")
note = Note.create(
    title="Design Doc",
    content="System architecture proposal",
    author=sally
)
```

You can update relationships the same way.

```python
tom = Person.create(name="Tom", email="tom@example.com")
note.author = tom
note.save()

print(note.author.name)
# => "Tom"
```

## Querying by Relationship

You can filter models by their relationship fields in two ways: exact match or nested field access.

### Exact Match

Pass a model instance to filter for exact matches.

```python
sally = Person.query.get(name="Sally")
sally_notes = Note.query.filter(author=sally)

for note in sally_notes:
    print(note.title)
# => "Meeting Notes"
# => "Design Doc"
```

This returns all `Note` instances where `author` points to the `sally` instance.

### Nested Field Access

Use double-underscore notation to query by fields on the related model.

```python
# Find notes where the author's name is "Sally"
notes = Note.query.filter(author__name="Sally")

# Combine with other filters
notes = Note.query.filter(
    author__name="Sally",
    title="Meeting Notes"
)
```

This queries the `Person` model's `name` field without loading the full `Person` instance first.

## Traversing Relationships

Because Redis does not support JOINs, you traverse relationships using list comprehensions or loops.

```python
# Get all authors who wrote notes with "roadmap" in the content
authors = [
    note.author
    for note in Note.query.all()
    if "roadmap" in note.content
]

for author in authors:
    print(author.name)
# => "Sally"
```

This loads each `Note`, checks its content, and collects the related `Person` instances.

!!! warning "N+1 Query Problem"
    Accessing relationships in loops triggers one Redis call per access. This can cause performance issues with large datasets.

    ```python
    # Anti-pattern: N Redis calls (1 for query + N for each author)
    notes = Note.query.all()
    for note in notes:
        print(note.author.name)  # Each access = Redis GET
    ```

    If you need to load many related instances, consider whether your data model could denormalize some fields to reduce round trips.

## How Relationships Work Internally

Understanding the internal mechanics helps you write efficient relationship queries and avoid common pitfalls.

### Storage Strategy

Relationships are **not** stored as full serialized model objects. Instead, Popoto stores the related model's Redis key as a string.

```python
sally = Person.create(name="Sally", email="sally@example.com")
note = Note.create(title="Meeting Notes", content="...", author=sally)

# Internally, Redis stores:
# Note:Meeting Notes → { "author": "Person:Sally", ... }
```

When you access `note.author`, Popoto checks if the value is a string. If so, it performs a Redis GET to load the full `Person` instance and caches it in memory.

### Lazy Loading

Related models are loaded only when accessed, not when the parent model is loaded.

```python
note = Note.query.get(title="Meeting Notes")

# At this point, `author` is still a string internally ("Person:Sally")
# Accessing it triggers a Redis GET
person = note.author  # Lazy-loaded here

# Subsequent access uses the cached instance
name = note.author.name  # No additional Redis call
```

This lazy-loading prevents unnecessary Redis calls when you do not need the related data.

### Circular Reference Prevention

Popoto handles circular references safely by tracking which models are currently being loaded.

```python
class Node(Model):
    name = KeyField()
    parent = Relationship(model='Node', null=True)

root = Node.create(name="Root", parent=None)
child = Node.create(name="Child", parent=root)

# This works without infinite recursion
print(child.parent.name)
# => "Root"
```

The relationship system uses a global set (`RELATED_MODEL_LOAD_SEQUENCE`) to detect circular references and break the loop.

### Relationship Index Maintenance

Popoto maintains Redis sets to enable efficient relationship queries. For each relationship field, it creates an index set.

```
# Example: Note with author=Person:Sally
$RelationshipF:Note:author:Person:Sally → {Note:Meeting Notes, Note:Design Doc, ...}
```

These indexes are automatically created on `save()`, updated when the relationship changes, and cleaned up on `delete()`.

## Null Relationships

Relationships can be optional by setting `null=True`.

```python
class Note(Model):
    title = KeyField()
    content = Field(type=str)
    author = Relationship(Person, null=True)

# Valid - author can be None
note = Note.create(title="Draft", content="...", author=None)

print(note.author)
# => None
```

This is useful when a relationship may not always exist (e.g., a note without an assigned author).

## Self-Referential Relationships

A model can reference itself, such as a tree structure or linked list.

```python
class Node(Model):
    name = KeyField()
    parent = Relationship(model='Node', null=True)

root = Node.create(name="Root", parent=None)
child = Node.create(name="Child", parent=root)
grandchild = Node.create(name="Grandchild", parent=child)

print(grandchild.parent.parent.name)
# => "Root"
```

Use a string for the model name (`'Node'`) when the model is not yet defined. Set `null=True` for the root node.

## Best Practices

Start your queries from the model that holds the relationship field. You cannot query in reverse unless you create a bidirectional relationship.

```python
# Good: Query from Note (which has the author field)
notes = Note.query.filter(author=sally)

# Not possible: Person has no back-reference to Note
# notes = Person.query.filter(notes=...)
```

If you need reverse lookups frequently, add a relationship field in both directions.

```python
class Person(Model):
    name = KeyField()
    notes = Field(type=list, default=list)  # Store Note keys

class Note(Model):
    title = KeyField()
    author = Relationship(Person)
```

This denormalizes data but makes reverse queries efficient.

!!! tip "Denormalize for Complex Queries"
    Redis excels at simple key-value lookups, not complex relational queries. If you need frequent multi-step traversals or aggregations, consider storing redundant data to reduce round trips.

    For example, if you often need to count a person's notes, store a `note_count` field on `Person` and increment it when creating or deleting notes.

!!! note "No Cascade Delete"
    Deleting a model does not cascade to related models. If you delete a `Person`, any `Note` instances still reference the deleted person's Redis key, which will raise an error when accessed.

    You must manually delete or update related instances before deleting the parent.
