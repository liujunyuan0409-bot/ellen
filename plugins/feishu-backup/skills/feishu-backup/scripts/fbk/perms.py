"""链接可读性判断 + 权限缺口 CSV。只读。"""
import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from . import chats
from .client import ApiError
from .names import EXPORT_EXT, SHANGHAI, sanitize
from .walker import RemoteItem

log = logging.getLogger(__name__)

_STATUS_CN = {"ok": "能打开", "denied": "无权限", "gone": "已删除或无效", "retry": "待重试"}
_HEADER = ["状态", "类型", "链接", "文件名线索", "分享人", "群名", "分享时间", "首次发现日期"]
_LIMIT = "# 局限：只能发现出现在备份账号所在群里、或能搜索到的文件；全企业盘点需要付费版审计日志。"


def resolve(client, kind: str, token: str) -> Tuple[str, Optional[str], str]:
    """返回 (状态, 解析出的类型, 真实 token)。wiki 先用 get_node 换成真实文档 token。"""
    real, ftype = token, chats.KIND_TO_TYPE.get(kind)
    if kind == "wiki":
        try:
            data = client.get("/wiki/v2/spaces/get_node", {"token": token})
        except ApiError as e:
            if e.code in (131006, 1063002) or "permission" in str(e.msg).lower():
                return "denied", None, token
            return "gone", None, token
        node = data.get("node") or {}
        real, ftype = node.get("obj_token") or token, node.get("obj_type")
    try:
        data = client.get("/drive/v1/permissions/%s/members/auth" % real, {"type": ftype, "action": "view"})
    except ApiError as e:
        if e.code in (1063005, 1063001):
            return "gone", ftype, real
        raise
    return ("ok" if data.get("auth_result") else "denied"), ftype, real


def classify(client, kind: str, token: str) -> Tuple[str, Optional[str]]:
    status, ftype, _real = resolve(client, kind, token)
    return status, ftype


def process_link(client, store, hit, known: set, now: int) -> Optional[RemoteItem]:
    """判断链接状态并记入清单；能打开、又不在 known 里的可导出文档返回 RemoteItem 交给 sync_item。"""
    status, ftype, real = resolve(client, hit.kind, hit.token)
    store.upsert_link(hit.token, kind=hit.kind, status=status, hint=hit.hint, sender=hit.sender,
                      chat_name=hit.chat_name, sent_at=hit.sent_at, first_seen=int(now), url=hit.url)
    if status != "ok" or real in known or not (ftype == "file" or ftype in EXPORT_EXT):
        return None
    mtime, title = chats.fetch_mtimes(client, [(real, ftype)]).get(real, (0, ""))
    return RemoteItem(token=real, type=ftype, name=sanitize(title or hit.hint.strip()[:50] or real),
                      rel_dir=(chats.SHARED_DIR, hit.chat_name), source="shared", mtime=mtime)


def _fmt_time(ts):
    return datetime.fromtimestamp(int(ts), SHANGHAI).strftime("%Y-%m-%d %H:%M") if ts else ""


def _fmt_date(ts):
    return datetime.fromtimestamp(int(ts), SHANGHAI).strftime("%Y-%m-%d") if ts else ""


def write_report(store, path: Path) -> dict:
    rows = list(store.links())
    denied = sum(1 for r in rows if r["status"] == "denied")
    gone = sum(1 for r in rows if r["status"] == "gone")
    retry = sum(1 for r in rows if r["status"] == "retry")
    order = {"denied": 0, "gone": 1, "retry": 2, "ok": 3}
    rows.sort(key=lambda r: (order.get(r["status"], 4), r["token"]))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8-sig", newline="") as f:
        f.write("# 共发现 %d 个链接，无权限 %d 个，已删除或无效 %d 个，待重试 %d 个\r\n"
                % (len(rows), denied, gone, retry))
        f.write(_LIMIT + "\r\n")
        w = csv.writer(f)
        w.writerow(_HEADER)
        for r in rows:
            ty = chats.KIND_TO_TYPE.get(r["kind"], r["kind"])
            w.writerow([_STATUS_CN.get(r["status"], r["status"] or ""), ty, r["url"] or "", r["hint"] or "",
                        r["sender"] or "", r["chat_name"] or "", _fmt_time(r["sent_at"]),
                        _fmt_date(r["first_seen"])])
    return {"total": len(rows), "denied": denied, "gone": gone, "retry": retry}
