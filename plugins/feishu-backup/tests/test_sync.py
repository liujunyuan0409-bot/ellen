import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import fetcher, sync  # noqa: E402
from fbk.auth import LoginRequired, NetworkError  # noqa: E402
from fbk.names import SHANGHAI  # noqa: E402
from fbk.store import Store  # noqa: E402
from fbk.sync import Outcome, SyncContext, archive_current, mark_missing, sync_item  # noqa: E402
from fbk.walker import RemoteItem  # noqa: E402
from tests.fakes import LpSpy  # noqa: E402

T1 = int(datetime(2025, 10, 2, 15, 30, tzinfo=SHANGHAI).timestamp())
T2 = int(datetime(2025, 11, 5, 9, 0, tzinfo=SHANGHAI).timestamp())
T3 = int(datetime(2025, 12, 1, 8, 15, tzinfo=SHANGHAI).timestamp())


class FakeFetch:
    """content[token] = bytes 或 Exception；每次下载/导出都写当前内容。"""

    def __init__(self):
        self.content = {}
        self.calls = []

    def _write(self, token, dest):
        self.calls.append((token, Path(dest)))
        c = self.content[token]
        if isinstance(c, BaseException):
            raise c
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(c)
        return Path(dest)

    def export_to(self, client, token, ftype, dest, **kw):
        return self._write(token, dest)

    def download_file(self, client, token, dest):
        return self._write(token, dest)

    def ensure_space(self, root, need_bytes, margin=0):
        pass

    sha256_file = staticmethod(fetcher.sha256_file)


def item(token, name, rel_dir=("我的空间",), ftype="docx", source="my", mtime=T1):
    return RemoteItem(token, ftype, name, tuple(rel_dir), source, mtime)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / ".feishu-backup" / "manifest.sqlite")
        self.fetch = FakeFetch()
        self.ctx = SyncContext(client=None, store=self.store, root=self.root, fetch=self.fetch)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def new_run(self):
        self.ctx = SyncContext(client=None, store=self.store, root=self.root, fetch=self.fetch)

    def all_files(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*")
                      if p.is_file() and ".feishu-backup" not in p.parts)

    # ---- 新增 ----
    def test_new_doc_saved_as_pdf(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        out = sync_item(self.ctx, item("doxFAKE01", "品牌手册"))
        self.assertEqual(out, Outcome.NEW)
        p = self.root / "我的空间" / "品牌手册.pdf"
        self.assertEqual(p.read_bytes(), b"v1")
        row = self.store.get_item("doxFAKE01")
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["local_path"], str(p))
        self.assertEqual(row["remote_mtime"], T1)
        self.assertEqual(json.loads(row["remote_dir"]), ["我的空间"])
        self.assertEqual(self.all_files(), ["我的空间/品牌手册.pdf"])

    def test_new_file_records_sha_and_blob(self):
        self.fetch.content["boxFAKE01"] = b"raw"
        self.assertEqual(sync_item(self.ctx, item("boxFAKE01", "a.zip", ftype="file")), Outcome.NEW)
        p = self.root / "我的空间" / "a.zip"
        row = self.store.get_item("boxFAKE01")
        self.assertEqual(row["sha256"], fetcher.sha256_file(p))
        self.assertEqual(self.store.find_blob(row["sha256"]), p)

    def test_unknown_type_skipped(self):
        out = sync_item(self.ctx, item("mndFAKE01", "思维导图", ftype="mindnote"))
        self.assertEqual(out, Outcome.SKIPPED)
        self.assertEqual(self.store.get_item("mndFAKE01")["status"], "skipped")
        self.assertEqual(self.fetch.calls, [])
        self.assertEqual(self.all_files(), [])

    def test_remote_folder_named_history_is_normal_dir(self):
        self.fetch.content["doxFAKE02"] = b"x"
        out = sync_item(self.ctx, item("doxFAKE02", "说明", rel_dir=("我的空间", "历史版本")))
        self.assertEqual(out, Outcome.NEW)
        self.assertEqual((self.root / "我的空间" / "历史版本" / "说明.pdf").read_bytes(), b"x")
        # 再次同步：不变，不被当成归档目录处理
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE02", "说明", rel_dir=("我的空间", "历史版本"))),
                         Outcome.UNCHANGED)
        self.assertEqual(self.all_files(), ["我的空间/历史版本/说明.pdf"])

    def test_same_name_different_token_disambiguated(self):
        self.fetch.content["doxAAAAAA1"] = b"a"
        self.fetch.content["doxBBBBBB2"] = b"b"
        self.assertEqual(sync_item(self.ctx, item("doxAAAAAA1", "周报")), Outcome.NEW)
        self.assertEqual(sync_item(self.ctx, item("doxBBBBBB2", "周报")), Outcome.NEW)
        d = self.root / "我的空间"
        self.assertEqual((d / "周报.pdf").read_bytes(), b"a")
        self.assertEqual((d / "周报 (doxBBB).pdf").read_bytes(), b"b")
        # 下一轮两者都稳定，不来回搬
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxAAAAAA1", "周报")), Outcome.UNCHANGED)
        self.assertEqual(sync_item(self.ctx, item("doxBBBBBB2", "周报")), Outcome.UNCHANGED)

    def test_new_never_overwrites_unknown_local_file(self):
        d = self.root / "我的空间"
        d.mkdir(parents=True)
        (d / "合同.pdf").write_bytes(b"user's own")
        self.fetch.content["doxFAKE03"] = b"remote"
        self.assertEqual(sync_item(self.ctx, item("doxFAKE03", "合同")), Outcome.NEW)
        self.assertEqual((d / "合同.pdf").read_bytes(), b"user's own")
        self.assertEqual((d / "合同 (doxFAK).pdf").read_bytes(), b"remote")

    # ---- 更新 ----
    def test_update_archives_old_with_old_mtime(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T1))
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        out = sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T2))
        self.assertEqual(out, Outcome.UPDATED)
        d = self.root / "我的空间"
        self.assertTrue((d / "历史版本" / "品牌手册_2025-10-02_1530.pdf").exists())
        self.assertEqual((d / "历史版本" / "品牌手册_2025-10-02_1530.pdf").read_bytes(), b"v1")
        self.assertEqual((d / "品牌手册.pdf").read_bytes(), b"v2")
        self.assertEqual(self.store.get_item("doxFAKE01")["remote_mtime"], T2)
        self.assertEqual(self.all_files(), ["我的空间/历史版本/品牌手册_2025-10-02_1530.pdf",
                                            "我的空间/品牌手册.pdf"])

    def test_update_twice_keeps_every_version(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T1))
        for data, t in ((b"v2", T2), (b"v3", T3)):
            self.fetch.content["doxFAKE01"] = data
            self.new_run()
            self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=t)), Outcome.UPDATED)
        h = self.root / "我的空间" / "历史版本"
        self.assertEqual((h / "品牌手册_2025-10-02_1530.pdf").read_bytes(), b"v1")
        self.assertEqual((h / "品牌手册_2025-11-05_0900.pdf").read_bytes(), b"v2")
        self.assertEqual((self.root / "我的空间" / "品牌手册.pdf").read_bytes(), b"v3")

    def test_update_download_failure_keeps_old_file_in_place(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T1))
        self.fetch.content["doxFAKE01"] = RuntimeError("导出失败")
        self.new_run()
        out = sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T2))
        self.assertEqual(out, Outcome.FAILED)
        self.assertEqual((self.root / "我的空间" / "品牌手册.pdf").read_bytes(), b"v1")
        self.assertEqual(self.all_files(), ["我的空间/品牌手册.pdf"])
        row = self.store.get_item("doxFAKE01")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["remote_mtime"], T1)  # 下次还会重试
        # 下次成功后正常归档
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T2)), Outcome.UPDATED)
        self.assertEqual((self.root / "我的空间" / "历史版本" / "品牌手册_2025-10-02_1530.pdf").read_bytes(), b"v1")

    def test_identical_file_content_not_archived(self):
        self.fetch.content["boxFAKE01"] = b"same"
        sync_item(self.ctx, item("boxFAKE01", "报价.xlsx", ftype="file", mtime=T1))
        self.new_run()
        out = sync_item(self.ctx, item("boxFAKE01", "报价.xlsx", ftype="file", mtime=T2))
        self.assertEqual(out, Outcome.UNCHANGED)
        self.assertEqual(self.all_files(), ["我的空间/报价.xlsx"])
        self.assertEqual(self.store.get_item("boxFAKE01")["remote_mtime"], T2)

    def test_file_content_changed_archived(self):
        self.fetch.content["boxFAKE01"] = b"old"
        sync_item(self.ctx, item("boxFAKE01", "报价.xlsx", ftype="file", mtime=T1))
        self.fetch.content["boxFAKE01"] = b"new"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("boxFAKE01", "报价.xlsx", ftype="file", mtime=T2)),
                         Outcome.UPDATED)
        d = self.root / "我的空间"
        self.assertEqual((d / "历史版本" / "报价_2025-10-02_1530.xlsx").read_bytes(), b"old")
        self.assertEqual((d / "报价.xlsx").read_bytes(), b"new")

    def test_stale_temp_from_crash_removed_and_old_kept(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T1))
        d = self.root / "我的空间"
        (d / "品牌手册.pdf.doxFAK.fbk-new").write_bytes(b"half")
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "品牌手册", mtime=T2)), Outcome.UPDATED)
        self.assertEqual(self.all_files(), ["我的空间/历史版本/品牌手册_2025-10-02_1530.pdf",
                                            "我的空间/品牌手册.pdf"])

    def test_archive_name_collision_uses_suffix(self):
        d = self.root / "我的空间"
        (d / "历史版本").mkdir(parents=True)
        (d / "历史版本" / "a_2025-10-02_1530.pdf").write_bytes(b"older")
        (d / "a.pdf").write_bytes(b"cur")
        dest = archive_current(d / "a.pdf", T1)
        self.assertEqual(dest.name, "a_2025-10-02_1530-2.pdf")
        self.assertEqual(dest.read_bytes(), b"cur")
        self.assertEqual((d / "历史版本" / "a_2025-10-02_1530.pdf").read_bytes(), b"older")
        self.assertFalse((d / "a.pdf").exists())

    def test_local_deleted_by_user_refetched(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册"))
        (self.root / "我的空间" / "品牌手册.pdf").unlink()
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "品牌手册")), Outcome.NEW)
        self.assertEqual((self.root / "我的空间" / "品牌手册.pdf").read_bytes(), b"v1")

    # ---- 去重 ----
    def test_same_token_second_location_is_duplicate(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "A"))), Outcome.NEW)
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("知识库", "团队"), source="wiki"))
        self.assertEqual(out, Outcome.DUPLICATE)
        # 同一轮同来源第二个位置（快捷方式）也是重复
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "B")))
        self.assertEqual(out, Outcome.DUPLICATE)
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("知识库", "团队"), source="wiki"))
        self.assertEqual(out, Outcome.DUPLICATE)
        row = self.store.get_item("doxFAKE01")
        self.assertEqual(json.loads(row["also_seen"]), ["知识库/团队", "我的空间/B"])
        self.assertEqual(row["local_path"], str(self.root / "我的空间" / "A" / "手册.pdf"))
        self.assertEqual(self.all_files(), ["我的空间/A/手册.pdf"])
        self.assertEqual(len(self.fetch.calls), 1)
        # 下一轮按阶段 my → wiki：wiki 位置仍是重复
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "A"))),
                         Outcome.UNCHANGED)
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("知识库", "团队"), source="wiki"))
        self.assertEqual(out, Outcome.DUPLICATE)

    def test_moved_out_of_my_space_into_wiki_keeps_updating(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", mtime=T1)), Outcome.NEW)
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()  # 我的空间里已没有它，只在知识库出现，且内容更新
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("知识库", "团队"), source="wiki", mtime=T2))
        self.assertIn(out, (Outcome.MOVED, Outcome.UPDATED))
        w = self.root / "知识库" / "团队"
        self.assertEqual((w / "手册.pdf").read_bytes(), b"v2")
        self.assertEqual((w / "历史版本" / "手册_2025-10-02_1530.pdf").read_bytes(), b"v1")
        self.assertEqual(self.all_files(), ["知识库/团队/历史版本/手册_2025-10-02_1530.pdf", "知识库/团队/手册.pdf"])
        row = self.store.get_item("doxFAKE01")
        self.assertEqual((row["status"], row["source"], row["remote_mtime"]), ("ok", "wiki", T2))

    def test_failed_sync_of_content_duplicate_never_touches_owner_file(self):
        # 复现：Y 内容与 X 相同被判重复；Y 下一次同步失败；再下一次 Y 有新内容
        X = lambda m: item("boxFAKEX1", "x.zip", ftype="file", mtime=m)
        Y = lambda m: item("boxFAKEY1", "y.zip", rel_dir=("共享给我",), ftype="file", source="shared", mtime=m)
        self.fetch.content["boxFAKEX1"] = b"X1"
        sync_item(self.ctx, X(T1))
        self.fetch.content["boxFAKEX1"] = b"X2"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, X(T2)), Outcome.UPDATED)
        self.fetch.content["boxFAKEY1"] = b"X2"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, Y(T1)), Outcome.DUPLICATE)
        self.fetch.content["boxFAKEY1"] = RuntimeError("boom")
        self.new_run()
        self.assertEqual(sync_item(self.ctx, Y(T2)), Outcome.FAILED)
        row = self.store.get_item("boxFAKEY1")
        self.assertEqual(row["status"], "duplicate")
        self.assertEqual(row["local_path"], str(self.root / "我的空间" / "x.zip"))
        self.fetch.content["boxFAKEY1"] = b"Y3"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, Y(T3)), Outcome.NEW)
        d = self.root / "我的空间"
        self.assertEqual((d / "x.zip").read_bytes(), b"X2")
        self.assertEqual((d / "历史版本" / "x_2025-10-02_1530.zip").read_bytes(), b"X1")
        self.assertEqual((self.root / "共享给我" / "y.zip").read_bytes(), b"Y3")
        self.assertEqual(self.all_files(), ["共享给我/y.zip", "我的空间/x.zip", "我的空间/历史版本/x_2025-10-02_1530.zip"])
        self.assertEqual(self.store.get_item("boxFAKEX1")["local_path"], str(d / "x.zip"))

    def test_concurrent_same_token_claimed_once(self):
        import threading
        gate = threading.Event()
        entered = threading.Event()
        orig = self.fetch._write

        def blocking_write(token, dest):
            entered.set()
            gate.wait(timeout=5)
            return orig(token, dest)
        self.fetch._write = blocking_write
        self.fetch.content["doxFAKE01"] = b"v1"
        res = []
        a = threading.Thread(target=lambda: res.append(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "A")))))
        b = threading.Thread(target=lambda: res.append(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "B")))))
        a.start()
        self.assertTrue(entered.wait(timeout=5))  # 第一个已在下载中
        b.start()
        b.join(timeout=5)
        gate.set()
        a.join(timeout=5)
        self.assertEqual(sorted(res), sorted([Outcome.NEW, Outcome.DUPLICATE]))
        self.assertEqual(len(self.all_files()), 1)
        self.assertEqual(len(self.fetch.calls), 1)

    def test_higher_priority_source_takes_over(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("知识库", "团队"), source="wiki"))
        self.new_run()
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间",), source="my"))
        self.assertEqual(out, Outcome.MOVED)
        self.assertEqual(self.all_files(), ["我的空间/手册.pdf"])
        row = self.store.get_item("doxFAKE01")
        self.assertEqual(row["source"], "my")
        self.assertEqual(json.loads(row["also_seen"]), ["知识库/团队"])

    def test_same_content_different_token_deduped(self):
        self.fetch.content["boxFAKE01"] = b"same bytes"
        self.fetch.content["boxFAKE02"] = b"same bytes"
        sync_item(self.ctx, item("boxFAKE01", "a.zip", ftype="file"))
        out = sync_item(self.ctx, item("boxFAKE02", "b.zip", rel_dir=("共享给我",), ftype="file", source="shared"))
        self.assertEqual(out, Outcome.DUPLICATE)
        self.assertEqual(self.all_files(), ["我的空间/a.zip"])
        row = self.store.get_item("boxFAKE02")
        self.assertEqual(row["local_path"], str(self.root / "我的空间" / "a.zip"))
        # 下一轮不变：仍是重复，不重复下载
        self.new_run()
        n = len(self.fetch.calls)
        out = sync_item(self.ctx, item("boxFAKE02", "b.zip", rel_dir=("共享给我",), ftype="file", source="shared"))
        self.assertEqual(out, Outcome.DUPLICATE)
        self.assertEqual(len(self.fetch.calls), n)

    def test_content_dedupe_ignores_stale_blob(self):
        # a.zip 被更新后，旧 sha 的 blob 指向的文件内容已变；新文件不能被误判重复而丢失
        self.fetch.content["boxFAKE01"] = b"X"
        sync_item(self.ctx, item("boxFAKE01", "a.zip", ftype="file", mtime=T1))
        self.fetch.content["boxFAKE01"] = b"Y"
        self.new_run()
        sync_item(self.ctx, item("boxFAKE01", "a.zip", ftype="file", mtime=T2))
        # 另一个 token 内容是 X（与已归档的旧版相同）
        self.fetch.content["boxFAKE02"] = b"X"
        # 伪造：旧 sha 的 blob 仍指向 a.zip（内容已是 Y）
        self.store.add_blob(fetcher.sha256_file(self.root / "我的空间" / "历史版本" / "a_2025-10-02_1530.zip"),
                            self.root / "我的空间" / "a.zip")
        out = sync_item(self.ctx, item("boxFAKE02", "b.zip", ftype="file"))
        self.assertEqual(out, Outcome.NEW)
        self.assertEqual((self.root / "我的空间" / "b.zip").read_bytes(), b"X")

    # ---- 改名 / 移动 ----
    def test_move_carries_history(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", rel_dir=("我的空间", "旧"), mtime=T1))
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        sync_item(self.ctx, item("doxFAKE01", "品牌手册", rel_dir=("我的空间", "旧"), mtime=T2))
        old = self.root / "我的空间" / "旧"
        # 同目录下别的文件的历史版本不能被一起搬走
        (old / "品牌手册_附录.pdf").write_bytes(b"other")
        (old / "历史版本" / "品牌手册_附录_2025-10-02_1530.pdf").write_bytes(b"other-old")
        self.new_run()
        out = sync_item(self.ctx, item("doxFAKE01", "品牌手册", rel_dir=("我的空间", "新"), mtime=T2))
        self.assertEqual(out, Outcome.MOVED)
        new = self.root / "我的空间" / "新"
        self.assertEqual((new / "品牌手册.pdf").read_bytes(), b"v2")
        self.assertEqual((new / "历史版本" / "品牌手册_2025-10-02_1530.pdf").read_bytes(), b"v1")
        self.assertFalse((old / "品牌手册.pdf").exists())
        self.assertEqual((old / "历史版本" / "品牌手册_附录_2025-10-02_1530.pdf").read_bytes(), b"other-old")
        self.assertEqual(self.store.get_item("doxFAKE01")["local_path"], str(new / "品牌手册.pdf"))
        self.assertEqual(len(self.all_files()), 4)

    def test_rename_renames_history(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "旧名", mtime=T1))
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        sync_item(self.ctx, item("doxFAKE01", "旧名", mtime=T2))
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "新名", mtime=T2)), Outcome.MOVED)
        self.assertEqual(self.all_files(), ["我的空间/历史版本/新名_2025-10-02_1530.pdf", "我的空间/新名.pdf"])

    def test_move_never_overwrites_destination(self):
        self.fetch.content["doxFAKE01"] = b"mine"
        sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "旧")))
        new = self.root / "我的空间" / "新"
        new.mkdir(parents=True)
        (new / "手册.pdf").write_bytes(b"someone else")
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "新"))), Outcome.MOVED)
        self.assertEqual((new / "手册.pdf").read_bytes(), b"someone else")
        self.assertEqual((new / "手册 (doxFAK).pdf").read_bytes(), b"mine")

    def test_move_and_update_same_run(self):
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "旧"), mtime=T1))
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        out = sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "新"), mtime=T2))
        self.assertEqual(out, Outcome.UPDATED)
        new = self.root / "我的空间" / "新"
        self.assertEqual((new / "手册.pdf").read_bytes(), b"v2")
        self.assertEqual((new / "历史版本" / "手册_2025-10-02_1530.pdf").read_bytes(), b"v1")

    def test_max_length_name_new_and_update(self):
        name = "长" * 79  # 237 字节 + .pdf = 241，接近上限
        self.fetch.content["doxFAKE05"] = b"v1"
        self.assertEqual(sync_item(self.ctx, item("doxFAKE05", name, mtime=T1)), Outcome.NEW)
        self.fetch.content["doxFAKE06"] = b"other"
        self.assertEqual(sync_item(self.ctx, item("doxFAKE06", name, mtime=T1)), Outcome.NEW)
        self.fetch.content["doxFAKE06"] = b"v2"
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE06", name, mtime=T2)), Outcome.UPDATED)
        for f in self.all_files():
            self.assertLessEqual(len(Path(f).name.encode("utf-8")), 255)
        self.assertEqual(len(self.all_files()), 3)
        for _, dest in self.fetch.calls:
            self.assertLessEqual(len((dest.name + ".fbk-part").encode("utf-8")), 255)

    def test_concurrent_same_name_tokens_do_not_clobber(self):
        import threading
        barrier = threading.Barrier(2)
        orig = self.fetch._write

        def slow_write(token, dest):
            barrier.wait(timeout=5)
            return orig(token, dest)
        self.fetch._write = slow_write
        self.fetch.content["doxAAAAAA1"] = b"a"
        self.fetch.content["doxBBBBBB2"] = b"b"
        res = {}
        ts = [threading.Thread(target=lambda t=t: res.__setitem__(t, sync_item(self.ctx, item(t, "周报"))))
              for t in ("doxAAAAAA1", "doxBBBBBB2")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(res, {"doxAAAAAA1": Outcome.NEW, "doxBBBBBB2": Outcome.NEW})
        got = {Path(self.store.get_item(t)["local_path"]).read_bytes() for t in res}
        self.assertEqual(got, {b"a", b"b"})
        self.assertEqual(len(self.all_files()), 2)

    # ---- 异常 ----
    def test_disk_full_login_network_propagate(self):
        for exc in (fetcher.DiskFull("满"), LoginRequired("登录"), NetworkError("断网")):
            self.fetch.content["doxFAKE09"] = exc
            self.new_run()
            with self.assertRaises(type(exc)):
                sync_item(self.ctx, item("doxFAKE09", "x"))
        self.assertEqual(self.all_files(), [])

    # ---- 缺失标记 ----
    def test_mark_missing_never_deletes(self):
        self.fetch.content["doxFAKE01"] = b"a"
        self.fetch.content["doxFAKE02"] = b"b"
        sync_item(self.ctx, item("doxFAKE01", "甲"))
        sync_item(self.ctx, item("doxFAKE02", "乙"))
        sync_item(self.ctx, item("mndFAKE03", "丙", ftype="mindnote"))
        n = mark_missing(self.store, {"doxFAKE01"})
        self.assertEqual(n, 1)
        self.assertEqual(self.store.get_item("doxFAKE02")["status"], "remote_deleted")
        self.assertEqual(self.store.get_item("doxFAKE01")["status"], "ok")
        self.assertEqual(self.store.get_item("mndFAKE03")["status"], "skipped")
        self.assertEqual(self.all_files(), ["我的空间/乙.pdf", "我的空间/甲.pdf"])

    # ---- v0.2：同名不区分大小写、长路径 ----
    def test_case_insensitive_collision(self):
        # 同目录先放 Report.pdf（token A），再同步 report.pdf（token B）
        self.fetch.content["doxAAAAAA1"] = b"A"
        self.fetch.content["doxBBBBBB2"] = b"B"
        self.assertEqual(sync_item(self.ctx, item("doxAAAAAA1", "Report")), Outcome.NEW)
        self.assertEqual(sync_item(self.ctx, item("doxBBBBBB2", "report")), Outcome.NEW)
        d = self.root / "我的空间"
        self.assertEqual(sorted(os.listdir(str(d))), ["Report.pdf", "report (doxBBB).pdf"])
        self.assertEqual((d / "Report.pdf").read_bytes(), b"A")
        self.assertEqual((d / "report (doxBBB).pdf").read_bytes(), b"B")

    def _case_sensitive_lexists(self):
        """模拟只认完全同名的检查（大小写敏感的卷、网络盘等），确认判断不靠文件系统本身。"""
        real = os.path.lexists

        def exact(p):
            p = str(p)
            return real(p) and os.path.basename(p) in os.listdir(os.path.dirname(p))
        return mock.patch("os.path.lexists", side_effect=exact)

    def test_case_insensitive_collision_independent_of_fs(self):
        d = self.root / "我的空间"
        d.mkdir(parents=True)
        (d / "Report.pdf").write_bytes(b"user's own")
        self.fetch.content["doxBBBBBB2"] = b"B"
        with self._case_sensitive_lexists():
            self.assertEqual(sync_item(self.ctx, item("doxBBBBBB2", "report")), Outcome.NEW)
        self.assertEqual(sorted(os.listdir(str(d))), ["Report.pdf", "report (doxBBB).pdf"])
        self.assertEqual((d / "Report.pdf").read_bytes(), b"user's own")

    def test_case_insensitive_move_never_overwrites(self):
        self.fetch.content["doxFAKE01"] = b"mine"
        sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "旧")))
        new = self.root / "我的空间" / "新"
        new.mkdir(parents=True)
        (new / "MANUAL.pdf").write_bytes(b"someone else")
        self.new_run()
        with self._case_sensitive_lexists():
            out = sync_item(self.ctx, item("doxFAKE01", "manual", rel_dir=("我的空间", "新")))
        self.assertEqual(out, Outcome.MOVED)
        self.assertEqual((new / "MANUAL.pdf").read_bytes(), b"someone else")
        self.assertEqual(sorted(os.listdir(str(new))), ["MANUAL.pdf", "manual (doxFAK).pdf"])

    def test_case_only_rename_of_own_file_stays_put_name(self):
        # 云端只改了大小写：自己的旧文件不算占用，直接改成新写法，不追加 (token)
        self.fetch.content["doxFAKE01"] = b"v1"
        sync_item(self.ctx, item("doxFAKE01", "Report", mtime=T1))
        self.fetch.content["doxFAKE01"] = b"v2"
        self.new_run()
        sync_item(self.ctx, item("doxFAKE01", "Report", mtime=T2))
        self.new_run()
        with self._case_sensitive_lexists():
            out = sync_item(self.ctx, item("doxFAKE01", "report", mtime=T2))
        self.assertEqual(out, Outcome.MOVED)
        d = self.root / "我的空间"
        self.assertEqual(sorted(os.listdir(str(d))), ["report.pdf", "历史版本"])
        self.assertEqual((d / "report.pdf").read_bytes(), b"v2")
        self.assertEqual(os.listdir(str(d / "历史版本")), ["report_2025-10-02_1530.pdf"])
        self.assertEqual(self.store.get_item("doxFAKE01")["local_path"], str(d / "report.pdf"))
        # 下一轮稳定
        self.new_run()
        self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "report", mtime=T2)), Outcome.UNCHANGED)

    def test_all_file_ops_go_through_lp(self):
        """sync / fetcher 里的 os.replace / unlink / makedirs 都必须用 lp() 处理过的路径。"""
        spy = LpSpy()
        with spy.patch(sync, fetcher):
            self.fetch.content["doxFAKE01"] = b"v1"
            self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "旧"), mtime=T1)),
                             Outcome.NEW)
            self.fetch.content["doxFAKE01"] = b"v2"
            self.new_run()
            self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册", rel_dir=("我的空间", "旧"), mtime=T2)),
                             Outcome.UPDATED)
            self.new_run()
            self.assertEqual(sync_item(self.ctx, item("doxFAKE01", "手册2", rel_dir=("我的空间", "新"), mtime=T2)),
                             Outcome.MOVED)
            (self.root / "x.fbk-part").write_bytes(b"")
            self.assertEqual(fetcher.clean_parts(self.root), 1)
        self.assertEqual(spy.bad, [])
        self.assertGreaterEqual(spy.ops["replace"], 4)  # 新增 1 + 归档 1 + 换新 1 + 移动 1 + 搬历史 1
        self.assertGreaterEqual(spy.ops["unlink"], 1)


if __name__ == "__main__":
    unittest.main()
