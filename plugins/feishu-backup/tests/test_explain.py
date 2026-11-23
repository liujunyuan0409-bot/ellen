import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

import fbk  # noqa: E402
from fbk.client import ApiError  # noqa: E402
from fbk.explain import LIMIT_TAG, explain, is_limit  # noqa: E402
from fbk.fetcher import ExportFailed  # noqa: E402
from fbk.report import LIMIT_LABEL, RunReport  # noqa: E402
from fbk.sync import Outcome  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent


class ExplainTest(unittest.TestCase):
    def test_attachment_too_large_is_limit(self):
        r = explain(ApiError(234037, "Downloaded file size exceeds limit."))
        self.assertTrue(is_limit(r))
        self.assertIn("手动下载", r)

    def test_export_too_large_is_limit(self):
        r = explain(ExportFailed("导出失败 job_status=107 export_size_limit", 107), "docx")
        self.assertTrue(is_limit(r))
        self.assertIn("另存为 PDF", r)

    def test_export_failed_3_names_type_not_limit(self):
        r = explain(ExportFailed("导出失败 job_status=3 failed", 3), "bitable")
        self.assertFalse(is_limit(r))
        self.assertIn("多维表格", r)

    def test_export_timeout_108(self):
        r = explain(ExportFailed("导出失败 job_status=108 job_timetout", 108), "docx")
        self.assertIn("超时", r)
        self.assertNotIn("timetout", r)

    def test_unknown_export_status_keeps_raw_and_type(self):
        r = explain(ExportFailed("导出失败 job_status=9 x", 9), "sheet")
        self.assertIn("job_status=9", r)
        self.assertIn("电子表格", r)

    def test_rate_limit_says_retry_next_round(self):
        r = explain(ApiError(99991400, "request trigger frequency limit"))
        self.assertFalse(is_limit(r))
        self.assertIn("下轮会自动重试", r)

    def test_other_errors_unchanged(self):
        self.assertEqual(explain(ValueError("boom")), "boom")


class ReportSplitTest(unittest.TestCase):
    def test_limit_failures_listed_separately(self):
        r = RunReport()
        r.add(Outcome.FAILED, "群聊附件/群/大.mov", "附件下载失败：（%s文件太大）" % LIMIT_TAG)
        r.add(Outcome.FAILED, "我的空间/a", "同步失败：boom")
        r.add(Outcome.NEW, "我的空间/b")
        s = r.summary()
        self.assertEqual(s["失败"], 2)
        self.assertEqual(s[LIMIT_LABEL], 1)
        with tempfile.TemporaryDirectory() as d:
            text = r.write_markdown(Path(d) / "2026-11-23_1100_备份日志.md", 0, 60).read_text("utf-8")
        fail_sec = text.split("## 失败")[1].split("## ")[0]
        manual_sec = text.split("## 需手动处理（飞书限制）")[1].split("## ")[0]
        self.assertIn("我的空间/a", fail_sec)
        self.assertNotIn("大.mov", fail_sec)
        self.assertIn("大.mov", manual_sec)

    def test_no_limit_no_section(self):
        r = RunReport()
        r.add(Outcome.FAILED, "我的空间/a", "同步失败：boom")
        self.assertNotIn(LIMIT_LABEL, r.summary())
        with tempfile.TemporaryDirectory() as d:
            text = r.write_markdown(Path(d) / "x_备份日志.md", 0, 60).read_text("utf-8")
        self.assertNotIn("需手动处理", text)


class VersionTest(unittest.TestCase):
    def test_code_version_matches_plugin_json(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text("utf-8"))
        self.assertEqual(fbk.__version__, manifest["version"])

    def test_marketplace_version_matches(self):
        mk = json.loads((PLUGIN.parent.parent / ".claude-plugin" / "marketplace.json").read_text("utf-8"))
        entry = [p for p in mk["plugins"] if p["name"] == "feishu-backup"][0]
        self.assertEqual(entry["version"], fbk.__version__)


if __name__ == "__main__":
    unittest.main()
