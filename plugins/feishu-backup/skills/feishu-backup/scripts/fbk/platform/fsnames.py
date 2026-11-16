"""两系统统一的文件名规则、同名比较键、Windows 长路径。

- clean_component：一个路径组成部分（文件名或目录名）在 macOS 和 Windows 上都合法的写法。
- fold：同目录同名判断用的比较键（不区分大小写，NFC 规整）。
- lp：只在真正调用系统文件操作时使用；Windows 上转成 \\\\?\\ 前缀的绝对路径，其他系统原样。
  程序内部比较、记录到清单的路径一律不带前缀。
"""
import ntpath
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, Optional, Union

from . import IS_WINDOWS

RESERVED = {"CON", "PRN", "AUX", "NUL", *("COM%d" % i for i in range(1, 10)),
            *("LPT%d" % i for i in range(1, 10))}

_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_PREFIX = "\\\\?\\"
_UNC_PREFIX = _PREFIX + "UNC\\"


def _is_reserved(name: str) -> bool:
    # Windows 按第一个点之前的部分（去掉结尾空格）判断：CON.txt、con .txt、COM1.tar.gz 都算
    return name.split(".", 1)[0].rstrip(" ").upper() in RESERVED


def clean_component(name: str) -> str:
    """替换非法字符和控制字符为 _，去掉结尾的空格和点，Windows 保留名加前缀 _。
    结果为空或只剩点号时返回 "_"。不负责长度限制（由 names.sanitize 处理）。"""
    s = _ILLEGAL.sub("_", name or "").rstrip(" .")
    if not s.strip("."):
        return "_"
    if _is_reserved(s):
        s = "_" + s
    return s


def fold(name: str) -> str:
    """同目录同名判断的比较键：NFC 规整后 casefold。"""
    return unicodedata.normalize("NFC", name).casefold()


def lp(p: Union[str, Path], is_windows: Optional[bool] = None) -> str:
    """系统文件操作用的路径字符串。Windows：绝对路径 → \\\\?\\C:\\...，UNC → \\\\?\\UNC\\server\\share\\...，
    已带前缀的不变；其他系统原样返回 str(p)。is_windows 缺省取当前系统。"""
    if is_windows is None:
        is_windows = IS_WINDOWS
    s = str(p)
    if not is_windows:
        return s
    if s.startswith(_PREFIX) or s.startswith("\\\\.\\"):
        return s
    s = s.replace("/", "\\")
    if not ntpath.isabs(s) and sys.platform == "win32":
        s = os.path.abspath(s)
    drive = ntpath.splitdrive(s)[0]
    if not drive or not ntpath.isabs(s):
        return s  # 没有盘符的相对路径无法加前缀，原样交给系统
    s = ntpath.normpath(s)  # 带前缀后系统不再规整 . 和 ..，这里先规整
    if drive.startswith("\\\\"):
        return _UNC_PREFIX + s[2:]
    return _PREFIX + s


def _listdir(d: Path) -> Iterable[str]:
    try:
        return os.listdir(lp(d))
    except (FileNotFoundError, NotADirectoryError):
        return ()


def is_taken(path: Path, ignore: Optional[Path] = None) -> bool:
    """path 是否已被占用：同目录下有名字 fold() 相同的条目（含完全同名）即算占用。
    ignore 是调用方自己的文件（例如同一 token 当前的文件），它不算占用。"""
    path = Path(path)
    key = fold(path.name)
    ignore_name = None
    if ignore is not None and Path(ignore).parent == path.parent:
        ignore_name = Path(ignore).name
    if os.path.lexists(lp(path)) and (ignore_name is None or fold(ignore_name) != key):
        return True
    for e in _listdir(path.parent):
        if e == ignore_name:
            continue
        if fold(e) == key:
            return True
    return False
