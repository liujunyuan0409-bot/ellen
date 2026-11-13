import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk.client import FeishuClient  # noqa: E402
from fbk.walker import RemoteItem, walk_my_space, walk_wiki  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402


def make(t):
    return FeishuClient(lambda: "tok", transport=t, sleep=lambda s: None)


def files_resp(t, folder, files, nxt=None):
    t.queue_json("GET", "/drive/v1/files", {"files": files, "has_more": bool(nxt),
                                             "next_page_token": nxt or ""})


def f(token, name, type_, mt="1700000000", **kw):
    d = {"token": token, "name": name, "type": type_, "modified_time": mt}
    d.update(kw)
    return d


class MySpaceTest(unittest.TestCase):
    def root(self, t):
        t.queue_json("GET", "/drive/explorer/v2/root_folder/meta", {"token": "ROOT", "id": "1", "user_id": "u"})

    def test_my_space_nested_paths(self):
        t = FakeTransport()
        self.root(t)
        # 根目录 -> 市场部 -> 品牌手册
        files_resp(t, "ROOT", [f("FLD1", "市场部", "folder")])
        files_resp(t, "FLD1", [f("DOC1", "品牌手册", "docx", "1700000123")])
        items = list(walk_my_space(make(t)))
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it.rel_dir, ("我的空间", "市场部"))
        self.assertEqual((it.token, it.type, it.name, it.source, it.mtime),
                         ("DOC1", "docx", "品牌手册", "my", 1700000123))
        q = [c["query"] for c in t.calls if c["path"] == "/drive/v1/files"]
        self.assertIn("folder_token=ROOT", q[0])
        self.assertIn("page_size=200", q[0])

    def test_pagination_uses_next_page_token(self):
        t = FakeTransport()
        self.root(t)
        files_resp(t, "ROOT", [f("A", "a", "docx")], nxt="P2")
        files_resp(t, "ROOT", [f("B", "b", "docx")])
        items = list(walk_my_space(make(t)))
        self.assertEqual([i.token for i in items], ["A", "B"])
        self.assertIn("page_token=P2", [c["query"] for c in t.calls if c["path"] == "/drive/v1/files"][1])

    def test_shortcut_resolves_to_target(self):
        t = FakeTransport()
        self.root(t)
        files_resp(t, "ROOT", [f("SC1", "我的快捷", "shortcut", "0",
                                 shortcut_info={"target_type": "sheet", "target_token": "TGT1"})])
        t.queue_json("POST", "/drive/v1/metas/batch_query", {
            "metas": [{"doc_token": "TGT1", "doc_type": "sheet", "title": "原表",
                       "latest_modify_time": "1700000999"}], "failed_list": []})
        it = list(walk_my_space(make(t)))[0]
        self.assertEqual((it.token, it.type), ("TGT1", "sheet"))
        self.assertEqual(it.name, "我的快捷")
        self.assertEqual(it.mtime, 1700000999)
        self.assertEqual(it.rel_dir, ("我的空间",))

    def test_shortcut_target_missing_skipped_with_warning(self):
        t = FakeTransport()
        self.root(t)
        files_resp(t, "ROOT", [f("SC1", "坏快捷", "shortcut", "0",
                                 shortcut_info={"target_type": "docx", "target_token": "GONE"}),
                               f("D", "ok", "docx")])
        t.queue_json("POST", "/drive/v1/metas/batch_query",
                     {"metas": [], "failed_list": [{"token": "GONE", "code": 970005}]})
        with self.assertLogs("fbk.walker", level="WARNING"):
            items = list(walk_my_space(make(t)))
        self.assertEqual([i.token for i in items], ["D"])

    def test_folder_name_with_slash_sanitized(self):
        t = FakeTransport()
        self.root(t)
        files_resp(t, "ROOT", [f("FLD1", "A/B", "folder")])
        files_resp(t, "FLD1", [f("D", "x", "docx")])
        self.assertEqual(list(walk_my_space(make(t)))[0].rel_dir, ("我的空间", "A_B"))

    def test_folder_cycle_guard(self):
        t = FakeTransport()
        self.root(t)
        # 同一个文件夹出现两次（如被快捷方式/重复引用），只遍历一次
        files_resp(t, "ROOT", [f("FLD1", "x", "folder"), f("FLD1", "x2", "folder")])
        files_resp(t, "FLD1", [f("D", "d", "docx")])
        items = list(walk_my_space(make(t)))
        self.assertEqual([i.token for i in items], ["D"])


class WikiTest(unittest.TestCase):
    def node(self, tok, obj, title, parent="", has_child=False, typ="docx", mt="1700000001"):
        return {"space_id": "S1", "node_token": tok, "obj_token": obj, "obj_type": typ,
                "parent_node_token": parent, "title": title, "has_child": has_child,
                "obj_edit_time": mt}

    def test_wiki_children_under_parent_title(self):
        t = FakeTransport()
        t.queue_json("GET", "/wiki/v2/spaces", {"items": [{"space_id": "S1", "name": "产品库"}], "has_more": False})
        t.queue_json("GET", "/wiki/v2/spaces/S1/nodes",
                     {"items": [self.node("N1", "O1", "上新规范", has_child=True)], "has_more": False})
        t.queue_json("GET", "/wiki/v2/spaces/S1/nodes",
                     {"items": [self.node("N2", "O2", "子文档", parent="N1", mt="1700000555")], "has_more": False})
        items = list(walk_wiki(make(t)))
        self.assertEqual(len(items), 2)
        parent, child = items
        self.assertTrue(parent.has_children)
        self.assertEqual(parent.rel_dir, ("知识库", "产品库"))
        self.assertEqual((parent.token, parent.type, parent.source), ("O1", "docx", "wiki"))
        self.assertFalse(child.has_children)
        self.assertEqual(child.rel_dir, ("知识库", "产品库", "上新规范"))
        self.assertEqual(child.mtime, 1700000555)
        qs = [c["query"] for c in t.calls if c["path"] == "/wiki/v2/spaces/S1/nodes"]
        self.assertNotIn("parent_node_token", qs[0])
        self.assertIn("parent_node_token=N1", qs[1])

    def test_wiki_sanitizes_and_missing_time(self):
        t = FakeTransport()
        t.queue_json("GET", "/wiki/v2/spaces", {"items": [{"space_id": "S1", "name": "a/b"}], "has_more": False})
        n = self.node("N1", "O1", "x/y", has_child=True)
        n["obj_edit_time"] = ""
        t.queue_json("GET", "/wiki/v2/spaces/S1/nodes", {"items": [n], "has_more": False})
        t.queue_json("GET", "/wiki/v2/spaces/S1/nodes",
                     {"items": [self.node("N2", "O2", "c", parent="N1")], "has_more": False})
        items = list(walk_wiki(make(t)))
        self.assertEqual(items[0].mtime, 0)
        self.assertEqual(items[1].rel_dir, ("知识库", "a_b", "x_y"))


if __name__ == "__main__":
    unittest.main()
