import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import chats  # noqa: E402
from fbk.client import ApiError, FeishuClient  # noqa: E402
from fbk.store import Store  # noqa: E402
from fbk.sync import Outcome, SyncContext  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

DOC = "https://example.feishu.cn/docx/AbCdEfGhIjK1"
SHEET = "https://example.feishu.cn/sheets/ShEeTtOkEn12"
WIKI = "https://example.feishu.cn/wiki/WiKiToKeN123"
NOW = 1763020800 + 86400  # 2025-11-14 16:00 +8


def make(t):
    return FeishuClient(lambda: "tok", transport=t, sleep=lambda s: None)


def msg(mid, mtype, content, ts=1763020800, sender="ou_1"):
    return {"message_id": mid, "msg_type": mtype, "create_time": str(ts * 1000),
            "sender": {"id": sender, "id_type": "open_id", "sender_type": "user"},
            "body": {"content": json.dumps(content, ensure_ascii=False)}}


def setup_chat(t, items, chat_name="项目群", members=True):
    t.queue_json("GET", "/im/v1/chats", {"items": [{"chat_id": "oc_1", "name": chat_name}], "has_more": False})
    if members:
        t.queue_json("GET", "/im/v1/chats/oc_1/members", {"items": [{"member_id": "ou_1", "name": "张三"}],
                                                          "has_more": False})
    t.queue_json("GET", "/im/v1/messages", {"items": items, "has_more": False})


class ExtractTest(unittest.TestCase):
    def test_text_post_card(self):
        text = json.dumps({"text": "看这个 " + DOC})
        post = json.dumps({"title": "", "content": [[{"tag": "a", "text": "表", "href": SHEET}]]})
        card = json.dumps({"header": {"title": {"content": "周报"}},
                           "elements": [{"tag": "markdown", "content": "[x](%s)" % WIKI}]})
        self.assertEqual(chats.extract_links(text), [("docx", "AbCdEfGhIjK1")])
        self.assertEqual(chats.extract_links(post), [("sheets", "ShEeTtOkEn12")])
        self.assertEqual(chats.extract_links(card), [("wiki", "WiKiToKeN123")])

    def test_escaped_slashes_and_dedupe(self):
        raw = '{"text":"https:\\/\\/example.feishu.cn\\/docx\\/AbCdEfGhIjK1 %s"}' % DOC
        self.assertEqual(chats.extract_links(raw), [("docx", "AbCdEfGhIjK1")])

    def test_attachment_filename(self):
        self.assertEqual(chats.attachment_filename(1763020800, "报价单.xlsx"), "2025-11-13_报价单.xlsx")

    def test_kind_map(self):
        self.assertEqual(chats.KIND_TO_TYPE["sheets"], "sheet")
        self.assertEqual(chats.KIND_TO_TYPE["base"], "bitable")


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / ".feishu-backup" / "manifest.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_scan_yields_links_and_attachments_not_images(self):
        t = FakeTransport()
        setup_chat(t, [
            msg("m1", "text", {"text": "文档 " + DOC + " 请看"}),
            msg("m2", "image", {"image_key": "img_1"}),
            msg("m3", "file", {"file_key": "fk1", "file_name": "报价单.xlsx"}),
            msg("m4", "audio", {"file_key": "fk2", "duration": 3}),
            msg("m5", "media", {"file_key": "fk3", "image_key": "i", "file_name": "v.mp4", "duration": 9}),
            msg("m6", "interactive", {"header": {"title": {"content": "周报卡片"}},
                                      "elements": [{"tag": "a", "href": SHEET}]}),
        ])
        hits = list(chats.scan(make(t), self.store, 180, NOW))
        links = [h for h in hits if isinstance(h, chats.LinkHit)]
        atts = [h for h in hits if isinstance(h, chats.AttachmentHit)]
        self.assertEqual([(l.kind, l.token) for l in links], [("docx", "AbCdEfGhIjK1"), ("sheets", "ShEeTtOkEn12")])
        self.assertEqual(links[0].sender, "张三")
        self.assertEqual(links[0].chat_name, "项目群")
        self.assertEqual(links[0].sent_at, 1763020800)
        self.assertEqual(links[0].url, DOC)
        self.assertEqual(links[0].hint, "文档  请看")
        self.assertEqual(links[1].hint, "周报卡片")
        self.assertEqual([a.file_key for a in atts], ["fk1", "fk2", "fk3"])
        self.assertEqual(atts[1].file_name, "m4.opus")
        self.assertEqual(atts[0].file_name, "报价单.xlsx")
        self.assertEqual(self.store.get_cursor("oc_1"), 1763020800)

    def test_window_and_cursor(self):
        t = FakeTransport()
        setup_chat(t, [msg("m1", "text", {"text": "hi"})])
        list(chats.scan(make(t), self.store, 180, NOW))
        q = parse_qs([c for c in t.calls if c["path"] == "/im/v1/messages"][0]["query"])
        self.assertEqual(q["start_time"], [str(NOW - 180 * 86400)])
        self.assertEqual(q["sort_type"], ["ByCreateTimeAsc"])
        self.assertEqual(q["container_id"], ["oc_1"])
        # 第二次：游标晚于窗口 → 从游标开始
        t2 = FakeTransport()
        setup_chat(t2, [])
        list(chats.scan(make(t2), self.store, 180, NOW))
        q = parse_qs([c for c in t2.calls if c["path"] == "/im/v1/messages"][0]["query"])
        self.assertEqual(q["start_time"], [str(1763020800 + 1)])
        # 游标早于窗口 → 不早于 now - lookback
        self.store.set_cursor("oc_1", 100)
        t3 = FakeTransport()
        setup_chat(t3, [])
        list(chats.scan(make(t3), self.store, 180, NOW))
        q = parse_qs([c for c in t3.calls if c["path"] == "/im/v1/messages"][0]["query"])
        self.assertEqual(q["start_time"], [str(NOW - 180 * 86400)])

    def test_members_failure_falls_back_to_id(self):
        t = FakeTransport()
        t.queue_json("GET", "/im/v1/chats", {"items": [{"chat_id": "oc_1", "name": "a/b"}], "has_more": False})
        t.queue_json("GET", "/im/v1/chats/oc_1/members", code=99, msg="no")
        t.queue_json("GET", "/im/v1/messages", {"items": [msg("m1", "text", {"text": DOC})], "has_more": False})
        hits = list(chats.scan(make(t), self.store, 180, NOW))
        self.assertEqual(hits[0].sender, "ou_1")
        self.assertEqual(hits[0].chat_name, "a_b")

    def test_failing_chat_skipped_cursor_kept(self):
        t = FakeTransport()
        t.queue_json("GET", "/im/v1/chats", {"items": [{"chat_id": "oc_1", "name": "x"}], "has_more": False})
        t.queue_json("GET", "/im/v1/messages", code=230002, msg="bot not in chat")
        self.assertEqual(list(chats.scan(make(t), self.store, 180, NOW)), [])
        self.assertIsNone(self.store.get_cursor("oc_1"))


class SaveAttachmentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / ".feishu-backup" / "manifest.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ctx(self, t):
        c = SyncContext(client=make(t), store=self.store, root=self.root)
        c.fetch = _Fetch()
        return c

    def hit(self, fk="fk1", name="a.txt", mid="m1"):
        return chats.AttachmentHit("oc_1", "项目群", mid, fk, name, 1763020800)

    def queue_dl(self, t, mid, fk, data):
        t.queue("GET", "/im/v1/messages/%s/resources/%s" % (mid, fk), body=data)

    def test_new_then_unchanged_by_file_key(self):
        t = FakeTransport()
        self.queue_dl(t, "m1", "fk1", b"hello")
        ctx = self.ctx(t)
        self.assertEqual(chats.save_attachment(ctx, self.hit()), Outcome.NEW)
        p = self.root / "群聊附件" / "项目群" / "2025-11-13_a.txt"
        self.assertEqual(p.read_bytes(), b"hello")
        self.assertEqual(self.store.get_attachment("fk1")["status"], "ok")
        n = len(t.calls)
        self.assertEqual(chats.save_attachment(ctx, self.hit()), Outcome.UNCHANGED)
        self.assertEqual(len(t.calls), n)

    def test_content_dedupe(self):
        t = FakeTransport()
        self.queue_dl(t, "m1", "fk1", b"same")
        self.queue_dl(t, "m2", "fk2", b"same")
        ctx = self.ctx(t)
        chats.save_attachment(ctx, self.hit())
        self.assertEqual(chats.save_attachment(ctx, self.hit("fk2", "b.txt", "m2")), Outcome.DUPLICATE)
        files = sorted(p.name for p in (self.root / "群聊附件" / "项目群").iterdir())
        self.assertEqual(files, ["2025-11-13_a.txt"])
        row = self.store.get_attachment("fk2")
        self.assertEqual(row["status"], "duplicate")
        self.assertTrue(row["local_path"].endswith("2025-11-13_a.txt"))

    def test_same_name_never_overwrites(self):
        t = FakeTransport()
        self.queue_dl(t, "m1", "fk1", b"one")
        self.queue_dl(t, "m2", "fk2", b"two")
        ctx = self.ctx(t)
        chats.save_attachment(ctx, self.hit())
        self.assertEqual(chats.save_attachment(ctx, self.hit("fk2", "a.txt", "m2")), Outcome.NEW)
        d = self.root / "群聊附件" / "项目群"
        self.assertEqual((d / "2025-11-13_a.txt").read_bytes(), b"one")
        self.assertEqual((d / "2025-11-13_a-2.txt").read_bytes(), b"two")

    def test_failure_recorded(self):
        t = FakeTransport()
        t.queue_json("GET", "/im/v1/messages/m1/resources/fk1", code=234001, msg="expired", status=400)
        self.assertEqual(chats.save_attachment(self.ctx(t), self.hit()), Outcome.FAILED)
        self.assertEqual(self.store.get_attachment("fk1")["status"], "failed")
        self.assertEqual(list((self.root / "群聊附件" / "项目群").glob("*")), [])


class _Fetch:
    """真实 fetcher，但磁盘余量检查不依赖本机。"""
    def __getattr__(self, n):
        from fbk import fetcher
        return getattr(fetcher, n)

    def ensure_space(self, root, need):
        return None


class SearchSharedTest(unittest.TestCase):
    def test_skip_known_paginate_and_mtime(self):
        t = FakeTransport()
        t.queue_json("POST", "/suite/docs-api/search/object", {
            "docs_entities": [{"docs_token": "K1", "docs_type": "docx", "title": "已有"},
                              {"docs_token": "N1", "docs_type": "sheet", "title": "新表"}],
            "has_more": True, "total": 3})
        t.queue_json("POST", "/suite/docs-api/search/object", {
            "docs_entities": [{"docs_token": "N2", "docs_type": "file", "title": "x.pdf"},
                              {"docs_token": "N3", "docs_type": "mindnote", "title": "脑图"}],
            "has_more": False, "total": 3})
        t.queue_json("POST", "/drive/v1/metas/batch_query", {"metas": [
            {"doc_token": "N1", "latest_modify_time": "1700000001"},
            {"doc_token": "N2", "latest_modify_time": "1700000002"}]})
        items = list(chats.search_shared(make(t), {"K1"}))
        self.assertEqual([i.token for i in items], ["N1", "N2"])
        self.assertEqual(items[0].type, "sheet")
        self.assertEqual(items[0].mtime, 1700000001)
        self.assertEqual(items[0].source, "shared")
        self.assertEqual(items[0].rel_dir, ("共享给我", "_搜索发现"))
        offs = [json.loads(c["body"])["offset"] for c in t.calls if c["path"] == "/suite/docs-api/search/object"]
        self.assertEqual(offs, [0, 50])


if __name__ == "__main__":
    unittest.main()
