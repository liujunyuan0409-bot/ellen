"""遍历我的空间 / 知识库 / 快捷方式，产出 RemoteItem。只读。"""
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from .client import ApiError
from .names import sanitize

log = logging.getLogger(__name__)

BATCH = 200


@dataclass
class RemoteItem:
    token: str
    type: str
    name: str
    rel_dir: tuple
    source: str  # "my" | "wiki" | "shared"
    mtime: int
    has_children: bool = False


@dataclass
class WalkError:
    rel_dir: tuple
    message: str


TOP_DIRS = ("我的空间", "知识库")


def parse_only(text: str) -> tuple:
    """把用户给的目录（「产品部/质量文件」「我的空间\\共享\\产品部」）拆成路径段，按备份目录的命名规则清洗。"""
    parts = [p.strip() for p in re.split(r"[/\\]+", text or "")]
    return tuple(_key(sanitize(p)) for p in parts if p)


def _key(seg: str) -> str:
    return unicodedata.normalize("NFC", seg).casefold()


def in_scope(item: "RemoteItem", only: tuple) -> bool:
    """item（连同它自己的名字）的路径以 only 开头。开头的「我的空间」「知识库」可以省略。不区分大小写。"""
    if not only:
        return True
    path = tuple(_key(s) for s in tuple(item.rel_dir) + (sanitize(item.name or ""),))
    n = len(only)
    if path[:n] == only:
        return True
    return bool(path) and path[0] in {_key(t) for t in TOP_DIRS} and path[1:1 + n] == only


def filter_only(items: Iterable["RemoteItem"], only: tuple) -> Iterator["RemoteItem"]:
    for it in items:
        if in_scope(it, only):
            yield it


def _record(errors, rel_dir, err):
    log.warning("遍历失败，已跳过：%s（%s）", "/".join(rel_dir), err)
    if errors is not None:
        errors.append(WalkError(rel_dir, str(err)))


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _resolve_shortcuts(client, shortcuts, rel_dir, errors):
    """shortcuts: [(token, type)]，返回 {token: mtime}；查不到或批次失败的不在结果里。"""
    out = {}
    uniq = []
    seen = set()
    for tok, typ in shortcuts:
        if tok not in seen:
            seen.add(tok)
            uniq.append((tok, typ))
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        try:
            data = client.post("/drive/v1/metas/batch_query", {
                "request_docs": [{"doc_token": t, "doc_type": ty} for t, ty in chunk],
                "with_url": False})
        except ApiError as e:
            _record(errors, rel_dir, e)
            continue
        for m in data.get("metas") or []:
            out[m.get("doc_token")] = _int(m.get("latest_modify_time"))
    return out


def walk_my_space(client, errors: Optional[list] = None) -> Iterator[RemoteItem]:
    top = ("我的空间",)
    try:
        root = client.get("/drive/explorer/v2/root_folder/meta")["token"]
    except ApiError as e:
        _record(errors, top, e)
        return
    visited = set()

    def walk(folder_token, rel_dir):
        if folder_token in visited:
            return
        visited.add(folder_token)
        try:
            files = list(client.paginate("/drive/v1/files", {"folder_token": folder_token, "page_size": 200},
                                         "files", token_field="next_page_token"))
        except ApiError as e:
            _record(errors, rel_dir, e)
            return
        shortcuts = []
        for f in files:
            if f.get("type") == "shortcut":
                info = f.get("shortcut_info") or {}
                if info.get("target_token") and info.get("target_type") != "folder":
                    shortcuts.append((info["target_token"], info.get("target_type") or ""))
        mtimes = _resolve_shortcuts(client, shortcuts, rel_dir, errors) if shortcuts else {}
        for f in files:
            typ = f.get("type")
            name = f.get("name") or ""
            if typ == "folder":
                yield from walk(f["token"], rel_dir + (sanitize(name),))
            elif typ == "shortcut":
                info = f.get("shortcut_info") or {}
                tgt, ttype = info.get("target_token"), info.get("target_type") or ""
                if not tgt:
                    log.warning("快捷方式无可用目标，已跳过：%s", name)
                elif ttype == "folder":
                    yield from walk(tgt, rel_dir + (sanitize(name),))
                elif tgt not in mtimes:
                    log.warning("快捷方式目标查不到，已跳过：%s", name)
                else:
                    yield RemoteItem(tgt, ttype, name, rel_dir, "my", mtimes[tgt])
            else:
                yield RemoteItem(f["token"], typ, name, rel_dir, "my", _int(f.get("modified_time")))

    yield from walk(root, top)


def walk_wiki(client, errors: Optional[list] = None) -> Iterator[RemoteItem]:
    try:
        spaces = list(client.paginate("/wiki/v2/spaces", {"page_size": 50}, "items"))
    except ApiError as e:
        _record(errors, ("知识库",), e)
        return
    for space in spaces:
        sid = space["space_id"]
        base = ("知识库", sanitize(space.get("name") or sid))
        visited = set()

        def walk(parent_token, rel_dir):
            params = {"page_size": 50}
            if parent_token:
                params["parent_node_token"] = parent_token
            try:
                nodes = list(client.paginate("/wiki/v2/spaces/%s/nodes" % sid, params, "items"))
            except ApiError as e:
                _record(errors, rel_dir, e)
                return
            for n in nodes:
                nt = n.get("node_token")
                if nt in visited:
                    continue
                visited.add(nt)
                title = n.get("title") or ""
                has_child = bool(n.get("has_child"))
                yield RemoteItem(n.get("obj_token"), n.get("obj_type"), title, rel_dir, "wiki",
                                 _int(n.get("obj_edit_time")), has_child)
                if has_child:
                    yield from walk(nt, rel_dir + (sanitize(title),))

        yield from walk(None, base)
