"""platform.notify.send：mac 用 osascript，Windows 用 PowerShell toast（标题/内容只走环境变量）。"""
import subprocess
import unittest

from fbk.platform import notify as pn


class MacSendTest(unittest.TestCase):
    def test_osascript_escaped(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0)
        self.assertTrue(pn.send("飞书备份", 'say "hi" \\ ok', runner=run, is_windows=False))
        self.assertEqual(calls[0][0][:2], ["osascript", "-e"])
        self.assertEqual(calls[0][0][2], 'display notification "say \\"hi\\" \\\\ ok" with title "飞书备份"')

    def test_failure_returns_false(self):
        def run(cmd, **kw):
            raise FileNotFoundError("osascript")
        self.assertFalse(pn.send("t", "m", runner=run, is_windows=False))

    def test_nonzero_returns_false(self):
        self.assertFalse(pn.send("t", "m", runner=lambda c, **k: subprocess.CompletedProcess(c, 1, stderr=b"x"),
                                 is_windows=False))


class WindowsSendTest(unittest.TestCase):
    def test_text_only_in_env(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0)
        self.assertTrue(pn.send("标题XYZ", "内容ABC'; evil", runner=run, is_windows=True))
        cmd, kw = calls[0]
        self.assertEqual(cmd[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        joined = " ".join(cmd)
        self.assertNotIn("标题XYZ", joined)
        self.assertNotIn("内容ABC", joined)
        self.assertEqual(kw["env"]["FBK_TITLE"], "标题XYZ")
        self.assertEqual(kw["env"]["FBK_MSG"], "内容ABC'; evil")
        self.assertEqual(kw["creationflags"], 0x08000000)
        self.assertEqual(kw["timeout"], 20)
        self.assertIn("$env:FBK_TITLE", cmd[4])
        self.assertIn("SecurityElement]::Escape", cmd[4])
        self.assertIn("ToastText02", cmd[4])
        self.assertIn("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}", cmd[4])

    def test_failure_swallowed(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 20)
        self.assertFalse(pn.send("t", "m", runner=run, is_windows=True))

    def test_nonzero_false(self):
        self.assertFalse(pn.send("t", "m", runner=lambda c, **k: subprocess.CompletedProcess(c, 1),
                                 is_windows=True))


if __name__ == "__main__":
    unittest.main()
