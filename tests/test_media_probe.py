"""Reading synthetic MKVs with the real MediaInfo and (if present) ffprobe."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.config import user_data_dir
from dubchecker.media_probe import (MediaProber, ProbeError, build_file_result, find_ffprobe, read_with_ffprobe,
                                    read_with_mediainfo)
from dubchecker.models import FileStatus, Lang
from tests.mkv import ENG, JPN, UND, write_mkv


def mediainfo_available() -> bool:
    try:
        from pymediainfo import MediaInfo
        return bool(MediaInfo.can_parse())
    except Exception:
        return False


FFPROBE = find_ffprobe(user_data_dir() / "bin")


class SyntheticFiles(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.dual = write_mkv(self.dir / "dual.mkv", [JPN, ENG])
        self.unlabeled = write_mkv(self.dir / "und.mkv", [JPN, UND])
        self.commentary = write_mkv(self.dir / "commentary.mkv", [JPN, ("eng", "Commentary")], channels=6)
        self.hinted = write_mkv(self.dir / "hinted.mkv", [("und", "English 5.1"), ("und", "日本語")])
        self.garbage = self.dir / "garbage.mkv"
        self.garbage.write_bytes(b"definitely not a video" * 64)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def check_reader(self, read) -> None:
        tracks = read(str(self.dual))
        self.assertEqual([t["title"] for t in tracks], ["Japanese", "English"])
        self.assertEqual(build_file_result("", 0, 0, tracks, True, "disk").status, FileStatus.DUAL)
        self.assertEqual(tracks[0]["channels"], "2")
        self.assertIs(build_file_result("", 0, 0, read(str(self.unlabeled)), True, "disk").status,
                      FileStatus.UNLABELED)
        commentary = build_file_result("", 0, 0, read(str(self.commentary)), True, "disk")
        self.assertIs(commentary.status, FileStatus.ORIGINAL_ONLY)
        self.assertEqual(commentary.tracks[1].channels, "6")
        hinted = build_file_result("", 0, 0, read(str(self.hinted)), True, "disk")
        self.assertEqual([t.lang for t in hinted.tracks], [Lang.ENGLISH, Lang.JAPANESE])

    @unittest.skipUnless(mediainfo_available(), "MediaInfo isn't installed")
    def test_mediainfo(self) -> None:
        self.check_reader(read_with_mediainfo)
        self.assertEqual(read_with_mediainfo(str(self.dual))[0]["language"], "jpn")
        with self.assertRaises(ProbeError):
            read_with_mediainfo(str(self.garbage))

    @unittest.skipUnless(FFPROBE, "ffprobe isn't available")
    def test_ffprobe(self) -> None:
        self.check_reader(lambda path: read_with_ffprobe(FFPROBE, path))
        with self.assertRaises(ProbeError):
            read_with_ffprobe(FFPROBE, str(self.garbage))

    @unittest.skipUnless(FFPROBE, "ffprobe isn't available")
    def test_prober_uses_ffprobe_when_mediainfo_is_missing(self) -> None:
        prober = MediaProber(self.dir / "bin")
        with mock.patch.object(MediaProber, "mediainfo_available", return_value=False), \
                mock.patch("dubchecker.media_probe.find_ffprobe", return_value=FFPROBE):
            self.assertEqual(len(prober.probe(str(self.dual))), 2)

    @unittest.skipUnless(mediainfo_available(), "MediaInfo isn't installed")
    def test_prober_reports_unreadable_files(self) -> None:
        prober = MediaProber(self.dir / "bin")
        with mock.patch("dubchecker.media_probe.find_ffprobe", return_value=None), self.assertRaises(ProbeError):
            prober.probe(str(self.garbage))
        with self.assertRaises(Exception):
            prober.probe(str(self.dir / "missing.mkv"))


if __name__ == "__main__":
    unittest.main()
