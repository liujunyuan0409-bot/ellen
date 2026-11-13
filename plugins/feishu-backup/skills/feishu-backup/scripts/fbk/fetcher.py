"""导出、下载、.part 临时文件、哈希、磁盘检查。只用标准库。"""
import hashlib
import os
import shutil
import time
from pathlib import Path

from .names import EXPORT_EXT

CHUNK = 1024 * 1024


class DiskFull(Exception):
    pass


class ExportFailed(Exception):
    pass


def ensure_space(root: Path, need_bytes: int, margin: int = 2 * 1024 ** 3) -> None:
    free = shutil.disk_usage(str(root)).free
    if free < need_bytes + margin:
        raise DiskFull("磁盘空间不足：剩余 %d 字节，需要 %d 字节外加 %d 字节余量" % (free, need_bytes, margin))


def _atomic(dest, transfer):
    """transfer(part_path) 写入 .part；成功后 os.replace 到 dest；任何异常都删 .part 并重抛。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".fbk-part")
    try:
        transfer(part)
        os.replace(str(part), str(dest))
    except BaseException:
        try:
            part.unlink()
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
    with open(str(path), "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def clean_parts(root):
    n = 0
    for p in Path(root).rglob("*.fbk-part"):
        if p.is_file():
            p.unlink()
            n += 1
    return n
