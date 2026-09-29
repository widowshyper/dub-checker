"""Air status from AniList, from Sonarr, or switched off (Settings > General > Air status)."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import Config
from dubchecker.models import AirInfo, AniListMatch, Category, FileResult, FileStatus, ShowGroup, ShowResult
from dubchecker.results_store import SavedScan, load_scan, save_scan
from dubchecker.sonarr import SonarrAirStatus, SonarrClient, SonarrSeries
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch
from tests.test_gui import tk_available
from tests.test_sonarr import API_KEY, CALENDAR_QUERIES, MockSonarr

FUTURE = "2099-01-07T15:00:00Z"
FUTURE_TS = int(datetime(2099, 1, 7, 15, 0, tzinfo=timezone.utc).timestamp())


def series(sid: int, folder: str, status: str, seasons: list | None = None, title: str = "") -> SonarrSeries:
    return SonarrSeries(sid, title or folder, None, f"/tv/{folder}", "anime", status, seasons or [])


class SonarrAirRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.air = SonarrAirStatus(
            [series(1, "Long Show", "continuing", [
                {"seasonNumber": 1, "statistics": {"previousAiring": "2020-01-01T00:00:00Z"}},
                {"seasonNumber": 2, "statistics": {"previousAiring": "2026-09-01T00:00:00Z"}},
                {"seasonNumber": 3, "statistics": {}},
                {"seasonNumber": 4, "statistics": {"previousAiring": "2026-01-01T00:00:00Z",
                                                   "nextAiring": "2099-06-01T00:00:00Z"}}]),
             series(2, "Done Show", "ended"),
             series(3, "Soon Show", "upcoming"),
             series(4, "Folder Name (2020)", "continuing", title="Real Title"),
             series(5, "Paused Show", "continuing")],
            [{"seriesId": 1, "seasonNumber": 2, "episodeNumber": 11, "airDateUtc": FUTURE},
             {"seriesId": 1, "seasonNumber": 2, "episodeNumber": 12, "airDateUtc": "2099-01-14T15:00:00Z"},
             {"seriesId": 1, "seasonNumber": 3, "episodeNumber": 1, "airDateUtc": "2099-03-01T15:00:00Z"},
             {"seriesId": 3, "seasonNumber": 1, "episodeNumber": 1, "airDateUtc": FUTURE},
             {"airDateUtc": "not a date", "seriesId": 1}])

    def status(self, folder: str, season: int | None, title: str = "") -> tuple:
        info = self.air.air(folder, title or folder, season)
        return (info.status, info.next_episode, info.next_airing_at) if info else None

    def test_airing_season_gets_its_next_episode(self) -> None:
        self.assertEqual(self.status("Long Show", 2), ("RELEASING", 11, FUTURE_TS))

    def test_earlier_seasons_of_a_continuing_show_are_finished(self) -> None:
        self.assertEqual(self.status("Long Show", 1), ("FINISHED", None, None))

    def test_a_season_that_has_not_started_yet(self) -> None:
        self.assertEqual(self.status("Long Show", 3)[:2], ("NOT_YET_RELEASED", 1))
        self.assertEqual(self.status("Soon Show", 1)[:2], ("NOT_YET_RELEASED", 1))

    def test_next_airing_beyond_the_calendar_window(self) -> None:
        self.assertEqual(self.status("Long Show", 4)[:2], ("RELEASING", None))

    def test_ended_and_between_seasons(self) -> None:
        self.assertEqual(self.status("Done Show", 1)[0], "FINISHED")
        self.assertEqual(self.status("Paused Show", None)[0], "HIATUS")  # continuing, nothing scheduled
        self.assertEqual(self.status("Long Show", None)[:2], ("RELEASING", 11))  # no season info: whole series

    def test_matched_by_folder_then_title(self) -> None:
        self.assertIsNotNone(self.air.air("folder name (2020)", "whatever", 1))
        self.assertIsNotNone(self.air.air("My Own Folder", "Real Title", 1))
        self.assertIsNone(self.air.air("Not In Sonarr", "Not In Sonarr", 1))
        self.assertEqual(self.air.air("Long Show", "", 2).source, "Sonarr")

    def test_wording_without_an_episode_number(self) -> None:
        r = ShowResult(ShowGroup("x", "x", "x"), [], Category.NEEDS_DUB, air=AirInfo("RELEASING", None, FUTURE_TS))
        self.assertTrue(ui.air_status_text(r, now=0).startswith("Airing - next episode on "))


class PipelineSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockSonarr)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/sonarr"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.lib, self.data = base / "Anime", base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")
        # Folder names as Sonarr has them, so a local files scan can be matched to Sonarr's series.
        touch(self.lib / "Attack on Titan (2013)" / "Season 01" / "e1 [dual].mkv")
        touch(self.lib / "Attack on Titan (2013)" / "Season 02" / "e1 [jpn].mkv")
        touch(self.lib / "Not In Sonarr" / "Season 01" / "e1 [dual].mkv")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def scan(self, source: str, air_client=None):
        log: list = []
        lookup = FakeLookup(log, air_status="RELEASING")
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber(log), lookup,
                                    config=Config(probe_workers=1, air_status_source=source))
        outcome = pipeline.run(root=str(self.lib), air_client=air_client)
        return outcome, [key for kind, key in log if kind == "lookup"], {r.key: r for r in outcome.results}

    def test_from_sonarr_after_a_local_files_scan(self) -> None:
        CALENDAR_QUERIES.clear()
        outcome, lookups, results = self.scan("sonarr", SonarrClient(self.url, API_KEY))
        self.assertEqual(lookups, ["Attack on Titan (2013)|S2"])  # no extra AniList lookups for air status
        s1, s2 = results["Attack on Titan (2013)|S1"], results["Attack on Titan (2013)|S2"]
        self.assertEqual((s2.air.status, s2.air.next_episode, s2.air.source), ("RELEASING", 9, "Sonarr"))
        self.assertEqual(s1.air.status, "FINISHED")
        self.assertIsNone(results["Not In Sonarr|S1"].air)
        self.assertEqual(outcome.warnings, [])
        self.assertEqual(CALENDAR_QUERIES[0]["unmonitored"], ["true"])

    def test_from_sonarr_without_sonarr_set_up(self) -> None:
        outcome, _, results = self.scan("sonarr", None)
        self.assertTrue(all(r.air is None for r in results.values()))
        self.assertIn("isn't set up", outcome.warnings[0])

    def test_from_sonarr_when_sonarr_rejects_the_key(self) -> None:
        outcome, _, _ = self.scan("sonarr", SonarrClient(self.url, "wrong"))
        self.assertIn("Couldn't get air status from Sonarr", outcome.warnings[0])

    def test_from_anilist(self) -> None:
        _, lookups, results = self.scan("anilist")
        self.assertEqual(len(lookups), 3)  # the looked-up season, then the two decided from files
        self.assertTrue(all(r.air and r.air.source == "AniList" for r in results.values()))

    def test_off(self) -> None:
        _, lookups, results = self.scan("off", SonarrClient(self.url, API_KEY))
        self.assertEqual(lookups, ["Attack on Titan (2013)|S2"])
        self.assertTrue(all(r.air is None for r in results.values()))


class SavedAirStatusTests(unittest.TestCase):
    def test_round_trip_and_older_saves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_scan.json"
            r = ShowResult(ShowGroup("S|S1", "S", "S", season=1), [FileResult("a.mkv", status=FileStatus.DUAL)],
                           Category.FULLY_DUBBED, air=AirInfo("RELEASING", 3, FUTURE_TS, "Sonarr"))
            save_scan(path, SavedScan([r]))
            self.assertEqual(load_scan(path).results[0].air, r.air)
            # 2.3.0 kept air status inside the AniList match
            data = json.loads(path.read_text("utf-8"))
            del data["results"][0]["air"]
            data["results"][0]["match"] = {"anilist_id": 1, "title": "S", "air_status": "FINISHED"}
            path.write_text(json.dumps(data), "utf-8")
            self.assertEqual(load_scan(path).results[0].air, AirInfo("FINISHED", None, None, "AniList"))


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
        self.app.model.add(ShowResult(ShowGroup("S|S1", "S", "S", season=1), [], Category.NEEDS_DUB,
                                      match=AniListMatch(1, "S"), air=AirInfo("RELEASING", 2, FUTURE_TS)))
        self.app.refresh_table()

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def columns(self) -> tuple:
        return tuple(self.app.tree.cget("displaycolumns"))

    def test_off_hides_the_air_status_column_and_airing_tab(self) -> None:
        from dubchecker.dialogs import SettingsDialog
        self.assertIn("air", self.columns())
        self.assertEqual(self.app.cards["AIRING"].winfo_manager(), "grid")
        dialog = SettingsDialog(self.app, 0)
        dialog.air_source.set("off")
        self.assertIn("hidden", dialog.air_hint.get())
        dialog._save()
        self.assertEqual(self.ctx.config.air_status_source, "off")
        self.assertNotIn("air", self.columns())
        self.assertEqual(self.app.cards["AIRING"].winfo_manager(), "")
        dialog = SettingsDialog(self.app, 0)
        dialog.air_source.set("sonarr")
        self.assertIn("Sonarr tab first", dialog.air_hint.get())  # Sonarr isn't set up in this test
        dialog._save()
        self.assertIn("air", self.columns())
        self.assertEqual(self.app.cards["AIRING"].winfo_manager(), "grid")


if __name__ == "__main__":
    unittest.main()
