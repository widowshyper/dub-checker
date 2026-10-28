import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.cache import MISSING, Cache, FileCacheWriter
from tests.fakes import touch


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Cache(Path(self._tmp.name) / "cache.db")

    def tearDown(self) -> None:
        self.cache.close()  # Windows can't delete an open database
        self._tmp.cleanup()

    def test_file_round_trip_in_batches(self) -> None:
        writer = FileCacheWriter(self.cache, batch_size=25)
        wanted = {}
        for n in range(1203):  # more than two lookup chunks of 500
            key = f"\\Anime\\Show {n // 12}\\ep {n}.mkv"
            wanted[key] = (1000 + n, 1_700_000_000.123 + n)
            writer.add(key, 1000 + n, 1_700_000_000.123 + n, [{"index": 0, "language": "jpn"}])
        writer.flush()
        self.assertEqual(self.cache.file_count(), 1203)
        found = self.cache.get_files(wanted)
        self.assertEqual(len(found), 1203)
        self.assertEqual(found["\\Anime\\Show 0\\ep 0.mkv"], [{"index": 0, "language": "jpn"}])

    def test_changed_or_unknown_files_are_misses(self) -> None:
        self.cache.save_files([("a.mkv", 10, 5.0, []), ("b.mkv", 10, 5.0, [])])
        found = self.cache.get_files({"a.mkv": (11, 5.0), "b.mkv": (10, 6.0), "c.mkv": (1, 1.0)})
        self.assertEqual(found, {})
        self.assertIn("a.mkv", self.cache.get_files({"a.mkv": (10, 5.0)}))

    def test_writer_saves_every_batch_and_on_flush(self) -> None:
        writer = FileCacheWriter(self.cache, batch_size=25)
        with mock.patch.object(self.cache, "save_files", wraps=self.cache.save_files) as save:
            for n in range(60):
                writer.add(f"{n}.mkv", n, 0.0, [])
            self.assertEqual(save.call_count, 2)
            writer.flush()
            self.assertEqual(save.call_count, 3)
        self.assertEqual(self.cache.file_count(), 60)

    def test_anilist_answers_expire_and_can_be_cleared(self) -> None:
        self.cache.save_anilist("search:x", {"data": {"Page": {"media": []}}})
        self.assertEqual(self.cache.get_anilist("search:x", 30), {"data": {"Page": {"media": []}}})
        with mock.patch("dubchecker.cache.time.time", return_value=time.time() + 31 * 86400):
            self.assertIs(self.cache.get_anilist("search:x", 30), MISSING)
        self.assertIs(self.cache.get_anilist("search:y", 30), MISSING)
        self.cache.save_files([("a.mkv", 1, 1.0, [])])
        self.assertEqual(self.cache.clear_anilist(), 1)
        self.assertIs(self.cache.get_anilist("search:x", 30), MISSING)
        self.assertEqual(self.cache.file_count(), 1)  # file info survives


class PruneTests(unittest.TestCase):
    """cache.db doesn't keep growing: files gone from the library and expired AniList answers are forgotten."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.cache = Cache(self.base / "cache.db")
        self.addCleanup(self.cache.close)

    def test_prune_files_under_a_folder(self) -> None:
        tracks = [{"index": 0, "language": "jpn"}]
        paths = [r"\\nas\anime\A\1.mkv", r"\\nas\anime\A\2.mkv", r"\\nas\anime2\B\1.mkv", r"\\nas\other\C\1.mkv"]
        self.cache.save_files([(p, 1, 1.0, tracks) for p in paths])
        self.assertEqual(self.cache.prune_files("\\\\nas\\anime\\", {paths[0]}), 1)
        left = self.cache.get_files({p: (1, 1.0) for p in paths})
        self.assertEqual(sorted(left), sorted([paths[0], paths[2], paths[3]]))  # "anime2" isn't under "anime\"

    def test_prune_expired_anilist_answers(self) -> None:
        from unittest import mock
        self.cache.save_anilist("search:old", {"data": {}})
        with mock.patch("dubchecker.cache.time.time", return_value=time.time() + 40 * 86400):
            self.cache.save_anilist("search:new", {"data": {}})
            self.assertEqual(self.cache.prune_anilist(30), 1)
            self.assertIsNot(self.cache.get_anilist("search:new", 30), MISSING)


class ForgetMissingFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.lib, self.data = base / "Anime", base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")
        self.addCleanup(self.cache.close)
        self.files = [touch(self.lib / show / "Season 01" / f"e{n} [dual].mkv") for show in ("A", "B") for n in (1, 2)]

    def scan(self, **run) -> None:
        from tests.fakes import FakeLookup, FakeProber, make_pipeline
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber([]), FakeLookup([]))
        pipeline.run(root=str(self.lib), **run)

    def test_deleted_files_are_forgotten_after_a_full_scan(self) -> None:
        self.scan()
        self.assertEqual(self.cache.file_count(), 4)
        self.files[0].unlink()
        self.scan(only={"B"})  # one tab: the rest of the library wasn't looked at
        self.assertEqual(self.cache.file_count(), 4)
        self.scan()
        self.assertEqual(self.cache.file_count(), 3)

    def test_kept_when_a_folder_couldnt_be_listed(self) -> None:
        from unittest import mock
        self.scan()
        self.files[0].unlink()
        real = os.scandir

        def scandir(path):
            if os.path.basename(str(path)) == "B":
                raise PermissionError("no access")
            return real(path)

        with mock.patch("dubchecker.scanner.os.scandir", scandir):
            self.scan()
        self.assertEqual(self.cache.file_count(), 4)

    def test_not_for_the_root_of_the_apps_own_drive(self) -> None:
        from unittest import mock

        from dubchecker.pipeline import ScanPipeline
        pipeline = mock.Mock(spec=ScanPipeline, cache=mock.Mock())
        pipeline._key.return_value = "\\"  # what E:\ becomes when the app runs from E:
        ScanPipeline._forget_missing_files(pipeline, "E:\\", [])
        pipeline.cache.prune_files.assert_not_called()


if __name__ == "__main__":
    unittest.main()
