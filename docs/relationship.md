# Relationships

__Important caveat:__
_Redis is by nature a key value data store. It is not a relational database like SQL. 
Therefore, Popoto may not be ideal for highly relational data because the query interface is very limited.
Review the features below before using `Relationship` fields._ 

## Relational Model 

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

## Relationship Query

```python

sally = Person.create(name="Sally")
friends = Group.create(name="My Line Friends")
Membership.create(member=sally, group=friends)

memberships = Membership.query.filter(member__name="Sally", group=friends_group)
print(memberships[0].member)
> Sally

```
