"""Orders screen with status management and driver assignment."""

from textual.app import ComposeResult
from textual.containers import Container, Horizontal
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from ..models import Customer, Driver, Order, Restaurant


class OrdersScreen(Container):
    """Order management screen with status workflow."""

    # Status display with colors/symbols
    STATUS_DISPLAY = {
        "pending": "⏳ Pending",
        "confirmed": "✓ Confirmed",
        "preparing": "🍳 Preparing",
        "ready": "📦 Ready",
        "delivering": "🚗 Delivering",
        "delivered": "✅ Delivered",
        "cancelled": "❌ Cancelled",
    }

    def compose(self) -> ComposeResult:
        yield Static("Orders", classes="screen-title")

        # Filter bar
        with Horizontal(classes="filter-container"):
            yield Select(
                [(s, s) for s in ["All Statuses"] + Order._STATUSES],
                prompt="Status",
                id="filter-status",
            )
            yield Input(placeholder="Min Total ($)", id="filter-min-total")
            yield Input(placeholder="Max Total ($)", id="filter-max-total")
            yield Button("Filter", id="btn-filter", variant="primary")
            yield Button("Clear", id="btn-clear-filter")

        # Results table
        yield DataTable(id="orders-table")

        # Action bar
        with Horizontal(classes="action-bar"):
            yield Button("+ New Order", id="btn-new", variant="primary")
            yield Button("→ Advance Status", id="btn-advance", variant="success")
            yield Button("Assign Driver", id="btn-assign-driver")
            yield Button("Cancel Order", id="btn-cancel", variant="error")
            yield Button("Refresh", id="btn-refresh")
            yield Label("", id="result-count")

    def on_mount(self) -> None:
        """Initialize the screen."""
        self._setup_table()
        self.refresh_data()

    def _setup_table(self) -> None:
        """Set up the data table columns."""
        table = self.query_one("#orders-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Order ID",
            "Customer",
            "Restaurant",
            "Items",
            "Total",
            "Status",
            "Driver",
            "Created",
        )

    def refresh_data(self, filters: dict = None) -> None:
        """Refresh the orders list."""
        table = self.query_one("#orders-table", DataTable)
        table.clear()

        try:
            # Build query for SortedField filters only (total)
            query_kwargs = {}
            status_filter = None

            if filters:
                if filters.get("status") and filters["status"] != "All Statuses":
                    status_filter = filters["status"]
                if filters.get("min_total"):
                    try:
                        query_kwargs["total__gte"] = float(filters["min_total"])
                    except ValueError:
                        pass
                if filters.get("max_total"):
                    try:
                        query_kwargs["total__lte"] = float(filters["max_total"])
                    except ValueError:
                        pass

            # Execute query (only SortedField params go to filter())
            if query_kwargs:
                orders = Order.query.filter(**query_kwargs)
            else:
                orders = Order.query.all()

            count = 0
            for order in orders:
                # Apply status filter client-side (plain Field, not indexed)
                if status_filter and order.status != status_filter:
                    continue
                # Get related model names
                customer_name = "Unknown"
                restaurant_name = "Unknown"
                driver_name = "-"

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

                try:
                    if order.driver:
                        driver_name = order.driver.name
                except Exception:
                    pass

                # Format items (show count)
                items = order.items or []
                items_str = f"{len(items)} item(s)"

                # Format order ID
                order_id = order.order_id[:8] if order.order_id else "N/A"

                # Format created time
                created = order.created_at[:16] if order.created_at else "N/A"

                # Status display
                status = self.STATUS_DISPLAY.get(order.status, order.status)

                table.add_row(
                    order_id,
                    customer_name,
                    restaurant_name,
                    items_str,
                    f"${order.total:.2f}",
                    status,
                    driver_name,
                    created,
                    key=order.db_key.redis_key,
                )
                count += 1

            # Update count label
            self.query_one("#result-count", Label).update(f"{count} orders")

        except Exception as e:
            self.app.notify(f"Error loading orders: {e}", severity="error")

    def _get_filters(self) -> dict:
        """Get current filter values."""
        status_select = self.query_one("#filter-status", Select)
        return {
            "status": (
                status_select.value if status_select.value != Select.BLANK else None
            ),
            "min_total": self.query_one("#filter-min-total", Input).value,
            "max_total": self.query_one("#filter-max-total", Input).value,
        }

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id

        if button_id == "btn-filter":
            self.refresh_data(filters=self._get_filters())
        elif button_id == "btn-clear-filter":
            self._clear_filters()
        elif button_id == "btn-new":
            self._create_order()
        elif button_id == "btn-advance":
            self._advance_status()
        elif button_id == "btn-assign-driver":
            self._assign_driver()
        elif button_id == "btn-cancel":
            self._cancel_order()
        elif button_id == "btn-refresh":
            self.refresh_data()
            self.app.notify("Refreshed!", timeout=1)

    def _clear_filters(self) -> None:
        """Clear all filters."""
        self.query_one("#filter-status", Select).value = Select.BLANK
        self.query_one("#filter-min-total", Input).value = ""
        self.query_one("#filter-max-total", Input).value = ""
        self.refresh_data()

    def _get_selected_order(self) -> Order | None:
        """Get the currently selected order."""
        table = self.query_one("#orders-table", DataTable)
        if table.cursor_row is not None:
            try:
                key = list(table._row_locations.keys())[table.cursor_row]
                return Order.query.get(redis_key=key.value)
            except Exception:
                pass
        return None

    def _create_order(self) -> None:
        """Create a new order with random data."""
        import random
        from datetime import datetime

        from ..models import MenuItem

        # Get random customer and restaurant
        customers = list(Customer.query.all())
        restaurants = list(Restaurant.query.all())

        if not customers or not restaurants:
            self.app.notify(
                "Create customers and restaurants first!", severity="warning"
            )
            return

        customer = random.choice(customers)
        restaurant = random.choice(restaurants)

        # Get some menu items from this restaurant
        menu_items = [
            m
            for m in MenuItem.query.all()
            if m.restaurant and m.restaurant.name == restaurant.name
        ]
        if menu_items:
            order_items = random.sample(
                menu_items, min(random.randint(1, 4), len(menu_items))
            )
            item_names = [item.name for item in order_items]
            total = sum(item.price for item in order_items)
        else:
            item_names = ["Chef's Special"]
            total = round(random.uniform(15.0, 45.0), 2)

        try:
            order = Order(
                customer=customer,
                restaurant=restaurant,
                items=item_names,
                total=round(total, 2),
                status="pending",
                created_at=datetime.utcnow().isoformat(),
                updated_at=datetime.utcnow().isoformat(),
            )
            order.save()
            self.refresh_data(filters=self._get_filters())
            self.app.notify(f"Created order: ${total:.2f}", severity="information")
        except Exception as e:
            self.app.notify(f"Error creating order: {e}", severity="error")

    def _advance_status(self) -> None:
        """Advance the selected order to next status."""
        order = self._get_selected_order()
        if order:
            if order.status in ["delivered", "cancelled"]:
                self.app.notify("Order is already complete", severity="warning")
                return

            if order.advance_status():
                self.refresh_data(filters=self._get_filters())
                status = self.STATUS_DISPLAY.get(order.status, order.status)
                self.app.notify(f"Order now: {status}", timeout=2)
            else:
                self.app.notify("Cannot advance status", severity="warning")
        else:
            self.app.notify("Select an order first", severity="warning")

    def _assign_driver(self) -> None:
        """Assign a driver to the selected order.

        Demonstrates the PR #163 fix: Relationship.on_save() index cleanup on
        value change. When replacing an existing driver, the order key is removed
        from the old driver's relationship index and added to the new driver's index.
        The notification shows the old → new driver name so the replacement is visible.
        """
        import random

        order = self._get_selected_order()
        if not order:
            self.app.notify("Select an order first", severity="warning")
            return

        if order.status in ["delivered", "cancelled"]:
            self.app.notify("Order is already complete", severity="warning")
            return

        # Capture old driver before replacing (for replacement notification)
        old_driver_name = None
        try:
            if order.driver:
                old_driver_name = order.driver.name
        except Exception:
            pass

        # Get active drivers
        active_drivers = [d for d in Driver.query.all() if d.active]
        if not active_drivers:
            self.app.notify("No active drivers available", severity="warning")
            return

        # Assign random driver (in real app, would use geo to find nearest)
        driver = random.choice(active_drivers)

        # Guard: don't reassign the same driver
        if old_driver_name and old_driver_name == driver.name:
            self.app.notify(
                f"Driver already assigned: {driver.name}", severity="warning"
            )
            return

        try:
            order.driver = driver
            order.save()
            self.refresh_data(filters=self._get_filters())
            if old_driver_name:
                # Replacement: show old → new (PR #163 fix is exercised here)
                self.app.notify(
                    f"Replaced driver: {old_driver_name} → {driver.name}",
                    severity="information",
                )
            else:
                self.app.notify(f"Assigned: {driver.name}", severity="information")
        except Exception as e:
            self.app.notify(f"Error assigning driver: {e}", severity="error")

    def _cancel_order(self) -> None:
        """Cancel the selected order."""
        order = self._get_selected_order()
        if order:
            if order.status == "cancelled":
                self.app.notify("Order is already cancelled", severity="warning")
                return

            if order.status == "delivered":
                self.app.notify("Cannot cancel delivered order", severity="warning")
                return

            try:
                order.status = "cancelled"
                order.save()
                self.refresh_data(filters=self._get_filters())
                self.app.notify("Order cancelled", severity="warning")
            except Exception as e:
                self.app.notify(f"Error: {e}", severity="error")
        else:
            self.app.notify("Select an order first", severity="warning")
