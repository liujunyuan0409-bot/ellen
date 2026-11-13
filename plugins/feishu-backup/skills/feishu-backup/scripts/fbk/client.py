"""只读 HTTP 客户端：令牌桶限流、重试、POST 白名单、分页。只用标准库。"""
import http.client
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple
from urllib.parse import urlencode

DEFAULT_BASE = "https://open.feishu.cn/open-apis"
MAX_RETRIES = 5

# transport(method, url, headers, body[, stream_to=Path][, read_body=False]) -> (status, headers, body)
# stream_to：200/206 时把响应体按 1 MiB 分块写入该文件并返回 b""；
# read_body=False：不读取响应体（返回 b""）。两个关键字仅在需要时才传入。
Transport = Callable[..., Tuple[int, dict, bytes]]
CHUNK = 1024 * 1024

ALLOWED_POST = {
    "/drive/v1/export_tasks",
    "/suite/docs-api/search/object",
    "/drive/v1/metas/batch_query",
    "/auth/v3/tenant_access_token/internal",
    "/authen/v2/oauth/token",
}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__("飞书接口错误 code=%s msg=%s" % (code, msg))
        self.code = code
        self.msg = msg


class ReadOnlyViolation(Exception):
    pass


def default_transport(method, url, headers, body, stream_to=None, read_body=True):
    req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            rh = dict(resp.headers.items())
            if stream_to is not None and resp.status in (200, 206):
                with open(str(stream_to), "wb") as f:
                    while True:
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        f.write(chunk)
                return resp.status, rh, b""
            if not read_body:
                return resp.status, rh, b""
            return resp.status, rh, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()) if e.headers else {}, e.read()


def _header(headers, name):
    lname = name.lower()
    for k, v in (headers or {}).items():
        if k.lower() == lname:
            return v
    return None


class FeishuClient:
    def __init__(self, token_provider, transport=default_transport, rps=5.0,
                 base=DEFAULT_BASE, sleep=time.sleep, clock=time.monotonic):
        self.token_provider = token_provider
        self.transport = transport
        self.rps = rps
        self.base = base.rstrip("/")
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._next_slot = 0.0

    # --- 限流：简单令牌桶（等间隔放行），线程安全 ---
    def _throttle(self):
        if not self.rps or self.rps <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = max(now, self._next_slot)
            self._next_slot = slot + 1.0 / self.rps
            wait = slot - now
        if wait > 0:
            self._sleep(wait)

    def _send(self, method, path, params=None, body=None, headers=None, stream_to=None, read_body=True):
        """发请求（含限流与重试），返回 (status, headers, raw)。method 必须已通过白名单。"""
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        h = {"Authorization": "Bearer " + self.token_provider()}
        if body is not None:
            h["Content-Type"] = "application/json; charset=utf-8"
        h.update(headers or {})
        data = json.dumps(body).encode("utf-8") if body is not None else None
        kw = {}
        if stream_to is not None:
            kw["stream_to"] = stream_to
        if not read_body:
            kw["read_body"] = False
        last = None
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            try:
                status, rh, raw = self.transport(method, url, h, data, **kw)
            except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
                last = e
            else:
                if status != 429 and status < 500:
                    return status, rh, raw
                last = ApiError(status, "HTTP %s" % status)
            if attempt < MAX_RETRIES:
                self._sleep(2 ** attempt)
        if isinstance(last, ApiError):
            raise last
        raise ApiError(-1, "网络异常：%s" % last)

    @staticmethod
    def _parse(status, raw):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(status, "非 JSON 响应")
        if not isinstance(obj, dict):
            raise ApiError(status, "响应格式异常")
        if obj.get("code", 0) != 0:
            raise ApiError(obj.get("code"), obj.get("msg", ""))
        if status >= 400:
            raise ApiError(status, obj.get("msg", "HTTP %s" % status))
        return obj.get("data", obj) if "data" in obj else {}

    def request(self, method, path, params=None, body=None):
        method = method.upper()
        if method == "GET":
            pass
        elif method == "POST":
            if path not in ALLOWED_POST:
                raise ReadOnlyViolation("POST 不在只读白名单内：%s" % path)
        else:
            raise ReadOnlyViolation("禁止的 HTTP 方法：%s" % method)
        status, _h, raw = self._send(method, path, params, body)
        return self._parse(status, raw)

    def get(self, path, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, body):
        return self.request("POST", path, body=body)

    def download(self, path, dest, params=None, headers=None):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        status, _h, raw = self._send("GET", path, params, None, headers, stream_to=dest)
        if status not in (200, 206):
            self._parse(status, raw)  # 通常抛 ApiError
            raise ApiError(status, "下载失败")
        return dest.stat().st_size

    def head_size(self, path):
        status, rh, _raw = self._send("GET", path, None, None, {"Range": "bytes=0-0"}, read_body=False)
        if status >= 400:
            raise ApiError(status, "HTTP %s" % status)
        cr = _header(rh, "Content-Range")
        if cr and "/" in cr:
            tail = cr.rsplit("/", 1)[1].strip()
            if tail.isdigit():
                return int(tail)
        if status == 200:
            cl = _header(rh, "Content-Length")
            if cl and cl.strip().isdigit():
                return int(cl.strip())
        return None

    def paginate(self, path, params, items_key, token_param="page_token",
                 token_field="page_token") -> Iterator[dict]:
        params = dict(params or {})
        while True:
            data = self.get(path, params)
            for item in data.get(items_key) or []:
                yield item
            nxt = data.get(token_field)
            if not data.get("has_more") or not nxt:
                return
            params[token_param] = nxt
