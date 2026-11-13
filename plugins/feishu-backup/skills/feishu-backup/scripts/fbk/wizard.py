"""配置向导各步骤的实测、doctor 体检、只统计不下载的扫描。只读，只用标准库。

所有对外文字都是中文，失败时 `fix` 写清楚下一步该做什么。
"""
import http.client
import math
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import auth, walker
from .client import ApiError
from .names import EXPORT_EXT

FEISHU_HOST = "open.feishu.cn"
MIN_PY = (3, 9)
GB = 1000 ** 3  # 与 Finder 显示一致，按十进制 GB
SPACE_FACTOR = 1.5

# 首次耗时估算（设计 §7）：导出每份约 15 秒、导出并发 3；下载约 5 MB/秒
EXPORT_SECONDS_EACH = 15
EXPORT_PARALLEL = 3
DOWNLOAD_BYTES_PER_SEC = 5 * 1000 ** 2
SAMPLE_PER_TYPE = 8
EXPORT_TIMEOUT = 120
EXPORT_POLL = 2.0

NET_DETAIL = "连不上飞书"
NET_FIX = "检查网络后重试"
SKIP_DETAIL = "没有可测的样本，跳过"
BOT_FIX = "在开发者后台为应用开启机器人能力并发版（应用能力 → 添加应用能力 → 机器人；版本管理与发布 → 创建版本并发布）"
SCOPE_FIX = "缺少权限 %s：开发者后台 → 权限管理 → 批量导入 → 发版 → 重新运行 setup --login"
CRED_FIX = ("去飞书开发者后台（https://open.feishu.cn/app）→ 你的应用 →「凭证与基础信息」核对 App ID 和 App Secret，"
            "并在「版本管理与发布」确认应用状态是「已启用」；改好后重新运行 setup --credentials")
LOGIN_FIX = "登录已失效或还没登录：运行 setup --login 重新登录"

TYPE_LABEL = {"docx": "文档", "doc": "旧版文档", "sheet": "电子表格", "bitable": "多维表格",
              "slides": "幻灯片", "file": "上传的文件", "mindnote": "思维笔记"}

_SCOPE_RE = re.compile(r"\b[a-z][a-z_]*(?::[a-z_.]+)+")
_SCOPE_CODES = {230027, 99991672, 99991679}
_TOKEN_CODES = {99991663, 99991668, 99991677}
_NET_ERRORS = (auth.NetworkError, urllib.error.URLError, OSError, http.client.HTTPException)


class Cancelled(Exception):
    pass


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    warn: bool = False  # 通过但需要提醒（例如在 iCloud 同步目录里）


def gb(n) -> str:
    return "%.1f GB" % ((n or 0) / GB)


# ---------------- [1/7] 环境 ----------------

def check_env(connect: Callable = socket.create_connection) -> List[Check]:
    v = sys.version_info
    py = Check("Python 版本", tuple(v[:2]) >= MIN_PY, "当前 %d.%d.%d" % (v[0], v[1], v[2]),
               "" if tuple(v[:2]) >= MIN_PY else "需要 Python 3.9 或更高：在终端运行 brew install python3 后重试")
    try:
        s = connect((FEISHU_HOST, 443), timeout=5)
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        net = Check("网络", True, "能连上 %s" % FEISHU_HOST)
    except OSError:
        net = Check("网络", False, NET_DETAIL, NET_FIX)
    return [py, net]


# ---------------- [2/7] 凭证 ----------------

DIALOG_TIMEOUT = 300  # 秒：弹窗 5 分钟没人填就自动关闭


def _dialog(prompt: str, hidden: bool, runner) -> str:
    script = ('text returned of (display dialog "%s" default answer ""%s with title "飞书备份" '
              'giving up after %d)') % (
        prompt.replace("\\", "\\\\").replace('"', '\\"'), " with hidden answer" if hidden else "",
        DIALOG_TIMEOUT)
    try:
        r = runner(["osascript", "-e", script], capture_output=True, text=True, timeout=DIALOG_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        raise Cancelled("已取消：5 分钟内没有填写，凭证未保存")
    except OSError:  # 含 FileNotFoundError：系统里没有 osascript
        raise Cancelled("已取消：无法弹出系统对话框（找不到 osascript），凭证未保存")
    if r.returncode != 0:
        raise Cancelled("已取消：没有填写，凭证未保存")
    value = (r.stdout or "").strip()
    if not value:
        raise Cancelled("已取消：输入为空或 5 分钟内没有填写，凭证未保存")
    return value


def prompt_secret(runner=subprocess.run) -> Tuple[str, str]:
    """弹两次系统对话框取 App ID 和 App Secret；输入不经过终端和聊天，也不打印。"""
    app_id = _dialog("请输入飞书应用的 App ID（开发者后台 → 你的应用 →「凭证与基础信息」）", False, runner)
    secret = _dialog("请输入 App Secret（同一页面；输入内容不会显示）", True, runner)
    return app_id, secret


def check_credentials(cfg, transport=None) -> Check:
    try:
        auth.tenant_token(cfg, transport)
    except auth.LoginRequired:
        return Check("应用凭证", False, "App ID 或 App Secret 换不到访问令牌", CRED_FIX)
    except _NET_ERRORS:
        return Check("应用凭证", False, NET_DETAIL, NET_FIX)
    return Check("应用凭证", True, "凭证有效，能换到访问令牌")


# ---------------- [4/7] 权限体检 ----------------

def _scopes_in(msg: str) -> List[str]:
    found = [s for s in _SCOPE_RE.findall(msg or "") if not s.startswith(("http", "https"))]
    known = [s for s in found if s in auth.SCOPES]
    out = []
    for s in known or found:
        if s not in out:
            out.append(s)
    return out


def explain(e: Exception, default_scope: str = "") -> Tuple[str, str]:
    """把异常翻译成 (detail, fix)。"""
    if isinstance(e, auth.LoginRequired):
        return "没有可用的登录", LOGIN_FIX
    if isinstance(e, _NET_ERRORS):
        return NET_DETAIL, NET_FIX
    if isinstance(e, ApiError):
        if e.code == -1:
            return NET_DETAIL, NET_FIX
        if e.code == 232025:
            return "应用没有开启机器人能力（错误码 232025）", BOT_FIX
        if e.code in _SCOPE_CODES:
            scopes = _scopes_in(str(e.msg)) or ([default_scope] if default_scope else [])
            return ("缺少权限（错误码 %s）" % e.code,
                    SCOPE_FIX % ("、".join(scopes) if scopes else "（见错误信息）"))
        if e.code in _TOKEN_CODES:
            return "登录令牌无效（错误码 %s）" % e.code, LOGIN_FIX
        return ("飞书返回错误 code=%s：%s" % (e.code, e.msg),
                "稍后重试；仍失败就把这一行发给 Claude 排查")
    return "意外错误：%s" % e, "把这一行发给 Claude 排查"


def _check(name: str, scope: str, fn) -> Check:
    try:
        return Check(name, True, fn())
    except Exception as e:  # noqa: BLE001 —— 体检要把每一项都跑完
        detail, fix = explain(e, scope)
        return Check(name, False, detail, fix)


def doctor(client) -> List[Check]:
    """8 项实测。样本取自我的空间第一页（docx 用于导出，file 用于读 1 字节）和第一个群。"""
    s = {"docx": None, "file": None, "any": None, "chat": None}

    def my_space():
        root = client.get("/drive/explorer/v2/root_folder/meta")["token"]
        files = client.get("/drive/v1/files", {"folder_token": root, "page_size": 200}).get("files") or []
        for f in files:
            t = f.get("type")
            if t == "docx" and s["docx"] is None:
                s["docx"] = f["token"]
            if t == "file" and s["file"] is None:
                s["file"] = f["token"]
            if (t == "file" or t in EXPORT_EXT) and s["any"] is None:
                s["any"] = (f["token"], t)
        return "能读取我的空间（第一页 %d 项）" % len(files)

    def wiki():
        items = client.get("/wiki/v2/spaces", {"page_size": 50}).get("items") or []
        return "能读取知识库（第一页 %d 个空间）" % len(items)

    def search():
        client.post("/suite/docs-api/search/object", {"search_key": "", "count": 1, "offset": 0})
        return "搜索接口可用"

    def export():
        if not s["docx"]:
            return SKIP_DETAIL + "（我的空间第一页没有文档）"
        client.post("/drive/v1/export_tasks", {"file_extension": "pdf", "token": s["docx"], "type": "docx"})
        return "能发起文档导出（只发起任务，没有下载）"

    def one_byte():
        if not s["file"]:
            return SKIP_DETAIL + "（我的空间第一页没有上传的文件）"
        size = client.head_size("/drive/v1/files/%s/download" % s["file"])
        return "能下载上传的文件（读了 1 字节%s）" % ("，文件共 %d 字节" % size if size else "")

    def perm():
        if not s["any"]:
            return SKIP_DETAIL + "（我的空间第一页没有可判断的文件）"
        tok, typ = s["any"]
        client.get("/drive/v1/permissions/%s/members/auth" % tok, {"type": typ, "action": "view"})
        return "能判断文件权限"

    def list_chats():
        items = client.get("/im/v1/chats", {"page_size": 20}).get("items") or []
        if items:
            s["chat"] = items[0].get("chat_id")
        return "能列出群聊（第一页 %d 个群）" % len(items)

    def read_msg():
        if not s["chat"]:
            return SKIP_DETAIL + "（备份账号不在任何群里）"
        client.get("/im/v1/messages", {"container_id_type": "chat", "container_id": s["chat"],
                                       "sort_type": "ByCreateTimeDesc", "page_size": 1})
        return "能读取群消息"

    plan = [
        ("列我的空间", "drive:drive:readonly", my_space),
        ("列知识库", "wiki:wiki:readonly", wiki),
        ("搜索", "search:docs:read", search),
        ("导出一份文档", "docs:document:export", export),
        ("下载文件 1 字节", "drive:file:download", one_byte),
        ("权限判断", "docs:permission.member:auth", perm),
        ("列群", "im:chat:readonly", list_chats),
        ("读一条群消息", "im:message.group_msg:get_as_user", read_msg),
    ]
    out = []
    for name, scope, fn in plan:
        if out and out[-1].detail == NET_DETAIL:  # 断网时每项都要重试半分钟，后面的直接判同样原因
            out.append(Check(name, False, NET_DETAIL, NET_FIX))
        else:
            out.append(_check(name, scope, fn))
    return out


def check_granted_scopes(tok: dict) -> Optional[Check]:
    """登录换到的 token 里列了实际授予的 scope；比对 SCOPES，缺的列出来（只提醒，不判失败）。"""
    granted = set((tok or {}).get("scope", "").split())
    if not granted:
        return None
    missing = [s for s in auth.SCOPES if s not in granted and s != "offline_access"]
    if not missing:
        return None
    return Check("权限范围", True, "登录时有 %d 项权限没拿到" % len(missing), SCOPE_FIX % "、".join(missing), warn=True)


# ---------------- [5/7] 备份目录 ----------------

def _under(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


def _icloud_check(path: Path, home: Path) -> Optional[Check]:
    cloud = home / "Library" / "Mobile Documents"
    if _under(path, cloud):
        return Check("iCloud 同步", True, "这个目录在 iCloud 云盘里：备份文件会被上传到 iCloud、本地可能被清空占位",
                     "建议换到外接盘或 ~/飞书备份", warn=True)
    for sub in ("Desktop", "Documents"):
        if _under(path, home / sub):
            synced = cloud / "com~apple~CloudDocs" / sub
            try:
                on = synced.exists()
            except OSError:
                on = None
            if on is False:
                return None
            if on:
                return Check("iCloud 同步", True, "「桌面与文稿」开启了 iCloud 同步，这个目录会被上传到 iCloud",
                             "建议换到外接盘或 ~/飞书备份", warn=True)
            return Check("iCloud 同步", True, "判断不了「桌面与文稿」是否开启了 iCloud 同步",
                         "如果开启了，建议换到外接盘或 ~/飞书备份", warn=True)
    return None


def check_root(path: Path, need_bytes: int, home: Optional[Path] = None) -> List[Check]:
    """检查备份目录；绝不创建目录。need_bytes 为首次备份预计体积（未知时传 0）。"""
    home = Path(home) if home is not None else Path.home()
    path = Path(os.path.expanduser(str(path)))
    if not path.exists():
        return [Check("目录存在", False, "目录不存在：%s" % path, "目录不存在：如果要新建，请确认后用 --create")]
    if not path.is_dir():
        return [Check("目录存在", False, "这不是文件夹：%s" % path, "换一个文件夹路径")]
    path = path.resolve()
    checks = [Check("目录存在", True, str(path))]

    probe = path / (".fbk-write-test-%d" % os.getpid())
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        checks.append(Check("可以写入", True, "能在这个目录里写文件"))
    except OSError as e:
        checks.append(Check("可以写入", False, "写不进去：%s" % e.strerror,
                            "换一个有写入权限的目录，或在 Finder「显示简介」里给自己读写权限"))

    free = shutil.disk_usage(str(path)).free
    if need_bytes and need_bytes > 0:
        want = int(need_bytes * SPACE_FACTOR)
        ok = free >= want
        checks.append(Check("剩余空间", ok,
                            "剩余 %s，首次备份预计 %s，建议至少留 %s" % (gb(free), gb(need_bytes), gb(want)),
                            "" if ok else "换一个空间更大的盘（例如外接盘），或先清理出空间"))
    else:
        checks.append(Check("剩余空间", True, "剩余 %s（还没做首次扫描，扫描后会再核对）" % gb(free)))

    ic = _icloud_check(path, home.resolve() if home.exists() else home)
    if ic:
        checks.append(ic)
    return checks


# ---------------- [6/7] 只统计扫描 ----------------

def _export_size(client, token, ftype, sleep, clock, timeout) -> Optional[int]:
    data = client.post("/drive/v1/export_tasks", {"file_extension": EXPORT_EXT[ftype], "token": token, "type": ftype})
    ticket = data["ticket"]
    deadline = clock() + timeout
    while True:
        res = client.get("/drive/v1/export_tasks/%s" % ticket, {"token": token}).get("result") or {}
        st = res.get("job_status")
        if st == 0:
            return int(res.get("file_size") or 0)
        if st not in (1, 2) or clock() >= deadline:
            return None
        sleep(EXPORT_POLL)


def estimate_minutes(export_count: int, upload_bytes: int) -> int:
    """导出数 × 15 秒 ÷ 3 并发 + 上传文件字节 ÷ 5 MB/秒，向上取整到分钟。"""
    sec = export_count * EXPORT_SECONDS_EACH / EXPORT_PARALLEL + (upload_bytes or 0) / DOWNLOAD_BYTES_PER_SEC
    return int(math.ceil(sec / 60.0))


def scan_stats(client, sleep=time.sleep, clock=time.monotonic, rng=None, sample=SAMPLE_PER_TYPE,
               export_timeout=EXPORT_TIMEOUT, workers=4, export_workers=EXPORT_PARALLEL) -> dict:
    """遍历我的空间 + 知识库，只统计不下载。"""
    rng = rng or random.Random()
    errors = []
    items, seen = [], set()
    for gen in (walker.walk_my_space(client, errors), walker.walk_wiki(client, errors)):
        for it in gen:
            if it.token and it.token not in seen:
                seen.add(it.token)
                items.append(it)
    by_type = {}
    for it in items:
        by_type[it.type] = by_type.get(it.type, 0) + 1

    def size_of(tok):
        try:
            return client.head_size("/drive/v1/files/%s/download" % tok)
        except ApiError:
            return None

    uploads = [it.token for it in items if it.type == "file"]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        sizes = list(ex.map(size_of, uploads))
    upload_bytes = sum(s for s in sizes if s)
    unknown = sum(1 for s in sizes if s is None)

    def exported(pair):
        tok, ftype = pair
        try:
            return ftype, _export_size(client, tok, ftype, sleep, clock, export_timeout)
        except (ApiError, KeyError):
            return ftype, None

    jobs = []
    for ftype in sorted(t for t in by_type if t in EXPORT_EXT):
        toks = [it.token for it in items if it.type == ftype]
        picked = toks if len(toks) <= sample else rng.sample(toks, sample)
        jobs.extend((t, ftype) for t in picked)
    with ThreadPoolExecutor(max_workers=max(1, export_workers)) as ex:
        results = list(ex.map(exported, jobs))
    export_by_type, export_est = {}, 0
    for ftype in sorted(t for t in by_type if t in EXPORT_EXT):
        ok = [n for t, n in results if t == ftype and n is not None]
        avg = sum(ok) / len(ok) if ok else 0
        est = int(round(avg * by_type[ftype]))
        export_by_type[ftype] = {"count": by_type[ftype], "sampled": len(ok), "avg_bytes": int(avg), "est_bytes": est}
        export_est += est
    export_count = sum(v for t, v in by_type.items() if t in EXPORT_EXT)
    skipped = {t: n for t, n in by_type.items() if t != "file" and t not in EXPORT_EXT}
    return {
        "items_by_type": by_type,
        "total_items": len(items),
        "upload_count": len(uploads),
        "upload_bytes": upload_bytes,
        "upload_unknown": unknown,
        "export_est_bytes": export_est,
        "export_by_type": export_by_type,
        "skipped_types": skipped,
        "walk_errors": len(errors),
        "est_minutes": estimate_minutes(export_count, upload_bytes),
    }


def _fmt_minutes(m: int) -> str:
    if m < 60:
        return "%d 分钟" % m
    return "%d 小时 %d 分钟" % (m // 60, m % 60)


def format_scan(st: dict) -> str:
    lines = ["只统计，没有下载任何文件。",
             "我的空间 + 知识库共 {:,} 项：".format(st["total_items"]),
             "  上传的文件 {:,} 个，共 {}（精确统计）".format(st["upload_count"], gb(st["upload_bytes"]))]
    if st.get("upload_unknown"):
        lines.append("    其中 %d 个读不到大小，没算进去" % st["upload_unknown"])
    if st["export_by_type"]:
        lines.append("  在线文档导出估算（每类抽 %d 份，抽样少，实际可能偏差几倍）：" % SAMPLE_PER_TYPE)
        for t, v in st["export_by_type"].items():
            extra = "" if v["sampled"] else "（样本全部导出失败，按 0 计）"
            lines.append("    {}（{}）{:,} 份 ≈ {}{}".format(TYPE_LABEL.get(t, t), t, v["count"], gb(v["est_bytes"]), extra))
    if st["skipped_types"]:
        parts = ["%s %d 个" % (TYPE_LABEL.get(t, t), n) for t, n in sorted(st["skipped_types"].items())]
        lines.append("  不支持导出、会跳过：" + "、".join(parts))
    total = st["upload_bytes"] + st["export_est_bytes"]
    lines.append("首次备份合计约 %s，预计耗时约 %s（按导出每份约 %d 秒、%d 个并行、下载约 5 MB/秒估算）。"
                 % (gb(total), _fmt_minutes(st["est_minutes"]), EXPORT_SECONDS_EACH, EXPORT_PARALLEL))
    lines.append("注意：以上不含群聊附件，群聊附件的体积要首次备份时才知道。")
    if st.get("walk_errors"):
        lines.append("另有 %d 处目录没读到，实际数量可能更多（详见日志）。" % st["walk_errors"])
    return "\n".join(lines)


def render_checks(checks: List[Check]) -> str:
    out = []
    for c in checks:
        mark = "⚠" if c.warn else ("✓" if c.ok else "✗")
        out.append("%s %s：%s" % (mark, c.name, c.detail))
        if c.fix and (not c.ok or c.warn):
            out.append("    怎么办：%s" % c.fix)
    return "\n".join(out)
