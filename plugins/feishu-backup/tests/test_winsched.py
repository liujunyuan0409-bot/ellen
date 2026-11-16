"""platform.winsched：任务计划程序 XML 生成、schtasks 安装/卸载/校验。runner 全部注入，不碰真 schtasks。"""
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import __version__  # noqa: E402
from fbk.platform import appcopy, winsched  # noqa: E402

NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
PYW = r"C:\Python312\pythonw.exe"
LAUNCHER = r"C:\Users\fake user\AppData\Local\feishu-backup\app\launch.py"
USER = r"FAKEPC\fakeuser"
SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"


def _parse(xml: str):
    return ET.fromstring(winsched.strip_declaration(xml))


def _text(root, path):
    el = root.find(path, NS)
    return None if el is None else el.text


class TaskXmlTest(unittest.TestCase):
    def setUp(self):
        self.xml = winsched.task_xml(PYW, LAUNCHER, USER, today=date(2026, 11, 16))
        self.root = _parse(self.xml)

    def test_declaration_and_root(self):
        self.assertTrue(self.xml.startswith('<?xml version="1.0" encoding="UTF-16"?>'))
        self.assertEqual(self.root.tag, "{%s}Task" % NS["t"])
        self.assertEqual(self.root.get("version"), "1.2")

    def test_triggers(self):
        self.assertEqual(_text(self.root, "t:Triggers/t:LogonTrigger/t:UserId"), USER)
        self.assertEqual(_text(self.root, "t:Triggers/t:LogonTrigger/t:Enabled"), "true")
        tt = "t:Triggers/t:TimeTrigger/"
        self.assertEqual(_text(self.root, tt + "t:StartBoundary"), "2026-11-16T00:00:00")
        self.assertEqual(_text(self.root, tt + "t:Repetition/t:Interval"), "PT1H")
        self.assertEqual(_text(self.root, tt + "t:Repetition/t:StopAtDurationEnd"), "false")
        self.assertIsNone(self.root.find(tt + "t:Repetition/t:Duration", NS))  # 不写 Duration = 无限重复
        self.assertEqual(_text(self.root, tt + "t:Enabled"), "true")

    def test_settings(self):
        s = "t:Settings/t:"
        self.assertEqual(_text(self.root, s + "StartWhenAvailable"), "true")
        self.assertEqual(_text(self.root, s + "DisallowStartIfOnBatteries"), "false")
        self.assertEqual(_text(self.root, s + "StopIfGoingOnBatteries"), "false")
        self.assertEqual(_text(self.root, s + "MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(_text(self.root, s + "ExecutionTimeLimit"), "PT0S")

    def test_principal(self):
        p = "t:Principals/t:Principal/t:"
        self.assertEqual(_text(self.root, p + "LogonType"), "InteractiveToken")
        self.assertEqual(_text(self.root, p + "RunLevel"), "LeastPrivilege")
        self.assertEqual(_text(self.root, p + "UserId"), USER)

    def test_action(self):
        self.assertEqual(_text(self.root, "t:Actions/t:Exec/t:Command"), PYW)
        self.assertEqual(_text(self.root, "t:Actions/t:Exec/t:Arguments"), '"%s" run' % LAUNCHER)
        self.assertEqual(winsched.parse_exec(self.xml), (PYW, LAUNCHER))

    def test_escapes_special_characters(self):
        xml = winsched.task_xml(PYW, r"C:\a&b<c>\launch.py", USER, today=date(2026, 1, 1))
        self.assertEqual(winsched.parse_exec(xml)[1], r"C:\a&b<c>\launch.py")

    def test_default_user(self):
        self.assertEqual(winsched.default_user({"USERDOMAIN": "FAKEPC", "USERNAME": "fakeuser"}), USER)
        self.assertEqual(winsched.default_user({"USERNAME": "fakeuser"}), "fakeuser")

    def test_default_user_falls_back_to_getuser_or_raises(self):
        self.assertEqual(winsched.default_user({"USERDOMAIN": "FAKEPC", "USERNAME": ""},
                                               getuser=lambda: "fakeuser"), USER)
        self.assertEqual(winsched.default_user({}, getuser=lambda: "fakeuser"), "fakeuser")

        def boom():
            raise OSError("no user")
        for g in (boom, lambda: ""):
            with self.assertRaises(RuntimeError) as cm:
                winsched.default_user({"USERDOMAIN": "FAKEPC"}, getuser=g)
            self.assertIn("用户名", str(cm.exception))


class Runner:
    def __init__(self, rc=0, stdout=b"", stderr=b""):
        self.calls, self.files, self.rc, self.stdout, self.stderr = [], [], rc, stdout, stderr

    def __call__(self, args, **kw):
        args = list(args)
        self.calls.append(args)
        if "/XML" in args and "/Create" in args:
            self.files.append(Path(args[args.index("/XML") + 1]).read_bytes())
        rc = self.rc(args) if callable(self.rc) else self.rc
        out = self.stdout(args) if callable(self.stdout) else self.stdout
        return subprocess.CompletedProcess(args, rc, out, self.stderr)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.xml = winsched.task_xml(PYW, LAUNCHER, USER, today=date(2026, 11, 16))

    def test_install_runs_schtasks_create_with_utf16_file(self):
        r = Runner()
        winsched.install(self.xml, runner=r, tmpdir=self.tmp.name)
        self.assertEqual(len(r.calls), 1)
        args = r.calls[0]
        self.assertEqual(args[:4], ["schtasks", "/Create", "/TN", "feishu-backup"])
        self.assertEqual(args[4], "/XML")
        self.assertEqual(args[6:], ["/F"])
        raw = r.files[0]
        self.assertTrue(raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"))  # UTF-16 带 BOM
        self.assertEqual(raw.decode("utf-16"), self.xml)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])  # 临时文件删掉了

    def test_install_failure_raises_chinese_with_stderr(self):
        r = Runner(rc=1, stderr="ERROR: Access is denied.".encode())
        with self.assertRaises(RuntimeError) as cm:
            winsched.install(self.xml, runner=r, tmpdir=self.tmp.name)
        self.assertIn("任务计划程序", str(cm.exception))
        self.assertIn("Access is denied", str(cm.exception))
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_uninstall_deletes_when_present(self):
        r = Runner()
        winsched.uninstall(runner=r)
        self.assertEqual(r.calls, [["schtasks", "/Query", "/TN", "feishu-backup"],
                                   ["schtasks", "/Delete", "/TN", "feishu-backup", "/F"]])

    def test_uninstall_absent_is_noop(self):
        for msg in ("ERROR: The system cannot find the file specified.", "错误: 系统找不到指定的文件。"):
            r = Runner(rc=1, stderr=msg.encode("utf-8"))
            winsched.uninstall(runner=r)
            self.assertEqual(r.calls, [["schtasks", "/Query", "/TN", "feishu-backup"]])

    def test_uninstall_other_query_failure_still_deletes(self):
        r = Runner(rc=lambda a: 1 if "/Query" in a else 0, stderr=b"ERROR: Access is denied.")
        winsched.uninstall(runner=r)
        self.assertEqual(r.calls[-1], ["schtasks", "/Delete", "/TN", "feishu-backup", "/F"])
        r = Runner(rc=1, stderr=b"ERROR: Access is denied.")
        with self.assertRaises(RuntimeError) as cm:
            winsched.uninstall(runner=r)
        self.assertIn("删除任务失败", str(cm.exception))
        self.assertIn("Access is denied", str(cm.exception))

    def test_uninstall_delete_failure_raises(self):
        r = Runner(rc=lambda a: 1 if "/Delete" in a else 0, stderr=b"denied")
        with self.assertRaises(RuntimeError) as cm:
            winsched.uninstall(runner=r)
        self.assertIn("denied", str(cm.exception))


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "app"
        self.launcher = appcopy.install(SCRIPTS, self.root, __version__)
        self.pyw = base / "pythonw.exe"
        self.pyw.write_bytes(b"")

    def _query(self, pyw, launcher):
        xml = winsched.task_xml(str(pyw), str(launcher), USER, today=date(2026, 11, 16))
        return Runner(stdout=xml.encode("utf-16"))

    def test_not_installed(self):
        self.assertIsNone(winsched.verify(runner=Runner(rc=1)))

    def test_ok(self):
        r = self._query(self.pyw, self.launcher)
        self.assertEqual(winsched.verify(runner=r), [])
        self.assertEqual(r.calls, [["schtasks", "/Query", "/TN", "feishu-backup", "/XML"]])

    def test_query_output_without_bom(self):
        xml = winsched.task_xml(str(self.pyw), str(self.launcher), USER, today=date(2026, 11, 16))
        self.assertEqual(winsched.verify(runner=Runner(stdout=xml.encode("utf-8"))), [])

    def test_missing_python_and_launcher(self):
        probs = winsched.verify(runner=self._query(self.pyw.with_name("gone.exe"), self.launcher))
        self.assertTrue(any("gone.exe" in p for p in probs), probs)
        self.launcher.unlink()
        probs = winsched.verify(runner=self._query(self.pyw, self.launcher))
        self.assertTrue(any("launch.py" in p for p in probs), probs)

    def test_version_mismatch(self):
        (self.root / "current").write_text("0.0.9")
        probs = winsched.verify(runner=self._query(self.pyw, self.launcher))
        self.assertTrue(any("0.0.9" in p for p in probs), probs)

    def test_unreadable_xml(self):
        probs = winsched.verify(runner=Runner(stdout=b"garbage"))
        self.assertTrue(probs and "读不了" in probs[0], probs)

    def test_old_style_task_without_launcher(self):
        r = self._query(self.pyw, self.root / "fbk.py")
        probs = winsched.verify(runner=r)
        self.assertTrue(probs)


if __name__ == "__main__":
    unittest.main()
