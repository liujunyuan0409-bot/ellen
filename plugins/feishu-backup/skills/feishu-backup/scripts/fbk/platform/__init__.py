"""各系统差异的集中地。本包名与标准库 platform 同名，fbk 内部不要写 `import platform`。"""
import sys

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def current() -> str:
    """返回 'windows' / 'mac' / 'other'。"""
    if IS_WINDOWS:
        return "windows"
    if IS_MAC:
        return "mac"
    return "other"
