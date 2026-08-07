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

## v1.4.4 Feature Demos (PR #346)

PR #346 added a new `ReviewScore` model and an operations script that
demonstrates four v1.4.4 features outside of the TUI. These are invoked
via `python -m popoto_kitchen --ops` and print results to stdout.

### ReviewScore Model

A new model using `ConfidenceField` with `partition_by="restaurant"` to
track review confidence per restaurant. See
[ConfidenceField docs](confidence-field.md) for the capped-evidence update formula.

```python
class ReviewScore(Model):
    restaurant = KeyField()
    reviewer = KeyField()
    score = ConfidenceField(initial_confidence=0.5, partition_by="restaurant")
```

The seed script creates 100 review scores with varied confidence signals
(2-6 signals per review, mix of positive and negative) to build realistic
evidence histories.

### Operations Script (`operations.py`)

| Demo | Feature | Description |
|------|---------|-------------|
| `demo_get_many()` | `query.get_many()` | Bulk-loads Order instances by key in a single pipeline call. Shows both default mode (with `None` placeholders) and `skip_none=True` mode. |
| `demo_check_and_clean_indexes()` | `check_indexes()` / `clean_indexes()` | Runs a read-only health check on all models, then surgically removes any orphaned index entries found. |
| `demo_companion_hash_keys()` | `get_data_hash_key()` | Inspects the companion Redis hash keys for ReviewScore's ConfidenceField, showing how `partition_by` creates per-restaurant hashes. Also demonstrates `get_data_hash_key_from_values()` for building keys without a loaded instance. |

### Entry Point Changes

New CLI flags added to `__main__.py`:

- `--ops` -- Run v1.4.4 operations demos and exit
- `--seed-only --clear` -- Seed fresh data without launching the TUI

## Running the Demo

```bash
# Start the TUI (interactive edge case demos)
python -m examples.popoto_kitchen

# Clear any stale seeded data first (category -> KeyField is a breaking schema change)
# In the Dashboard screen, click "Clear All Data", then "Seed Data"

# Run the v1.4.4 operations demos (non-interactive, stdout output)
python -m popoto_kitchen --seed-only --clear
python -m popoto_kitchen --ops
```

Navigate to each screen and use the new buttons to observe the edge case
behaviors described above. Use `--ops` for the v1.4.4 feature demos.
