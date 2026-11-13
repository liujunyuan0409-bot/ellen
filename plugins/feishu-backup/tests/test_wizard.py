import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from collections import namedtuple
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from tests.fakes import FakeTransport  # noqa: E402
from fbk import config, runner, schedule, wizard  # noqa: E402
from fbk.client import FeishuClient  # noqa: E402
from fbk.store import Store  # noqa: E402

_spec = importlib.util.spec_from_file_location("fbk_cli", str(SCRIPTS / "fbk.py"))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

Usage = namedtuple("Usage", "total used free")
GB = 1000 ** 3


def make_client(t):
    return FeishuClient(lambda: "u-fake", transport=t, rps=0, sleep=lambda s: None)


def space_fixture(t, files=None):
    t.queue_json("GET", "/drive/explorer/v2/root_folder/meta", {"token": "fldROOT0001"})
    files = files if files is not None else [
        {"token": "doxAAAA0001", "type": "docx", "name": "周报", "modified_time": "1700000000"},
        {"token": "boxBBBB0001", "type": "file", "name": "合同.pdf", "modified_time": "1700000000"},
    ]
    t.queue_json("GET", "/drive/v1/files", {"files": files, "has_more": False})


def doctor_fixture(t):
    space_fixture(t)
    t.queue_json("GET", "/wiki/v2/spaces", {"items": [{"space_id": "sp1", "name": "产品库"}], "has_more": False})
    t.queue_json("POST", "/suite/docs-api/search/object", {"docs_entities": [], "has_more": False})
    t.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tkt0001"})
    t.queue("GET", "/drive/v1/files/boxBBBB0001/download", status=206,
            headers={"Content-Range": "bytes 0-0/12345"})
    t.queue_json("GET", "/drive/v1/permissions/doxAAAA0001/members/auth", {"auth_result": True})
    t.queue_json("GET", "/im/v1/chats", {"items": [{"chat_id": "oc_fake01", "name": "测试群"}], "has_more": False})
    t.queue_json("GET", "/im/v1/messages", {"items": [{"message_id": "om_1"}], "has_more": False})


def by_name(checks):
    return {c.name: c for c in checks}


class DoctorTest(unittest.TestCase):
    def test_all_ok(self):
        t = FakeTransport()
        doctor_fixture(t)
        checks = wizard.doctor(make_client(t))
        self.assertEqual(len(checks), 8)
        self.assertTrue(all(c.ok for c in checks), [c for c in checks if not c.ok])
        posts = [c for c in t.calls if c["method"] == "POST"]
        self.assertTrue(any(c["path"] == "/drive/v1/export_tasks" for c in posts))
        # 导出只发起任务，不轮询、不下载
        self.assertFalse(any(c["path"].startswith("/drive/v1/export_tasks/") for c in t.calls))

    def test_bot_not_enabled_232025(self):
        t = FakeTransport()
        doctor_fixture(t)
        t._q[("GET", "/im/v1/chats")] = []
        t.queue_json("GET", "/im/v1/chats", code=232025, msg="Bot ability is not activated.", status=400)
        checks = wizard.doctor(make_client(t))
        bad = [c for c in checks if not c.ok]
        self.assertTrue(bad)
        self.assertIn("机器人", bad[0].fix)

    def test_missing_scope_names_scope(self):
        t = FakeTransport()
        doctor_fixture(t)
        t._q[("GET", "/wiki/v2/spaces")] = []
        t.queue_json("GET", "/wiki/v2/spaces", code=99991672, status=400,
                     msg="Access denied. One of the following scopes is required: [wiki:wiki:readonly].")
        c = [c for c in wizard.doctor(make_client(t)) if not c.ok][0]
        self.assertIn("缺少权限", c.fix)
        self.assertIn("wiki:wiki:readonly", c.fix)
        self.assertIn("setup --login", c.fix)

    def test_no_samples_skips(self):
        t = FakeTransport()
        doctor_fixture(t)
        t._q[("GET", "/drive/v1/files")] = []
        t.queue_json("GET", "/drive/v1/files", {"files": [], "has_more": False})
        t._q[("GET", "/im/v1/chats")] = []
        t.queue_json("GET", "/im/v1/chats", {"items": [], "has_more": False})
        checks = wizard.doctor(make_client(t))
        self.assertTrue(all(c.ok for c in checks))
        skipped = [c for c in checks if "没有可测的样本，跳过" in c.detail]
        self.assertEqual(len(skipped), 4)  # 导出、1 字节、权限判断、读群消息

    def test_network_error(self):
        def boom(*a, **k):
            raise urllib.error.URLError("no route")
        calls = []

        def counting(*a, **k):
            calls.append(1)
            return boom()
        checks = wizard.doctor(make_client(counting))
        self.assertEqual(len(checks), 8)
        self.assertFalse(checks[0].ok)
        self.assertEqual(checks[0].detail, "连不上飞书")
        self.assertEqual(checks[0].fix, "检查网络后重试")
        # 断网后后面几项不再逐个重试
        self.assertTrue(all(c.detail == "连不上飞书" for c in checks))
        self.assertEqual(len(calls), 6)  # 只有第一项的 1 次 + 5 次重试

    def test_missing_scopes_from_login(self):
        granted = " ".join(s for s in wizard.auth.SCOPES if s != "im:chat:readonly")
        c = wizard.check_granted_scopes({"scope": granted})
        self.assertTrue(c.warn)
        self.assertIn("im:chat:readonly", c.fix)
        self.assertIsNone(wizard.check_granted_scopes({"scope": " ".join(wizard.auth.SCOPES)}))
        self.assertIsNone(wizard.check_granted_scopes({}))


class CheckRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def test_missing_path_fails_and_is_not_created(self):
        p = self.home / "外接盘" / "飞书备份"
        checks = wizard.check_root(p, 0, home=self.home)
        self.assertFalse(all(c.ok for c in checks))
        self.assertIn("--create", checks[0].fix)
        self.assertFalse(p.exists())
        self.assertFalse(p.parent.exists())

    def test_writable_and_enough_space(self):
        p = self.home / "备份"
        p.mkdir()
        with mock.patch("fbk.wizard.shutil.disk_usage", return_value=Usage(500 * GB, 100 * GB, 400 * GB)):
            checks = wizard.check_root(p, 10 * GB, home=self.home)
        self.assertTrue(all(c.ok for c in checks), checks)
        self.assertEqual(list(p.iterdir()), [])  # 写入探针已删除

    def test_not_enough_space(self):
        p = self.home / "备份"
        p.mkdir()
        with mock.patch("fbk.wizard.shutil.disk_usage", return_value=Usage(100 * GB, 90 * GB, 10 * GB)):
            checks = wizard.check_root(p, 10 * GB, home=self.home)
        space = by_name(checks)["剩余空间"]
        self.assertFalse(space.ok)
        self.assertIn("15.0 GB", space.detail)

    def test_icloud_dir_warns_but_passes(self):
        p = self.home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "备份"
        p.mkdir(parents=True)
        checks = wizard.check_root(p, 0, home=self.home)
        self.assertTrue(all(c.ok for c in checks))
        self.assertTrue(any(c.warn and "iCloud" in c.detail for c in checks))

    def test_documents_with_icloud_desktop_sync_warns(self):
        (self.home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "Documents").mkdir(parents=True)
        p = self.home / "Documents" / "飞书备份"
        p.mkdir(parents=True)
        checks = wizard.check_root(p, 0, home=self.home)
        self.assertTrue(all(c.ok for c in checks))
        self.assertTrue(any(c.warn for c in checks))

    def test_plain_dir_no_warning(self):
        p = self.home / "飞书备份"
        p.mkdir()
        checks = wizard.check_root(p, 0, home=self.home)
        self.assertFalse(any(c.warn for c in checks))


class ScanStatsTest(unittest.TestCase):
    def fixture(self, export_responses):
        t = FakeTransport()
        space_fixture(t, files=[
            {"token": "boxF0000001", "type": "file", "name": "a.mov", "modified_time": "1"},
            {"token": "boxF0000002", "type": "file", "name": "b.psd", "modified_time": "1"},
            {"token": "doxD0000001", "type": "docx", "name": "d1", "modified_time": "1"},
            {"token": "doxD0000002", "type": "docx", "name": "d2", "modified_time": "1"},
            {"token": "bmnM0000001", "type": "mindnote", "name": "m1", "modified_time": "1"},
        ])
        t.queue_json("GET", "/wiki/v2/spaces", {"items": [{"space_id": "sp1", "name": "产品库"}], "has_more": False})
        t.queue_json("GET", "/wiki/v2/spaces/sp1/nodes", {"items": [
            {"node_token": "wikN0000001", "obj_token": "shtS0000001", "obj_type": "sheet",
             "title": "预算", "has_child": False}], "has_more": False})
        t.queue("GET", "/drive/v1/files/boxF0000001/download", status=206,
                headers={"Content-Range": "bytes 0-0/1000"})
        t.queue("GET", "/drive/v1/files/boxF0000002/download", status=206,
                headers={"Content-Range": "bytes 0-0/2500"})
        t.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tkt0001"})
        for r in export_responses:
            t.queue_json("GET", "/drive/v1/export_tasks/tkt0001", {"result": r})
        return t

    def test_counts_bytes_and_estimates(self):
        t = self.fixture([{"job_status": 0, "file_size": 300}])
        stats = wizard.scan_stats(make_client(t), sleep=lambda s: None)
        self.assertEqual(stats["items_by_type"], {"file": 2, "docx": 2, "mindnote": 1, "sheet": 1})
        self.assertEqual(stats["upload_bytes"], 3500)
        self.assertEqual(stats["export_est_bytes"], 900)  # docx 2×300 + sheet 1×300
        self.assertIsInstance(stats["est_minutes"], int)
        self.assertGreaterEqual(stats["est_minutes"], 1)
        # 只统计不下载
        self.assertFalse(any(c["path"].endswith("/download") and "export_tasks" in c["path"] for c in t.calls))
        for c in t.calls:
            if c["path"].startswith("/drive/v1/files/") and c["path"].endswith("/download"):
                self.assertEqual(c["headers"].get("Range"), "bytes=0-0")

    def test_polls_until_done(self):
        t = self.fixture([{"job_status": 2}, {"job_status": 0, "file_size": 300}])
        sleeps = []
        stats = wizard.scan_stats(make_client(t), sleep=sleeps.append, export_workers=1)
        self.assertEqual(stats["export_est_bytes"], 900)
        self.assertTrue(sleeps)

    def test_summary_text(self):
        t = self.fixture([{"job_status": 0, "file_size": 300}])
        text = wizard.format_scan(wizard.scan_stats(make_client(t), sleep=lambda s: None))
        self.assertIn("群聊附件", text)
        self.assertIn("GB", text)
        self.assertIn("6", text)


class CredentialsTest(unittest.TestCase):
    def test_ok(self):
        t = FakeTransport()
        t.queue("POST", "/auth/v3/tenant_access_token/internal",
                body={"code": 0, "msg": "ok", "tenant_access_token": "t-fake", "expire": 7200})
        c = wizard.check_credentials(config.Config("cli_fake", "sec_fake"), transport=t)
        self.assertTrue(c.ok)

    def test_bad_secret(self):
        t = FakeTransport()
        t.queue("POST", "/auth/v3/tenant_access_token/internal", body={"code": 10014, "msg": "app secret invalid"})
        c = wizard.check_credentials(config.Config("cli_fake", "sec_bad"), transport=t)
        self.assertFalse(c.ok)
        self.assertIn("凭证与基础信息", c.fix)
        self.assertIn("已启用", c.fix)
        self.assertNotIn("sec_bad", c.detail + c.fix)

    def test_offline(self):
        def boom(*a, **k):
            raise urllib.error.URLError("down")
        with mock.patch("fbk.auth.RETRIES", 0):
            c = wizard.check_credentials(config.Config("cli_fake", "sec_fake"), transport=boom)
        self.assertFalse(c.ok)
        self.assertEqual(c.detail, "连不上飞书")


class EnvTest(unittest.TestCase):
    def test_ok(self):
        checks = wizard.check_env(connect=lambda addr, timeout: mock.MagicMock())
        self.assertTrue(all(c.ok for c in checks))

    def test_offline(self):
        def boom(addr, timeout):
            raise OSError("unreachable")
        net = wizard.check_env(connect=boom)[1]
        self.assertFalse(net.ok)
        self.assertEqual(net.detail, "连不上飞书")


class PromptSecretTest(unittest.TestCase):
    def test_two_dialogs_second_hidden(self):
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            out = "cli_fake\n" if len(calls) == 1 else "sec_fake\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        self.assertEqual(wizard.prompt_secret(runner=fake), ("cli_fake", "sec_fake"))
        self.assertEqual(calls[0][0], "osascript")
        self.assertNotIn("hidden answer", calls[0][-1])
        self.assertIn("with hidden answer", calls[1][-1])

    def test_cancel(self):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="User canceled. (-128)")
        with self.assertRaises(wizard.Cancelled) as cm:
            wizard.prompt_secret(runner=fake)
        self.assertIn("已取消", str(cm.exception))

    def test_dialog_gives_up_after_300s(self):
        calls = []

        def fake(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, stdout="x\n", stderr="")
        wizard.prompt_secret(runner=fake)
        for cmd, kw in calls:
            self.assertIn("giving up after 300", cmd[-1])
            self.assertGreater(kw.get("timeout", 0), 300)

    def test_gave_up_returns_empty(self):
        def fake(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")
        with self.assertRaises(wizard.Cancelled) as cm:
            wizard.prompt_secret(runner=fake)
        self.assertIn("5 分钟", str(cm.exception))

    def test_subprocess_timeout(self):
        def fake(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        with self.assertRaises(wizard.Cancelled) as cm:
            wizard.prompt_secret(runner=fake)
        self.assertIn("已取消", str(cm.exception))

    def test_osascript_missing(self):
        def fake(cmd, **kw):
            raise FileNotFoundError("osascript")
        with self.assertRaises(wizard.Cancelled) as cm:
            wizard.prompt_secret(runner=fake)
        self.assertIn("osascript", str(cm.exception))


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.agents = base / "LaunchAgents"
        for obj, name, val in ((config, "CONFIG_DIR", base / "cfg"), (config, "LOG_DIR", base / "logs")):
            p = mock.patch.object(obj, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(schedule, "default_agents_dir", return_value=self.agents)
        p.start()
        self.addCleanup(p.stop)
        self.base = base

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(list(argv))
        return code, out.getvalue()


class CliStatusTest(CliBase):
    def test_status_json_fresh_machine(self):
        code, out = self.cli("status", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertFalse(d["config"])
        self.assertFalse(d["logged_in"])
        self.assertIsNone(d["last_success"])
        self.assertEqual(d["last_success_text"], "从未成功")
        self.assertFalse(d["schedule_installed"])

    def test_status_json_empty_manifest(self):
        root = self.base / "备份盘"
        root.mkdir()
        config.save_config(config.Config("cli_fake", "sec_fake", backup_root=root))
        Store(root / ".feishu-backup" / "manifest.sqlite").close()
        code, out = self.cli("status", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertTrue(d["config"])
        self.assertEqual(d["backup_root"], str(root))
        self.assertEqual(d["counts"]["items"], {})
        self.assertEqual(d["last_success_text"], "从未成功")
        self.assertNotIn("sec_fake", out)

    def test_status_with_corrupt_token_and_config(self):
        config.CONFIG_DIR.mkdir(parents=True)
        (config.CONFIG_DIR / "token.json").write_text("{bad", encoding="utf-8")
        (config.CONFIG_DIR / "config.json").write_text("{bad", encoding="utf-8")
        code, out = self.cli("status", "--json")
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertFalse(d["logged_in"])
        self.assertFalse(d["config"])
        code, out = self.cli("setup", "--status", "--json")
        self.assertEqual(code, 0)

    def test_status_human_fresh(self):
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("从未成功", out)

    def test_status_with_runs_and_reports(self):
        root = self.base / "备份盘"
        root.mkdir()
        config.save_config(config.Config("cli_fake", "sec_fake", backup_root=root))
        s = Store(root / ".feishu-backup" / "manifest.sqlite")
        s.upsert_item(token="doxA", type="docx", status="ok")
        s.upsert_item(token="doxB", type="docx", status="failed")
        rid = s.start_run()
        s.finish_run(rid, True, {})
        s.close()
        (root / "_报告").mkdir()
        (root / "_报告" / "2026-01-01_备份日志.md").write_text("x")
        (root / "_报告" / "2026-01-02_备份日志.md").write_text("x")
        code, out = self.cli("status", "--json")
        d = json.loads(out)
        self.assertEqual(d["counts"]["items"], {"ok": 1, "failed": 1})
        self.assertIsNotNone(d["last_success"])
        self.assertTrue(d["reports"]["log"].endswith("2026-01-02_备份日志.md"))


class CliSetupTest(CliBase):
    def test_setup_status_offline(self):
        code, out = self.cli("setup", "--status", "--json")
        self.assertEqual(code, 0)
        steps = json.loads(out)["steps"]
        self.assertEqual(len(steps), 7)
        self.assertEqual(steps[0]["state"], "done")  # Python 版本
        self.assertEqual(steps[1]["state"], "todo")

    def test_root_missing_without_create(self):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        p = self.base / "新盘" / "飞书备份"
        code, out = self.cli("setup", "--root", str(p))
        self.assertEqual(code, 1)
        self.assertFalse(p.exists())
        self.assertIn("--create", out)
        self.assertIsNone(config.load_config().backup_root)

    def test_root_create_saves(self):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        p = self.base / "新盘" / "飞书备份"
        code, out = self.cli("setup", "--root", str(p), "--create", "--json")
        self.assertEqual(code, 0, out)
        self.assertTrue(p.is_dir())
        self.assertEqual(config.load_config().backup_root, p.resolve())
        self.assertEqual(config.load_config().app_secret, "sec_fake")

    def test_credentials_saved_only_when_ok(self):
        with mock.patch.object(wizard, "prompt_secret", return_value=("cli_fake", "sec_fake")), \
                mock.patch.object(wizard, "check_credentials",
                                  return_value=wizard.Check("应用凭证", False, "换不到 token", "核对")):
            code, out = self.cli("setup", "--credentials")
        self.assertEqual(code, 1)
        self.assertFalse((config.CONFIG_DIR / "config.json").exists())
        with mock.patch.object(wizard, "prompt_secret", return_value=("cli_fake", "sec_fake")), \
                mock.patch.object(wizard, "check_credentials",
                                  return_value=wizard.Check("应用凭证", True, "能换到 token")):
            code, out = self.cli("setup", "--credentials")
        self.assertEqual(code, 0)
        self.assertEqual(config.load_config().app_secret, "sec_fake")
        self.assertNotIn("sec_fake", out)

    def test_credentials_cancel(self):
        with mock.patch.object(wizard, "prompt_secret", side_effect=wizard.Cancelled("已取消")):
            code, out = self.cli("setup", "--credentials")
        self.assertEqual(code, 1)
        self.assertIn("已取消", out)


class CliRunTest(CliBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(runner, "install_term_handler")
        self.term = p.start()
        self.addCleanup(p.stop)

    def test_run_installs_sigterm_handler(self):
        with mock.patch.object(runner, "run", return_value=0):
            self.cli("run")
        self.term.assert_called_once_with()

    def test_run_passthrough(self):
        with mock.patch.object(runner, "run", return_value=3) as r:
            code, _ = self.cli("run", "--now", "--limit", "5")
        self.assertEqual(code, 3)
        r.assert_called_once_with(force=True, limit=5)

    def test_run_default_respects_due(self):
        with mock.patch.object(runner, "run", return_value=0) as r:
            code, _ = self.cli("run")
        self.assertEqual(code, 0)
        r.assert_called_once_with(force=False, limit=None)

    def test_schedule_uninstall(self):
        with mock.patch.object(schedule, "uninstall") as u:
            code, out = self.cli("schedule", "uninstall", "--json")
        self.assertEqual(code, 0)
        u.assert_called_once()
        self.assertTrue(json.loads(out)["ok"])

    def test_scan_nothing_readable_fails_and_not_saved(self):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        empty = {"items_by_type": {}, "total_items": 0, "upload_count": 0, "upload_bytes": 0,
                 "upload_unknown": 0, "export_est_bytes": 0, "export_by_type": {}, "skipped_types": {},
                 "walk_errors": 2, "est_minutes": 0}
        with mock.patch.object(cli, "_make_client"), mock.patch.object(wizard, "scan_stats", return_value=empty):
            code, out = self.cli("scan")
        self.assertEqual(code, 1)
        self.assertIn("doctor", out)
        self.assertFalse((config.CONFIG_DIR / "scan.json").exists())

    def test_perms_no_report(self):
        code, out = self.cli("perms", "--json")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["report"])


if __name__ == "__main__":
    unittest.main()
