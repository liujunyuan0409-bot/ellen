"""platform.prompt.ask：mac osascript；Windows tkinter，回退 PowerShell。全部离线注入。"""
import base64
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fbk import wizard
from fbk.platform import prompt as pp


class FakeWidget:
    def __init__(self, tk, kind, *a, **kw):
        self.tk, self.kind, self.kw = tk, kind, kw
        self.attrs = {}
        self.binds = {}
        self.destroyed = False
        self.withdrawn = False
        self.after_calls = []
        tk.widgets.append(self)

    def pack(self, **k):
        pass

    def withdraw(self):
        self.withdrawn = True

    def attributes(self, k, v):
        self.attrs[k] = v

    def title(self, t):
        pass

    def bind(self, seq, fn):
        self.binds[seq] = fn

    def protocol(self, name, fn):
        self.binds[name] = fn

    def wait_visibility(self): self.tk.waited = True

    def lift(self): pass
    def focus_force(self): self.tk.focused = True
    def focus_set(self): pass
    def grab_set(self):
        if self.tk.grab_fail > 0:
            self.tk.grab_fail -= 1
            raise RuntimeError("window not viewable")
        self.tk.grabbed = True

    def after(self, ms, fn):
        self.tk.after_calls.append((ms, fn))

    def quit(self):
        self.tk.quit_called = True

    def destroy(self):
        self.destroyed = True

    def get(self):
        return self.tk.text

    def mainloop(self):
        self.tk.script(self.tk)


class FakeTk:
    """script(fake) 在 mainloop 里执行，模拟用户操作。"""

    def __init__(self, script, text=""):
        self.script, self.text = script, text
        self.widgets, self.after_calls = [], []
        self.focused = self.grabbed = self.quit_called = self.waited = False
        self.grab_fail = 0

    def Tk(self):
        return FakeWidget(self, "root")

    def Toplevel(self, root):
        return FakeWidget(self, "top")

    def Label(self, *a, **k): return FakeWidget(self, "label", **k)
    def Frame(self, *a, **k): return FakeWidget(self, "frame", **k)
    def Entry(self, *a, **k): return FakeWidget(self, "entry", **k)
    def Button(self, *a, **k): return FakeWidget(self, "button", **k)

    def find(self, kind, **kw):
        return [w for w in self.widgets if w.kind == kind and all(w.kw.get(k) == v for k, v in kw.items())]


def click(label):
    return lambda fk: fk.find("button", text=label)[0].kw["command"]()


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
    def run_ask(self, fk, hidden=True):
        return pp.ask("请输入", hidden, is_windows=True, tk_factory=lambda: fk)

    def test_ok_hidden_topmost_grab(self):
        def script(f):
            f.after_calls[1][1]()  # mainloop 里先跑抢焦点回调
            click("确定")(f)
        fk = FakeTk(script, text="secret1")
        self.assertEqual(self.run_ask(fk), "secret1")
        self.assertEqual(fk.find("entry")[0].kw["show"], "*")
        top = fk.find("top")[0]
        self.assertTrue(top.attrs["-topmost"])
        self.assertTrue(fk.grabbed and fk.focused)
        self.assertFalse(fk.waited)  # 绝不调 wait_visibility（可能永远阻塞）
        self.assertEqual(fk.after_calls[0][0], 300000)  # 超时定时器先挂上
        self.assertEqual(fk.after_calls[1][0], 0)       # 抢焦点排在它后面、跑在 mainloop 里
        self.assertTrue(all(w.destroyed for w in fk.widgets if w.kind in ("root", "top")))
        self.assertTrue(fk.find("root")[0].withdrawn)

    def test_no_focus_or_grab_before_mainloop(self):
        seen = {}

        def script(f):
            seen.update(grabbed=f.grabbed, focused=f.focused, waited=f.waited)
            click("取消")(f)
        with self.assertRaises(wizard.Cancelled):
            self.run_ask(FakeTk(script, text="x"))
        self.assertEqual(seen, {"grabbed": False, "focused": False, "waited": False})

    def test_grab_failure_retries_then_gives_up_quietly(self):
        def script(f):
            f.grab_fail = 99
            f.after_calls[1][1]()  # 第一次失败 -> 排重试
            self.assertEqual(f.after_calls[2][0], 50)
            f.after_calls[2][1]()
            click("确定")(f)
        fk = FakeTk(script, text="v")
        self.assertEqual(self.run_ask(fk), "v")
        self.assertFalse(fk.grabbed)

    def test_grab_retry_succeeds_when_window_maps(self):
        def script(f):
            f.grab_fail = 1
            f.after_calls[1][1]()
            f.after_calls[2][1]()
            click("确定")(f)
        fk = FakeTk(script, text="v")
        self.run_ask(fk)
        self.assertTrue(fk.grabbed)

    def test_visible_entry(self):
        fk = FakeTk(click("确定"), text="cli_x")
        self.run_ask(fk, hidden=False)
        self.assertEqual(fk.find("entry")[0].kw["show"], "")

    def test_enter_key_submits_escape_cancels(self):
        fk = FakeTk(lambda f: f.find("top")[0].binds["<Return>"](), text="abc")
        self.assertEqual(self.run_ask(fk), "abc")
        fk = FakeTk(lambda f: f.find("top")[0].binds["<Escape>"](), text="abc")
        with self.assertRaises(wizard.Cancelled):
            self.run_ask(fk)

    def test_cancel_button_and_window_close(self):
        fk = FakeTk(click("取消"), text="abc")
        with self.assertRaises(wizard.Cancelled):
            self.run_ask(fk)
        self.assertTrue(all(w.destroyed for w in fk.widgets if w.kind in ("root", "top")))
        fk = FakeTk(lambda f: f.find("top")[0].binds["WM_DELETE_WINDOW"](), text="abc")
        with self.assertRaises(wizard.Cancelled):
            self.run_ask(fk)

    def test_empty(self):
        with self.assertRaises(wizard.Cancelled):
            self.run_ask(FakeTk(click("确定"), text="   "))

    def test_timeout_is_cancel(self):
        fk = FakeTk(lambda f: f.after_calls[0][1](), text="late")
        with self.assertRaises(wizard.Cancelled) as cm:
            self.run_ask(fk)
        self.assertIn("5 分钟", str(cm.exception))
        self.assertTrue(all(w.destroyed for w in fk.widgets if w.kind in ("root", "top")))

    def test_mainloop_error_still_destroys(self):
        def boom(f):
            raise RuntimeError("x")
        fk = FakeTk(boom)
        with self.assertRaises(RuntimeError):
            self.run_ask(fk)
        self.assertTrue(all(w.destroyed for w in fk.widgets if w.kind in ("root", "top")))


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
        self.assertEqual(cmd[:3], ["powershell", "-NoProfile", "-EncodedCommand"])
        script = base64.b64decode(cmd[-1]).decode("utf-16-le")
        self.assertIn("-AsSecureString", script)
        self.assertIn("[Console]::Error.Write($env:FBK_PROMPT", script)
        self.assertNotIn("-Prompt", script)
        self.assertNotIn("pw-secret", " ".join(cmd))
        self.assertNotIn("请输入 Secret", " ".join(cmd))
        self.assertEqual(kw["env"]["FBK_PROMPT"], "请输入 Secret")
        self.assertNotIn("creationflags", kw)
        self.assertNotIn("capture_output", kw)
        self.assertEqual(kw["stdout"], subprocess.PIPE)
        self.assertNotIn("stderr", kw)
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
        self.assertNotIn("-AsSecureString", base64.b64decode(calls[0][-1]).decode("utf-16-le"))

    def test_prompt_echo_in_stdout_is_dropped(self):
        r = pp.ask("p", True, runner=lambda c, **k: subprocess.CompletedProcess(
            c, 0, stdout="请输入 Secret: \r\nSECRET9\r\n"),
            is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)
        self.assertEqual(r, "SECRET9")

    def test_fallback_reason_logged_without_input(self):
        with self.assertLogs("fbk.platform.prompt", level="INFO") as lg:
            pp.ask("p", True, runner=lambda c, **k: subprocess.CompletedProcess(c, 0, stdout="x\n"),
                   is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)
        self.assertIn("ImportError", "\n".join(lg.output))

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
        with mock.patch.object(pp._paths, "app_root", return_value=Path("/nonexistent-fbk-app")):
            with self.assertRaises(wizard.Cancelled) as cm:
                pp.ask("p", True, runner=run, is_windows=True, tk_factory=self._no_tk, isatty=lambda: False)
        msg = str(cm.exception)
        self.assertIn("py -3", msg)
        self.assertIn("setup --credentials", msg)
        self.assertIn("fbk.py", msg)

    def test_no_console_names_installed_launcher_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "launch.py").write_text("x")
            with mock.patch.object(pp._paths, "app_root", return_value=Path(d)):
                with self.assertRaises(wizard.Cancelled) as cm:
                    pp.ask("p", True, runner=None, is_windows=True, tk_factory=self._no_tk, isatty=lambda: False)
        self.assertIn(str(Path(d) / "launch.py"), str(cm.exception))
        self.assertIn("setup --credentials", str(cm.exception))

    def test_powershell_missing(self):
        def boom(c, **k):
            raise FileNotFoundError()
        with self.assertRaises(wizard.Cancelled):
            pp.ask("p", True, runner=boom, is_windows=True, tk_factory=self._no_tk, isatty=lambda: True)


if __name__ == "__main__":
    unittest.main()
