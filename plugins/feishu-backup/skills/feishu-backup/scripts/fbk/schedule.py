"""调度：是否到期、运行锁、LaunchAgent 安装与卸载。"""
import errno
import fcntl
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


class RunLock:
    """root/.feishu-backup/run.lock 上的 fcntl.flock 排他锁，整轮运行期间持有。

    锁由内核随进程退出（含被 SIGTERM/SIGKILL 杀掉、关机）自动释放，所以不会有「死锁文件」挡住
    以后的备份。文件里的 PID 只供人看，不参与判断；锁文件本身不删除（删了会让两个进程锁住不同的文件）。
    """

    def __init__(self, root):
        self.path = Path(root) / STATE_DIR / LOCK_NAME
        self.pid = os.getpid()
        self._fd = None

    def _holder(self) -> Optional[int]:
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    def acquire(self):
        self.path.parent.mkdir(exist_ok=True)  # root 必须已存在，这里不建 root
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            os.close(fd)
            if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                pid = self._holder()
                raise AlreadyRunning("已有备份进程在运行%s" % ("（PID %d）" % pid if pid else ""))
            raise
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % self.pid).encode())
        self._fd = fd
        return self

    def release(self):
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

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
