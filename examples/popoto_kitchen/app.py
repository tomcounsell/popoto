"""Main Popoto Kitchen TUI Application.

A terminal-based demo showcasing Popoto Redis ORM capabilities
through an interactive food delivery system interface.
"""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, TabbedContent, TabPane

from .screens.dashboard import DashboardScreen
from .screens.drivers import DriversScreen
from .screens.menu import MenuScreen
from .screens.orders import OrdersScreen
from .screens.restaurants import RestaurantsScreen


class PopotoKitchen(App):
    """Popoto Kitchen - Interactive Redis ORM Demo."""

    TITLE = "Popoto Kitchen"
    SUB_TITLE = "Redis ORM Demo"
    CSS_PATH = "styles/kitchen.tcss"

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True, priority=True),
        Binding("d", "switch_tab('dashboard')", "Dashboard", show=True),
        Binding("r", "switch_tab('restaurants')", "Restaurants", show=True),
        Binding("m", "switch_tab('menu')", "Menu", show=True),
        Binding("o", "switch_tab('orders')", "Orders", show=True),
        Binding("v", "switch_tab('drivers')", "Drivers", show=True),
        Binding("f5", "refresh", "Refresh", show=True),
    ]

    def compose(self) -> ComposeResult:
        """Create the application layout."""
        yield Header()
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield DashboardScreen()
            with TabPane("Restaurants", id="restaurants"):
                yield RestaurantsScreen()
            with TabPane("Menu", id="menu"):
                yield MenuScreen()
            with TabPane("Orders", id="orders"):
                yield OrdersScreen()
            with TabPane("Drivers", id="drivers"):
                yield DriversScreen()
        yield Footer()

    def action_switch_tab(self, tab_id: str) -> None:
        """Switch to a specific tab."""
        self.query_one(TabbedContent).active = tab_id

    def action_refresh(self) -> None:
        """Refresh the current screen's data."""
        tabbed = self.query_one(TabbedContent)
        active_pane = tabbed.get_pane(tabbed.active)
        # Find the screen widget inside the pane
        for child in active_pane.children:
            if hasattr(child, "refresh_data"):
                child.refresh_data()
                self.notify("Data refreshed", timeout=1)
                return
        self.notify("Refreshed", timeout=1)


def main():
    """Run the Popoto Kitchen application."""
    app = PopotoKitchen()
    app.run()


if __name__ == "__main__":
    main()
