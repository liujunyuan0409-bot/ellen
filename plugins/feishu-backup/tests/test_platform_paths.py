import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import config  # noqa: E402
from fbk.platform import paths  # noqa: E402
import fbk.platform as plat  # noqa: E402

HOME = Path("/home/fake")


class MacPaths(unittest.TestCase):
    def test_mac(self):
        self.assertEqual(paths.config_dir("darwin", {}, HOME), HOME / ".config" / "feishu-backup")
        self.assertEqual(paths.log_dir("darwin", {}, HOME), HOME / "Library" / "Logs" / "feishu-backup")
        self.assertEqual(paths.app_root("darwin", {}, HOME),
                         HOME / "Library" / "Application Support" / "feishu-backup" / "app")


class WinPaths(unittest.TestCase):
    ENV = {"APPDATA": "C:/Users/fake/AppData/Roaming", "LOCALAPPDATA": "C:/Users/fake/AppData/Local"}

    def test_win_with_env(self):
        self.assertEqual(paths.config_dir("win32", self.ENV, HOME), Path(self.ENV["APPDATA"]) / "feishu-backup")
        self.assertEqual(paths.log_dir("win32", self.ENV, HOME),
                         Path(self.ENV["LOCALAPPDATA"]) / "feishu-backup" / "logs")
        self.assertEqual(paths.app_root("win32", self.ENV, HOME),
                         Path(self.ENV["LOCALAPPDATA"]) / "feishu-backup" / "app")

    def test_win_fallback(self):
        self.assertEqual(paths.config_dir("win32", {}, HOME), HOME / "AppData" / "Roaming" / "feishu-backup")
        self.assertEqual(paths.log_dir("win32", {}, HOME),
                         HOME / "AppData" / "Local" / "feishu-backup" / "logs")
        self.assertEqual(paths.app_root("win32", {}, HOME),
                         HOME / "AppData" / "Local" / "feishu-backup" / "app")

    def test_win_empty_env_value_falls_back(self):
        self.assertEqual(paths.config_dir("win32", {"APPDATA": ""}, HOME),
                         HOME / "AppData" / "Roaming" / "feishu-backup")


class Flags(unittest.TestCase):
    def test_flags(self):
        self.assertEqual(plat.IS_WINDOWS, sys.platform == "win32")
        self.assertEqual(plat.IS_MAC, sys.platform == "darwin")

    def test_config_module_attrs_are_paths(self):
        self.assertIsInstance(config.CONFIG_DIR, Path)
        self.assertIsInstance(config.LOG_DIR, Path)


if __name__ == "__main__":
    unittest.main()
