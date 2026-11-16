import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import config, schedule  # noqa: E402
from fbk.platform import launchd, winsched  # noqa: E402
from fbk.schedule import (AlreadyRunning, RunLock, is_due, is_stale, last_slot, next_slot,  # noqa: E402
                          parse_time, plist_xml, RETRY_AFTER_INCOMPLETE, weekday_name)

H = 3600
NOW = 1_800_000_000


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


TZ = datetime.now().astimezone().tzinfo


def _ts(*a):
    return int(datetime(*a, tzinfo=TZ).timestamp())


class SlotTest(unittest.TestCase):
    def test_last_slot_boundaries(self):
        mon_noon = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)  # 周一
        self.assertEqual(last_slot(mon_noon, 1, 12, 0), mon_noon)
        self.assertEqual(last_slot(mon_noon - timedelta(seconds=1), 1, 12, 0), mon_noon - timedelta(days=7))
        self.assertEqual(last_slot(datetime(2027, 1, 1, 9, 0, tzinfo=TZ), 1, 12, 0),
                         datetime(2026, 12, 28, 12, 0, tzinfo=TZ))  # 跨年
        self.assertIsNotNone(last_slot(mon_noon, 1, 12, 0).tzinfo)

    def test_next_slot_strictly_after(self):
        mon_noon = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)
        self.assertEqual(next_slot(mon_noon, 1, 12, 0), mon_noon + timedelta(days=7))
        self.assertEqual(next_slot(mon_noon - timedelta(seconds=1), 1, 12, 0), mon_noon)
        self.assertEqual(next_slot(datetime(2026, 11, 18, 3, 0, tzinfo=TZ), 5, 20, 0),
                         datetime(2026, 11, 20, 20, 0, tzinfo=TZ))

    def test_parse_time(self):
        self.assertEqual(parse_time("12:00"), (12, 0))
        self.assertEqual(parse_time("7:05"), (7, 5))

    def test_parse_time_rejects_bad(self):
        for bad in ("25:00", "12", "ab", "12:60", ""):
            with self.assertRaises(ValueError) as cm:
                parse_time(bad)
            self.assertIn("HH:MM", str(cm.exception))

    def test_weekday_name(self):
        self.assertEqual([weekday_name(i) for i in (1, 5, 7)], ["周一", "周五", "周日"])


@unittest.skipUnless(hasattr(__import__("time"), "tzset"), "需要 time.tzset 切换本机时区")
class FixedOffsetTzTest(unittest.TestCase):
    """终审 TZ：本机时区不是 +8（固定 UTC-5，无夏令时）时，计划时间按本机墙上时间算。"""

    def setUp(self):
        import time as _time
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            _time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "UTC+5"  # POSIX 写法：本地 = UTC − 5 小时
        _time.tzset()

    def test_slots_in_local_minus_5(self):
        from datetime import timezone
        utc = timezone.utc
        mon_1300_local = datetime(2026, 11, 16, 18, 0, tzinfo=utc)  # 周一 13:00（UTC-5）
        self.assertEqual(last_slot(mon_1300_local, 1, 12, 0), datetime(2026, 11, 16, 17, 0, tzinfo=utc))
        mon_1130_local = datetime(2026, 11, 16, 16, 30, tzinfo=utc)  # 周一 11:30（UTC-5），还没到 12:00
        self.assertEqual(last_slot(mon_1130_local, 1, 12, 0), datetime(2026, 11, 9, 17, 0, tzinfo=utc))
        self.assertEqual(next_slot(mon_1130_local, 1, 12, 0), datetime(2026, 11, 16, 17, 0, tzinfo=utc))
        self.assertEqual(last_slot(mon_1300_local, 1, 12, 0).utcoffset(), timedelta(hours=-5))
        # 北京时间已经是周二凌晨，但本机还是周一：按本机算
        tue_0100_bj = datetime(2026, 11, 16, 17, 30, tzinfo=utc)
        self.assertTrue(is_due(int(mon_1130_local.timestamp()), int(tue_0100_bj.timestamp()), 1, 12, 0))
        self.assertFalse(is_due(int(datetime(2026, 11, 16, 17, 5, tzinfo=utc).timestamp()),
                                int(tue_0100_bj.timestamp()), 1, 12, 0))


class IsDueTest(unittest.TestCase):
    def test_never_succeeded_is_due(self):
        self.assertTrue(is_due(None, _ts(2026, 11, 18, 9), 1, 12, 0))

    def test_is_due_once_per_week(self):
        ok = _ts(2026, 11, 16, 12, 5)  # 周一 12:05 成功
        for now in (_ts(2026, 11, 16, 12, 6), _ts(2026, 11, 19, 8), _ts(2026, 11, 23, 11, 59)):
            self.assertFalse(is_due(ok, now, 1, 12, 0))
        self.assertTrue(is_due(ok, _ts(2026, 11, 23, 12, 0), 1, 12, 0))

    def test_missed_monday_catches_up(self):
        ok = _ts(2026, 11, 9, 12, 5)  # 上周一成功，本周三才开机
        self.assertTrue(is_due(ok, _ts(2026, 11, 18, 9, 0), 1, 12, 0))

    def test_stale_after_two_missed_slots(self):
        ok = _ts(2026, 11, 9, 12, 5)
        self.assertFalse(is_stale(None, _ts(2026, 12, 1), 1, 12, 0))
        self.assertFalse(is_stale(ok, _ts(2026, 11, 22, 9), 1, 12, 0))   # 只错过 11-16
        self.assertFalse(is_stale(ok, _ts(2026, 11, 23, 11, 59), 1, 12, 0))
        self.assertTrue(is_stale(ok, _ts(2026, 11, 23, 12, 0), 1, 12, 0))  # 往前第二个点 11-16 已晚于 ok

    def test_retry_constant(self):
        self.assertEqual(RETRY_AFTER_INCOMPLETE, 6 * 3600)


_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.write(fd, b"%d\\n" % os.getpid())
print("ready", flush=True)
sys.stdin.read()
"""


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / ".feishu-backup" / "run.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def hold_in_other_process(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(self.lock_path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.readline().strip(), b"ready")

        def stop():
            p.stdin.close()
            p.wait(5)
            p.stdout.close()
        self.addCleanup(stop)
        return p

    def test_writes_pid_and_releases(self):
        with RunLock(self.root):
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))
        with RunLock(self.root):  # 释放后可以再次取得
            pass

    def test_flock_held_by_other_process_raises(self):
        p = self.hold_in_other_process()
        with self.assertRaises(AlreadyRunning):
            with RunLock(self.root):
                self.fail("不应进入")
        self.assertEqual(self.lock_path.read_text().strip(), str(p.pid))  # 别人的锁不动

    def test_stale_pid_1_without_flock_is_taken_over(self):
        # 终审 C2：关机时被 SIGTERM 杀掉留下的锁文件，PID 被系统进程复用也不能挡住备份
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text("1\n")
        with RunLock(self.root):
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))

    def test_dead_pid_taken_over(self):
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text(str(_dead_pid()))
        with RunLock(self.root):
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))

    def test_garbage_lock_taken_over(self):
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text("not-a-pid")
        with RunLock(self.root):
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))

    def test_lock_released_when_holder_killed(self):
        p = self.hold_in_other_process()
        p.kill()
        p.wait(5)
        with RunLock(self.root):
            pass


class PlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.logdir = Path(self.tmp.name) / "logs"
        p = mock.patch.object(config, "LOG_DIR", self.logdir)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plist_roundtrip(self):
        d = plistlib.loads(launchd.plist_xml("/usr/bin/python3", "/opt/app/launch.py"))
        self.assertEqual(d["Label"], "local.feishu-backup")
        self.assertIs(d["RunAtLoad"], True)
        self.assertEqual(d["StartInterval"], 3600)
        # 终审 I1：caffeinate -i 防止跑到一半进入空闲睡眠
        self.assertEqual(d["ProgramArguments"],
                         ["/usr/bin/caffeinate", "-i", "/usr/bin/python3", "/opt/app/launch.py", "run"])
        self.assertTrue(d["StandardOutPath"].startswith(str(self.logdir)))
        self.assertTrue(d["StandardErrorPath"].startswith(str(self.logdir)))
        self.assertEqual(plist_xml("/usr/bin/python3", "/opt/app/launch.py"),
                         launchd.plist_xml("/usr/bin/python3", "/opt/app/launch.py"))

    def test_install_and_uninstall_use_injected_paths(self):
        agents = Path(self.tmp.name) / "LaunchAgents"
        calls = []

        def runner(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, b"", b"")

        p = launchd.install("/usr/bin/python3", "/opt/app/launch.py", agents_dir=agents, runner=runner, uid=501)
        self.assertEqual(p, agents / "local.feishu-backup.plist")
        self.assertEqual(plistlib.loads(p.read_bytes())["ProgramArguments"][-1], "run")
        self.assertTrue(self.logdir.is_dir())
        self.assertEqual(calls, [
            ["launchctl", "bootout", "gui/501/local.feishu-backup"],
            ["launchctl", "bootstrap", "gui/501", str(p)],
        ])
        calls.clear()
        launchd.uninstall(agents_dir=agents, runner=runner, uid=501)
        self.assertFalse(p.exists())
        self.assertEqual(calls, [["launchctl", "bootout", "gui/501/local.feishu-backup"]])

    def test_install_ignores_bootout_failure(self):
        agents = Path(self.tmp.name) / "LaunchAgents"

        def runner(args, **kw):
            rc = 3 if args[1] == "bootout" else 0
            return subprocess.CompletedProcess(args, rc, b"", b"")

        launchd.install("p", "s", agents_dir=agents, runner=runner, uid=1)

    def test_install_raises_when_bootstrap_fails(self):
        agents = Path(self.tmp.name) / "LaunchAgents"

        def runner(args, **kw):
            rc = 5 if args[1] == "bootstrap" else 0
            return subprocess.CompletedProcess(args, rc, b"", "boom".encode())

        with self.assertRaises(RuntimeError):
            launchd.install("p", "s", agents_dir=agents, runner=runner, uid=1, sleep=lambda s: None)

    def test_bootstrap_eio_retried_once(self):
        # 终审 M-2：bootout 之后立刻 bootstrap 可能 EIO，等 1 秒重试一次
        agents = Path(self.tmp.name) / "LaunchAgents"
        boots, sleeps = [], []

        def runner(args, **kw):
            if args[1] == "bootstrap":
                boots.append(1)
                rc = 5 if len(boots) == 1 else 0
                return subprocess.CompletedProcess(args, rc, b"", b"Input/output error")
            return subprocess.CompletedProcess(args, 0, b"", b"")

        launchd.install("p", "s", agents_dir=agents, runner=runner, uid=1, sleep=sleeps.append)
        self.assertEqual(len(boots), 2)
        self.assertEqual(sleeps, [1])


def _ok(args, **kw):
    return subprocess.CompletedProcess(args, 0, b"", b"")


class AppCopyTest(unittest.TestCase):
    """终审 I1 + 小问题 2：定时任务不指向插件缓存，而是指向按版本存放的程序副本的启动器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.app = self.base / "AppSupport" / "feishu-backup" / "app"
        self.agents = self.base / "LaunchAgents"
        p = mock.patch.object(config, "LOG_DIR", self.base / "logs")
        p.start()
        self.addCleanup(p.stop)

    def test_install_app_versioned_copy(self):
        launcher = schedule.install_app(app_root=self.app)
        self.assertEqual(launcher, self.app / "launch.py")
        v = self.app / "versions" / schedule.__version__
        self.assertTrue((v / "fbk.py").is_file())
        self.assertTrue((v / "fbk" / "runner.py").is_file())
        self.assertEqual((self.app / "current").read_text(encoding="utf-8"), schedule.__version__)

    def test_mac_install_points_plist_at_launcher(self):
        p = schedule.install(python="/usr/bin/python3", agents_dir=self.agents, runner=_ok, uid=1,
                             app_root=self.app, is_windows=False)
        self.assertEqual(Path(p), self.agents / "local.feishu-backup.plist")
        args = plistlib.loads(Path(p).read_bytes())["ProgramArguments"]
        self.assertEqual(args[3], str(self.app / "launch.py"))
        self.assertTrue(Path(args[3]).is_file())
        self.assertNotIn("plugins", args[3])

    def test_windows_install_dispatches_to_schtasks(self):
        calls, files = [], []

        def runner(args, **kw):
            calls.append(list(args))
            if "/Create" in args:
                files.append(Path(args[args.index("/XML") + 1]).read_bytes().decode("utf-16"))
            return subprocess.CompletedProcess(args, 0, b"", b"")

        with mock.patch.object(winsched.tempfile, "tempdir", str(self.base)):
            out = schedule.install(python=r"C:\Py\pythonw.exe", runner=runner, app_root=self.app,
                                   is_windows=True, user="FAKEPC\\fakeuser")
        self.assertIn("feishu-backup", str(out))
        self.assertEqual(calls[0][:4], ["schtasks", "/Create", "/TN", "feishu-backup"])
        self.assertEqual(winsched.parse_exec(files[0]), (r"C:\Py\pythonw.exe", str(self.app / "launch.py")))
        self.assertFalse(self.agents.exists())

    def test_windows_uninstall_and_verify_dispatch(self):
        calls = []

        def runner(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 1, b"", b"ERROR: The system cannot find the file specified.")

        schedule.uninstall(runner=runner, is_windows=True)
        self.assertIsNone(schedule.verify(runner=runner, is_windows=True))
        self.assertTrue(all(c[0] == "schtasks" for c in calls))

    def test_pick_python_prefers_system_python_when_new_enough(self):
        good = lambda args, **kw: subprocess.CompletedProcess(args, 0, "True\n", "")  # noqa: E731
        old = lambda args, **kw: subprocess.CompletedProcess(args, 0, "False\n", "")  # noqa: E731
        kw = {"is_windows": False}
        self.assertEqual(schedule.pick_python(runner=good, exists=lambda p: True, **kw), "/usr/bin/python3")
        self.assertEqual(schedule.pick_python(runner=old, exists=lambda p: True, **kw), sys.executable)
        self.assertEqual(schedule.pick_python(runner=good, exists=lambda p: False, **kw), sys.executable)

        def boom(args, **kw):
            raise OSError("no")
        self.assertEqual(schedule.pick_python(runner=boom, exists=lambda p: True, **kw), sys.executable)

    def test_pick_python_windows_prefers_pythonw(self):
        exe = r"C:\Python312\python.exe"
        seen = []

        def exists(p):
            seen.append(p)
            return True

        got = schedule.pick_python(exists=exists, is_windows=True, executable=exe)
        self.assertEqual(got.replace("/", "\\"), r"C:\Python312\pythonw.exe")
        self.assertEqual(schedule.pick_python(exists=lambda p: False, is_windows=True, executable=exe), exe)

    def _installed(self):
        with mock.patch.object(launchd, "CAFFEINATE", sys.executable):
            return schedule.install(python=sys.executable, agents_dir=self.agents, runner=_ok, uid=1,
                                    app_root=self.app, is_windows=False)

    def test_verify_ok_missing_launcher_and_version_mismatch(self):
        plist = self._installed()
        self.assertEqual(schedule.verify(plist, is_windows=False), [])
        (self.app / "current").write_text("0.0.9")
        probs = schedule.verify(plist, is_windows=False)
        self.assertTrue(any("0.0.9" in p for p in probs), probs)
        (self.app / "launch.py").unlink()
        self.assertTrue(any("launch.py" in p for p in schedule.verify(plist, is_windows=False)))
        self.assertIsNone(schedule.verify(self.base / "nope.plist", is_windows=False))  # 没装定时任务

    def test_verify_default_plist_uses_agents_dir(self):
        self._installed()
        with mock.patch.object(schedule, "default_agents_dir", return_value=self.agents):
            self.assertEqual(schedule.verify(is_windows=False), [])

    def test_verify_old_plist_without_launcher(self):
        self.agents.mkdir(parents=True)
        (self.agents / "local.feishu-backup.plist").write_bytes(
            launchd.plist_xml(sys.executable, str(self.base / "cache" / "fbk.py")))
        with mock.patch.object(launchd, "CAFFEINATE", sys.executable):
            probs = schedule.verify(self.agents / "local.feishu-backup.plist", is_windows=False)
        self.assertTrue(probs)



class TlsProbeTest(unittest.TestCase):
    """终审 I1：setup --schedule 用定时任务将要用的 Python 试连一次飞书。"""

    def _run(self, rc=0, exc=None):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(cmd, rc, "", "")
        return runner, calls

    def test_ok(self):
        r, calls = self._run(0)
        self.assertIsNone(schedule.tls_probe("/x/python3", runner=r))
        cmd, kw = calls[0]
        self.assertEqual(cmd[:2], ["/x/python3", "-c"])
        self.assertIn("open.feishu.cn", cmd[2])
        self.assertEqual(kw["timeout"], 20)

    def test_cert(self):
        self.assertEqual(schedule.tls_probe("p", runner=self._run(schedule.TLS_PROBE_CERT)[0]), "cert")

    def test_network(self):
        self.assertEqual(schedule.tls_probe("p", runner=self._run(schedule.TLS_PROBE_NET)[0]), "network")
        self.assertEqual(schedule.tls_probe("p", runner=self._run(exc=subprocess.TimeoutExpired("p", 20))[0]),
                         "network")

    def test_python_unusable(self):
        self.assertEqual(schedule.tls_probe("p", runner=self._run(exc=OSError("nope"))[0]), "python")
        self.assertEqual(schedule.tls_probe("p", runner=self._run(1)[0]), "python")

    def test_probe_script_classifies_for_real(self):
        # 真的用当前 Python 跑探测脚本，但把 urlopen 换成抛证书错误（不联网）
        import ssl  # noqa: F401
        src = ("import ssl, urllib.request\n"
               "def boom(*a, **k): raise ssl.SSLCertVerificationError(1, 'certificate verify failed')\n"
               "urllib.request.urlopen = boom\n") + schedule.TLS_PROBE_SRC
        r = subprocess.run([sys.executable, "-c", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, schedule.TLS_PROBE_CERT, r.stderr)


if __name__ == "__main__":
    unittest.main()
