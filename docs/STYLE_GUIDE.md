# Documentation Style Guide

This guide defines conventions for all Popoto documentation. Follow these patterns to keep docs consistent and approachable.

## Canonical Example Models

Use these models across all documentation pages unless a feature specifically requires a different model. Consistent examples help readers build familiarity.

```python
from popoto import Model, KeyField, Field, SortedField, GeoField, AutoKeyField, UniqueKeyField
from popoto import Relationship, DatetimeField

class Restaurant(Model):
    name = KeyField()
    cuisine = Field(type=str)
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

class MenuItem(Model):
    item_id = AutoKeyField()
    name = Field(type=str)
    price = SortedField(type=float)
    restaurant = Relationship(Restaurant)
    available = Field(type=bool, default=True)

class Customer(Model):
    username = KeyField()
    email = UniqueKeyField()
    name = Field(type=str)
    address = GeoField()

class Driver(Model):
    driver_id = AutoKeyField()
    name = Field(type=str)
    phone = UniqueKeyField()
    rating = SortedField(type=float)
    location = GeoField()
    active = Field(type=bool, default=True)

class Order(Model):
    order_id = AutoKeyField()
    customer = Relationship(Customer)
    restaurant = Relationship(Restaurant)
    driver = Relationship(Driver, null=True)
    total = SortedField(type=float)
    status = Field(type=str, default="pending")
    created_at = DatetimeField(auto_now_add=True)
    updated_at = DatetimeField(auto_now=True)

    class Meta:
        order_by = "-created_at"
        ttl = 2592000  # 30 days
```

These five models cover all Popoto features: `KeyField`, `AutoKeyField`, `UniqueKeyField`, `SortedField`, `GeoField`, `DatetimeField`, `Relationship`, `Meta` with `order_by` and `ttl`. Only introduce a specialized model when the canonical ones cannot demonstrate the feature (e.g., `DataFrameField` examples).

## Tone and Voice

- Address the reader directly with "you."
- Explain *why* before showing *how* — give context before code.
- Stay conversational but authoritative. OK to give opinionated guidance ("We recommend..." or "In most cases...").
- Avoid hype language. Describe features factually rather than with marketing phrases.

## Page Structure

Every feature page should follow this order:

1. **Concept introduction** — What is this and why would you use it?
2. **Basic usage** — Simplest working example.
3. **Parameters and options** — Reference for all configuration.
4. **Advanced usage** — Complex or combined scenarios.
5. **Gotchas and notes** — Common mistakes, limitations, edge cases.

### Headings

- **H1** (`#`): Page title. One per page.
- **H2** (`##`): Major sections.
- **H3** (`###`): Subsections.
- Do not skip heading levels (e.g., no H3 directly under H1).

### Prose Between Code Blocks

Never place two code blocks back-to-back without explanatory text between them. Even a single sentence of context helps the reader understand what changed or why the next example matters.

## Code Examples

### Imports

Use explicit imports from `popoto`:

```python
# Good
from popoto import Model, KeyField, Field

# Avoid
import popoto
from popoto import *
```

Include imports only in the first example on each page. Subsequent examples on the same page may omit them.

### Expected Output

Show expected output with the `# =>` prefix:

```python
person = Person.query.get(name="Sally")
print(person.name)
# => "Sally"
```

Do not use `>>>` (Python REPL style) or `>` (quote style) for output.

### Self-Contained Examples

Where possible, make code blocks self-contained so a reader could copy-paste and run them. If an example depends on prior setup, reference it clearly.

### Use Canonical Models

Default to `Restaurant`, `MenuItem`, `Customer`, `Driver`, and `Order` for examples. Only create ad hoc models when the canonical ones cannot demonstrate the feature.

## Admonitions

Use [mkdocs admonition syntax](https://squidfunk.github.io/mkdocs-material/reference/admonitions/):

```markdown
!!! note
    Supplementary information that adds context.

!!! warning
    Gotchas, common mistakes, or things that might surprise you.

!!! tip
    Performance advice, best practices, or shortcuts.
```

Do not use inline markers like "Pro Tip:", "Note:", or "NB:" in paragraph text. Convert these to admonitions.

## Cross-References

Link to related pages at the point of relevance, not only at the bottom of a page:

```markdown
See [Making Queries](query.md) for filter options.
See [SortedField](#sortedfield) for details on numeric indexing.
```

Use the format `[Display Text](page.md)` for page links and `[Display Text](#anchor)` for same-page anchors.

## General Formatting

- Use fenced code blocks with the `python` language tag for all Python examples.
- Use `bash` for shell commands.
- Use backticks for inline code references: `KeyField`, `Model.query.filter()`.
- Keep lines in source Markdown under 120 characters where practical.
