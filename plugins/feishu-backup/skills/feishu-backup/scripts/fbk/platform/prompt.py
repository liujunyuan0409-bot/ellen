"""密码/ID 弹窗：macOS osascript；Windows 先 tkinter，没有就回退 PowerShell Read-Host。
输入内容永远不进命令行参数，也不写日志。"""
import base64
import logging
import os
import subprocess
import sys
from pathlib import Path

from . import IS_WINDOWS

log = logging.getLogger(__name__)

DIALOG_TIMEOUT = 300  # 秒：5 分钟没人填就自动关闭


class PromptCancelled(Exception):
    """取消、超时、空输入或无法弹窗。wizard.Cancelled 就是它。"""


def _launch_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "fbk.py")


def _default_tk():
    import tkinter
    return tkinter


def _encoded(script: str) -> str:
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _clean(value) -> str:
    value = (value or "").strip()
    if not value:
        raise PromptCancelled("已取消：输入为空或 5 分钟内没有填写，凭证未保存")
    return value


def _ask_mac(prompt, hidden, runner):
    script = ('text returned of (display dialog "%s" default answer ""%s with title "飞书备份" '
              'giving up after %d)') % (
        prompt.replace("\\", "\\\\").replace('"', '\\"'), " with hidden answer" if hidden else "",
        DIALOG_TIMEOUT)
    try:
        r = runner(["osascript", "-e", script], capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        raise PromptCancelled("已取消：5 分钟内没有填写，凭证未保存")
    except OSError:  # 含 FileNotFoundError：系统里没有 osascript
        raise PromptCancelled("已取消：无法弹出系统对话框（找不到 osascript），凭证未保存")
    if r.returncode != 0:
        raise PromptCancelled("已取消：没有填写，凭证未保存")
    return _clean(r.stdout)


def _ask_tk(prompt, hidden, tk_factory):
    """自制置顶小对话框（simpledialog 的窗口不吃 root 的 topmost）。
    tkinter 不可用（含无图形界面）时抛 _TkUnavailable。tk_factory() 返回 tkinter 模块。"""
    try:
        tk = tk_factory()
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001 —— ImportError / TclError(no display) 等
        raise _TkUnavailable("%s" % type(e).__name__)
    state = {"value": None, "timed_out": False}
    try:
        root.withdraw()
        top = tk.Toplevel(root)
        top.title("飞书备份")
        top.attributes("-topmost", True)
        tk.Label(top, text=prompt, justify="left", wraplength=420).pack(padx=16, pady=(14, 6))
        entry = tk.Entry(top, show="*" if hidden else "", width=48)
        entry.pack(padx=16, pady=6)

        def ok(_e=None):
            state["value"] = entry.get()
            root.quit()

        def cancel(_e=None):
            state["value"] = None
            root.quit()

        def on_timeout():
            state["timed_out"] = True
            root.quit()
        frame = tk.Frame(top)
        frame.pack(pady=(6, 14))
        tk.Button(frame, text="确定", command=ok, width=8).pack(side="left", padx=6)
        tk.Button(frame, text="取消", command=cancel, width=8).pack(side="left", padx=6)
        top.bind("<Return>", ok)
        top.bind("<Escape>", cancel)
        top.protocol("WM_DELETE_WINDOW", cancel)
        root.after(DIALOG_TIMEOUT * 1000, on_timeout)
        top.lift()
        top.focus_force()
        entry.focus_set()
        try:
            top.grab_set()
        except Exception:  # noqa: BLE001 —— 抢不到焦点不影响使用
            pass
        root.mainloop()
    finally:
        for w in (locals().get("top"), root):
            try:
                if w is not None:
                    w.destroy()
            except Exception:  # noqa: BLE001
                pass
    if state["timed_out"]:
        raise PromptCancelled("已取消：5 分钟内没有填写，凭证未保存")
    if state["value"] is None:
        raise PromptCancelled("已取消：没有填写，凭证未保存")
    return _clean(state["value"])


class _TkUnavailable(Exception):
    pass


def _ps_script(hidden: bool) -> str:
    # 提示语写到 stderr（不进被捕获的 stdout），Read-Host 本身不带 -Prompt
    head = "[Console]::Error.Write($env:FBK_PROMPT + ' '); "
    if hidden:
        return (head + "$s = Read-Host -AsSecureString; "
                "[Runtime.InteropServices.Marshal]::PtrToStringBSTR("
                "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))")
    return head + "Read-Host"


def _ask_powershell(prompt, hidden, runner, isatty):
    if not isatty():
        raise PromptCancelled(
            "已取消：这里弹不出输入窗口，也没有可输入的终端，凭证未保存。"
            "请自己打开一个 PowerShell 或命令提示符窗口，运行下面这行再按提示填写：\n"
            "py -3 \"%s\" setup --credentials" % _launch_path())
    env = dict(os.environ)
    env["FBK_PROMPT"] = prompt  # 提示语走环境变量；Read-Host 需要控制台，所以不加 CREATE_NO_WINDOW
    try:
        r = runner(["powershell", "-NoProfile", "-EncodedCommand", _encoded(_ps_script(hidden))],
                   capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 30, env=env)
    except subprocess.TimeoutExpired:
        raise PromptCancelled("已取消：5 分钟内没有填写，凭证未保存")
    except OSError:
        raise PromptCancelled("已取消：无法弹出输入窗口（找不到 PowerShell），凭证未保存")
    if r.returncode != 0:
        raise PromptCancelled("已取消：没有填写，凭证未保存")
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    return _clean(lines[-1] if lines else "")


def ask(prompt: str, hidden: bool, runner=subprocess.run, is_windows: bool = IS_WINDOWS,
        tk_factory=None, isatty=None) -> str:
    if not is_windows:
        return _ask_mac(prompt, hidden, runner)
    try:
        return _ask_tk(prompt, hidden, tk_factory or _default_tk)
    except _TkUnavailable as e:
        log.info("弹窗不可用，改用 PowerShell：%s", e)
    if isatty is None:
        def isatty():
            try:
                return bool(sys.stdin and sys.stdin.isatty())
            except Exception:  # noqa: BLE001
                return False
    return _ask_powershell(prompt, hidden, runner, isatty)
