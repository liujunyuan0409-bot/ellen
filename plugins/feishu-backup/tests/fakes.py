"""可复用的假传输层：按 (method, path) 返回预置响应，并记录调用。"""
import json
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
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode("utf-8")
        self._q.setdefault((method.upper(), path), []).append((status, dict(headers or {}), bytes(body)))
        return self

    def queue_json(self, method, path, data=None, code=0, msg="ok", status=200, headers=None):
        return self.queue(method, path, status=status, headers=headers,
                          body={"code": code, "msg": msg, "data": data if data is not None else {}})

    def __call__(self, method, url, headers, body):
        parts = urlsplit(url)
        path = parts.path
        if path.startswith(API_PREFIX):
            path = path[len(API_PREFIX):]
        self.calls.append({"method": method, "path": path, "query": parts.query,
                           "headers": dict(headers or {}), "body": body})
        key = (method.upper(), path)
        items = self._q.get(key)
        if not items:
            return 404, {}, json.dumps({"code": 404, "msg": "no fake response for %s %s" % key}).encode()
        return items.pop(0) if len(items) > 1 else items[0]
