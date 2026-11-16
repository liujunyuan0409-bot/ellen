"""platform.prompt.ask：mac osascript；Windows tkinter，回退 PowerShell。全部离线注入。"""
import subprocess
import unittest

from fbk import wizard
from fbk.platform import prompt as pp


class FakeRoot:
    def __init__(self):
        self.after_calls = []
        self.attrs = {}
        self.withdrawn = False
        self.destroyed = False

    def withdraw(self):
        self.withdrawn = True

    def attributes(self, k, v):
        self.attrs[k] = v

    def after(self, ms, fn):
        self.after_calls.append((ms, fn))

    def destroy(self):
        self.destroyed = True


def make_tk(answer, root=None, fire_timeout=False):
    root = root or FakeRoot()

    class TkMod:
        @staticmethod
        def Tk():
            return root

    class SD:
        seen = {}

        @staticmethod
        def askstring(title, prompt, **kw):
            SD.seen = dict(title=title, prompt=prompt, **kw)
            if fire_timeout:
                root.after_calls[0][1]()
            return answer
    return (lambda: (TkMod, SD)), root, SD


class MacAskTest(unittest.TestCase):
    def test_osascript_hidden(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout="v\n", stderr="")
        self.assertEqual(pp.ask("p", True, runner=run, is_windows=False), "v")
        self.assertIn("with hidden answer", calls[0][0][-1])
        self.assertIn("giving up after 300", calls[0][0][-1])
        self.assertGreater(calls[0][1]["timeout"], 300)

    def test_cancel_empty_missing(self):
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", False, runner=lambda c, **k: subprocess.CompletedProcess(c, 1, stdout="", stderr=""),
                   is_windows=False)
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", False, runner=lambda c, **k: subprocess.CompletedProcess(c, 0, stdout="\n", stderr=""),
                   is_windows=False)

        def boom(c, **k):
            raise FileNotFoundError()
        with self.assertRaises(wizard.Cancelled) as cm:
            pp.ask("p", False, runner=boom, is_windows=False)
        self.assertIn("osascript", str(cm.exception))


class WindowsTkTest(unittest.TestCase):
    def test_tk_hidden_topmost(self):
        factory, root, sd = make_tk("secret1")
        r = pp.ask("请输入", True, runner=None, is_windows=True, tk_factory=factory)
        self.assertEqual(r, "secret1")
        self.assertEqual(sd.seen["show"], "*")
        self.assertTrue(root.withdrawn)
        self.assertTrue(root.attrs["-topmost"])
        self.assertEqual(root.after_calls[0][0], 300000)
        self.assertTrue(root.destroyed)

    def test_tk_visible(self):
        factory, root, sd = make_tk("cli_x")
        pp.ask("p", False, tk_factory=factory, is_windows=True)
        self.assertIsNone(sd.seen["show"])

    def test_tk_cancel_none(self):
        factory, root, sd = make_tk(None)
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, tk_factory=factory, is_windows=True)
        self.assertTrue(root.destroyed)

    def test_tk_empty(self):
        factory, root, sd = make_tk("   ")
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, tk_factory=factory, is_windows=True)

    def test_tk_timeout_is_cancel(self):
        factory, root, sd = make_tk("late", fire_timeout=True)
        with self.assertRaises(wizard.Cancelled) as cm:
            pp.ask("p", True, tk_factory=factory, is_windows=True)
        self.assertIn("5 分钟", str(cm.exception))
        self.assertTrue(root.destroyed)

    def test_tk_timeout_with_tclerror_still_cancel(self):
        root = FakeRoot()

        class TkMod:
            Tk = staticmethod(lambda: root)

        class SD:
            @staticmethod
            def askstring(*a, **k):
                root.after_calls[0][1]()
                raise RuntimeError("application has been destroyed")
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, tk_factory=lambda: (TkMod, SD), is_windows=True)


class WindowsPowerShellFallbackTest(unittest.TestCase):
    def _no_tk(self):
        raise ImportError("no tkinter")

    def test_falls_back_to_powershell(self):
        calls = []

        def run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout="  pw-secret \r\n", stderr="")
        r = pp.ask("请输入 Secret", True, runner=run, is_windows=True,
                   tk_factory=self._no_tk, isatty=lambda: True)
        self.assertEqual(r, "pw-secret")
        cmd, kw = calls[0]
        self.assertEqual(cmd[0], "powershell")
        self.assertIn("-AsSecureString", cmd[-1])
        self.assertNotIn("pw-secret", " ".join(cmd))
        self.assertNotIn("请输入 Secret", " ".join(cmd))
        self.assertEqual(kw["env"]["FBK_PROMPT"], "请输入 Secret")
        self.assertNotIn("creationflags", kw)
        self.assertGreater(kw["timeout"], 300)

    def test_tk_failure_other_than_import_also_falls_back(self):
        def bad():
            raise RuntimeError("no display")
        r = pp.ask("p", False, runner=lambda c, **k: subprocess.CompletedProcess(c, 0, stdout="x\n"),
                   is_windows=True, tk_factory=bad, isatty=lambda: True)
        self.assertEqual(r, "x")

    def test_visible_uses_read_host_plain(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="cli_x\n")
        pp.ask("p", False, runner=run, is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)
        self.assertNotIn("-AsSecureString", calls[0][-1])

    def test_nonzero_cancel(self):
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, runner=lambda c, **k: subprocess.CompletedProcess(c, 1, stdout=""),
                   is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)

    def test_empty_cancel(self):
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, runner=lambda c, **k: subprocess.CompletedProcess(c, 0, stdout="\r\n"),
                   is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)

    def test_no_console_tells_user_command(self):
        def run(cmd, **kw):
            raise AssertionError("must not run without a console")
        with self.assertRaises(wizard.Cancelled) as cm:
            pp.ask("p", True, runner=run, is_windows=True, tk_factory=self._no_tk, isatty=lambda: False)
        msg = str(cm.exception)
        self.assertIn("py -3", msg)
        self.assertIn("setup --credentials", msg)
        self.assertIn("fbk.py", msg)

    def test_powershell_missing(self):
        def boom(c, **k):
            raise FileNotFoundError()
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, runner=boom, is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)


if __name__ == "__main__":
    unittest.main()
