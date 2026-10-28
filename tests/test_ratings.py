"""Each show's rating: Sonarr's for the series, else AniList's average score for the season."""
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from dubchecker import ui_common as ui
from dubchecker.anilist import to_match
from dubchecker.cache import Cache
from dubchecker.config import Config
from dubchecker.models import (AniListMatch, Category, FileResult, FileStatus, Rating, ShowGroup, ShowResult,
                               rating_from_match)
from dubchecker.results_store import SavedScan, load_scan, save_scan
from dubchecker.sonarr import SonarrClient
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch
from tests.test_sonarr import API_KEY, MockSonarr


def season(key: str, rating: Rating | None = None, score: int | None = None) -> ShowResult:
    match = AniListMatch(1, key, score=score) if score is not None else None
    return ShowResult(ShowGroup(key, key, key), [FileResult("a.mkv", status=FileStatus.DUAL)], Category.FULLY_DUBBED,
                      match=match, rating=rating)


class RatingTextTests(unittest.TestCase):
    def test_sonarr_first_then_anilist(self) -> None:
        self.assertEqual(ui.rating_of(season("a", Rating(8.6, "Sonarr", 10), score=70)).source, "Sonarr")
        self.assertEqual(ui.rating_of(season("a", score=73)), Rating(7.3, "AniList"))
        self.assertIsNone(ui.rating_of(season("a")))
        self.assertIsNone(rating_from_match(AniListMatch(1, "x", score=0)))  # AniList's "no score yet"

    def test_a_whole_show(self) -> None:
        self.assertAlmostEqual(ui.show_rating([season("1", score=80), season("2", score=70), season("3")]), 7.5)
        sonarr = Rating(8.6, "Sonarr", 10)
        self.assertEqual(ui.show_rating([season("1", sonarr, score=50), season("2", score=90)]), 8.6)
        self.assertIsNone(ui.show_rating([season("1")]))

    def test_wording(self) -> None:
        self.assertEqual(ui.rating_text(8.64), "8.6")
        self.assertEqual(ui.rating_text(None), "-")
        self.assertEqual(ui.rating_detail(season("a", Rating(8.6, "Sonarr", 1234))),
                         "8.6 out of 10, from Sonarr (1,234 votes)")
        self.assertEqual(ui.rating_detail(season("a", score=73)), "7.3 out of 10, from AniList's average score")
        self.assertTrue(ui.rating_detail(season("a")).startswith("No rating"))

    def test_anilist_score_is_read(self) -> None:
        self.assertEqual(to_match({"id": 5, "title": {"romaji": "X"}, "averageScore": 81}, 90).score, 81)
        self.assertIsNone(to_match({"id": 5, "title": {"romaji": "X"}, "averageScore": None}, 90).score)

    def test_saved_with_the_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_scan.json"
            save_scan(path, SavedScan([season("a", Rating(8.6, "Sonarr", 1234), score=70)]))
            loaded = load_scan(path).results[0]
            self.assertEqual((loaded.rating, loaded.match.score), (Rating(8.6, "Sonarr", 1234), 70))
            data = json.loads(path.read_text("utf-8"))  # saved before ratings
            del data["results"][0]["rating"]
            path.write_text(json.dumps(data), "utf-8")
            self.assertIsNone(load_scan(path).results[0].rating)


class RatingScanTests(unittest.TestCase):
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
        touch(self.lib / "Attack on Titan (2013)" / "Season 01" / "e1 [dual].mkv")
        touch(self.lib / "Attack on Titan (2013)" / "Season 02" / "e1 [jpn].mkv")
        touch(self.lib / "Not In Sonarr" / "Season 01" / "e1 [dual].mkv")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def scan(self, sonarr: SonarrClient | None, **settings):
        log: list = []
        config = Config(probe_workers=1, **{"air_status_source": "off", **settings})
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber(log), FakeLookup(log, score=77), config=config)
        outcome = pipeline.run(root=str(self.lib), dates_client=sonarr)
        return [key for kind, key in log if kind == "lookup"], {r.key: r for r in outcome.results}

    def test_sonarr_series_ratings(self) -> None:
        series = {s.title: s for s in SonarrClient(self.url, API_KEY).series()}
        self.assertEqual((series["Attack on Titan"].rating, series["Attack on Titan"].votes), (8.6, 1234))
        self.assertIsNone(series["Frieren"].rating)  # no votes yet
        self.assertIsNone(series["Friends"].rating)  # Sonarr didn't say

    def test_from_sonarr_with_anilist_for_the_rest(self) -> None:
        lookups, results = self.scan(SonarrClient(self.url, API_KEY))
        for key in ("Attack on Titan (2013)|S1", "Attack on Titan (2013)|S2"):  # a local files scan, matched by folder
            self.assertEqual(results[key].rating, Rating(8.6, "Sonarr", 1234))
        # Decided from its files and not in Sonarr: looked up on AniList for its score. Its group stays.
        self.assertEqual(lookups, ["Attack on Titan (2013)|S2", "Not In Sonarr|S1"])
        other = results["Not In Sonarr|S1"]
        self.assertEqual((ui.rating_of(other), other.category), (Rating(7.7, "AniList"), Category.FULLY_DUBBED))
        self.assertIsNone(other.air)  # air status is off

    def test_without_sonarr(self) -> None:
        lookups, results = self.scan(None)
        self.assertEqual(len(lookups), 3)
        self.assertTrue(all(ui.rating_of(r) == Rating(7.7, "AniList") for r in results.values()))

    def test_anilist_ratings_off(self) -> None:
        lookups, results = self.scan(None, anilist_ratings=False)
        self.assertEqual(lookups, ["Attack on Titan (2013)|S2"])  # only the season that needed AniList anyway
        self.assertIsNone(ui.rating_of(results["Not In Sonarr|S1"]))

    def test_no_second_lookup_with_air_status_from_anilist(self) -> None:
        lookups, results = self.scan(SonarrClient(self.url, API_KEY), air_status_source="anilist")
        self.assertEqual(len(lookups), 3)  # every season once: the air status step already found their scores
        self.assertEqual(results["Attack on Titan (2013)|S1"].rating.source, "Sonarr")


if __name__ == "__main__":
    unittest.main()
