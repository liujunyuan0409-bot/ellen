import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import chats, perms  # noqa: E402
from fbk.client import ApiError, FeishuClient  # noqa: E402
from fbk.store import Store  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402


def make(t):
    return FeishuClient(lambda: "tok", transport=t, sleep=lambda s: None)


def auth(t, token, **kw):
    t.queue_json("GET", "/drive/v1/permissions/%s/members/auth" % token, **kw)


class ClassifyTest(unittest.TestCase):
    def test_ok_denied_gone(self):
        t = FakeTransport()
        auth(t, "TokOk00000001", data={"auth_result": True})
        self.assertEqual(perms.classify(make(t), "docx", "TokOk00000001"), ("ok", "docx"))
        q = t.calls[0]["query"]
        self.assertIn("type=docx", q)
        self.assertIn("action=view", q)
        t = FakeTransport()
        auth(t, "TokNo00000001", data={"auth_result": False})
        self.assertEqual(perms.classify(make(t), "sheets", "TokNo00000001"), ("denied", "sheet"))
        for code in (1063005, 1063001):
            t = FakeTransport()
            auth(t, "TokGone0000001", code=code, msg="x")
            self.assertEqual(perms.classify(make(t), "docx", "TokGone0000001"), ("gone", "docx"))

    def test_other_api_error_reraises(self):
        t = FakeTransport()
        auth(t, "TokErr0000001", code=99999, msg="boom")
        with self.assertRaises(ApiError):
            perms.classify(make(t), "docx", "TokErr0000001")

    def test_wiki_uses_get_node(self):
        t = FakeTransport()
        t.queue_json("GET", "/wiki/v2/spaces/get_node", {"node": {"obj_token": "RealTok00001", "obj_type": "docx",
                                                                  "title": "T"}})
        auth(t, "RealTok00001", data={"auth_result": True})
        self.assertEqual(perms.classify(make(t), "wiki", "WikiTok00001"), ("ok", "docx"))
        self.assertIn("token=WikiTok00001", t.calls[0]["query"])
        self.assertIn("RealTok00001", t.calls[1]["path"])
        self.assertEqual(perms.resolve(make(t), "wiki", "WikiTok00001")[2], "RealTok00001")

    def test_wiki_errors(self):
        t = FakeTransport()
        t.queue_json("GET", "/wiki/v2/spaces/get_node", code=131006, msg="permission denied")
        self.assertEqual(perms.classify(make(t), "wiki", "WikiTok00001"), ("denied", None))
        t = FakeTransport()
        t.queue_json("GET", "/wiki/v2/spaces/get_node", code=131005, msg="not found")
        self.assertEqual(perms.classify(make(t), "wiki", "WikiTok00001"), ("gone", None))


class ReportTest(unittest.TestCase):
    def test_csv(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            s = Store(root / ".feishu-backup" / "m.sqlite")
            s.upsert_link("A", kind="docx", status="denied", hint="预算", sender="张三", chat_name="项目群",
                          sent_at=1763020800, first_seen=1763020800, url="https://example.feishu.cn/docx/A")
            s.upsert_link("B", kind="sheets", status="gone", hint="", sender="李四", chat_name="群2",
                          sent_at=1763020800, first_seen=1763107200, url="https://example.feishu.cn/sheets/B")
            s.upsert_link("C", kind="docx", status="ok", hint="", sender="", chat_name="群2",
                          sent_at=1763020800, first_seen=1763020800, url="u")
            out = root / "_报告" / "perm.csv"
            res = perms.write_report(s, out)
            self.assertEqual(res, {"total": 3, "denied": 1, "gone": 1})
            raw = out.read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
            lines = raw.decode("utf-8-sig").splitlines()
            self.assertEqual(lines[0], "# 共发现 3 个链接，无权限 1 个，已删除或无效 1 个")
            self.assertTrue(lines[1].startswith("# 局限："))
            self.assertEqual(lines[2], "状态,类型,链接,文件名线索,分享人,群名,分享时间,首次发现日期")
            rows = list(csv.reader(lines[3:]))
            self.assertEqual(len(rows), 3)
            byurl = {r[2]: r for r in rows}
            r = byurl["https://example.feishu.cn/docx/A"]
            self.assertEqual(r[:2], ["无权限", "docx"])
            self.assertEqual(r[6], "2025-11-13 16:00")
            self.assertEqual(r[7], "2025-11-13")
            self.assertEqual(byurl["https://example.feishu.cn/sheets/B"][0], "已删除或无效")
            self.assertEqual(byurl["https://example.feishu.cn/sheets/B"][1], "sheet")
            self.assertEqual(byurl["u"][0], "能打开")
            s.close()


class ProcessLinkTest(unittest.TestCase):
    def test_openable_unknown_link_becomes_shared_item(self):
        with tempfile.TemporaryDirectory() as d:
            s = Store(Path(d) / ".feishu-backup" / "m.sqlite")
            t = FakeTransport()
            auth(t, "AbCdEfGhIjK1", data={"auth_result": True})
            t.queue_json("POST", "/drive/v1/metas/batch_query", {"metas": [
                {"doc_token": "AbCdEfGhIjK1", "latest_modify_time": "1700000009", "title": "2026预算"}]})
            hit = chats.LinkHit("docx", "AbCdEfGhIjK1", "项目群", "张三", 1763020800, "预算", "https://x/docx/AbCdEfGhIjK1")
            item = perms.process_link(make(t), s, hit, known=set(), now=1763100000)
            self.assertEqual(item.rel_dir, ("共享给我", "项目群"))
            self.assertEqual(item.name, "2026预算")
            self.assertEqual((item.source, item.type, item.token, item.mtime), ("shared", "docx", "AbCdEfGhIjK1", 1700000009))
            row = s.links()[0]
            self.assertEqual((row["status"], row["first_seen"], row["url"]), ("ok", 1763100000, "https://x/docx/AbCdEfGhIjK1"))
            # 已在清单里 → 不再产出
            t2 = FakeTransport()
            auth(t2, "AbCdEfGhIjK1", data={"auth_result": True})
            self.assertIsNone(perms.process_link(make(t2), s, hit, known={"AbCdEfGhIjK1"}, now=1763200000))
            self.assertEqual(s.links()[0]["first_seen"], 1763100000)
            # 无权限 → 不产出
            t3 = FakeTransport()
            auth(t3, "Zzzzzzzzzz11", data={"auth_result": False})
            h2 = chats.LinkHit("docx", "Zzzzzzzzzz11", "群", "a", 1, "", "u")
            self.assertIsNone(perms.process_link(make(t3), s, h2, known=set(), now=5))
            s.close()


if __name__ == "__main__":
    unittest.main()
