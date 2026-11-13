import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from tests.fakes import FakeTransport  # noqa: E402
from fbk import chats, config, fetcher, notify, perms, runner, sync, walker  # noqa: E402
from fbk.auth import LoginRequired, NetworkError  # noqa: E402
from fbk.chats import AttachmentHit, LinkHit  # noqa: E402
from fbk.client import ApiError  # noqa: E402
from fbk.names import SHANGHAI  # noqa: E402
from fbk.report import RunReport  # noqa: E402
from fbk.store import Store  # noqa: E402
from fbk.sync import Outcome  # noqa: E402
from fbk.walker import RemoteItem, WalkError  # noqa: E402

NOW = int(datetime(2026, 3, 1, 10, 0, tzinfo=SHANGHAI).timestamp())
T1 = NOW - 86400 * 3


def ri(token, source="my", name=None, ftype="docx", rel=None):
    rel = rel or {"my": ("我的空间",), "wiki": ("知识库", "产品库"), "shared": ("共享给我", "_搜索发现")}[source]
    return RemoteItem(token, ftype, name or token, rel, source, T1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.root = base / "备份盘"
        self.root.mkdir()
        self.cfgdir = base / "cfg"
        self.logdir = base / "logs"
        for target, val in ((config, ("CONFIG_DIR", self.cfgdir)), (config, ("LOG_DIR", self.logdir))):
            p = mock.patch.object(target, val[0], val[1])
            p.start()
            self.addCleanup(p.stop)
        config.save_config(config.Config(app_id="cli_fake", app_secret="sec_fake", backup_root=self.root))
        config.save_token({"access_token": "u-fake", "refresh_token": "r-fake",
                           "expires_in": 7200, "obtained_at": int(time.time()) + 10 ** 6})
        n = mock.patch.object(notify, "notify")
        self.notify = n.start()
        self.addCleanup(n.stop)
        e = mock.patch.object(fetcher, "ensure_space")
        e.start()
        self.addCleanup(e.stop)
        self.t = FakeTransport()
        self.t.queue_json("GET", "/drive/explorer/v2/root_folder/meta", {"token": "fldROOT0001"})
        self.t.queue_json("GET", "/wiki/v2/spaces", {"items": [], "has_more": False})
        self.t.queue_json("GET", "/im/v1/chats", {"items": [], "has_more": False})
        self.t.queue_json("POST", "/suite/docs-api/search/object", {"docs_entities": [], "has_more": False})
        self.t.queue_json("GET", "/drive/v1/files", {"files": [], "has_more": False})

    def tearDown(self):
        logging_cleanup()
        self.tmp.cleanup()

    def run_it(self, **kw):
        kw.setdefault("now", NOW)
        kw.setdefault("force", True)
        return runner.run(transport=self.t, sleep=lambda s: None, **kw)

    def store(self):
        s = Store(self.root / ".feishu-backup" / "manifest.sqlite")
        self.addCleanup(s.close)
        return s

    def report_text(self):
        return (self.root / "_报告" / "2026-03-01_备份日志.md").read_text(encoding="utf-8")

    def notified_keys(self):
        return [c.kwargs.get("key") for c in self.notify.call_args_list]


def logging_cleanup():
    import logging
    lg = logging.getLogger("fbk")
    for h in list(lg.handlers):
        if getattr(h, "_fbk_runner", False):
            lg.removeHandler(h)
            h.close()


class EndToEndTest(Base):
    def preset_drive(self):
        self.t.queue_json("GET", "/drive/v1/files", {"files": [
            {"token": "doxFAKE0001", "type": "docx", "name": "周报", "modified_time": str(T1)},
            {"token": "boxFAKE0001", "type": "file", "name": "合同.zip", "modified_time": str(T1)},
        ], "has_more": False})
        self.t._q[("GET", "/drive/v1/files")].pop(0)  # 去掉 setUp 的空列表
        self.t.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tkFAKE"})
        self.t.queue_json("GET", "/drive/v1/export_tasks/tkFAKE", {"result": {"job_status": 0, "file_token": "ftFAKE"}})
        self.t.queue("GET", "/drive/v1/export_tasks/file/ftFAKE/download", body=b"%PDF-fake")
        self.t.queue("GET", "/drive/v1/files/boxFAKE0001/download", body=b"zip-bytes")

    def test_end_to_end_docx_and_file(self):
        self.preset_drive()
        self.assertEqual(self.run_it(), 0)
        self.assertEqual((self.root / "我的空间" / "周报.pdf").read_bytes(), b"%PDF-fake")
        self.assertEqual((self.root / "我的空间" / "合同.zip").read_bytes(), b"zip-bytes")
        reports = list((self.root / "_报告").glob("*_备份日志.md"))
        self.assertEqual([p.name for p in reports], ["2026-03-01_备份日志.md"])
        self.assertTrue((self.root / "_报告" / "2026-03-01_权限缺口.csv").exists())
        text = self.report_text()
        self.assertIn("## 新增", text)
        self.assertIn("我的空间/周报", text)
        self.assertIsNotNone(self.store().last_success())
        self.notify.assert_not_called()

    def test_same_day_rerun_overwrites_reports(self):
        self.preset_drive()
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(sorted(p.name for p in (self.root / "_报告").iterdir()),
                         ["2026-03-01_备份日志.md", "2026-03-01_权限缺口.csv"])

    def test_run_cleans_stale_parts(self):
        self.preset_drive()
        stale = self.root / "我的空间" / "旧文件.pdf.fbk-part"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"half")
        keep = self.root / "我的空间" / "用户自己的.part"
        keep.write_bytes(b"mine")
        self.assertEqual(self.run_it(), 0)
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())

    def test_log_file_written_and_old_logs_pruned(self):
        self.logdir.mkdir(parents=True)
        old = self.logdir / "fbk-2020-01-01.log"
        old.write_text("old")
        os.utime(str(old), (time.time() - 40 * 86400,) * 2)
        recent = self.logdir / "fbk-recent.log"
        recent.write_text("x")
        self.run_it()
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        today = datetime.now(SHANGHAI).strftime("%Y-%m-%d")
        self.assertTrue((self.logdir / ("fbk-%s.log" % today)).exists())


class GateTest(Base):
    def test_missing_root_does_not_create(self):
        gone = Path(self.tmp.name) / "外接盘" / "飞书备份"
        config.save_config(config.Config(app_id="cli_fake", app_secret="sec_fake", backup_root=gone))
        self.assertEqual(self.run_it(), 2)
        self.assertFalse(gone.exists())
        self.assertFalse(gone.parent.exists())
        self.assertEqual(self.notified_keys(), ["root_missing"])
        self.assertEqual(self.t.calls, [])

    def test_not_due_returns_0_without_requests(self):
        s = self.store()
        s.finish_run(s.start_run(), True, {})
        self.assertEqual(self.run_it(force=False, now=int(time.time())), 0)
        self.assertEqual(self.t.calls, [])

    def test_already_running_quiet(self):
        lock = self.root / ".feishu-backup" / "run.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text(str(os.getppid()))
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(self.t.calls, [])

    def test_offline_first_request_quiet_not_success(self):
        self.t._q[("GET", "/drive/explorer/v2/root_folder/meta")] = [
            (0, {}, urllib.error.URLError("no route"))]
        self.assertEqual(self.run_it(), 0)
        s = self.store()
        self.assertIsNone(s.last_success())
        self.notify.assert_not_called()
        self.assertFalse((self.root / "_报告").exists())

    def test_login_required_notifies(self):
        (self.cfgdir / "token.json").unlink()
        self.assertEqual(self.run_it(), 3)
        self.assertEqual(self.notified_keys(), ["login_required"])
        self.assertIn("飞书备份重新登录", self.notify.call_args.args[1])
        self.assertIsNone(self.store().last_success())


class _Recorder:
    """假 sync_item：记录每个 item 的开始/结束顺序。"""

    def __init__(self, slow=("my",), raise_on=None):
        self.events = []
        self.lock = threading.Lock()
        self.slow = slow
        self.raise_on = raise_on or {}
        self.calls = []

    def __call__(self, ctx, item):
        with self.lock:
            self.events.append(("start", item.source, item.token))
            self.calls.append((ctx, item))
        if item.token in self.raise_on:
            raise self.raise_on[item.token]
        if item.source in self.slow:
            time.sleep(0.05)
        with self.lock:
            self.events.append(("end", item.source, item.token))
        return Outcome.NEW


class OrchestrationTest(Base):
    def patch(self, obj, name, **kw):
        p = mock.patch.object(obj, name, **kw)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def setup_sources(self, my=(), wiki=(), shared=(), wiki_error=False, links=(), atts=()):
        def my_walk(client, errors=None):
            yield from my

        def wiki_walk(client, errors=None):
            if wiki_error:
                errors.append(WalkError(("知识库", "坏空间"), "飞书接口错误 code=1 msg=x"))
            yield from wiki

        self.patch(walker, "walk_my_space", side_effect=my_walk)
        self.patch(walker, "walk_wiki", side_effect=wiki_walk)
        self.search = self.patch(chats, "search_shared", side_effect=lambda c, known: iter(shared))
        self.scan = self.patch(chats, "scan", side_effect=lambda *a, **k: iter(list(links) + list(atts)))
        self.mark = self.patch(sync, "mark_missing", return_value=0)

    def test_phases_strictly_ordered_and_single_ctx(self):
        rec = _Recorder(slow=("my", "wiki"))
        self.patch(sync, "sync_item", side_effect=rec)
        self.setup_sources(my=[ri("m%d" % i) for i in range(6)],
                           wiki=[ri("w%d" % i, "wiki") for i in range(6)],
                           shared=[ri("s1", "shared")])
        self.assertEqual(self.run_it(), 0)
        order = rec.events
        last_my_end = max(i for i, e in enumerate(order) if e[1] == "my" and e[0] == "end")
        first_wiki_start = min(i for i, e in enumerate(order) if e[1] == "wiki" and e[0] == "start")
        last_wiki_end = max(i for i, e in enumerate(order) if e[1] == "wiki" and e[0] == "end")
        first_shared = min(i for i, e in enumerate(order) if e[1] == "shared")
        self.assertLess(last_my_end, first_wiki_start)
        self.assertLess(last_wiki_end, first_shared)
        self.assertEqual(len({id(c) for c, _ in rec.calls}), 1)
        self.assertEqual(len(rec.calls), 13)
        self.mark.assert_called_once()
        seen = self.mark.call_args.args[1]
        self.assertTrue({"m0", "w5"} <= set(seen))

    def test_walk_error_skips_mark_missing_and_is_reported(self):
        self.patch(sync, "sync_item", side_effect=_Recorder())
        self.setup_sources(my=[ri("m1")], wiki_error=True)
        self.assertEqual(self.run_it(), 0)
        self.mark.assert_not_called()
        self.assertIn("坏空间", self.report_text())

    def test_mark_missing_keeps_shared_rows(self):
        s = self.store()
        s.upsert_item(token="sOLD", source="shared", status="ok", type="docx")
        s.upsert_item(token="mOLD", source="my", status="ok", type="docx")
        self.patch(sync, "sync_item", side_effect=_Recorder())
        self.setup_sources(my=[ri("m1")])
        self.run_it()
        seen = set(self.mark.call_args.args[1])
        self.assertIn("sOLD", seen)
        self.assertNotIn("mOLD", seen)

    def test_network_error_mid_run_quiet_not_success(self):
        self.patch(sync, "sync_item", side_effect=_Recorder(raise_on={"m2": NetworkError("x")}))
        self.setup_sources(my=[ri("m%d" % i) for i in range(5)], wiki=[ri("w1", "wiki")])
        self.assertEqual(self.run_it(), 0)
        s = self.store()
        self.assertIsNone(s.last_success())
        self.notify.assert_not_called()
        self.search.assert_not_called()

    def test_login_required_in_worker_exit_3(self):
        self.patch(sync, "sync_item", side_effect=_Recorder(raise_on={"m1": LoginRequired("x")}))
        self.setup_sources(my=[ri("m0"), ri("m1")], wiki=[ri("w1", "wiki")])
        self.assertEqual(self.run_it(), 3)
        self.assertEqual(self.notified_keys(), ["login_required"])
        s = self.store()
        self.assertIsNone(s.last_success())
        self.assertEqual(s._read("SELECT ok FROM runs")[0][0], 0)

    def test_disk_full_exit_4(self):
        self.patch(sync, "sync_item", side_effect=_Recorder(raise_on={"w1": fetcher.DiskFull("x")}))
        self.setup_sources(my=[ri("m0")], wiki=[ri("w1", "wiki")])
        self.assertEqual(self.run_it(), 4)
        self.assertEqual(self.notified_keys(), ["disk_full"])
        self.assertIn("备份盘空间不足", self.notify.call_args.args[1])
        self.assertIsNone(self.store().last_success())

    def test_link_api_error_counted_and_continues(self):
        rec = _Recorder()
        self.patch(sync, "sync_item", side_effect=rec)
        bad = LinkHit("docx", "doxBAD00001", "项目群", "张三", NOW - 60, "坏链接")
        good = LinkHit("docx", "doxGOOD0001", "项目群", "张三", NOW - 50, "好链接")

        def fake_process(client, store, hit, known, now):
            if hit.token == "doxBAD00001":
                raise ApiError(500, "boom")
            return ri("doxGOOD0001", "shared", rel=("共享给我", "项目群"))

        self.patch(perms, "process_link", side_effect=fake_process)
        self.setup_sources(my=[ri("m1")], links=[bad, good])
        self.assertEqual(self.run_it(), 0)
        self.assertIn("doxGOOD0001", [i.token for _, i in rec.calls])
        text = self.report_text()
        self.assertIn("## 失败", text)
        self.assertIn("doxBAD00001", text)

    def test_attachments_new_and_earlier_failures_saved(self):
        self.patch(sync, "sync_item", side_effect=_Recorder())
        s = self.store()
        s.upsert_attachment("fkOLD", chat_id="oc_1", message_id="om_1", status="failed",
                            file_name="旧.pdf", chat_name="项目群", sent_at=NOW - 999)
        new = AttachmentHit("oc_1", "项目群", "om_2", "fkNEW", "新.pdf", NOW - 10)
        saved = []
        self.patch(chats, "save_attachment", side_effect=lambda ctx, h: saved.append(h.file_key) or Outcome.NEW)
        self.setup_sources(atts=[new])
        self.assertEqual(self.run_it(), 0)
        self.assertEqual(sorted(saved), ["fkNEW", "fkOLD"])

    def test_limit_stops_and_skips_chats_and_mark_missing(self):
        rec = _Recorder(slow=())
        self.patch(sync, "sync_item", side_effect=rec)
        self.setup_sources(my=[ri("m%d" % i) for i in range(3)], wiki=[ri("w1", "wiki")])
        self.assertEqual(self.run_it(limit=2), 0)
        self.assertEqual(len(rec.calls), 2)
        self.mark.assert_not_called()
        self.scan.assert_not_called()
        self.search.assert_not_called()
        self.assertIsNone(self.store().last_success())  # 冒烟跑不算一次完整成功

    def test_failed_and_updated_reported(self):
        outs = {"m1": Outcome.FAILED, "m2": Outcome.UPDATED, "m3": Outcome.SKIPPED}

        def fake(ctx, item):
            if item.token == "m1":
                import logging
                logging.getLogger("fbk.sync").warning("同步失败：我的空间/m1（导出超时）")
            return outs[item.token]

        self.patch(sync, "sync_item", side_effect=fake)
        self.setup_sources(my=[ri("m1"), ri("m2"), ri("m3", ftype="mindnote")])
        self.run_it()
        text = self.report_text()
        self.assertIn("导出超时", text)
        self.assertIn("mindnote", text)
        arch = text.split("## 已归档的历史版本")[1]
        self.assertIn("我的空间/m2", arch)


class ReportTest(unittest.TestCase):
    def test_sections_in_order(self):
        r = RunReport()
        r.add(Outcome.NEW, "我的空间/a.pdf")
        r.add(Outcome.FAILED, "我的空间/b.pdf", "导出失败")
        r.add(Outcome.UNCHANGED, "我的空间/c.pdf")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "_报告" / "x.md"
            r.write_markdown(p, NOW, NOW + 125)
            text = p.read_text(encoding="utf-8")
        heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
        self.assertEqual(heads[:7], ["## 新增", "## 更新", "## 移动", "## 去重", "## 跳过", "## 失败",
                                     "## 已归档的历史版本"])
        self.assertIn("导出失败", text)
        self.assertIn("2 分 5 秒", text)
        self.assertEqual(r.counts[Outcome.UNCHANGED], 1)


if __name__ == "__main__":
    unittest.main()
