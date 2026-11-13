"""同步引擎：新增 / 更新归档 / 改名移动 / 去重 / 缺失标记。

铁律：本地已有的版本文件永不删除、永不覆盖。旧版只会被 os.replace 移进
同目录的 历史版本/（目标名已存在时用 unique_path 换名）。本模块只会删除
自己产生的临时文件（*.fbk-new）和刚下载、确认与已有文件内容完全相同的副本。

一次运行应共用同一个 SyncContext：它记录本轮已处理的 token，用来区分
「同一文件的第二个位置」（DUPLICATE）和「文件被移动」（MOVED）。
"""
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from . import fetcher
from .auth import LoginRequired, NetworkError
from .names import MAX_FS_BYTES, _fit, local_filename, unique_path, version_name

log = logging.getLogger(__name__)

HISTORY_DIR = "历史版本"
TMP_SUFFIX = ".fbk-new"
_PART_SUFFIX = ".fbk-part"  # fetcher 会在临时名后再加这个
# 落位（选名 + os.replace）全程持锁：并发下载时两个同名文件不会抢同一个路径
_PLACE_LOCK = threading.RLock()
PRIORITY = {"my": 0, "wiki": 1, "shared": 2}
_TS_LEN = len("_YYYY-MM-DD_HHMM")
# 只有这些状态的行「拥有」local_path 指向的文件
_OWNING = ("ok", "failed", "remote_deleted")


class Outcome(str, Enum):
    NEW = "NEW"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    MOVED = "MOVED"
    DUPLICATE = "DUPLICATE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass
class SyncContext:
    client: object
    store: object
    root: Path
    fetch: object = fetcher
    done: dict = field(default_factory=dict, repr=False)  # 本轮已作为主位置处理的 token -> 优先级


def _prio(source) -> int:
    return PRIORITY.get(source, 99)


def _exists(p: Path) -> bool:
    return os.path.lexists(str(p))


def _tmp_for(p: Path, token: str) -> Path:
    """临时名带 token 前缀，并发时不撞名；连同 fetcher 的 .fbk-part 总长不超过 255 字节。"""
    tail = "%s.%s%s" % (p.suffix, token[:6], TMP_SUFFIX)
    return p.with_name(_fit(p.stem, tail, MAX_FS_BYTES - len(_PART_SUFFIX)))


def _drop_tmp(p: Path) -> None:
    """只删本模块自己的临时文件。"""
    if p.name.endswith(TMP_SUFFIX) and p.is_file():
        p.unlink()


def archive_current(local: Path, old_mtime: int) -> Path:
    """把 local 移进 local.parent/历史版本/原名_时间.扩展名。只移动，不删除，不覆盖。"""
    local = Path(local)
    hist = local.parent / HISTORY_DIR
    with _PLACE_LOCK:
        hist.mkdir(parents=True, exist_ok=True)
        dest = unique_path(hist / version_name(local.name, int(old_mtime)))
        os.replace(str(local), str(dest))
    return dest


def _choose_path(desired: Path, token: str, cur: Optional[Path] = None) -> Path:
    """desired 空着就用；被别的文件占了就用「名字 (token前6位).扩展名」，再冲突追加 -2、-3。
    cur（本 token 现有文件）出现在候选序列里时原地不动，保证每轮结果稳定。"""
    if cur is not None and desired == cur:
        return cur
    if not _exists(desired):
        return desired
    alt = desired.with_name(_fit(desired.stem, " (%s)%s" % (token[:6], desired.suffix), MAX_FS_BYTES))
    cand, n = alt, 1
    while True:
        if cur is not None and cand == cur:
            return cur
        if not _exists(cand):
            return cand
        n += 1
        cand = alt.with_name(_fit(alt.stem, "-%d%s" % (n, alt.suffix), MAX_FS_BYTES))


def _version_stem(filename: str) -> str:
    """version_name 实际使用的（可能被截断的）主干。"""
    ext = os.path.splitext(filename)[1]
    vn = version_name(filename, 0)
    return vn[:len(vn) - _TS_LEN - len(ext)]


def _move_history(old: Path, new: Path) -> None:
    """把 old 的历史版本（原名_YYYY-MM-DD_HHMM[-N].扩展名）搬到 new 所在目录的 历史版本/，并换成新主干。"""
    old_hist = old.parent / HISTORY_DIR
    if not old_hist.is_dir():
        return
    old_ext = os.path.splitext(old.name)[1]
    new_stem, new_ext = os.path.splitext(new.name)
    pat = re.compile(re.escape(_version_stem(old.name)) + r"(_\d{4}-\d{2}-\d{2}_\d{4}(?:-\d+)?)"
                     + re.escape(old_ext))
    new_hist = new.parent / HISTORY_DIR
    for f in sorted(old_hist.iterdir()):
        m = pat.fullmatch(f.name)
        if not m or not f.is_file() or f.is_symlink():
            continue
        dest = new_hist / _fit(new_stem, m.group(1) + new_ext, MAX_FS_BYTES)
        if dest == f:
            continue
        new_hist.mkdir(parents=True, exist_ok=True)
        dest = unique_path(dest)
        os.replace(str(f), str(dest))


def _add_seen(store, row, location: str) -> None:
    seen = json.loads(row["also_seen"]) if row is not None and row["also_seen"] else []
    if location not in seen:
        seen.append(location)
    store.upsert_item(token=row["token"], also_seen=seen)


def _fetch_into(ctx, item, dest: Path) -> None:
    if item.type == "file":
        ctx.fetch.download_file(ctx.client, item.token, dest)
    else:
        ctx.fetch.export_to(ctx.client, item.token, item.type, dest)


def _owns_file(row) -> Optional[Path]:
    if row is None or row["status"] not in _OWNING or not row["local_path"]:
        return None
    p = Path(row["local_path"])
    return p if p.is_file() else None


def sync_item(ctx: SyncContext, item) -> Outcome:
    try:
        return _sync(ctx, item)
    except (fetcher.DiskFull, LoginRequired, NetworkError):
        raise
    except Exception as e:  # noqa: BLE001 —— 单个文件失败不影响整轮
        log.warning("同步失败：%s/%s（%s）", "/".join(item.rel_dir), item.name, e)
        try:
            if ctx.store.get_item(item.token) is not None:
                ctx.store.set_status(item.token, "failed")
            else:
                ctx.store.upsert_item(token=item.token, type=item.type, name=item.name, source=item.source,
                                      remote_dir=list(item.rel_dir), status="failed")
        except Exception:  # noqa: BLE001
            log.exception("记录失败状态时出错：%s", item.token)
        return Outcome.FAILED


def _sync(ctx: SyncContext, item) -> Outcome:
    store = ctx.store
    here = "/".join(item.rel_dir)
    fname = local_filename(item.type, item.name)
    if fname is None:
        store.upsert_item(token=item.token, type=item.type, name=item.name, source=item.source,
                          remote_dir=list(item.rel_dir), remote_mtime=item.mtime, status="skipped")
        return Outcome.SKIPPED

    row = store.get_item(item.token)
    owned = _owns_file(row)
    new_prio = _prio(item.source)

    # 规则 2：同一 token 的第二个位置
    if item.token in ctx.done:
        if new_prio >= ctx.done[item.token]:
            if row is not None:
                _add_seen(store, row, here)
            return Outcome.DUPLICATE
    elif owned is not None and _prio(row["source"]) < new_prio \
            and json.loads(row["remote_dir"] or "[]") != list(item.rel_dir):
        _add_seen(store, row, here)
        return Outcome.DUPLICATE
    ctx.done[item.token] = new_prio

    # 内容去重得到的行：没变就继续指向已有文件
    if row is not None and row["status"] == "duplicate" and row["local_path"] \
            and Path(row["local_path"]).is_file() \
            and json.loads(row["remote_dir"] or "[]") == list(item.rel_dir) \
            and item.mtime <= (row["remote_mtime"] or 0):
        return Outcome.DUPLICATE

    desired = ctx.root.joinpath(*item.rel_dir) / fname
    base = dict(token=item.token, type=item.type, name=item.name, source=item.source,
                remote_dir=list(item.rel_dir))

    if owned is None:
        return _new(ctx, item, desired, base)

    # 规则 4：位置或名字变了 → 移动当前文件和它的历史版本
    cur = owned
    with _PLACE_LOCK:
        target = _choose_path(desired, item.token, cur)
        moved = target != cur
        if moved:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(cur), str(target))
            store.upsert_item(token=item.token, local_path=target)  # 立刻记下，防中断后找不到
            _move_history(cur, target)
        if row["source"] != item.source and row["remote_dir"]:
            _add_seen(store, store.get_item(item.token), "/".join(json.loads(row["remote_dir"])))

    old_mtime = row["remote_mtime"]
    if old_mtime is None or item.mtime > old_mtime:
        changed = _update(ctx, item, target, old_mtime, base)
        if changed:
            return Outcome.UPDATED
    else:
        store.upsert_item(status="ok", **base)
    return Outcome.MOVED if moved else Outcome.UNCHANGED


def _new(ctx, item, desired: Path, base: dict) -> Outcome:
    store = ctx.store
    tmp = _tmp_for(desired, item.token)
    _drop_tmp(tmp)  # 上次中断留下的
    ctx.fetch.ensure_space(ctx.root, 0)
    try:
        _fetch_into(ctx, item, tmp)
        sha = None
        if item.type == "file":
            sha = ctx.fetch.sha256_file(tmp)
            blob = store.find_blob(sha)
            if blob is not None and blob != desired and blob.is_file() \
                    and ctx.fetch.sha256_file(blob) == sha:
                _drop_tmp(tmp)  # 刚下载的副本，与已有文件逐字节相同
                store.upsert_item(status="duplicate", local_path=blob, sha256=sha,
                                  remote_mtime=item.mtime, last_backup_at=int(time.time()), **base)
                return Outcome.DUPLICATE
        with _PLACE_LOCK:  # 选名与落位原子化：目标已被占用就换名，绝不覆盖
            target = _choose_path(desired, item.token)
            os.replace(str(tmp), str(target))
    finally:
        _drop_tmp(tmp)
    store.upsert_item(status="ok", local_path=target, sha256=sha, remote_mtime=item.mtime,
                      last_backup_at=int(time.time()), **base)
    if sha:
        store.add_blob(sha, target)
    return Outcome.NEW


def _update(ctx, item, local: Path, old_mtime, base: dict) -> bool:
    """新版先落到临时文件，成功后才归档旧版、换上新版。返回是否真的换了版本。"""
    store = ctx.store
    tmp = _tmp_for(local, item.token)
    _drop_tmp(tmp)
    ctx.fetch.ensure_space(ctx.root, 0)
    try:
        _fetch_into(ctx, item, tmp)
        sha = None
        if item.type == "file":
            sha = ctx.fetch.sha256_file(tmp)
            if sha == ctx.fetch.sha256_file(local):
                _drop_tmp(tmp)
                store.upsert_item(status="ok", remote_mtime=item.mtime, sha256=sha, **base)
                return False
        if old_mtime is None:
            old_mtime = int(local.stat().st_mtime)
        with _PLACE_LOCK:  # 归档与换新一步完成，期间别的线程不能占用 local
            archive_current(local, old_mtime)
            if _exists(local):  # 理论上不会发生；宁可报错也不覆盖
                raise FileExistsError(str(local))
            os.replace(str(tmp), str(local))
    finally:
        _drop_tmp(tmp)
    store.upsert_item(status="ok", local_path=local, sha256=sha, remote_mtime=item.mtime,
                      last_backup_at=int(time.time()), **base)
    if sha:
        store.add_blob(sha, local)
    return True


def mark_missing(store, seen) -> int:
    """清单里 ok 但本次没遍历到的 token 改成 remote_deleted。不碰本地文件。"""
    n = 0
    for token in store.all_tokens(("ok",)) - set(seen):
        store.set_status(token, "remote_deleted")
        n += 1
    return n
