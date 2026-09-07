"""Headless smoke test for the Popoto Kitchen TUI.

The demo's acceptance signal used to be "run it and look at it". Every screen
wraps its popoto calls in `except Exception as e: self.app.notify(..., severity="error")`,
so a broken query renders as an empty table and a toast — a naive
"does it mount?" test passes against a completely non-functional app.

This suite closes that hole by overriding `App.notify()`, which is public and
stable, and treating any `severity="error"` notification as a test failure.
That single assertion is what surfaced all three popoto-semantics defects fixed
alongside this test. It is paired with row-count and label assertions so a
screen that silently renders nothing also fails.

One structural trap to know about: widget ids are only unique among siblings,
and the five screens reuse `#btn-filter`, `#btn-clear-filter`, `#btn-refresh`,
`#filter-name`, and `#result-count`. An app-level `query_one("#btn-filter")`
resolves to whichever screen comes first in DOM order (restaurants), so every
lookup below is scoped through its screen container. Only the DataTable ids
(`#menu-table`, `#restaurants-table`, ...) are app-unique.

Requires Redis. Bind an explicit test database before pytest starts — see
`conftest.py` for why it cannot be done from a fixture:

    REDIS_URL=redis://localhost:6379/13 pytest
"""

from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, Label, Select, TabbedContent

import popoto_kitchen
from popoto_kitchen.app import PopotoKitchen
from popoto_kitchen.screens.menu import MenuScreen
from popoto_kitchen.screens.restaurants import RestaurantsScreen

TAB_IDS = ["dashboard", "restaurants", "menu", "orders", "drivers"]


class KitchenUnderTest(PopotoKitchen):
    """PopotoKitchen that records notifications instead of swallowing them.

    `notify()` is the public Textual API the screens already call; overriding it
    needs no private attribute access and no version pin.
    """

    # Textual resolves a relative CSS_PATH against the module that defines the
    # App *subclass*, not the base class — from this file that would be
    # tests/styles/kitchen.tcss. Re-anchor it on the package.
    CSS_PATH = str(Path(popoto_kitchen.__file__).parent / "styles" / "kitchen.tcss")

    def __init__(self) -> None:
        super().__init__()
        self.captured: list[tuple[str, str]] = []

    def notify(
        self, message, *, title="", severity="information", timeout=None, markup=True
    ):
        self.captured.append((severity, str(message)))
        # Deliberately not calling super(): the real implementation schedules a
        # toast widget, which is pure overhead in a headless run.

    @property
    def errors(self) -> list[str]:
        return [m for sev, m in self.captured if sev == "error"]


def assert_no_errors(app: KitchenUnderTest, context: str) -> None:
    assert not app.errors, f"{context}: app reported errors: {app.errors}"


def label_text(screen, selector: str) -> str:
    return str(screen.query_one(selector, Label).content)


@pytest.fixture
def app(seeded_kitchen) -> KitchenUnderTest:
    return KitchenUnderTest()


async def open_tab(app, pilot, tab_id: str):
    app.query_one(TabbedContent).active = tab_id
    await pilot.pause()


async def test_all_tabs_mount_and_populate(app, seeded_kitchen):
    """Every tab mounts, queries Redis, and renders rows without an error toast."""
    async with app.run_test() as pilot:
        for tab_id in TAB_IDS:
            await open_tab(app, pilot, tab_id)
            assert_no_errors(app, f"tab {tab_id}")

        assert (
            app.query_one("#restaurants-table", DataTable).row_count
            == seeded_kitchen["num_restaurants"]
        )
        assert (
            app.query_one("#drivers-table", DataTable).row_count
            == seeded_kitchen["num_drivers"]
        )
        assert (
            app.query_one("#orders-table", DataTable).row_count
            == seeded_kitchen["num_orders"]
        )
        assert app.query_one("#menu-table", DataTable).row_count > 0

        stat = app.query_one("#stat-restaurants")
        assert str(seeded_kitchen["num_restaurants"]) in str(
            stat.query_one(Label).content
        )


async def test_restaurant_geo_search(app):
    """Geo search returns rows without error.

    Note the ordering trap: `btn-clear-filter` wipes the geo inputs, so the
    inputs must be filled immediately before pressing `btn-geo-search`.
    """
    async with app.run_test() as pilot:
        await open_tab(app, pilot, "restaurants")
        screen = app.query_one(RestaurantsScreen)

        screen.query_one("#geo-lat", Input).value = "40.7128"
        screen.query_one("#geo-lng", Input).value = "-74.0060"
        screen.query_one("#geo-radius", Input).value = "500"
        await pilot.click(screen.query_one("#btn-geo-search", Button))
        await pilot.pause()

        assert_no_errors(app, "geo search")
        assert app.query_one("#restaurants-table", DataTable).row_count > 0


async def test_restaurant_rename_migrates_key(app, seeded_kitchen):
    """Renaming exercises save(migrate_key=True) on a KeyField.

    Without the flag popoto refuses the write, which the screen turns into an
    error toast — so this asserts the migration path, not just the button.
    """
    async with app.run_test() as pilot:
        await open_tab(app, pilot, "restaurants")
        screen = app.query_one(RestaurantsScreen)

        table = app.query_one("#restaurants-table", DataTable)
        before = {table.get_row_at(i)[0] for i in range(table.row_count)}

        await pilot.click(screen.query_one("#btn-rename", Button))
        await pilot.pause()

        assert_no_errors(app, "rename")
        after = {table.get_row_at(i)[0] for i in range(table.row_count)}
        # Row count is preserved: the old key is deleted, not left as a twin.
        assert table.row_count == seeded_kitchen["num_restaurants"]
        assert before != after, "rename did not change any restaurant name"


async def test_menu_move_category_migrates_key(app):
    """Moving an item across categories rewrites its KeyField identity."""
    async with app.run_test() as pilot:
        await open_tab(app, pilot, "menu")
        screen = app.query_one(MenuScreen)

        table = app.query_one("#menu-table", DataTable)
        before_count = table.row_count
        before_categories = [table.get_row_at(i)[1] for i in range(table.row_count)]

        await pilot.click(screen.query_one("#btn-move-category", Button))
        await pilot.pause()

        assert_no_errors(app, "move category")
        assert table.row_count == before_count
        after_categories = [table.get_row_at(i)[1] for i in range(table.row_count)]
        assert before_categories != after_categories, "no item changed category"


async def test_menu_price_filter_needs_a_category(app):
    """The price range only reaches Redis when a category names the partition.

    `MenuItem.price` is a SortedField partitioned by the `category` KeyField, so
    an un-partitioned range query has no single sorted set to scan. The screen
    falls back to an in-memory filter and says so; with a category selected it
    queries the sorted index. Neither path may raise.
    """
    async with app.run_test() as pilot:
        await open_tab(app, pilot, "menu")
        screen = app.query_one(MenuScreen)

        screen.query_one("#filter-min-price", Input).value = "5"
        screen.query_one("#filter-max-price", Input).value = "50"

        await pilot.click(screen.query_one("#btn-filter", Button))
        await pilot.pause()
        assert_no_errors(app, "price filter without category")
        assert "in memory" in label_text(screen, "#result-count")

        screen.query_one("#filter-category", Select).value = "Main"
        await pilot.click(screen.query_one("#btn-filter", Button))
        await pilot.pause()
        assert_no_errors(app, "price filter with category")
        label = label_text(screen, "#result-count")
        assert "from Redis" in label, label
        table = app.query_one("#menu-table", DataTable)
        assert table.row_count > 0
        assert all(table.get_row_at(i)[1] == "Main" for i in range(table.row_count))


async def test_restaurant_filters_and_clear(app, seeded_kitchen):
    """Filter and clear round-trip, covering the Select.NULL read path."""
    async with app.run_test() as pilot:
        await open_tab(app, pilot, "restaurants")
        screen = app.query_one(RestaurantsScreen)
        table = app.query_one("#restaurants-table", DataTable)

        # No selection: Select.NULL must read as "no filter", not as a value.
        await pilot.click(screen.query_one("#btn-filter", Button))
        await pilot.pause()
        assert_no_errors(app, "filter with empty selects")
        assert table.row_count == seeded_kitchen["num_restaurants"]

        await pilot.click(screen.query_one("#btn-clear-filter", Button))
        await pilot.pause()
        assert_no_errors(app, "clear filters")
        assert table.row_count == seeded_kitchen["num_restaurants"]
        assert "restaurants" in label_text(screen, "#result-count")
