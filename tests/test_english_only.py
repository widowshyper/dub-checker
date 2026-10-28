"""Episodes with only English audio: their notes, the English Only tab and column, and replacing them."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.categorize import categorize_local
from dubchecker.config import Config, Overrides
from dubchecker.models import Category, FileResult, FileStatus, ShowGroup, ShowResult
from tests.test_gui import tk_available

D, O, E = FileStatus.DUAL, FileStatus.ORIGINAL_ONLY, FileStatus.ENGLISH_ONLY


def season(show: str, number: int, *statuses: FileStatus) -> ShowResult:
    files = [FileResult(f"/lib/{show}/S{number}E{n}.mkv", status=s) for n, s in enumerate(statuses, 1)]
    return categorize_local(ShowGroup(f"{show}|S{number}", show, show, season=number), files)


class NotesAndGroupTests(unittest.TestCase):
    def test_notes_say_how_many_episodes_are_english_only(self) -> None:
        self.assertIn("2 of 3 episodes only have English audio", season("A", 1, D, E, E).notes)
        self.assertIn("Every episode only has English audio", season("A", 1, E, E).notes)
        self.assertIn("The episode only has English audio", season("A", 1, E).notes)
        self.assertFalse(any("English audio" in n for n in season("A", 1, D, O).notes))

    def test_the_group_is_unchanged(self) -> None:
        # English-only episodes still have English, so they don't make a season Need English Audio.
        self.assertIs(season("A", 1, D, E).category, Category.FULLY_DUBBED)
        self.assertIs(season("A", 1, D, O, E).category, Category.NEEDS_DUB)
        self.assertEqual(season("A", 1, D, O, E, E).english_only, 2)


class TabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ui.ResultsModel()
        for r in (season("Alpha", 1, D, D), season("Alpha", 2, D, E),
                  season("Beta", 1, O, E), season("Gamma", 1, E, E), season("Delta", 1, O)):
            self.model.add(r)

    def test_tab_lists_seasons_with_english_only_episodes(self) -> None:
        rows = self.model.rows({ui.ENGLISH_ONLY})
        self.assertEqual([r.title for r in rows], ["Alpha", "Beta", "Gamma"])
        self.assertEqual([s.group.season for s in rows[0].seasons], [2])  # only the season that has them
        self.assertEqual(self.model.counts()[ui.ENGLISH_ONLY], 3)
        # It cuts across the groups: Beta is also in Needs English Audio.
        self.assertIn("Beta", [r.title for r in self.model.rows({"NEEDS_DUB"})])

    def test_english_only_file_names(self) -> None:
        self.assertEqual(ui.english_only_files(self.model.by_show()["Beta"]), ["S1E2.mkv"])
        self.assertEqual(ui.original_only_files(self.model.by_show()["Beta"]), ["S1E1.mkv"])

    def test_new_tab_is_added_for_people_with_a_saved_order(self) -> None:
        self.assertIn(ui.ENGLISH_ONLY, ui.ordered_tabs(["AIRING", "NEEDS_DUB", "NO_DUB", "FULLY_DUBBED", "CHECK"]))


@unittest.skipUnless(tk_available(), "no display for Tk")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        config = Config(theme="light", sonarr_url="http://nas:8989", sonarr_api_key="key")
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, AppContext(config, data / "config.json", Overrides(data / "o.json"), self.cache,
                                             data))
        for r in (season("Beta", 1, O, E, E), season("Gamma", 1, D, D)):
            self.app.model.add(r)
        self.app.update_cards()
        self.app.select_tabs({ui.ENGLISH_ONLY})

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def menu_labels(self, iid: str) -> list[str]:
        menus = []
        with mock.patch("tkinter.Menu.tk_popup", lambda menu, *_: menus.append(menu)), \
                mock.patch.object(self.app.tree, "identify_row", return_value=iid):
            self.app._on_right_click(SimpleNamespace(y=1, x_root=0, y_root=0))
        menu = menus[0]
        return [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) == "command"]

    def test_tab_card_column_and_group_column(self) -> None:
        app = self.app
        self.assertEqual(app.cards[ui.ENGLISH_ONLY].count.cget("text"), "1")
        self.assertEqual(app.tree.get_children(), ("s|Beta|S1",))
        self.assertEqual(str(app.tree.set("s|Beta|S1", "eng")), "2")
        self.assertIn("group", app.tree.cget("displaycolumns"))  # the tab cuts across groups: say which
        app.toggle("s|Beta|S1")
        episodes = app.tree.get_children("s|Beta|S1")
        self.assertEqual([app.tree.set(e, "eng") for e in episodes], ["", "✓", "✓"])
        self.assertIn("audio_ENGLISH_ONLY", app.tree.item(episodes[1], "tags"))

    def test_right_click_offers_both_replacement_searches(self) -> None:
        labels = self.menu_labels("s|Beta|S1")
        self.assertIn("Search for replacements in Sonarr...", labels)
        self.assertIn("Search for dual-audio replacements of the English-only episodes in Sonarr...", labels)
        self.app.toggle("s|Beta|S1")
        english_episode = self.app.tree.get_children("s|Beta|S1")[1]
        self.assertIn("Search for a dual-audio replacement in Sonarr...", self.menu_labels(english_episode))

    def test_searching_for_dual_audio_versions(self) -> None:
        import time
        beta = [self.app.model.results["Beta|S1"]]
        with mock.patch("dubchecker.gui.messagebox.askyesno", return_value=True) as ask, \
                mock.patch("dubchecker.sonarr_actions.search_for_replacements",
                           return_value="Sonarr is searching.") as search:
            self.app.search_sonarr(beta, english=True)
            for thread in self.app.helpers:
                thread.join(5)
            deadline = time.monotonic() + 5
            while "Sonarr is searching." not in self.app.status_var.get() and time.monotonic() < deadline:
                self.root.update()
        self.assertIn("2 episodes of Beta (Season 1) that only have English audio", ask.call_args.args[1])
        self.assertIn("prefer dual-audio releases", ask.call_args.args[1])
        self.assertEqual(search.call_args.args[4], ["S1E2.mkv", "S1E3.mkv"])

    def test_export_has_an_english_only_column(self) -> None:
        target = Path(self._tmp.name) / "list.csv"
        with mock.patch("dubchecker.gui.filedialog.asksaveasfilename", return_value=str(target)):
            self.app.export_csv()
        header, row = target.read_text("utf-8-sig").splitlines()[:2]
        columns = header.split(",")
        self.assertEqual(row.split(",")[columns.index("English only")], "2")


if __name__ == "__main__":
    unittest.main()
