# Relationships

__Important caveat:__
_Redis is by nature a key value data store. It is not a relational database like SQL.
Therefore, Popoto may not be ideal for highly relational data because the query interface is very limited.
Review the features below before using `Relationship` fields._

## Relational Model

The `Relationship` field creates a reference to another model instance. Relationships are stored as Redis keys (strings) and lazy-loaded to full model instances when accessed.

```python
from popoto import Model, KeyField, Relationship

class Person(Model):
    name = KeyField()

class Group(Model):
    name = KeyField()

class Membership(Model):
    member = Relationship(model=Person)
    group = Relationship(model=Group)
```

## Creating Relationships

```python
sally = Person.create(name="Sally")
friends_group = Group.create(name="My Line Friends")

# Create membership linking Person to Group
membership = Membership.create(member=sally, group=friends_group)

# Access related instances
print(membership.member.name)  # "Sally"
print(membership.group.name)   # "My Line Friends"
```

## Querying Relationships

### Exact Match

Query by exact model instance:

```python
# Find all memberships for Sally
sally_memberships = Membership.query.filter(member=sally)

# Find all members of friends_group
friends_memberships = Membership.query.filter(group=friends_group)
```

### Nested Field Access

Use double-underscore notation to query by related model's fields:

```python
# Find memberships where member's name is "Sally"
memberships = Membership.query.filter(member__name="Sally")

# Combine with other filters
memberships = Membership.query.filter(
    member__name="Sally",
    group=friends_group
)
```

### Traversing Relationships

Use list comprehensions to get related objects:

```python
# Get all people in a group
members = [m.member for m in Membership.query.filter(group=friends_group)]

# Get all groups a person belongs to
groups = [m.group for m in Membership.query.filter(member=sally)]
```

## How Relationships Work Internally

### Storage Strategy

Relationships are **not** stored as full serialized model objects. Instead:

1. When saving: The related model's `redis_key` string is stored (e.g., `"Person:Sally"`)
2. When loading: The string is lazy-loaded to a full model instance on first access
3. This prevents circular reference issues and keeps data normalized

```python
# Example of what's stored in Redis
# Membership:1 HSET:
# {
#   "member": "Person:Sally",      # Stored as string, not full object
#   "group": "Group:My Line Friends"
# }
```

### Circular Reference Prevention

Popoto handles circular references safely:

```python
class Node(Model):
    name = KeyField()
    parent = Relationship(model='Node', null=True)  # Self-referential

root = Node.create(name="Root", parent=None)
child = Node.create(name="Child", parent=root)

# This works without infinite recursion:
print(child.parent.name)  # "Root"
```

The circular reference detection uses a global `RELATED_MODEL_LOAD_SEQUENCE` set that tracks which models are currently being loaded to prevent infinite loops.

### Lazy Loading

Related models are loaded on first access:

```python
membership = Membership.query.get(...)

# At this point, member is still a string internally
# First access triggers the load from Redis
person = membership.member  # Lazy-loaded here

# Subsequent access uses cached instance
name = membership.member.name  # No additional Redis call
```

## Relationship Index Maintenance

Popoto maintains bidirectional Redis sets for efficient relationship queries:

```
# For each relationship, Redis stores:
$RelationshipF:Membership:member:Person:Sally → {Membership:1, Membership:2, ...}
$RelationshipF:Membership:group:Group:My Line Friends → {Membership:1, Membership:3, ...}
```

These indexes are automatically:
- Created on `save()`
- Updated when relationship field values change
- Cleaned up on `delete()`

## Null Relationships

Relationships can be optional:

```python
class Membership(Model):
    member = Relationship(model=Person)
    group = Relationship(model=Group, null=True)  # Optional

# Valid - group can be None
membership = Membership.create(member=sally, group=None)
```

## Best Practices

1. **Start with the model you want returned**: Query from the model that holds the relationship field

```python
# ✅ Good - returns Memberships
memberships = Membership.query.filter(member=sally)

# ❌ Not possible - Person doesn't have a back-reference
# memberships = Person.query.filter(memberships=...)
```

2. **Use list comprehensions for traversal**: Redis doesn't support JOIN operations

```python
# ✅ Good - explicit traversal
members = [m.member for m in Membership.query.filter(group=friends_group)]

# ❌ Not possible - no JOIN syntax
# members = Person.query.join(Membership).filter(group=friends_group)
```

3. **Consider denormalization for complex queries**: If you need frequent reverse lookups, add relationship fields in both directions or store redundant data

```python
class Person(Model):
    name = KeyField()
    groups = Field(type=list, default=list)  # Denormalized group IDs

class Membership(Model):
    member = Relationship(model=Person)
    group = Relationship(model=Group)
```

4. **Be mindful of N+1 queries**: Accessing relationships in loops triggers Redis calls

```python
# ⚠️ N+1 problem - N Redis calls
for membership in Membership.query.all():
    print(membership.member.name)  # Each access = Redis call

# Better: Use batch loading patterns if needed
```
