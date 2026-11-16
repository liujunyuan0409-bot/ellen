"""一次备份运行的编排：门禁 → 加锁 → 清理 → 分阶段同步 → 群聊 → 报告。

退出码：0 正常（含不到期、已有进程在跑、离线静默退出）；1 意外错误；2 未配置或备份目录不存在；
3 需要重新登录；4 备份盘空间不足。

分阶段约定（sync.SyncContext 的要求）：整轮只用一个 SyncContext；我的空间 → 知识库 → 共享给我
严格按顺序，上一阶段全部结束才开始下一阶段，只在阶段内并发；同一轮内不重试同一项。
"""
import http.client
import json
import logging
import signal
import sqlite3
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import auth, chats, config, fetcher, notify, perms, schedule, sync, walker
from .client import DEFAULT_BASE, ApiError, FeishuClient, default_transport
from .names import SHANGHAI
from .platform import awake
from .report import RunReport
from .store import Store
from .sync import Outcome

log = logging.getLogger(__name__)

TITLE = "飞书备份"
MSG_LOGIN = "需要重新登录：对你的 AI 助手说『飞书备份重新登录』"
MSG_DISK = "备份盘空间不足"
MSG_PERMISSION = "没有权限访问备份目录：在 系统设置 → 隐私与安全性 → 完全磁盘访问权限 里给 python3 授权"
MSG_PERMISSION_WIN = "没有权限访问备份目录：在文件夹上点右键 → 属性 → 安全，确认自己有「修改」权限，或换一个目录"


def _msg_permission() -> str:
    return MSG_PERMISSION_WIN if schedule.IS_WINDOWS else MSG_PERMISSION
DOWNLOAD_WORKERS = 4
EXPORT_WORKERS = 3
LOG_KEEP_DAYS = 30
REPORT_DIR = "_报告"
PROBE_PATH = "/drive/explorer/v2/root_folder/meta"
_FATAL = (auth.LoginRequired, fetcher.DiskFull, auth.NetworkError)
_OFFLINE_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException)


class _Offline(Exception):
    pass


# ---------------- 日志 ----------------

class _LastWarning(logging.Handler):
    """按线程记住最近一条 WARNING，用来给失败项写原因。"""

    def __init__(self):
        super().__init__(logging.WARNING)
        self._by_thread = {}

    def emit(self, record):
        self._by_thread[threading.get_ident()] = record.getMessage()

    def clear(self):
        self._by_thread.pop(threading.get_ident(), None)

    def take(self) -> str:
        return self._by_thread.pop(threading.get_ident(), "")


def _setup_logging(now: float) -> logging.Handler:
    logdir = config.LOG_DIR
    logdir.mkdir(parents=True, exist_ok=True)
    cutoff = now - LOG_KEEP_DAYS * 86400
    for p in logdir.glob("fbk-*.log"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass
    lg = logging.getLogger("fbk")
    for h in list(lg.handlers):
        if getattr(h, "_fbk_runner", False):
            lg.removeHandler(h)
            h.close()
    name = "fbk-%s.log" % datetime.fromtimestamp(now, SHANGHAI).strftime("%Y-%m-%d")
    fh = logging.FileHandler(str(logdir / name), encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(threadName)s %(message)s"))
    fh._fbk_runner = True
    lg.addHandler(fh)
    reasons = _LastWarning()
    reasons._fbk_runner = True
    lg.addHandler(reasons)
    if lg.level == logging.NOTSET or lg.level > logging.INFO:
        lg.setLevel(logging.INFO)
    return reasons


# ---------------- 并发限制 ----------------

class _LimitedFetch:
    """把 fetcher 包一层：导出任务并发 ≤3（下载并发由线程池 ≤4 限制）。"""

    def __init__(self, fetch, exports=EXPORT_WORKERS):
        self._f = fetch
        self._sem = threading.BoundedSemaphore(exports)

    def export_to(self, *a, **kw):
        with self._sem:
            return self._f.export_to(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._f, name)


# ---------------- 一轮运行 ----------------

def _item_path(item) -> str:
    return "/".join(tuple(item.rel_dir) + (item.name or item.token,))


def _att_path(hit) -> str:
    return "%s/%s/%s" % (chats.ATTACH_DIR, hit.chat_name or hit.chat_id, hit.file_name)


class _Run:
    def __init__(self, client, store, root: Path, fetch, report: RunReport, reasons: _LastWarning,
                 now: int, limit: Optional[int], lookback_days: int):
        self.client = client
        self.lookback_days = lookback_days
        self.store = store
        self.root = root
        self.report = report
        self.reasons = reasons
        self.now = now
        self.limit = limit
        self.ctx = sync.SyncContext(client=client, store=store, root=root, fetch=_LimitedFetch(fetch))
        self.submitted = 0
        self.walked = set()
        self.walk_errors = []
        self.file_fail_ids = set()  # 失败的文件：drive/共享项 token、附件 file_key
        self.other_fail_ids = set()  # 失败的链接检查
        self._ids_lock = threading.Lock()

    def _sync_one(self, item):
        self.reasons.clear()
        outcome = sync.sync_item(self.ctx, item)
        reason = ""
        if outcome == Outcome.FAILED:
            reason = self.reasons.take() or "未知原因，见日志"
            with self._ids_lock:
                self.file_fail_ids.add("item:" + item.token)
        elif outcome == Outcome.SKIPPED:
            reason = "类型 %s 不支持导出" % item.type
        self.report.add(outcome, _item_path(item), reason)

    def phase(self, items, counted: bool):
        """一个阶段：阶段内并发，全部结束（或遇到致命错误）才返回。致命错误原样抛出。"""
        fatal = []

        def work(item):
            try:
                self._sync_one(item)
            except _FATAL as e:
                fatal.append(e)
                raise

        ex = ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS, thread_name_prefix="fbk")
        futures = []
        try:
            for item in items:
                if fatal:
                    break
                if counted and self.limit is not None and self.submitted >= self.limit:
                    break
                if counted:
                    self.walked.add(item.token)
                    self.submitted += 1
                if not item.token:
                    log.warning("缺少 token，已跳过：%s", _item_path(item))
                    continue
                futures.append(ex.submit(work, item))
            for f in futures:
                f.result()  # 工作线程里的 LoginRequired / DiskFull / NetworkError 在这里重新抛出
        finally:
            ex.shutdown(wait=True, cancel_futures=True)
        if fatal:
            raise fatal[0]

    def drive(self):
        errors = []
        self.phase(walker.walk_my_space(self.client, errors), counted=True)
        self.phase(walker.walk_wiki(self.client, errors), counted=True)
        self.walk_errors = errors
        for e in errors:
            self.report.error("%s：%s" % ("/".join(e.rel_dir), e.message))
        if self.limit is not None:
            self.report.note("本次为限量试跑（最多 %d 项），未扫描群聊和搜索，未标记云端已删除" % self.limit)
            return
        if errors:
            log.warning("遍历有 %d 处失败，本轮不标记云端已删除", len(errors))
            return
        shared_rows = {t for t in self.store.all_tokens(("ok",))
                       if (self.store.get_item(t) or {"source": None})["source"] == "shared"}
        n = sync.mark_missing(self.store, self.walked | shared_rows)
        if n:
            self.report.note("云端已删除或已移出范围：%d 个（本地文件保留）" % n)

    def save_attachment(self, hit):
        self.reasons.clear()
        try:
            outcome = chats.save_attachment(self.ctx, hit)
        except _FATAL:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("附件保存失败：%s（%s）", _att_path(hit), e)
            outcome = Outcome.FAILED
        reason = (self.reasons.take() or "未知原因，见日志") if outcome == Outcome.FAILED else ""
        if outcome == Outcome.FAILED:
            with self._ids_lock:
                self.file_fail_ids.add("att:" + hit.file_key)
        self.report.add(outcome, _att_path(hit), reason)

    # ---- 阶段 3：共享给我 ----

    def _shared_rows(self, status):
        rows = (self.store.get_item(t) for t in self.store.all_tokens((status,)))
        return [r for r in rows if r is not None and r["source"] == "shared"]

    @staticmethod
    def _item_from_row(row, mtime=None) -> walker.RemoteItem:
        rel = tuple(json.loads(row["remote_dir"])) if row["remote_dir"] else (chats.SHARED_DIR,)
        return walker.RemoteItem(token=row["token"], type=row["type"], name=row["name"] or row["token"],
                                 rel_dir=rel, source="shared",
                                 mtime=int(mtime if mtime is not None else (row["remote_mtime"] or 0)))

    def _pending_shared(self):
        """上轮没完成的共享文件（status=failed）+ 已备份但云端有更新的共享文件。"""
        failed = self._shared_rows("failed")
        # 有本地文件的 failed 行多半是「更新失败」：要用云端最新 mtime，否则会被当成没变
        refresh_failed = [r for r in failed if r["local_path"]]
        ok_rows = self._shared_rows("ok")
        ask = ok_rows + refresh_failed
        mt = chats.fetch_mtimes(self.client, [(r["token"], r["type"]) for r in ask]) if ask else {}
        out = []
        for r in failed:
            remote = mt.get(r["token"], (0, ""))[0] if r["local_path"] else 0
            out.append(self._item_from_row(r, max(remote, r["remote_mtime"] or 0)))
        for r in ok_rows:
            remote = mt.get(r["token"], (0, ""))[0]
            if remote > (r["remote_mtime"] or 0):
                out.append(self._item_from_row(r, remote))
        return out

    def _remember(self, item):
        """发现即落清单：本轮没来得及同步（中断、盘满）的共享文件，下轮按 failed 重试。"""
        if self.store.get_item(item.token) is None:
            self.store.upsert_item(token=item.token, type=item.type, source="shared",
                                   remote_dir=list(item.rel_dir), name=item.name,
                                   remote_mtime=item.mtime, status="failed")

    def _link(self, hit, known, found):
        try:
            item = perms.process_link(self.client, self.store, hit, known, self.now)
        except ApiError as e:
            log.warning("链接检查失败，下轮重试：%s（%s）", hit.url or hit.token, e)
            self.store.upsert_link(hit.token, kind=hit.kind, status="retry", hint=hit.hint, sender=hit.sender,
                                   chat_name=hit.chat_name, sent_at=hit.sent_at, first_seen=int(self.now),
                                   url=hit.url)
            with self._ids_lock:
                self.other_fail_ids.add("link:" + hit.token)
            self.report.add(Outcome.FAILED, "链接 %s %s" % (hit.chat_name, hit.url or hit.token),
                            "无法判断权限（下轮重试）：%s" % e)
            return
        if item is not None:
            known.add(item.token)
            self._remember(item)
            found.append(item)

    def shared(self):
        retry_atts = chats.failed_attachment_hits(self.store)  # 先取，避免本轮新失败的再重试一次
        retry_links = [chats.LinkHit(r["kind"], r["token"], r["chat_name"] or "", r["sender"] or "",
                                     r["sent_at"] or 0, r["hint"] or "", r["url"] or "")
                       for r in self.store.links() if r["status"] == "retry"]
        found = self._pending_shared()
        for hit in retry_atts:
            self.save_attachment(hit)
        known = set(self.store.all_tokens()) | set(self.ctx.done)
        for hit in retry_links:
            self._link(hit, known, found)
        try:
            for hit in chats.scan(self.client, self.store, self.lookback_days, self.now):
                if isinstance(hit, chats.AttachmentHit):
                    self.save_attachment(hit)
                elif isinstance(hit, chats.LinkHit):
                    self._link(hit, known, found)
        except ApiError as e:
            log.warning("群聊扫描失败：%s", e)
            self.report.error("群聊扫描：%s" % e)
        try:
            for item in chats.search_shared(self.client, known):
                known.add(item.token)
                self._remember(item)
                found.append(item)
        except ApiError as e:
            log.warning("搜索共享文件失败：%s", e)
            self.report.error("搜索共享文件：%s" % e)
        uniq, seen = [], set()
        for item in found:
            if item.token not in seen:
                seen.add(item.token)
                uniq.append(item)
        self.phase(uniq, counted=False)


def _notify_failures(r) -> None:
    """失败集合（文件、附件、链接、没读全的目录）和上次通知时一样就不再通知，避免天天提醒同一批。"""
    walk_ids = {"walk:" + "/".join(e.rel_dir) for e in r.walk_errors}
    ids = r.file_fail_ids | r.other_fail_ids | walk_ids
    if not (r.file_fail_ids or walk_ids):
        ids = set()  # 只有链接检查失败不发通知（CSV 里已标「待重试」）
    msg = "飞书备份有 %d 个文件失败" % len(r.file_fail_ids)
    if r.walk_errors:
        msg += "、%d 处目录没读全" % len(r.walk_errors)
    notify.notify_if_changed(TITLE, msg + "，详见 _报告", "run_failures", ids, now=r.now)


def _check_stale(last_success, cfg, now) -> None:
    """定时运行开头：连续错过两个计划时间点就提醒（每天最多一次），防止静默地一直不成功。"""
    hh, mm = schedule.parse_time(cfg.schedule_time)
    if not schedule.is_stale(last_success, now, cfg.schedule_weekday, hh, mm):
        return
    days = (int(now) - int(last_success)) // 86400
    log.warning("已经 %d 天没有成功完成备份", days)
    notify.notify(TITLE, "飞书备份已经 %d 天没有成功完成，对你的 AI 助手说『飞书备份状态』看原因" % days,
                  key="stale", now=now)


_NO_WAIT_ABORTS = ("offline", "login_required", "disk_full")  # 不会重复一次昂贵扫描的中止


def _waiting_retry(row, now) -> bool:
    """上次是一次真实尝试且没读全（ok=0），距今不满 6 小时 → True。
    冒烟跑（--limit，summary 里有 limit）和离线 / 需重新登录 / 磁盘满的中止不算「没读全」，不拖慢下次运行。"""
    if row is None or row["ok"]:
        return False
    try:
        summary = json.loads(row["summary"]) if row["summary"] else {}
    except ValueError:
        summary = {}
    if not isinstance(summary, dict) or "limit" in summary or summary.get("中止") in _NO_WAIT_ABORTS:
        return False
    return int(now) - int(row["finished"]) < schedule.RETRY_AFTER_INCOMPLETE


def _on_term(signum, frame):
    raise SystemExit(128 + signum)


def install_term_handler() -> None:
    """launchd 在关机/注销时发 SIGTERM：转成 SystemExit，让 finally（释放锁、关清单）都能执行。
    只能在主线程调用（fbk.py run 入口）。"""
    signal.signal(signal.SIGTERM, _on_term)
    if hasattr(signal, "SIGBREAK"):  # Windows 只有 Ctrl-Break 会发 SIGBREAK；关控制台 / 关机会直接结束进程，由系统释放锁和防睡眠状态
        signal.signal(signal.SIGBREAK, _on_term)


def _probe(transport, token: str) -> None:
    """第一个请求：只发一次、不重试；连不上就当离线。"""
    try:
        transport("GET", DEFAULT_BASE + PROBE_PATH, {"Authorization": "Bearer " + token}, None)
    except _OFFLINE_ERRORS as e:
        raise _Offline(str(e))


def _report_paths(root: Path, now: int):
    """备份日志每次运行一份（YYYY-MM-DD_HHMM，北京时间），同日重跑不覆盖；权限缺口 CSV 是快照，按天一份。"""
    t = datetime.fromtimestamp(now, SHANGHAI)
    d = root / REPORT_DIR
    return (d / ("%s_备份日志.md" % t.strftime("%Y-%m-%d_%H%M")),
            d / ("%s_权限缺口.csv" % t.strftime("%Y-%m-%d")))


def run(now: Optional[int] = None, force: bool = False, limit: Optional[int] = None, *,
        transport=None, fetch=None, sleep=time.sleep) -> int:
    """执行一轮备份，返回退出码。transport / fetch / sleep 仅供测试注入。"""
    now = int(now if now is not None else time.time())
    reasons = _setup_logging(time.time())
    transport = transport or default_transport
    fetch = fetch or fetcher

    try:
        cfg = config.load_config()
    except config.ConfigCorrupt as e:
        log.error("%s", e)
        notify.notify(TITLE, "配置文件损坏，需要重新设置：对你的 AI 助手说『飞书备份设置』", key="config_missing")
        return 2
    except config.ConfigMissing as e:
        log.error("%s", e)
        notify.notify(TITLE, "还没有完成设置：对你的 AI 助手说『飞书备份设置』", key="config_missing")
        return 2
    root = cfg.backup_root
    try:
        root_ok = root is not None and Path(root).is_dir()
        if root_ok:
            root = Path(root)
            store = Store(root / schedule.STATE_DIR / "manifest.sqlite")
    except PermissionError as e:
        log.error("没有权限访问备份目录 %s：%s", root, e)
        notify.notify(TITLE, _msg_permission(), key="error")
        return 2
    except (OSError, sqlite3.Error) as e:
        log.exception("打不开备份清单")
        notify.notify(TITLE, "打不开备份清单（%s），详见日志：%s" % (e, config.LOG_DIR), key="error")
        return 1
    if not root_ok:
        log.warning("备份目录不存在（外接盘没插？）：%s", root)
        notify.notify(TITLE, "备份目录不存在：%s。插上备份盘后会自动继续。" % root, key="root_missing")
        return 2

    try:
        last = store.last_success()
        if not force:
            _check_stale(last, cfg, now)
            if _waiting_retry(store.last_run(), now):
                log.info("上次没读全，6 小时内不重试，跳过")
                return 0
            hh, mm = schedule.parse_time(cfg.schedule_time)
            if not schedule.is_due(last, now, cfg.schedule_weekday, hh, mm):
                log.info("本周期已经成功备份过，跳过")
                return 0
        try:
            lock = schedule.RunLock(root).acquire()
        except schedule.AlreadyRunning as e:
            log.info("%s", e)
            return 0
        except PermissionError as e:
            log.error("没有权限访问备份目录 %s：%s", root, e)
            notify.notify(TITLE, _msg_permission(), key="error")
            return 2
        try:
            with awake.keep_awake():
                return _locked(cfg, root, store, transport, fetch, sleep, reasons, now, limit)
        finally:
            lock.release()
    finally:
        store.close()


def _locked(cfg, root, store, transport, fetch, sleep, reasons, now, limit) -> int:
    n = fetcher.clean_parts(root)
    if n:
        log.info("清理了 %d 个中断留下的临时文件", n)
    tm = auth.TokenManager(cfg, transport=transport)
    run_id = None
    report = RunReport()
    started = int(time.time())
    try:
        _probe(transport, tm.access_token())
        client = FeishuClient(tm.access_token, transport=transport, sleep=sleep)
        run_id = store.start_run()
        r = _Run(client, store, root, fetch, report, reasons, now, limit, cfg.lookback_days)
        r.drive()
        if limit is None:
            r.shared()
        log_path, csv_path = _report_paths(root, now)
        gaps = perms.write_report(store, csv_path) if limit is None else {}
        summary = dict(report.summary(), **({"权限缺口": gaps} if gaps else {}))
        if limit is not None:
            summary["limit"] = limit  # 冒烟跑标记：不触发「没读全 6 小时重试」
        report.write_markdown(log_path, started, int(time.time()))
        # 遍历不完整也不算成功：下个小时醒来再跑（设计 §6.3）
        store.finish_run(run_id, ok=limit is None and not r.walk_errors, summary=summary)
        log.info("本轮完成：%s", summary)
        _notify_failures(r)
        return 0
    except (_Offline, auth.NetworkError) as e:
        log.info("网络不通，本轮跳过：%s", e)
        _abort(store, run_id, report, root, now, started, "offline")
        return 0
    except auth.LoginRequired as e:
        log.error("需要重新登录：%s", e)
        _abort(store, run_id, report, root, now, started, "login_required")
        notify.notify(TITLE, MSG_LOGIN, key="login_required")
        return 3
    except fetcher.DiskFull as e:
        log.error("%s", e)
        _abort(store, run_id, report, root, now, started, "disk_full")
        notify.notify(TITLE, MSG_DISK, key="disk_full")
        return 4
    except Exception as e:  # noqa: BLE001
        log.exception("备份运行出错")
        _abort(store, run_id, report, root, now, started, "error: %s" % e)
        notify.notify(TITLE, "备份运行出错，详见日志：%s" % config.LOG_DIR, key="error")
        return 1


def _abort(store, run_id, report, root, now, started, why):
    if run_id is None:
        return
    report.note("本轮中止：%s" % why)
    try:
        report.write_markdown(_report_paths(root, now)[0], started, int(time.time()))
    except Exception:  # noqa: BLE001 —— 盘满时写报告也可能失败
        log.exception("写运行报告失败")
    try:
        store.finish_run(run_id, ok=False, summary=dict(report.summary(), 中止=why))
    except Exception:  # noqa: BLE001
        log.exception("记录运行结果失败")
