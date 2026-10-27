"""Dubs on their way: simulcast dubs trail the original by a few weeks, so recent original-only episodes go to
Dub Coming instead of Needs English Audio."""
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.categorize import categorize_local, categorize_lookup, dub_tag
from dubchecker.config import Config, Overrides
from dubchecker.dub_sources import LookupResult
from dubchecker.models import AirInfo, AniListMatch, Category, FileResult, FileStatus, ShowGroup, ShowResult
from dubchecker.results_store import SavedScan, load_scan, save_scan
from dubchecker.sonarr import SonarrClient
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch
from tests.test_gui import tk_available
from tests.test_sonarr import API_KEY, MockSonarr

D, O, E = FileStatus.DUAL, FileStatus.ORIGINAL_ONLY, FileStatus.ENGLISH_ONLY
NOW = 1_800_000_000
DAY = 86400


def season(show: str, number: int, *episodes, airing: bool = False, looked_up: bool = False) -> ShowResult:
    """``episodes`` are statuses, or (status, days since it aired)."""
    files = []
    for n, episode in enumerate(episodes, 1):
        status, days = episode if isinstance(episode, tuple) else (episode, None)
        files.append(FileResult(f"/lib/{show}/{show} - S{number:02d}E{n:02d}.mkv", status=status,
                                aired_at=NOW - days * DAY if days is not None else None))
    group = ShowGroup(f"{show}|S{number}", show, show, season=number)
    if looked_up:
        r = categorize_lookup(group, files, LookupResult(AniListMatch(7, show, confidence=100), True, None), 80)
    else:
        r = categorize_local(group, files)
    r.air = AirInfo("RELEASING" if airing else "FINISHED", source="Sonarr")
    return r


def timing(r: ShowResult, grace: int = 21) -> ShowResult:
    ui.mark_dub_timing(r, grace, now=NOW)
    return r


class WithDatesTests(unittest.TestCase):
    def test_recent_episodes_are_waiting_for_their_dub(self) -> None:
        r = timing(season("A", 1, (D, 30), (D, 23), (O, 16), (O, 9)))
        self.assertTrue(r.dub_coming)
        self.assertEqual(r.pending, {2, 3})
        self.assertEqual(r.dub_expected, NOW - 9 * DAY + 21 * DAY)  # 21 days after the newest one aired
        self.assertEqual(ui.group_tab(r), "DUB_COMING")
        self.assertEqual(ui.display_code(r), "ddpp")

    def test_an_old_missing_dub_keeps_it_in_needs_english_audio(self) -> None:
        r = timing(season("A", 1, (D, 60), (O, 40), (O, 5)))
        self.assertFalse(r.dub_coming)
        self.assertEqual(r.pending, {2})  # the newest one is still shown as on its way
        self.assertEqual(ui.group_tab(r), "NEEDS_DUB")

    def test_dates_win_over_air_status(self) -> None:
        # A finished show: its last episodes aired a week ago, so the dub can still be catching up.
        self.assertTrue(timing(season("A", 1, (D, 14), (O, 7), airing=False)).dub_coming)
        # Still airing, but an episode is months old: that dub is missing, not late.
        self.assertFalse(timing(season("A", 1, (D, 200), (O, 100), (O, 3), airing=True)).dub_coming)

    def test_an_episode_without_a_date_counts_as_missing(self) -> None:
        self.assertFalse(timing(season("A", 1, (D, 14), (O, 7), O)).dub_coming)

    def test_the_delay_setting(self) -> None:
        self.assertFalse(timing(season("A", 1, (D, 30), (O, 10)), grace=7).dub_coming)
        self.assertTrue(timing(season("A", 1, (D, 30), (O, 10)), grace=14).dub_coming)
        off = timing(season("A", 1, (D, 30), (O, 1)), grace=0)
        self.assertEqual((off.dub_coming, off.pending), (False, frozenset()))

    def test_only_needs_english_audio_is_affected(self) -> None:
        self.assertFalse(timing(season("A", 1, (D, 3), (D, 1))).dub_coming)  # nothing missing
        no_dub = ShowResult(ShowGroup("B", "B", "B"), [FileResult("b", status=O, aired_at=NOW)], Category.NO_DUB)
        self.assertFalse(timing(no_dub).dub_coming)


class WithoutDatesTests(unittest.TestCase):
    def test_airing_and_only_the_newest_episodes_lack_english(self) -> None:
        r = timing(season("A", 1, D, D, O, O, airing=True))
        self.assertTrue(r.dub_coming)
        self.assertIsNone(r.dub_expected)
        self.assertEqual(r.pending, {2, 3})
        self.assertEqual(ui.dub_coming_note(r), "Dub on its way - still airing, and the dub is catching up")

    def test_not_when_finished_or_the_dub_has_not_started_or_has_gaps(self) -> None:
        self.assertFalse(timing(season("A", 1, D, O, airing=False)).dub_coming)          # finished
        self.assertFalse(timing(season("A", 1, O, O, looked_up=True, airing=True)).dub_coming)  # no dub yet
        self.assertFalse(timing(season("A", 1, D, O, D, O, airing=True)).dub_coming)     # a gap, not the newest
        self.assertTrue(timing(season("A", 1, E, O, airing=True)).dub_coming)           # English-only counts


class TabsTagsAndSavingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ui.ResultsModel()
        self.model.dub_grace_days = 21
        for r in (season("Airing", 1, D, D, O, airing=True), season("Old", 1, D, O),
                  season("Both", 1, D, D), season("Both", 2, D, O, airing=True)):
            self.model.add(r)

    def test_tabs_and_counts(self) -> None:
        self.assertEqual([r.title for r in self.model.rows({"DUB_COMING"})], ["Airing", "Both"])
        self.assertEqual([r.title for r in self.model.rows({"NEEDS_DUB"})], ["Old"])
        counts = self.model.counts()
        self.assertEqual((counts["DUB_COMING"], counts["NEEDS_DUB"]), (2, 1))
        both = self.model.rows({"FULLY_DUBBED"})[0]
        self.assertEqual(both.also_text(), "Also: Season 2 is in Dub Coming")

    def test_the_sonarr_tag(self) -> None:
        shows = self.model.by_show()
        self.assertEqual(dub_tag(shows["Airing"]), "dub-pending")
        self.assertEqual(dub_tag(shows["Both"]), "dub-pending")
        self.assertEqual(dub_tag(shows["Old"]), "missing-dub")
        # A real missing dub in another season is the bigger problem.
        self.assertEqual(dub_tag(shows["Both"] + shows["Old"]), "missing-dub")

    def test_turning_it_off_moves_them_back(self) -> None:
        self.assertTrue(self.model.refresh_dub_timing(0))
        self.assertEqual(self.model.counts()["DUB_COMING"], 0)
        self.assertFalse(self.model.refresh_dub_timing(0))  # nothing changed this time

    def test_air_dates_are_saved(self) -> None:
        r = season("A", 1, (D, 30), (O, 3))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last_scan.json"
            save_scan(path, SavedScan([r]))
            loaded = load_scan(path).results[0]
        self.assertEqual([f.aired_at for f in loaded.files], [f.aired_at for f in r.files])
        self.assertFalse(loaded.dub_coming)  # worked out when shown, not saved


class AniListDatesLookup(FakeLookup):
    def __init__(self, log: list, schedule: dict[int, int]) -> None:
        super().__init__(log, dub=True)
        self.schedule = schedule

    def aired_episodes(self, anilist_id: int) -> dict[int, int]:
        self.log.append(("aired", anilist_id))
        return self.schedule


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockSonarr)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/sonarr"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.lib, self.data = base / "Anime", base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def test_dates_from_sonarr_by_file_name(self) -> None:
        local = self.lib / "tv"
        touch(local / "Attack on Titan (2013)" / "Season 02" / "AoT - S02E02 [jpn].mkv")
        log: list = []
        pipeline, events = make_pipeline(self.data, self.cache, FakeProber(log), FakeLookup(log),
                                         config=Config(probe_workers=1, air_status_source="off",
                                                       sonarr_path_from="/tv", sonarr_path_to=str(local)))
        outcome = pipeline.run(sonarr=SonarrClient(self.url, API_KEY))
        s2 = {r.key: r for r in outcome.results}["Attack on Titan (2013)|S2"]
        self.assertIs(s2.category, Category.NEEDS_DUB)
        expected = [int(datetime(2017, 4, d, 15, tzinfo=timezone.utc).timestamp()) for d in (1, 8)]
        self.assertEqual([f.aired_at for f in s2.files], expected)
        self.assertIn("Last step: Checking when the newest episodes aired", [e[1] for e in events if e[0] == "phase"])

    def test_dates_from_anilist_by_episode_number(self) -> None:
        for n in (1, 2):
            touch(self.lib / "Frieren" / "Season 01" / f"Frieren - S01E0{n} [jpn].mkv")
        log: list = []
        lookup = AniListDatesLookup(log, {1: 1_700_000_000, 2: 1_700_600_000, 3: 1_701_200_000})
        pipeline, _ = make_pipeline(self.data, self.cache, FakeProber(log), lookup)
        outcome = pipeline.run(root=str(self.lib))
        self.assertEqual([f.aired_at for f in outcome.results[0].files], [1_700_000_000, 1_700_600_000])
        self.assertIn(("aired", 1), log)

    def test_switched_off_skips_the_step(self) -> None:
        touch(self.lib / "Frieren" / "Season 01" / "Frieren - S01E01 [jpn].mkv")
        log: list = []
        pipeline, events = make_pipeline(self.data, self.cache, FakeProber(log), AniListDatesLookup(log, {1: 1}),
                                         config=Config(probe_workers=1, air_status_source="off", dub_grace_days=0))
        outcome = pipeline.run(root=str(self.lib))
        self.assertIsNone(outcome.results[0].files[0].aired_at)
        self.assertNotIn(("aired", 1), log)


@unittest.skipUnless(tk_available(), "no display for Tk")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        self.ctx = AppContext(Config(theme="light"), data / "config.json", Overrides(data / "o.json"), self.cache,
                              data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.ctx)
        self.app.model.add(season("Airing", 1, D, D, O, airing=True))
        self.app.update_cards()
        self.app.select_tabs({"DUB_COMING"})

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def test_tab_episode_rows_and_notes(self) -> None:
        app = self.app
        self.assertEqual(app.cards["DUB_COMING"].count.cget("text"), "1")
        self.assertEqual(app.cards["NEEDS_DUB"].count.cget("text"), "0")
        row = "s|Airing|S1"
        self.assertTrue(app.tree.set(row, "notes").startswith("Dub on its way"))
        app.toggle(row)
        last = app.tree.get_children(row)[-1]
        self.assertIn("audio_PENDING", app.tree.item(last, "tags"))
        self.assertTrue(app.tree.set(last, "notes").endswith("- dub on its way"))

    def test_changing_the_setting_moves_seasons(self) -> None:
        from dubchecker.dialogs import SettingsDialog
        dialog = SettingsDialog(self.app, 0)
        dialog.dub_grace.set("0")
        dialog._save()
        self.assertEqual(self.ctx.config.dub_grace_days, 0)
        self.assertEqual(self.app.cards["NEEDS_DUB"].count.cget("text"), "1")
        self.assertEqual(self.app.cards["DUB_COMING"].count.cget("text"), "0")

    def test_checked_again_every_hour(self) -> None:
        self.app.model.results["Airing|S1"].air = AirInfo("FINISHED")  # it finished; no dates, so no longer
        self.app._timing_hour -= 1
        with mock.patch.object(self.app, "start_scan"):
            self.app._check_schedule()
        self.assertEqual(self.app.cards["NEEDS_DUB"].count.cget("text"), "1")


if __name__ == "__main__":
    unittest.main()
