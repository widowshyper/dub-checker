"""The two-phase scan: reading, caching, lookups only where needed, and Stop."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.cache import Cache
from dubchecker.categorize import STOPPED_NOTE
from dubchecker.config import Config, Overrides
from dubchecker.media_probe import ProbeUnavailable
from dubchecker.models import Category, FileStatus
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.lib = base / "Anime"
        self.data = base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")
        self.log: list = []
        # A Show S1 has dual audio; S2 does not and must be looked up on its own.
        touch(self.lib / "A Show (2020)" / "Season 01" / "A - S01E01 [dual].mkv")
        touch(self.lib / "A Show (2020)" / "Season 01" / "A - S01E02 [jpn].mkv")
        touch(self.lib / "A Show (2020)" / "Season 02" / "A - S02E01 [jpn].mkv")
        touch(self.lib / "A Show (2020)" / "Season 02" / "A - S02E02 [jpn].mkv")
        touch(self.lib / "B Show" / "Season 01" / "B - S01E01 [dual].mkv")
        touch(self.lib / "C Show" / "C - 01 [eng].mkv")
        touch(self.lib / "D Show" / "Season 01" / "D - S01E01 [dual].mkv")
        touch(self.lib / "D Show" / "Season 01" / "D - S01E02 [und].mkv")
        self.total_files = 8

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def scan(self, lookup=None, prober=None, forbid_stat: bool = False, **kwargs):
        pipeline, events = make_pipeline(self.data, self.cache, prober or FakeProber(self.log),
                                         lookup or FakeLookup(self.log, dub=False), **kwargs)
        if forbid_stat:
            with mock.patch("os.stat", side_effect=AssertionError("os.stat was called")):
                outcome = pipeline.run(root=str(self.lib))
        else:
            outcome = pipeline.run(root=str(self.lib))
        return outcome, events, {r.key: r for r in outcome.results}

    def test_reads_everything_before_any_lookup(self) -> None:
        outcome, _, _ = self.scan()
        kinds = [kind for kind, _ in self.log]
        self.assertEqual(kinds.count("read"), self.total_files)
        self.assertEqual(kinds, sorted(kinds, key=lambda k: k == "lookup"))  # all reads, then all lookups
        self.assertIsNone(outcome.error)

    def test_only_seasons_without_dual_audio_are_looked_up(self) -> None:
        _, _, results = self.scan()
        looked_up = [key for kind, key in self.log if kind == "lookup"]
        self.assertEqual(sorted(looked_up), ["A Show (2020)|S2", "C Show"])
        self.assertIs(results["A Show (2020)|S1"].category, Category.NEEDS_DUB)
        self.assertIs(results["B Show|S1"].category, Category.FULLY_DUBBED)
        self.assertIs(results["D Show|S1"].category, Category.CHECK)
        self.assertIs(results["C Show"].category, Category.FULLY_DUBBED)

    def test_a_later_season_is_not_dubbed_just_because_season_1_is(self) -> None:
        _, _, results = self.scan(lookup=FakeLookup(self.log, dub=False))
        self.assertIs(results["A Show (2020)|S2"].category, Category.NO_DUB)
        self.assertTrue(results["A Show (2020)|S2"].looked_up)

    def test_seasons_decided_locally_arrive_first_and_together(self) -> None:
        _, events, _ = self.scan()
        kinds = [e[0] for e in events if e[0] != "progress"]
        self.assertEqual(kinds, ["results", "result", "result", "done"])
        batch = events[[e[0] for e in events].index("results")][1]
        self.assertEqual(sorted(r.key for r in batch), ["A Show (2020)|S1", "B Show|S1", "D Show|S1"])
        for result in batch:  # no AniList match, yet not in Check Manually because of it
            self.assertIsNone(result.match)
            self.assertFalse(any("AniList" in note for note in result.notes))

    def test_cached_rescan_reads_nothing_and_never_calls_os_stat(self) -> None:
        self.scan()
        prober = FakeProber([])
        outcome, _, results = self.scan(prober=prober, forbid_stat=True)
        self.assertEqual(prober.reads, 0)
        self.assertEqual((outcome.stats.cached, outcome.stats.read), (self.total_files, 0))
        self.assertIs(results["A Show (2020)|S1"].category, Category.NEEDS_DUB)
        self.assertTrue(all(f.source == "cache" for r in outcome.results for f in r.files))

    def test_a_changed_file_is_read_again(self) -> None:
        self.scan()
        touch(self.lib / "B Show" / "Season 01" / "B - S01E01 [dual].mkv", size=99)
        prober = FakeProber([])
        outcome, _, _ = self.scan(prober=prober)
        self.assertEqual(prober.reads, 1)
        self.assertEqual(outcome.stats.cached, self.total_files - 1)

    def test_a_broken_file_does_not_stop_the_scan(self) -> None:
        touch(self.lib / "B Show" / "Season 01" / "B - S01E02 [broken].mkv")
        outcome, _, results = self.scan()
        broken = [f for f in results["B Show|S1"].files if f.status is FileStatus.ERROR]
        self.assertEqual(len(broken), 1)
        self.assertIn("corrupt", broken[0].error)
        self.assertEqual((outcome.stats.failed, outcome.stats.read), (1, self.total_files))
        self.assertIs(results["B Show|S1"].category, Category.CHECK)
        self.assertEqual(len(outcome.results), 5)

    def test_failed_reads_are_not_cached(self) -> None:
        touch(self.lib / "B Show" / "Season 01" / "B - S01E02 [broken].mkv")
        self.scan()
        prober = FakeProber([])
        self.scan(prober=prober)
        self.assertEqual(prober.reads, 1)

    def test_stop_during_lookups_keeps_what_is_finished(self) -> None:
        cancel = threading.Event()
        outcome, _, results = self.scan(lookup=FakeLookup(self.log, on_lookup=lambda _g: cancel.set()),
                                        cancel=cancel)
        self.assertTrue(outcome.stopped)
        self.assertEqual(len(outcome.results), 5)
        stopped = [r for r in outcome.results if r.stopped]
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0].notes, [STOPPED_NOTE])
        self.assertIs(stopped[0].category, Category.CHECK)
        self.assertIs(results["B Show|S1"].category, Category.FULLY_DUBBED)

    def test_stop_during_reading(self) -> None:
        cancel = threading.Event()
        prober = FakeProber(self.log, on_read=lambda n: n >= 3 and cancel.set())
        outcome, _, _ = self.scan(prober=prober, cancel=cancel, config=Config(probe_workers=1))
        self.assertTrue(outcome.stopped)
        self.assertEqual(prober.reads, 3)
        self.assertNotIn("lookup", [kind for kind, _ in self.log])
        read = sum(len(r.files) for r in outcome.results)
        self.assertEqual(read, 3)
        self.assertTrue(all(r.stopped for r in outcome.results if r.category is Category.CHECK))
        self.assertEqual(self.cache.file_count(), 3)  # saved even though stopped

    def test_manual_match_on_a_dual_audio_season_keeps_the_local_verdict(self) -> None:
        overrides = Overrides(self.data / "overrides.json")
        overrides.set("B Show|S1", 555)
        _, _, results = self.scan(overrides=overrides)
        result = results["B Show|S1"]
        self.assertIs(result.category, Category.FULLY_DUBBED)
        self.assertEqual((result.match.anilist_id, result.match.manual), (555, True))

    def test_nothing_to_read_with(self) -> None:
        class NoReader:
            def probe(self, path: str) -> list:
                raise ProbeUnavailable("Install MediaInfo")

        outcome, events, _ = self.scan(prober=NoReader())
        self.assertEqual(outcome.error, "Install MediaInfo")
        self.assertEqual(events[-1][0], "done")

    def test_missing_folder(self) -> None:
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber([]), FakeLookup([]))
        outcome = pipeline.run(root=str(self.lib / "nope"))
        self.assertIn("wasn't found", outcome.error)

    def test_loose_files_join_a_show_folder_with_the_same_name(self) -> None:
        touch(self.lib / "Frieren" / "Frieren - 01 [jpn].mkv")
        touch(self.lib / "Frieren - 02 [jpn].mkv")
        _, _, results = self.scan()
        self.assertEqual(len(results["Frieren"].files), 2)

    def test_statistics(self) -> None:
        outcome, _, _ = self.scan()
        stats = outcome.stats
        self.assertEqual((stats.files, stats.read, stats.cached, stats.lookups), (self.total_files, 8, 0, 2))
        self.assertGreaterEqual(stats.folders_listed, 8)
        self.assertEqual(stats.short_text(), "(8 read from disk)")
        self.assertIn("8 files", stats.log_line(1.0))


if __name__ == "__main__":
    unittest.main()
