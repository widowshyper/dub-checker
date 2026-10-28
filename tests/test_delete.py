"""Deleting selected shows from Sonarr: entirely, their episodes and unmonitoring them, or just their episodes."""
import json
import tempfile
import unittest
from pathlib import Path

from dubchecker import ui_common as ui
from dubchecker.cache import Cache
from dubchecker.config import Config, Overrides
from dubchecker.models import Category, FileResult, FileStatus, ShowGroup, ShowResult, season_key
from dubchecker.sonarr import (DELETE_FILES, DELETE_FILES_UNMONITOR, DELETE_SHOW, DeleteTarget, SonarrClient,
                               delete_from_sonarr)

GB = 1024 ** 3
SERIES = [{"id": 1, "title": "Attack on Titan", "path": "/tv/Attack on Titan (2013)",
           "statistics": {"sizeOnDisk": 40 * GB}},
          {"id": 2, "title": "Frieren", "path": "/tv/Frieren"},
          {"id": 3, "title": "Broken", "path": "/tv/Broken"}]
FILES = {1: [{"id": 11, "seasonNumber": 1, "size": GB}, {"id": 12, "seasonNumber": 2, "size": GB},
            {"id": 13, "seasonNumber": 2, "size": GB // 2}],
         2: [{"id": 21, "seasonNumber": 1}], 3: [{"id": 31, "seasonNumber": 1}]}
EPISODES = {1: [{"id": 101, "seasonNumber": 1}, {"id": 102, "seasonNumber": 2}, {"id": 103, "seasonNumber": 3}],
            2: [{"id": 201, "seasonNumber": 1}], 3: [{"id": 301, "seasonNumber": 1}]}


class Response:
    def __init__(self, status: int, body: object = None) -> None:
        self.status_code = status
        self.content = b"" if body is None else json.dumps(body).encode()

    def json(self) -> object:
        return json.loads(self.content)


class FakeSession:
    """Sonarr's API, recording every change it's asked for. Series 3 refuses to delete its files."""

    def __init__(self) -> None:
        self.changes: list[tuple] = []

    def request(self, method, url, params=None, json=None, timeout=None, headers=None):  # noqa: A002
        path = url.split("/api/v3", 1)[1]
        query = params or {}
        if method == "GET":
            if path == "/series":
                return Response(200, SERIES)
            if path.startswith("/series/"):
                sid = int(path.rsplit("/", 1)[1])
                return Response(200, {"id": sid, "monitored": True, "seasons": [
                    {"seasonNumber": n, "monitored": True} for n in (0, 1, 2, 3)]})
            if path == "/episodefile":
                return Response(200, FILES[int(query["seriesId"])])
            if path == "/episode":
                return Response(200, EPISODES[int(query["seriesId"])])
        if method == "DELETE" and path == "/episodefile/bulk" and 31 in json["episodeFileIds"]:
            return Response(500, {"message": "disk is read-only"})
        self.changes.append((method, path, query, json))
        return Response(200)  # Sonarr answers changes with an empty body


def season(folder: str, number: int | None, title: str = "") -> ShowResult:
    return ShowResult(ShowGroup(season_key(folder, number), folder, title or folder, season=number),
                      [FileResult("e.mkv", status=FileStatus.DUAL)], Category.FULLY_DUBBED)


class DeleteInSonarrTests(unittest.TestCase):
    def run_delete(self, targets: list[DeleteTarget], action: str):
        session = FakeSession()
        report = delete_from_sonarr(SonarrClient("http://sonarr", "key", session=session), targets, action)
        return report, session.changes

    def test_delete_the_show_entirely(self) -> None:
        report, changes = self.run_delete([DeleteTarget("Attack on Titan (2013)", "Attack on Titan"),
                                           DeleteTarget("Not There", "Not There")], DELETE_SHOW)
        self.assertEqual(changes, [("DELETE", "/series/1", {"deleteFiles": "true", "addImportListExclusion": "false"},
                                    None)])
        self.assertEqual((report.done, report.not_in_sonarr), (["Attack on Titan (2013)"], ["Not There"]))
        self.assertEqual(report.text(), "Deleted 1 show from Sonarr, with their files, freeing 40.0 GB. "
                                        "Not found in Sonarr: Not There.")

    def test_just_the_episodes_of_some_seasons(self) -> None:
        report, changes = self.run_delete([DeleteTarget("Attack on Titan (2013)", "Attack on Titan", {2})],
                                          DELETE_FILES)
        self.assertEqual(changes, [("DELETE", "/episodefile/bulk", {}, {"episodeFileIds": [12, 13]})])
        self.assertEqual(report.text(), "Deleted 2 episode files of 1 show, freeing 1.5 GB.")

    def test_episodes_and_unmonitor_the_whole_show(self) -> None:
        report, changes = self.run_delete([DeleteTarget("Frieren", "Frieren")], DELETE_FILES_UNMONITOR)
        unmonitor, series, delete = changes
        self.assertEqual(unmonitor[:2], ("PUT", "/episode/monitor"))  # first, so nothing is downloaded again
        self.assertEqual(unmonitor[3], {"episodeIds": [201], "monitored": False})
        self.assertEqual(series[:2], ("PUT", "/series/2"))
        self.assertFalse(series[3]["monitored"])
        self.assertFalse(any(s["monitored"] for s in series[3]["seasons"]))
        self.assertEqual(delete[3], {"episodeFileIds": [21]})
        self.assertEqual(report.text(), "Deleted 1 episode file of 1 show and unmonitored them in Sonarr.")

    def test_unmonitor_only_the_chosen_seasons(self) -> None:
        _, changes = self.run_delete([DeleteTarget("Attack on Titan (2013)", "Attack on Titan", {2})],
                                     DELETE_FILES_UNMONITOR)
        self.assertEqual(changes[0][3], {"episodeIds": [102], "monitored": False})
        series = changes[1][3]
        self.assertTrue(series["monitored"])  # the show itself stays monitored
        self.assertEqual([s["seasonNumber"] for s in series["seasons"] if not s["monitored"]], [2])

    def test_one_failure_doesnt_stop_the_rest(self) -> None:
        report, changes = self.run_delete([DeleteTarget("Broken", "Broken"), DeleteTarget("Frieren", "Frieren")],
                                          DELETE_FILES)
        self.assertEqual(report.done, ["Frieren"])
        self.assertEqual(len(report.failed), 1)
        self.assertIn("Broken", report.text())
        self.assertEqual(changes, [("DELETE", "/episodefile/bulk", {}, {"episodeFileIds": [21]})])


class SizeTests(unittest.TestCase):
    def test_wording(self) -> None:
        self.assertEqual([ui.size_text(n) for n in (0, 850 * 1024 ** 2, 12_345 * 1024 ** 2, 2048 * GB)],
                         ["-", "850 MB", "12.1 GB", "2,048.0 GB"])

    def test_a_season_and_a_show(self) -> None:
        a = ShowResult(ShowGroup("a|S1", "a", "a", season=1), [FileResult("1", GB), FileResult("2", GB // 2)],
                       Category.FULLY_DUBBED)
        self.assertEqual(a.size, GB + GB // 2)
        model = ui.ResultsModel()
        model.add(a)
        model.add(ShowResult(ShowGroup("a|S2", "a", "a", season=2), [FileResult("3", GB)], Category.NEEDS_DUB))
        self.assertEqual(model.rows(set())[0].total("size"), 2 * GB + GB // 2)

    def test_sonarr_series_size(self) -> None:
        series = SonarrClient("http://sonarr", "key", session=FakeSession()).series()
        self.assertEqual([s.size for s in series], [40 * GB, 0, 0])


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ui.ResultsModel()
        for r in (season("AoT", 1, "Attack on Titan"), season("AoT", 2, "Attack on Titan"),
                  season("AoT", 3, "Attack on Titan"), season("Lain", None)):
            self.model.add(r)

    def targets(self, *iids: str) -> dict:
        return {t.folder_name: (t.seasons, keys) for t, keys in ui.delete_targets(list(iids), self.model)}

    def test_a_show_row_is_the_whole_show(self) -> None:
        self.assertEqual(self.targets("p|AoT", "s|AoT|S2"), {"AoT": (None, ["AoT|S1", "AoT|S2", "AoT|S3"])})

    def test_season_rows_are_those_seasons(self) -> None:
        self.assertEqual(self.targets("s|AoT|S1", "s|AoT|S3", "e|AoT|S1|0"), {"AoT": ({1, 3}, ["AoT|S1", "AoT|S3"])})

    def test_every_season_is_the_whole_show(self) -> None:
        self.assertIsNone(self.targets("s|AoT|S1", "s|AoT|S2", "s|AoT|S3")["AoT"][0])
        lain = season_key("Lain", None)
        self.assertEqual(self.targets("s|" + lain), {"Lain": (None, [lain])})  # no season number

    def test_episode_rows_alone_select_nothing(self) -> None:
        self.assertEqual(self.targets("e|AoT|S1|0"), {})


def tk_available() -> bool:
    try:
        import tkinter
        tkinter.Tk().destroy()
        return True
    except Exception:
        return False


@unittest.skipUnless(tk_available(), "no display for Tk")
class DeleteWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        import tkinter as tk

        from dubchecker.gui import App, AppContext
        self._tmp = tempfile.TemporaryDirectory()
        data = Path(self._tmp.name)
        self.cache = Cache(data / "cache.db")
        config = Config(library_path=str(data), theme="light", sonarr_url="http://sonarr", sonarr_api_key="key")
        ctx = AppContext(config, data / "config.json", Overrides(data / "overrides.json"), self.cache, data)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, ctx)
        for r, size in ((season("AoT", 1, "Attack on Titan"), 2 * GB), (season("AoT", 2, "Attack on Titan"), 5 * GB // 2),
                        (season("Lain", None), GB)):
            r.files[0].size = size
            self.app.model.add(r)
        self.app.expanded.add("AoT")
        self.app.select_tabs(set())  # list every show
        self.asked: list = []
        self.app.delete_in_sonarr = lambda targets, action: self.asked.append((targets, action))

    def tearDown(self) -> None:
        self.app.stop_polling()
        self.root.destroy()
        self.cache.close()
        self._tmp.cleanup()

    def open_dialog(self, *iids: str):
        from dubchecker.dialogs import DeleteDialog
        self.app.tree.selection_set(iids)
        self.app.delete_selected()
        return next(w for w in self.root.winfo_children() if isinstance(w, DeleteDialog))

    def test_several_shows_can_be_selected_and_deleted(self) -> None:
        self.assertEqual(str(self.app.tree.cget("selectmode")), "extended")
        dialog = self.open_dialog("p|AoT", "s|" + season_key("Lain", None))
        self.assertEqual(dialog.delete_button.instate(["disabled"]), True)  # nothing is chosen for you
        dialog.choice.set(DELETE_FILES_UNMONITOR)
        dialog._update_button()
        dialog._delete()
        (targets, action), = self.asked
        self.assertEqual(action, DELETE_FILES_UNMONITOR)
        self.assertEqual([(t.folder_name, t.seasons) for t, _ in targets], [("AoT", None), ("Lain", None)])

    def test_the_dialog_says_how_much_is_freed(self) -> None:
        dialog = self.open_dialog("s|AoT|S1", "s|" + season_key("Lain", None))
        self.assertEqual(dialog.space.cget("text"), "Selected: 3.0 GB")
        for action, freed in ((DELETE_FILES, "3.0 GB"), (DELETE_SHOW, "5.5 GB")):  # the whole of AoT goes
            dialog.choice.set(action)
            dialog._update_button()
            self.assertEqual(dialog.space.cget("text"), f"Frees about {freed}")
        dialog.close()

    def test_the_size_column(self) -> None:
        tree = self.app.tree
        self.assertEqual(tree.set("p|AoT", "size"), "4.5 GB")
        self.assertEqual(tree.set("s|AoT|S2", "size"), "2.5 GB")

    def test_deleted_seasons_leave_the_list(self) -> None:
        self.app._on_deleted(["AoT|S2"], "Deleted 3 episode files of 1 show.", False)
        self.assertEqual(sorted(self.app.model.results), ["AoT|S1", "Lain"])
        self.assertEqual(self.app.status_var.get(), "Deleted 3 episode files of 1 show.")

    def test_the_right_click_menu_for_several_rows(self) -> None:
        import tkinter as tk
        menu = tk.Menu(self.root, tearoff=False)
        self.app._selection_menu(menu, ("s|AoT|S1", "s|AoT|S2", "s|Lain"))
        labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1) if menu.type(i) == "command"]
        self.assertEqual(labels[0], "Delete 2 shows from Sonarr...")
        self.assertIn("Dismiss 2 shows (hide them from every tab)", labels)


if __name__ == "__main__":
    unittest.main()
