import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import config, schedule  # noqa: E402
from fbk.schedule import AlreadyRunning, RunLock, is_due, plist_xml  # noqa: E402

H = 3600
NOW = 1_800_000_000


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


class IsDueTest(unittest.TestCase):
    def test_never_succeeded_is_due(self):
        self.assertTrue(is_due(None, 24, NOW))

    def test_23_hours_not_due(self):
        self.assertFalse(is_due(NOW - 23 * H, 24, NOW))

    def test_24_hours_due(self):
        self.assertTrue(is_due(NOW - 24 * H, 24, NOW))


class RunLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lock_path = self.root / ".feishu-backup" / "run.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def test_writes_pid_and_releases(self):
        with RunLock(self.root):
            self.assertEqual(self.lock_path.read_text().strip(), str(os.getpid()))
        self.assertFalse(self.lock_path.exists())

    def test_live_pid_raises(self):
        self.lock_path.parent.mkdir(parents=True)
        self.lock_path.write_text(str(os.getppid()))
        with self.assertRaises(AlreadyRunning):
            with RunLock(self.root):
                self.fail("不应进入")
        self.assertEqual(self.lock_path.read_text().strip(), str(os.getppid()))  # 别人的锁不动

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
        d = plistlib.loads(plist_xml("/usr/bin/python3", "/opt/fbk.py"))
        self.assertEqual(d["Label"], "local.feishu-backup")
        self.assertIs(d["RunAtLoad"], True)
        self.assertEqual(d["StartInterval"], 3600)
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", "/opt/fbk.py", "run"])
        self.assertTrue(d["StandardOutPath"].startswith(str(self.logdir)))
        self.assertTrue(d["StandardErrorPath"].startswith(str(self.logdir)))

    def test_install_and_uninstall_use_injected_paths(self):
        agents = Path(self.tmp.name) / "LaunchAgents"
        calls = []

        def runner(args, **kw):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, b"", b"")

        p = schedule.install(python="/usr/bin/python3", script="/opt/fbk.py",
                             agents_dir=agents, runner=runner, uid=501)
        self.assertEqual(p, agents / "local.feishu-backup.plist")
        self.assertEqual(plistlib.loads(p.read_bytes())["ProgramArguments"][2], "run")
        self.assertTrue(self.logdir.is_dir())
        self.assertEqual(calls, [
            ["launchctl", "bootout", "gui/501/local.feishu-backup"],
            ["launchctl", "bootstrap", "gui/501", str(p)],
        ])
        calls.clear()
        schedule.uninstall(agents_dir=agents, runner=runner, uid=501)
        self.assertFalse(p.exists())
        self.assertEqual(calls, [["launchctl", "bootout", "gui/501/local.feishu-backup"]])

    def test_install_ignores_bootout_failure(self):
        agents = Path(self.tmp.name) / "LaunchAgents"

        def runner(args, **kw):
            rc = 3 if args[1] == "bootout" else 0
            return subprocess.CompletedProcess(args, rc, b"", b"")

        schedule.install(python="p", script="s", agents_dir=agents, runner=runner, uid=1)

    def test_install_raises_when_bootstrap_fails(self):
        agents = Path(self.tmp.name) / "LaunchAgents"

        def runner(args, **kw):
            rc = 5 if args[1] == "bootstrap" else 0
            return subprocess.CompletedProcess(args, rc, b"", "boom".encode())

        with self.assertRaises(RuntimeError):
            schedule.install(python="p", script="s", agents_dir=agents, runner=runner, uid=1)


if __name__ == "__main__":
    unittest.main()
