"""Sonarr: media info parsing, path mapping, and a scan against a mock Sonarr server."""
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dubchecker.cache import Cache
from dubchecker.config import Config
from dubchecker.media_probe import build_file_result
from dubchecker.models import Category, FileStatus
from dubchecker.sonarr import (SonarrClient, SonarrError, connection_summary, last_path_part, map_path,
                               normalise_url, search_for_replacements, tracks_from_media_info)
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch

API_KEY = "secret"
SERIES = [
    {"id": 1, "title": "Attack on Titan", "year": 2013, "path": "/tv/Attack on Titan (2013)", "seriesType": "anime",
     "status": "continuing", "seasons": [
         {"seasonNumber": 1, "statistics": {"previousAiring": "2013-09-28T15:00:00Z"}},
         {"seasonNumber": 2, "statistics": {"previousAiring": "2017-06-17T15:00:00Z",
                                            "nextAiring": "2099-01-07T15:00:00Z"}}]},
    {"id": 2, "title": "Friends", "year": 1994, "path": "/tv/Friends", "seriesType": "standard", "status": "ended"},
    {"id": 3, "title": "Frieren", "year": 2023, "path": "/tv/Frieren", "seriesType": "anime", "status": "ended"},
]
CALENDAR = [{"seriesId": 1, "seasonNumber": 2, "episodeNumber": 9, "airDateUtc": "2099-01-07T15:00:00Z"},
            {"seriesId": 1, "seasonNumber": 2, "episodeNumber": 10, "airDateUtc": "2099-01-14T15:00:00Z"}]
CALENDAR_QUERIES: list[dict] = []
FILES = {
    1: [
        {"id": 11, "path": "/tv/Attack on Titan (2013)/Season 01/AoT - S01E01.mkv", "seasonNumber": 1, "size": 100,
         "mediaInfo": {"audioLanguages": "Japanese/English", "audioStreamCount": 2, "audioCodec": "FLAC"}},
        {"id": 12, "path": "/tv/Attack on Titan (2013)/Season 02/AoT - S02E01.mkv", "seasonNumber": 2, "size": 100,
         "mediaInfo": {"audioLanguages": "jpn", "audioStreamCount": 1}},
        {"id": 13, "path": "/tv/Attack on Titan (2013)/Season 02/AoT - S02E02 [jpn].mkv", "seasonNumber": 2, "size": 100,
         "mediaInfo": None},
    ],
    2: [{"path": "/tv/Friends/Season 01/Friends - S01E01.mkv", "seasonNumber": 1, "size": 1,
         "mediaInfo": {"audioLanguages": "eng", "audioStreamCount": 1}}],
    3: [{"id": 31, "path": "/tv/Frieren/Season 01/Frieren - S01E01 [dual].mkv", "seasonNumber": 1, "size": 100,
         "mediaInfo": {"audioLanguages": "jpn", "audioStreamCount": 2}}],
}
EPISODES = {
    1: [{"id": 101, "seasonNumber": 1, "episodeFileId": 11}, {"id": 102, "seasonNumber": 2, "episodeFileId": 12},
        {"id": 103, "seasonNumber": 2, "episodeFileId": 13}, {"id": 104, "seasonNumber": 2, "episodeFileId": 0}],
    3: [{"id": 301, "seasonNumber": 1, "episodeFileId": 31}],
}
COMMANDS: list[dict] = []  # bodies POSTed to /command


class MockSonarr(BaseHTTPRequestHandler):
    url_base = "/sonarr"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith(self.url_base + "/api/v3/"):
            return self._send(404, {"message": "NotFound"})
        if self.headers.get("X-Api-Key") != API_KEY:
            return self._send(401, {"error": "Unauthorized"})
        route = parsed.path[len(self.url_base):]
        if route == "/api/v3/system/status":
            return self._send(200, {"version": "4.0.9.2244"})
        if route == "/api/v3/series":
            return self._send(200, SERIES)
        if route == "/api/v3/episodefile":
            return self._send(200, FILES.get(int(parse_qs(parsed.query)["seriesId"][0]), []))
        if route == "/api/v3/calendar":
            CALENDAR_QUERIES.append(parse_qs(parsed.query))
            return self._send(200, CALENDAR)
        if route == "/api/v3/episode":
            return self._send(200, EPISODES.get(int(parse_qs(parsed.query)["seriesId"][0]), []))
        return self._send(404, {"message": "NotFound"})

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get("X-Api-Key") != API_KEY:
            return self._send(401, {"error": "Unauthorized"})
        if urlparse(self.path).path != self.url_base + "/api/v3/command":
            return self._send(404, {"message": "NotFound"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        COMMANDS.append(body)
        return self._send(201, {"id": len(COMMANDS), "name": body["name"], "status": "queued"})

    def _send(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


class ParsingTests(unittest.TestCase):
    def status(self, media_info: dict) -> FileStatus:
        return build_file_result("x", 0, 0, tracks_from_media_info(media_info), True, "sonarr").status

    def test_media_info(self) -> None:
        self.assertIsNone(tracks_from_media_info(None))
        tracks = tracks_from_media_info({"audioLanguages": "jpn/eng", "audioStreamCount": 2, "audioCodec": "AAC",
                                         "audioChannels": 2.0})
        self.assertEqual([t["language"] for t in tracks], ["jpn", "eng"])
        self.assertEqual((tracks[0]["codec"], tracks[0]["channels"], tracks[1]["codec"]), ("AAC", "2.0", ""))

    def test_statuses(self) -> None:
        self.assertIs(self.status({"audioLanguages": "Japanese / English", "audioStreamCount": 2}), FileStatus.DUAL)
        self.assertIs(self.status({"audioLanguages": "jpn", "audioStreamCount": 1}), FileStatus.ORIGINAL_ONLY)
        self.assertIs(self.status({"audioLanguages": "jpn", "audioStreamCount": 2}), FileStatus.UNLABELED)
        self.assertIs(self.status({"audioLanguages": "", "audioStreamCount": 1}), FileStatus.UNLABELED)

    def test_path_mapping(self) -> None:
        self.assertEqual(map_path("/tv/Show/Season 01/e.mkv", "/tv", "\\\\nas\\media\\tv"),
                         "\\\\nas\\media\\tv\\Show\\Season 01\\e.mkv")
        self.assertEqual(map_path("/data/tv/Show/e.mkv", "/data/tv/", "/mnt/tv"), "/mnt/tv/Show/e.mkv")
        self.assertEqual(map_path("/TV/Show", "/tv", "T:\\"), "T:\\Show")
        self.assertEqual(map_path("/tv", "/tv", "T:\\TV"), "T:\\TV")
        self.assertEqual(map_path("/tvshows/Show", "/tv", "T:\\"), "/tvshows/Show")
        self.assertEqual(map_path("/tv/Show", "", ""), "/tv/Show")

    def test_folder_names_and_urls(self) -> None:
        self.assertEqual(last_path_part("/tv/Attack on Titan (2013)"), "Attack on Titan (2013)")
        self.assertEqual(last_path_part("D:\\TV\\Show\\"), "Show")
        self.assertEqual(normalise_url("nas:8989/"), "http://nas:8989")


class ServerTests(unittest.TestCase):
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
        self.data = Path(self._tmp.name) / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def test_client(self) -> None:
        client = SonarrClient(self.url, API_KEY)
        self.assertEqual(client.status()["version"], "4.0.9.2244")
        self.assertEqual([s.title for s in client.series()], ["Attack on Titan", "Friends", "Frieren"])
        self.assertEqual(len(client.episode_files(1)), 3)
        self.assertEqual(connection_summary(client), "Connected to Sonarr 4.0.9.2244. It has 3 series, "
                                                     "2 of them set to the Anime series type.")

    def test_friendly_errors(self) -> None:
        with self.assertRaisesRegex(SonarrError, "rejected the API key"):
            SonarrClient(self.url, "wrong").status()
        with self.assertRaisesRegex(SonarrError, "URL base"):
            SonarrClient(self.url.replace("/sonarr", ""), API_KEY).status()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]
        with self.assertRaisesRegex(SonarrError, "Couldn't connect"):
            SonarrClient(f"http://127.0.0.1:{free_port}", API_KEY, timeout=5).status()
        with self.assertRaisesRegex(SonarrError, "API key"):
            SonarrClient(self.url, "").status()

    def scan(self, **config):
        log: list = []
        prober = FakeProber(log)
        config.setdefault("air_status_source", "off")  # the air status steps have their own tests
        pipeline, _ = make_pipeline(self.data, self.cache, prober, FakeLookup(log), config=Config(**config))
        outcome = pipeline.run(sonarr=SonarrClient(self.url, API_KEY))
        return outcome, prober, log, {r.key: r for r in outcome.results}

    def test_scan_with_path_mapping(self) -> None:
        local = Path(self._tmp.name) / "nas" / "tv"
        touch(local / "Attack on Titan (2013)" / "Season 02" / "AoT - S02E02 [jpn].mkv")
        touch(local / "Frieren" / "Season 01" / "Frieren - S01E01 [dual].mkv")
        outcome, prober, log, results = self.scan(sonarr_path_from="/tv", sonarr_path_to=str(local))
        self.assertIsNone(outcome.error)
        self.assertEqual(sorted(results), ["Attack on Titan (2013)|S1", "Attack on Titan (2013)|S2", "Frieren|S1"])
        self.assertEqual(prober.reads, 2)  # only the files Sonarr couldn't label
        self.assertEqual(outcome.stats.from_sonarr, 3)
        self.assertIs(results["Attack on Titan (2013)|S1"].category, Category.FULLY_DUBBED)
        self.assertIs(results["Frieren|S1"].category, Category.FULLY_DUBBED)  # dual once read locally
        self.assertIs(results["Attack on Titan (2013)|S2"].category, Category.NEEDS_DUB)
        self.assertEqual([key for kind, key in log if kind == "lookup"], ["Attack on Titan (2013)|S2"])
        self.assertEqual(outcome.warnings, [])

    def test_unreachable_files_give_one_warning(self) -> None:
        outcome, prober, _, results = self.scan()
        self.assertEqual(prober.reads, 0)
        self.assertEqual(len(outcome.warnings), 1)
        self.assertIn("path mapping", outcome.warnings[0])
        self.assertIs(results["Frieren|S1"].category, Category.CHECK)

    def test_all_series_types(self) -> None:
        _, _, _, results = self.scan(sonarr_anime_only=False, sonarr_read_unknown=False)
        self.assertIn("Friends|S1", results)

    def test_search_for_the_japanese_only_episodes(self) -> None:
        COMMANDS.clear()
        client = SonarrClient(self.url, API_KEY)
        message = search_for_replacements(client, "Attack on Titan (2013)", "Attack on Titan", [2],
                                          ["AoT - S02E01.mkv", "aot - s02e02 [JPN].mkv"])
        self.assertEqual(COMMANDS, [{"name": "EpisodeSearch", "episodeIds": [102, 103]}])
        self.assertEqual(message, "Sonarr is searching for new releases of 2 episodes of Attack on Titan.")

    def test_search_falls_back_to_whole_seasons(self) -> None:
        COMMANDS.clear()
        client = SonarrClient(self.url, API_KEY)
        message = search_for_replacements(client, "Attack on Titan (2013)", "Attack on Titan", [2, 0, 2],
                                          ["renamed since.mkv"])
        self.assertEqual(COMMANDS, [{"name": "SeasonSearch", "seriesId": 1, "seasonNumber": 0},
                                    {"name": "SeasonSearch", "seriesId": 1, "seasonNumber": 2}])
        self.assertIn("Specials, Season 2", message)
        COMMANDS.clear()
        search_for_replacements(client, "Frieren", "Frieren", [None], [])
        self.assertEqual(COMMANDS, [{"name": "SeriesSearch", "seriesId": 3}])

    def test_series_is_found_by_folder_then_title(self) -> None:
        client = SonarrClient(self.url, API_KEY)
        self.assertEqual(client.find_series("Attack on Titan (2013)").id, 1)
        self.assertEqual(client.find_series("My AoT folder", "Attack on Titan").id, 1)  # a local folder named differently
        self.assertIsNone(client.find_series("Unknown Show", "Unknown Show"))
        with self.assertRaisesRegex(SonarrError, "Couldn't find"):
            search_for_replacements(client, "Unknown Show", "Unknown Show", [1], ["x.mkv"])

    def test_bad_key_is_reported(self) -> None:
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber([]), FakeLookup([]))
        outcome = pipeline.run(sonarr=SonarrClient(self.url, "nope"))
        self.assertIn("rejected the API key", outcome.error)


if __name__ == "__main__":
    unittest.main()
