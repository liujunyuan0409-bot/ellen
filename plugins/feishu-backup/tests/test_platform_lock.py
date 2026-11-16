import errno
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import runner, schedule  # noqa: E402
from fbk.platform import awake, lock  # noqa: E402


class FakeMsvcrt:
    LK_NBLCK = 2
    LK_UNLCK = 0

    def __init__(self, busy=False, exc=None):
        self.calls = []
        self.busy = busy
        self.exc = exc

    def locking(self, fd, mode, nbytes):
        self.calls.append((fd, mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode == self.LK_NBLCK and (self.busy or self.exc):
            raise self.exc or OSError(13, "locked")


class WindowsLockTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.fd = os.open(os.path.join(d, "x.lock"), os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, self.fd)

    def test_try_lock_uses_nblck_at_offset_zero(self):
        os.lseek(self.fd, 5, os.SEEK_SET)
        m = FakeMsvcrt()
        self.assertTrue(lock._windows_try_lock(self.fd, m))
        self.assertEqual(m.calls, [(self.fd, m.LK_NBLCK, 1, 0)])

    def test_busy_returns_false(self):
        self.assertFalse(lock._windows_try_lock(self.fd, FakeMsvcrt(busy=True)))

    def test_other_oserror_is_reraised(self):
        with self.assertRaises(OSError):
            lock._windows_try_lock(self.fd, FakeMsvcrt(exc=OSError(errno.EBADF, "bad")))

    def test_deadlock_errno_is_busy(self):
        self.assertFalse(lock._windows_try_lock(self.fd, FakeMsvcrt(exc=OSError(errno.EDEADLK, "d"))))

    def test_acquire_success_writes_pid_after_lock_and_release_unlocks_before_close(self):
        d = tempfile.mkdtemp()
        events = []
        m = FakeMsvcrt()
        orig = m.locking
        m.locking = lambda fd, mode, n: (events.append("lock" if mode == m.LK_NBLCK else "unlock"), orig(fd, mode, n))[1]
        real_write, real_close = os.write, os.close
        with mock.patch.object(lock, "try_lock", lambda fd: lock._windows_try_lock(fd, m)), \
             mock.patch.object(lock, "unlock", lambda fd: lock._windows_unlock(fd, m)), \
             mock.patch.object(os, "write", lambda fd, b: (events.append("write"), real_write(fd, b))[1]), \
             mock.patch.object(os, "close", lambda fd: (events.append("close"), real_close(fd))[1]):
            rl = schedule.RunLock(d).acquire()
            rl.release()
        self.assertEqual(events[:2], ["lock", "write"])
        self.assertLess(events.index("unlock"), len(events) - 1 - events[::-1].index("close"))
        self.assertEqual(events[-2:], ["unlock", "close"])

    def test_unlock_uses_lk_unlck_at_offset_zero(self):
        m = FakeMsvcrt()
        os.lseek(self.fd, 3, os.SEEK_SET)
        lock._windows_unlock(self.fd, m)
        self.assertEqual(m.calls, [(self.fd, m.LK_UNLCK, 1, 0)])

    def test_unlock_swallows_oserror(self):
        m = FakeMsvcrt(exc=OSError(1, "x"))
        m.LK_NBLCK = -1  # 只让 UNLCK 抛
        lock._windows_unlock(self.fd, m)

    def test_runlock_raises_already_running_via_windows_impl(self):
        d = tempfile.mkdtemp()
        m = FakeMsvcrt(busy=True)
        with mock.patch.object(lock, "try_lock", lambda fd: lock._windows_try_lock(fd, m)):
            with self.assertRaises(schedule.AlreadyRunning):
                schedule.RunLock(d).acquire()
        self.assertEqual(m.calls[0][1], m.LK_NBLCK)


class PosixLockTest(unittest.TestCase):
    def test_second_fd_cannot_lock_then_can_after_unlock(self):
        p = os.path.join(tempfile.mkdtemp(), "x.lock")
        a = os.open(p, os.O_RDWR | os.O_CREAT)
        b = os.open(p, os.O_RDWR | os.O_CREAT)
        self.addCleanup(os.close, a)
        self.addCleanup(os.close, b)
        self.assertTrue(lock._posix_try_lock(a))
        self.assertFalse(lock._posix_try_lock(b))
        lock._posix_unlock(a)
        self.assertTrue(lock._posix_try_lock(b))


class FakeKernel32:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def SetThreadExecutionState(self, flags):
        self.calls.append(flags)
        if self.fail:
            raise OSError("boom")
        return 1


class AwakeTest(unittest.TestCase):
    def test_windows_flags_on_enter_and_exit(self):
        k = FakeKernel32()
        with awake.keep_awake(is_windows=True, kernel32=k):
            self.assertEqual(k.calls, [0x80000000 | 0x00000001])
        self.assertEqual(k.calls, [0x80000000 | 0x00000001, 0x80000000])

    def test_exit_runs_on_exception(self):
        k = FakeKernel32()
        with self.assertRaises(ValueError):
            with awake.keep_awake(is_windows=True, kernel32=k):
                raise ValueError
        self.assertEqual(k.calls[-1], 0x80000000)

    def test_failure_never_raises(self):
        with awake.keep_awake(is_windows=True, kernel32=FakeKernel32(fail=True)):
            pass

    def test_non_windows_noop(self):
        k = FakeKernel32()
        with awake.keep_awake(is_windows=False, kernel32=k):
            pass
        self.assertEqual(k.calls, [])


class TermHandlerTest(unittest.TestCase):
    def test_sigbreak_registered_when_present(self):
        calls = []
        fake_sig = mock.Mock(wraps=signal)
        fake_sig.SIGTERM = signal.SIGTERM
        fake_sig.SIGBREAK = 21
        fake_sig.signal = lambda s, h: calls.append(s)
        with mock.patch.object(runner, "signal", fake_sig):
            runner.install_term_handler()
        self.assertEqual(calls, [signal.SIGTERM, 21])

    def test_no_sigbreak_on_posix(self):
        calls = []
        with mock.patch.object(runner.signal, "signal", lambda s, h: calls.append(s)):
            runner.install_term_handler()
        self.assertEqual(calls, [signal.SIGTERM])


if __name__ == "__main__":
    unittest.main()
