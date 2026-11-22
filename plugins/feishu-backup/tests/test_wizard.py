import contextlib
import importlib.util
import io
import json
import ssl
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
    OK_TLS = staticmethod(lambda host: None)

    def test_ok(self):
        checks = wizard.check_env(connect=lambda addr, timeout: mock.MagicMock(), tls_handshake=self.OK_TLS)
        self.assertTrue(all(c.ok for c in checks))
        self.assertIn("证书", [c.name for c in checks])

    def test_offline(self):
        def boom(addr, timeout):
            raise OSError("unreachable")
        net = wizard.check_env(connect=boom, tls_handshake=self.OK_TLS)[1]
        self.assertFalse(net.ok)
        self.assertEqual(net.detail, "连不上飞书")

    def _bad_cert(self, host):
        raise ssl.SSLCertVerificationError("bad cert")

    def test_cert_error_mac_fix(self):
        c = wizard.check_env(connect=lambda a, timeout: mock.MagicMock(), tls_handshake=self._bad_cert,
                             is_windows=False,
                             executable="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3")[-1]
        self.assertFalse(c.ok)
        self.assertEqual(c.name, "证书")
        self.assertIn("/Applications/Python 3.12/Install Certificates.command", c.fix)

    # 终审 M7：按 Python 的安装方式给修法
    def test_cert_fix_by_install_type(self):
        fx = lambda exe: wizard.cert_fix(exe, is_windows=False)  # noqa: E731
        self.assertIn("/Applications/Python 3.11/Install Certificates.command",
                      fx("/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"))
        for sysexe in ("/usr/bin/python3",
                       "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/bin/python3"):
            f = fx(sysexe)
            self.assertIn("更新 macOS", f)
            self.assertIn("python.org", f)
            self.assertNotIn("Install Certificates.command", f)
        for brew in ("/opt/homebrew/bin/python3", "/usr/local/Cellar/python@3.12/3.12.1/bin/python3"):
            self.assertIn("brew reinstall ca-certificates openssl", fx(brew))
        other = fx("/somewhere/else/python3")
        self.assertIn("Install Certificates.command", other)
        self.assertIn("brew reinstall ca-certificates openssl", other)
        self.assertIn("更新 macOS", other)
        self.assertEqual(wizard.cert_fix("C:\\Python312\\pythonw.exe", is_windows=True), wizard.CERT_FIX_WIN)

    def test_cert_error_windows_fix(self):
        c = wizard.check_env(connect=lambda a, timeout: mock.MagicMock(), tls_handshake=self._bad_cert,
                             is_windows=True)[-1]
        self.assertFalse(c.ok)
        self.assertIn("更新 Python 或检查公司代理证书", c.fix)
        self.assertNotIn("Install Certificates", c.fix)

    def test_old_python_fix_per_os(self):
        # v0.2：修复说明按系统给（mac 用 xcode-select，Windows 用 winget），不再提 brew
        with mock.patch.object(wizard, "MIN_PY", (99, 0)):
            mac = wizard.check_env(connect=lambda a, timeout: mock.MagicMock(), tls_handshake=self.OK_TLS,
                                   is_windows=False)[0]
            win = wizard.check_env(connect=lambda a, timeout: mock.MagicMock(), tls_handshake=self.OK_TLS,
                                   is_windows=True)[0]
        self.assertFalse(mac.ok)
        self.assertIn("xcode-select --install", mac.fix)
        self.assertIn("winget install Python.Python.3.12", win.fix)
        self.assertNotIn("brew", mac.fix + win.fix)

    def test_other_tls_error_keeps_net_mapping(self):
        def reset(host):
            raise ssl.SSLError("handshake failure")
        c = wizard.check_env(connect=lambda a, timeout: mock.MagicMock(), tls_handshake=reset)[-1]
        self.assertFalse(c.ok)
        self.assertEqual(c.detail, "连不上飞书")

    def test_default_tls_uses_injected_context_and_hostname(self):
        calls = {}
        ctx = mock.MagicMock()
        ctx.wrap_socket.side_effect = lambda sock, server_hostname: calls.update(host=server_hostname) or sock
        raw = mock.MagicMock()
        wizard._tls_handshake("open.feishu.cn",
                              connect=lambda addr, timeout: calls.update(addr=addr, timeout=timeout) or raw,
                              context_factory=lambda: ctx)
        self.assertEqual(calls["addr"], ("open.feishu.cn", 443))
        self.assertEqual(calls["timeout"], 10)
        self.assertEqual(calls["host"], "open.feishu.cn")

    def test_default_tls_cert_error_propagates(self):
        ctx = mock.MagicMock()
        ctx.wrap_socket.side_effect = ssl.SSLCertVerificationError("x")
        with self.assertRaises(ssl.SSLCertVerificationError):
            wizard._tls_handshake("open.feishu.cn", connect=lambda a, timeout: mock.MagicMock(),
                                  context_factory=lambda: ctx)


class CloudAndPathWarnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.od = self.base / "OneDrive"
        (self.od / "备份").mkdir(parents=True)
        (self.base / "local").mkdir()

    def run_root(self, p, env, win=True):
        return wizard.check_root(p, 0, home=self.base / "h", is_windows=win, env=env)

    def names(self, checks):
        return {c.name: c for c in checks}

    def test_onedrive_warns_on_windows(self):
        c = self.names(self.run_root(self.od / "备份", {"OneDrive": str(self.od)}))["OneDrive 同步"]
        self.assertTrue(c.ok and c.warn)

    def test_onedrive_commercial_and_case_insensitive(self):
        env = {"OneDriveCommercial": str(self.od).upper()}
        self.assertIn("OneDrive 同步", self.names(self.run_root(self.od / "备份", env)))

    def test_onedrive_consumer_var(self):
        self.assertIn("OneDrive 同步", self.names(self.run_root(self.od / "备份", {"OneDriveConsumer": str(self.od)})))

    def test_not_under_onedrive_no_warning(self):
        self.assertNotIn("OneDrive 同步", self.names(self.run_root(self.base / "local", {"OneDrive": str(self.od)})))

    def test_sibling_prefix_is_not_under_onedrive(self):
        sib = self.base / "OneDrive2"
        sib.mkdir()
        self.assertNotIn("OneDrive 同步", self.names(self.run_root(sib, {"OneDrive": str(self.od)})))

    def test_onedrive_ignored_off_windows(self):
        self.assertNotIn("OneDrive 同步", self.names(self.run_root(self.od / "备份", {"OneDrive": str(self.od)}, win=False)))

    def test_long_path_warns_only_on_windows(self):
        deep = self.base / ("很长的目录名" * 10) / ("b" * 20)
        deep.mkdir(parents=True)
        self.assertGreater(len(str(deep)), 100)
        w = self.names(self.run_root(deep, {}))["路径长度"]
        self.assertTrue(w.ok and w.warn)
        self.assertIn("D:\\飞书备份", w.fix)
        self.assertNotIn("路径长度", self.names(self.run_root(deep, {}, win=False)))

    def test_short_path_no_length_warning(self):
        self.assertNotIn("路径长度", self.names(self.run_root(self.base / "local", {})))


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
        self.app = base / "AppSupport" / "app"
        p = mock.patch.object(schedule, "default_app_root", return_value=self.app)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(schedule, "IS_WINDOWS", False)  # CLI 测试走 mac 分支，不碰 schtasks
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(schedule, "tls_probe", return_value=None)  # 不联网
        self.tls_probe = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(schedule, "pick_python", return_value="/usr/bin/python3")
        p.start()
        self.addCleanup(p.stop)
        self.base = base

    def broken_schedule(self):
        """装一个指向已不存在的程序副本的定时任务（模拟插件更新后缓存被清掉）。"""
        self.agents.mkdir(parents=True, exist_ok=True)
        (self.agents / "local.feishu-backup.plist").write_bytes(
            schedule.plist_xml(sys.executable, str(self.base / "cache" / "0.0.9" / "fbk.py")))

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
        (root / "_报告" / "2026-01-02_0900_备份日志.md").write_text("x")
        (root / "_报告" / "2026-01-02_1430_备份日志.md").write_text("x")
        code, out = self.cli("status", "--json")
        d = json.loads(out)
        self.assertEqual(d["counts"]["items"], {"ok": 1, "failed": 1})
        self.assertIsNotNone(d["last_success"])
        self.assertTrue(d["reports"]["log"].endswith("2026-01-02_1430_备份日志.md"))


class CliScheduleVerifyTest(CliBase):
    """终审 I1：status / doctor 发现定时任务指向的程序不在了或版本不一致，给出 ✗ 和修法。"""

    def test_status_reports_broken_schedule(self):
        self.broken_schedule()
        code, out = self.cli("status", "--json")
        d = json.loads(out)
        self.assertTrue(d["schedule_installed"])
        self.assertTrue(d["schedule_problems"])
        code, out = self.cli("status")
        self.assertIn("✗", out)
        self.assertIn("重新运行 setup --schedule", out)

    def test_doctor_reports_broken_schedule(self):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        self.broken_schedule()
        with mock.patch.object(cli, "_make_client"), mock.patch.object(wizard, "doctor", return_value=[]):
            code, out = self.cli("doctor", "--json")
        self.assertEqual(code, 1)
        checks = {c["name"]: c for c in json.loads(out)["checks"]}
        self.assertFalse(checks["定时任务"]["ok"])
        self.assertIn("setup --schedule", checks["定时任务"]["fix"])

    def test_doctor_without_schedule_has_no_schedule_check(self):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        with mock.patch.object(cli, "_make_client"), mock.patch.object(wizard, "doctor", return_value=[]):
            code, out = self.cli("doctor", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["checks"], [])


class CliWindowsNoticeTest(CliBase):
    NOTICE = "⚠ Windows 版未经真机验证，遇到问题请对照 WINDOWS_CHECKLIST.md"

    def _doctor(self, win, *extra):
        config.save_config(config.Config("cli_fake", "sec_fake"))
        with mock.patch.object(cli, "_make_client"), mock.patch.object(wizard, "doctor", return_value=[]), \
                mock.patch("fbk.platform.IS_WINDOWS", win):
            return self.cli("doctor", *extra)

    def test_doctor_first_line_on_windows(self):
        code, out = self._doctor(True)
        self.assertEqual(out.splitlines()[0], self.NOTICE)

    def test_doctor_no_notice_on_mac(self):
        code, out = self._doctor(False)
        self.assertNotIn("Windows 版未经真机验证", out)

    def test_doctor_json_flag(self):
        code, out = self._doctor(True, "--json")
        self.assertIs(json.loads(out)["windows_unverified"], True)
        code, out = self._doctor(False, "--json")
        self.assertNotIn("windows_unverified", json.loads(out))

    def test_status_text_and_json(self):
        with mock.patch("fbk.platform.IS_WINDOWS", True):
            code, out = self.cli("status")
            self.assertEqual(out.splitlines()[0], self.NOTICE)
            code, out = self.cli("status", "--json")
            self.assertIs(json.loads(out)["windows_unverified"], True)
        code, out = self.cli("status", "--json")
        self.assertNotIn("windows_unverified", json.loads(out))

    def test_doctor_without_credentials_still_marked(self):
        with mock.patch("fbk.platform.IS_WINDOWS", True):
            code, out = self.cli("doctor")
        self.assertEqual(out.splitlines()[0], self.NOTICE)


class CliSetupTest(CliBase):
    def test_setup_status_offline(self):
        code, out = self.cli("setup", "--status", "--json")
        self.assertEqual(code, 0)
        steps = json.loads(out)["steps"]
        self.assertEqual(len(steps), 7)
        self.assertEqual(steps[0]["state"], "done")  # Python 版本
        self.assertEqual(steps[1]["state"], "todo")

    def test_setup_status_schedule_with_problems_is_failed(self):
        # 终审 M1：定时任务装了但跑不了，第 7 步不能显示 ✓
        with mock.patch.object(schedule, "verify", return_value=["找不到 Python：/x/python3"]):
            steps = json.loads(self.cli("setup", "--status", "--json")[1])["steps"]
            _, text = self.cli("setup", "--status")
        self.assertEqual(steps[6]["state"], "failed")
        self.assertIn("找不到 Python：/x/python3", steps[6]["detail"])
        self.assertIn("setup --schedule", steps[6]["detail"])
        self.assertIn("✗", text.splitlines()[6])
        with mock.patch.object(schedule, "verify", return_value=[]):
            steps = json.loads(self.cli("setup", "--status", "--json")[1])["steps"]
        self.assertEqual(steps[6]["state"], "done")

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
        r.assert_called_once_with(force=True, limit=5, only="")

    def test_run_default_respects_due(self):
        with mock.patch.object(runner, "run", return_value=0) as r:
            code, _ = self.cli("run")
        self.assertEqual(code, 0)
        r.assert_called_once_with(force=False, limit=None, only="")

    def _cfg(self):
        config.save_config(config.Config("cli_fake", "sec_fake", backup_root=Path(self.tmp.name)))

    def test_setup_schedule_saves_weekday_time(self):
        self._cfg()
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")):
            code, out = self.cli("setup", "--schedule", "--weekday", "5", "--time", "20:00")
        self.assertEqual(code, 0)
        self.assertIn("每周五 20:00", out)
        got = config.load_config()
        self.assertEqual((got.schedule_weekday, got.schedule_time), (5, "20:00"))

    def test_setup_schedule_keeps_existing_values(self):
        self._cfg()
        c = config.load_config()
        c.schedule_weekday, c.schedule_time = 3, "08:30"
        config.save_config(c)
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")):
            code, out = self.cli("setup", "--schedule")
        self.assertEqual(code, 0)
        self.assertIn("每周三 08:30", out)
        self.assertEqual(config.load_config().schedule_time, "08:30")

    def test_credentials_keeps_schedule(self):
        self._cfg()
        c = config.load_config()
        c.schedule_weekday, c.schedule_time = 5, "20:00"
        config.save_config(c)
        ok = wizard.Check("应用凭证", True, "ok")
        with mock.patch.object(wizard, "prompt_secret", return_value=("cli_new", "sec_new")), \
                mock.patch.object(wizard, "check_credentials", return_value=ok):
            code, _ = self.cli("setup", "--credentials")
        self.assertEqual(code, 0)
        got = config.load_config()
        self.assertEqual((got.app_id, got.schedule_weekday, got.schedule_time), ("cli_new", 5, "20:00"))
        self.assertEqual(got.backup_root, Path(self.tmp.name))

    def test_status_with_bad_schedule_does_not_crash(self):
        self._cfg()
        path = config.CONFIG_DIR / "config.json"
        d = json.loads(path.read_text())
        d["schedule_time"] = "7:5"
        path.write_text(json.dumps(d))
        code, out = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn("计划时间有问题", out)
        self.assertIn("HH:MM", out)
        self.assertEqual(json.loads(self.cli("status", "--json")[1])["schedule_problem"].count("HH:MM"), 1)

    def test_setup_schedule_defaults_first_time(self):
        self._cfg()
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")):
            _, out = self.cli("setup", "--schedule")
        self.assertIn("每周一 12:00", out)

    def test_setup_schedule_bad_input_exit_1(self):
        self._cfg()
        with mock.patch.object(schedule, "install") as inst:
            for argv in (("--weekday", "8"), ("--weekday", "0"), ("--time", "25:00"), ("--time", "ab"),
                         ("--weekday", "abc"), ("--weekday", "周一"), ("--weekday", "1.5")):
                code, out = self.cli("setup", "--schedule", *argv)
                self.assertEqual(code, 1, argv)
                self.assertIn("✗", out, argv)
                self.assertNotIn("invalid", out, argv)
                if argv[0] == "--weekday":
                    self.assertIn("1–7", out, argv)
        inst.assert_not_called()
        self.assertEqual(config.load_config().schedule_weekday, 1)

    def test_status_shows_next_plan(self):
        self._cfg()
        c = config.load_config()
        c.schedule_weekday, c.schedule_time = 1, "12:00"
        config.save_config(c)
        code, out = self.cli("status")
        self.assertIn("下次计划：每周一 12:00（", out)
        d = json.loads(self.cli("status", "--json")[1])
        self.assertRegex(d["next_run_text"], r"^\d{4}-\d{2}-\d{2} 12:00$")

    def test_setup_schedule_cert_failure_does_not_install(self):
        self._cfg()
        self.tls_probe.return_value = "cert"
        with mock.patch.object(schedule, "install") as inst:
            code, out = self.cli("setup", "--schedule")
        self.assertEqual(code, 1)
        inst.assert_not_called()
        self.assertIn("✗", out)
        self.assertIn("证书", out)
        self.assertIn("/usr/bin/python3", out)
        self.tls_probe.assert_called_once_with("/usr/bin/python3")
        self.assertIsNone(config.load_config().schedule_installed_at)

    def test_setup_schedule_probe_failure_keeps_old_plan(self):
        # 再审 4：试连失败时新的 --weekday / --time 不能先存进配置
        self._cfg()
        self.tls_probe.return_value = "cert"
        with mock.patch.object(schedule, "install") as inst:
            code, out = self.cli("setup", "--schedule", "--weekday", "5", "--time", "20:00")
        self.assertEqual(code, 1)
        inst.assert_not_called()
        got = config.load_config()
        self.assertEqual((got.schedule_weekday, got.schedule_time), (1, "12:00"))
        self.assertIn("计划时间没有改动", out)

    def test_setup_schedule_install_failure_keeps_old_plan(self):
        self._cfg()
        with mock.patch.object(schedule, "install", side_effect=RuntimeError("launchctl bootstrap 失败")):
            code, out = self.cli("setup", "--schedule", "--weekday", "5", "--time", "20:00")
        self.assertEqual(code, 1)
        got = config.load_config()
        self.assertEqual((got.schedule_weekday, got.schedule_time), (1, "12:00"))
        self.assertIn("计划时间没有改动", out)

    def test_setup_schedule_network_failure_does_not_install(self):
        self._cfg()
        self.tls_probe.return_value = "network"
        with mock.patch.object(schedule, "install") as inst:
            code, out = self.cli("setup", "--schedule", "--json")
        self.assertEqual(code, 1)
        inst.assert_not_called()

    def test_setup_schedule_uses_probed_python_and_records_install_time(self):
        self._cfg()
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")) as inst:
            code, _ = self.cli("setup", "--schedule")
        self.assertEqual(code, 0)
        self.assertEqual(inst.call_args.kwargs.get("python"), "/usr/bin/python3")
        first = config.load_config().schedule_installed_at
        self.assertIsInstance(first, int)
        c = config.load_config()
        c.schedule_installed_at = 1000
        config.save_config(c)
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")):
            self.cli("setup", "--schedule", "--time", "09:00")
        self.assertEqual(config.load_config().schedule_installed_at, 1000)  # 改时间不重置

    def test_setup_schedule_corrupt_config_says_so(self):
        # 终审 M4：配置文件坏了不能说成「还没设置备份目录」
        self._cfg()
        (config.CONFIG_DIR / "config.json").write_text("{not json", encoding="utf-8")
        with mock.patch.object(schedule, "install") as inst:
            code, out = self.cli("setup", "--schedule")
        self.assertEqual(code, 1)
        inst.assert_not_called()
        self.assertIn("配置文件损坏", out)
        self.assertNotIn("还没设置备份目录", out)
        self.assertIn("setup --credentials", out)
        self.assertIn("setup --root", out)

    def test_setup_schedule_time_hint(self):
        self._cfg()
        with mock.patch.object(schedule, "install", return_value=Path("/x/p.plist")):
            _, out = self.cli("setup", "--schedule")
        self.assertIn("每周一 12:00（错过会在开机后补跑）", out)

    def test_setup_schedule_copies_app_then_installs(self):
        self._cfg()
        from fbk.platform import launchd
        with mock.patch.object(launchd, "install", return_value=self.agents / "p.plist") as inst, \
                mock.patch.object(schedule, "pick_python", return_value="/usr/bin/python3"):
            code, out = self.cli("setup", "--schedule", "--json")
        self.assertEqual(code, 0, out)
        launcher = self.app / "launch.py"
        self.assertTrue(launcher.is_file())
        self.assertTrue((self.app / "versions" / schedule.__version__ / "fbk.py").is_file())
        self.assertEqual(inst.call_args[0][:2], ("/usr/bin/python3", str(launcher)))
        self.assertEqual(inst.call_args[1]["agents_dir"], self.agents)

    def test_install_json_copies_app_without_schedule(self):
        with mock.patch.object(schedule, "install") as sched:
            code, out = self.cli("install", "--json")
        self.assertEqual(code, 0, out)
        sched.assert_not_called()
        d = json.loads(out)
        self.assertTrue(d["ok"])
        self.assertEqual(d["launcher"], str(self.app / "launch.py"))
        self.assertEqual(d["app_dir"], str(self.app / "versions" / schedule.__version__))
        self.assertEqual(d["version"], schedule.__version__)
        self.assertTrue(Path(d["launcher"]).is_file())
        self.assertFalse(self.agents.exists())
        code, out = self.cli("install")
        self.assertEqual(code, 0)
        self.assertIn(str(self.app / "launch.py"), out)
        self.assertIn("setup --schedule", out)

    def test_schedule_uninstall(self):
        with mock.patch.object(schedule, "uninstall") as u:
            code, out = self.cli("schedule", "uninstall", "--json")
        self.assertEqual(code, 0)
        u.assert_called_once()
        self.assertTrue(json.loads(out)["ok"])

    def test_schedule_uninstall_failure_is_chinese_exit_1(self):
        # 终审 M2：Windows 上 schtasks 删不掉时不能抛英文异常
        with mock.patch.object(schedule, "uninstall", side_effect=RuntimeError("从任务计划程序删除任务失败（schtasks 返回 1）：拒绝访问")):
            code, out = self.cli("schedule", "uninstall")
            jcode, jout = self.cli("schedule", "uninstall", "--json")
        self.assertEqual((code, jcode), (1, 1))
        self.assertIn("✗", out)
        self.assertIn("拒绝访问", out)
        self.assertIn("任务计划程序", out)
        self.assertFalse(json.loads(jout)["ok"])
        with mock.patch.object(schedule, "uninstall", side_effect=RuntimeError("x")), \
                mock.patch.object(schedule, "IS_WINDOWS", True):
            code, out = self.cli("schedule", "uninstall")
        self.assertEqual(code, 1)
        self.assertIn("任务计划程序库", out)

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



class CliOnlyTest(CliBase):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(runner, "install_term_handler")
        p.start()
        self.addCleanup(p.stop)

    def test_run_only_passthrough(self):
        with mock.patch.object(runner, "run", return_value=0) as r:
            self.cli("run", "--only", "产品部/质量文件")
        r.assert_called_once_with(force=False, limit=None, only="产品部/质量文件")

    def test_run_lines_state_cutoff_time(self):
        text = cli._run_lines({"started": "2026-11-17 22:40:00", "finished": "2026-11-17 22:41:05", "ok": True,
                               "summary": {"新增": 1, "未变化": 290}, "log": None})
        self.assertIn("只代表 2026-11-17 22:41:05 之前飞书上的状态", text)
        self.assertIn("新增 1，未变化 290", text)

    def test_run_lines_only_not_found(self):
        text = cli._run_lines({"started": "a", "finished": "b", "ok": False,
                               "summary": {"only": "X", "only_items": 0}, "log": None})
        self.assertIn("✗ 没有找到「X」", text)
        self.assertNotIn("only", text.split("范围")[0])  # 内部键不进结果统计


if __name__ == "__main__":
    unittest.main()


class AiAgnosticTextTest(unittest.TestCase):
    """v0.2：使用者可能用任何 AI 助手，面向用户的提示里不写死 Claude。"""

    def test_no_claude_in_user_facing_code(self):
        base = Path(wizard.__file__).resolve().parent
        files = list(base.rglob("*.py")) + [base.parent / "fbk.py"]
        hits = [str(f) for f in files if "Claude" in f.read_text(encoding="utf-8")]
        self.assertEqual(hits, [])


class WindowsStdioTest(unittest.TestCase):
    """中文 Windows 上输出被 AI 工具接走（管道）时，Python 默认按 GBK 编码，打印 ✓ 会直接报错退出。"""

    def _pipe(self, enc="cp936"):
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding=enc)

    def test_gbk_pipe_switched_to_utf8_on_windows(self):
        raw, out = self._pipe()
        cli._utf8_stdio([out], is_windows=True, env={})
        print("✓ 列我的空间", file=out)
        out.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "✓ 列我的空间\n")

    def test_respects_pythonioencoding(self):
        _raw, out = self._pipe()
        cli._utf8_stdio([out], is_windows=True, env={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(out.encoding, "cp936")

    def test_mac_untouched(self):
        _raw, out = self._pipe()
        cli._utf8_stdio([out], is_windows=False, env={})
        self.assertEqual(out.encoding, "cp936")

    def test_none_stream_ok(self):
        cli._utf8_stdio([None], is_windows=True, env={})  # pythonw.exe 下 sys.stdout 是 None
