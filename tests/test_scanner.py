"""Title cleaning, season parsing, grouping and library batching."""
import os
import tempfile
import unittest
from pathlib import Path

from dubchecker.models import strip_id_tags
from dubchecker.scanner import (FolderLister, clean_title, group_loose_files, group_show, list_library,
                                parse_release_name, season_from_filename, season_from_folder)


def touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


class CleanTitleTests(unittest.TestCase):
    def check(self, name: str, title: str, year: int | None = None, season: int | None = None) -> None:
        cleaned = clean_title(name)
        self.assertEqual((cleaned.title, cleaned.year, cleaned.season), (title, year, season), name)

    def test_plex_and_sonarr_names(self) -> None:
        self.check("Attack on Titan (2013) {tvdb-267440}", "Attack on Titan", 2013)
        self.check("86 EIGHTY-SIX (2021) [imdbid-tt13718450]", "86 EIGHTY-SIX", 2021)
        self.check("Frieren - Beyond Journey's End (2023) {tmdb-209867}", "Frieren - Beyond Journey's End", 2023)

    def test_hyphenated_title_survives(self) -> None:
        self.check("86 EIGHTY-SIX", "86 EIGHTY-SIX")
        self.check("Re-Kan!", "Re-Kan!")

    def test_scene_names(self) -> None:
        self.check("Shingeki.no.Kyojin.S01.1080p.BluRay.x264-GROUP", "Shingeki no Kyojin", season=1)
        self.check("Mob.Psycho.100.2016.1080p.WEB-DL.AAC2.0.H.264-GRP", "Mob Psycho 100", 2016)
        self.check("86.EIGHTY-SIX.S01.1080p.WEB", "86 EIGHTY-SIX", season=1)
        self.check("Some_Show_Name_2019", "Some Show Name", 2019)

    def test_release_group_names(self) -> None:
        self.check("[Judas] Vinland Saga (Season 2) [1080p][HEVC x265 10bit][Dual-Audio]", "Vinland Saga", season=2)
        self.check("Cowboy Bebop 1080p BluRay x265-GROUP", "Cowboy Bebop")
        self.check("Bocchi the Rock! Dual Audio", "Bocchi the Rock!")

    def test_season_suffixes(self) -> None:
        self.check("Spy x Family Season 2", "Spy x Family", season=2)
        self.check("Oshi no Ko 2nd Season", "Oshi no Ko", season=2)
        self.check("Oshi no Ko S2", "Oshi no Ko", season=2)
        self.check("Dr. Stone: Season 3", "Dr. Stone", season=3)

    def test_years(self) -> None:
        self.check("2001 Nights", "2001 Nights")  # a year as the first word is part of the title
        self.check("Cowboy Bebop 1998", "Cowboy Bebop", 1998)
        self.check("Blade Runner 2049", "Blade Runner 2049")  # not a plausible year

    def test_noise_word_at_start_is_kept(self) -> None:
        self.check("Opus.COLORs", "Opus COLORs")

    def test_strip_id_tags(self) -> None:
        self.assertEqual(strip_id_tags("Show {tvdb-123}"), "Show")
        self.assertEqual(strip_id_tags("Show [imdbid-tt0123] (2020)"), "Show (2020)")
        self.assertEqual(strip_id_tags("[Group] Show"), "[Group] Show")


class SeasonParsingTests(unittest.TestCase):
    def test_season_folders(self) -> None:
        cases = {"Season 01": 1, "Season 1": 1, "season 12": 12, "S 2": 2, "S02": 2, "Specials": 0, "Extras": 0,
                 "OVA": 0, "Season 00": 0, "Season 2 {tvdb-1}": 2, "Soundtrack": None, "Some Show": None,
                 "Sonic": None}
        for name, season in cases.items():
            self.assertEqual(season_from_folder(name), season, name)

    def test_season_from_filename(self) -> None:
        self.assertEqual(season_from_filename("Show - S02E05.mkv"), 2)
        self.assertEqual(season_from_filename("show.s1e1.mkv"), 1)
        self.assertEqual(season_from_filename("Show S03 E07.mkv"), 3)
        self.assertIsNone(season_from_filename("Show - 05.mkv"))
        self.assertIsNone(season_from_filename("Mars2E05.mkv"))

    def test_release_names(self) -> None:
        parsed = parse_release_name("[SubsPlease] Bocchi the Rock! - 01 (1080p) [ABCD1234].mkv")
        self.assertEqual((parsed.title, parsed.season), ("Bocchi the Rock!", None))
        parsed = parse_release_name("[Erai-raws] Oshi no Ko 2nd Season - 03 [1080p].mkv")
        self.assertEqual((parsed.title, parsed.season), ("Oshi no Ko", 2))
        parsed = parse_release_name("Frieren.S01E03.1080p.mkv")
        self.assertEqual((parsed.title, parsed.season), ("Frieren", 1))


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_groups_by_season_folder(self) -> None:
        show = self.root / "Show A (2020) {tvdb-5}"
        touch(show / "Season 01" / "Show A - S01E01.mkv")
        touch(show / "Season 01" / "Show A - S01E02.mkv")
        touch(show / "Season 02" / "Show A - S02E01.mkv")
        touch(show / "Specials" / "Show A - S00E01.mkv")
        [folder] = list_library(str(self.root), FolderLister())
        groups = group_show(folder, FolderLister())
        self.assertEqual([g.key for g in groups],
                         ["Show A (2020) {tvdb-5}|S1", "Show A (2020) {tvdb-5}|S2", "Show A (2020) {tvdb-5}|S0"])
        self.assertEqual([len(g.files) for g in groups], [2, 1, 1])
        self.assertEqual((groups[0].search_title, groups[0].year), ("Show A", 2020))
        self.assertEqual(groups[2].display_title, "Show A - Specials")

    def test_groups_by_file_name_without_season_folders(self) -> None:
        touch(self.root / "Show B" / "Show B - S01E01.mkv")
        touch(self.root / "Show B" / "Show B - S02E01.mkv")
        touch(self.root / "Show C" / "Show C - 01.mkv")
        groups = [g for show in list_library(str(self.root), FolderLister()) for g in group_show(show, FolderLister())]
        self.assertEqual([g.key for g in groups], ["Show B|S1", "Show B|S2", "Show C"])
        self.assertIsNone(groups[2].season)

    def test_season_in_the_folder_name(self) -> None:
        touch(self.root / "Spy x Family Season 2" / "Spy x Family - 01.mkv")
        [group] = group_show(list_library(str(self.root), FolderLister())[0], FolderLister())
        self.assertEqual((group.key, group.search_title, group.season), ("Spy x Family Season 2|S2", "Spy x Family", 2))

    def test_skips_hidden_and_non_video_files(self) -> None:
        show = self.root / "Show D"
        touch(show / "Show D - 01.mkv")
        touch(show / "._Show D - 01.mkv")
        touch(show / ".hidden.mkv")
        touch(show / "cover.jpg")
        touch(show / "Show D - 02.MP4")
        touch(show / "@eaDir" / "Show D - 01.mkv")
        [group] = group_show(list_library(str(self.root), FolderLister())[0], FolderLister())
        self.assertEqual(sorted(f.name for f in group.files), ["Show D - 01.mkv", "Show D - 02.MP4"])

    def test_batches_are_a_to_z_with_loose_files_last(self) -> None:
        for name in ("b show", "A Show", "c show", "10 show", "9 show"):
            touch(self.root / name / "ep 01.mkv")
        touch(self.root / "[SubsPlease] Loose Show - 01 (1080p).mkv")
        shows = list_library(str(self.root), FolderLister())
        self.assertEqual([s.name for s in shows], ["9 show", "10 show", "A Show", "b show", "c show", "Loose files"])
        self.assertIsNotNone(shows[-1].loose_files)

    def test_root_with_season_folders_is_one_show(self) -> None:
        show = self.root / "Frieren (2023)"
        touch(show / "Season 01" / "Frieren - S01E01.mkv")
        touch(show / "Frieren - S00E01.mkv")
        shows = list_library(str(show), FolderLister())
        self.assertEqual([s.name for s in shows], ["Frieren (2023)"])
        groups = group_show(shows[0], FolderLister())
        self.assertEqual([g.key for g in groups], ["Frieren (2023)|S1", "Frieren (2023)|S0"])

    def test_loose_files_are_grouped_by_title_and_season(self) -> None:
        files = [touch(self.root / n) for n in (
            "[SubsPlease] Bocchi the Rock! - 01 (1080p).mkv", "[SubsPlease] Bocchi the Rock! - 02 (1080p).mkv",
            "[Erai-raws] Oshi no Ko 2nd Season - 01 [1080p].mkv")]
        listing = FolderLister()(str(self.root))
        self.assertEqual(len(listing.files), len(files))
        groups = group_loose_files(listing.files)
        self.assertEqual([(g.key, len(g.files)) for g in groups], [("Bocchi the Rock!", 2), ("Oshi no Ko|S2", 1)])

    def test_listing_counts_folders(self) -> None:
        touch(self.root / "Show" / "Season 01" / "e.mkv")
        lister = FolderLister()
        for show in list_library(str(self.root), lister):
            group_show(show, lister)
        self.assertEqual(lister.folders_listed, 3)

    def test_listing_uses_scandir_sizes(self) -> None:
        path = touch(self.root / "Show" / "e.mkv", size=1234)
        [video] = FolderLister()(str(self.root / "Show")).files
        self.assertEqual(video.size, 1234)
        self.assertAlmostEqual(video.mtime, os.path.getmtime(path), places=3)


if __name__ == "__main__":
    unittest.main()
