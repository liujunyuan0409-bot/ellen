import http.client
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk.client import ApiError, FeishuClient, ReadOnlyViolation  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402


def make(t, **kw):
    sleeps = []
    c = FeishuClient(lambda: "tok", transport=t, sleep=sleeps.append, **kw)
    return c, sleeps


class ClientTest(unittest.TestCase):
    def test_post_outside_allowlist_raises_without_sending(self):
        t = FakeTransport()
        c, _ = make(t)
        with self.assertRaises(ReadOnlyViolation):
            c.post("/drive/v1/files/abc/delete", {})
        self.assertEqual(t.calls, [])

    def test_post_on_allowlist_is_sent(self):
        t = FakeTransport().queue_json("POST", "/drive/v1/metas/batch_query", {"metas": []})
        c, _ = make(t)
        self.assertEqual(c.post("/drive/v1/metas/batch_query", {"x": 1}), {"metas": []})
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Bearer tok")

    def test_non_get_methods_blocked(self):
        t = FakeTransport()
        c, _ = make(t)
        for m in ("PUT", "PATCH", "DELETE"):
            with self.assertRaises(ReadOnlyViolation):
                c.request(m, "/drive/v1/files/abc")
        self.assertEqual(t.calls, [])

    def test_retries_429_then_succeeds(self):
        t = (FakeTransport().queue("GET", "/x", status=429)
             .queue("GET", "/x", status=429).queue_json("GET", "/x", {"a": 1}))
        c, sleeps = make(t)
        self.assertEqual(c.get("/x"), {"a": 1})
        self.assertEqual(len(t.calls), 3)
        self.assertIn(1, sleeps)
        self.assertIn(2, sleeps)

    def test_gives_up_after_five_retries(self):
        t = FakeTransport().queue("GET", "/x", status=503)
        c, _ = make(t)
        with self.assertRaises(ApiError):
            c.get("/x")
        self.assertEqual(len(t.calls), 6)

    def test_retries_network_error(self):
        calls = []

        def tr(m, u, h, b):
            calls.append(1)
            if len(calls) < 2:
                raise OSError("boom")
            return 200, {}, b'{"code":0,"data":{"ok":true}}'
        c = FeishuClient(lambda: "t", transport=tr, sleep=lambda s: None)
        self.assertEqual(c.get("/x"), {"ok": True})

    def test_api_error_on_nonzero_code(self):
        t = FakeTransport().queue_json("GET", "/x", code=99991663, msg="bad token")
        c, _ = make(t)
        with self.assertRaises(ApiError) as cm:
            c.get("/x")
        self.assertEqual(cm.exception.code, 99991663)

    def test_paginate_follows_tokens(self):
        t = (FakeTransport()
             .queue_json("GET", "/p", {"items": [{"i": 1}], "has_more": True, "page_token": "n1"})
             .queue_json("GET", "/p", {"items": [{"i": 2}], "has_more": False}))
        c, _ = make(t)
        got = list(c.paginate("/p", {"page_size": 50}, "items"))
        self.assertEqual(got, [{"i": 1}, {"i": 2}])
        self.assertIn("page_token=n1", t.calls[1]["query"])

    def test_head_size_parses_content_range(self):
        t = FakeTransport().queue("GET", "/d", status=206,
                                  headers={"content-range": "bytes 0-0/7704352"}, body=b"x")
        c, _ = make(t)
        self.assertEqual(c.head_size("/d"), 7704352)
        self.assertEqual(t.calls[0]["headers"]["Range"], "bytes=0-0")

    def test_download_writes_bytes(self):
        t = FakeTransport().queue("GET", "/d", status=200, body=b"hello")
        c, _ = make(t)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "sub" / "f.bin"
            self.assertEqual(c.download("/d", dest), 5)
            self.assertEqual(dest.read_bytes(), b"hello")

    def test_download_error_raises(self):
        t = FakeTransport().queue_json("GET", "/d", code=1061004, msg="forbidden", status=403)
        c, _ = make(t)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ApiError):
                c.download("/d", Path(d) / "f")
            self.assertFalse((Path(d) / "f").exists())

    def test_download_streams_to_dest(self):
        t = FakeTransport().queue("GET", "/d", status=200, body=b"hello")
        c, _ = make(t)
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "f"
            self.assertEqual(c.download("/d", dest), 5)
            self.assertEqual(dest.read_bytes(), b"hello")

    def test_incomplete_read_is_retried(self):
        t = (FakeTransport().queue("GET", "/d", body=http.client.IncompleteRead(b"he"))
             .queue("GET", "/d", status=200, body=b"hello"))
        c, sleeps = make(t)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(c.download("/d", Path(d) / "f"), 5)
        self.assertEqual(len(t.calls), 2)

    def test_head_size_200_uses_content_length(self):
        t = FakeTransport().queue("GET", "/d", status=200, headers={"Content-Length": "123"}, body=b"x" * 123)
        c, _ = make(t)
        self.assertEqual(c.head_size("/d"), 123)

    def test_head_size_none_without_headers(self):
        t = FakeTransport().queue("GET", "/d", status=200, body=b"x")
        c, _ = make(t)
        self.assertIsNone(c.head_size("/d"))

    def test_head_size_403_raises(self):
        t = FakeTransport().queue_json("GET", "/d", code=1061004, status=403)
        c, _ = make(t)
        with self.assertRaises(ApiError):
            c.head_size("/d")

    def test_rate_limit_spaces_requests(self):
        t = FakeTransport().queue_json("GET", "/x", {})
        now = [0.0]
        sleeps = []
        c = FeishuClient(lambda: "t", transport=t, rps=5.0, sleep=sleeps.append, clock=lambda: now[0])
        c.get("/x")
        c.get("/x")
        self.assertTrue(any(abs(s - 0.2) < 1e-9 for s in sleeps))


if __name__ == "__main__":
    unittest.main()
