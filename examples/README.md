# Popoto Kitchen

An interactive Terminal User Interface (TUI) demo application showcasing the Popoto Redis ORM.

![Popoto Kitchen](https://img.shields.io/badge/TUI-Textual-blue)
![Python](https://img.shields.io/badge/python-3.8+-green)

## Overview

Popoto Kitchen is a food delivery system simulation that demonstrates all major features of the Popoto Redis ORM through an interactive terminal interface. Create restaurants, manage menus, place orders, and track drivers - all while seeing Popoto's powerful querying capabilities in action.

## Quick Start

### Prerequisites

- Python 3.8+
- Redis server running on localhost:6379 (or set `REDIS_URL`)

### Running

```bash
# From the examples/ directory
cd examples

# Start the app
uv run popoto-kitchen

# Seed sample data first
uv run popoto-kitchen --seed

# Clear and re-seed
uv run popoto-kitchen --clear --seed

# Only seed, don't start the app
uv run popoto-kitchen --seed-only
```

## Features

### Dashboard
- Real-time statistics (restaurants, orders, drivers)
- Recent orders feed
- Quick data seeding and clearing

**Popoto features demonstrated:**
- `Model.query.count()` - Count records
- `Model.query.all()` - Retrieve all records
- `Model.query.filter(status="delivered")` - Filter by field value

### Restaurants
- Create, edit, delete restaurants
- Filter by cuisine, rating
- **Geo search** - Find restaurants within a radius

**Popoto features demonstrated:**
- `KeyField` - Restaurant name as primary key
- `SortedField` - Rating for range queries
- `GeoField` - Location-based queries with distance
- `location_radius`, `location_with_distances` - Geo query parameters

### Menu Items
- Browse items across all restaurants
- **Price range filtering** using SortedField
- View restaurant relationships

**Popoto features demonstrated:**
- `Relationship` - MenuItem → Restaurant
- `SortedField` range queries - `price__gte`, `price__lte`
- Lazy relationship loading

### Orders
- Create orders with items
- **Status workflow** - pending → confirmed → preparing → ready → delivering → delivered
- Assign drivers to orders

**Popoto features demonstrated:**
- Multiple `Relationship` fields (customer, restaurant, driver)
- `Meta.order_by` - Default ordering by created time
- Complex object graphs with lazy loading

### Drivers
- Track driver locations
- Toggle availability status
- **Geo search** - Find nearest available drivers
- Simulate location updates

**Popoto features demonstrated:**
- `AutoKeyField` with UUID strategy
- `UniqueKeyField` for phone uniqueness
- `GeoField` with real-time updates
- `SortedField` for rating-based queries

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit |
| `d` | Dashboard tab |
| `r` | Restaurants tab |
| `m` | Menu tab |
| `o` | Orders tab |
| `v` | Drivers tab |
| `F5` | Refresh current view |
| `↑/↓` | Navigate table rows |
| `Enter` | Select row |

## Sample Data

The seed script generates:
- 50 restaurants across 10 cuisines
- 500 menu items
- 200 customers
- 20 drivers
- 500 orders with various statuses

All data is located in the NYC area for realistic geo queries.

## Architecture

```
popoto_kitchen/
├── __main__.py      # Entry point
├── app.py           # Main Textual application
├── models.py        # Popoto models (Restaurant, MenuItem, etc.)
├── seed.py          # Sample data generator
├── screens/
│   ├── dashboard.py # Overview with stats
│   ├── restaurants.py # Restaurant CRUD + geo
│   ├── menu.py      # Menu items with price filtering
│   ├── orders.py    # Order management
│   └── drivers.py   # Driver tracking
└── styles/
    └── kitchen.tcss # Textual CSS styling
```

## Popoto Features Showcased

| Feature | Screen | Example |
|---------|--------|---------|
| KeyField | Restaurants | `Restaurant.name` as unique key |
| AutoKeyField | Orders, Drivers | UUID-based order IDs |
| UniqueKeyField | Customers, Drivers | Unique email/phone |
| SortedField | Menu, Drivers | Price and rating queries |
| GeoField | Restaurants, Drivers | Location search with radius |
| Relationship | Orders, Menu | Model associations |
| Meta.order_by | Orders | Default sort by created_at |
| Range queries | Menu | `price__gte`, `price__lte` |
| Geo queries | Restaurants, Drivers | `location_radius`, `location_with_distances` |

## Development

Built with [Textual](https://textual.textualize.io/) - the modern Python TUI framework.

```bash
# Run with dev console
uv run textual run --dev -c python -m popoto_kitchen

# Take a screenshot
uv run textual run -c python -m popoto_kitchen --screenshot
```
