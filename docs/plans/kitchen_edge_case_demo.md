---
status: Planning
type: feature
appetite: Medium
owner: valorengels
created: 2026-03-11
tracking: https://github.com/tomcounsell/popoto/issues/166
last_comment_id:
---

# Kitchen Edge Case Demo

## Problem

The Popoto Kitchen TUI demo (`examples/popoto_kitchen/`) exercises basic CRUD, geo search, sorted-field range queries, and relationship lazy-loading — but never mutates a record after initial save. The four classes of index-corruption bugs fixed by PRs #159–#163 are all triggered by mutation-after-save scenarios: changing a partition key, reassigning a Relationship, using partial `save(update_fields=[...])`, and renaming a KeyField. None of these scenarios are visible in the demo today, which means:

1. A developer running the demo cannot observe the correct post-fix behavior.
2. Regressions in those fixes are invisible — the demo cannot serve as a living integration test.

**Current behavior:** The demo creates and deletes records. No screen mutates a field after an object is first saved. `MenuItem.category` is a plain `Field(str)` — not a KeyField — so the SortedField partition-key scenario (PR #159) cannot be shown at all. The "Assign Driver" flow assigns a driver unconditionally without demonstrating replacement.

**Desired outcome:** Each of the four fixed edge cases is exercised by an explicit interactive flow. A user running the app can observe the before/after state; a developer can verify no ghost index entries exist.

## Prior Art

No prior issues found related to kitchen demo mutation exercises.

The underlying bugs were fixed by:
- **PR #159** — Fix SortedField ghost entries on partition key change
- **PR #161** — Fix `save()` to remove obsolete key from class set
- **PR #162** — Fix partial save (`update_fields`) obsolete key cleanup
- **PR #163** — Fix `Relationship.on_save()` index cleanup on value change

## Data Flow

### Edge Case 1 — SortedField partition key change (PR #159)

1. User clicks "Move Category" on a selected `MenuItem`
2. UI picks a new category value (different from current)
3. `item.category = new_category; item.save()`
4. `SortedField.on_save()` detects old partition key, removes item from old sorted set `MenuItem:_price:<old_category>`, adds to new sorted set `MenuItem:_price:<new_category>`
5. UI re-filters by old and new category to show item has moved

### Edge Case 2 — Relationship replacement (PR #163)

1. User clicks "Assign Driver" on an order that already has a driver assigned
2. UI selects a different driver
3. `order.driver = new_driver; order.save()`
4. `Relationship.on_save()` removes order key from old driver's index set and adds it to new driver's set
5. Orders screen shows updated driver name; both old and new driver's order counts update

### Edge Case 3 — Partial save (PR #162)

1. User clicks "Advance Status" on a selected order
2. `order.status = next_status; order.save(update_fields=["status"])`
3. Only `status` field is written and its `on_save()` hook fires; `total` SortedField index is unaffected
4. Status updates visibly; no side-effects on price/total sorted indexes

### Edge Case 4 — KeyField rename (PR #161)

1. User clicks "Rename" on a selected restaurant
2. UI prompts for a new name
3. `restaurant.name = new_name; restaurant.save()`
4. `save()` detects `obsolete_redis_key`, calls `on_delete()` for old key on all indexes, removes old key from class set, writes new key
5. `Restaurant.query.all()` returns record under new key; old key is gone

## Architectural Impact

- **New dependencies**: None — only modifies files within `examples/popoto_kitchen/`.
- **Interface changes**: `MenuItem.category` changes from `Field(str)` to `KeyField()`. This changes the Redis key structure for `MenuItem` instances from `MenuItem:<item_id>` to `MenuItem:<category>:<item_id>`. All code referencing `MenuItem.query.get(item_id)` must be updated to supply both key parts, or use a scan-based lookup.
- **Coupling**: No change to library coupling. Demo-only schema change.
- **Data ownership**: No change.
- **Reversibility**: Moderate. The `MenuItem.category → KeyField` change is a breaking schema change for any existing seeded data. Running "Clear All Data" + re-seed handles migration. No persistent data is expected in dev.
- **Seed data impact**: `seed.py` currently creates `MenuItem` with `category` as a plain field. Must be updated so category-aware key generation works. Seed items have known categories from `MENU_ITEMS` dict — no change to data values, just to how the key is constructed.

## Appetite

**Size:** Medium

**Team:** Solo dev

**Interactions:**
- PM check-ins: 0
- Review rounds: 1

## Prerequisites

| Requirement | Check Command | Purpose |
|-------------|---------------|---------|
| Redis running | `redis-cli ping` | All Popoto operations require Redis |
| Popoto installed | `python -c "import popoto"` | Library must be importable |

Run all checks: `python scripts/check_prerequisites.py docs/plans/kitchen_edge_case_demo.md`

## Solution

### Key Elements

- **`MenuItem` model**: Change `category` from `Field(str)` to `KeyField()` and add `partition_by="category"` to the `price` SortedField. This makes `category` part of the Redis key and scopes the price sorted set per category.
- **Menu screen — "Move Category" button**: Selects a different category for the chosen item, calls `item.save()`, then refreshes the category filter to show the item left the old category and appears in the new one.
- **Orders screen — "Advance Status" flow**: Change `order.advance_status()` to call `super().save(update_fields=["status"])` instead of `super().save()`, demonstrating partial save.
- **Orders screen — "Assign Driver" flow**: Before assigning, show the current driver name in the notification so the user can see it is a replacement, not a first assignment. The fix (PR #163) handles the index cleanup transparently.
- **Restaurants screen — "Rename" button**: Prompts for a new name string, sets `restaurant.name`, calls `restaurant.save()`, then refreshes so the renamed record appears under its new key and the old entry is gone.
- **Seed data compatibility**: Update `seed.py` `MenuItem` creation to be compatible with `category` as `KeyField`. No data value changes needed — categories are already set.

### Flow

**Menu screen** → Select item → Click "Move Category" → Category picker (dropdown) → `item.category = new; item.save()` → Filter view refreshes → Old category shows 0 for this item, new category shows it

**Orders screen** → Select order → Click "→ Advance Status" → `order.save(update_fields=["status"])` → Table refreshes with new status

**Orders screen** → Select order with existing driver → Click "Assign Driver" → Notification shows "Replaced: Alice → Bob" → Table shows new driver name

**Restaurants screen** → Select restaurant → Click "Rename" → Input dialog → `restaurant.name = new; restaurant.save()` → Table refreshes showing new name, old name gone

### Technical Approach

- `MenuItem` model: `category = KeyField()` + `price = SortedField(type=float, partition_by="category")`. Remove `category` from `Field` position, add to `KeyField` position. Since `item_id = AutoKeyField(strategy="uuid4")` remains, Redis key becomes `MenuItem:<category>:<item_id>`.
- All `MenuItem.query.get(...)` calls in `menu.py` extract the item_id from the row key. With two KeyFields, the redis_key pattern is `MenuItem:<category>:<item_id>`. The row key stored in the DataTable is already the full `redis_key` string — loading via `MenuItem.query.get(redis_key)` (full key) must be verified, OR use `MenuItem.query.filter(item_id=...)` as alternative lookup.
- `Order.advance_status()` in `models.py`: replace `self.save()` with `super(Order, self).save(update_fields=["status"])`. The method already manages `updated_at` timestamp — need to also include `"updated_at"` in `update_fields` to keep timestamp current.
- "Move Category" in `menu.py`: add button to action bar. On click, get current item, pick next category from CATEGORIES list (cycle through), set and save, notify with before/after, refresh.
- "Rename" in `restaurants.py`: add button to action bar. For simplicity (no modal dialog in current Textual setup), append a random adjective to current name to simulate rename without requiring text input. Alternatively, use existing Input widget approach matching `_edit_selected` pattern.
- `seed.py`: no functional changes — `MenuItem` already has `category` in constructor kwargs; the KeyField change just affects how the Redis key is built, not what data is seeded.

## Failure Path Test Strategy

### Exception Handling Coverage

The screens use broad `except Exception as e: self.app.notify(...)` blocks throughout. These are already user-visible. No silent swallowing occurs — every exception produces a notification. New action methods (`_move_category`, `_rename_restaurant`) must follow the same pattern.

### Empty/Invalid Input Handling

- "Move Category": must guard against the case where only one category exists (cycle logic would return the same category — detect and notify "No other categories available").
- "Rename": if using an Input widget approach, guard against empty string input.
- "Assign Driver" replacement: guard against the new driver being the same as the current driver — notify "Driver already assigned".

### Error State Rendering

All new flows follow the existing pattern: success via `self.app.notify(..., severity="information")`, failure via `self.app.notify(..., severity="error")`. No silent failures.

## Rabbit Holes

- **Index Health panel on Dashboard**: The issue mentions an optional "Index Health" panel that scans for orphaned keys. This requires raw Redis SCAN + pattern matching and surfacing the results in Textual. It is valuable but disproportionately complex for a demo. Defer to a separate issue.
- **Textual modal dialogs for rename input**: The current demo has no modal dialog infrastructure. Implementing a proper input modal (using Textual's `ModalScreen`) to get the new restaurant name is a Textual UI rabbit hole. Use a simpler approach: append a generated suffix to the name (same pattern as `_create_restaurant`), or cycle through a short list of name variants.
- **Fixing MenuItem lookup after KeyField change**: With `category` as a KeyField, the existing `MenuItem.query.get(item_id)` calls will break because they only supply one key part. The row key stored in the DataTable is the full `redis_key` — verify that `Model.query.get(full_redis_key)` works as a full-key lookup, or switch to fetching via `Model.db_key.get_instance(redis_key)`. Do not over-engineer a two-field lookup system.

## Risks

### Risk 1: MenuItem key lookup breaks after category → KeyField change
**Impact:** Every action that retrieves a selected MenuItem from the table (toggle available, delete, move category) uses `MenuItem.query.get(key.value.split(":")[-1])`. With `category` as a KeyField, the last segment is still `item_id` but the query must also supply `category`. The existing lookup pattern will fail.
**Mitigation:** Before implementing, verify whether `Model.query.get(redis_key_string)` accepts a full redis key string (not just the last segment). If so, change the split to use the full key. Inspect `query.py` `get()` signature during build. If full-key lookup is not supported, use `Model.query.filter(item_id=uuid_part)` as fallback — but note this returns a list.

### Risk 2: Seed data migration breaks on restart
**Impact:** Existing seeded data (stored under `MenuItem:<item_id>` old key pattern) is unreachable after `category` becomes a KeyField (new key pattern `MenuItem:<category>:<item_id>`). App startup would show 0 menu items.
**Mitigation:** The Dashboard's "Clear All Data" button calls `clear_database()` which uses `Model.delete_all()`. Document in the plan that testers must clear and re-seed after this change. No data migration script needed for a demo-only change.

### Risk 3: `advance_status` timestamp with update_fields
**Impact:** `Order.save()` override sets `self.updated_at = now` before calling `super().save()`. If `super().save(update_fields=["status"])` does not include `"updated_at"`, the timestamp field is not persisted.
**Mitigation:** Change `advance_status` to explicitly call `super(Order, self).save(update_fields=["status", "updated_at"])` after setting both `self.status` and `self.updated_at`.

## Race Conditions

No race conditions identified. All operations are synchronous. The TUI is single-user and single-threaded; the Textual event loop processes one button press at a time.

## No-Gos (Out of Scope)

- Index Health panel / orphaned key scanner on Dashboard
- Textual ModalScreen dialog for rename input
- Any changes to library code (`src/popoto/`) — demo-only changes only
- Customer or Driver mutation scenarios (not targeted by PRs #159–#163)
- Automated `pytest` tests for the TUI screens (Textual testing requires async fixtures; out of scope for this appetite)

## Update System

No update system changes required — this feature modifies only the demo application under `examples/`. No new dependencies or config files.

## Agent Integration

No agent integration required — this is a demo-only change with no MCP server or bridge involvement.

## Documentation

### Inline Documentation
- [ ] Update docstrings on `MenuItem` to reflect `category` is now a `KeyField` and drives `price` partitioning
- [ ] Add brief comments in each new action method explaining which edge case / PR fix it demonstrates

### No external docs site changes needed for a demo-only feature.

## Success Criteria

- [ ] `MenuItem.category` is a `KeyField` and `price` uses `partition_by="category"` in `examples/popoto_kitchen/models.py`
- [ ] Menu screen has a "Move Category" button that reassigns `MenuItem.category`, calls `item.save()`, and refreshes the filter — the item appears in the new category and is absent from the old one
- [ ] `Order.advance_status()` calls `save(update_fields=["status", "updated_at"])` (partial save)
- [ ] Orders screen "Assign Driver" works when the order already has a driver assigned (replacement case), and notifies with old driver name → new driver name
- [ ] Restaurants screen has a "Rename" button that changes `Restaurant.name` (KeyField), calls `restaurant.save()`, and the renamed record appears in `Restaurant.query.all()` with the old key gone
- [ ] `seed.py` successfully seeds `MenuItem` instances with the new `category` KeyField schema
- [ ] `pytest tests/` passes with no regressions (`exit code 0`)

## Team Orchestration

### Team Members

- **Builder (kitchen-demo)**
  - Name: kitchen-builder
  - Role: Implement all four edge case demo flows plus model schema change
  - Agent Type: builder
  - Resume: true

- **Validator (kitchen-demo)**
  - Name: kitchen-validator
  - Role: Verify all success criteria, run pytest, inspect Redis keys to confirm no ghost entries
  - Agent Type: validator
  - Resume: true

## Step by Step Tasks

### 1. Update MenuItem model schema
- **Task ID**: build-model-schema
- **Depends On**: none
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: false
- In `examples/popoto_kitchen/models.py`: change `category = Field(type=str, default="Main")` to `category = KeyField()` with no default (KeyFields must be explicit)
- Change `price = SortedField(type=float)` to `price = SortedField(type=float, partition_by="category")`
- Update `MenuItem.__str__` if needed
- Verify `seed.py` already passes `category=` keyword — confirm it does, no change needed
- Update the `_create_item` method in `menu.py` to pass `category=random.choice(CATEGORIES)` explicitly (already does this — confirm it)

### 2. Fix MenuItem lookup to use full redis_key
- **Task ID**: build-fix-lookup
- **Depends On**: build-model-schema
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: false
- In `menu.py`, all three places that do `MenuItem.query.get(key.value.split(":")[-1])` must be updated
- Inspect `query.py` `get()` method to determine if full redis_key string is accepted; if so, pass `key.value` directly instead of splitting
- Update `_toggle_available`, `_delete_selected`, and the new `_move_category` method to use the correct lookup

### 3. Add "Move Category" to Menu screen
- **Task ID**: build-move-category
- **Depends On**: build-fix-lookup
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: false
- Add `Button("Move Category", id="btn-move-category")` to the action bar in `menu.py`
- Implement `_move_category(self)`: get selected item, determine current category, pick the next one in CATEGORIES list (wrap around), set `item.category = new_category`, call `item.save()`, notify with "Moved: {name} from {old} → {new}", refresh data
- Guard: if item has only one possible category or new == old, notify accordingly

### 4. Update Order.advance_status() for partial save
- **Task ID**: build-partial-save
- **Depends On**: none
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: true
- In `examples/popoto_kitchen/models.py`, update `advance_status()`:
  - After `self.status = self._STATUSES[current_idx + 1]`, also set `self.updated_at = datetime.utcnow().isoformat()`
  - Replace `self.save()` with `super(Order, self).save(update_fields=["status", "updated_at"])`
- This explicitly exercises the partial save code path fixed in PR #162

### 5. Update "Assign Driver" to show replacement
- **Task ID**: build-driver-replace
- **Depends On**: none
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: true
- In `orders.py`, `_assign_driver()`: before assigning, capture `old_driver_name = order.driver.name if order.driver else None`
- Guard: if `old_driver` is the same as new `driver`, notify "Driver already assigned"
- Update notification to show "Assigned: {driver.name}" if no previous driver, or "Replaced driver: {old} → {new}" if replacing

### 6. Add "Rename" to Restaurants screen
- **Task ID**: build-rename-restaurant
- **Depends On**: none
- **Assigned To**: kitchen-builder
- **Agent Type**: builder
- **Parallel**: true
- In `restaurants.py`, add `Button("Rename", id="btn-rename")` to the action bar
- Implement `_rename_restaurant(self)`: get selected restaurant, generate new name by calling `restaurant_name(restaurant.cuisine)` (imports `restaurant_name` from seed), set `restaurant.name = new_name`, call `restaurant.save()`, notify "Renamed to: {new_name}", refresh
- Guard: if generated name equals existing name, try once more

### 7. Validate all changes
- **Task ID**: validate-all
- **Depends On**: build-move-category, build-partial-save, build-driver-replace, build-rename-restaurant
- **Assigned To**: kitchen-validator
- **Agent Type**: validator
- **Parallel**: false
- Run `pytest tests/ -x -q` and confirm exit code 0
- Grep confirm: `grep -n "KeyField" examples/popoto_kitchen/models.py | grep category`
- Grep confirm: `grep -n "partition_by" examples/popoto_kitchen/models.py | grep price`
- Grep confirm: `grep -n "update_fields" examples/popoto_kitchen/models.py | grep advance_status`
- Grep confirm: `grep -n "btn-move-category\|btn-rename" examples/popoto_kitchen/screens/menu.py examples/popoto_kitchen/screens/restaurants.py`
- Verify all success criteria from this plan are met
- Report pass/fail

## Verification

| Check | Command | Expected |
|-------|---------|----------|
| Tests pass | `pytest tests/ -x -q` | exit code 0 |
| category is KeyField | `grep -n "category = KeyField" examples/popoto_kitchen/models.py` | output contains `category = KeyField` |
| price uses partition_by | `grep -n "partition_by" examples/popoto_kitchen/models.py` | output contains `partition_by` |
| partial save in advance_status | `grep -n "update_fields" examples/popoto_kitchen/models.py` | output contains `update_fields` |
| Move Category button exists | `grep -n "btn-move-category" examples/popoto_kitchen/screens/menu.py` | output contains `btn-move-category` |
| Rename button exists | `grep -n "btn-rename" examples/popoto_kitchen/screens/restaurants.py` | output contains `btn-rename` |

---

## Open Questions

1. **MenuItem full-key lookup**: Does `MenuItem.query.get(full_redis_key_string)` accept the full redis key string (e.g., `MenuItem:Main:abc123`) or only the last key segment? This determines whether the fix is a simple `key.value` substitution or requires a more involved lookup change. Should be confirmed by reading `src/popoto/models/query.py` `get()` method signature before building.

2. **KeyField with no default**: `category = KeyField()` has no default. The seed script always passes an explicit `category=` value, and `_create_item` in `menu.py` also passes it explicitly. But is there any code path that creates a `MenuItem` without `category`? If so, it will raise at save time. Should be audited.

3. **"Rename" via generated name vs. user input**: The plan proposes generating a new restaurant name programmatically (to avoid needing a modal dialog). Is a programmatic rename acceptable for demonstrating the KeyField cleanup behavior, or does the supervisor prefer an actual user-typed input (which requires adding a Textual `Input` widget to the action bar or a modal)?
