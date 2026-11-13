"""遍历我的空间 / 知识库 / 快捷方式，产出 RemoteItem。只读。"""
import logging
from dataclasses import dataclass
from typing import Iterator

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


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _resolve_shortcuts(client, shortcuts):
    """shortcuts: [(token, type)]，返回 {token: mtime}；查不到的不在结果里。"""
    out = {}
    uniq = []
    for tok, typ in shortcuts:
        if tok not in [u[0] for u in uniq]:
            uniq.append((tok, typ))
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        data = client.post("/drive/v1/metas/batch_query", {
            "request_docs": [{"doc_token": t, "doc_type": ty} for t, ty in chunk],
            "with_url": False})
        for m in data.get("metas") or []:
            out[m.get("doc_token")] = _int(m.get("latest_modify_time"))
    return out


def _list_folder(client, folder_token):
    return list(client.paginate("/drive/v1/files", {"folder_token": folder_token, "page_size": 200},
                                "files", token_field="next_page_token"))


def walk_my_space(client) -> Iterator[RemoteItem]:
    root = client.get("/drive/explorer/v2/root_folder/meta")["token"]
    visited = set()

    def walk(folder_token, rel_dir):
        if folder_token in visited:
            return
        visited.add(folder_token)
        files = _list_folder(client, folder_token)
        shortcuts = []
        for f in files:
            if f.get("type") == "shortcut":
                info = f.get("shortcut_info") or {}
                if info.get("target_token") and info.get("target_type") != "folder":
                    shortcuts.append((info["target_token"], info.get("target_type") or ""))
        mtimes = _resolve_shortcuts(client, shortcuts) if shortcuts else {}
        for f in files:
            typ = f.get("type")
            name = f.get("name") or ""
            if typ == "folder":
                yield from walk(f["token"], rel_dir + (sanitize(name),))
            elif typ == "shortcut":
                info = f.get("shortcut_info") or {}
                tgt, ttype = info.get("target_token"), info.get("target_type") or ""
                if not tgt or ttype == "folder":
                    log.warning("快捷方式无可用目标，已跳过：%s", name)
                elif tgt not in mtimes:
                    log.warning("快捷方式目标查不到，已跳过：%s", name)
                else:
                    yield RemoteItem(tgt, ttype, name, rel_dir, "my", mtimes[tgt])
            else:
                yield RemoteItem(f["token"], typ, name, rel_dir, "my", _int(f.get("modified_time")))

    yield from walk(root, ("我的空间",))


def walk_wiki(client) -> Iterator[RemoteItem]:
    for space in client.paginate("/wiki/v2/spaces", {"page_size": 50}, "items"):
        sid = space["space_id"]
        base = ("知识库", sanitize(space.get("name") or sid))
        visited = set()

        def walk(parent_token, rel_dir):
            params = {"page_size": 50}
            if parent_token:
                params["parent_node_token"] = parent_token
            for n in client.paginate("/wiki/v2/spaces/%s/nodes" % sid, params, "items"):
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
