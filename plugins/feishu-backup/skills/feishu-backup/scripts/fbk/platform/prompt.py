"""密码/ID 弹窗：macOS osascript；Windows 先 tkinter，没有就回退 PowerShell Read-Host。
输入内容永远不进命令行参数，也不写日志。"""
import os
import subprocess
import sys
from pathlib import Path

from . import IS_WINDOWS

DIALOG_TIMEOUT = 300  # 秒：5 分钟没人填就自动关闭


class PromptCancelled(Exception):
    """取消、超时、空输入或无法弹窗。wizard.Cancelled 就是它。"""


def _launch_path() -> str:
    return str(Path(__file__).resolve().parents[2] / "fbk.py")


def _default_tk():
    import tkinter
    from tkinter import simpledialog
    return tkinter, simpledialog


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
    """返回字符串；tkinter 不可用（含无图形界面）时抛 _TkUnavailable。"""
    try:
        tk, simpledialog = tk_factory()
        root = tk.Tk()
    except Exception as e:  # noqa: BLE001 —— ImportError / TclError(no display) 等
        raise _TkUnavailable(str(e))
    timed_out = []
    try:
        root.withdraw()
        root.attributes("-topmost", True)

        def on_timeout():
            timed_out.append(1)
            root.destroy()
        root.after(DIALOG_TIMEOUT * 1000, on_timeout)
        try:
            value = simpledialog.askstring("飞书备份", prompt, show="*" if hidden else None, parent=root)
        except Exception:  # noqa: BLE001 —— 超时销毁窗口后 tk 可能抛错
            if timed_out:
                value = None
            else:
                raise
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass
    if timed_out:
        raise PromptCancelled("已取消：5 分钟内没有填写，凭证未保存")
    if value is None:
        raise PromptCancelled("已取消：没有填写，凭证未保存")
    return _clean(value)


class _TkUnavailable(Exception):
    pass


def _ps_script(hidden: bool) -> str:
    if hidden:
        return ("$s = Read-Host -AsSecureString -Prompt $env:FBK_PROMPT; "
                "[Runtime.InteropServices.Marshal]::PtrToStringBSTR("
                "[Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))")
    return "Read-Host -Prompt $env:FBK_PROMPT"


def _ask_powershell(prompt, hidden, runner, isatty):
    if not isatty():
        raise PromptCancelled(
            "已取消：这里弹不出输入窗口，也没有可输入的终端，凭证未保存。"
            "请自己打开一个 PowerShell 或命令提示符窗口，运行下面这行再按提示填写：\n"
            "py -3 \"%s\" setup --credentials" % _launch_path())
    env = dict(os.environ)
    env["FBK_PROMPT"] = prompt  # 提示语走环境变量；Read-Host 需要控制台，所以不加 CREATE_NO_WINDOW
    try:
        r = runner(["powershell", "-NoProfile", "-Command", _ps_script(hidden)],
                   capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 30, env=env)
    except subprocess.TimeoutExpired:
        raise PromptCancelled("已取消：5 分钟内没有填写，凭证未保存")
    except OSError:
        raise PromptCancelled("已取消：无法弹出输入窗口（找不到 PowerShell），凭证未保存")
    if r.returncode != 0:
        raise PromptCancelled("已取消：没有填写，凭证未保存")
    return _clean(r.stdout)


def ask(prompt: str, hidden: bool, runner=subprocess.run, is_windows: bool = IS_WINDOWS,
        tk_factory=None, isatty=None) -> str:
    if not is_windows:
        return _ask_mac(prompt, hidden, runner)
    try:
        return _ask_tk(prompt, hidden, tk_factory or _default_tk)
    except _TkUnavailable:
        pass
    if isatty is None:
        def isatty():
            try:
                return bool(sys.stdin and sys.stdin.isatty())
            except Exception:  # noqa: BLE001
                return False
    return _ask_powershell(prompt, hidden, runner, isatty)
