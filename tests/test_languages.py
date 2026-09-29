import unittest

from dubchecker.media_probe import _pick_language, classify_file, classify_language, is_commentary, make_tracks
from dubchecker.models import FileStatus, Lang


def raw(language: str | None, title: str = "") -> dict:
    return {"language": language, "title": title}


class ClassifyLanguageTests(unittest.TestCase):
    def test_japanese_tags(self) -> None:
        for tag in ("jpn", "ja", "JP", "Japanese", "ja-JP", "JPN"):
            self.assertIs(classify_language(tag), Lang.JAPANESE, tag)

    def test_english_tags(self) -> None:
        for tag in ("eng", "en", "EN-us", "English", "en_GB"):
            self.assertIs(classify_language(tag), Lang.ENGLISH, tag)

    def test_unknown_tags(self) -> None:
        for tag in ("und", "", None, "fre", "spa", "zxx"):
            self.assertIs(classify_language(tag), Lang.UNKNOWN, tag)

    def test_title_hints_when_tag_is_missing_or_unknown(self) -> None:
        self.assertIs(classify_language("und", "English 5.1"), Lang.ENGLISH)
        self.assertIs(classify_language(None, "eng stereo"), Lang.ENGLISH)
        self.assertIs(classify_language("", "Japanese"), Lang.JAPANESE)
        self.assertIs(classify_language("und", "jpn 2.0"), Lang.JAPANESE)
        self.assertIs(classify_language(None, "日本語"), Lang.JAPANESE)
        self.assertIs(classify_language("fre", "English dub"), Lang.ENGLISH)

    def test_conflicting_title_hints_stay_unknown(self) -> None:
        self.assertIs(classify_language("und", "English / Japanese"), Lang.UNKNOWN)
        self.assertIs(classify_language(None, "日本語 + English"), Lang.UNKNOWN)

    def test_hint_needs_a_whole_word(self) -> None:
        self.assertIs(classify_language("und", "Engaged audio"), Lang.UNKNOWN)
        self.assertIs(classify_language("und", "Stereo"), Lang.UNKNOWN)

    def test_tag_wins_over_title(self) -> None:
        self.assertIs(classify_language("jpn", "English"), Lang.JAPANESE)

    def test_commentary(self) -> None:
        self.assertTrue(is_commentary("Director's Commentary"))
        self.assertTrue(is_commentary("commentary"))
        self.assertFalse(is_commentary("English"))
        self.assertFalse(is_commentary(None))

    def test_pick_language_prefers_a_recognised_three_letter_code(self) -> None:
        self.assertEqual(_pick_language("ja", ["Japanese", "ja", "jpn"]), "jpn")
        self.assertEqual(_pick_language("en", None), "en")
        self.assertEqual(_pick_language(None, None), "")
        self.assertEqual(_pick_language("de", ["German", "deu"]), "de")


class ClassifyFileTests(unittest.TestCase):
    def status(self, *tracks: dict, ignore: bool = True) -> FileStatus:
        return classify_file(make_tracks(list(tracks), ignore))

    def test_dual(self) -> None:
        self.assertIs(self.status(raw("jpn"), raw("eng")), FileStatus.DUAL)

    def test_dual_as_soon_as_both_are_present(self) -> None:
        self.assertIs(self.status(raw("jpn"), raw("eng"), raw("und")), FileStatus.DUAL)

    def test_single_language(self) -> None:
        self.assertIs(self.status(raw("jpn")), FileStatus.ORIGINAL_ONLY)
        self.assertIs(self.status(raw("eng"), raw("en")), FileStatus.ENGLISH_ONLY)

    def test_unknown_track_makes_it_unlabeled(self) -> None:
        self.assertIs(self.status(raw("jpn"), raw("und")), FileStatus.UNLABELED)
        self.assertIs(self.status(raw("und")), FileStatus.UNLABELED)
        self.assertIs(self.status(raw("fre")), FileStatus.UNLABELED)

    def test_no_audio_is_unlabeled(self) -> None:
        self.assertIs(self.status(), FileStatus.UNLABELED)

    def test_title_hint_is_used(self) -> None:
        self.assertIs(self.status(raw("und", "Japanese"), raw("und", "English")), FileStatus.DUAL)

    def test_commentary_is_ignored(self) -> None:
        tracks = (raw("jpn", "Japanese"), raw("eng", "Commentary with the director"))
        self.assertIs(self.status(*tracks), FileStatus.ORIGINAL_ONLY)
        self.assertIs(self.status(*tracks, ignore=False), FileStatus.DUAL)
        self.assertTrue(make_tracks(list(tracks), True)[1].ignored)

    def test_only_commentary_counts_as_no_audio(self) -> None:
        self.assertIs(self.status(raw("eng", "Commentary")), FileStatus.UNLABELED)


if __name__ == "__main__":
    unittest.main()
