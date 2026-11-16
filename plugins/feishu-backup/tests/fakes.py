"""可复用的假传输层：按 (method, path) 返回预置响应，并记录调用。"""
import json
import os
from contextlib import ExitStack
from unittest import mock
from typing import Dict, List, Tuple
from urllib.parse import urlsplit

API_PREFIX = "/open-apis"


class FakeTransport:
    """transport(method, url, headers, body) -> (status, headers, body)。

    queue(method, path, ...) 为 (method, path) 追加一条响应；同一键的响应按顺序消费，
    最后一条用完后重复使用最后一条。path 不含 query，也不含 /open-apis 前缀。
    calls 为 dict 列表：method / path / query / headers / body。
    """

    def __init__(self):
        self.calls = []  # type: List[dict]
        self._q = {}  # type: Dict[Tuple[str, str], list]

    def queue(self, method, path, status=200, headers=None, body=b""):
        if not isinstance(body, (bytes, bytearray, Exception)):
            body = json.dumps(body).encode("utf-8")
        self._q.setdefault((method.upper(), path), []).append((status, dict(headers or {}), body if isinstance(body, Exception) else bytes(body)))
        return self

    def queue_json(self, method, path, data=None, code=0, msg="ok", status=200, headers=None):
        return self.queue(method, path, status=status, headers=headers,
                          body={"code": code, "msg": msg, "data": data if data is not None else {}})

    def __call__(self, method, url, headers, body, stream_to=None, read_body=True, timeout=None):
        parts = urlsplit(url)
        path = parts.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX):]
        self.calls.append({"method": method, "path": path, "query": parts.query,
                           "headers": dict(headers or {}), "body": body, "timeout": timeout})
        key = (method.upper(), path)
        items = self._q.get(key)
        if not items:
            return 404, {}, json.dumps({"code": 404, "msg": "no fake response for %s %s" % key}).encode()
        resp = items.pop(0) if len(items) > 1 else items[0]
        status, rh, rb = resp
        if isinstance(rb, Exception):
            raise rb
        if stream_to is not None and status in (200, 206):
            with open(str(stream_to), "wb") as f:
                f.write(rb)
            return status, rh, b""
        if not read_body:
            return status, rh, b""
        return status, rh, rb


class LpStr(str):
    """lp() 的返回值打上标记，用来检查文件操作是否都经过了 lp()。"""


class LpSpy:
    def __init__(self):
        self.bad = []
        self.ops = {}
        self._depth = 0

    def lp(self, p, is_windows=None):
        return LpStr(str(p))

    def _wrap(self, name, fn, nargs):
        def w(*a, **k):
            if self._depth:  # os.makedirs 递归建上级目录时的内部调用，不是程序的调用
                return fn(*a, **k)
            self.ops[name] = self.ops.get(name, 0) + 1
            for x in a[:nargs]:
                if not isinstance(x, LpStr):
                    self.bad.append((name, x))
            self._depth += 1
            try:
                return fn(*a, **k)
            finally:
                self._depth -= 1
        return w

    def patch(self, *modules):
        st = ExitStack()
        for m in modules:
            st.enter_context(mock.patch.object(m, "lp", self.lp))
        for name, nargs in (("replace", 2), ("rename", 2), ("unlink", 1), ("remove", 1), ("makedirs", 1)):
            st.enter_context(mock.patch("os." + name, self._wrap(name, getattr(os, name), nargs)))
        return st

