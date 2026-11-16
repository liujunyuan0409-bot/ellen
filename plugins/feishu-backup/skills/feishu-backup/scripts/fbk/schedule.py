"""调度：是否到期、运行锁；定时任务的安装 / 卸载 / 校验按系统转给 platform.launchd / platform.winsched，
程序副本（按版本存放 + 启动器）由 platform.appcopy 负责。"""
import ntpath
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from . import __version__, config
from . import platform as _plat
from .platform import appcopy, fsnames, launchd, lock as _lock, paths as _paths, winsched

LOCK_NAME = "run.lock"
STATE_DIR = ".feishu-backup"

IS_WINDOWS = _plat.IS_WINDOWS  # 模块级，测试可替换
SYSTEM_PYTHON = "/usr/bin/python3"
LABEL = launchd.LABEL
START_INTERVAL = launchd.START_INTERVAL
TASK_NAME = winsched.TASK_NAME
plist_xml = launchd.plist_xml


def default_agents_dir() -> Path:
    return launchd.default_agents_dir()


def default_app_root() -> Path:
    """程序副本所在（launch.py、current、versions/）。插件缓存目录会在插件更新时被清掉，所以不能直接指向它。"""
    return _paths.app_root()


def default_script() -> Path:
    return Path(__file__).resolve().parent.parent / "fbk.py"


RETRY_AFTER_INCOMPLETE = 6 * 3600
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


def _win(is_windows: Optional[bool]) -> bool:
    return IS_WINDOWS if is_windows is None else is_windows


def install_app(src_dir: Optional[Path] = None, app_root: Optional[Path] = None) -> Path:
    """把 scripts/（fbk.py + fbk/）复制成 app_root/versions/<当前版本>/ 并更新 current 和启动器。返回启动器路径。
    app_root 里只有程序自己的代码，不含任何备份数据。"""
    src = Path(src_dir) if src_dir is not None else default_script().parent
    root = Path(app_root) if app_root is not None else default_app_root()
    return appcopy.install(src, root, __version__)


def pick_python(runner=subprocess.run, exists=os.path.exists, is_windows: Optional[bool] = None,
                executable: Optional[str] = None) -> str:
    """mac：优先用系统自带的 /usr/bin/python3（升级 Homebrew 等不会让它消失），要求 ≥3.9；否则用当前解释器。
    Windows：用当前解释器同目录的 pythonw.exe（不弹黑窗口），没有就用当前解释器。"""
    exe = executable or sys.executable
    if _win(is_windows):
        w = ntpath.join(ntpath.dirname(exe), "pythonw.exe")
        return w if exists(w) else exe
    if exists(SYSTEM_PYTHON):
        try:
            r = runner([SYSTEM_PYTHON, "-c", "import sys; print(sys.version_info[:2] >= (3, 9))"],
                       capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and (r.stdout or "").strip() == "True":
                return SYSTEM_PYTHON
        except (OSError, subprocess.SubprocessError):
            pass
    return exe


TLS_PROBE_TIMEOUT = 20  # 秒
TLS_PROBE_CERT = 3
TLS_PROBE_NET = 4
# 用定时任务将要用的 Python 试连一次飞书：0 正常；3 证书校验失败；4 其他连不上（HTTP 错误码也算连上了）。
TLS_PROBE_SRC = r"""
import ssl, sys, urllib.error, urllib.request
try:
    urllib.request.urlopen("https://open.feishu.cn/", timeout=15).close()
except urllib.error.HTTPError:
    pass
except Exception as e:
    r = getattr(e, "reason", e)
    cert = isinstance(r, ssl.SSLCertVerificationError) or (
        isinstance(r, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(r))
    sys.exit(%d if cert else %d)
""" % (TLS_PROBE_CERT, TLS_PROBE_NET)


def tls_probe(python: str, runner=subprocess.run) -> Optional[str]:
    """正常返回 None；否则 "cert"（证书校验失败）、"network"（连不上 / 超时）或 "python"（这个 Python 跑不起来）。"""
    try:
        r = runner([python, "-c", TLS_PROBE_SRC], capture_output=True, text=True, timeout=TLS_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return "network"
    except (OSError, subprocess.SubprocessError):
        return "python"
    if r.returncode == 0:
        return None
    return {TLS_PROBE_CERT: "cert", TLS_PROBE_NET: "network"}.get(r.returncode, "python")


def install(python: Optional[str] = None, launcher: Optional[str] = None, *, app_root: Optional[Path] = None,
            agents_dir: Optional[Path] = None, runner=subprocess.run, uid: Optional[int] = None,
            sleep=time.sleep, is_windows: Optional[bool] = None, user: Optional[str] = None):
    """先复制程序副本（没给 launcher 时），再装定时任务。返回装好的位置（mac：plist 路径；Windows：说明文字）。"""
    if launcher is None:
        launcher = str(install_app(app_root=app_root))
    if python is None:
        python = pick_python(is_windows=is_windows)
    if _win(is_windows):
        name = winsched.install(winsched.task_xml(python, launcher, user or winsched.default_user()), runner=runner)
        return "任务计划程序里的「%s」" % name
    return launchd.install(python, launcher, agents_dir=agents_dir if agents_dir is not None else default_agents_dir(),
                           runner=runner, uid=uid, sleep=sleep)


def uninstall(agents_dir: Optional[Path] = None, runner=subprocess.run, uid: Optional[int] = None,
              is_windows: Optional[bool] = None) -> None:
    if _win(is_windows):
        winsched.uninstall(runner=runner)
        return
    launchd.uninstall(agents_dir=agents_dir if agents_dir is not None else default_agents_dir(), runner=runner, uid=uid)


def verify(plist_path: Optional[Path] = None, runner=subprocess.run,
           is_windows: Optional[bool] = None) -> Optional[List[str]]:
    """检查已装的定时任务还能不能跑。没装返回 None；正常返回 []；否则返回中文问题列表（修法：重新运行 setup --schedule）。"""
    if _win(is_windows):
        return winsched.verify(runner=runner, expected_version=__version__)
    path = Path(plist_path) if plist_path is not None else default_agents_dir() / (LABEL + ".plist")
    return launchd.verify(path, expected_version=__version__)
