import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import config, notify  # noqa: E402
from fbk.names import SHANGHAI  # noqa: E402

DAY1 = int(datetime(2026, 3, 1, 10, 0, tzinfo=SHANGHAI).timestamp())
DAY1_LATER = int(datetime(2026, 3, 1, 23, 0, tzinfo=SHANGHAI).timestamp())
DAY2 = int(datetime(2026, 3, 2, 0, 30, tzinfo=SHANGHAI).timestamp())


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        p = mock.patch.object(config, "CONFIG_DIR", Path(self.tmp.name) / "cfg")
        p.start()
        self.addCleanup(p.stop)
        r = mock.patch.object(notify.subprocess, "run",
                              return_value=subprocess.CompletedProcess([], 0))
        self.run_mock = r.start()
        self.addCleanup(r.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def scripts(self):
        return [c.args[0][2] for c in self.run_mock.call_args_list]

    def test_calls_osascript_with_escaped_text(self):
        self.assertTrue(notify.notify("飞书备份", 'say "hi" \\ ok'))
        args = self.run_mock.call_args.args[0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertEqual(args[2], 'display notification "say \\"hi\\" \\\\ ok" with title "飞书备份"')

    def test_key_throttled_once_per_day(self):
        self.assertTrue(notify.notify("t", "m", key="root_missing", now=DAY1))
        self.assertFalse(notify.notify("t", "m", key="root_missing", now=DAY1_LATER))
        self.assertTrue(notify.notify("t", "m", key="other", now=DAY1_LATER))
        self.assertTrue(notify.notify("t", "m", key="root_missing", now=DAY2))
        self.assertEqual(len(self.scripts()), 3)
        state = json.loads((config.CONFIG_DIR / "notify_state.json").read_text())
        self.assertEqual(state["root_missing"], "2026-03-02")

    def test_without_key_not_throttled(self):
        notify.notify("t", "m", now=DAY1)
        notify.notify("t", "m", now=DAY1)
        self.assertEqual(len(self.scripts()), 2)

    def test_failure_swallowed(self):
        self.run_mock.side_effect = FileNotFoundError("osascript")
        self.assertFalse(notify.notify("t", "m", key="k", now=DAY1))

    def test_corrupt_state_ignored(self):
        config.CONFIG_DIR.mkdir(parents=True)
        (config.CONFIG_DIR / "notify_state.json").write_text("{oops")
        self.assertTrue(notify.notify("t", "m", key="k", now=DAY1))


class NotifyIfChangedTest(NotifyTest):
    def test_same_set_next_day_not_notified_new_set_is(self):
        self.assertTrue(notify.notify_if_changed("t", "m", "run_failures", ["item:a", "att:b"], now=DAY1))
        self.assertFalse(notify.notify_if_changed("t", "m", "run_failures", ["att:b", "item:a"], now=DAY2))
        self.assertTrue(notify.notify_if_changed("t", "m", "run_failures", ["item:a", "att:b", "item:c"],
                                                 now=DAY2 + 86400))
        self.assertEqual(len(self.scripts()), 2)
        state = json.loads((config.CONFIG_DIR / "notify_state.json").read_text())
        self.assertEqual(len(state["run_failures_set"]), 64)

    def test_daily_throttle_still_applies(self):
        notify.notify_if_changed("t", "m", "run_failures", ["item:a"], now=DAY1)
        self.assertFalse(notify.notify_if_changed("t", "m", "run_failures", ["item:b"], now=DAY1_LATER))
        self.assertEqual(len(self.scripts()), 1)
        # 被节流的那次没记指纹：第二天同一集合仍会通知
        self.assertTrue(notify.notify_if_changed("t", "m", "run_failures", ["item:b"], now=DAY2))

    def test_empty_set_clears_fingerprint(self):
        notify.notify_if_changed("t", "m", "run_failures", ["item:a"], now=DAY1)
        self.assertFalse(notify.notify_if_changed("t", "m", "run_failures", [], now=DAY2))
        self.assertTrue(notify.notify_if_changed("t", "m", "run_failures", ["item:a"], now=DAY2 + 86400))
        self.assertEqual(len(self.scripts()), 2)


if __name__ == "__main__":
    unittest.main()
