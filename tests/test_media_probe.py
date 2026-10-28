"""Reading synthetic MKVs with the real MediaInfo and (if present) ffprobe."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.config import user_data_dir
from dubchecker.media_probe import (MediaProber, ProbeError, build_file_result, ffprobe_name, find_ffprobe,
                                    read_with_ffprobe, read_with_mediainfo)
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


class FfprobeDownloadTests(unittest.TestCase):
    """A downloaded ffprobe is only unpacked and run when it matches the checksum its site publishes."""

    def setUp(self) -> None:
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.bin = Path(self._tmp.name) / "bin"

    def download(self, archive: bytes, published: str):
        import hashlib
        from unittest import mock

        from dubchecker import media_probe
        source = media_probe.FfprobeSource("https://example.test/ff.zip", "https://example.test/ff.zip.sha256",
                                           "sha256")
        checksum = mock.Mock(text=published.replace("GOOD", hashlib.sha256(archive).hexdigest()))
        with mock.patch.object(media_probe, "ffprobe_sources", return_value=[source]), \
                mock.patch("requests.get", return_value=checksum), \
                mock.patch.object(media_probe, "_download", return_value=archive), \
                mock.patch.object(media_probe, "_extract_member") as extract, \
                mock.patch.object(media_probe.subprocess, "run") as run:
            try:
                return media_probe.download_ffprobe(self.bin), extract, run
            except media_probe.ProbeUnavailable:
                return None, extract, run

    def test_a_matching_download_is_used(self) -> None:
        path, extract, run = self.download(b"the real archive", "GOOD  ffmpeg-release-essentials.zip\n")
        self.assertTrue(path.endswith(ffprobe_name()))
        extract.assert_called_once()
        run.assert_called_once()

    def test_a_download_that_doesnt_match_is_never_run(self) -> None:
        with self.assertLogs("dubchecker.media_probe", "WARNING") as logs:
            path, extract, run = self.download(b"something else", "0" * 64)
        self.assertIsNone(path)
        extract.assert_not_called()
        run.assert_not_called()
        self.assertIn("doesn't match its published sha256 checksum", "\n".join(logs.output))

    def test_an_unreadable_checksum_is_never_run(self) -> None:
        with self.assertLogs("dubchecker.media_probe", "WARNING"):
            path, extract, run = self.download(b"archive", "<html>Not found</html>")
        self.assertIsNone(path)
        extract.assert_not_called()

    def test_checksum_files(self) -> None:
        from dubchecker.media_probe import ProbeError, published_checksum
        self.assertEqual(published_checksum("7FA72b652e19bf84c9461e332ea1cdf3  ffmpeg.tar.xz", "md5"),
                         "7fa72b652e19bf84c9461e332ea1cdf3")
        self.assertEqual(published_checksum("a" * 64 + "\n", "sha256"), "a" * 64)
        for bad in ("", "a" * 63, "a" * 65, "not a checksum"):
            with self.assertRaises(ProbeError):
                published_checksum(bad, "sha256")

    def test_only_sources_with_checksums(self) -> None:
        from unittest import mock

        from dubchecker import media_probe
        with mock.patch.object(media_probe.os, "name", "nt"):
            self.assertEqual([s.algorithm for s in media_probe.ffprobe_sources()], ["sha256"])
        with mock.patch.object(media_probe.os, "name", "posix"), \
                mock.patch.object(media_probe.sys, "platform", "linux"), \
                mock.patch.object(media_probe.platform, "machine", return_value="x86_64"):
            self.assertEqual([s.algorithm for s in media_probe.ffprobe_sources()], ["md5"])
        for system, machine in (("darwin", "arm64"), ("linux", "aarch64")):
            with mock.patch.object(media_probe.os, "name", "posix"), \
                    mock.patch.object(media_probe.sys, "platform", system), \
                    mock.patch.object(media_probe.platform, "machine", return_value=machine):
                self.assertEqual(media_probe.ffprobe_sources(), [])
                self.assertIn("no ffprobe download for this computer", media_probe._unavailable_message())


if __name__ == "__main__":
    unittest.main()
