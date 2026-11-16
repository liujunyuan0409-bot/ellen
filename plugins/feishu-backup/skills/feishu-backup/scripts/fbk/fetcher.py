"""导出、下载、.fbk-part 临时文件、哈希、磁盘检查。只用标准库。

系统文件操作一律用 lp(路径)（Windows 长路径）。_atomic 的 dest 只能是调用方的临时名（.fbk-new），
真正的落位（选名、不覆盖）由 sync / chats 负责。"""
import hashlib
import os
import shutil
import time
from pathlib import Path

from .names import EXPORT_EXT
from .platform.fsnames import lp

CHUNK = 1024 * 1024


class DiskFull(Exception):
    pass


class ExportFailed(Exception):
    pass


def ensure_space(root: Path, need_bytes: int, margin: int = 2 * 1024 ** 3) -> None:
    free = shutil.disk_usage(lp(root)).free
    if free < need_bytes + margin:
        raise DiskFull("磁盘空间不足：剩余 %d 字节，需要 %d 字节外加 %d 字节余量" % (free, need_bytes, margin))


def _atomic(dest, transfer):
    """transfer(part_path) 写入 .fbk-part；成功后 os.replace 到 dest；任何异常都删 .fbk-part 并重抛。"""
    dest = Path(dest)
    os.makedirs(lp(dest.parent), exist_ok=True)
    part = dest.with_name(dest.name + ".fbk-part")
    try:
        transfer(Path(lp(part)))  # 写入方（client.download）直接拿到带前缀的路径
        os.replace(lp(part), lp(dest))
    except BaseException:
        try:
            os.unlink(lp(part))
        except FileNotFoundError:
            pass
        raise
    return dest


def export_to(client, token, ftype, dest, poll=2.0, timeout=300, sleep=time.sleep, clock=time.monotonic):
    ext = EXPORT_EXT.get(ftype)
    if ext is None:
        raise ValueError("不支持导出的类型：%s" % ftype)

    def transfer(part):
        data = client.post("/drive/v1/export_tasks", {"file_extension": ext, "token": token, "type": ftype})
        ticket = data["ticket"]
        deadline = clock() + timeout
        while True:
            res = client.get("/drive/v1/export_tasks/%s" % ticket, {"token": token}).get("result") or {}
            status = res.get("job_status")
            if status == 0:
                break
            if status not in (1, 2):
                raise ExportFailed("导出失败 job_status=%s %s" % (status, res.get("job_error_msg", "")))
            if clock() >= deadline:
                raise ExportFailed("导出超时（%s 秒）" % timeout)
            sleep(poll)
        client.download("/drive/v1/export_tasks/file/%s/download" % res["file_token"], part)

    return _atomic(dest, transfer)


def download_file(client, token, dest):
    return _atomic(dest, lambda part: client.download("/drive/v1/files/%s/download" % token, part))


def download_attachment(client, message_id, file_key, dest):
    return _atomic(dest, lambda part: client.download(
        "/im/v1/messages/%s/resources/%s" % (message_id, file_key), part, params={"type": "file"}))


def sha256_file(path):
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# 只清程序自己产生的临时文件：.fbk-part（下载中）和 .fbk-new（待落位，含附件的 .dl-*.fbk-new）
TEMP_SUFFIXES = (".fbk-part", ".fbk-new")


def clean_parts(root):
    root = Path(root)
    base = lp(root)
    n = 0
    for dirpath, _dirs, files in os.walk(base):  # 不跟随符号链接目录
        rel_dir = os.path.relpath(dirpath, base)
        for fn in files:
            if not fn.endswith(TEMP_SUFFIXES):
                continue
            p = root / fn if rel_dir == os.curdir else root / rel_dir / fn
            if os.path.isfile(lp(p)) and not os.path.islink(lp(p)):
                os.unlink(lp(p))
                n += 1
    return n
