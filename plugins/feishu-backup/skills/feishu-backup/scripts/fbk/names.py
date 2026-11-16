"""文件名清洗、版本名、扩展名规则。"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .platform.fsnames import clean_component, is_taken

EXPORT_EXT = {"docx": "pdf", "doc": "pdf", "slides": "pdf", "sheet": "xlsx", "bitable": "xlsx"}

SHANGHAI = timezone(timedelta(hours=8))


MAX_FS_BYTES = 255


def _fit(stem: str, ext: str, max_bytes: int) -> str:
    """截断 stem（按 UTF-8 字符边界），使 stem+ext 不超过 max_bytes。"""
    budget = max_bytes - len(ext.encode("utf-8"))
    out = ""
    for ch in stem:
        if len((out + ch).encode("utf-8")) > budget:
            break
        out += ch
    return out + ext


def sanitize(name: str, max_bytes: int = 240) -> str:
    """两系统统一的文件名规则（见 platform.fsnames.clean_component），超长时截断主干并保留扩展名。
    保证：结果 ≤ max_bytes 字节（UTF-8）、不为空、不是只有点号、不以空格或点结尾、不是 Windows 保留名。"""
    s = clean_component((name or "").strip())
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    stem, ext = os.path.splitext(s)
    if len(ext.encode("utf-8")) > 16 or len(ext.encode("utf-8")) >= max_bytes:
        stem, ext = s, ""  # 不像真扩展名，或扩展名本身就放不下：整体当主干
    budget = max_bytes
    while True:
        # 截断后可能露出结尾的空格/点，再按规则清一遍；清完（加了 _ 前缀）若又超长就再少留一点
        out = clean_component(_fit(stem, ext, budget))
        if len(out.encode("utf-8")) <= max_bytes or budget <= 1:
            return out
        budget -= 1


def local_filename(ftype: str, name: str) -> Optional[str]:
    """上传文件保留原名；可导出类型加 .pdf/.xlsx；其他类型返回 None。"""
    if ftype == "file":
        return sanitize(name)
    ext = EXPORT_EXT.get(ftype)
    if ext is None:
        return None
    return sanitize(name, max_bytes=240 - len(ext) - 1) + "." + ext


def version_name(filename: str, mtime: int) -> str:
    """旧版文件名：原名_YYYY-MM-DD_HHMM.扩展名，时间为 Asia/Shanghai。"""
    stem, ext = os.path.splitext(filename)
    ts = datetime.fromtimestamp(mtime, SHANGHAI).strftime("%Y-%m-%d_%H%M")
    suffix = "_" + ts
    return _fit(stem, suffix + ext, MAX_FS_BYTES)


def unique_path(path: Path, ignore: Optional[Path] = None) -> Path:
    """path 被占用时依次试 stem-2.ext、stem-3.ext……
    占用判断不区分大小写（platform.fsnames.is_taken）；ignore 是调用方自己的文件，不算占用。"""
    if not is_taken(path, ignore):
        return path
    n = 2
    while True:
        cand = path.with_name(_fit(path.stem, "-%d%s" % (n, path.suffix), MAX_FS_BYTES))
        if not is_taken(cand, ignore):
            return cand
        n += 1
