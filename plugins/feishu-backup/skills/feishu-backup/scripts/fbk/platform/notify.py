"""系统通知：macOS 用 osascript，Windows 用 PowerShell toast。尽力而为：任何失败返回 False，不抛异常。"""
import logging
import os
import subprocess

from . import IS_WINDOWS
from .ps import encode_command

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000  # 不让控制台窗口闪一下
TIMEOUT = 20

# PowerShell 自己已注册的 AUMID。toast 必须挂在一个已注册的应用 ID 下才会显示；
# 借用 PowerShell 的，免得为本工具注册开始菜单快捷方式。
POWERSHELL_AUMID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"

# 标题和内容只从环境变量读，不拼进脚本；XML 转义交给 SecurityElement.Escape。
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Security.SecurityElement]::Escape($env:FBK_TITLE)
$m = [Security.SecurityElement]::Escape($env:FBK_MSG)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastText02'><text id='1'>$t</text><text id='2'>$m</text></binding></visual></toast>")
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('%s').Show($toast)
""" % POWERSHELL_AUMID


def _text(b) -> str:
    if isinstance(b, bytes):
        return b.decode("utf-8", "replace").strip()
    return str(b or "").strip()


def _quote(s: str) -> str:
    """AppleScript 字符串字面量：转义反斜杠和双引号，换行改空格。"""
    s = str(s).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % s.replace("\r", " ").replace("\n", " ")


def send(title: str, message: str, runner=subprocess.run, is_windows: bool = IS_WINDOWS) -> bool:
    try:
        if is_windows:
            env = dict(os.environ)
            env["FBK_TITLE"] = str(title)
            env["FBK_MSG"] = str(message)
            r = runner(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encode_command(TOAST_SCRIPT)],
                       capture_output=True, timeout=TIMEOUT, env=env, creationflags=CREATE_NO_WINDOW)
        else:
            script = "display notification %s with title %s" % (_quote(message), _quote(title))
            r = runner(["osascript", "-e", script], capture_output=True, timeout=15)
        if r.returncode != 0:
            log.warning("系统通知失败：%s", _text(r.stderr))
            return False
    except Exception as e:  # noqa: BLE001 —— 通知是尽力而为
        log.warning("系统通知失败：%s", e)
        return False
    return True
