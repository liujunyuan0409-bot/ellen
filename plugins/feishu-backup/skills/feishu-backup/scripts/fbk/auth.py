"""OAuth 登录、token 续期、租户 token。只用标准库。"""
import http.client
import json
import secrets
import threading
import time
import urllib.error
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from . import config
from .client import DEFAULT_BASE, LoginRequired, NetworkError, default_transport  # noqa: F401 —— 对外仍以 auth.* 使用

REDIRECT_URI = "http://127.0.0.1:8765/callback"
AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
REFRESH_MARGIN = 300  # 离过期不到这么多秒就续期

# 设计文档 §3 的授权范围（auth:user.id:read 由飞书隐式授予，不列入）
SCOPES = [
    "base:app:copy",
    "base:app:create",
    "base:app:read",
    "base:app:update",
    "base:appmode_block:create",
    "base:appmode_block:delete",
    "base:appmode_block:read",
    "base:appmode_block:update",
    "base:appmode_page:create",
    "base:appmode_page:delete",
    "base:appmode_page:read",
    "base:dashboard:copy",
    "base:dashboard:create",
    "base:dashboard:delete",
    "base:dashboard:read",
    "base:dashboard:update",
    "base:workspace:create",
    "base:workspace:list",
    "base:workspace:read",
    "base:workspace:update",
    "bitable:app",
    "bitable:app:readonly",
    "board:whiteboard:node:create",
    "board:whiteboard:node:delete",
    "board:whiteboard:node:read",
    "board:whiteboard:node:update",
    "docs:doc",
    "docs:doc:readonly",
    "docs:document.comment:create",
    "docs:document.comment:delete",
    "docs:document.comment:read",
    "docs:document.comment:update",
    "docs:document.comment:write_only",
    "docs:document:copy",
    "docs:document:export",
    "docs:document:import",
    "docs:document.media:download",
    "docs:document.media:upload",
    "docs:permission.member",
    "docs:permission.member:auth",
    "docs:permission.member:create",
    "docs:permission.member:delete",
    "docs:permission.member:readonly",
    "docs:permission.member:retrieve",
    "docs:permission.member:transfer",
    "docs:permission.member:update",
    "docx:document",
    "docx:document.block:convert",
    "docx:document:create",
    "docx:document:readonly",
    "docx:document:write_only",
    "drive:document.content:read",
    "drive:document.mention:read",
    "drive:drive",
    "drive:drive.metadata:readonly",
    "drive:drive.search:readonly",
    "drive:drive:readonly",
    "drive:drive:version",
    "drive:drive:version:readonly",
    "drive:export:readonly",
    "drive:file",
    "drive:file.like:readonly",
    "drive:file:download",
    "drive:file:readonly",
    "drive:file:upload",
    "drive:file:view_record:readonly",
    "im:chat",
    "im:chat:read",
    "im:chat:readonly",
    "im:chat:update",
    "im:message",
    "im:message:readonly",
    "im:resource",
    "mindnote:node:create",
    "mindnote:node:read",
    "minutes:minutes",
    "minutes:minutes.artifacts:read",
    "minutes:minutes.basic:read",
    "minutes:minutes.media:export",
    "minutes:minutes.search:read",
    "minutes:minutes:readonly",
    "search:docs:read",
    "sheets:spreadsheet",
    "sheets:spreadsheet.meta:read",
    "sheets:spreadsheet.meta:write_only",
    "sheets:spreadsheet:create",
    "sheets:spreadsheet:read",
    "sheets:spreadsheet:readonly",
    "sheets:spreadsheet:write_only",
    "slides:presentation:create",
    "slides:presentation:read",
    "slides:presentation:update",
    "slides:presentation:write_only",
    "space:document.event:read",
    "space:document:delete",
    "space:document:move",
    "space:document:retrieve",
    "space:document:shortcut",
    "space:folder:create",
    "wiki:member:create",
    "wiki:member:retrieve",
    "wiki:member:update",
    "wiki:node:copy",
    "wiki:node:create",
    "wiki:node:move",
    "wiki:node:read",
    "wiki:node:retrieve",
    "wiki:node:update",
    "wiki:setting:read",
    "wiki:space:read",
    "wiki:space:retrieve",
    "wiki:space:write_only",
    "wiki:wiki",
    "wiki:wiki:readonly",
    "contact:user.base:readonly",
    "offline_access",
    "im:message.group_msg:get_as_user",
    "im:message.p2p_msg:get_as_user",
    "im:chat.members:read",
]


RETRIES = 3


def build_authorize_url(app_id, redirect_uri, scopes, state):
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })


def _post_json(transport, path, payload, sleep=time.sleep):
    body = json.dumps(payload).encode("utf-8")
    last = None
    for attempt in range(RETRIES + 1):
        try:
            status, _h, raw = transport("POST", DEFAULT_BASE + path,
                                        {"Content-Type": "application/json; charset=utf-8"}, body)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            last = e
        else:
            if status < 500:
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return {"code": status or -1, "msg": "非 JSON 响应"}
            last = "HTTP %s" % status
        if attempt < RETRIES:
            sleep(2 ** attempt)
    raise NetworkError("网络异常，稍后重试：%s" % last)


def _exchange(cfg, transport, sleep=time.sleep, **grant):
    payload = {"client_id": cfg.app_id, "client_secret": cfg.app_secret}
    payload.update(grant)
    return _post_json(transport, "/authen/v2/oauth/token", payload, sleep)


def _stamp(resp):
    tok = dict(resp)
    tok["obtained_at"] = int(time.time())
    return tok


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        q = parse_qs(parts.query)
        self.server.result = {k: v[0] for k, v in q.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<html><body><h3>授权完成，可以关闭此页面。</h3></body></html>".encode("utf-8"))

    def log_message(self, *args):
        pass


def run_login(cfg, open_browser=True, timeout=600, transport=None, port=8765):
    """起本地回调服务 -> 浏览器授权 -> 换 token -> 保存。返回保存的 token dict。"""
    transport = transport or default_transport
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(cfg.app_id, REDIRECT_URI, SCOPES, state)
    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.result = None
    server.timeout = 1
    print("请在浏览器完成授权：\n%s" % url)
    if open_browser:
        threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    deadline = time.time() + timeout
    try:
        while server.result is None and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    res = server.result
    if res is None:
        raise LoginRequired("等待授权超时")
    if res.get("state") != state:
        raise LoginRequired("授权回调 state 不一致")
    if "code" not in res:
        raise LoginRequired("授权被拒绝：%s" % res.get("error", "未知原因"))
    resp = _exchange(cfg, transport, grant_type="authorization_code",
                     code=res["code"], redirect_uri=REDIRECT_URI)
    if resp.get("code", 0) != 0 or "access_token" not in resp:
        raise LoginRequired("换取 token 失败：%s" % (resp.get("error_description") or resp.get("msg") or resp))
    tok = _stamp(resp)
    config.save_token(tok)
    return tok


class TokenManager:
    def __init__(self, cfg, transport=None, now=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.transport = transport or default_transport
        self.now = now
        self._sleep = sleep
        self._lock = threading.Lock()

    def access_token(self):
        with self._lock:
            try:
                tok = config.load_token()
            except (OSError, ValueError) as e:
                raise LoginRequired("登录信息文件损坏或读不了（%s），请重新登录" % e)
            if tok is not None and not isinstance(tok, dict):
                raise LoginRequired("登录信息文件格式不对，请重新登录")
            if not tok:
                raise LoginRequired("尚未登录，请先运行登录")
            expires_at = tok.get("obtained_at", 0) + tok.get("expires_in", 0)
            if expires_at - self.now() >= REFRESH_MARGIN:
                return tok["access_token"]
            return self._refresh(tok)

    def _refresh(self, tok):
        rt = tok.get("refresh_token")
        if not rt:
            raise LoginRequired("没有 refresh token，请重新登录")
        resp = _exchange(self.cfg, self.transport, self._sleep, grant_type="refresh_token", refresh_token=rt)
        if resp.get("code", 0) != 0 or "access_token" not in resp:
            raise LoginRequired("续期失败（code=%s），请重新登录" % resp.get("code"))
        new = dict(tok)
        new.update(resp)
        new["obtained_at"] = int(self.now())
        config.save_token(new)
        return new["access_token"]


def tenant_token(cfg, transport=None):
    resp = _post_json(transport or default_transport, "/auth/v3/tenant_access_token/internal",
                      {"app_id": cfg.app_id, "app_secret": cfg.app_secret})
    if resp.get("code") != 0 or "tenant_access_token" not in resp:
        raise LoginRequired("获取 tenant token 失败：%s" % resp.get("msg"))
    return resp["tenant_access_token"]
