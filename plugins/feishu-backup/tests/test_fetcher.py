import hashlib
import os
import sys
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk import fetcher  # noqa: E402
from fbk.client import FeishuClient  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402


def make_client(ft):
    return FeishuClient(lambda: "tok", transport=ft, rps=0, sleep=lambda s: None)


def job(status, **kw):
    d = {"result": {"job_status": status, "file_token": "ftk", "file_size": 3,
                    "file_extension": "pdf", "job_error_msg": kw.get("msg", "")}}
    return d


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_export_polls_until_done(self):
        ft = FakeTransport()
        ft.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tk1"})
        ft.queue_json("GET", "/drive/v1/export_tasks/tk1", job(1))
        ft.queue_json("GET", "/drive/v1/export_tasks/tk1", job(2))
        ft.queue_json("GET", "/drive/v1/export_tasks/tk1", job(0))
        ft.queue("GET", "/drive/v1/export_tasks/file/ftk/download", body=b"PDF")
        sleeps = []
        dest = self.root / "sub" / "a.pdf"
        out = fetcher.export_to(make_client(ft), "doctok", "docx", dest, sleep=sleeps.append)
        self.assertEqual(out, dest)
        self.assertEqual(dest.read_bytes(), b"PDF")
        self.assertFalse((self.root / "sub" / "a.pdf.part").exists())
        self.assertEqual(len(sleeps), 2)
        post = [c for c in ft.calls if c["method"] == "POST"][0]
        self.assertIn(b'"file_extension": "pdf"', post["body"])
        self.assertIn("token=doctok", [c for c in ft.calls if c["path"] == "/drive/v1/export_tasks/tk1"][0]["query"])

    def test_export_failure_status(self):
        ft = FakeTransport()
        ft.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tk1"})
        ft.queue_json("GET", "/drive/v1/export_tasks/tk1", job(3, msg="boom"))
        dest = self.root / "a.pdf"
        with self.assertRaises(fetcher.ExportFailed):
            fetcher.export_to(make_client(ft), "t", "docx", dest, sleep=lambda s: None)
        self.assertFalse(dest.exists())

    def test_export_timeout(self):
        ft = FakeTransport()
        ft.queue_json("POST", "/drive/v1/export_tasks", {"ticket": "tk1"})
        ft.queue_json("GET", "/drive/v1/export_tasks/tk1", job(1))
        t = [0.0]

        def sleep(s):
            t[0] += s
        with self.assertRaises(fetcher.ExportFailed):
            fetcher.export_to(make_client(ft), "t", "sheet", self.root / "a.xlsx",
                              poll=2.0, timeout=10, sleep=sleep, clock=lambda: t[0])

    def test_export_unknown_type(self):
        with self.assertRaises(ValueError):
            fetcher.export_to(make_client(FakeTransport()), "t", "mindnote", self.root / "x")

    def test_download_file_and_attachment(self):
        ft = FakeTransport()
        ft.queue("GET", "/drive/v1/files/tk/download", body=b"hello")
        ft.queue("GET", "/im/v1/messages/om_1/resources/fk", body=b"att")
        c = make_client(ft)
        d1 = fetcher.download_file(c, "tk", self.root / "f.bin")
        d2 = fetcher.download_attachment(c, "om_1", "fk", self.root / "x" / "g.bin")
        self.assertEqual(d1.read_bytes(), b"hello")
        self.assertEqual(d2.read_bytes(), b"att")
        self.assertIn("type=file", ft.calls[1]["query"])

    def test_part_file_not_committed_on_error(self):
        ft = FakeTransport()
        ft.queue("GET", "/drive/v1/files/tk/download", body=OSError("conn reset"))
        dest = self.root / "f.bin"
        with self.assertRaises(Exception):
            fetcher.download_file(make_client(ft), "tk", dest)
        self.assertFalse(dest.exists())
        self.assertFalse((self.root / "f.bin.part").exists())

    def test_error_after_partial_write_cleans_part_and_keeps_existing_dest(self):
        dest = self.root / "f.bin"
        dest.write_bytes(b"old")

        class C:
            def download(self, path, p, params=None, headers=None):
                Path(p).write_bytes(b"half")
                raise RuntimeError("mid-stream")
        with self.assertRaises(RuntimeError):
            fetcher.download_file(C(), "tk", dest)
        self.assertEqual(dest.read_bytes(), b"old")
        self.assertFalse((self.root / "f.bin.part").exists())

    def test_success_overwrites_existing(self):
        ft = FakeTransport()
        ft.queue("GET", "/drive/v1/files/tk/download", body=b"new")
        dest = self.root / "f.bin"
        dest.write_bytes(b"old")
        fetcher.download_file(make_client(ft), "tk", dest)
        self.assertEqual(dest.read_bytes(), b"new")

    def test_ensure_space(self):
        U = namedtuple("U", "total used free")
        with mock.patch("fbk.fetcher.shutil.disk_usage", return_value=U(100, 90, 10)):
            with self.assertRaises(fetcher.DiskFull):
                fetcher.ensure_space(self.root, 5, margin=10)
            fetcher.ensure_space(self.root, 5, margin=5)  # free == need+margin: ok

    def test_sha256_file(self):
        p = self.root / "a"
        data = b"x" * (2 * 1024 * 1024 + 5)
        p.write_bytes(data)
        self.assertEqual(fetcher.sha256_file(p), hashlib.sha256(data).hexdigest())

    def test_clean_parts(self):
        (self.root / "a" / "b").mkdir(parents=True)
        for rel in ["x.part", "a/y.pdf.part", "a/b/z.part", "a/keep.pdf"]:
            (self.root / rel).write_bytes(b"1")
        self.assertEqual(fetcher.clean_parts(self.root), 3)
        self.assertTrue((self.root / "a" / "keep.pdf").exists())
        self.assertFalse((self.root / "x.part").exists())


if __name__ == "__main__":
    unittest.main()
