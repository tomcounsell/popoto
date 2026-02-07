"""Drivers screen with location tracking and availability management."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ..models import Driver


class DriversScreen(Container):
    """Driver management screen with geo search capabilities."""

    def compose(self) -> ComposeResult:
        yield Static("Drivers", classes="screen-title")

        # Filter bar
        with Horizontal(classes="filter-container"):
            yield Input(placeholder="Search by name...", id="filter-name")
            yield Select(
                [("All", "all"), ("Active", "active"), ("Inactive", "inactive")],
                prompt="Status",
                id="filter-status",
            )
            yield Input(placeholder="Min Rating", id="filter-min-rating")
            yield Select(
                [("All", "all"), ("Car", "car"), ("Bike", "bike"), ("Scooter", "scooter")],
                prompt="Vehicle",
                id="filter-vehicle",
            )
            yield Button("Filter", id="btn-filter", variant="primary")
            yield Button("Clear", id="btn-clear-filter")

        # Geo search section
        with Horizontal(classes="filter-container"):
            yield Input(placeholder="Latitude (e.g., 40.7128)", id="geo-lat")
            yield Input(placeholder="Longitude (e.g., -74.0060)", id="geo-lng")
            yield Input(placeholder="Radius (km)", id="geo-radius", value="3")
            yield Button("Find Nearby", id="btn-geo-search", variant="primary")

        # Results table
        yield DataTable(id="drivers-table")

        # Action bar
        with Horizontal(classes="action-bar"):
            yield Button("+ New Driver", id="btn-new", variant="primary")
            yield Button("Toggle Active", id="btn-toggle")
            yield Button("Update Location", id="btn-update-location")
            yield Button("Delete Selected", id="btn-delete", variant="error")
            yield Button("Refresh", id="btn-refresh")
            yield Label("", id="result-count")

    def on_mount(self) -> None:
        """Initialize the screen."""
        self._setup_table()
        self.refresh_data()

    def _setup_table(self) -> None:
        """Set up the data table columns."""
        table = self.query_one("#drivers-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Name",
            "Phone",
            "Vehicle",
            "Rating",
            "Location",
            "Status",
            "Distance",
        )

    def refresh_data(self, filters: dict = None, geo_search: dict = None) -> None:
        """Refresh the drivers list."""
        table = self.query_one("#drivers-table", DataTable)
        table.clear()

        try:
            if geo_search:
                # Geo-based search
                drivers = Driver.query.filter(
                    location=(geo_search["lat"], geo_search["lng"]),
                    location_radius=geo_search["radius"],
                    location_radius_unit="km",
                    location_with_distances=True,
                )
            else:
                # Standard query with rating filter
                query_kwargs = {}
                if filters and filters.get("min_rating"):
                    try:
                        query_kwargs["rating__gte"] = float(filters["min_rating"])
                    except ValueError:
                        pass

                if query_kwargs:
                    drivers = Driver.query.filter(**query_kwargs)
                else:
                    drivers = Driver.query.all()

            count = 0
            for driver in drivers:
                # Apply filters
                if filters:
                    if filters.get("name"):
                        if filters["name"].lower() not in driver.name.lower():
                            continue
                    if filters.get("status") and filters["status"] != "all":
                        is_active = driver.active
                        if filters["status"] == "active" and not is_active:
                            continue
                        if filters["status"] == "inactive" and is_active:
                            continue
                    if filters.get("vehicle") and filters["vehicle"] != "all":
                        if driver.vehicle != filters["vehicle"]:
                            continue

                # Format location
                loc = driver.location
                if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                    location_str = f"{loc[0]:.4f}, {loc[1]:.4f}"
                else:
                    location_str = str(loc)

                # Format distance if available
                distance_str = ""
                if hasattr(driver, "_geo_distance"):
                    distance_str = f"{driver._geo_distance:.2f} km"

                # Rating with stars
                rating = driver.rating or 0
                rating_str = f"{rating:.1f} ★"

                # Status display
                status = "🟢 Active" if driver.active else "⚫ Offline"

                # Vehicle emoji
                vehicle_emoji = {"car": "🚗", "bike": "🚲", "scooter": "🛵"}.get(driver.vehicle, "🚗")
                vehicle_str = f"{vehicle_emoji} {driver.vehicle.title()}"

                table.add_row(
                    driver.name,
                    driver.phone,
                    vehicle_str,
                    rating_str,
                    location_str,
                    status,
                    distance_str,
                    key=driver.redis_key,
                )
                count += 1

            # Update count label
            self.query_one("#result-count", Label).update(f"{count} drivers")

        except Exception as e:
            self.app.notify(f"Error loading drivers: {e}", severity="error")

    def _get_filters(self) -> dict:
        """Get current filter values."""
        status_select = self.query_one("#filter-status", Select)
        vehicle_select = self.query_one("#filter-vehicle", Select)
        return {
            "name": self.query_one("#filter-name", Input).value,
            "status": status_select.value if status_select.value != Select.BLANK else "all",
            "min_rating": self.query_one("#filter-min-rating", Input).value,
            "vehicle": vehicle_select.value if vehicle_select.value != Select.BLANK else "all",
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-filter":
            self.refresh_data(filters=self._get_filters())
        elif button_id == "btn-clear-filter":
            self._clear_filters()
        elif button_id == "btn-geo-search":
            self._geo_search()
        elif button_id == "btn-new":
            self._create_driver()
        elif button_id == "btn-toggle":
            self._toggle_active()
        elif button_id == "btn-update-location":
            self._update_location()
        elif button_id == "btn-delete":
            self._delete_selected()
        elif button_id == "btn-refresh":
            self.refresh_data()
            self.app.notify("Refreshed!", timeout=1)

    def _clear_filters(self) -> None:
        """Clear all filters."""
        self.query_one("#filter-name", Input).value = ""
        self.query_one("#filter-status", Select).value = Select.BLANK
        self.query_one("#filter-min-rating", Input).value = ""
        self.query_one("#filter-vehicle", Select).value = Select.BLANK
        self.query_one("#geo-lat", Input).value = ""
        self.query_one("#geo-lng", Input).value = ""
        self.query_one("#geo-radius", Input).value = "3"
        self.refresh_data()

    def _geo_search(self) -> None:
        """Perform geo-based search."""
        try:
            lat = float(self.query_one("#geo-lat", Input).value)
            lng = float(self.query_one("#geo-lng", Input).value)
            radius = float(self.query_one("#geo-radius", Input).value or "3")

            geo_search = {"lat": lat, "lng": lng, "radius": radius}
            self.refresh_data(geo_search=geo_search)
            self.app.notify(f"Found drivers within {radius}km", timeout=2)

        except ValueError:
            self.app.notify("Please enter valid coordinates", severity="error")

    def _get_selected_driver(self) -> Driver | None:
        """Get the currently selected driver."""
        table = self.query_one("#drivers-table", DataTable)
        if table.cursor_row is not None:
            try:
                key = list(table._row_locations.keys())[table.cursor_row]
                return Driver.query.get(key.value.split(":")[-1])
            except Exception:
                pass
        return None

    def _create_driver(self) -> None:
        """Create a new driver with random data."""
        import random
        from ..seed import random_location, random_name, random_phone

        name = random_name()
        phone = random_phone()

        # Ensure unique phone
        attempts = 0
        while attempts < 10:
            try:
                driver = Driver(
                    name=name,
                    phone=phone,
                    rating=round(random.uniform(4.0, 5.0), 1),
                    location=random_location(),
                    active=True,
                    vehicle=random.choice(["car", "bike", "scooter"]),
                )
                driver.save()
                self.refresh_data(filters=self._get_filters())
                self.app.notify(f"Created driver: {name}", severity="information")
                return
            except Exception:
                phone = random_phone()
                attempts += 1

        self.app.notify("Error creating driver", severity="error")

    def _toggle_active(self) -> None:
        """Toggle active status of selected driver."""
        driver = self._get_selected_driver()
        if driver:
            try:
                driver.active = not driver.active
                driver.save()
                self.refresh_data(filters=self._get_filters())
                status = "active" if driver.active else "offline"
                self.app.notify(f"{driver.name} is now {status}", timeout=2)
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select a driver first", severity="warning")

    def _update_location(self) -> None:
        """Simulate driver movement by updating location."""
        import random
        from ..seed import random_location

        driver = self._get_selected_driver()
        if driver:
            try:
                # Move slightly from current location
                current = driver.location
                if isinstance(current, (list, tuple)) and len(current) >= 2:
                    new_lat = current[0] + random.uniform(-0.01, 0.01)
                    new_lng = current[1] + random.uniform(-0.01, 0.01)
                    driver.location = (round(new_lat, 6), round(new_lng, 6))
                else:
                    driver.location = random_location()

                driver.save()
                self.refresh_data(filters=self._get_filters())
                self.app.notify(f"Updated {driver.name}'s location", timeout=1)
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select a driver first", severity="warning")

    def _delete_selected(self) -> None:
        """Delete the selected driver."""
        driver = self._get_selected_driver()
        if driver:
            try:
                name = driver.name
                driver.delete()
                self.refresh_data(filters=self._get_filters())
                self.app.notify(f"Deleted: {name}", severity="warning")
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select a driver first", severity="warning")
