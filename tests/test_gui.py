"""The results model and the main window's Clear results button."""
import tempfile
import unittest
from pathlib import Path

from dubchecker import ui_common as ui
from dubchecker.gui import ARROW_CLOSED, ARROW_OPEN
from dubchecker.cache import MISSING, Cache
from dubchecker.categorize import categorize_local
from dubchecker.config import Config, Overrides
from dubchecker.models import AniListMatch, Category, FileResult, FileStatus, ShowGroup, ShowResult


def season(show: str, number: int | None, category: Category, episodes: int = 2) -> ShowResult:
    group = ShowGroup(f"{show}|S{number}", show, show, season=number)
    files = [FileResult(f"/lib/{show}/{n}.mkv", status=FileStatus.ORIGINAL_ONLY) for n in range(episodes)]
    return ShowResult(group, files, category, match=AniListMatch(number or 1, f"{show} AniList", confidence=90))


def tk_available() -> bool:
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


class ResultsModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ui.ResultsModel()
        for result in (season("Alpha", 1, Category.FULLY_DUBBED), season("Alpha", 2, Category.NEEDS_DUB),
                       season("Alpha", 3, Category.NEEDS_DUB), season("Beta", 1, Category.NEEDS_DUB),
                       season("Gamma", None, Category.CHECK)):
            self.model.add(result)

    def test_counts_are_shows_not_seasons(self) -> None:
        counts = self.model.counts()
        self.assertEqual(counts["NEEDS_DUB"], 2)
        self.assertEqual(counts["FULLY_DUBBED"], 1)
        self.assertEqual(counts["NO_DUB"], 0)

    def test_rows_group_seasons_under_their_show(self) -> None:
        rows = self.model.rows({"NEEDS_DUB"})
        self.assertEqual([r.title for r in rows], ["Alpha", "Beta"])
        alpha = rows[0]
        self.assertFalse(alpha.single)
        self.assertEqual(alpha.seasons_text(), "2 of 3 seasons")
        self.assertEqual(alpha.total("episodes"), 4)
        self.assertEqual(alpha.also_text(), "Also: Season 1 is in Fully Dubbed")
        self.assertTrue(rows[1].single)

    def test_a_show_appears_in_every_group_holding_one_of_its_seasons(self) -> None:
        self.assertEqual([r.title for r in self.model.rows({"FULLY_DUBBED"})], ["Alpha"])

    def test_filter(self) -> None:
        self.assertEqual([r.title for r in self.model.rows({"NEEDS_DUB"}, "bet")], ["Beta"])
        self.assertEqual([r.title for r in self.model.rows({"NEEDS_DUB"}, "ALPHA anilist")], ["Alpha"])
        self.assertEqual(self.model.rows({"NEEDS_DUB"}, "zzz"), [])

    def test_replace_only_existing(self) -> None:
        self.assertTrue(self.model.replace(season("Beta", 1, Category.NO_DUB)))
        self.assertFalse(self.model.replace(season("Delta", 1, Category.NO_DUB)))
        self.assertEqual(self.model.counts()["NO_DUB"], 1)


@unittest.skipUnless(tk_available(), "no display for Tk")
class ClearResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        self.cache.save_files([("\\Anime\\Show\\e1.mkv", 1, 1.0, [{"index": 0, "language": "jpn"}])])
        self.cache.save_anilist("search:show", {"data": {"Page": {"media": []}}})
        self.overrides = Overrides(data / "overrides.json")
        self.overrides.set("Show|S2", 4242)
        config = Config(library_path=str(data), theme="light")
        self.ctx = AppContext(config, data / "config.json", self.overrides, self.cache, data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.ctx)

    def tearDown(self) -> None:
        import tkinter as tk
        self.app.stop_polling()
        try:
            self.root.destroy()
        except tk.TclError:  # already closed by the test
            pass
        self.cache.close()
        self._tmp.cleanup()

    def fill(self) -> None:
        for result in (season("Alpha", 1, Category.NEEDS_DUB), season("Alpha", 2, Category.NEEDS_DUB),
                       categorize_local(ShowGroup("Beta", "Beta", "Beta"), [FileResult("b", status=FileStatus.DUAL)])):
            self.app.model.add(result)
        self.app.expanded.add("Alpha")
        self.app.update_cards()
        self.app.refresh_table()
        self.app.filter_var.set("al")
        self.app._show_progress("Reading audio tracks: file 3 of 9", 0.3, False)
        self.root.update_idletasks()

    def new_app(self):
        import tkinter as tk

        from dubchecker.gui import App
        root = tk.Tk()
        root.withdraw()
        return root, App(root, self.ctx)

    def test_clear_results_resets_the_window_but_keeps_saved_data(self) -> None:
        self.fill()
        self.app.save_results()
        self.assertTrue(self.app.results_path.exists())
        self.assertEqual(len(self.app.tree.get_children()), 1)
        self.assertEqual(self.app.cards["NEEDS_DUB"].count.cget("text"), "1")
        self.assertTrue(self.app.progress.winfo_manager())

        self.app.clear_results()
        self.root.update_idletasks()
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(len(self.app.model), 0)
        self.assertTrue(all(card.count.cget("text") == "-" for card in self.app.cards.values()))
        self.assertEqual(self.app.filter_var.get(), "")
        self.assertEqual(self.app.status_var.get(), "Ready.")
        self.assertEqual(self.app.progress.winfo_manager(), "")
        self.assertEqual(float(self.app.progress.cget("value")), 0.0)
        self.assertTrue(self.app.empty_text.startswith("Choose your anime folder"))
        # Nothing saved was touched.
        self.assertEqual(self.cache.file_count(), 1)
        self.assertIsNot(self.cache.get_anilist("search:show", 30), MISSING)
        self.assertEqual(Overrides(self.ctx.data_dir / "overrides.json").get("Show|S2"), 4242)
        # ...but the list itself is forgotten, so it doesn't come back after a restart.
        self.assertFalse(self.app.results_path.exists())

    def test_results_are_still_there_after_closing_and_reopening(self) -> None:
        self.fill()
        self.app.select_tabs({"FULLY_DUBBED"})
        self.app.on_close()  # the real close path saves
        root, app = self.new_app()
        try:
            self.assertEqual(len(app.model), 3)
            self.assertEqual(app.tabs, {"FULLY_DUBBED"})
            self.assertIn("Alpha", app.expanded)
            self.assertEqual(app.cards["NEEDS_DUB"].count.cget("text"), "1")
            self.assertEqual(app.tree.get_children(), ("s|Beta",))
            self.assertTrue(app.status_var.get().startswith("Showing your last scan from"))
            self.assertEqual(app.model.results["Alpha|S2"].files[0].status, FileStatus.ORIGINAL_ONLY)
        finally:
            app.stop_polling()
            root.destroy()

    def test_closing_mid_scan_keeps_the_finished_part(self) -> None:
        self.fill()
        self.app.scanning = True
        self.app.on_close()
        root, app = self.new_app()
        try:
            self.assertEqual(len(app.model), 3)
            self.assertIn("stopped before it finished", app.status_var.get())
        finally:
            app.stop_polling()
            root.destroy()

    def test_settings_window_opens_saves_and_encrypts_the_key(self) -> None:
        from dubchecker import dpapi
        from dubchecker.dialogs import SettingsDialog
        config = self.ctx.config
        config.api_key_unreadable = True  # as if saved on another PC
        dialog = SettingsDialog(self.app, 2)
        self.root.update()

        def label_texts(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "TLabel":
                    yield str(child.cget("text"))
                yield from label_texts(child)

        self.assertTrue(any("another PC" in text for text in label_texts(dialog)))
        dialog.sonarr_key.set("typed-in-key")
        dialog._save()
        self.assertEqual(config.sonarr_api_key, "typed-in-key")
        self.assertFalse(config.api_key_unreadable)
        if dpapi.available():
            self.assertNotIn("typed-in-key", self.ctx.config_path.read_text("utf-8"))
        self.assertNotIn("no API key", self.app.sonarr_label.cget("text"))  # the main window caught up

    def test_scan_source_is_called_local_files(self) -> None:
        self.assertEqual(self.app.source_buttons[0].cget("text"), "Local files")
        self.assertTrue(self.app.results_are_local)
        self.app.results_source = "sonarr"
        self.assertFalse(self.app.results_are_local)

    def test_open_file_location(self) -> None:
        from unittest import mock
        results = [season("Alpha", 1, Category.NEEDS_DUB), season("Alpha", 2, Category.NEEDS_DUB)]
        with mock.patch("dubchecker.gui.ui.open_file_location", return_value=True) as opener:
            self.app.open_location(results[:1], select_file=True)
            self.app.open_location(results, select_file=False)
        self.assertEqual([c.args[0] for c in opener.call_args_list], ["/lib/Alpha/0.mkv", ui.shared_folder(results)])

    def test_search_for_replacements_in_sonarr(self) -> None:
        import time
        from unittest import mock
        self.assertFalse(self.app.sonarr_configured)
        self.ctx.config.sonarr_api_key = "key"
        self.assertTrue(self.app.sonarr_configured)
        results = [season("Alpha", 2, Category.NEEDS_DUB)]
        with mock.patch("dubchecker.gui.messagebox.askyesno", return_value=True) as ask, \
                mock.patch("dubchecker.gui.search_for_replacements", return_value="Sonarr is searching.") as search:
            self.app.search_sonarr(results)
            for thread in self.app.helpers:
                thread.join(5)
            deadline = time.monotonic() + 5
            while "Sonarr is searching." not in self.app.status_var.get() and time.monotonic() < deadline:
                self.root.update()
        self.assertIn("2 episodes of Alpha (Season 2)", ask.call_args.args[1])
        self.assertEqual(search.call_args.args[1:], ("Alpha", "Alpha", [2], ["0.mkv", "1.mkv"]))
        self.assertIn("Sonarr is searching.", self.app.status_var.get())

    def test_clear_results_is_disabled_while_scanning(self) -> None:
        self.fill()
        self.app._set_scanning(True)
        self.assertIn("disabled", self.app.clear_button.state())
        self.app.clear_results()
        self.assertEqual(len(self.app.model), 3)
        self.app._set_scanning(False)
        self.assertNotIn("disabled", self.app.clear_button.state())

    def test_refresh_keeps_open_shows_and_selection(self) -> None:
        self.fill()
        self.app.filter_var.set("")
        self.assertTrue(self.app.tree.item("p|Alpha", "open"))
        self.app.tree.selection_set("s|Alpha|S2")
        self.app.model.add(season("Zeta", 1, Category.NEEDS_DUB))
        self.app.refresh_table()
        self.assertTrue(self.app.tree.item("p|Alpha", "open"))
        self.assertEqual(self.app.tree.selection(), ("s|Alpha|S2",))
        self.app.toggle("p|Alpha")
        self.assertFalse(self.app.tree.item("p|Alpha", "open"))
        self.assertTrue(self.app.tree.set("p|Alpha", "show").startswith(ARROW_CLOSED))

    def test_export_writes_utf8_csv_with_bom_one_row_per_season(self) -> None:
        from unittest import mock
        self.fill()
        self.app.filter_var.set("")
        target = self.ctx.data_dir / "export.csv"
        with mock.patch("dubchecker.gui.filedialog.asksaveasfilename", return_value=str(target)):
            self.app.export_csv()
        raw = target.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        lines = raw.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0].split(",")[:3], ["Group", "Show", "Season"])
        self.assertEqual(len(lines), 3)  # header + Alpha S1 + Alpha S2
        self.assertIn("Needs English Audio,Alpha,Season 2", lines[2])

    def test_closing_mid_scan_stops_the_worker_before_the_database_closes(self) -> None:
        import threading
        import time
        from unittest import mock

        from tests.fakes import FakeProber, touch
        library = self.ctx.data_dir / "Anime"
        for n in range(40):
            touch(library / f"Show {n}" / f"Show {n} - 01 [jpn].mkv")
        self.app.folder_var.set(str(library))
        slow = FakeProber([], on_read=lambda _n: time.sleep(0.05))
        with mock.patch("dubchecker.gui.MediaProber", return_value=slow):
            self.app.start_scan()
            deadline = time.monotonic() + 5
            while slow.reads < 3 and time.monotonic() < deadline:
                self.root.update()
            worker = self.app.worker
            self.app.cancel.set()  # what on_close() does before destroying the window
            self.app.join_workers(timeout=10)
        self.assertFalse(worker.is_alive())
        self.assertLess(slow.reads, 40)
        outcomes = []
        while not self.app.queue.empty():
            kind, payload = self.app.queue.get()
            if kind == "done":
                outcomes.append(payload[0])
        self.assertEqual(len(outcomes), 1)
        self.assertIsNone(outcomes[0].error)
        self.assertTrue(outcomes[0].stopped)
        self.assertIsInstance(self.app.closing, threading.Event)

    def test_sorting_moves_shows_and_keeps_season_order(self) -> None:
        self.fill()
        self.app.filter_var.set("")
        self.app.model.add(season("Zeta", 1, Category.NEEDS_DUB, episodes=9))
        self.app.refresh_table()
        self.app.sort_by("episodes")  # numbers sort largest first
        self.assertEqual(self.app.tree.get_children(), ("s|Zeta|S1", "p|Alpha"))
        self.assertEqual(self.app.tree.get_children("p|Alpha"), ("s|Alpha|S1", "s|Alpha|S2"))

    def mixed_season(self) -> ShowResult:
        from dubchecker.media_probe import build_file_result
        files = [build_file_result(f"/lib/Kr/Kr - S01E0{n}.mkv", 1, 1.0,
                                   [{"index": i, "language": lang} for i, lang in enumerate(langs)], True, "disk")
                 for n, langs in enumerate((["kor", "eng"], ["kor"], ["eng"], ["und"]), 1)]
        result = ShowResult(ShowGroup("Kr|S1", "Kr", "Kr", season=1), files, Category.NEEDS_DUB)
        self.app.model.add(result)
        self.app.refresh_table()
        return result

    def test_a_season_drops_down_to_its_episodes(self) -> None:
        tree = self.app.tree
        self.mixed_season()
        self.assertEqual(tree.get_children("s|Kr|S1"), ())  # added only when opened
        self.assertTrue(tree.set("s|Kr|S1", "show").startswith(ARROW_CLOSED))
        self.app.toggle("s|Kr|S1")
        episodes = tree.get_children("s|Kr|S1")
        self.assertEqual(len(episodes), 4)
        self.assertTrue(tree.set("s|Kr|S1", "show").startswith(ARROW_OPEN))
        first, second, third, fourth = episodes
        self.assertEqual(tree.set(first, "dual"), "✓")
        self.assertEqual(tree.set(second, "orig"), "✓")
        self.assertEqual(tree.set(second, "notes"), "Korean only")
        self.assertEqual(tree.set(third, "notes"), "English only")
        self.assertIn("audio track 1 has no language", tree.set(fourth, "notes"))
        self.assertIn("audio_ORIGINAL_ONLY", tree.item(second, "tags"))
        self.assertTrue(tree.set(first, "show").strip().endswith("Kr - S01E01"))
        self.assertTrue(tree.item(first, "image"))
        self.assertIs(self.app._file_for(second).status, FileStatus.ORIGINAL_ONLY)
        self.assertEqual(self.app._result_for(second).key, "Kr|S1")

    def test_audio_bars_have_one_block_per_episode(self) -> None:
        self.mixed_season()
        image_name = self.app.tree.item("s|Kr|S1", "image")[0]
        bar = self.app.bars.bar(self.app.model.results["Kr|S1"].audio_code)
        self.assertEqual(str(bar), image_name)
        pal = self.app.palette
        width = bar.width()
        colours = ["#%02x%02x%02x" % bar.get(int(width * (n + 0.5) / 4), 2) for n in range(4)]
        self.assertEqual(colours, [pal["green"], pal["red"], pal["blue"], pal["amber"]])
        self.assertTrue(all(label.cget("image") for label, _ in self.app.legend_labels))

    def test_arrow_keys_open_and_close(self) -> None:
        self.mixed_season()
        tree = self.app.tree
        tree.focus("s|Kr|S1")
        self.app._on_arrow_key(True)
        self.assertTrue(tree.item("s|Kr|S1", "open"))
        episode = tree.get_children("s|Kr|S1")[0]
        tree.focus(episode)
        self.app._on_arrow_key(False)  # from an episode, Left goes up to its season
        self.assertEqual(tree.focus(), "s|Kr|S1")
        self.app._on_arrow_key(False)
        self.assertFalse(tree.item("s|Kr|S1", "open"))

    def test_open_seasons_are_remembered(self) -> None:
        self.mixed_season()
        self.app.toggle("s|Kr|S1")
        self.app.on_close()
        root, app = self.new_app()
        try:
            self.assertIn("Kr|S1", app.expanded_seasons)
            self.assertEqual(len(app.tree.get_children("s|Kr|S1")), 4)
        finally:
            app.stop_polling()
            root.destroy()

    def test_sort_by_audio_puts_shows_needing_the_most_english_first(self) -> None:
        self.fill()
        self.app.filter_var.set("")
        self.mixed_season()  # 1 of 4 episodes original-only; Alpha is 4 of 4
        self.app.sort_by("audio")
        self.assertEqual(self.app.tree.get_children()[0], "p|Alpha")

    def test_search_nyaa_opens_the_browser(self) -> None:
        from unittest import mock
        with mock.patch("dubchecker.gui.ui.open_url") as opener:
            self.app.search_nyaa("Frieren - Beyond Journey's End")
        url = opener.call_args.args[0]
        self.assertTrue(url.startswith("https://nyaa.si/?"))
        self.assertIn("q=Frieren+Beyond+Journey%27s+End+dual+audio", url)
        self.assertIn("Nyaa", self.app.status_var.get())


@unittest.skipUnless(tk_available(), "no display for Tk")
class ClickTests(unittest.TestCase):
    """Clicking in the results table, with a real (off-screen) window so rows have positions."""

    tearDown = ClearResultsTests.tearDown
    mixed_season = ClearResultsTests.mixed_season

    def setUp(self) -> None:
        ClearResultsTests.setUp(self)
        self.root.geometry("1300x700+-4000+-4000")
        self.root.deiconify()
        self.result = self.mixed_season()
        self.root.update()

    def event(self, iid: str, column: str, dx: int = 4):
        from types import SimpleNamespace
        x, y, _, height = self.app.tree.bbox(iid, column)
        return SimpleNamespace(x=x + dx, y=y + height // 2, x_root=0, y_root=0)

    def test_clicking_the_audio_bar_or_the_arrow_opens_a_season(self) -> None:
        tree = self.app.tree
        self.assertEqual(self.app._on_click(self.event("s|Kr|S1", "#0", dx=40)), "break")
        self.assertTrue(tree.item("s|Kr|S1", "open"))
        self.assertEqual(self.app._on_click(self.event("s|Kr|S1", "#1", dx=6)), "break")  # on the arrow
        self.assertFalse(tree.item("s|Kr|S1", "open"))
        self.assertIsNone(self.app._on_click(self.event("s|Kr|S1", "#1", dx=160)))  # on the name: just selects
        self.assertFalse(tree.item("s|Kr|S1", "open"))

    def test_double_click_toggles_once(self) -> None:
        tree = self.app.tree
        on_arrow = self.event("s|Kr|S1", "#1", dx=6)
        self.app._on_click(on_arrow)          # first click of the double-click opens it...
        self.app._on_double_click(on_arrow)   # ...and the double-click doesn't close it again
        self.assertTrue(tree.item("s|Kr|S1", "open"))
        self.app._on_double_click(self.event("s|Kr|S1", "#1", dx=160))  # on the name
        self.assertFalse(tree.item("s|Kr|S1", "open"))

    def test_double_click_on_an_episode_opens_the_episode_window_at_it(self) -> None:
        from unittest import mock
        self.app.toggle("s|Kr|S1")
        self.root.update()
        episode = self.app.tree.get_children("s|Kr|S1")[1]
        with mock.patch.object(self.app, "show_details") as details:
            self.app._on_double_click(self.event(episode, "#3"))
        self.assertEqual(details.call_args.args, ("Kr|S1", self.result.files[1]))

    def test_episode_window_starts_at_the_chosen_file(self) -> None:
        from dubchecker.dialogs import EpisodeDetailsDialog
        dialog = EpisodeDetailsDialog(self.app, self.result, self.result.files[2].path)
        try:
            self.assertEqual(dialog.row_files[dialog.tree.selection()[0]], self.result.files[2].path)
        finally:
            dialog.close()

    def test_hover_highlight_and_hand_cursor(self) -> None:
        tree = self.app.tree
        self.app._on_motion(self.event("s|Kr|S1", "#0"))
        self.assertIn("hover", tree.item("s|Kr|S1", "tags"))
        self.assertEqual(str(tree.cget("cursor")), "hand2")
        self.app._on_motion(self.event("s|Kr|S1", "#3"))  # not over a toggle
        self.assertEqual(str(tree.cget("cursor")), "")
        self.app.refresh_table()  # survives the table being rebuilt
        self.assertIn("hover", tree.item("s|Kr|S1", "tags"))
        self.app._on_leave(None)
        self.assertNotIn("hover", tree.item("s|Kr|S1", "tags"))

    def test_tooltip_shows_cut_off_text_only(self) -> None:
        tree = self.app.tree
        self.result.notes = ["3 of 4 episodes need a closer look because their audio isn't labelled"]
        self.app.refresh_table()
        tree.column("notes", width=60)
        self.app.tooltip.cell = ("s|Kr|S1", "notes")
        self.app.tooltip._show(100, 100)
        self.assertIsNotNone(self.app.tooltip.window)
        self.app.tooltip.hide()
        tree.column("notes", width=900)
        self.app.tooltip.cell = ("s|Kr|S1", "notes")
        self.app.tooltip._show(100, 100)
        self.assertIsNone(self.app.tooltip.window)


class NyaaTests(unittest.TestCase):
    def test_query_is_cleaned_of_search_operators(self) -> None:
        self.assertEqual(ui.nyaa_query("Attack on Titan"), "Attack on Titan dual audio")
        self.assertEqual(ui.nyaa_query("Frieren - Beyond Journey's End"), "Frieren Beyond Journey's End dual audio")
        self.assertEqual(ui.nyaa_query("86 EIGHTY-SIX"), "86 EIGHTY-SIX dual audio")
        self.assertEqual(ui.nyaa_query('Show | "Name" (TV) -Extra'), "Show Name TV Extra dual audio")

    def test_url(self) -> None:
        from urllib.parse import parse_qs, urlparse
        url = urlparse(ui.nyaa_search_url("Spy x Family"))
        self.assertEqual((url.scheme, url.netloc), ("https", "nyaa.si"))
        self.assertEqual(parse_qs(url.query), {"f": ["0"], "c": ["1_0"], "q": ["Spy x Family dual audio"],
                                               "s": ["seeders"], "o": ["desc"]})

    def test_titles_include_romaji_when_different(self) -> None:
        r = season("Attack on Titan", 1, Category.NEEDS_DUB)
        r.match.romaji, r.match.english = "Shingeki no Kyojin", "Attack on Titan"
        self.assertEqual(ui.nyaa_titles([r]), ["Attack on Titan", "Shingeki no Kyojin"])
        self.assertEqual(ui.nyaa_titles([season("Solo", 1, Category.NEEDS_DUB)]), ["Solo"])
        self.assertEqual(ui.nyaa_titles([]), [])


if __name__ == "__main__":
    unittest.main()
