"""Scanning only the shows in one tab, and scanning on a schedule."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import SCAN_SCOPES, Config, Overrides
from dubchecker.models import Category
from dubchecker.pipeline import ScanOutcome
from dubchecker.sonarr import SonarrClient
from tests.fakes import FakeLookup, FakeProber, make_pipeline, touch
from tests.test_gui import season, tk_available
from tests.test_sonarr import API_KEY, MockSonarr


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.lib, self.data = base / "Anime", base / "UserData"
        self.data.mkdir()
        self.cache = Cache(self.data / "cache.db")
        for show in ("A Show", "B Show", "C Show"):
            for number in (1, 2):
                touch(self.lib / show / "Season 01" / f"{show} - S01E0{number} [jpn].mkv")
        touch(self.lib / "Loose Show - 01 [jpn].mkv")

    def tearDown(self) -> None:
        self.cache.close()
        self._tmp.cleanup()

    def scan(self, only=None):
        log: list = []
        prober = FakeProber(log)
        pipeline, events = make_pipeline(self.data, self.cache, prober, FakeLookup(log))
        outcome = pipeline.run(root=str(self.lib), only=only)
        return outcome, prober, events

    def test_only_the_chosen_shows_are_listed_and_read(self) -> None:
        full, full_prober, _ = self.scan()
        self.assertEqual(full_prober.reads, 7)
        outcome, prober, events = self.scan(only={"B Show"})
        self.assertEqual({r.key for r in outcome.results}, {"B Show|S1"})
        self.assertEqual(prober.reads, 0)  # already read by the full scan: straight from the cache
        self.assertLess(outcome.stats.folders_listed, full.stats.folders_listed)  # other shows aren't listed
        explanation = next(e[2] for e in events if e[0] == "phase")
        self.assertIn("the 1 show you chose to scan", explanation)

    def test_loose_files_are_filtered_by_title(self) -> None:
        outcome, prober, _ = self.scan(only={"Loose Show", "C Show"})
        self.assertEqual({r.group.folder_name for r in outcome.results}, {"Loose Show", "C Show"})
        self.assertEqual(prober.reads, 3)

    def test_nothing_matching_is_an_empty_scan(self) -> None:
        outcome, prober, _ = self.scan(only={"Not There"})
        self.assertEqual((outcome.results, prober.reads, outcome.error), ([], 0, None))

    def test_sonarr_scan_asks_only_about_the_chosen_series(self) -> None:
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), MockSonarr)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = SonarrClient(f"http://127.0.0.1:{server.server_port}/sonarr", API_KEY)
            asked = []
            original = client.episode_files
            client.episode_files = lambda series_id: asked.append(series_id) or original(series_id)
            pipeline, _ = make_pipeline(self.data, self.cache, FakeProber([]), FakeLookup([]))
            outcome = pipeline.run(sonarr=client, only={"Frieren"})
            self.assertEqual(asked, [3])
            self.assertEqual({r.key for r in outcome.results}, {"Frieren|S1"})
        finally:
            server.shutdown()
            server.server_close()

    def test_scopes_are_the_tabs(self) -> None:
        self.assertEqual(SCAN_SCOPES, ("all", *ui.TAB_IDS))


@unittest.skipUnless(tk_available(), "no display for Tk")
class WindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.library = data / "Anime"
        self.library.mkdir()
        self.cache = Cache(data / "cache.db")
        self.ctx = AppContext(Config(theme="light", library_path=str(self.library)), data / "config.json",
                              Overrides(data / "o.json"), self.cache, data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.ctx)

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def fill(self) -> None:
        for r in (season("Alpha", 1, Category.FULLY_DUBBED), season("Alpha", 2, Category.NEEDS_DUB),
                  season("Beta", 1, Category.NEEDS_DUB), season("Gamma", 1, Category.FULLY_DUBBED)):
            self.app.model.add(r)
        self.app.results_source = "folder"
        self.app.update_cards()

    def start(self, scope: str, scheduled: bool = False):
        """Start a scan without really scanning: returns what the worker would have been given."""
        calls = []
        with mock.patch.object(self.app, "_scan_worker", lambda *args: calls.append(args)), \
                mock.patch("dubchecker.gui.messagebox.showinfo") as info:
            started = self.app.start_scan(scope, scheduled=scheduled)
            if self.app.worker is not None:
                self.app.worker.join(5)
        return started, (calls[0] if calls else None), info

    def finish(self, results, **outcome) -> None:
        self.app.queue.put(("results", (results,)))
        self.app.queue.put(("done", (ScanOutcome(**outcome),)))
        self.app._poll()

    # ---------------------------------------------------------------- the Scan: drop-down

    def test_drop_down_lists_the_tabs_with_counts_and_remembers_the_choice(self) -> None:
        app = self.app
        self.assertEqual(app.scope_box.get(), "Every show")
        self.fill()
        labels = list(app.scope_box.cget("values"))
        self.assertEqual(labels[:2], ["Every show", "Only Needs English Audio (2 shows)"])
        app.scope_box.current(1)
        app._on_scope_chosen()
        self.assertEqual(self.ctx.config.scan_scope, "NEEDS_DUB")

    def test_one_tab_needs_an_earlier_full_scan(self) -> None:
        started, worker_args, info = self.start("NEEDS_DUB")
        self.assertFalse(started)
        self.assertIsNone(worker_args)
        self.assertIn("scan every show from your local files once first", info.call_args.args[1])

    def test_an_empty_tab_has_nothing_to_scan(self) -> None:
        self.fill()
        started, _, info = self.start("NO_DUB")
        self.assertFalse(started)
        self.assertIn("No shows are in No Dub Exists", info.call_args.args[1])

    def test_one_tab_scan_covers_only_its_shows_and_keeps_the_rest(self) -> None:
        self.fill()
        started, worker_args, _ = self.start("NEEDS_DUB")
        self.assertTrue(started)
        self.assertEqual(worker_args[3], {"Alpha", "Beta"})  # (source, path, cancel, only)
        self.assertEqual(len(self.app.model), 4)  # nothing cleared
        # Alpha comes back with only Season 2 (Season 1's folder is gone), Beta is now Fully Dubbed.
        self.finish([season("Alpha", 2, Category.NEEDS_DUB), season("Beta", 1, Category.FULLY_DUBBED)])
        results = self.app.model.results
        self.assertEqual(sorted(results), ["Alpha|S2", "Beta|S1", "Gamma|S1"])
        self.assertIs(results["Beta|S1"].category, Category.FULLY_DUBBED)
        self.assertIn("rechecked 2 shows in Needs English Audio", self.app.status_var.get())
        self.assertIsNone(self.app.scan_only)

    def test_a_stopped_one_tab_scan_keeps_what_it_did_not_reach(self) -> None:
        from dubchecker.categorize import stopped_result
        self.fill()
        self.start("NEEDS_DUB")
        alpha2 = self.app.model.results["Alpha|S2"]
        self.finish([stopped_result(alpha2.group, alpha2.files)], stopped=True)
        self.assertFalse(self.app.model.results["Alpha|S2"].stopped)
        self.assertIn("Alpha|S1", self.app.model.results)  # not forgotten when stopped
        self.assertIn("Stopped", self.app.status_var.get())

    def test_a_full_scan_still_starts_afresh(self) -> None:
        self.fill()
        started, worker_args, _ = self.start("all")
        self.assertTrue(started)
        self.assertIsNone(worker_args[3])
        self.assertEqual(len(self.app.model), 0)

    # ---------------------------------------------------------------- the schedule

    def test_a_due_schedule_starts_its_scan(self) -> None:
        cfg = self.ctx.config
        cfg.schedule_enabled, cfg.schedule_scope, cfg.schedule_every_hours = True, "NEEDS_DUB", 6
        cfg.schedule_last_run = time.time() - 7 * 3600
        with mock.patch.object(self.app, "start_scan") as start:
            self.app._check_schedule()
        start.assert_called_once_with("NEEDS_DUB", scheduled=True)
        self.assertAlmostEqual(cfg.schedule_last_run, time.time(), delta=5)
        self.assertIn("Next scheduled scan (Needs English Audio):", self.app.schedule_label.cget("text"))
        self.assertIsNotNone(self.app._schedule_id)  # checks again later

    def test_nothing_happens_before_it_is_due_or_when_off(self) -> None:
        cfg = self.ctx.config
        cfg.schedule_enabled, cfg.schedule_last_run = True, time.time()
        with mock.patch.object(self.app, "start_scan") as start:
            self.app._check_schedule()
            cfg.schedule_enabled, cfg.schedule_last_run = False, 0.0
            self.app._check_schedule()
        start.assert_not_called()
        self.assertEqual(self.app.schedule_label.cget("text"), "")

    def test_a_scheduled_scan_that_cannot_run_says_so_quietly(self) -> None:
        started, _, info = self.start("NEEDS_DUB", scheduled=True)
        self.assertFalse(started)
        info.assert_not_called()  # nobody may be watching: no message box
        self.assertIn("The scheduled scan didn't run", self.app.status_var.get())

    def test_scheduled_warnings_go_to_the_status_line(self) -> None:
        self.fill()
        self.start("NEEDS_DUB", scheduled=True)
        with mock.patch("dubchecker.gui.messagebox.showwarning") as warn:
            self.finish([season("Alpha", 2, Category.NEEDS_DUB)], warnings=["Something to know"])
        warn.assert_not_called()
        self.assertIn("Scheduled scan: Done", self.app.status_var.get())
        self.assertIn("see logs/app.log", self.app.status_var.get())

    def test_settings_schedule_tab(self) -> None:
        from dubchecker.dialogs import SettingsDialog
        dialog = SettingsDialog(self.app, 3)
        self.assertIn("disabled", dialog.schedule_hours_spin.state())
        dialog.schedule_on.set(True)
        dialog._update_switches()
        self.assertNotIn("disabled", dialog.schedule_hours_spin.state())
        dialog.schedule_scope.set("Only English Only")
        dialog.schedule_hours.set("12")
        dialog._save()
        cfg = self.ctx.config
        self.assertEqual((cfg.schedule_enabled, cfg.schedule_scope, cfg.schedule_every_hours),
                         (True, "ENGLISH_ONLY", 12))
        self.assertAlmostEqual(cfg.schedule_last_run, time.time(), delta=5)  # first scan in 12 hours
        self.assertIn("Next scheduled scan (English Only):", self.app.schedule_label.cget("text"))


if __name__ == "__main__":
    unittest.main()
