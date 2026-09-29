"""Saving and loading the last scan's results."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.categorize import categorize_local, stopped_result
from dubchecker.media_probe import build_file_result
from dubchecker.models import (AniListMatch, Category, DubInfo, FileResult, FileStatus, ShowGroup, ShowResult,
                               VideoFile)
from dubchecker.results_store import SavedScan, delete_scan, load_scan, save_scan


def sample_results() -> list[ShowResult]:
    group = ShowGroup("Show (2020)|S2", "Show (2020)", "Show", 2020, 2)
    dual = build_file_result("E:\\Anime\\Show (2020)\\Season 02\\e1.mkv", 123, 1700000000.5,
                             [{"index": 0, "language": "jpn", "title": "Japanese", "codec": "AAC", "channels": "2"},
                              {"index": 1, "language": "eng", "title": "Commentary", "codec": "AAC", "channels": "2"}],
                             True, "disk")
    broken = FileResult("E:\\Anime\\Show (2020)\\Season 02\\e2.mkv", 5, 1.0, status=FileStatus.ERROR,
                        error="corrupt", source="disk")
    looked_up = ShowResult(group, [dual, broken], Category.CHECK,
                           match=AniListMatch(7, "Show Season 2", "Show 2", "Show S2", 70, 2021, "TV", 12, 95.5,
                                              manual=True, note="You chose this match"),
                           dub=DubInfo(True, ["AniList cast list"], incomplete=True), notes=["1 file couldn't be read"],
                           looked_up=True)
    local = categorize_local(ShowGroup("Other", "Other", "Other"),
                             [FileResult("\\\\nas\\anime\\Other\\1.mkv", status=FileStatus.DUAL)])
    stopped = stopped_result(ShowGroup("Late|S1", "Late", "Late", season=1),
                             [FileResult("D:\\Late\\1.mkv", status=FileStatus.ORIGINAL_ONLY)])
    for result in (looked_up, local, stopped):  # as the scan builds them
        result.group.files = [VideoFile(f.path, f.size, f.mtime) for f in result.files]
    return [looked_up, local, stopped]


class ResultsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "last_scan.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_round_trip_keeps_everything_the_table_and_details_show(self) -> None:
        original = sample_results()
        save_scan(self.path, SavedScan(original, "folder", 1700000000.0, True, ["CHECK", "AIRING"], ["Show (2020)"]))
        loaded = load_scan(self.path)
        self.assertEqual((loaded.source, loaded.finished_at, loaded.stopped, loaded.tabs, loaded.expanded),
                         ("folder", 1700000000.0, True, ["CHECK", "AIRING"], ["Show (2020)"]))
        self.assertEqual(loaded.results, original)
        for before, after in zip(original, loaded.results):
            for text in (ui.anilist_text, ui.confirmed_text, ui.match_text, ui.notes_text):
                self.assertEqual(text(before), text(after))
        self.assertTrue(loaded.results[0].files[0].tracks[1].ignored)
        self.assertEqual(loaded.results[0].group.files[0].size, 123)

    def test_paths_on_the_app_drive_follow_a_new_drive_letter(self) -> None:
        with mock.patch("dubchecker.config.app_drive", return_value="E:"):
            save_scan(self.path, SavedScan(sample_results()))
        stored = json.loads(self.path.read_text("utf-8"))["results"][0]["files"][0]["path"]
        self.assertEqual(stored, "\\Anime\\Show (2020)\\Season 02\\e1.mkv")
        with mock.patch("dubchecker.config.app_drive", return_value="F:"):
            loaded = load_scan(self.path)
        self.assertEqual(loaded.results[0].files[0].path, "F:\\Anime\\Show (2020)\\Season 02\\e1.mkv")
        self.assertEqual(loaded.results[1].files[0].path, "\\\\nas\\anime\\Other\\1.mkv")  # UNC unchanged

    def test_missing_broken_or_foreign_files_are_ignored(self) -> None:
        self.assertIsNone(load_scan(self.path))
        self.path.write_text("{broken", "utf-8")
        self.assertIsNone(load_scan(self.path))
        self.path.write_text(json.dumps({"version": 99, "results": []}), "utf-8")
        self.assertIsNone(load_scan(self.path))
        self.path.write_text(json.dumps({"version": 1, "results": [{"files": []}]}), "utf-8")
        self.assertIsNone(load_scan(self.path))

    def test_delete(self) -> None:
        save_scan(self.path, SavedScan(sample_results()))
        delete_scan(self.path)
        self.assertFalse(self.path.exists())
        delete_scan(self.path)  # nothing there is fine


class HelperTests(unittest.TestCase):
    def test_shared_folder_and_japanese_only_files(self) -> None:
        results = [ShowResult(ShowGroup(f"S|S{n}", "S", "S", season=n),
                              [FileResult(f"/lib/S/Season {n}/e{n}.mkv", status=status)], Category.NEEDS_DUB)
                   for n, status in ((1, FileStatus.DUAL), (2, FileStatus.ORIGINAL_ONLY))]
        self.assertEqual(Path(ui.shared_folder(results)), Path("/lib/S"))
        self.assertEqual(Path(ui.shared_folder(results[:1])), Path("/lib/S/Season 1"))
        self.assertEqual(ui.original_only_files(results), ["e2.mkv"])
        self.assertEqual(ui.shared_folder([]), "")


if __name__ == "__main__":
    unittest.main()
