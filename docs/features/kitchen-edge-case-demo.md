# Kitchen Demo: Edge Case Mutation Flows

The Popoto Kitchen TUI demo (`examples/popoto_kitchen/`) now exercises four
mutation-after-save scenarios that correspond to the index-correctness fixes
landed in PRs #159–#163. Each scenario is reachable via an interactive button
in the demo.

## Background

Before this change the demo only created and deleted records. All four classes
of index-corruption bugs fixed by the referenced PRs are triggered exclusively
by mutation scenarios:

| PR | Fix | Scenario |
|----|-----|----------|
| #159 | SortedField ghost entries on partition key change | Move a MenuItem to a different category |
| #161 | save() removes obsolete key from class set on KeyField rename | Rename a Restaurant |
| #162 | Partial save (`update_fields`) obsolete key cleanup | Advance an Order status |
| #163 | Relationship.on_save() index cleanup on value change | Replace an Order's driver |

## Model Changes

### MenuItem — category promoted to KeyField

`MenuItem.category` was previously a plain `Field(type=str)`. It is now a
`KeyField()`, making category part of the Redis key:

```
Before: MenuItem:<item_id>
After:  MenuItem:<category>:<item_id>
```

The `price` SortedField now uses `partition_by="category"`, so price index
sorted sets are scoped per category (`MenuItem:_price:Main`,
`MenuItem:_price:Appetizer`, etc.).

**Seed data**: `seed.py` already passes an explicit `category=` argument for
every `MenuItem` — no data-value changes were needed, only the field declaration
changed.

**Full-key lookup**: All `MenuItem` lookups in `menu.py` now use
`MenuItem.query.get(redis_key=key.value)` (full key string) rather than
splitting the key on `":"` to extract just the last segment.

### Order.advance_status() — partial save

`advance_status()` now calls:

```python
super(Order, self).save(update_fields=["status", "updated_at"])
```

Only the `status` and `updated_at` fields are written. The `total` SortedField
index is untouched, demonstrating that partial save does not produce ghost
index entries (PR #162 fix).

## New Interactive Flows

### Menu Screen — "Move Category" (PR #159)

1. Select a menu item in the table.
2. Click **Move Category**.
3. The item cycles to the next category in `["Appetizer", "Main", "Side", "Dessert", "Drink"]`.
4. `SortedField.on_save()` removes the item from the old price sorted set and
   adds it to the new one.
5. A notification shows `"Moved: {name} from {old} → {new}"`.

Guard: if the computed next category is the same as the current (only one
category exists in the list), the action is a no-op with a warning.

### Orders Screen — "→ Advance Status" (PR #162)

Unchanged UI flow; the underlying implementation now uses partial save
(`update_fields=["status", "updated_at"]`) so that only the status field and
its timestamp are written. The total SortedField index is unaffected.

### Orders Screen — "Assign Driver" (PR #163)

When an order already has a driver assigned, reassigning shows:

```
Replaced driver: Alice → Bob
```

The old notification (`"Assigned driver: Bob"`) is now shown only for first
assignment. A guard prevents reassigning the same driver twice.

`Relationship.on_save()` handles the index cleanup: the order key is removed
from the old driver's relationship index and added to the new driver's index.

### Restaurants Screen — "Rename" (PR #161)

1. Select a restaurant in the table.
2. Click **Rename**.
3. A new name is generated via `restaurant_name(restaurant.cuisine)`.
4. `restaurant.name = new_name; restaurant.save()` — save detects the obsolete
   Redis key, removes the old key from the `Restaurant` class set, and writes
   the new key.
5. `Restaurant.query.all()` returns the restaurant under the new key; the old
   key is gone.

## Running the Demo

```bash
# Clear any stale seeded data first (category → KeyField is a breaking schema change)
# In the Dashboard screen, click "Clear All Data", then "Seed Data"

# Start the TUI
python -m examples.popoto_kitchen
```

Navigate to each screen and use the new buttons to observe the edge case
behaviors described above.
