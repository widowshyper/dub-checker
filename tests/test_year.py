"""The release year: each season's (AniList's for its match, else the folder's or Sonarr's) and each show's
(the earliest), shown in a sortable Year column."""
import tempfile
import unittest
from pathlib import Path

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import Config, Overrides
from dubchecker.models import AniListMatch, Category, FileResult, FileStatus, ShowGroup, ShowResult, season_key


def season(folder: str, number: int | None, folder_year: int | None = None, anilist_year: int | None = None,
           title: str = "") -> ShowResult:
    match = AniListMatch(1, title or folder, year=anilist_year) if anilist_year is not None else None
    return ShowResult(ShowGroup(season_key(folder, number), folder, title or folder, folder_year, number),
                      [FileResult("e.mkv", status=FileStatus.DUAL)], Category.FULLY_DUBBED, match=match)


class YearTests(unittest.TestCase):
    def test_a_seasons_year(self) -> None:
        self.assertEqual(ui.season_year(season("AoT", 2, folder_year=2013, anilist_year=2017)), 2017)
        self.assertEqual(ui.season_year(season("AoT", 2, folder_year=2013)), 2013)  # no AniList match
        self.assertIsNone(ui.season_year(season("Lain", 1)))
        self.assertEqual(ui.year_text(None), "-")

    def test_a_shows_year_is_when_it_first_came_out(self) -> None:
        # Only season 2 is in the library, but the folder says when the show started.
        self.assertEqual(ui.show_year([season("AoT", 2, folder_year=2013, anilist_year=2017)]), 2013)
        self.assertEqual(ui.show_year([season("X", 3, anilist_year=2022), season("X", 2, anilist_year=2020)]), 2020)
        self.assertIsNone(ui.show_year([season("Lain", 1)]))

    def test_search_by_year(self) -> None:
        model = ui.ResultsModel()
        for r in (season("AoT", 1, anilist_year=2013), season("AoT", 2, anilist_year=2017),
                  season("Lain", 1, anilist_year=1998)):
            model.add(r)
        self.assertEqual([row.title for row in model.rows(set(), "2017")], ["AoT"])
        self.assertEqual([row.title for row in model.rows(set(), "1998")], ["Lain"])
        self.assertEqual(model.rows(set(), "2001"), [])


def tk_available() -> bool:
    try:
        import tkinter
        tkinter.Tk().destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(tk_available(), "no display for Tk")
class YearColumnTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        self.addCleanup(self.cache.close)
        ctx = AppContext(Config(theme="light"), data / "config.json", Overrides(data / "overrides.json"), self.cache,
                         data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, ctx)
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.app.stop_polling)
        for r in (season("AoT", 1, 2013, 2013), season("AoT", 2, 2013, 2017), season("Lain", 1, anilist_year=1998),
                  season("Frieren", 1, anilist_year=2023), season("Unknown", 1)):
            self.app.model.add(r)
        self.app.expanded.add("AoT")
        self.app.select_tabs(set())  # list every show

    def shows(self) -> list[str]:
        return [self.app.tree.set(iid, "show").strip(" ▶▼►") for iid in self.app.tree.get_children()]

    def test_the_column(self) -> None:
        tree = self.app.tree
        self.assertIn("year", tree.cget("displaycolumns"))
        self.assertEqual(tree.set("p|AoT", "year"), "2013")         # the show: its first season
        self.assertEqual(tree.set("s|AoT|S2", "year"), "2017")      # season 2 aired later
        self.assertEqual(tree.set("s|Unknown|S1", "year"), "-")

    def test_sorting_puts_unknown_years_last(self) -> None:
        self.app.sort_by("year")  # newest first
        self.assertEqual([s.split(" - ")[0] for s in self.shows()], ["Frieren", "AoT", "Lain", "Unknown"])
        self.app.sort_by("year")  # oldest first; still unknown last
        self.assertEqual([s.split(" - ")[0] for s in self.shows()], ["Lain", "AoT", "Frieren", "Unknown"])


if __name__ == "__main__":
    unittest.main()
