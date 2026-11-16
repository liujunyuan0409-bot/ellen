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


def strip_lp(p: Union[str, Path]) -> str:
    """去掉 lp() 加的前缀，还原成普通路径字符串（写进清单、做比较前用）。"""
    s = str(p)
    if s.startswith(_UNC_PREFIX):
        return "\\\\" + s[len(_UNC_PREFIX):]
    if s.startswith(_PREFIX):
        return s[len(_PREFIX):]
    return s


def _listdir(d: Path) -> Iterable[str]:
    try:
        return os.listdir(lp(d))
    except (FileNotFoundError, NotADirectoryError):
        return ()


def _same_file(a: Path, b: Path) -> bool:
    """a、b 是否是磁盘上的同一个文件（同一 inode / 文件 ID）。出错一律按「不是」处理（更保守）。"""
    try:
        return os.path.samefile(lp(a), lp(b))
    except (OSError, ValueError):
        return False


def is_taken(path: Path, ignore: Optional[Path] = None) -> bool:
    """path 是否已被占用：同目录下有名字 fold() 相同的条目（含完全同名）即算占用。
    ignore 是调用方自己的文件（例如同一 token 当前的文件）：只有确认是磁盘上同一个文件、且本身不是符号链接的条目才不算占用
    （按 samefile 判断，所以上级目录大小写不同、磁盘名是 NFD 也认得出；大小写敏感的卷上不同文件仍算占用）。"""
    path = Path(path)
    key = fold(path.name)
    if os.path.lexists(lp(path)) and (ignore is None or not _is_own(path, ignore)):
        return True
    for e in _listdir(path.parent):
        if fold(e) != key:
            continue
        if ignore is not None and _is_own(path.parent / e, ignore):
            continue
        return True
    return False


def _is_own(entry: Path, ignore: Path) -> bool:
    """entry 是否就是调用方自己的文件。符号链接一律不是（samefile 会跟随链接，
    用户指向我们文件的链接必须算占用，不能被 os.replace 换掉）。"""
    if os.path.islink(lp(entry)):
        return False
    return _same_file(entry, ignore)
