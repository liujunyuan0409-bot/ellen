"""文件名清洗、版本名、扩展名规则。"""
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

EXPORT_EXT = {"docx": "pdf", "doc": "pdf", "slides": "pdf", "sheet": "xlsx", "bitable": "xlsx"}

SHANGHAI = timezone(timedelta(hours=8))
_ILLEGAL = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')


def sanitize(name: str, max_bytes: int = 240) -> str:
    """替换非法字符，超长时截断主干并保留扩展名。"""
    s = _ILLEGAL.sub("_", name).strip()
    if len(s.encode("utf-8")) <= max_bytes:
        return s
    stem, ext = os.path.splitext(s)
    if len(ext.encode("utf-8")) > 16:  # 不像真扩展名，整体当主干
        stem, ext = s, ""
    budget = max_bytes - len(ext.encode("utf-8"))
    out = ""
    for ch in stem:
        if len((out + ch).encode("utf-8")) > budget:
            break
        out += ch
    return out + ext


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
    return "%s_%s%s" % (stem, ts, ext)


def unique_path(path: Path) -> Path:
    """path 已存在时依次试 stem-2.ext、stem-3.ext……"""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name("%s-%d%s" % (path.stem, n, path.suffix))
        if not cand.exists():
            return cand
        n += 1
