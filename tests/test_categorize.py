import unittest

from dubchecker import ui_common as ui
from dubchecker.categorize import (STOPPED_NOTE, categorize_local, categorize_lookup, needs_lookup,
                                   stopped_result)
from dubchecker.dub_sources import LookupResult, MalDubs, combine_dub_signals
from dubchecker.models import AniListMatch, Category, FileResult, FileStatus, ShowGroup

D, J, E, U, X = (FileStatus.DUAL, FileStatus.ORIGINAL_ONLY, FileStatus.ENGLISH_ONLY, FileStatus.UNLABELED,
                 FileStatus.ERROR)


def files(*statuses: FileStatus) -> list[FileResult]:
    return [FileResult(f"/lib/Show/e{n}.mkv", status=s) for n, s in enumerate(statuses)]


GROUP = ShowGroup("Show|S2", "Show", "Show", season=2)
MATCH = AniListMatch(7, "Show Season 2", mal_id=70, confidence=95)


def lookup(match: AniListMatch | None = MATCH, va: bool | None = True, maldubs: str | None = "dubbed") -> LookupResult:
    return LookupResult(match, va, maldubs)


class LocalEvidenceTests(unittest.TestCase):
    """Seasons with at least one Japanese + English file skip AniList."""

    def test_needs_lookup(self) -> None:
        self.assertFalse(needs_lookup(files(J, D)))
        self.assertTrue(needs_lookup(files(J, J)))
        self.assertTrue(needs_lookup(files(E)))
        self.assertTrue(needs_lookup(files()))

    def test_every_file_has_english(self) -> None:
        result = categorize_local(GROUP, files(D, D, E))
        self.assertIs(result.category, Category.FULLY_DUBBED)
        self.assertEqual(ui.confirmed_text(result), "Your files")
        self.assertEqual(ui.anilist_text(result), "Not needed - your files have English audio")

    def test_some_japanese_only(self) -> None:
        result = categorize_local(GROUP, files(D, J, J))
        self.assertIs(result.category, Category.NEEDS_DUB)
        self.assertIn("2 of 3 episodes only have original-language audio", result.notes)

    def test_unlabeled_or_unreadable_needs_a_manual_check(self) -> None:
        self.assertIs(categorize_local(GROUP, files(D, U)).category, Category.CHECK)
        self.assertIs(categorize_local(GROUP, files(D, X)).category, Category.CHECK)

    def test_no_anilist_match_is_not_a_reason_to_check(self) -> None:
        result = categorize_local(GROUP, files(D))
        self.assertIsNone(result.match)
        self.assertIsNot(result.category, Category.CHECK)
        self.assertFalse(result.looked_up)

    def test_manual_match_is_shown(self) -> None:
        result = categorize_local(GROUP, files(D, J), match=MATCH)
        self.assertEqual(ui.anilist_text(result), "Show Season 2")
        self.assertIs(result.category, Category.NEEDS_DUB)


class LookupCategoryTests(unittest.TestCase):
    def test_dub_exists(self) -> None:
        result = categorize_lookup(GROUP, files(J, J), lookup(), 80)
        self.assertIs(result.category, Category.NEEDS_DUB)
        self.assertEqual(ui.confirmed_text(result), "AniList cast list, MAL-Dubs list")

    def test_no_dub(self) -> None:
        result = categorize_lookup(GROUP, files(J, J), lookup(va=False, maldubs="not_listed"), 80)
        self.assertIs(result.category, Category.NO_DUB)

    def test_one_signal_is_enough(self) -> None:
        self.assertIs(categorize_lookup(GROUP, files(J), lookup(va=False, maldubs="dubbed"), 80).category,
                      Category.NEEDS_DUB)
        self.assertIs(categorize_lookup(GROUP, files(J), lookup(va=True, maldubs=None), 80).category,
                      Category.NEEDS_DUB)

    def test_incomplete_dub_counts_with_a_note(self) -> None:
        result = categorize_lookup(GROUP, files(J), lookup(va=False, maldubs="incomplete"), 80)
        self.assertIs(result.category, Category.NEEDS_DUB)
        self.assertTrue(any("isn't complete" in n for n in result.notes))

    def test_nothing_could_be_checked(self) -> None:
        result = categorize_lookup(GROUP, files(J), lookup(va=None, maldubs=None), 80)
        self.assertIs(result.category, Category.CHECK)
        self.assertEqual(ui.confirmed_text(result), "Couldn't check")

    def test_unmatched_and_low_confidence(self) -> None:
        self.assertIs(categorize_lookup(GROUP, files(J), lookup(match=None), 80).category, Category.CHECK)
        weak = AniListMatch(7, "Maybe", confidence=70)
        result = categorize_lookup(GROUP, files(J), lookup(match=weak), 80)
        self.assertIs(result.category, Category.CHECK)
        self.assertTrue(any("70%" in n for n in result.notes))

    def test_unlabeled_files(self) -> None:
        self.assertIs(categorize_lookup(GROUP, files(J, U), lookup(), 80).category, Category.CHECK)

    def test_all_english_is_fully_dubbed(self) -> None:
        result = categorize_lookup(GROUP, files(E, E), lookup(match=None), 80)
        self.assertIs(result.category, Category.FULLY_DUBBED)
        self.assertIn("Your files", result.dub.sources)

    def test_stopped(self) -> None:
        result = stopped_result(GROUP, files(J))
        self.assertIs(result.category, Category.CHECK)
        self.assertEqual(result.notes, [STOPPED_NOTE])
        self.assertEqual(ui.anilist_text(result), "Not checked yet")


class DubSignalTests(unittest.TestCase):
    def test_combiner(self) -> None:
        self.assertEqual(combine_dub_signals(True, None, False).sources, ["AniList cast list"])
        self.assertTrue(combine_dub_signals(False, "dubbed", False).exists)
        self.assertTrue(combine_dub_signals(None, None, True).exists)
        self.assertEqual(combine_dub_signals(True, "dubbed", True).sources,
                         ["AniList cast list", "MAL-Dubs list", "Your files"])
        self.assertIs(combine_dub_signals(False, "not_listed", False).exists, False)
        self.assertIs(combine_dub_signals(False, None, False).exists, False)
        self.assertIsNone(combine_dub_signals(None, None, False).exists)
        self.assertTrue(combine_dub_signals(None, "incomplete", False).incomplete)

    def test_mal_dubs_status(self) -> None:
        maldubs = MalDubs.__new__(MalDubs)
        maldubs.available, maldubs.dubbed, maldubs.incomplete = True, {1}, {2}
        self.assertEqual([maldubs.status(i) for i in (1, 2, 3, None)], ["dubbed", "incomplete", "not_listed", None])
        maldubs.available = False
        self.assertIsNone(maldubs.status(1))


if __name__ == "__main__":
    unittest.main()
