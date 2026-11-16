"""群消息扫描：从群聊里发现文档链接和文件附件，并把附件备份下来。只读。"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Iterator, Union

from . import fetcher
from .client import ApiError
from .names import EXPORT_EXT, SHANGHAI, sanitize, unique_path
from .platform.fsnames import lp
from .sync import Outcome, _PLACE_LOCK
from .walker import RemoteItem

log = logging.getLogger(__name__)

LINK_RE = re.compile(r"https?://[\w.-]*feishu\.cn/(docx|docs|doc|sheets|base|wiki|file|mindnotes|slides)/([A-Za-z0-9]{10,})")
KIND_TO_TYPE = {"docx": "docx", "docs": "doc", "doc": "doc", "sheets": "sheet", "base": "bitable",
                "file": "file", "mindnotes": "mindnote", "slides": "slides"}
ATTACHMENT_TYPES = {"file", "audio", "media"}
ATTACH_DIR = "群聊附件"
SHARED_DIR = "共享给我"
_URL_RE = re.compile(r"https?://\S+")
_HINT_LEN = 80
_BATCH = 200
_SEARCH_PAGE = 50
_SEARCH_CAP = 200  # 搜索接口要求 offset + count < 200


@dataclass
class LinkHit:
    kind: str
    token: str
    chat_name: str
    sender: str
    sent_at: int
    hint: str
    url: str = ""


@dataclass
class AttachmentHit:
    chat_id: str
    chat_name: str
    message_id: str
    file_key: str
    file_name: str
    sent_at: int


def _unescape(s: str) -> str:
    return s.replace("\\/", "/")


def _find_links(body_json: str):
    seen, out = set(), []
    for m in LINK_RE.finditer(_unescape(body_json or "")):
        if m.group(2) in seen:
            continue
        seen.add(m.group(2))
        out.append((m.group(1), m.group(2), m.group(0)))
    return out


def extract_links(body_json: str) -> list:
    return [(k, t) for k, t, _u in _find_links(body_json)]


def attachment_filename(sent_at: int, name: str) -> str:
    return "%s_%s" % (datetime.fromtimestamp(int(sent_at), SHANGHAI).strftime("%Y-%m-%d"), name)


def _texts(node, out):
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                if k in ("text", "content", "title"):
                    out.append(v)
            else:
                _texts(v, out)
    elif isinstance(node, list):
        for v in node:
            _texts(v, out)


def _hint(content: str) -> str:
    try:
        data = json.loads(content)
    except ValueError:
        data = None
    if isinstance(data, dict):
        title = ((data.get("header") or {}).get("title") or {}).get("content") if isinstance(data.get("header"), dict) else None
        if title:
            return title[:_HINT_LEN]
        parts = []
        _texts(data, parts)
        text = " ".join(parts)
    else:
        text = content or ""
    return _URL_RE.sub("", _unescape(text))[:_HINT_LEN]


def _members(client, chat_id) -> dict:
    names = {}
    try:
        for m in client.paginate("/im/v1/chats/%s/members" % chat_id,
                                 {"member_id_type": "open_id", "page_size": 100}, "items"):
            if m.get("member_id"):
                names[m["member_id"]] = m.get("name") or m["member_id"]
    except ApiError as e:
        log.info("群成员列表不可用，改用 id 显示：%s（%s）", chat_id, e)
    return names


def _scan_chat(client, chat_id, chat_name, start) -> Iterator[Union[LinkHit, AttachmentHit]]:
    names = None
    params = {"container_id_type": "chat", "container_id": chat_id, "start_time": str(start),
              "sort_type": "ByCreateTimeAsc", "page_size": 50}
    for m in client.paginate("/im/v1/messages", params, "items"):
        content = (m.get("body") or {}).get("content") or ""
        sent_at = int(int(m.get("create_time") or 0) // 1000)
        links = _find_links(content)
        mtype = m.get("msg_type")
        att = None
        if mtype in ATTACHMENT_TYPES:
            try:
                c = json.loads(content)
            except ValueError:
                c = {}
            if isinstance(c, dict) and c.get("file_key"):
                att = (c["file_key"], c.get("file_name") or "%s.opus" % m["message_id"])
        if links:
            if names is None:
                names = _members(client, chat_id)
            sid = (m.get("sender") or {}).get("id") or ""
            hint = _hint(content)
            for kind, token, url in links:
                yield LinkHit(kind, token, chat_name, names.get(sid, sid), sent_at, hint, url)
        if att:
            yield AttachmentHit(chat_id, chat_name, m["message_id"], att[0], att[1], sent_at)
        yield sent_at  # 内部：用于推进游标


def scan(client, store, lookback_days: int, now: int) -> Iterator[Union[LinkHit, AttachmentHit]]:
    floor = int(now) - int(lookback_days) * 86400
    for chat in client.paginate("/im/v1/chats", {"page_size": 100}, "items"):
        chat_id = chat["chat_id"]
        raw_name = (chat.get("name") or "").strip()
        chat_name = sanitize(raw_name) if raw_name.strip(".") else chat_id
        cur = store.get_cursor(chat_id)
        start = max(cur + 1 if cur is not None else 0, floor)
        latest = None
        try:
            for x in _scan_chat(client, chat_id, chat_name, start):
                if isinstance(x, int):
                    latest = x if latest is None else max(latest, x)
                else:
                    yield x
        except ApiError as e:
            log.warning("群消息读取失败，已跳过：%s（%s）", chat_name, e)
            continue
        if latest is not None:
            store.set_cursor(chat_id, latest)


def save_attachment(ctx, hit: AttachmentHit) -> Outcome:
    store = ctx.store
    row = store.get_attachment(hit.file_key)
    if row is not None and row["status"] in ("ok", "duplicate"):
        return Outcome.UNCHANGED
    folder = ctx.root / ATTACH_DIR / (sanitize(hit.chat_name) or hit.chat_id)
    desired = folder / sanitize(attachment_filename(hit.sent_at, hit.file_name))
    # 临时名取完整 file_key 的哈希：file_key 前缀常常相同，截前缀会撞
    tmp = folder / (".dl-%s.fbk-new" % hashlib.sha1(hit.file_key.encode("utf-8")).hexdigest()[:16])
    base = dict(chat_id=hit.chat_id, message_id=hit.message_id, file_name=hit.file_name,
                chat_name=hit.chat_name, sent_at=hit.sent_at)
    ctx.fetch.ensure_space(ctx.root, 0)
    try:
        ctx.fetch.download_attachment(ctx.client, hit.message_id, hit.file_key, tmp)
        sha = ctx.fetch.sha256_file(tmp)
        blob = store.find_blob(sha)
        if blob is not None and os.path.isfile(lp(blob)):
            os.unlink(lp(tmp))
            store.upsert_attachment(hit.file_key, sha256=sha, local_path=blob, status="duplicate", **base)
            return Outcome.DUPLICATE
        with _PLACE_LOCK:
            target = unique_path(desired)  # 被占用（不区分大小写）就换名，绝不覆盖
            os.replace(lp(tmp), lp(target))
    except (ApiError, OSError) as e:  # DiskFull / NetworkError / LoginRequired 不是这两类，照常抛出
        log.warning("附件下载失败：%s/%s（%s）", hit.chat_name, hit.file_name, e)
        store.upsert_attachment(hit.file_key, status="failed", **base)
        return Outcome.FAILED
    finally:
        if os.path.isfile(lp(tmp)):
            os.unlink(lp(tmp))
    store.add_blob(sha, target)
    store.upsert_attachment(hit.file_key, sha256=sha, local_path=target, status="ok", **base)
    return Outcome.NEW


def failed_attachment_hits(store) -> list:
    """清单里下载失败的附件，重建成 AttachmentHit，供 runner 每轮重试。"""
    return [AttachmentHit(r["chat_id"], r["chat_name"] or r["chat_id"], r["message_id"], r["file_key"],
                          r["file_name"] or "%s.opus" % r["message_id"], r["sent_at"] or 0)
            for r in store.attachments_by_status("failed")]


def fetch_mtimes(client, pairs) -> dict:
    """pairs: [(token, type)] → {token: (mtime, title)}。批量 ≤200。失败的批次缺省。"""
    out = {}
    pairs = list(pairs)
    for i in range(0, len(pairs), _BATCH):
        chunk = pairs[i:i + _BATCH]
        try:
            data = client.post("/drive/v1/metas/batch_query",
                               {"request_docs": [{"doc_token": t, "doc_type": ty} for t, ty in chunk]})
        except ApiError as e:
            log.warning("读取修改时间失败：%s", e)
            continue
        for meta in data.get("metas") or []:
            try:
                out[meta["doc_token"]] = (int(meta.get("latest_modify_time") or 0), meta.get("title") or "")
            except (TypeError, ValueError):
                out[meta.get("doc_token")] = (0, meta.get("title") or "")
    return out


def search_shared(client, known: set) -> Iterator[RemoteItem]:
    found, seen, offset = [], set(), 0
    while True:
        count = min(_SEARCH_PAGE, _SEARCH_CAP - 1 - offset)
        if count <= 0:
            log.info("搜索结果超过接口上限（offset+count < %d），后面的没有取", _SEARCH_CAP)
            break
        try:
            data = client.post("/suite/docs-api/search/object",
                               {"search_key": "", "count": count, "offset": offset})
        except ApiError as e:
            log.warning("搜索翻页失败，保留已取到的 %d 个：%s", len(found), e)
            break
        ents = data.get("docs_entities") or []
        for e in ents:
            ty, tok = e.get("docs_type"), e.get("docs_token")
            if tok and tok not in known and tok not in seen and (ty == "file" or ty in EXPORT_EXT):
                seen.add(tok)
                found.append((tok, ty, e.get("title") or tok))
        if not data.get("has_more") or not ents:
            break
        offset += count
    mt = fetch_mtimes(client, [(t, ty) for t, ty, _ in found])
    for tok, ty, title in found:
        yield RemoteItem(token=tok, type=ty, name=title, rel_dir=(SHARED_DIR, "_搜索发现"),
                         source="shared", mtime=mt.get(tok, (0, ""))[0])
