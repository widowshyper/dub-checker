"""Organising the list: moving tabs, the tab tick boxes, the Air status filter and dismissed shows."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import Config, Dismissed
from tests.test_gui import tk_available
from tests.test_tabs_and_airing import ModelTests


class TabOrderTests(unittest.TestCase):
    def test_saved_order_is_kept_and_new_tabs_are_added(self) -> None:
        self.assertEqual(ui.ordered_tabs([]), list(ui.TAB_IDS))
        self.assertEqual(ui.ordered_tabs(["AIRING", "CHECK", "nonsense", "CHECK"]),
                         ["AIRING", "CHECK", "NEEDS_DUB", "NO_DUB", "FULLY_DUBBED"])


class AirFilterModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ModelTests()
        self.model.setUp()

    def titles(self, air_filter: str) -> list[str]:
        return [row.title for row in self.model.model.rows(set(), air_filter=air_filter)]

    def test_filters_seasons_by_air_status(self) -> None:
        self.assertEqual(self.titles("all"), ["Alpha", "Beta", "Delta", "Gamma"])
        self.assertEqual(self.titles("FINISHED"), ["Alpha", "Beta"])
        self.assertEqual(self.titles("RELEASING"), ["Alpha"])
        self.assertEqual(self.titles("NONE"), ["Delta"])  # never checked
        alpha = self.model.model.rows(set(), air_filter="RELEASING")[0]
        self.assertEqual([s.group.season for s in alpha.seasons], [2])

    def test_config_keeps_only_known_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"air_filter": "RELEASING", "tab_order": ["CHECK"]}), "utf-8")
            config = Config.load(path)
            self.assertEqual((config.air_filter, config.tab_order), ("RELEASING", ["CHECK"]))
            path.write_text(json.dumps({"air_filter": "sometimes"}), "utf-8")
            self.assertEqual(Config.load(path).air_filter, "all")


class DismissedStoreTests(unittest.TestCase):
    def test_saved_and_read_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dismissed.json"
            store = Dismissed(path)
            store.add("Beta", "Beta", 100.0)
            store.add("Alpha (2020)", "Alpha", 200.0)
            again = Dismissed(path)
            self.assertEqual([folder for folder, _ in again.items()], ["Alpha (2020)", "Beta"])  # newest first
            again.remove(["Beta", "not there"])
            self.assertEqual(Dismissed(path).keys(), frozenset({"Alpha (2020)"}))
            path.write_text("not json", "utf-8")
            self.assertEqual(len(Dismissed(path)), 0)

    def test_dismissed_shows_leave_the_tabs_and_counts(self) -> None:
        model = ModelTests()
        model.setUp()
        model.model.dismissed = frozenset({"Alpha"})
        self.assertEqual([r.title for r in model.model.rows(set())], ["Beta", "Delta", "Gamma"])
        counts = model.model.counts()
        self.assertEqual((counts["NEEDS_DUB"], counts["AIRING"], counts["FULLY_DUBBED"]), (1, 1, 0))


@unittest.skipUnless(tk_available(), "no display for Tk")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.config import Overrides
        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        self.ctx = AppContext(Config(theme="light"), data / "config.json", Overrides(data / "o.json"), self.cache,
                              data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.ctx)
        self.fill(self.app)

    @staticmethod
    def fill(app) -> None:
        model = ModelTests()
        model.setUp()
        for r in model.model.results.values():
            app.model.add(r)
        app.update_cards()
        app.refresh_table()

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def saved(self) -> dict:
        return json.loads(self.ctx.config_path.read_text("utf-8"))

    def listed(self) -> list[str]:
        """The shows listed, by folder: rows are "p|<show>" or, for one-season shows, "s|<show>|S1"."""
        return [iid.split("|")[1] for iid in self.app.tree.get_children()]

    def checks_order(self) -> list[str]:
        return [str(w.cget("text")) for w in self.app.tab_checks_frame.pack_slaves()]

    # ---------------------------------------------------------------- moving tabs

    def test_move_left_and_right_is_remembered(self) -> None:
        app = self.app
        app.move_tab("AIRING", -1)
        self.assertEqual(app.visible_tabs(), ["NEEDS_DUB", "NO_DUB", "FULLY_DUBBED", "AIRING", "CHECK"])
        self.assertEqual(int(app.cards["AIRING"].grid_info()["column"]), 3)
        self.assertEqual(self.checks_order()[3], "Airing")  # the tick boxes follow
        app.move_tab("NEEDS_DUB", 1)
        self.assertEqual(self.saved()["tab_order"][:2], ["NO_DUB", "NEEDS_DUB"])
        app.move_tab("NO_DUB", -1)  # already first: nothing happens
        self.assertEqual(app.visible_tabs()[0], "NO_DUB")

    def fake_positions(self) -> None:
        """The window is hidden in tests, so give each card a place on screen: 200 px wide, side by side."""
        for column, tab in enumerate(self.app.visible_tabs()):
            card = self.app.cards[tab]
            card.winfo_rootx = lambda c=column: c * 200
            card.winfo_width = lambda: 200

    def test_dragging_a_tab_moves_it_instead_of_selecting_it(self) -> None:
        app = self.app
        self.fake_positions()
        app.card_press("CHECK", SimpleNamespace(x_root=700))
        app.card_drag("CHECK", SimpleNamespace(x_root=705))  # a wobble, not a drag
        self.assertFalse(app.cards["CHECK"].dragging)
        app.card_drag("CHECK", SimpleNamespace(x_root=150))  # over the first tab's right half
        self.assertTrue(app.cards["CHECK"].dragging)
        app.card_release("CHECK", SimpleNamespace(x_root=150))
        self.assertEqual(app.visible_tabs(), ["NEEDS_DUB", "CHECK", "NO_DUB", "FULLY_DUBBED", "AIRING"])
        self.assertEqual(app.tabs, {"NEEDS_DUB"})  # not selected by the drag
        self.assertFalse(app.cards["CHECK"].dragging)
        self.assertEqual(self.saved()["tab_order"][:2], ["NEEDS_DUB", "CHECK"])

    def test_a_click_still_selects(self) -> None:
        app = self.app
        app.card_press("CHECK", SimpleNamespace(x_root=700))
        app.card_release("CHECK", SimpleNamespace(x_root=702))
        self.assertEqual(app.tabs, {"NEEDS_DUB", "CHECK"})

    def test_hidden_tabs_keep_their_place_when_others_move(self) -> None:
        app = self.app
        app.set_tab_visible("NO_DUB", False)
        app.move_tab("FULLY_DUBBED", -1)
        self.assertEqual(ui.ordered_tabs(self.ctx.config.tab_order),
                         ["FULLY_DUBBED", "NO_DUB", "NEEDS_DUB", "CHECK", "AIRING"])

    # ---------------------------------------------------------------- tick boxes

    def test_tick_boxes_hide_and_show_tabs(self) -> None:
        app = self.app
        self.assertEqual(self.checks_order(), [ui.TABS[t].title for t in ui.TAB_IDS])
        app.tab_visible_vars["CHECK"].set(False)
        app.tab_checks["CHECK"].invoke()  # ticks it again, as a click would
        self.assertEqual(app.cards["CHECK"].winfo_manager(), "grid")
        app.tab_checks["CHECK"].invoke()
        self.assertEqual(app.cards["CHECK"].winfo_manager(), "")
        self.assertEqual(self.saved()["hidden_tabs"], ["CHECK"])
        for tab in ui.TAB_IDS:
            app.set_tab_visible(tab, False)
        self.assertTrue(app.tab_checks_frame.winfo_manager())  # the boxes stay, so tabs can come back

    def test_airing_tick_box_goes_when_air_status_is_off(self) -> None:
        self.ctx.config.air_status_source = "off"
        self.app.settings_saved()
        self.assertNotIn("Airing", self.checks_order())
        self.assertEqual(self.app.air_filter_frame.winfo_manager(), "")

    # ---------------------------------------------------------------- air status filter

    def choose_air(self, key: str) -> None:
        self.app.air_filter_box.set(ui.AIR_FILTER_LABELS[key])
        self.app._on_air_filter()

    def test_air_status_filter(self) -> None:
        app = self.app
        app.select_tabs(set())
        self.choose_air("FINISHED")
        self.assertEqual(self.listed(), ["Alpha", "Beta"])
        self.assertIn("Finished", app.group_description.cget("text"))
        self.assertEqual(self.saved()["air_filter"], "FINISHED")
        app.select_tabs({"CHECK"})
        self.assertIn("No shows here are “Finished”", app.empty_text)
        self.choose_air("all")
        self.assertEqual(self.listed(), ["Gamma"])

    def test_air_status_filter_is_ignored_when_air_status_is_off(self) -> None:
        self.choose_air("RELEASING")
        self.ctx.config.air_status_source = "off"
        self.app.settings_saved()
        self.app.select_tabs(set())
        self.assertEqual(len(self.listed()), 4)

    # ---------------------------------------------------------------- dismissed shows

    def test_dismiss_and_bring_back(self) -> None:
        from dubchecker.gui import App
        app = self.app
        self.assertEqual(app.dismissed_button.cget("text"), "Dismissed shows")
        app.select_tabs(set())
        app.dismiss_show("Alpha", "Alpha")
        self.assertNotIn("Alpha", self.listed())
        self.assertEqual(app.cards["NEEDS_DUB"].count.cget("text"), "1")
        self.assertEqual(app.dismissed_button.cget("text"), "Dismissed shows (1)")
        self.assertIn("Dismissed Alpha", app.status_var.get())

        # Still dismissed next time, even with a fresh scan's results.
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
            again = App(root, self.ctx.__class__(self.ctx.config, self.ctx.config_path, self.ctx.overrides,
                                                 self.cache, self.ctx.data_dir))
            self.fill(again)
            again.select_tabs(set())
            self.assertNotIn("p|Alpha", again.tree.get_children())
            again.restore_shows(["Alpha"])
            self.assertIn("p|Alpha", again.tree.get_children())
            self.assertEqual(again.dismissed_button.cget("text"), "Dismissed shows")
            self.assertEqual(len(again.dismissed), 0)
            again.stop_polling()
        finally:
            root.destroy()

    def test_dismissed_shows_window(self) -> None:
        from dubchecker.dialogs import DismissedDialog
        app = self.app
        self.ctx.dismissed.add("Alpha", "Alpha", 100.0)
        self.ctx.dismissed.add("Gone Show", "Gone Show", 200.0)  # not in these results
        app._dismissed_changed()
        dialog = DismissedDialog(app)
        try:
            tree = dialog.tree
            self.assertEqual(tree.get_children(), ("Gone Show", "Alpha"))  # newest first
            self.assertEqual(tree.set("Alpha", "where"), "Fully Dubbed, Needs English Audio")
            self.assertEqual(tree.set("Gone Show", "where"), "Not in the current results")
            self.assertIn("disabled", dialog.restore_button.state())
            tree.selection_set("Alpha")
            dialog.update()
            dialog.restore_button.invoke()
            self.assertEqual(tree.get_children(), ("Gone Show",))
            dialog.restore_all_button.invoke()
            self.assertEqual(tree.get_children(), ())
            self.assertTrue(dialog.empty.winfo_manager())
        finally:
            dialog.close()

    def test_right_click_offers_dismiss(self) -> None:
        app = self.app
        app.select_tabs(set())
        row = app.tree.get_children()[0]
        app.tree.see(row)
        menus = []
        with mock.patch("tkinter.Menu.tk_popup", lambda menu, *_: menus.append(menu)), \
                mock.patch.object(app.tree, "identify_row", return_value=row):
            app._on_right_click(SimpleNamespace(y=1, x_root=0, y_root=0))
        menu = menus[0]
        menu.invoke(next(i for i in range(menu.index("end") + 1) if menu.type(i) == "command"
                         and menu.entrycget(i, "label") == "Dismiss this show (hide it from every tab)"))
        self.assertIn("Alpha", self.ctx.dismissed)

    def test_export_leaves_out_dismissed_shows(self) -> None:
        app = self.app
        app.select_tabs(set())
        app.dismiss_show("Beta", "Beta")
        target = self.ctx.data_dir / "all.csv"
        with mock.patch("dubchecker.gui.filedialog.asksaveasfilename", return_value=str(target)):
            app.export_csv()
        self.assertNotIn("Beta", target.read_text("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()
