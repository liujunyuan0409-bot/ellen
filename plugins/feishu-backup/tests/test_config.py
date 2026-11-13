import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import config  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "fb"
        p = mock.patch.object(config, "CONFIG_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_load_missing_raises(self):
        with self.assertRaises(config.ConfigMissing):
            config.load_config()

    def test_save_config_is_0600_and_dir_0700(self):
        config.save_config(config.Config(app_id="id", app_secret="sec"))
        p = self.dir / "config.json"
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)

    def test_roundtrip_with_backup_root(self):
        cfg = config.Config(app_id="id", app_secret="sec", backup_root=Path("/tmp/bk"), interval_hours=12)
        config.save_config(cfg)
        got = config.load_config()
        self.assertEqual(got, cfg)
        self.assertEqual(got.lookback_days, 180)
        self.assertIsInstance(json.loads((self.dir / "config.json").read_text())["backup_root"], str)

    def test_roundtrip_without_backup_root(self):
        config.save_config(config.Config(app_id="id", app_secret="sec"))
        self.assertIsNone(config.load_config().backup_root)

    def test_token_roundtrip_and_0600(self):
        self.assertIsNone(config.load_token())
        config.save_token({"access_token": "a", "expires_at": 1})
        self.assertEqual(config.load_token(), {"access_token": "a", "expires_at": 1})
        self.assertEqual(stat.S_IMODE(os.stat(self.dir / "token.json").st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
