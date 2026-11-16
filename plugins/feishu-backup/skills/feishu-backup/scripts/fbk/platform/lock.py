"""跨平台文件锁：posix 用 flock，Windows 用 msvcrt.locking。锁随进程退出由系统释放。"""
import errno
import os

from . import IS_WINDOWS

_BUSY = (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EDEADLK)


def _posix_try_lock(fd: int, fcntl=None) -> bool:
    if fcntl is None:
        import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY:
            return False
        raise
    return True


def _posix_unlock(fd: int, fcntl=None) -> None:
    if fcntl is None:
        import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)


def _windows_try_lock(fd: int, msvcrt=None) -> bool:
    """msvcrt.locking 锁的是「当前文件位置起」的字节，所以先回到 0。被占用时抛 OSError → 返回 False。"""
    if msvcrt is None:
        import msvcrt
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True


def _windows_unlock(fd: int, msvcrt=None) -> None:
    if msvcrt is None:
        import msvcrt
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # 没锁住 / 已释放；关 fd 时系统也会释放


def try_lock(fd: int) -> bool:
    return _windows_try_lock(fd) if IS_WINDOWS else _posix_try_lock(fd)


def unlock(fd: int) -> None:
    if IS_WINDOWS:
        _windows_unlock(fd)
    else:
        _posix_unlock(fd)
