"""Restaurants screen with CRUD operations and geo filtering."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ..models import CUISINES, Restaurant


class RestaurantsScreen(Container):
    """Restaurant management screen with geo search capabilities."""

    def compose(self) -> ComposeResult:
        yield Static("Restaurants", classes="screen-title")

        # Filter bar
        with Horizontal(classes="filter-container"):
            yield Input(placeholder="Search by name...", id="filter-name")
            yield Select(
                [(c, c) for c in ["All Cuisines"] + CUISINES],
                prompt="Cuisine",
                id="filter-cuisine",
            )
            yield Input(placeholder="Min Rating (0-5)", id="filter-rating")
            yield Button("Filter", id="btn-filter", variant="primary")
            yield Button("Clear", id="btn-clear-filter")

        # Geo search section
        with Horizontal(classes="filter-container"):
            yield Input(placeholder="Latitude (e.g., 40.7128)", id="geo-lat")
            yield Input(placeholder="Longitude (e.g., -74.0060)", id="geo-lng")
            yield Input(placeholder="Radius (km)", id="geo-radius", value="5")
            yield Button("Find Nearby", id="btn-geo-search", variant="primary")

        # Results table
        yield DataTable(id="restaurants-table")

        # Action bar
        with Horizontal(classes="action-bar"):
            yield Button("+ New Restaurant", id="btn-new", variant="primary")
            yield Button("Edit Selected", id="btn-edit")
            yield Button("Delete Selected", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
            yield Label("", id="result-count")

    def on_mount(self) -> None:
        """Initialize the screen."""
        self._setup_table()
        self.refresh_data()

    def _setup_table(self) -> None:
        """Set up the data table columns."""
        table = self.query_one("#restaurants-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Name",
            "Cuisine",
            "Rating",
            "Location",
            "Status",
            "Distance",
        )

    def refresh_data(self, filters: dict = None, geo_search: dict = None) -> None:
        """Refresh the restaurant list."""
        table = self.query_one("#restaurants-table", DataTable)
        table.clear()

        try:
            if geo_search:
                # Geo-based search
                restaurants = Restaurant.query.filter(
                    location=(geo_search["lat"], geo_search["lng"]),
                    location_radius=geo_search["radius"],
                    location_radius_unit="km",
                    location_with_distances=True,
                )
            else:
                # Standard query
                restaurants = Restaurant.query.all()

            count = 0
            for restaurant in restaurants:
                # Apply filters
                if filters:
                    if filters.get("name"):
                        if filters["name"].lower() not in restaurant.name.lower():
                            continue
                    if filters.get("cuisine") and filters["cuisine"] != "All Cuisines":
                        if restaurant.cuisine != filters["cuisine"]:
                            continue
                    if filters.get("rating"):
                        try:
                            min_rating = float(filters["rating"])
                            if restaurant.rating < min_rating:
                                continue
                        except ValueError:
                            pass

                # Format location
                loc = restaurant.location
                if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                    location_str = f"{loc[0]:.4f}, {loc[1]:.4f}"
                else:
                    location_str = str(loc)

                # Format distance if available
                distance_str = ""
                if hasattr(restaurant, "_geo_distance"):
                    distance_str = f"{restaurant._geo_distance:.2f} km"

                # Rating color indicator
                rating = restaurant.rating or 0
                rating_str = f"{rating:.1f} ★"

                table.add_row(
                    restaurant.name,
                    restaurant.cuisine,
                    rating_str,
                    location_str,
                    "Active" if restaurant.active else "Inactive",
                    distance_str,
                    key=restaurant.redis_key,
                )
                count += 1

            # Update count label
            self.query_one("#result-count", Label).update(f"{count} restaurants")

        except Exception as e:
            self.app.notify(f"Error loading restaurants: {e}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-filter":
            self._apply_filters()
        elif button_id == "btn-clear-filter":
            self._clear_filters()
        elif button_id == "btn-geo-search":
            self._geo_search()
        elif button_id == "btn-new":
            self._create_restaurant()
        elif button_id == "btn-edit":
            self._edit_selected()
        elif button_id == "btn-delete":
            self._delete_selected()
        elif button_id == "btn-refresh":
            self.refresh_data()
            self.app.notify("Refreshed!", timeout=1)

    def _apply_filters(self) -> None:
        """Apply current filter values."""
        filters = {
            "name": self.query_one("#filter-name", Input).value,
            "cuisine": self.query_one("#filter-cuisine", Select).value,
            "rating": self.query_one("#filter-rating", Input).value,
        }
        self.refresh_data(filters=filters)

    def _clear_filters(self) -> None:
        """Clear all filters."""
        self.query_one("#filter-name", Input).value = ""
        self.query_one("#filter-cuisine", Select).value = Select.BLANK
        self.query_one("#filter-rating", Input).value = ""
        self.query_one("#geo-lat", Input).value = ""
        self.query_one("#geo-lng", Input).value = ""
        self.query_one("#geo-radius", Input).value = "5"
        self.refresh_data()

    def _geo_search(self) -> None:
        """Perform geo-based search."""
        try:
            lat = float(self.query_one("#geo-lat", Input).value)
            lng = float(self.query_one("#geo-lng", Input).value)
            radius = float(self.query_one("#geo-radius", Input).value or "5")

            geo_search = {"lat": lat, "lng": lng, "radius": radius}
            self.refresh_data(geo_search=geo_search)
            self.app.notify(f"Found restaurants within {radius}km", timeout=2)

        except ValueError:
            self.app.notify("Please enter valid coordinates", severity="error")

    def _create_restaurant(self) -> None:
        """Create a new restaurant (simplified - uses random data)."""
        import random
        from ..seed import random_location, restaurant_name

        cuisine = random.choice(CUISINES)
        name = restaurant_name(cuisine)

        try:
            restaurant = Restaurant(
                name=name,
                cuisine=cuisine,
                rating=round(random.uniform(3.0, 5.0), 1),
                location=random_location(),
                active=True,
            )
            restaurant.save()
            self.refresh_data()
            self.app.notify(f"Created: {name}", severity="information")
        except Exception as e:
            self.app.notify(f"Error creating restaurant: {e}", severity="error")

    def _edit_selected(self) -> None:
        """Edit the selected restaurant."""
        table = self.query_one("#restaurants-table", DataTable)
        if table.cursor_row is not None:
            try:
                row_key = table.get_row_at(table.cursor_row)
                # For now, just toggle active status
                key = list(table._row_locations.keys())[table.cursor_row]
                restaurant = Restaurant.query.get(key.value.split(":")[-1])
                if restaurant:
                    restaurant.active = not restaurant.active
                    restaurant.save()
                    self.refresh_data()
                    status = "activated" if restaurant.active else "deactivated"
                    self.app.notify(f"Restaurant {status}", timeout=1)
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select a restaurant first", severity="warning")

    def _delete_selected(self) -> None:
        """Delete the selected restaurant."""
        table = self.query_one("#restaurants-table", DataTable)
        if table.cursor_row is not None:
            try:
                key = list(table._row_locations.keys())[table.cursor_row]
                restaurant = Restaurant.query.get(key.value.split(":")[-1])
                if restaurant:
                    name = restaurant.name
                    restaurant.delete()
                    self.refresh_data()
                    self.app.notify(f"Deleted: {name}", severity="warning")
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select a restaurant first", severity="warning")
