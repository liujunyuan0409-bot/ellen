import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk.store import Store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / ".feishu-backup" / "manifest.sqlite"
        self.s = Store(self.db)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def test_schema_and_version(self):
        names = {r[0] for r in self.s._conn.execute("select name from sqlite_master where type='table'")}
        self.assertTrue({"items", "blobs", "chat_cursor", "attachments", "links", "runs"} <= names)
        self.assertEqual(self.s._conn.execute("pragma user_version").fetchone()[0], 2)

    def test_paths_stored_relative_and_resolved_against_current_root(self):
        # 终审 C1：清单里存相对备份根目录的路径，读出来按当前根目录拼回绝对路径
        p = self.root / "我的空间" / "a" / "周报.pdf"
        self.s.upsert_item(token="t1", status="ok", local_path=p)
        self.s.upsert_attachment("fk", status="ok", local_path=self.root / "群聊附件" / "群" / "x.txt")
        self.s.add_blob("h", p)
        raw = lambda q: self.s._conn.execute(q).fetchone()[0]  # noqa: E731
        self.assertEqual(raw("select local_path from items"), "我的空间/a/周报.pdf")
        self.assertEqual(raw("select local_path from attachments"), "群聊附件/群/x.txt")
        self.assertEqual(raw("select local_path from blobs"), "我的空间/a/周报.pdf")
        self.assertEqual(self.s.get_item("t1")["local_path"], str(p))
        self.assertEqual(self.s.find_blob("h"), p)
        self.assertEqual(self.s.attachments_by_status("ok")[0]["local_path"],
                         str(self.root / "群聊附件" / "群" / "x.txt"))

    def test_migrates_absolute_paths_from_old_root(self):
        import sqlite3
        self.s.close()
        old = "/Volumes/旧盘/飞书备份"
        c = sqlite3.connect(str(self.db))
        c.execute("insert into items (token, status, local_path) values ('t1', 'ok', ?)",
                  (old + "/我的空间/x/我的空间.pdf",))
        c.execute("insert into items (token, status, local_path) values ('t2', 'ok', ?)",
                  (str(self.root / "知识库" / "库" / "y.pdf"),))
        c.execute("insert into items (token, status, local_path) values ('t3', 'ok', '/elsewhere/z.pdf')")
        c.execute("insert into blobs values ('h', ?)", (old + "/共享给我/群/b.zip",))
        c.execute("insert into attachments (file_key, status, local_path) values ('fk', 'ok', ?)",
                  (old + "/群聊附件/群/2025-01-01_a.txt",))
        c.execute("pragma user_version = 1")
        c.commit()
        c.close()
        self.s = Store(self.db)
        raw = lambda q: [r[0] for r in self.s._conn.execute(q)]  # noqa: E731
        self.assertEqual(raw("select local_path from items order by token"),
                         ["我的空间/x/我的空间.pdf", "知识库/库/y.pdf", "/elsewhere/z.pdf"])
        self.assertEqual(raw("select local_path from blobs"), ["共享给我/群/b.zip"])
        self.assertEqual(raw("select local_path from attachments"), ["群聊附件/群/2025-01-01_a.txt"])
        self.assertEqual(self.s.get_item("t1")["local_path"], str(self.root / "我的空间" / "x" / "我的空间.pdf"))
        self.assertEqual(self.s.get_item("t3")["local_path"], "/elsewhere/z.pdf")

    def test_missing_root_raises(self):
        with self.assertRaises(FileNotFoundError):
            Store(self.root / "nope" / ".feishu-backup" / "m.sqlite")

    def test_upsert_item_overwrites_and_json(self):
        self.s.upsert_item(token="t1", type="docx", source="my", remote_dir=["我的", "a"],
                           name="x", remote_mtime=1, status="ok", local_path=Path("/a/b.pdf"))
        self.s.upsert_item(token="t1", name="y", remote_mtime=2, also_seen=["k"])
        r = self.s.get_item("t1")
        self.assertEqual((r["name"], r["remote_mtime"], r["status"]), ("y", 2, "ok"))
        self.assertEqual(r["remote_dir"], '["我的", "a"]')
        self.assertEqual(r["also_seen"], '["k"]')
        self.assertEqual(r["local_path"], "/a/b.pdf")
        self.assertIsNone(self.s.get_item("zz"))

    def test_all_tokens_and_set_status(self):
        self.s.upsert_item(token="a", status="ok")
        self.s.upsert_item(token="b", status="failed")
        self.s.upsert_item(token="c", status="remote_deleted")
        self.assertEqual(self.s.all_tokens(), {"a"})
        self.assertEqual(self.s.all_tokens(("ok", "failed")), {"a", "b"})
        self.s.set_status("b", "ok")
        self.assertEqual(self.s.all_tokens(), {"a", "b"})

    def test_blobs_and_cursor(self):
        self.assertIsNone(self.s.find_blob("h"))
        self.s.add_blob("h", Path("/x/y"))
        self.assertEqual(self.s.find_blob("h"), Path("/x/y"))
        self.assertIsNone(self.s.get_cursor("c"))
        self.s.set_cursor("c", 5)
        self.s.set_cursor("c", 9)
        self.assertEqual(self.s.get_cursor("c"), 9)

    def test_attachment(self):
        self.s.upsert_attachment("fk", chat_id="c", message_id="m", status="failed")
        self.s.upsert_attachment("fk", sha256="h", local_path=Path("/p"), status="ok")
        r = self.s.get_attachment("fk")
        self.assertEqual((r["chat_id"], r["sha256"], r["status"], r["local_path"]), ("c", "h", "ok", "/p"))
        self.assertIsNone(self.s.get_attachment("nope"))

    def test_link_keeps_earliest_first_seen(self):
        self.s.upsert_link("L", kind="docx", first_seen=100, status="unknown")
        self.s.upsert_link("L", status="readable", first_seen=200)
        self.s.upsert_link("L2", kind="sheet", first_seen=50)
        rows = {r["token"]: r for r in self.s.links()}
        self.assertEqual(rows["L"]["first_seen"], 100)
        self.assertEqual(rows["L"]["status"], "readable")
        self.assertEqual(rows["L"]["kind"], "docx")
        self.assertEqual(len(rows), 2)

    def test_last_run_is_latest_finished(self):
        self.assertIsNone(self.s.last_run())
        r1 = self.s.start_run()
        self.s.finish_run(r1, True, {"n": 1})
        r2 = self.s.start_run()
        self.s.finish_run(r2, False, {"err": "x"})
        self.s.start_run()  # 还没结束的不算
        row = self.s.last_run()
        self.assertEqual((row["id"], row["ok"]), (r2, 0))
        self.assertEqual(json.loads(row["summary"]), {"err": "x"})

    def test_last_success_only_ok(self):
        self.assertIsNone(self.s.last_success())
        r1 = self.s.start_run()
        self.s.finish_run(r1, True, {"n": 1})
        r2 = self.s.start_run()
        self.s.finish_run(r2, False, {"err": "x"})
        row = self.s._conn.execute("select * from runs where id=?", (r1,)).fetchone()
        self.assertEqual(row["ok"], 1)
        self.assertEqual(self.s.last_success(), row["finished"])
        self.assertIsInstance(self.s.last_success(), int)

    def test_thread_safe_writes(self):
        def work(i):
            for j in range(25):
                self.s.upsert_item(token="t%d_%d" % (i, j), status="ok")
        ts = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(len(self.s.all_tokens()), 100)

    def test_durable_after_reopen(self):
        self.s.upsert_item(token="d", status="ok")
        s2 = Store(self.db)
        self.assertEqual(s2.all_tokens(), {"d"})
        s2.close()

    def test_link_url_column_and_migration(self):
        self.s.upsert_link("TOK", kind="docx", url="https://example.feishu.cn/docx/TOK", first_seen=5)
        self.assertEqual(self.s.links()[0]["url"], "https://example.feishu.cn/docx/TOK")
        # 旧库（links 无 url 列）打开时自动补列
        import sqlite3
        db = self.root / ".feishu-backup" / "old.sqlite"
        c = sqlite3.connect(str(db))
        c.execute("CREATE TABLE links (token TEXT PRIMARY KEY, kind TEXT, status TEXT, hint TEXT, "
                  "sender TEXT, chat_name TEXT, sent_at INTEGER, first_seen INTEGER)")
        c.commit()
        c.close()
        s2 = Store(db)
        s2.upsert_link("X", url="u")
        self.assertEqual(s2.links()[0]["url"], "u")
        s2.close()


if __name__ == "__main__":
    unittest.main()
