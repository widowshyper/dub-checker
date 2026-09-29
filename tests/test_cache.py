import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.cache import MISSING, Cache, FileCacheWriter


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


if __name__ == "__main__":
    unittest.main()
