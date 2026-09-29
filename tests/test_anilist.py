import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.anilist import (AniListClient, AniListError, AniListMatcher, english_cast_listed, has_season_marker,
                                normalise, ordinal, parse_anilist_id, rank, strip_season_marker, title_score,
                                year_adjust)
from dubchecker.cache import Cache
from dubchecker.models import Cancelled, ShowGroup


def media(media_id: int, romaji: str, english: str | None = None, year: int | None = None, popularity: int = 0,
          fmt: str = "TV", synonyms: list[str] | None = None, mal: int | None = None,
          relations: list[tuple[str, dict]] | None = None, voice_actors: bool | None = None) -> dict:
    data = {"id": media_id, "idMal": mal, "type": "ANIME", "title": {"romaji": romaji, "english": english,
            "native": None}, "synonyms": synonyms or [], "format": fmt, "episodes": 12, "seasonYear": year,
            "startDate": {"year": year, "month": 4, "day": 1}, "popularity": popularity}
    data["relations"] = {"edges": [{"relationType": kind, "node": node} for kind, node in relations or []]}
    edges = [] if voice_actors is None else [{"node": {"id": 1}, "voiceActors": [{"id": 9}] if voice_actors else []}]
    data["characters"] = {"edges": edges}
    return data


class FakeClient:
    """Stands in for AniListClient: search results by query, details by id."""

    def __init__(self, searches: dict[str, list[dict]], items: list[dict]) -> None:
        self.searches = searches
        self.items = {m["id"]: m for m in items}
        self.calls: list[tuple[str, object]] = []

    def search(self, text: str) -> list[dict]:
        self.calls.append(("search", text))
        return self.searches.get(text, [])

    def details(self, media_id: int) -> dict | None:
        self.calls.append(("details", media_id))
        return self.items.get(media_id)


def group(title: str, season: int | None = None, year: int | None = None) -> ShowGroup:
    return ShowGroup(key=f"{title}|S{season}", folder_name=title, search_title=title, year=year, season=season)


class HelperTests(unittest.TestCase):
    def test_parse_anilist_id(self) -> None:
        self.assertEqual(parse_anilist_id("16498"), 16498)
        self.assertEqual(parse_anilist_id(" https://anilist.co/anime/16498/Shingeki-no-Kyojin/ "), 16498)
        self.assertEqual(parse_anilist_id("anilist.co/anime/5"), 5)
        self.assertEqual(parse_anilist_id("HTTPS://ANILIST.CO/ANIME/77"), 77)
        for bad in ("", "abc", "0", "https://myanimelist.net/anime/16498", "https://anilist.co/manga/30002"):
            self.assertIsNone(parse_anilist_id(bad), bad)

    def test_normalise(self) -> None:
        self.assertEqual(normalise("Frieren: Beyond Journey’s End"), "frieren beyond journey s end")
        self.assertEqual(normalise("Spy×Family & Friends"), "spy x family and friends")
        self.assertEqual(normalise("ＳＨＯＷ"), "show")

    def test_ordinal(self) -> None:
        self.assertEqual([ordinal(n) for n in (1, 2, 3, 4, 11, 12, 13, 21, 22)],
                         ["1st", "2nd", "3rd", "4th", "11th", "12th", "13th", "21st", "22nd"])

    def test_season_markers(self) -> None:
        for title in ("Attack on Titan Season 2", "Oshi no Ko 2nd Season", "Overlord II", "Mob Psycho 100 II",
                      "Show 2", "Kaguya-sama: Love is War Season 2"):
            self.assertTrue(has_season_marker(title, 2), title)
        for title in ("Mob Psycho 100", "Attack on Titan Season 3", "Show 12", "Overlord III"):
            self.assertFalse(has_season_marker(title, 2), title)
        self.assertEqual(strip_season_marker("Overlord II", 2), "overlord")

    def test_title_score_uses_all_titles(self) -> None:
        item = media(1, "Shingeki no Kyojin", "Attack on Titan", synonyms=["AoT"])
        self.assertEqual(title_score("Attack on Titan", item), 100)
        self.assertEqual(title_score("shingeki no kyojin", item), 100)
        self.assertLess(title_score("Cowboy Bebop", item), 60)

    def test_year_adjust(self) -> None:
        item = media(1, "X", year=2013)
        self.assertEqual(year_adjust(90, 2013, item), 98)
        self.assertEqual(year_adjust(95, 2013, item), 100)
        self.assertEqual(year_adjust(90, 2014, item), 93)
        self.assertEqual(year_adjust(90, 2020, item), 75)
        self.assertEqual(year_adjust(90, None, item), 90)

    def test_rank_breaks_ties_on_popularity(self) -> None:
        quiet, popular = media(1, "Same Title", popularity=10), media(2, "Same Title", popularity=5000)
        self.assertEqual([m["id"] for _, m in rank("Same Title", None, [quiet, popular])], [2, 1])

    def test_english_cast(self) -> None:
        self.assertTrue(english_cast_listed(media(1, "X", voice_actors=True)))
        self.assertFalse(english_cast_listed(media(1, "X", voice_actors=False)))
        self.assertIsNone(english_cast_listed(media(1, "X")))
        self.assertIsNone(english_cast_listed(None))


class MatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.s3 = media(3, "Shingeki no Kyojin 3", "Attack on Titan Season 3", 2018, fmt="TV")
        self.s2 = media(2, "Shingeki no Kyojin Season 2", "Attack on Titan Season 2", 2017,
                        relations=[("SEQUEL", self.s3)])
        self.movie = media(9, "Shingeki no Kyojin Movie", fmt="MOVIE", year=2016)
        self.s1 = media(1, "Shingeki no Kyojin", "Attack on Titan", 2013, popularity=900,
                        relations=[("SEQUEL", self.movie), ("SEQUEL", self.s2), ("PREQUEL", media(8, "Other"))])
        self.items = [self.s1, self.s2, self.s3, self.movie]

    def matcher(self, searches: dict[str, list[dict]]) -> AniListMatcher:
        return AniListMatcher(FakeClient(searches, self.items), threshold=80)

    def test_first_season(self) -> None:
        outcome = self.matcher({"Attack on Titan": [self.s2, self.s1]}).find(group("Attack on Titan", 1, 2013))
        self.assertEqual(outcome.match.anilist_id, 1)
        self.assertGreaterEqual(outcome.match.confidence, 99)
        self.assertEqual(outcome.details["id"], 1)

    def test_later_season_when_sequel_chain_and_search_agree(self) -> None:
        outcome = self.matcher({"Attack on Titan": [self.s1],
                                "Attack on Titan Season 2": [self.s2, self.s1]}).find(group("Attack on Titan", 2, 2013))
        self.assertEqual(outcome.match.anilist_id, 2)
        self.assertEqual(outcome.match.confidence, 100)
        self.assertEqual(outcome.notes, [])

    def test_sequel_chain_skips_movies_and_follows_n_minus_1_steps(self) -> None:
        outcome = self.matcher({"Attack on Titan": [self.s1]}).find(group("Attack on Titan", 3, 2013))
        self.assertEqual(outcome.match.anilist_id, 3)
        self.assertAlmostEqual(outcome.match.confidence, 95.0, places=1)  # base score x 0.95
        self.assertIn("sequel list", outcome.match.note)

    def test_later_season_found_only_by_search(self) -> None:
        lonely_s2 = media(20, "Oshi no Ko 2nd Season", year=2024)
        base = media(10, "Oshi no Ko", year=2023)
        matcher = AniListMatcher(FakeClient({"Oshi no Ko": [base], "Oshi no Ko Season 2": [lonely_s2, base]},
                                            [base, lonely_s2]), 80)
        outcome = matcher.find(group("Oshi no Ko", 2))
        self.assertEqual(outcome.match.anilist_id, 20)
        self.assertGreaterEqual(outcome.match.confidence, 80)

    def test_later_season_not_found(self) -> None:
        base = media(10, "Lonely Show", year=2020)
        outcome = AniListMatcher(FakeClient({"Lonely Show": [base]}, [base]), 80).find(group("Lonely Show", 4))
        self.assertIsNone(outcome.match)
        self.assertIn("Season 4", outcome.notes[0])

    def test_specials_are_capped_below_the_threshold(self) -> None:
        outcome = self.matcher({"Attack on Titan": [self.s1]}).find(group("Attack on Titan", 0, 2013))
        self.assertLess(outcome.match.confidence, 80)

    def test_manual_match(self) -> None:
        outcome = self.matcher({}).find(group("Whatever", 2), override_id=3)
        self.assertEqual((outcome.match.anilist_id, outcome.match.confidence, outcome.match.manual), (3, 100, True))

    def test_manual_match_that_does_not_exist(self) -> None:
        outcome = self.matcher({}).find(group("Whatever"), override_id=424242)
        self.assertIsNone(outcome.match)
        self.assertIn("424242", outcome.notes[0])

    def test_retry_with_the_part_before_a_colon(self) -> None:
        item = media(5, "Made in Abyss", year=2017)
        matcher = AniListMatcher(FakeClient({"Made in Abyss": [item]}, [item]), 80)
        outcome = matcher.find(group("Made in Abyss: The Complete First Season Collection"))
        self.assertEqual(outcome.match.anilist_id, 5)

    def test_no_results(self) -> None:
        outcome = self.matcher({}).find(group("Nothing Like This"))
        self.assertIsNone(outcome.match)


class FakeResponse:
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self) -> dict:
        return self._body


class FakeSession:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.posts = 0

    def post(self, *_args, **_kwargs) -> FakeResponse:
        self.posts += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self._tmp.name) / "cache.db")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def client(self, responses: list, **kwargs) -> tuple[AniListClient, FakeSession]:
        session = FakeSession(responses)
        return AniListClient(self.cache, min_interval=0, session=session, **kwargs), session

    def test_answers_are_cached_including_not_found(self) -> None:
        found = FakeResponse(200, {"data": {"Page": {"media": [media(1, "Show")]}}})
        missing = FakeResponse(404, {"errors": [{"status": 404}], "data": {"Media": None}})
        client, session = self.client([found, missing])
        self.assertEqual(len(client.search("Show")), 1)
        self.assertEqual(len(client.search("Show")), 1)
        self.assertIsNone(client.details(99))
        self.assertIsNone(client.details(99))
        self.assertEqual(session.posts, 2)

    def test_slow_down_on_429(self) -> None:
        messages: list[str] = []
        ok = FakeResponse(200, {"data": {"Page": {"media": []}}})
        client, session = self.client([FakeResponse(429, headers={"Retry-After": "1"}), ok], status=messages.append)
        self.assertEqual(client.search("Anything"), [])
        self.assertEqual(session.posts, 2)
        self.assertTrue(any("AniList is busy and asked Dub Checker to wait" in m for m in messages))

    def test_server_errors_are_retried(self) -> None:
        ok = FakeResponse(200, {"data": {"Page": {"media": []}}})
        client, session = self.client([FakeResponse(502), ok])
        with mock.patch("dubchecker.anilist.wait_or_cancel"):
            client.search("Anything")
        self.assertEqual(session.posts, 2)

    def test_goes_offline_after_repeated_connection_errors(self) -> None:
        import requests
        client, session = self.client([requests.ConnectionError("down")] * 3)
        with mock.patch("dubchecker.anilist.wait_or_cancel"), self.assertRaises(AniListError):
            client.search("Anything")
        with self.assertRaises(AniListError):
            client.search("Something else")
        self.assertEqual(session.posts, 3)

    def test_waits_can_be_cancelled(self) -> None:
        cancel = threading.Event()
        client, _ = self.client([FakeResponse(429, headers={"Retry-After": "30"})], cancel=cancel)
        threading.Timer(0.2, cancel.set).start()
        with self.assertRaises(Cancelled):
            client.search("Anything")


if __name__ == "__main__":
    unittest.main()
