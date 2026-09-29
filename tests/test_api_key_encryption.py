"""The Sonarr API key is never written to config.json as plain text on Windows."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dubchecker import dpapi
from dubchecker.config import Config

KEY = "0123456789abcdef0123456789abcdef"


@unittest.skipUnless(dpapi.available(), "DPAPI is Windows-only")
class DpapiTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        token = dpapi.encrypt(KEY)
        self.assertTrue(token.startswith(dpapi.PREFIX))
        self.assertNotIn(KEY, token)
        self.assertEqual(dpapi.decrypt(token), KEY)
        self.assertNotEqual(dpapi.encrypt(KEY), token)  # randomised: equal keys don't look equal

    def test_damaged_or_foreign_data_is_rejected(self) -> None:
        token = dpapi.encrypt(KEY)
        self.assertIsNone(dpapi.decrypt(token[:-12] + "AAAAAAAAAAA="))
        self.assertIsNone(dpapi.decrypt("dpapi:v1:***"))
        self.assertIsNone(dpapi.decrypt(KEY))  # plain text isn't mistaken for a token
        with mock.patch.object(dpapi, "_ENTROPY", b"another program"):
            self.assertIsNone(dpapi.decrypt(token))


@unittest.skipUnless(dpapi.available(), "DPAPI is Windows-only")
class ConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stored(self) -> dict:
        return json.loads(self.path.read_text("utf-8"))

    def test_saved_encrypted_and_read_back(self) -> None:
        config = Config.load(self.path)
        config.sonarr_api_key = KEY
        config.save(self.path)
        self.assertNotIn(KEY, self.path.read_text("utf-8"))
        self.assertNotIn("sonarr_api_key", self.stored())
        self.assertTrue(self.stored()["sonarr_api_key_encrypted"].startswith(dpapi.PREFIX))
        loaded = Config.load(self.path)
        self.assertEqual(loaded.sonarr_api_key, KEY)
        self.assertFalse(loaded.api_key_unreadable)

    def test_old_plain_text_key_is_encrypted_on_first_start(self) -> None:
        self.path.write_text(json.dumps({"sonarr_url": "http://nas:8989", "sonarr_api_key": KEY}), "utf-8")
        self.assertEqual(Config.load(self.path).sonarr_api_key, KEY)
        self.assertNotIn(KEY, self.path.read_text("utf-8"))
        self.assertEqual(Config.load(self.path).sonarr_api_key, KEY)

    def test_no_key_saves_nothing_secret(self) -> None:
        Config.load(self.path)
        self.assertEqual(self.stored()["sonarr_api_key"], "")
        self.assertNotIn("sonarr_api_key_encrypted", self.stored())

    def test_key_from_another_pc_is_kept_but_reported(self) -> None:
        config = Config.load(self.path)
        config.sonarr_api_key = KEY
        config.save(self.path)
        saved_token = self.stored()["sonarr_api_key_encrypted"]
        with mock.patch.object(dpapi, "decrypt", return_value=None):  # as if on another PC
            elsewhere = Config.load(self.path)
        self.assertEqual(elsewhere.sonarr_api_key, "")
        self.assertTrue(elsewhere.api_key_unreadable)
        self.assertEqual(self.stored()["sonarr_api_key_encrypted"], saved_token)  # still works back home
        elsewhere.sonarr_api_key = "new-key-typed-in-settings"
        elsewhere.forget_unreadable_key()
        elsewhere.save(self.path)
        self.assertEqual(Config.load(self.path).sonarr_api_key, "new-key-typed-in-settings")

    def test_clearing_the_key_in_settings_removes_it(self) -> None:
        with mock.patch.object(dpapi, "decrypt", return_value=None):
            self.path.write_text(json.dumps({"sonarr_api_key_encrypted": dpapi.encrypt(KEY)}), "utf-8")
            config = Config.load(self.path)
        config.forget_unreadable_key()
        config.save(self.path)
        self.assertNotIn("sonarr_api_key_encrypted", self.stored())


class WithoutDpapiTests(unittest.TestCase):
    def test_falls_back_to_plain_text_when_encryption_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(dpapi, "available", return_value=False):
            path = Path(tmp) / "config.json"
            config = Config.load(path)
            config.sonarr_api_key = KEY
            config.save(path)
            self.assertEqual(json.loads(path.read_text("utf-8"))["sonarr_api_key"], KEY)
            self.assertEqual(Config.load(path).sonarr_api_key, KEY)


if __name__ == "__main__":
    unittest.main()
