"""程序副本：按版本分目录存放 + current 指针 + 启动器（修 v0.1.0 小问题 2）。

布局（app_root 见 platform.paths.app_root()）：

    app_root/
      launch.py            启动器，定时任务运行的就是它；读 current，运行 versions/<current>/fbk.py
      current              一行文本：当前版本号
      versions/<版本号>/    fbk.py + fbk/ 的完整副本；in_use 是「有进程正在用」的锁文件

- 安装：先复制到 versions/ 下的临时目录再改名；同版本已存在就先挪到旁边、新副本到位后再清掉；
  复制源就是目标本身（从启动器运行的副本里安装），或目标已是一模一样的同版本副本时，不复制也不换目录；
  然后原子改写 current 和 launch.py（临时文件 + os.replace）。任何时刻 current 都指向一个完整的副本。
- 「正在用」：启动器在整个运行期间对 versions/<v>/in_use 持有**共享**锁（posix flock LOCK_SH；
  Windows 没有共享锁，就在前 IN_USE_SLOTS 个字节里锁住一个空闲字节）。清理时要拿到**整个文件的排他锁**
  才删，所以只要还有任何一个进程在用，这个版本目录就不会被删。posix 上删除期间一直拿着排他锁；
  Windows 上打开着的文件删不掉，只能先放锁再删——这一瞬间新启动的进程若锁住了它，删除会中途失败
  （留下半个目录，下次清理再删），而启动器锁住后会重读 current，发现已不是当前版本就换用新版本，不会运行它。
  启动器每次锁住后都重读 current，所以「读了旧 current、还没来得及锁」的竞争也不会运行被删的目录。
- 清理只删 app_root/versions/ 下的真实目录（不跟随符号链接 / 目录联接），且名字必须是版本号或
  安装时的旁置名（.<版本>.old-<时间>-<pid>）；current 指向的版本永远不删；current 读不出来时什么都不删。
- v0.1.0 的旧布局（app_root 下直接是 fbk.py + fbk/）：挪进 app_root/app.legacy-<时间>/，永不删除。
"""
import filecmp
import os
import re
import shutil
import stat
import time
from pathlib import Path
from typing import List, Optional

from . import IS_WINDOWS, lock as _lock
from .fsnames import lp

CURRENT = "current"
LAUNCHER = "launch.py"
VERSIONS = "versions"
IN_USE = "in_use"
IN_USE_SLOTS = 64  # Windows：最多这么多个进程各占一个字节；全满时启动器照常运行（不再受保护）

_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*\Z")
_ASIDE_RE = re.compile(r"^\.(.+)\.old-\d+(?:-\d+)*\Z")  # .<版本>.old-<时间>-<pid>[-<序号>]（旧格式 .old-<pid> 也认）
_INIT_VERSION_RE = re.compile(r"""__version__\s*=\s*["']([^"']+)["']""")

FIX = "重新运行 setup --schedule"

# 启动器：自带全部逻辑（运行时 fbk 还不在 sys.path 上），只用标准库，兼容 Python 3.9。
LAUNCHER_SOURCE = r'''#!/usr/bin/env python3
"""飞书备份启动器（由 fbk.py install / setup --schedule 生成，不要手改）。

读同目录的 current（版本号），在 versions/<版本>/in_use 上持有共享锁（进程退出时系统自动释放，
期间这个版本目录不会被清理），然后运行 versions/<版本>/fbk.py，命令行参数原样传过去。"""
import os
import re
import runpy
import stat
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SLOTS = %(slots)d
TRIES = 5
RETRY_SLEEP = 0.2  # 秒：两次尝试之间稍等（清理进程可能正拿着排他锁）
_OK = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]*\Z")
_HELD = []  # 锁文件句柄：整个进程期间保持打开


def _current():
    try:
        with open(os.path.join(ROOT, "current"), encoding="utf-8") as f:
            v = f.read().strip()
    except (OSError, ValueError):
        return None
    if not _OK.match(v) or ".." in v:
        return None
    return v


def _is_link(p):
    """符号链接或 Windows 重解析点（目录联接等）算链接；读不了也按链接处理。不存在返回 False。"""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)


def _hold(vdir):
    """返回已持有共享锁的 in_use 句柄；目录不在、in_use 是链接或锁不上时返回 None（由调用方重试）。"""
    path = os.path.join(vdir, "in_use")
    if _is_link(path):
        return None
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o644)
    except OSError:
        return None
    locked = False
    if os.name == "nt":
        import msvcrt
        for i in range(SLOTS):
            try:
                os.lseek(fd, i, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                locked = True
                break
            except OSError:
                continue
    else:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass  # 清理进程正拿着排他锁（正在删这个版本）
    if not locked:
        os.close(fd)
        return None
    return fd


def main(sleep=time.sleep):
    for i in range(TRIES):
        v = _current()
        if v is None:
            sys.exit("飞书备份：找不到程序副本的当前版本（%%s）。请重新运行 setup --schedule"
                     %% os.path.join(ROOT, "current"))
        vdir = os.path.join(ROOT, "versions", v)
        fd = _hold(vdir)
        if fd is not None:
            if _current() == v and os.path.isfile(os.path.join(vdir, "fbk.py")):
                _HELD.append(fd)
                break
            os.close(fd)
        if i < TRIES - 1:
            sleep(RETRY_SLEEP)
    else:
        sys.exit("飞书备份：程序副本不完整或正被清理（%%s）。请稍后再试，或重新运行 setup --schedule" %% ROOT)
    script = os.path.join(vdir, "fbk.py")
    sys.path.insert(0, vdir)
    sys.argv = [script] + sys.argv[1:]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
''' % {"slots": IN_USE_SLOTS}


def valid_version(v: str) -> bool:
    return bool(isinstance(v, str) and _VERSION_RE.match(v) and ".." not in v)


def read_current(app_root: Path) -> Optional[str]:
    try:
        v = (Path(app_root) / CURRENT).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return v if valid_version(v) else None


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(".%s.tmp-%d" % (path.name, os.getpid()))
    with open(lp(tmp), "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(lp(tmp), lp(path))


def _is_real_dir(p: Path) -> bool:
    """真实目录：不是符号链接，也不是 Windows 目录联接等重解析点。"""
    try:
        st = os.lstat(lp(p))
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    attrs = getattr(st, "st_file_attributes", 0)
    return not (attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _rmtree(p: Path) -> None:
    shutil.rmtree(lp(p))


def _move_legacy(app_root: Path) -> None:
    """v0.1.0 直接把 fbk.py + fbk/ 放在 app_root 下。挪到 app.legacy-<时间>/，不删。"""
    legacy = [n for n in ("fbk.py", "fbk") if os.path.lexists(lp(app_root / n))]
    if not legacy:
        return
    dest = app_root / ("app.legacy-%s-%d" % (time.strftime("%Y%m%d%H%M%S"), os.getpid()))
    os.mkdir(lp(dest))
    for n in legacy:
        os.replace(lp(app_root / n), lp(dest / n))


def _try_lock_whole(fd: int) -> bool:
    """排他锁住整个 in_use：posix 用 flock LOCK_EX（与启动器的 LOCK_SH 冲突）；
    Windows 锁住前 IN_USE_SLOTS 个字节（任何一个字节被启动器锁着都会失败）。"""
    if not IS_WINDOWS:
        return _lock.try_lock(fd)
    import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, IN_USE_SLOTS)
    except OSError:
        return False
    return True


def _unlock_whole(fd: int) -> None:
    if not IS_WINDOWS:
        _lock.unlock(fd)
        return
    import msvcrt
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, IN_USE_SLOTS)
    except OSError:
        pass


def _remove_if_unused(vdir: Path) -> bool:
    """拿得到 in_use 的排他锁才删（删除期间一直拿着，启动器拿不到锁会重读 current 换版本）。"""
    path = vdir / IN_USE
    if os.path.lexists(lp(path)):
        try:
            st = os.lstat(lp(path))
        except OSError:
            return False
        if stat.S_ISLNK(st.st_mode) or (getattr(st, "st_file_attributes", 0)
                                         & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            return False  # in_use 是链接：不跟随、不删，留给人看
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lp(path), flags, 0o644)
    except OSError:
        return False
    try:
        if not _try_lock_whole(fd):
            return False
        try:
            if IS_WINDOWS:  # Windows 上打开着的文件删不掉：先关掉 in_use 再删（极短的窗口，见模块说明）
                _unlock_whole(fd)
                os.close(fd)
                fd = None
            _rmtree(vdir)
        except OSError:
            return False
        return True
    finally:
        if fd is not None:
            try:
                _unlock_whole(fd)
            finally:
                os.close(fd)


def cleanup(app_root: Path) -> List[Path]:
    """删掉 versions/ 下没人在用、也不是 current 的版本目录。返回删掉的目录。"""
    app_root = Path(app_root)
    vers = app_root / VERSIONS
    cur = read_current(app_root)
    if cur is None or not _is_real_dir(vers):
        return []
    removed = []
    for name in sorted(os.listdir(lp(vers))):
        if name == cur:
            continue
        m = _ASIDE_RE.match(name)
        if not (valid_version(name) or (m and valid_version(m.group(1)))):
            continue  # 临时目录（.X.new-pid，可能有别的安装进程正在写）和别的东西一律不碰
        p = vers / name
        if os.path.dirname(os.path.abspath(str(p))) != os.path.abspath(str(vers)):
            continue
        if not _is_real_dir(p):
            continue
        if _remove_if_unused(p):
            removed.append(p)
    return removed


def install(src_dir: Path, app_root: Path, version: str) -> Path:
    """把 src_dir（fbk.py + fbk/）复制到 app_root/versions/<version>/，改写 current 和 launch.py，
    再清理没人用的旧版本。返回 launch.py 的路径。"""
    if not valid_version(version):
        raise ValueError("版本号不合法：%r" % (version,))
    src, app_root = Path(src_dir), Path(app_root)
    app_root.mkdir(parents=True, exist_ok=True)
    _move_legacy(app_root)
    vers = app_root / VERSIONS
    vers.mkdir(exist_ok=True)
    if not _is_real_dir(vers):
        raise RuntimeError("%s 不是普通文件夹，没法放程序副本" % vers)
    target = vers / version
    if _same_dir(src, target) or _identical_copy(src, target, version):
        # 从正在运行的副本里安装（启动器 → setup --schedule），或目标已是一模一样的副本：不复制、不换目录
        # （Windows 上被占用的目录改不了名；mac 上也不必留一个多余的旁置目录），只改写 current 和启动器。
        return _finish(app_root, version)
    tmp = vers / (".%s.new-%d" % (version, os.getpid()))
    if _is_real_dir(tmp):
        _rmtree(tmp)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    os.mkdir(lp(tmp))
    moved_aside = False
    try:
        shutil.copy2(lp(src / "fbk.py"), lp(tmp / "fbk.py"))
        shutil.copytree(lp(src / "fbk"), lp(tmp / "fbk"), ignore=ignore)
        if os.path.lexists(lp(target)):
            aside = _free_aside(vers, version)
            try:
                os.replace(lp(target), lp(aside))
                moved_aside = True
            except OSError as e:
                raise RuntimeError("程序副本 %s 正被运行中的备份占用，暂时换不了；等这次备份结束后再运行一次（%s）"
                                   % (version, e))
        os.replace(lp(tmp), lp(target))
    except BaseException:
        if moved_aside and not os.path.lexists(lp(target)):
            try:  # 新副本没放进去：把旧副本放回原处（尽力而为，绝不删旧副本）
                os.replace(lp(aside), lp(target))
            except OSError:
                pass
        if _is_real_dir(tmp):
            shutil.rmtree(lp(tmp), ignore_errors=True)
        raise
    return _finish(app_root, version)


def _free_aside(vers: Path, version: str) -> Path:
    """旁置名带时间和 PID；同名的旧旁置目录没人用才删（拿 in_use 排他锁），有人用就换个序号，绝不硬删。"""
    base = ".%s.old-%s-%d" % (version, time.strftime("%Y%m%d%H%M%S"), os.getpid())
    for i in range(1000):
        aside = vers / (base if i == 0 else "%s-%d" % (base, i))
        if not os.path.lexists(lp(aside)):
            return aside
        if _is_real_dir(aside) and _remove_if_unused(aside):
            return aside
    raise RuntimeError("%s 里的旧副本太多，清理不掉；请稍后再运行一次 setup --schedule" % vers)


def _finish(app_root: Path, version: str) -> Path:
    _atomic_write(app_root / CURRENT, version)
    launcher = app_root / LAUNCHER
    _atomic_write(launcher, LAUNCHER_SOURCE)
    cleanup(app_root)
    return launcher


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return os.path.lexists(lp(b)) and os.path.samefile(lp(a), lp(b))
    except OSError:
        return False


_CMP_IGNORE = list(filecmp.DEFAULT_IGNORES) + [IN_USE, "__pycache__"]


def _trees_equal(d: filecmp.dircmp) -> bool:
    if d.left_only or d.right_only or d.diff_files or d.funny_files or d.common_funny:
        return False
    # dircmp 默认按 stat 签名比较；签名不同才比内容。这里对共同文件再按内容确认一遍，避免同签名不同内容
    _, mismatch, errors = filecmp.cmpfiles(d.left, d.right, d.common_files, shallow=False)
    if mismatch or errors:
        return False
    return all(_trees_equal(sub) for sub in d.subdirs.values())


def _identical_copy(src: Path, target: Path, version: str) -> bool:
    """target 已是 src 的完整、同版本副本（忽略 in_use 和 __pycache__）。"""
    if not _is_real_dir(target) or copied_version(target) != version:
        return False
    try:
        extra = set(os.listdir(lp(target))) - {"fbk.py", "fbk", IN_USE, "__pycache__"}
        if extra or not filecmp.cmp(lp(src / "fbk.py"), lp(target / "fbk.py"), shallow=False):
            return False
        return _trees_equal(filecmp.dircmp(lp(src / "fbk"), lp(target / "fbk"), ignore=_CMP_IGNORE))
    except OSError:
        return False


def copied_version(vdir: Path) -> Optional[str]:
    try:
        m = _INIT_VERSION_RE.search((Path(vdir) / "fbk" / "__init__.py").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m.group(1) if m else None


def verify(app_root: Path, expected_version: str) -> Optional[str]:
    """程序副本能不能用：正常返回 None，否则返回一句中文的问题描述。"""
    app_root = Path(app_root)
    if not (app_root / LAUNCHER).is_file():
        return "找不到启动器 %s" % (app_root / LAUNCHER)
    v = read_current(app_root)
    if v is None:
        return "程序副本的版本记录（%s）丢了或读不了" % (app_root / CURRENT)
    vdir = app_root / VERSIONS / v
    if not (vdir / "fbk.py").is_file():
        return "找不到程序副本 %s" % (vdir / "fbk.py")
    got = copied_version(vdir)
    if v != expected_version or got != expected_version:
        return "定时任务用的程序版本是 %s，当前插件是 %s" % ((got or "未知") if v == expected_version else v, expected_version)
    return None
