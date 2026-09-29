"""Tabs you can combine and hide, the Airing tab, and air status from AniList."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.anilist import AniListClient, to_match
from dubchecker.cache import Cache
from dubchecker.config import Config
from dubchecker.models import AirInfo, AniListMatch, Category, FileResult, FileStatus, ShowGroup, ShowResult
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch
from tests.test_anilist import FakeResponse, FakeSession, media
from tests.test_gui import tk_available

NOW = 1_790_000_000  # a fixed "now" for the date wording


def season(show: str, number: int, category: Category, air: str = "", next_at: int | None = None) -> ShowResult:
    match = AniListMatch(number, show, air_status=air, next_episode=8 if next_at else None, next_airing_at=next_at)
    files = [FileResult(f"/lib/{show}/{number}.mkv", status=FileStatus.ORIGINAL_ONLY)]
    air = AirInfo(air, match.next_episode, next_at, "AniList") if air else None
    return ShowResult(ShowGroup(f"{show}|S{number}", show, show, season=number), files, category, match=match,
                      air=air)


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ui.ResultsModel()
        for r in (season("Alpha", 1, Category.FULLY_DUBBED, "FINISHED"),
                  season("Alpha", 2, Category.NEEDS_DUB, "RELEASING", NOW + 3600),
                  season("Beta", 1, Category.NO_DUB, "FINISHED"),
                  season("Gamma", 1, Category.CHECK, "NOT_YET_RELEASED"),
                  season("Delta", 1, Category.NEEDS_DUB)):
            self.model.add(r)

    def titles(self, tabs: set[str]) -> list[str]:
        return [row.title for row in self.model.rows(tabs)]

    def test_no_tab_lists_everything(self) -> None:
        self.assertEqual(self.titles(set()), ["Alpha", "Beta", "Delta", "Gamma"])

    def test_several_tabs_list_together(self) -> None:
        self.assertEqual(self.titles({"NEEDS_DUB", "NO_DUB"}), ["Alpha", "Beta", "Delta"])
        alpha = self.model.rows({"NEEDS_DUB", "FULLY_DUBBED"})[0]
        self.assertEqual(len(alpha.seasons), 2)
        self.assertEqual(alpha.also_text(), "")

    def test_airing_tab_cuts_across_groups(self) -> None:
        self.assertEqual(self.titles({ui.AIRING}), ["Alpha", "Gamma"])
        alpha = self.model.rows({ui.AIRING})[0]
        self.assertEqual([s.group.season for s in alpha.seasons], [2])
        self.assertEqual(alpha.also_text(), "Also: Season 1 is in Fully Dubbed")

    def test_counts_include_airing(self) -> None:
        counts = self.model.counts()
        self.assertEqual((counts["NEEDS_DUB"], counts["AIRING"], counts["CHECK"]), (2, 2, 1))

    def test_air_status_wording(self) -> None:
        with mock.patch("dubchecker.ui_common.time.localtime", return_value=time.gmtime(NOW + 3600)):
            airing = ui.air_status_text(season("A", 1, Category.NEEDS_DUB, "RELEASING", NOW + 3600), now=NOW)
            aired = ui.air_status_text(season("A", 1, Category.NEEDS_DUB, "RELEASING", NOW + 3600), now=NOW + 7200)
        day = time.gmtime(NOW + 3600)
        expected_date = f"{day.tm_mday} {time.strftime('%b', day)}"
        self.assertEqual(airing, f"Airing - ep 8 on {expected_date}")
        self.assertEqual(aired, f"Airing - ep 8 aired {expected_date}")
        self.assertEqual(ui.air_status_text(season("A", 1, Category.NO_DUB, "FINISHED")), "Finished")
        self.assertEqual(ui.air_status_text(season("A", 1, Category.NO_DUB, "HIATUS")), "On hiatus")
        self.assertEqual(ui.air_status_text(season("A", 1, Category.NO_DUB)), "-")

    def test_air_sort_and_which_season_speaks_for_the_show(self) -> None:
        keys = [ui.air_sort_key(season("X", 1, Category.NO_DUB, s)) for s in
                ("FINISHED", "RELEASING", "", "NOT_YET_RELEASED")]
        self.assertEqual(sorted(keys), [keys[1], keys[3], keys[0], keys[2]])
        alpha = self.model.by_show()["Alpha"]
        self.assertEqual(ui.show_air_status(alpha).group.season, 2)  # the airing one
        self.assertIsNone(ui.show_air_status(self.model.by_show()["Delta"]))


class AniListAirStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self._tmp.name) / "cache.db")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def test_match_carries_air_status(self) -> None:
        item = dict(media(5, "Show"), status="RELEASING", nextAiringEpisode={"airingAt": NOW, "episode": 9})
        match = to_match(item, 95)
        self.assertEqual((match.air_status, match.next_episode, match.next_airing_at), ("RELEASING", 9, NOW))
        self.assertTrue(match.still_airing)
        self.assertFalse(to_match(dict(media(6, "Old"), status="FINISHED"), 95).still_airing)

    def details_after(self, saved: dict, age_hours: float) -> int:
        """How many requests details() makes when ``saved`` was cached ``age_hours`` ago."""
        self.cache.save_anilist("media:5", {"data": {"Media": saved}})
        with self.cache._lock:
            self.cache._conn.execute("UPDATE anilist SET fetched_at = ?", (time.time() - age_hours * 3600,))
        session = FakeSession([FakeResponse(200, {"data": {"Media": dict(saved, status="FINISHED")}})])
        AniListClient(self.cache, min_interval=0, session=session).details(5)
        return session.posts

    def test_finished_shows_are_remembered(self) -> None:
        self.assertEqual(self.details_after(dict(media(5, "S"), status="FINISHED"), age_hours=24 * 20), 0)

    def test_airing_shows_are_refreshed_after_12_hours(self) -> None:
        self.assertEqual(self.details_after(dict(media(5, "S"), status="RELEASING"), age_hours=2), 0)
        self.assertEqual(self.details_after(dict(media(5, "S"), status="RELEASING"), age_hours=13), 1)

    def test_answers_saved_before_air_status_are_refreshed(self) -> None:
        old = media(5, "S")  # no "status" field at all
        self.assertEqual(self.details_after(old, age_hours=24), 1)


class PipelineAirStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.lib, self.data = base / "Anime", base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")
        touch(self.lib / "Done Show" / "Season 01" / "e1 [dual].mkv")               # Fully Dubbed from files
        touch(self.lib / "Half Show" / "Season 01" / "e1 [dual].mkv")               # Needs English Audio from files
        touch(self.lib / "Half Show" / "Season 01" / "e2 [jpn].mkv")
        touch(self.lib / "Raw Show" / "Season 01" / "e1 [jpn].mkv")                 # needs an AniList lookup

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def scan(self, check: bool, cancel: threading.Event | None = None, on_lookup=None):
        log: list = []
        lookup = FakeLookup(log, dub=True, air_status="RELEASING", on_lookup=on_lookup)
        pipeline, events = make_pipeline(self.data, self.cache, FakeProber(log), lookup,
                                         config=Config(probe_workers=1, air_status_source="anilist" if check else "off"),
                                         cancel=cancel)
        outcome = pipeline.run(root=str(self.lib))
        return outcome, [key for kind, key in log if kind == "lookup"], {r.key: r for r in outcome.results}

    def test_seasons_decided_from_files_get_air_status_last(self) -> None:
        outcome, lookups, results = self.scan(check=True)
        self.assertEqual(lookups, ["Raw Show|S1", "Half Show|S1", "Done Show|S1"])  # Needs English Audio first
        self.assertIs(results["Half Show|S1"].category, Category.NEEDS_DUB)       # group unchanged
        self.assertIs(results["Done Show|S1"].category, Category.FULLY_DUBBED)
        self.assertTrue(all(r.air and r.air.still_airing and r.air.source == "AniList" for r in results.values()))
        self.assertEqual(outcome.stats.lookups, 3)
        self.assertEqual(len(outcome.results), 3)

    def test_switched_off(self) -> None:
        _, lookups, results = self.scan(check=False)
        self.assertEqual(lookups, ["Raw Show|S1"])
        self.assertIsNone(results["Half Show|S1"].match)

    def test_stop_during_the_air_status_step_keeps_everything(self) -> None:
        cancel = threading.Event()
        outcome, lookups, results = self.scan(check=True, cancel=cancel,
                                              on_lookup=lambda g: g.key == "Half Show|S1" and cancel.set())
        self.assertTrue(outcome.stopped)
        self.assertEqual(len(results), 3)
        self.assertIs(results["Done Show|S1"].category, Category.FULLY_DUBBED)
        self.assertFalse(any(r.stopped for r in results.values()))  # nothing was left undecided


class ConfigTests(unittest.TestCase):
    def test_hidden_tabs_are_saved_as_a_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"hidden_tabs": ["NO_DUB", "AIRING"], "check_air_status": False}), "utf-8")
            config = Config.load(path)  # 2.3.0's switch turned off stays off
            self.assertEqual((config.hidden_tabs, config.air_status_source), (["NO_DUB", "AIRING"], "off"))
            path.write_text(json.dumps({"hidden_tabs": "not a list"}), "utf-8")
            self.assertEqual(Config.load(path).hidden_tabs, [])
            self.assertEqual(Config().air_status_source, "anilist")
            path.write_text(json.dumps({"air_status_source": "sonarr"}), "utf-8")
            self.assertEqual(Config.load(path).air_status_source, "sonarr")
            path.write_text(json.dumps({"air_status_source": "tvdb"}), "utf-8")
            self.assertEqual(Config.load(path).air_status_source, "anilist")


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
        model_tests = ModelTests()  # reuse its sample shows
        model_tests.setUp()
        for r in model_tests.model.results.values():
            self.app.model.add(r)
        self.app.update_cards()
        self.app.refresh_table()

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def shown(self) -> tuple:
        return self.app.tree.get_children()

    def test_click_toggles_tabs_and_several_combine(self) -> None:
        app = self.app
        self.assertEqual(app.tabs, {"NEEDS_DUB"})
        self.assertNotIn("group", app.tree.cget("displaycolumns"))
        app.toggle_tab("NO_DUB")
        self.assertEqual(app.tabs, {"NEEDS_DUB", "NO_DUB"})
        self.assertEqual(len(self.shown()), 3)
        self.assertIn("group", app.tree.cget("displaycolumns"))  # mixed groups: say which is which
        self.assertEqual(app.group_title.cget("text"), "Needs English Audio + No Dub Exists")
        self.assertTrue(app.cards["NO_DUB"].selected)
        app.toggle_tab("NEEDS_DUB")
        app.toggle_tab("NO_DUB")
        self.assertEqual(app.tabs, set())
        self.assertEqual(app.group_title.cget("text"), "All shows")
        self.assertEqual(len(self.shown()), 4)

    def test_airing_tab_and_air_status_column(self) -> None:
        app = self.app
        self.assertEqual(app.cards["AIRING"].count.cget("text"), "2")
        app.select_tabs({"AIRING"})
        shows = self.shown()
        self.assertEqual(len(shows), 2)
        self.assertTrue(app.tree.set(shows[0], "air").startswith("Airing - ep 8"))

    def test_hiding_tabs_is_remembered(self) -> None:
        app = self.app
        app.toggle_tab("NO_DUB")
        app.set_tab_visible("NO_DUB", False)
        self.assertEqual(app.cards["NO_DUB"].winfo_manager(), "")
        self.assertNotIn("NO_DUB", app.tabs)
        self.assertEqual(json.loads(self.ctx.config_path.read_text("utf-8"))["hidden_tabs"], ["NO_DUB"])
        for tab in ("NEEDS_DUB", "FULLY_DUBBED", "CHECK", "AIRING"):
            app.set_tab_visible(tab, False)
        self.assertEqual(app.cards_frame.winfo_manager(), "")  # nothing left to show: the whole row goes
        self.assertEqual(app.group_title.cget("text"), "All shows")
        app.show_all_tabs()
        self.assertEqual(app.cards_frame.winfo_manager(), "grid")
        self.assertTrue(all(card.winfo_manager() == "grid" for card in app.cards.values()))
        self.assertEqual(self.ctx.config.hidden_tabs, [])

    def test_export_has_each_seasons_own_group_and_air_status(self) -> None:
        self.app.select_tabs(set())
        target = self.ctx.data_dir / "all.csv"
        with mock.patch("dubchecker.gui.filedialog.asksaveasfilename", return_value=str(target)):
            self.app.export_csv()
        lines = target.read_text("utf-8-sig").splitlines()
        self.assertIn("Air status", lines[0])
        self.assertTrue(any(line.startswith("Fully Dubbed,Alpha") for line in lines))
        self.assertTrue(any(line.startswith("No Dub Exists,Beta") for line in lines))


if __name__ == "__main__":
    unittest.main()
