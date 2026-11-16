"""macOS LaunchAgent：生成 plist、launchctl 安装 / 卸载、校验。runner / uid / sleep 可注入，测试不碰真 launchctl。"""
import os
import plistlib
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from . import appcopy

LABEL = "local.feishu-backup"
START_INTERVAL = 3600
CAFFEINATE = "/usr/bin/caffeinate"


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_xml(python: str, launcher: str, log_dir: Optional[Path] = None) -> bytes:
    """[caffeinate -i python launch.py run]：登录时跑一次 + 每小时一次；caffeinate 防止跑到一半闲置睡眠。"""
    if log_dir is None:
        from .. import config
        log_dir = config.LOG_DIR
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [CAFFEINATE, "-i", str(python), str(launcher), "run"],
        "RunAtLoad": True,
        "StartInterval": START_INTERVAL,
        "StandardOutPath": str(Path(log_dir) / "launchd.out.log"),
        "StandardErrorPath": str(Path(log_dir) / "launchd.err.log"),
    })


def _launchctl(runner, *args):
    return runner(["launchctl"] + list(args), capture_output=True)


def install(python: str, launcher: str, agents_dir: Optional[Path] = None, runner=subprocess.run,
            uid: Optional[int] = None, sleep=time.sleep, log_dir: Optional[Path] = None) -> Path:
    """写 ~/Library/LaunchAgents/local.feishu-backup.plist，先 bootout 旧的（忽略失败）再 bootstrap
    （刚 bootout 完偶尔报 EIO，等 1 秒重试一次）。返回 plist 路径。"""
    if log_dir is None:
        from .. import config
        log_dir = config.LOG_DIR
    agents_dir = Path(agents_dir) if agents_dir is not None else default_agents_dir()
    uid = os.getuid() if uid is None else uid
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / (LABEL + ".plist")
    path.write_bytes(plist_xml(python, launcher, log_dir))
    _launchctl(runner, "bootout", "gui/%d/%s" % (uid, LABEL))
    r = _launchctl(runner, "bootstrap", "gui/%d" % uid, str(path))
    if r.returncode != 0:
        sleep(1)
        r = _launchctl(runner, "bootstrap", "gui/%d" % uid, str(path))
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        raise RuntimeError("launchctl bootstrap 失败（%s）：%s" % (r.returncode, err.strip()))
    return path


def uninstall(agents_dir: Optional[Path] = None, runner=subprocess.run, uid: Optional[int] = None) -> None:
    agents_dir = Path(agents_dir) if agents_dir is not None else default_agents_dir()
    uid = os.getuid() if uid is None else uid
    _launchctl(runner, "bootout", "gui/%d/%s" % (uid, LABEL))
    try:
        (agents_dir / (LABEL + ".plist")).unlink()
    except FileNotFoundError:
        pass


def verify(plist_path: Optional[Path] = None, expected_version: Optional[str] = None) -> Optional[List[str]]:
    """没装返回 None；正常返回 []；否则返回中文问题列表：ProgramArguments 里的路径都在、
    指向启动器、程序副本完整且版本与当前插件一致。"""
    if expected_version is None:
        from .. import __version__ as expected_version
    path = Path(plist_path) if plist_path is not None else default_agents_dir() / (LABEL + ".plist")
    if not path.exists():
        return None
    try:
        args = [str(a) for a in (plistlib.loads(path.read_bytes()).get("ProgramArguments") or [])]
    except Exception as e:  # noqa: BLE001
        return ["定时任务配置文件读不了：%s" % e]
    probs = []
    for a in args:
        if a.startswith("/") and not os.path.exists(a):
            probs.append("找不到 %s" % a)
    launchers = [a for a in args if Path(a).name == appcopy.LAUNCHER]
    if not launchers:
        probs.append("定时任务里没有启动器（%s），可能是旧版装的" % appcopy.LAUNCHER)
    elif os.path.exists(launchers[0]):
        p = appcopy.verify(Path(launchers[0]).parent, expected_version)
        if p:
            probs.append(p)
    return probs
