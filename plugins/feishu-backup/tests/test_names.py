import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk.names import sanitize, local_filename, version_name, unique_path  # noqa: E402


class NamesTest(unittest.TestCase):
    def test_sanitize_replaces_illegal_and_newlines(self):
        self.assertEqual(sanitize("a/b:c\\d\ne"), "a_b_c_d_e")

    def test_sanitize_truncates_keeping_ext(self):
        s = sanitize("长" * 200 + ".xlsx")
        self.assertTrue(s.endswith(".xlsx"))
        self.assertLessEqual(len(s.encode()), 240)

    def test_local_filename_rules(self):
        self.assertEqual(local_filename("docx", "品牌手册"), "品牌手册.pdf")
        self.assertEqual(local_filename("sheet", "预算"), "预算.xlsx")
        self.assertEqual(local_filename("file", "合同.zip"), "合同.zip")
        self.assertIsNone(local_filename("mindnote", "脑图"))

    def test_version_name_uses_shanghai_time(self):
        # 1759390200 = 2025-10-02 15:30 Asia/Shanghai
        self.assertEqual(version_name("2026 预算.xlsx", 1759390200), "2026 预算_2025-10-02_1530.xlsx")

    def test_unique_path_appends_counter(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self.assertEqual(unique_path(d / "a.pdf").name, "a.pdf")
            (d / "a.pdf").write_text("x")
            (d / "a-2.pdf").write_text("x")
            self.assertEqual(unique_path(d / "a.pdf").name, "a-3.pdf")


if __name__ == "__main__":
    unittest.main()
