"""Menu screen with price filtering and restaurant relationship display."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ..models import CATEGORIES, MenuItem, Restaurant


class MenuScreen(Container):
    """Menu items screen with price range queries and relationship display."""

    def compose(self) -> ComposeResult:
        yield Static("Menu Items", classes="screen-title")

        # Filter bar
        with Horizontal(classes="filter-container"):
            yield Input(placeholder="Search items...", id="filter-name")
            yield Input(placeholder="Min Price", id="filter-min-price")
            yield Input(placeholder="Max Price", id="filter-max-price")
            yield Select(
                [(c, c) for c in ["All Categories"] + CATEGORIES],
                prompt="Category",
                id="filter-category",
            )
            yield Button("Filter", id="btn-filter", variant="primary")
            yield Button("Clear", id="btn-clear-filter")

        # Restaurant filter
        with Horizontal(classes="filter-container"):
            yield Select(
                [],
                prompt="All Restaurants",
                id="filter-restaurant",
            )
            yield Button("Sort by Price ↑", id="btn-sort-asc")
            yield Button("Sort by Price ↓", id="btn-sort-desc")

        # Results table
        yield DataTable(id="menu-table")

        # Action bar
        with Horizontal(classes="action-bar"):
            yield Button("+ New Item", id="btn-new", variant="primary")
            yield Button("Toggle Available", id="btn-toggle")
            yield Button("Delete Selected", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
            yield Label("", id="result-count")

    def on_mount(self) -> None:
        """Initialize the screen."""
        self._setup_table()
        self._load_restaurant_options()
        self.refresh_data()

    def _setup_table(self) -> None:
        """Set up the data table columns."""
        table = self.query_one("#menu-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Name",
            "Category",
            "Price",
            "Restaurant",
            "Available",
        )

    def _load_restaurant_options(self) -> None:
        """Load restaurant options for the filter dropdown."""
        try:
            restaurants = Restaurant.query.all()
            options = [("All Restaurants", "all")]
            options.extend([(r.name, r.name) for r in restaurants])

            select = self.query_one("#filter-restaurant", Select)
            select.set_options(options)
        except Exception:
            pass

    def refresh_data(self, filters: dict = None, sort_order: str = None) -> None:
        """Refresh the menu items list."""
        table = self.query_one("#menu-table", DataTable)
        table.clear()

        try:
            # Build query with price range if specified
            query_kwargs = {}

            if filters:
                if filters.get("min_price"):
                    try:
                        query_kwargs["price__gte"] = float(filters["min_price"])
                    except ValueError:
                        pass
                if filters.get("max_price"):
                    try:
                        query_kwargs["price__lte"] = float(filters["max_price"])
                    except ValueError:
                        pass

            # Execute query
            if query_kwargs:
                items = MenuItem.query.filter(**query_kwargs)
            else:
                items = MenuItem.query.all()

            # Convert to list for sorting and filtering
            items_list = list(items)

            # Apply additional filters
            if filters:
                if filters.get("name"):
                    name_filter = filters["name"].lower()
                    items_list = [
                        i for i in items_list if name_filter in i.name.lower()
                    ]
                if filters.get("category") and filters["category"] != "All Categories":
                    items_list = [
                        i for i in items_list if i.category == filters["category"]
                    ]
                if filters.get("restaurant") and filters["restaurant"] != "all":
                    items_list = [
                        i
                        for i in items_list
                        if i.restaurant and i.restaurant.name == filters["restaurant"]
                    ]

            # Sort by price if requested
            if sort_order == "asc":
                items_list.sort(key=lambda x: x.price or 0)
            elif sort_order == "desc":
                items_list.sort(key=lambda x: x.price or 0, reverse=True)

            count = 0
            for item in items_list:
                # Get restaurant name
                restaurant_name = "Unknown"
                try:
                    if item.restaurant:
                        restaurant_name = item.restaurant.name
                except Exception:
                    pass

                table.add_row(
                    item.name,
                    item.category or "Main",
                    f"${item.price:.2f}",
                    restaurant_name,
                    "Yes" if item.available else "No",
                    key=item.db_key.redis_key,
                )
                count += 1

            # Update count label
            self.query_one("#result-count", Label).update(f"{count} items")

        except Exception as e:
            self.app.notify(f"Error loading menu: {e}", severity="error")

    def _get_filters(self) -> dict:
        """Get current filter values."""
        category_select = self.query_one("#filter-category", Select)
        restaurant_select = self.query_one("#filter-restaurant", Select)
        return {
            "name": self.query_one("#filter-name", Input).value,
            "min_price": self.query_one("#filter-min-price", Input).value,
            "max_price": self.query_one("#filter-max-price", Input).value,
            "category": (
                category_select.value
                if category_select.value != Select.BLANK
                else "All Categories"
            ),
            "restaurant": (
                restaurant_select.value
                if restaurant_select.value != Select.BLANK
                else "all"
            ),
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-filter":
            self.refresh_data(filters=self._get_filters())
        elif button_id == "btn-clear-filter":
            self._clear_filters()
        elif button_id == "btn-sort-asc":
            self.refresh_data(filters=self._get_filters(), sort_order="asc")
        elif button_id == "btn-sort-desc":
            self.refresh_data(filters=self._get_filters(), sort_order="desc")
        elif button_id == "btn-new":
            self._create_item()
        elif button_id == "btn-toggle":
            self._toggle_available()
        elif button_id == "btn-delete":
            self._delete_selected()
        elif button_id == "btn-refresh":
            self._load_restaurant_options()
            self.refresh_data()
            self.app.notify("Refreshed!", timeout=1)

    def _clear_filters(self) -> None:
        """Clear all filters."""
        self.query_one("#filter-name", Input).value = ""
        self.query_one("#filter-min-price", Input).value = ""
        self.query_one("#filter-max-price", Input).value = ""
        self.query_one("#filter-category", Select).value = Select.BLANK
        self.query_one("#filter-restaurant", Select).value = Select.BLANK
        self.refresh_data()

    def _create_item(self) -> None:
        """Create a new menu item."""
        import random

        # Get a random restaurant
        restaurants = list(Restaurant.query.all())
        if not restaurants:
            self.app.notify("Create a restaurant first!", severity="warning")
            return

        restaurant = random.choice(restaurants)

        try:
            item = MenuItem(
                name=f"Special Item #{random.randint(100, 999)}",
                description="A delicious new menu item",
                price=round(random.uniform(8.99, 24.99), 2),
                restaurant=restaurant,
                category=random.choice(CATEGORIES),
                available=True,
            )
            item.save()
            self.refresh_data()
            self.app.notify(f"Created: {item.name}", severity="information")
        except Exception as e:
            self.app.notify(f"Error creating item: {e}", severity="error")

    def _toggle_available(self) -> None:
        """Toggle availability of selected item."""
        table = self.query_one("#menu-table", DataTable)
        if table.cursor_row is not None:
            try:
                key = list(table._row_locations.keys())[table.cursor_row]
                item = MenuItem.query.get(key.value.split(":")[-1])
                if item:
                    item.available = not item.available
                    item.save()
                    self.refresh_data(filters=self._get_filters())
                    status = "available" if item.available else "unavailable"
                    self.app.notify(f"Item marked {status}", timeout=1)
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select an item first", severity="warning")

    def _delete_selected(self) -> None:
        """Delete the selected menu item."""
        table = self.query_one("#menu-table", DataTable)
        if table.cursor_row is not None:
            try:
                key = list(table._row_locations.keys())[table.cursor_row]
                item = MenuItem.query.get(key.value.split(":")[-1])
                if item:
                    name = item.name
                    item.delete()
                    self.refresh_data(filters=self._get_filters())
                    self.app.notify(f"Deleted: {name}", severity="warning")
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select an item first", severity="warning")
