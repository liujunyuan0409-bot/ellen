"""调度：是否到期、运行锁、LaunchAgent 安装与卸载。"""
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from . import __version__, config
from .platform import fsnames, lock as _lock

LABEL = "local.feishu-backup"
START_INTERVAL = 3600
LOCK_NAME = "run.lock"
STATE_DIR = ".feishu-backup"


CAFFEINATE = "/usr/bin/caffeinate"
SYSTEM_PYTHON = "/usr/bin/python3"
_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")


def default_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def default_app_dir() -> Path:
    """定时任务实际运行的程序副本。插件缓存目录会在插件更新时被清掉，所以不能直接指向它。"""
    return Path.home() / "Library" / "Application Support" / "feishu-backup" / "app"


def default_script() -> Path:
    return Path(__file__).resolve().parent.parent / "fbk.py"


RETRY_AFTER_INCOMPLETE = 6 * 3600
_WEEKDAYS = "一二三四五六日"
_WEEKDAYS = "一二三四五六日"
parse_time = config.parse_time  # 与加载配置共用同一套规则


def weekday_name(weekday: int) -> str:
    return "周" + _WEEKDAYS[int(weekday) - 1]


def _last_naive(now: datetime, weekday: int, hh: int, mm: int) -> datetime:
    """在本地「墙上时间」里算：避免夏令时让 7 天不等于 168 小时。"""
    n = now.astimezone().replace(tzinfo=None)
    cand = (n - timedelta(days=(n.isoweekday() - weekday) % 7)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    return cand - timedelta(days=7) if cand > n else cand


def last_slot(now: datetime, weekday: int, hh: int, mm: int) -> datetime:
    """≤ now 的最近一个计划时间点（本机时区，aware）。"""
    return _last_naive(now, weekday, hh, mm).astimezone()


def next_slot(now: datetime, weekday: int, hh: int, mm: int) -> datetime:
    """严格大于 now 的下一个计划时间点。"""
    return (_last_naive(now, weekday, hh, mm) + timedelta(days=7)).astimezone()


def _dt(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts)).astimezone()


def is_due(last_success: Optional[int], now: int, weekday: int, hh: int, mm: int) -> bool:
    if last_success is None:
        return True
    return int(last_success) < last_slot(_dt(now), weekday, hh, mm).timestamp()


def is_stale(last_success: Optional[int], now: int, weekday: int, hh: int, mm: int) -> bool:
    """上次成功早于「往前数第二个计划时间点」（连续错过两个）。从没成功过不算。"""
    if last_success is None:
        return False
    prev = _last_naive(_dt(now), weekday, hh, mm) - timedelta(days=7)
    return int(last_success) < prev.astimezone().timestamp()


class AlreadyRunning(Exception):
    pass


class RunLock:
    """root/.feishu-backup/run.lock 上的排他锁（posix flock / Windows msvcrt.locking），整轮运行期间持有。

    锁由内核随进程退出（含被 SIGTERM/SIGKILL 杀掉、关机）自动释放，所以不会有「死锁文件」挡住
    以后的备份。文件里的 PID 只供人看，不参与判断（Windows 上 PID 所在字节被锁住，其他进程读不到，此时「已有备份进程在运行」提示不带 PID）；锁文件本身不删除（删了会让两个进程锁住不同的文件）。
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
        fd = os.open(fsnames.lp(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            got = _lock.try_lock(fd)
        except BaseException:
            os.close(fd)
            raise
        if not got:
            os.close(fd)
            pid = self._holder()
            raise AlreadyRunning("已有备份进程在运行%s" % ("（PID %d）" % pid if pid else ""))
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
            _lock.unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def install_app(src_dir: Optional[Path] = None, app_dir: Optional[Path] = None) -> Path:
    """把 scripts/（fbk.py + fbk/）复制到 app_dir：先复制到同级临时目录，再换名替换旧副本。返回 fbk.py 路径。
    app_dir 里只有程序自己的代码，不含任何备份数据。"""
    src = Path(src_dir) if src_dir is not None else default_script().parent
    app = Path(app_dir) if app_dir is not None else default_app_dir()
    app.parent.mkdir(parents=True, exist_ok=True)
    tmp = app.with_name(".%s.new-%d" % (app.name, os.getpid()))
    old = app.with_name(".%s.old-%d" % (app.name, os.getpid()))
    for d in (tmp, old):
        if d.exists():
            shutil.rmtree(str(d))
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    tmp.mkdir()
    shutil.copy2(str(src / "fbk.py"), str(tmp / "fbk.py"))
    shutil.copytree(str(src / "fbk"), str(tmp / "fbk"), ignore=ignore)
    if app.exists():
        os.replace(str(app), str(old))
    os.replace(str(tmp), str(app))
    if old.exists():
        shutil.rmtree(str(old), ignore_errors=True)
    return app / "fbk.py"


def pick_python(runner=subprocess.run, exists=os.path.exists) -> str:
    """优先用系统自带的 /usr/bin/python3（升级 Homebrew 等不会让它消失），要求 ≥3.9；否则用当前解释器。"""
    if exists(SYSTEM_PYTHON):
        try:
            r = runner([SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                       capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and (r.stdout or "").strip() == "True":
                return SYSTEM_PYTHON
        except (OSError, subprocess.SubprocessError):
            pass
    return sys.executable


def installed_version(script: Path) -> Optional[str]:
    try:
        m = _VERSION_RE.search((Path(script).parent / "fbk" / "__init__.py").read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def verify(plist_path: Optional[Path] = None) -> Optional[List[str]]:
    """检查已装的定时任务还能不能跑：ProgramArguments 里的路径都在、程序副本版本与当前一致。
    没装返回 None；正常返回 []；否则返回中文问题列表（修法：重新运行 setup --schedule）。"""
    path = Path(plist_path) if plist_path is not None else default_agents_dir() / (LABEL + ".plist")
    if not path.exists():
        return None
    try:
        args = plistlib.loads(path.read_bytes()).get("ProgramArguments") or []
    except Exception as e:  # noqa: BLE001
        return ["定时任务配置文件读不了：%s" % e]
    probs = []
    for a in args:
        if str(a).startswith("/") and not os.path.exists(str(a)):
            probs.append("找不到 %s" % a)
    scripts = [a for a in args if str(a).endswith("fbk.py")]
    if not scripts:
        probs.append("定时任务里没有备份程序")
    elif os.path.exists(scripts[0]):
        v = installed_version(Path(scripts[0]))
        if v != __version__:
            probs.append("定时任务用的程序版本是 %s，当前插件是 %s" % (v or "未知", __version__))
    return probs


def plist_xml(python: str, script: str) -> bytes:
    logdir = config.LOG_DIR
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [CAFFEINATE, "-i", str(python), str(script), "run"],
        "RunAtLoad": True,
        "StartInterval": START_INTERVAL,
        "StandardOutPath": str(logdir / "launchd.out.log"),
        "StandardErrorPath": str(logdir / "launchd.err.log"),
    })


def _launchctl(runner, *args):
    return runner(["launchctl"] + list(args), capture_output=True)


def install(python: Optional[str] = None, script: Optional[str] = None, agents_dir: Optional[Path] = None,
            runner=subprocess.run, uid: Optional[int] = None, app_dir: Optional[Path] = None,
            sleep=time.sleep) -> Path:
    """复制程序到 app_dir，写 ~/Library/LaunchAgents/local.feishu-backup.plist，
    先 bootout 旧的（忽略失败）再 bootstrap（失败等 1 秒重试一次）。"""
    agents_dir = Path(agents_dir) if agents_dir is not None else default_agents_dir()
    uid = os.getuid() if uid is None else uid
    if script is None:
        script = str(install_app(app_dir=app_dir))
    if python is None:
        python = pick_python()
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    path = agents_dir / (LABEL + ".plist")
    path.write_bytes(plist_xml(python, script))
    _launchctl(runner, "bootout", "gui/%d/%s" % (uid, LABEL))
    r = _launchctl(runner, "bootstrap", "gui/%d" % uid, str(path))
    if r.returncode != 0:  # 刚 bootout 完立刻 bootstrap 偶尔报 EIO（5）
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
