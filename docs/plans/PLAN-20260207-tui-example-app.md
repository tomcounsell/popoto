# Plan: Popoto TUI Example Application

**Created:** 2026-02-07
**Status:** Draft
**Type:** Feature

---

## Problem

Popoto's documentation contains excellent code examples using a food delivery domain (restaurants, menus, orders, customers, drivers), but users can't experience these examples interactively. There's no runnable demo that showcases Popoto's capabilities at scale - making it harder for potential users to understand the library's power and ease of use.

---

## Appetite

**Time Budget:** 2-3 days of focused work

This is a "small batch" project. We're building a polished demo application, not a production system. The goal is to impress and educate, not to build something users will deploy.

---

## Solution

Build **Popoto Kitchen** - an interactive Terminal User Interface (TUI) application that brings the food delivery documentation examples to life. Users can create restaurants, add menu items, place orders, track drivers, and see real-time updates - all from their terminal.

### Why "Popoto Kitchen"?
- Matches the food delivery theme from docs
- "Kitchen" evokes both food and the "kitchen sink" test pattern
- Playful, memorable name

### Technology Choice: Textual

**Textual** is the modern Python TUI framework built on Rich. It provides:
- Async-first architecture (perfect for Redis operations)
- 60 FPS rendering with CSS-like styling
- Rich widget library (DataTable, Input, Button, Tree, Tabs)
- Reactive attributes that auto-update UI when data changes
- Worker system for background tasks (pub/sub listeners)
- Can serve apps in browser via `textual serve`

### Application Structure

```
examples/
├── popoto_kitchen/
│   ├── __init__.py
│   ├── __main__.py          # Entry point: python -m popoto_kitchen
│   ├── app.py                # Main Textual Application
│   ├── models.py             # Food delivery models (from docs)
│   ├── seed.py               # Generate sample data at scale
│   ├── screens/
│   │   ├── __init__.py
│   │   ├── dashboard.py      # Overview with stats
│   │   ├── restaurants.py    # Restaurant CRUD + geo search
│   │   ├── menu.py           # Menu items with price filtering
│   │   ├── orders.py         # Order management + status updates
│   │   ├── drivers.py        # Driver tracking + location
│   │   └── pubsub.py         # Live message viewer
│   ├── widgets/
│   │   ├── __init__.py
│   │   ├── model_table.py    # Generic Popoto model table
│   │   ├── query_builder.py  # Interactive filter UI
│   │   └── stats_panel.py    # Real-time statistics
│   └── styles/
│       └── kitchen.tcss      # Textual CSS styling
└── README.md                  # How to run the demo
```

### Core Features

#### 1. Dashboard (Home Screen)
- Real-time statistics: total restaurants, orders today, active drivers
- Recent activity feed (last 10 orders)
- Quick actions: "Add Restaurant", "Place Order", "View Drivers"
- Demonstrates: `count()`, `filter()`, `order_by()`, `limit()`

#### 2. Restaurants Screen
- DataTable listing all restaurants with cuisine, rating, location
- **Create**: Form to add new restaurant with geo coordinates
- **Filter**: By cuisine type, minimum rating, location radius
- **Sort**: By rating, name
- Demonstrates: `KeyField`, `SortedField`, `GeoField`, geo queries with distance

#### 3. Menu Screen
- Browse menu items across restaurants
- **Filter by price range**: Slider or input for min/max price
- **Filter by restaurant**: Dropdown selection
- **Relationship display**: Show restaurant name for each item
- Demonstrates: `Relationship`, `SortedField` range queries, `order_by`

#### 4. Orders Screen
- Active orders with status (pending, preparing, delivering, delivered)
- **Create order**: Select customer, restaurant, items
- **Update status**: Buttons to advance order state
- **Assign driver**: Pick from available drivers
- **TTL indicator**: Show time remaining before order expires
- Demonstrates: `Relationship`, `DatetimeField`, `Meta.ttl`, pub/sub status updates

#### 5. Drivers Screen
- Map-style view of driver locations (ASCII or simple grid)
- **Toggle availability**: Active/inactive status
- **Find nearest**: Given a location, find closest available drivers
- **Update location**: Simulate driver movement
- Demonstrates: `GeoField`, geo queries with `location_radius`, `location_with_distances`

#### 6. Pub/Sub Live Screen
- Real-time message feed showing order status updates
- **Publish**: Send test messages to channels
- **Subscribe**: Listen to order updates, driver locations
- **Channel selector**: Switch between different pub/sub channels
- Demonstrates: `Publisher`, `Subscriber`, async message handling

### Scale Demonstration

The seed script generates impressive data volumes:
```python
# seed.py generates:
# - 100 restaurants across 10 cuisines
# - 1,000 menu items (10 per restaurant)
# - 500 customers
# - 50 drivers with random locations
# - 2,000 orders (mix of statuses)
```

This allows demos of:
- Pagination through large datasets
- Range queries on thousands of items
- Geo queries finding nearest from many options
- Performance at realistic scale

### Visual Polish

The Textual CSS will create a cohesive, impressive look:
- Color scheme matching docs site
- Consistent spacing and borders
- Loading indicators for Redis operations
- Toast notifications for actions (saved, deleted, error)
- Keyboard shortcuts for power users

---

## Rabbit Holes (Avoid)

1. **Don't build a real delivery tracking system** - This is a demo, not production software
2. **Don't add user authentication** - Unnecessary complexity for a demo
3. **Don't create a web version** - Terminal-first; `textual serve` is bonus
4. **Don't optimize for mobile terminals** - Standard 80x24 minimum is fine
5. **Don't build custom map visualization** - Simple ASCII grid or table is sufficient for geo

---

## No-Gos

1. **No external API integrations** - All data is local Redis
2. **No persistent configuration** - Fresh state each run (or optional seed)
3. **No multi-user support** - Single user demo application

---

## Risks & Mitigations

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Textual learning curve | Medium | Medium | Start with simple screens, iterate |
| Async complexity | Low | Medium | Use Textual's worker pattern |
| Redis connection issues | Low | High | Clear error messages, connection check on startup |
| Too many features | Medium | High | Focus on 4 core screens first, add pub/sub if time |

---

## Team Orchestration

**Solo implementation with parallel exploration:**

1. **Phase 1: Foundation** (Core)
   - Set up examples/ directory structure
   - Create models.py with all food delivery models
   - Build seed.py for data generation
   - Create basic App shell with navigation

2. **Phase 2: Core Screens** (Parallel where possible)
   - Dashboard with stats
   - Restaurants with CRUD + geo
   - Orders with status flow

3. **Phase 3: Polish**
   - Menu screen with relationship display
   - Drivers screen with location
   - CSS styling refinement
   - README documentation

4. **Phase 4: Advanced** (If time permits)
   - Pub/Sub live screen
   - Query builder widget
   - Keyboard shortcuts

---

## Tasks

### Setup
- [ ] Create `examples/popoto_kitchen/` directory structure
- [ ] Add `textual` to dev dependencies in pyproject.toml
- [ ] Create `models.py` with Restaurant, MenuItem, Customer, Driver, Order
- [ ] Create `seed.py` to generate sample data
- [ ] Create `__main__.py` entry point

### Core Application
- [ ] Build main `app.py` with Textual App and screen routing
- [ ] Create base styles in `kitchen.tcss`
- [ ] Build `model_table.py` widget for displaying Popoto models

### Screens
- [ ] Dashboard screen with statistics and recent activity
- [ ] Restaurants screen with CRUD and geo filtering
- [ ] Menu screen with price range queries
- [ ] Orders screen with status management
- [ ] Drivers screen with location tracking

### Polish
- [ ] Add keyboard shortcuts (q=quit, r=refresh, n=new, etc.)
- [ ] Add toast notifications for actions
- [ ] Create README.md with usage instructions
- [ ] Test with large dataset (2000+ records)

### Optional
- [ ] Pub/Sub live message screen
- [ ] Interactive query builder widget
- [ ] Browser serving via `textual serve`

---

## Success Criteria

1. **Runnable**: `python -m popoto_kitchen` launches the app
2. **Impressive**: Visual polish that showcases terminal capabilities
3. **Educational**: Each screen clearly demonstrates Popoto features
4. **Scalable**: Handles 1000+ records without lag
5. **Documented**: README explains what each screen demonstrates
6. **Self-contained**: No external dependencies beyond Redis

---

## Dependencies

- Python 3.8+
- Redis/Valkey server running
- `textual` (TUI framework)
- `popoto` (this library, installed in dev mode)

---

## References

- [Textual Documentation](https://textual.textualize.io/)
- [Textual Widget Gallery](https://textual.textualize.io/widget_gallery/)
- [Popoto Documentation](https://popoto.readthedocs.io/)
- Food delivery models from `docs/fields.md`, `docs/query.md`, `docs/relationship.md`
