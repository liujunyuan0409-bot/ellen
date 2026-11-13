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
        self.assertEqual(self.s._conn.execute("pragma user_version").fetchone()[0], 1)

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


if __name__ == "__main__":
    unittest.main()
