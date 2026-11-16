"""运行期间防睡眠。mac 由 caffeinate 包住整个进程，这里什么都不做；Windows 用 SetThreadExecutionState。"""
import contextlib
import logging

from . import IS_WINDOWS

log = logging.getLogger("fbk.awake")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _kernel32():
    import ctypes
    return ctypes.windll.kernel32


def _set(k, flags) -> None:
    try:
        k.SetThreadExecutionState(flags)
    except Exception as e:  # 防睡眠只是锦上添花，绝不能让备份失败
        log.warning("设置防睡眠失败，已忽略：%s", e)


@contextlib.contextmanager
def keep_awake(is_windows=None, kernel32=None):
    if is_windows is None:
        is_windows = IS_WINDOWS
    if not is_windows:
        yield
        return
    try:
        k = kernel32 if kernel32 is not None else _kernel32()
    except Exception as e:
        log.warning("取不到 kernel32，跳过防睡眠：%s", e)
        k = None
    if k is None:
        yield
        return
    _set(k, ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        _set(k, ES_CONTINUOUS)
