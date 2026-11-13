"""调度：是否到期、运行锁、LaunchAgent 安装与卸载。"""
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Optional

from . import config

LABEL = "local.feishu-backup"
START_INTERVAL = 3600
LOCK_NAME = "run.lock"
STATE_DIR = ".feishu-backup"


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def default_script() -> Path:
    return Path(__file__).resolve().parent.parent / "fbk.py"


def is_due(last_success: Optional[int], interval_hours: int, now: int) -> bool:
    if last_success is None:
        return True
    return int(now) - int(last_success) >= int(interval_hours) * 3600


class AlreadyRunning(Exception):
    pass


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在，只是不归我们
    except OSError:
        return False
    return True


class RunLock:
    """root/.feishu-backup/run.lock，内容为 PID。存活进程持有时抛 AlreadyRunning，死 PID 则接管。"""

    def __init__(self, root):
        self.path = Path(root) / STATE_DIR / LOCK_NAME
        self.pid = os.getpid()
        self._held = False

    def _try_create(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w") as f:
            f.write("%d\n" % self.pid)
        return True

    def _holder(self) -> Optional[int]:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire(self):
        self.path.parent.mkdir(exist_ok=True)  # root 必须已存在，这里不建 root
        for _ in range(3):
            if self._try_create():
                self._held = True
                return self
            pid = self._holder()
            if pid is not None and pid != self.pid and _alive(pid):
                raise AlreadyRunning("已有备份进程在运行（PID %d）" % pid)
            try:  # 死锁文件：删掉后用 O_EXCL 重新抢，两个进程同时接管时只有一个成功
                self.path.unlink()
            except FileNotFoundError:
                pass
        raise AlreadyRunning("无法取得运行锁：%s" % self.path)

    def release(self):
        if self._held and self._holder() == self.pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def plist_xml(python: str, script: str) -> bytes:
    logdir = config.LOG_DIR
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(python), str(script), "run"],
        "RunAtLoad": True,
        "StartInterval": START_INTERVAL,
        "StandardOutPath": str(logdir / "launchd.out.log"),
        "StandardErrorPath": str(logdir / "launchd.err.log"),
    })


def _launchctl(runner, *args):
    return runner(["launchctl"] + list(args), capture_output=True)


def install(python: Optional[str] = None, script: Optional[str] = None, agents_dir: Optional[Path] = None,
            runner=subprocess.run, uid: Optional[int] = None) -> Path:
    """写 ~/Library/LaunchAgents/local.feishu-backup.plist，先 bootout 旧的（忽略失败）再 bootstrap。"""
    agents_dir = Path(agents_dir) if agents_dir is not None else default_agents_dir()
    uid = os.getuid() if uid is None else uid
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / (LABEL + ".plist")
    path.write_bytes(plist_xml(python or sys.executable, script or str(default_script())))
    _launchctl(runner, "bootout", "gui/%d/%s" % (uid, LABEL))
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
