"""Korean and Chinese shows are handled like Japanese ones: original language + English is dual audio."""
import tempfile
import unittest
from pathlib import Path

from dubchecker.categorize import categorize_local, file_notes
from dubchecker.media_probe import build_file_result, classify_language, language_from_tag
from dubchecker.models import FileResult, FileStatus, Lang, ShowGroup, language_word
from dubchecker.results_store import load_scan
from tests.mkv import write_mkv


def result(*languages: str, titles: tuple[str, ...] = ()) -> FileResult:
    titles = titles or ("",) * len(languages)
    raw = [{"index": i, "language": lang, "title": title} for i, (lang, title) in enumerate(zip(languages, titles))]
    return build_file_result("show/e.mkv", 0, 0, raw, True, "disk")


class TagTests(unittest.TestCase):
    def test_korean_and_chinese_tags(self) -> None:
        for tag in ("kor", "ko", "KO-kr", "Korean"):
            self.assertIs(language_from_tag(tag), Lang.KOREAN, tag)
        for tag in ("chi", "zho", "zh", "zh-CN", "zh-TW", "zh_Hant", "cmn", "yue", "Chinese", "Mandarin",
                    "Cantonese"):
            self.assertIs(language_from_tag(tag), Lang.CHINESE, tag)
        self.assertIsNone(language_from_tag("kok"))  # Konkani, not Korean

    def test_track_name_hints(self) -> None:
        for title in ("Korean 2.0", "한국어", "KOR"):
            self.assertIs(classify_language("und", title), Lang.KOREAN, title)
        for title in ("Mandarin", "Cantonese 5.1", "中文", "国语", "國語", "粵語", "普通话", "华语"):
            self.assertIs(classify_language("und", title), Lang.CHINESE, title)
        self.assertIs(classify_language("und", "Korean / Chinese"), Lang.UNKNOWN)  # conflicting
        self.assertIs(classify_language("und", "Chips"), Lang.UNKNOWN)  # a whole word is needed


class StatusTests(unittest.TestCase):
    def test_original_plus_english_is_dual(self) -> None:
        for original in ("kor", "chi", "zh-TW", "jpn"):
            self.assertIs(result(original, "eng").status, FileStatus.DUAL, original)

    def test_original_only(self) -> None:
        self.assertIs(result("kor").status, FileStatus.ORIGINAL_ONLY)
        self.assertIs(result("cmn", "yue").status, FileStatus.ORIGINAL_ONLY)
        self.assertIs(result("kor", "und").status, FileStatus.UNLABELED)

    def test_status_text_names_the_language(self) -> None:
        self.assertEqual(result("kor").status_text, "Korean only")
        self.assertEqual(result("zho", "eng").status_text, "Chinese + English")
        self.assertEqual(result("jpn", "eng").status_text, "Japanese + English")
        self.assertEqual(result("eng").status_text, "English only")
        self.assertEqual(FileResult("x", status=FileStatus.ORIGINAL_ONLY).status_text, "Original language only")

    def test_notes_name_the_language(self) -> None:
        self.assertEqual(language_word([result("kor"), result("kor")]), "Korean")
        self.assertEqual(language_word([result("kor"), result("jpn")]), "original-language")
        notes = file_notes([result("chi"), result("chi"), result("chi", "eng")])
        self.assertIn("2 of 3 episodes only have Chinese audio", notes)
        season = categorize_local(ShowGroup("Donghua|S1", "Donghua", "Donghua", season=1),
                                  [result("chi", "eng"), result("chi")])
        self.assertEqual(season.original_only, 1)
        self.assertEqual(season.dual, 1)


class RealFileTests(unittest.TestCase):
    def test_mediainfo_reads_korean_and_chinese(self) -> None:
        try:
            from pymediainfo import MediaInfo
            if not MediaInfo.can_parse():
                self.skipTest("MediaInfo isn't installed")
        except ImportError:
            self.skipTest("MediaInfo isn't installed")
        from dubchecker.media_probe import read_with_mediainfo
        with tempfile.TemporaryDirectory() as tmp:
            korean = write_mkv(Path(tmp) / "kr.mkv", [("kor", "Korean"), ("eng", "English")])
            chinese = write_mkv(Path(tmp) / "cn.mkv", [("chi", None)])
            self.assertEqual(build_file_result("", 0, 0, read_with_mediainfo(str(korean)), True, "disk").status_text,
                             "Korean + English")
            self.assertEqual(build_file_result("", 0, 0, read_with_mediainfo(str(chinese)), True, "disk").status_text,
                             "Chinese only")


class OldSavesTests(unittest.TestCase):
    def test_results_saved_before_the_rename_still_load(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_scan.json"
            path.write_text(json.dumps({"version": 1, "results": [{
                "group": {"key": "Show|S1", "folder_name": "Show", "search_title": "Show", "season": 1},
                "files": [{"path": "C:\\a.mkv", "status": "JAPANESE_ONLY",
                           "tracks": [{"index": 0, "language": "jpn", "lang": "JAPANESE"}]}],
                "category": "NEEDS_DUB"}]}), "utf-8")
            loaded = load_scan(path)
            self.assertIs(loaded.results[0].files[0].status, FileStatus.ORIGINAL_ONLY)
            self.assertEqual(loaded.results[0].files[0].status_text, "Japanese only")
            self.assertEqual(loaded.expanded_seasons, [])


if __name__ == "__main__":
    unittest.main()
