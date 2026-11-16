"""Windows 任务计划程序：生成任务 XML（Task Scheduler 1.2）、用 schtasks 安装 / 卸载 / 校验。

触发：用户登录时一次 + 每小时一次（从今天 0 点起无限重复），由程序自己判断要不要真跑；
只在用户登录时运行（InteractiveToken，不保存 Windows 密码）；用 pythonw.exe 运行，不弹黑窗口。
所有 schtasks 调用都用参数列表（不经 shell），runner 可注入，测试里不碰真 schtasks。"""
import os
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

from . import appcopy

TASK_NAME = "feishu-backup"
NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
DECLARATION = '<?xml version="1.0" encoding="UTF-16"?>'
_DECL_RE = re.compile(r"^\s*<\?xml[^>]*\?>\s*")
_ARGS_RE = re.compile(r'^\s*"([^"]+)"\s+run\s*$')


def default_user(env: Mapping = os.environ) -> str:
    """DOMAIN\\user（本机账户的 DOMAIN 是电脑名）；拿不到域时只写用户名。"""
    user, dom = env.get("USERNAME", ""), env.get("USERDOMAIN", "")
    return "%s\\%s" % (dom, user) if dom else user


def _sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, "{%s}%s" % (NS, tag), attrs)
    if text is not None:
        el.text = text
    return el


def task_xml(pythonw: str, launcher: str, user: str, today: Optional[date] = None) -> str:
    """任务定义 XML（字符串，带 UTF-16 声明；写文件时要用 UTF-16 编码）。元素顺序与系统导出的一致。"""
    today = today or date.today()
    ET.register_namespace("", NS)
    task = ET.Element("{%s}Task" % NS, {"version": "1.2"})
    reg = _sub(task, "RegistrationInfo")
    _sub(reg, "Description", "飞书备份：登录时和每小时检查一次，到了计划时间才真正备份")

    trig = _sub(task, "Triggers")
    lt = _sub(trig, "LogonTrigger")
    _sub(lt, "Enabled", "true")
    _sub(lt, "UserId", user)
    tt = _sub(trig, "TimeTrigger")
    rep = _sub(tt, "Repetition")
    _sub(rep, "Interval", "PT1H")  # 不写 Duration：无限重复
    _sub(rep, "StopAtDurationEnd", "false")
    _sub(tt, "StartBoundary", today.strftime("%Y-%m-%dT00:00:00"))  # 本机时间，不带 Z
    _sub(tt, "Enabled", "true")

    pr = _sub(_sub(task, "Principals"), "Principal", id="Author")
    _sub(pr, "UserId", user)
    _sub(pr, "LogonType", "InteractiveToken")
    _sub(pr, "RunLevel", "LeastPrivilege")

    st = _sub(task, "Settings")
    _sub(st, "MultipleInstancesPolicy", "IgnoreNew")
    _sub(st, "DisallowStartIfOnBatteries", "false")
    _sub(st, "StopIfGoingOnBatteries", "false")
    _sub(st, "AllowHardTerminate", "true")
    _sub(st, "StartWhenAvailable", "true")
    _sub(st, "RunOnlyIfNetworkAvailable", "false")
    _sub(st, "AllowStartOnDemand", "true")
    _sub(st, "Enabled", "true")
    _sub(st, "Hidden", "false")
    _sub(st, "RunOnlyIfIdle", "false")
    _sub(st, "WakeToRun", "false")
    _sub(st, "ExecutionTimeLimit", "PT0S")

    ex = _sub(_sub(task, "Actions", Context="Author"), "Exec")
    _sub(ex, "Command", str(pythonw))
    _sub(ex, "Arguments", '"%s" run' % launcher)
    return DECLARATION + "\n" + ET.tostring(task, encoding="unicode")


def strip_declaration(xml: str) -> str:
    return _DECL_RE.sub("", xml.lstrip("﻿"), count=1)


def parse_exec(xml: str) -> Tuple[str, str]:
    """从任务 XML 取出 (Command, 启动器路径)。Arguments 不是 "<路径>" run 的样子时，启动器返回原样的 Arguments。"""
    root = ET.fromstring(strip_declaration(xml))
    ns = {"t": NS}
    cmd = root.find("t:Actions/t:Exec/t:Command", ns)
    args = root.find("t:Actions/t:Exec/t:Arguments", ns)
    a = (args.text or "") if args is not None else ""
    m = _ARGS_RE.match(a)
    return ((cmd.text or "").strip() if cmd is not None else "", m.group(1) if m else a.strip())


def _decode(b) -> str:
    """schtasks 的输出：/XML 时通常是 UTF-16（带 BOM），报错信息是控制台代码页。"""
    if isinstance(b, str):
        return b
    b = b or b""
    if b.startswith(b"\xff\xfe") or b.startswith(b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    for enc in ("utf-8", "oem", "mbcs"):
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("utf-8", "replace")


def _schtasks(runner, *args):
    return runner(["schtasks"] + list(args), capture_output=True)


def _fail_text(r) -> str:
    return (_decode(r.stderr) or _decode(r.stdout)).strip()


def install(xml: str, name: str = TASK_NAME, runner=subprocess.run, tmpdir=None) -> str:
    """把 xml 以 UTF-16 写到临时文件，schtasks /Create /XML 导入（/F 覆盖同名旧任务）。返回任务名。"""
    fd, path = tempfile.mkstemp(prefix="feishu-backup-", suffix=".xml", dir=tmpdir)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(xml.encode("utf-16"))  # Python 的 utf-16 编码自带 BOM
        r = _schtasks(runner, "/Create", "/TN", name, "/XML", path, "/F")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if r.returncode != 0:
        raise RuntimeError("在任务计划程序里创建任务失败（schtasks 返回 %s）：%s" % (r.returncode, _fail_text(r)))
    return name


def uninstall(name: str = TASK_NAME, runner=subprocess.run) -> None:
    """没装就什么都不做；装了就删，删不掉报错。"""
    if _schtasks(runner, "/Query", "/TN", name).returncode != 0:
        return
    r = _schtasks(runner, "/Delete", "/TN", name, "/F")
    if r.returncode != 0:
        raise RuntimeError("从任务计划程序删除任务失败（schtasks 返回 %s）：%s" % (r.returncode, _fail_text(r)))


def verify(name: str = TASK_NAME, runner=subprocess.run, expected_version: Optional[str] = None,
           exists=os.path.exists) -> Optional[List[str]]:
    """没装返回 None；正常返回 []；否则返回中文问题列表。"""
    if expected_version is None:
        from .. import __version__ as expected_version
    r = _schtasks(runner, "/Query", "/TN", name, "/XML")
    if r.returncode != 0:
        return None
    try:
        cmd, launcher = parse_exec(_decode(r.stdout))
    except ET.ParseError as e:
        return ["定时任务的配置读不了：%s" % e]
    probs = []
    if not cmd or not exists(cmd):
        probs.append("找不到 Python：%s" % (cmd or "（空）"))
    if Path(launcher).name != appcopy.LAUNCHER:
        probs.append("定时任务里没有启动器（%s），可能是旧版装的" % (launcher or "空"))
    elif not exists(launcher):
        probs.append("找不到 %s" % launcher)
    else:
        p = appcopy.verify(Path(launcher).parent, expected_version)
        if p:
            probs.append(p)
    return probs
