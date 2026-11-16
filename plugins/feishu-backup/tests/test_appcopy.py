"""platform.appcopy：程序副本按版本存放 + current 指针 + 启动器；旧版本没人用了才清理。全部在临时目录里。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import __version__  # noqa: E402
from fbk.platform import IS_WINDOWS, appcopy  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"

# 假 fbk.py：报告 argv、sys.path[0]、能否 import fbk（及其版本）、自己版本目录的 in_use 是否被启动器占着；
# 带 wait 参数时先打印 ready 再等 stdin 一行，模拟一次正在跑的备份。
FAKE_MAIN = r'''
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fbk
here = os.path.dirname(os.path.abspath(__file__))
busy = None
if os.name != "nt":
    import fcntl
    fd = os.open(os.path.join(here, "in_use"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        busy = False
    except OSError:
        busy = True
def main():
    print(json.dumps({"argv": sys.argv, "path0": sys.path[0], "version": fbk.__version__,
                      "here": here, "in_use_busy": busy}), flush=True)
    if "wait" in sys.argv:
        print("ready", flush=True)
        sys.stdin.readline()
    return 7
if __name__ == "__main__":
    sys.exit(main())
'''


def make_src(base: Path, version: str, main: str = FAKE_MAIN) -> Path:
    src = base / ("src-" + version)
    (src / "fbk").mkdir(parents=True)
    (src / "fbk.py").write_text(main, encoding="utf-8")
    (src / "fbk" / "__init__.py").write_text('__version__ = "%s"\n' % version, encoding="utf-8")
    (src / "fbk" / "__pycache__").mkdir()
    (src / "fbk" / "__pycache__" / "x.pyc").write_bytes(b"junk")
    return src


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "feishu-backup" / "app"

    def launch(self, *args, **kw):
        return subprocess.run([sys.executable, str(self.root / "launch.py")] + list(args),
                              capture_output=True, text=True, timeout=60, **kw)


class InstallTest(Base):
    def test_versioned_copy_current_and_launcher(self):
        launcher = appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        self.assertEqual(launcher, self.root / "launch.py")
        self.assertTrue(launcher.is_file())
        v = self.root / "versions" / "1.0.0"
        self.assertTrue((v / "fbk.py").is_file())
        self.assertTrue((v / "fbk" / "__init__.py").is_file())
        self.assertFalse(list(v.rglob("__pycache__")))
        self.assertEqual((self.root / "current").read_text(encoding="utf-8").strip(), "1.0.0")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["current", "launch.py", "versions"])
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])

    def test_copies_real_scripts(self):
        appcopy.install(SCRIPTS, self.root, __version__)
        v = self.root / "versions" / __version__
        self.assertTrue((v / "fbk" / "platform" / "appcopy.py").is_file())
        self.assertIsNone(appcopy.verify(self.root, __version__))

    def test_new_version_removes_unused_old_version(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.1.0"])
        self.assertEqual((self.root / "current").read_text(encoding="utf-8"), "1.1.0")

    def test_same_version_reinstall_replaces_content(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        stale = self.root / "versions" / "1.0.0" / "fbk" / "old_module.py"
        stale.write_text("x")
        appcopy.install(make_src(self.base / "again", "1.0.0"), self.root, "1.0.0")
        self.assertFalse(stale.exists())
        self.assertTrue((self.root / "versions" / "1.0.0" / "fbk.py").is_file())
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])  # 不留旁置目录

    def test_rejects_bad_version(self):
        src = make_src(self.base, "1.0.0")
        for bad in ("", "../x", "a/b", "a\\b", "..", ".hidden", "1.0 0", "1..0"):
            with self.assertRaises(ValueError, msg=bad):
                appcopy.install(src, self.root, bad)
        self.assertFalse((self.root / "versions").exists())

    def test_legacy_flat_copy_is_moved_aside_not_deleted(self):
        # v0.1.0 的布局：app/ 下直接放 fbk.py + fbk/
        self.root.mkdir(parents=True)
        (self.root / "fbk.py").write_text("legacy main")
        (self.root / "fbk").mkdir()
        (self.root / "fbk" / "__init__.py").write_text('__version__ = "0.1.0"\n')
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        self.assertFalse((self.root / "fbk.py").exists())
        self.assertFalse((self.root / "fbk").exists())
        legacy = [p for p in self.root.iterdir() if p.name.startswith("app.legacy-")]
        self.assertEqual(len(legacy), 1)
        self.assertEqual((legacy[0] / "fbk.py").read_text(), "legacy main")
        self.assertTrue((legacy[0] / "fbk" / "__init__.py").is_file())
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        self.assertTrue((legacy[0] / "fbk.py").is_file())  # 之后的安装也不碰它


@unittest.skipIf(IS_WINDOWS, "用 fcntl 模拟占用")
class CleanupTest(Base):
    def _hold(self, version):
        import fcntl
        fd = os.open(str(self.root / "versions" / version / "in_use"), os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)  # 与启动器同样的共享锁
        self.addCleanup(lambda: _close(fd))
        return fd

    def test_old_version_in_use_kept_then_cleaned_after_release(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        fd = self._hold("1.0.0")
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        names = sorted(p.name for p in (self.root / "versions").iterdir())
        self.assertEqual(names, ["1.0.0", "1.1.0"])
        self.assertTrue((self.root / "versions" / "1.0.0" / "fbk.py").is_file())  # 正在用的副本完好
        os.close(fd)
        self.assertEqual(appcopy.cleanup(self.root), [self.root / "versions" / "1.0.0"])
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.1.0"])

    def test_same_version_in_use_aside_dir_kept_until_free(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        fd = self._hold("1.0.0")
        appcopy.install(make_src(self.base / "again", "1.0.0", main=FAKE_MAIN + "\n# changed\n"), self.root, "1.0.0")
        aside = [p for p in (self.root / "versions").iterdir() if p.name != "1.0.0"]
        self.assertEqual(len(aside), 1)
        self.assertTrue(aside[0].name.startswith(".1.0.0.old-"))
        os.close(fd)
        appcopy.cleanup(self.root)
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])

    def _windows_like_replace(self):
        """模拟 Windows：被运行中的进程打开着 in_use 的版本目录不能改名。"""
        real = os.replace
        busy = str(self.root / "versions" / "1.0.0")

        def replace(src, dst):
            if os.path.abspath(str(src)) == os.path.abspath(busy):
                raise PermissionError(13, "Access is denied", str(src))
            return real(src, dst)
        return replace

    def test_install_from_inside_running_copy_skips_swap(self):
        # 启动器运行 versions/1.0.0/fbk.py setup --schedule：复制源就是目标本身，且 in_use 正被占着
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        self._hold("1.0.0")
        (self.root / "launch.py").unlink()
        (self.root / "current").write_text("bogus-old", encoding="utf-8")
        from unittest import mock
        with mock.patch.object(appcopy.os, "replace", side_effect=self._windows_like_replace()):
            launcher = appcopy.install(self.root / "versions" / "1.0.0", self.root, "1.0.0")
        self.assertTrue(launcher.is_file())
        self.assertEqual(appcopy.read_current(self.root), "1.0.0")
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])  # 没有 .old-* / .new-*
        self.assertTrue((self.root / "versions" / "1.0.0" / "fbk.py").is_file())
        self.assertIsNone(appcopy.verify(self.root, "1.0.0"))

    def test_identical_copy_reinstall_does_not_swap(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        self._hold("1.0.0")
        from unittest import mock
        with mock.patch.object(appcopy.os, "replace", side_effect=self._windows_like_replace()):
            appcopy.install(make_src(self.base / "again", "1.0.0"), self.root, "1.0.0")
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])
        self.assertIsNone(appcopy.verify(self.root, "1.0.0"))

    def test_different_content_same_version_still_swaps(self):
        appcopy.install(make_src(self.base, "1.0.0", main="OLD"), self.root, "1.0.0")
        appcopy.install(make_src(self.base / "again", "1.0.0", main="NEW"), self.root, "1.0.0")
        self.assertEqual((self.root / "versions" / "1.0.0" / "fbk.py").read_text(), "NEW")

    def test_cleanup_never_touches_current_symlinks_files_or_tmp(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        outside = self.base / "outside"
        (outside / "keep").mkdir(parents=True)
        (outside / "keep" / "data.txt").write_text("precious")
        vers = self.root / "versions"
        os.symlink(str(outside / "keep"), str(vers / "0.9.0"))  # 看起来像版本号的符号链接
        (vers / "notes.txt").write_text("x")  # 普通文件
        (vers / ".1.2.0.new-123").mkdir()  # 另一个安装进程的临时目录
        self.assertEqual(appcopy.cleanup(self.root), [])
        self.assertTrue((outside / "keep" / "data.txt").is_file())
        self.assertTrue((vers / "0.9.0").is_symlink())
        self.assertTrue((vers / "notes.txt").is_file())
        self.assertTrue((vers / ".1.2.0.new-123").is_dir())
        self.assertTrue((vers / "1.0.0" / "fbk.py").is_file())

    def test_cleanup_refuses_when_versions_dir_is_symlink(self):
        target = self.base / "elsewhere"
        (target / "0.5.0").mkdir(parents=True)
        self.root.mkdir(parents=True)
        os.symlink(str(target), str(self.root / "versions"))
        (self.root / "current").write_text("1.0.0")
        self.assertEqual(appcopy.cleanup(self.root), [])
        self.assertTrue((target / "0.5.0").is_dir())

    def test_cleanup_without_valid_current_deletes_nothing(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        (self.root / "current").write_text("../evil")
        self.assertEqual(appcopy.cleanup(self.root), [])
        (self.root / "current").unlink()
        self.assertEqual(appcopy.cleanup(self.root), [])
        self.assertTrue((self.root / "versions" / "1.0.0").is_dir())


def _close(fd):
    try:
        os.close(fd)
    except OSError:
        pass


class LauncherTest(Base):
    def test_launcher_runs_current_version_main(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        r = self.launch("status", "--json")
        self.assertEqual(r.returncode, 7, r.stderr)
        d = json.loads(r.stdout.splitlines()[0])
        vdir = str(self.root / "versions" / "1.0.0")
        self.assertEqual(os.path.realpath(d["here"]), os.path.realpath(vdir))
        self.assertEqual(os.path.realpath(d["path0"]), os.path.realpath(vdir))
        self.assertEqual(d["version"], "1.0.0")
        self.assertEqual(d["argv"][1:], ["status", "--json"])
        self.assertTrue(d["argv"][0].endswith("fbk.py"))
        if not IS_WINDOWS:
            self.assertTrue(d["in_use_busy"])  # 启动器运行期间持有 in_use

    def test_launcher_follows_current(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        d = json.loads(self.launch().stdout.splitlines()[0])
        self.assertEqual(d["version"], "1.1.0")

    def test_launcher_bad_current_fails_with_chinese_message(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        for content in ("../../etc", "", "nope"):
            (self.root / "current").write_text(content)
            r = self.launch("run")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("setup --schedule", r.stderr)

    @unittest.skipIf(IS_WINDOWS, "posix 端到端")
    def test_running_launcher_protects_its_version_from_cleanup(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        p = subprocess.Popen([sys.executable, str(self.root / "launch.py"), "wait"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        p.stdout.readline()
        self.assertEqual(p.stdout.readline().strip(), "ready")
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        self.assertTrue((self.root / "versions" / "1.0.0" / "fbk.py").is_file())
        p.communicate("go\n", timeout=30)
        self.assertEqual(p.returncode, 7)
        appcopy.cleanup(self.root)
        self.assertEqual([x.name for x in (self.root / "versions").iterdir()], ["1.1.0"])


def _load_launcher(path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("fbk_launch_under_test", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FixRound1Test(Base):
    @unittest.skipIf(IS_WINDOWS, "posix 符号链接")
    def test_symlinked_in_use_not_followed(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        appcopy.install(make_src(self.base, "1.1.0"), self.root, "1.1.0")
        old = self.root / "versions" / "1.0.0"
        old.mkdir()  # 模拟残留的旧版本目录，in_use 是指向外部的链接
        (old / "fbk.py").write_text("x")
        victim = self.base / "victim.txt"
        victim.write_text("precious")
        os.symlink(str(victim), str(old / "in_use"))
        self.assertEqual(appcopy.cleanup(self.root), [])  # 不跟随、不删
        self.assertTrue(old.is_dir())
        self.assertEqual(victim.read_text(), "precious")
        # 启动器同样拒绝链接的 in_use
        mod = _load_launcher(self.root / "launch.py")
        self.assertIsNone(mod._hold(str(old)))
        dangling = self.root / "versions" / "1.1.0" / "in_use"
        if dangling.exists():
            dangling.unlink()
        os.symlink(str(self.base / "created-by-follow"), str(dangling))
        self.assertIsNone(mod._hold(str(dangling.parent)))
        self.assertFalse((self.base / "created-by-follow").exists())

    @unittest.skipIf(IS_WINDOWS, "用 fcntl 模拟清理进程的排他锁")
    def test_launcher_lock_failure_retries_with_sleep_then_gives_up(self):
        import fcntl
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        fd = os.open(str(self.root / "versions" / "1.0.0" / "in_use"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(lambda: _close(fd))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 清理进程正拿着排他锁
        mod = _load_launcher(self.root / "launch.py")
        sleeps = []
        with self.assertRaises(SystemExit) as cm:
            mod.main(sleep=sleeps.append)
        self.assertIn("setup --schedule", str(cm.exception.code))
        self.assertEqual(sleeps, [mod.RETRY_SLEEP] * (mod.TRIES - 1))
        self.assertEqual(mod._HELD, [])  # 没有在没锁住的情况下继续运行

    @unittest.skipIf(IS_WINDOWS, "用 fcntl 模拟清理进程的排他锁")
    def test_launcher_retries_after_lock_released(self):
        import fcntl
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        fd = os.open(str(self.root / "versions" / "1.0.0" / "in_use"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(lambda: _close(fd))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        mod = _load_launcher(self.root / "launch.py")
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            fcntl.flock(fd, fcntl.LOCK_UN)  # 清理放弃了

        ran = []
        mod.runpy = type("R", (), {"run_path": staticmethod(lambda p, run_name=None: ran.append(p))})
        argv = sys.argv[:]
        try:
            mod.main(sleep=sleep)
        finally:
            sys.argv = argv
            for h in mod._HELD:
                _close(h)
        self.assertEqual(sleeps, [mod.RETRY_SLEEP])
        self.assertEqual(len(mod._HELD), 1)
        self.assertTrue(ran and ran[0].endswith("fbk.py"))

    def test_failed_swap_restores_old_copy(self):
        appcopy.install(make_src(self.base, "1.0.0", main="OLD"), self.root, "1.0.0")
        real = os.replace

        def flaky(src, dst):
            if ".new-" in os.path.basename(str(src)):
                raise OSError("disk hiccup")
            return real(src, dst)

        from unittest import mock
        with mock.patch.object(appcopy.os, "replace", side_effect=flaky):
            with self.assertRaises(OSError):
                appcopy.install(make_src(self.base / "again", "1.0.0", main="NEW"), self.root, "1.0.0")
        target = self.root / "versions" / "1.0.0"
        self.assertEqual((target / "fbk.py").read_text(), "OLD")
        self.assertEqual([p.name for p in (self.root / "versions").iterdir()], ["1.0.0"])


class VerifyTest(Base):
    def test_ok(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        self.assertIsNone(appcopy.verify(self.root, "1.0.0"))

    def test_launcher_missing(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        (self.root / "launch.py").unlink()
        self.assertIn("launch.py", appcopy.verify(self.root, "1.0.0"))

    def test_current_missing_or_bad(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        (self.root / "current").write_text("../x")
        self.assertIn("current", appcopy.verify(self.root, "1.0.0"))
        (self.root / "current").unlink()
        self.assertIn("current", appcopy.verify(self.root, "1.0.0"))

    def test_version_dir_missing(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        (self.root / "versions" / "1.0.0" / "fbk.py").unlink()
        self.assertIn("找不到", appcopy.verify(self.root, "1.0.0"))

    def test_version_mismatch(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        msg = appcopy.verify(self.root, "2.0.0")
        self.assertIn("1.0.0", msg)
        self.assertIn("2.0.0", msg)

    def test_copied_code_version_differs_from_pointer(self):
        appcopy.install(make_src(self.base, "1.0.0"), self.root, "1.0.0")
        (self.root / "versions" / "1.0.0" / "fbk" / "__init__.py").write_text('__version__ = "0.0.9"\n')
        self.assertIn("0.0.9", appcopy.verify(self.root, "1.0.0"))


if __name__ == "__main__":
    unittest.main()
