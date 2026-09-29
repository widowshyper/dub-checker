import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dubchecker.config import Config, Overrides, from_portable, to_portable


class PortablePathTests(unittest.TestCase):
    def test_same_drive_loses_its_letter(self) -> None:
        self.assertEqual(to_portable("E:\\Anime\\Shows", "E:"), "\\Anime\\Shows")
        self.assertEqual(to_portable("e:\\Anime", "E:"), "\\Anime")

    def test_other_drive_and_unc_are_kept(self) -> None:
        self.assertEqual(to_portable("D:\\Anime", "E:"), "D:\\Anime")
        self.assertEqual(to_portable("\\\\nas\\media\\anime", "E:"), "\\\\nas\\media\\anime")
        self.assertEqual(from_portable("\\\\nas\\media\\anime", "F:"), "\\\\nas\\media\\anime")

    def test_resolved_against_the_current_drive(self) -> None:
        self.assertEqual(from_portable("\\Anime", "F:"), "F:\\Anime")
        self.assertEqual(from_portable("D:\\Anime", "F:"), "D:\\Anime")
        self.assertEqual(from_portable(to_portable("E:\\Anime", "E:"), "G:"), "G:\\Anime")

    def test_no_drive_letters_off_windows(self) -> None:
        self.assertEqual(to_portable("/mnt/anime", ""), "/mnt/anime")
        self.assertEqual(from_portable("/mnt/anime", ""), "/mnt/anime")

    def test_empty(self) -> None:
        self.assertEqual(to_portable("", "E:"), "")
        self.assertEqual(from_portable("", "E:"), "")


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_defaults_are_written(self) -> None:
        config = Config.load(self.path)
        self.assertEqual(config.confidence_threshold, 80)
        self.assertEqual(json.loads(self.path.read_text("utf-8"))["probe_workers"], 4)

    def test_unknown_keys_are_ignored_and_new_keys_appear(self) -> None:
        self.path.write_text(json.dumps({"probe_workers": "2", "pause_enabled": "true", "old_setting": 1}), "utf-8")
        config = Config.load(self.path)
        self.assertEqual((config.probe_workers, config.pause_enabled), (2, True))
        saved = json.loads(self.path.read_text("utf-8"))
        self.assertNotIn("old_setting", saved)
        self.assertIn("network_workers_enabled", saved)

    def test_bad_values_fall_back(self) -> None:
        self.path.write_text(json.dumps({"probe_workers": "lots", "theme": "neon", "confidence_threshold": 500}),
                             "utf-8")
        config = Config.load(self.path)
        self.assertEqual((config.probe_workers, config.theme, config.confidence_threshold), (4, "system", 100))

    def test_broken_file_and_bom_are_survived(self) -> None:
        self.path.write_text("{not json", "utf-8")
        self.assertEqual(Config.load(self.path).probe_workers, 4)
        self.path.write_text(json.dumps({"probe_workers": 3}), "utf-8-sig")  # e.g. saved by Notepad
        self.assertEqual(Config.load(self.path).probe_workers, 3)

    def test_library_path_is_stored_without_the_app_drive(self) -> None:
        with mock.patch("dubchecker.config.app_drive", return_value="E:"):
            config = Config.load(self.path)
            config.library_path = "E:\\Anime"
            config.save(self.path)
            self.assertEqual(json.loads(self.path.read_text("utf-8"))["library_path"], "\\Anime")
        with mock.patch("dubchecker.config.app_drive", return_value="F:"):
            self.assertEqual(Config.load(self.path).library_path, "F:\\Anime")


class OverridesTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "overrides.json"
            overrides = Overrides(path)
            overrides.set("Show|S2", 12345)
            self.assertEqual(Overrides(path).get("Show|S2"), 12345)
            self.assertIn("Show|S2", overrides)
            overrides.remove("Show|S2")
            self.assertIsNone(Overrides(path).get("Show|S2"))


if __name__ == "__main__":
    unittest.main()
