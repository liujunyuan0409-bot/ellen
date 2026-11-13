"""一次备份运行的编排：门禁 → 加锁 → 清理 → 分阶段同步 → 群聊 → 报告。

退出码：0 正常（含不到期、已有进程在跑、离线静默退出）；1 意外错误；2 未配置或备份目录不存在；
3 需要重新登录；4 备份盘空间不足。

分阶段约定（sync.SyncContext 的要求）：整轮只用一个 SyncContext；我的空间 → 知识库 → 共享给我
严格按顺序，上一阶段全部结束才开始下一阶段，只在阶段内并发；同一轮内不重试同一项。
"""
import http.client
import logging
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
from .report import RunReport
from .store import Store
from .sync import Outcome

log = logging.getLogger(__name__)

TITLE = "飞书备份"
MSG_LOGIN = "需要重新登录：对 Claude 说『飞书备份重新登录』"
MSG_DISK = "备份盘空间不足"
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

    def _sync_one(self, item):
        self.reasons.clear()
        outcome = sync.sync_item(self.ctx, item)
        reason = ""
        if outcome == Outcome.FAILED:
            reason = self.reasons.take() or "未知原因，见日志"
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
        self.report.add(outcome, _att_path(hit), reason)

    def shared(self):
        retry = chats.failed_attachment_hits(self.store)  # 先取，避免本轮新失败的再重试一次
        for hit in retry:
            self.save_attachment(hit)
        known = set(self.store.all_tokens()) | set(self.ctx.done)
        found = []
        try:
            for hit in chats.scan(self.client, self.store, self.lookback_days, self.now):
                if isinstance(hit, chats.AttachmentHit):
                    self.save_attachment(hit)
                elif isinstance(hit, chats.LinkHit):
                    try:
                        item = perms.process_link(self.client, self.store, hit, known, self.now)
                    except ApiError as e:
                        log.warning("链接检查失败：%s（%s）", hit.url or hit.token, e)
                        self.report.add(Outcome.FAILED, "链接 %s %s" % (hit.chat_name, hit.url or hit.token),
                                        "无法判断权限：%s" % e)
                        continue
                    if item is not None:
                        known.add(item.token)
                        found.append(item)
        except ApiError as e:
            log.warning("群聊扫描失败：%s", e)
            self.report.error("群聊扫描：%s" % e)
        try:
            found.extend(chats.search_shared(self.client, known))
        except ApiError as e:
            log.warning("搜索共享文件失败：%s", e)
            self.report.error("搜索共享文件：%s" % e)
        self.phase(found, counted=False)


def _probe(transport, token: str) -> None:
    """第一个请求：只发一次、不重试；连不上就当离线。"""
    try:
        transport("GET", DEFAULT_BASE + PROBE_PATH, {"Authorization": "Bearer " + token}, None)
    except _OFFLINE_ERRORS as e:
        raise _Offline(str(e))


def _report_paths(root: Path, now: int):
    day = datetime.fromtimestamp(now, SHANGHAI).strftime("%Y-%m-%d")
    d = root / REPORT_DIR
    return d / ("%s_备份日志.md" % day), d / ("%s_权限缺口.csv" % day)


def run(now: Optional[int] = None, force: bool = False, limit: Optional[int] = None, *,
        transport=None, fetch=None, sleep=time.sleep) -> int:
    """执行一轮备份，返回退出码。transport / fetch / sleep 仅供测试注入。"""
    now = int(now if now is not None else time.time())
    reasons = _setup_logging(time.time())
    transport = transport or default_transport
    fetch = fetch or fetcher

    try:
        cfg = config.load_config()
    except config.ConfigMissing as e:
        log.error("%s", e)
        notify.notify(TITLE, "还没有完成设置：对 Claude 说『飞书备份设置』", key="config_missing")
        return 2
    root = cfg.backup_root
    if root is None or not Path(root).is_dir():
        log.warning("备份目录不存在（外接盘没插？）：%s", root)
        notify.notify(TITLE, "备份目录不存在：%s。插上备份盘后会自动继续。" % root, key="root_missing")
        return 2
    root = Path(root)

    store = Store(root / schedule.STATE_DIR / "manifest.sqlite")
    try:
        if not force and not schedule.is_due(store.last_success(), cfg.interval_hours, now):
            log.info("距上次成功不足 %s 小时，跳过", cfg.interval_hours)
            return 0
        try:
            lock = schedule.RunLock(root).acquire()
        except schedule.AlreadyRunning as e:
            log.info("%s", e)
            return 0
        try:
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
        report.write_markdown(log_path, started, int(time.time()))
        store.finish_run(run_id, ok=limit is None, summary=summary)
        log.info("本轮完成：%s", summary)
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
