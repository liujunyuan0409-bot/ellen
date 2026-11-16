"""OAuth 登录、token 续期、租户 token。只用标准库。"""
import http.client
import json
import secrets
import ssl
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

# 登录时申请的权限：只列程序实际调用到的只读接口所需（auth:user.id:read 由飞书隐式授予，不列入）。
# 开发者后台给应用开了更多权限也没关系，程序只申请这些，且 HTTP 客户端只放行读取类请求。
SCOPES = [
    "drive:drive:readonly",  # 列我的空间、根目录
    "drive:drive.metadata:readonly",  # 批量读修改时间（metas/batch_query）
    "drive:file:readonly",  # 读上传的文件
    "drive:file:download",  # 下载上传的文件
    "drive:export:readonly",  # 导出任务
    "docs:document:export",  # 导出在线文档
    "docs:doc:readonly",  # 导出旧版文档
    "docx:document:readonly",  # 导出文档
    "sheets:spreadsheet:readonly",  # 导出电子表格
    "bitable:app:readonly",  # 导出多维表格
    "slides:presentation:read",  # 导出幻灯片
    "wiki:wiki:readonly",  # 知识库
    "wiki:node:read",  # 知识库节点 / get_node
    "wiki:space:read",  # 知识库空间列表
    "space:document:retrieve",  # 云空间文件列表
    "search:docs:read",  # 搜索共享给我的文档
    "docs:permission.member:auth",  # 判断链接能否打开
    "im:chat:readonly",  # 列群
    "im:message:readonly",  # 读群消息
    "im:message.group_msg:get_as_user",  # 以用户身份读群消息
    "im:resource",  # 下载群附件
    "im:chat.members:read",  # 群成员姓名（权限缺口报告的分享人）
    "contact:user.base:readonly",  # 登录后显示用户名
    "offline_access",  # refresh token，自动续期
]


RETRIES = 3

# 飞书 OAuth v2 续期时「续期凭据无效 / 过期 / 被吊销 / 已用过」的错误码：只有这些（或 error=invalid_grant）
# 才需要重新登录；其他错误码当作暂时故障，下次再试。
REFRESH_INVALID_CODES = frozenset({20026, 20037, 20064, 20073, 20074})


class TlsError(NetworkError):
    """连得上飞书，但证书校验失败：多半是这个 Python 没有可用的根证书。不是离线，重试也没用。"""


def is_cert_error(e) -> bool:
    """证书校验失败（直接的，或包在 URLError.reason 里的）。其他 SSL 错误（如连接被断开）不算。"""
    r = e.reason if isinstance(e, urllib.error.URLError) else e
    return isinstance(r, ssl.SSLCertVerificationError) or (
        isinstance(r, ssl.SSLError) and "CERTIFICATE_VERIFY_FAILED" in str(r))


class RefreshFailed(NetworkError):
    """续期被飞书以「非凭据失效」的原因拒绝（服务端繁忙等）：暂时故障，不提示重新登录。"""


def build_authorize_url(app_id, redirect_uri, scopes, state):
    return AUTHORIZE_URL + "?" + urlencode({
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })


def _post_json(transport, path, payload, sleep=time.sleep, attempts=None, timeout=None):
    """POST JSON。attempts：最多试几次（默认 RETRIES+1，之间按 1、2、4… 秒退避）；timeout：给了才传给 transport。"""
    if attempts is None:
        attempts = RETRIES + 1
    body = json.dumps(payload).encode("utf-8")
    extra = {"timeout": timeout} if timeout is not None else {}
    last = None
    for attempt in range(attempts):
        try:
            status, _h, raw = transport("POST", DEFAULT_BASE + path,
                                        {"Content-Type": "application/json; charset=utf-8"}, body, **extra)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            if is_cert_error(e):
                raise TlsError("证书校验失败：%s" % e)
            last = e
        else:
            if status < 500:
                try:
                    return json.loads(raw.decode("utf-8"))
                except ValueError:
                    return {"code": status or -1, "msg": "非 JSON 响应"}
            last = "HTTP %s" % status
        if attempt < attempts - 1:
            sleep(2 ** attempt)
    raise NetworkError("网络异常，稍后重试：%s" % last)


def _exchange(cfg, transport, sleep=time.sleep, attempts=None, timeout=None, **grant):
    payload = {"client_id": cfg.app_id, "client_secret": cfg.app_secret}
    payload.update(grant)
    return _post_json(transport, "/authen/v2/oauth/token", payload, sleep, attempts=attempts, timeout=timeout)


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
            return self.refresh(tok)

    def refresh(self, tok, attempts=None, timeout=None):
        """用 refresh token 续期并保存，返回新的 access token。
        凭据失效（REFRESH_INVALID_CODES / invalid_grant）→ LoginRequired；其他拒绝 → RefreshFailed（暂时故障）；
        网络不通 → NetworkError。attempts / timeout 见 _post_json。"""
        rt = tok.get("refresh_token")
        if not rt:
            raise LoginRequired("没有 refresh token，请重新登录")
        resp = _exchange(self.cfg, self.transport, self._sleep, attempts=attempts, timeout=timeout,
                         grant_type="refresh_token", refresh_token=rt)
        code = resp.get("code", 0)
        if code in REFRESH_INVALID_CODES or resp.get("error") == "invalid_grant":
            raise LoginRequired("续期凭据已失效（code=%s），请重新登录" % code)
        if code != 0 or "access_token" not in resp:
            raise RefreshFailed("续期暂时没成功（code=%s %s），稍后再试"
                                % (code, resp.get("error_description") or resp.get("msg") or resp.get("error") or ""))
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
