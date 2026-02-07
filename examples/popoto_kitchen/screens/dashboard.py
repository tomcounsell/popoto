"""Dashboard screen showing overview statistics and recent activity."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Label, Static

from ..models import Customer, Driver, MenuItem, Order, Restaurant


class StatBox(Static):
    """A box displaying a statistic with label and value."""

    def __init__(self, label: str, value: str = "0", classes: str = "") -> None:
        super().__init__(classes=f"stat-box {classes}")
        self.label = label
        self.value = value

    def compose(self) -> ComposeResult:
        yield Label(self.value, classes="stat-value", id=f"stat-{self.label.lower().replace(' ', '-')}")
        yield Label(self.label, classes="stat-label")

    def update_value(self, value: str) -> None:
        """Update the displayed value."""
        self.query_one(".stat-value", Label).update(value)


class DashboardScreen(Container):
    """Main dashboard with statistics and recent activity."""

    def compose(self) -> ComposeResult:
        yield Static("Welcome to Popoto Kitchen!", classes="welcome-text")

        with Horizontal(classes="stats-container"):
            yield StatBox("Restaurants", "0", id="stat-restaurants")
            yield StatBox("Menu Items", "0", id="stat-menu")
            yield StatBox("Customers", "0", id="stat-customers")
            yield StatBox("Drivers", "0", id="stat-drivers")

        with Horizontal(classes="stats-container"):
            yield StatBox("Total Orders", "0", id="stat-orders")
            yield StatBox("Active Orders", "0", id="stat-active")
            yield StatBox("Delivered Today", "0", id="stat-delivered")
            yield StatBox("Active Drivers", "0", id="stat-active-drivers")

        yield Static("Recent Orders", classes="recent-activity-title")
        yield DataTable(id="recent-orders")

        with Horizontal(classes="action-bar"):
            yield Button("Seed Sample Data", id="btn-seed", variant="primary")
            yield Button("Clear All Data", id="btn-clear", variant="warning")
            yield Button("Refresh Stats", id="btn-refresh", variant="default")

    def on_mount(self) -> None:
        """Initialize the dashboard when mounted."""
        self._setup_recent_orders_table()
        self.refresh_data()

    def _setup_recent_orders_table(self) -> None:
        """Set up the recent orders table columns."""
        table = self.query_one("#recent-orders", DataTable)
        table.add_columns(
            "Order ID",
            "Customer",
            "Restaurant",
            "Total",
            "Status",
            "Created",
        )

    def refresh_data(self) -> None:
        """Refresh all dashboard statistics."""
        self._update_stats()
        self._update_recent_orders()

    def _update_stats(self) -> None:
        """Update statistic values."""
        try:
            # Count totals
            restaurant_count = Restaurant.query.count()
            menu_count = MenuItem.query.count()
            customer_count = Customer.query.count()
            driver_count = Driver.query.count()
            order_count = Order.query.count()

            # Active orders (not delivered or cancelled)
            active_statuses = ["pending", "confirmed", "preparing", "ready", "delivering"]
            active_count = sum(
                Order.query.filter(status=s).count() for s in active_statuses
            )

            # Delivered count
            delivered_count = Order.query.filter(status="delivered").count()

            # Active drivers
            active_drivers = Driver.query.filter(active=True).count()

            # Update UI
            self._set_stat("stat-restaurants", str(restaurant_count))
            self._set_stat("stat-menu", str(menu_count))
            self._set_stat("stat-customers", str(customer_count))
            self._set_stat("stat-drivers", str(driver_count))
            self._set_stat("stat-orders", str(order_count))
            self._set_stat("stat-active", str(active_count))
            self._set_stat("stat-delivered", str(delivered_count))
            self._set_stat("stat-active-drivers", str(active_drivers))

        except Exception as e:
            self.app.notify(f"Error loading stats: {e}", severity="error")

    def _set_stat(self, stat_id: str, value: str) -> None:
        """Update a specific stat box value."""
        try:
            stat_box = self.query_one(f"#{stat_id}", StatBox)
            stat_box.update_value(value)
        except Exception:
            pass  # Stat box might not exist yet

    def _update_recent_orders(self) -> None:
        """Update the recent orders table."""
        table = self.query_one("#recent-orders", DataTable)
        table.clear()

        try:
            # Get 10 most recent orders
            orders = Order.query.all()[:10]

            for order in orders:
                # Get customer and restaurant names
                customer_name = "Unknown"
                restaurant_name = "Unknown"

                try:
                    if order.customer:
                        customer_name = order.customer.name
                except Exception:
                    pass

                try:
                    if order.restaurant:
                        restaurant_name = order.restaurant.name
                except Exception:
                    pass

                # Format order ID (first 8 chars)
                order_id = order.order_id[:8] if order.order_id else "N/A"

                # Format created time
                created = order.created_at[:16] if order.created_at else "N/A"

                table.add_row(
                    order_id,
                    customer_name,
                    restaurant_name,
                    f"${order.total:.2f}",
                    order.status.upper(),
                    created,
                )

        except Exception as e:
            self.app.notify(f"Error loading orders: {e}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-seed":
            self._seed_data()
        elif button_id == "btn-clear":
            self._clear_data()
        elif button_id == "btn-refresh":
            self.refresh_data()
            self.app.notify("Data refreshed!", timeout=1)

    def _seed_data(self) -> None:
        """Seed sample data."""
        from ..seed import seed_database

        self.app.notify("Seeding data... this may take a moment")
        try:
            seed_database(
                num_restaurants=50,
                num_customers=200,
                num_drivers=20,
                num_orders=500,
            )
            self.refresh_data()
            self.app.notify("Sample data seeded successfully!", severity="information")
        except Exception as e:
            self.app.notify(f"Error seeding data: {e}", severity="error")

    def _clear_data(self) -> None:
        """Clear all data."""
        from ..seed import clear_database

        try:
            clear_database()
            self.refresh_data()
            self.app.notify("All data cleared!", severity="warning")
        except Exception as e:
            self.app.notify(f"Error clearing data: {e}", severity="error")
