"""Tagging shows dual-audio, missing-dub or no-dub in Sonarr."""
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dubchecker.cache import Cache
from dubchecker.categorize import categorize_local, categorize_lookup, dub_tag, stopped_result
from dubchecker.config import Config, Overrides
from dubchecker.dub_sources import LookupResult
from dubchecker.models import AniListMatch, FileResult, FileStatus, ShowGroup, ShowResult
from dubchecker.pipeline import ScanOutcome
from dubchecker.sonarr import DUB_TAGS, SonarrClient, apply_dub_tags
from tests.test_gui import tk_available
from tests.test_sonarr import API_KEY, TAG_EDITS, TAGS, MockSonarr, reset_tags

D, O, E, U = FileStatus.DUAL, FileStatus.ORIGINAL_ONLY, FileStatus.ENGLISH_ONLY, FileStatus.UNLABELED


def files(show: str, number: int, statuses) -> tuple[ShowGroup, list[FileResult]]:
    group = ShowGroup(f"{show}|S{number}", show, show, season=number)
    return group, [FileResult(f"/lib/{show}/S{number}E{n}.mkv", status=s) for n, s in enumerate(statuses, 1)]


def local(show: str, number: int, *statuses: FileStatus) -> ShowResult:
    """A season decided from the files alone (it has dual-audio episodes)."""
    return categorize_local(*files(show, number, statuses))


def looked_up(show: str, number: int, dub_on_anilist: bool, *statuses: FileStatus) -> ShowResult:
    """A season with no dual-audio episodes, checked on AniList."""
    lookup = LookupResult(AniListMatch(1, show, confidence=100), anilist_va=dub_on_anilist, maldubs=None)
    return categorize_lookup(*files(show, number, statuses), lookup, threshold=80)


class RuleTests(unittest.TestCase):
    def test_every_episode_dual_audio(self) -> None:
        self.assertEqual(dub_tag([local("A", 1, D, D), local("A", 2, D)]), "dual-audio")

    def test_some_dual_some_japanese_only(self) -> None:
        self.assertEqual(dub_tag([local("A", 1, D, O, O)]), "missing-dub")
        self.assertEqual(dub_tag([local("A", 1, D, D), looked_up("A", 2, True, O, O)]), "missing-dub")

    def test_japanese_only_is_decided_by_anilist(self) -> None:
        self.assertEqual(dub_tag([looked_up("A", 1, True, O, O)]), "missing-dub")  # a dub exists
        self.assertEqual(dub_tag([looked_up("A", 1, False, O, O)]), "no-dub")      # it doesn't

    def test_dubbed_season_and_a_season_with_no_dub(self) -> None:
        # Season 2's Japanese-only episodes have no dub to get, so it isn't "missing" one.
        self.assertEqual(dub_tag([local("A", 1, D, D), looked_up("A", 2, False, O)]), "no-dub")

    def test_no_tag_when_unsure_or_outside_the_rules(self) -> None:
        self.assertIsNone(dub_tag([local("A", 1, D, U)]))  # unlabeled audio: Check Manually
        self.assertIsNone(dub_tag([local("A", 1, D, D), stopped_result(*files("A", 2, [O]))]))
        self.assertIsNone(dub_tag([]))

    def test_english_only_episodes(self) -> None:
        self.assertEqual(dub_tag([local("A", 1, D, E)]), "english-only")
        self.assertEqual(dub_tag([local("A", 1, D, D), looked_up("A", 2, True, E, E)]), "english-only")
        # A missing dub or no dub is the bigger problem, and a show gets one tag.
        self.assertEqual(dub_tag([local("A", 1, D, E, O)]), "missing-dub")
        self.assertEqual(dub_tag([local("A", 1, D, E), looked_up("A", 2, False, O)]), "no-dub")


class SonarrTaggingTests(unittest.TestCase):
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
        reset_tags()  # "anime" (1) and "no-dub" (2) exist; Attack on Titan has both

    def test_tags_are_created_added_and_swapped(self) -> None:
        report = apply_dub_tags(SonarrClient(self.url, API_KEY), {
            ("Some local folder", "Attack on Titan"): "missing-dub",  # found by title
            ("Frieren", "Frieren"): "dual-audio",
            ("Not In Sonarr", "Not In Sonarr"): "no-dub"})
        self.assertEqual([t["label"] for t in TAGS], ["anime", "no-dub", "dual-audio", "missing-dub"])
        ids = {t["label"]: t["id"] for t in TAGS}
        self.assertCountEqual(TAG_EDITS, [
            {"seriesIds": [1], "tags": [ids["no-dub"]], "applyTags": "remove"},  # its old dub tag goes
            {"seriesIds": [1], "tags": [ids["missing-dub"]], "applyTags": "add"},
            {"seriesIds": [3], "tags": [ids["dual-audio"]], "applyTags": "add"}])
        self.assertEqual(report.tagged, {"dual-audio": 1, "missing-dub": 1, "no-dub": 0, "english-only": 0,
                                         "dub-pending": 0})
        self.assertEqual((report.changed, report.not_in_sonarr), (2, 1))
        self.assertEqual(report.text(), "Sonarr tags: 1 dual-audio, 1 missing-dub, 0 no-dub, 0 english-only, "
                                        "0 dub-pending (2 shows changed). 1 not found in Sonarr.")

    def test_nothing_changes_when_the_tag_is_already_right(self) -> None:
        report = apply_dub_tags(SonarrClient(self.url, API_KEY), {("Attack on Titan (2013)", "x"): "no-dub"})
        self.assertEqual((TAG_EDITS, report.changed), ([], 0))

    def test_no_tag_takes_the_dub_tags_away_but_leaves_others(self) -> None:
        apply_dub_tags(SonarrClient(self.url, API_KEY), {("Attack on Titan (2013)", "x"): None})
        self.assertEqual(TAG_EDITS, [{"seriesIds": [1], "tags": [2], "applyTags": "remove"}])  # "anime" stays
        self.assertEqual(len(TAGS), 2)  # nothing created that isn't used

    def test_tag_names(self) -> None:
        self.assertEqual(DUB_TAGS, ("dual-audio", "missing-dub", "no-dub", "english-only", "dub-pending"))

    def test_english_only_tag_is_created_and_swapped_in(self) -> None:
        apply_dub_tags(SonarrClient(self.url, API_KEY), {("Attack on Titan (2013)", "x"): "english-only"})
        self.assertEqual(TAGS[-1]["label"], "english-only")
        self.assertCountEqual(TAG_EDITS, [{"seriesIds": [1], "tags": [2], "applyTags": "remove"},
                                          {"seriesIds": [1], "tags": [TAGS[-1]["id"]], "applyTags": "add"}])


@unittest.skipUnless(tk_available(), "no display for Tk")
class WindowTests(unittest.TestCase):
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
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        reset_tags()
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        self.ctx = AppContext(Config(theme="light", sonarr_url=self.url, sonarr_api_key=API_KEY),
                              data / "config.json", Overrides(data / "o.json"), self.cache, data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.ctx)
        for r in (local("Attack on Titan (2013)", 1, D, O), local("Frieren", 1, D, D),
                  looked_up("Mystery", 1, False, O), local("Cowboy Bebop", 1, E, E)):
            self.app.model.add(r)

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def wait(self) -> None:
        for thread in self.app.helpers:
            thread.join(10)
        self.app._poll()

    def test_button_only_shows_when_sonarr_is_set_up(self) -> None:
        self.assertTrue(self.app.tag_button.winfo_manager())
        self.ctx.config.sonarr_api_key = ""
        self.app.update_sonarr_summary()
        self.assertEqual(self.app.tag_button.winfo_manager(), "")

    def test_tagging_asks_first_and_reports(self) -> None:
        self.app.dismiss_show("Mystery", "Mystery")  # dismissed shows are left alone
        with mock.patch("dubchecker.gui.messagebox.askyesno", return_value=True) as ask:
            self.app.tag_in_sonarr()
            self.wait()
        question = ask.call_args.args[1]
        self.assertIn("dual-audio: 1 show", question)
        self.assertIn("missing-dub: 1 show", question)
        self.assertIn("no-dub: 0 shows", question)
        self.assertIn("english-only: 1 show", question)
        self.assertEqual(self.app.status_var.get(), "Sonarr tags: 1 dual-audio, 1 missing-dub, 0 no-dub, "
                                                    "0 english-only, 0 dub-pending (2 shows changed). "
                                                    "1 not found in Sonarr.")
        self.assertEqual(len(TAG_EDITS), 3)

    def test_saying_no_changes_nothing(self) -> None:
        with mock.patch("dubchecker.gui.messagebox.askyesno", return_value=False):
            self.app.tag_in_sonarr()
            self.wait()
        self.assertEqual(TAG_EDITS, [])

    def test_tags_after_every_scan_when_switched_on(self) -> None:
        with mock.patch.object(self.app, "tag_in_sonarr") as tag:
            self.app._scan_finished(ScanOutcome())
            tag.assert_not_called()
            self.ctx.config.sonarr_tag_after_scan = True
            self.app._scan_finished(ScanOutcome(stopped=True))  # not after a stopped scan
            tag.assert_not_called()
            self.app._scan_finished(ScanOutcome())
        tag.assert_called_once_with(automatic=True)

    def test_automatic_tagging_reports_problems_quietly(self) -> None:
        self.ctx.config.sonarr_api_key = "wrong"
        with mock.patch("dubchecker.gui.messagebox.showerror") as error:
            self.app.tag_in_sonarr(automatic=True)
            self.wait()
        error.assert_not_called()
        self.assertIn("Couldn't tag shows in Sonarr", self.app.status_var.get())

    def test_settings_switch(self) -> None:
        from dubchecker.dialogs import SettingsDialog
        dialog = SettingsDialog(self.app, 2)
        dialog.tag_after_scan.set(True)
        dialog._save()
        self.assertTrue(self.ctx.config.sonarr_tag_after_scan)


if __name__ == "__main__":
    unittest.main()
