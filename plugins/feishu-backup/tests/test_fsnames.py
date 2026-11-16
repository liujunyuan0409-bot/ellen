import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "skills" / "feishu-backup" / "scripts"))

from fbk.names import local_filename, sanitize, unique_path  # noqa: E402
from fbk.platform.fsnames import RESERVED, clean_component, fold, lp, strip_lp  # noqa: E402


class CleanComponentTest(unittest.TestCase):
    def test_windows_reserved_and_trailing(self):
        self.assertEqual(clean_component("con.txt"), "_con.txt")
        self.assertEqual(clean_component("Aux.PDF"), "_Aux.PDF")
        self.assertEqual(clean_component("nul"), "_nul")
        self.assertEqual(clean_component("报告. "), "报告")
        self.assertEqual(clean_component("a:b*c?.pdf"), "a_b_c_.pdf")

    def test_reserved_set(self):
        self.assertEqual(len(RESERVED), 22)
        for n in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
            self.assertIn(n, RESERVED)
        self.assertNotIn("COM0", RESERVED)

    def test_reserved_variants(self):
        self.assertEqual(clean_component("com1.tar.gz"), "_com1.tar.gz")
        self.assertEqual(clean_component("LPT9"), "_LPT9")
        self.assertEqual(clean_component("nul."), "_nul")  # 先去结尾点再判保留名
        self.assertEqual(clean_component("CON .txt"), "_CON .txt")
        # 不是保留名
        for ok in ("console.txt", "COM10", "LPT0.pdf", "con_x.pdf", "xcon"):
            self.assertEqual(clean_component(ok), ok)

    def test_illegal_and_control_chars(self):
        self.assertEqual(clean_component('a/b\\c|d"e<f>g'), "a_b_c_d_e_f_g")
        self.assertEqual(clean_component("a\x00b\x1fc\x7f"), "a_b_c_")

    def test_empty_or_dots(self):
        for bad in ("", ".", "..", " . ", "   "):
            self.assertEqual(clean_component(bad), "_")

    def test_fold(self):
        self.assertEqual(fold("Report.PDF"), fold("report.pdf"))
        self.assertEqual(fold("Stra\u00dfe"), fold("STRASSE"))
        self.assertEqual(fold("e\u0301"), fold("\u00e9"))  # NFD 与 NFC 同键
        self.assertNotEqual(fold("a.pdf"), fold("b.pdf"))


class LongPathTest(unittest.TestCase):
    def test_long_path_prefix_applied_on_windows(self):
        self.assertEqual(lp(r"C:\a\b", is_windows=True), "\\\\?\\C:\\a\\b")
        self.assertEqual(lp(r"\\srv\share\x", is_windows=True), "\\\\?\\UNC\\srv\\share\\x")
        self.assertEqual(lp("/tmp/x", is_windows=False), "/tmp/x")

    def test_prefix_idempotent_and_normalized(self):
        self.assertEqual(lp("\\\\?\\C:\\a", is_windows=True), "\\\\?\\C:\\a")
        self.assertEqual(lp("\\\\?\\UNC\\s\\h\\x", is_windows=True), "\\\\?\\UNC\\s\\h\\x")
        # \\?\ 关掉了系统的路径规整，这里必须先规整好
        self.assertEqual(lp("C:/a/./b/../c", is_windows=True), "\\\\?\\C:\\a\\c")
        self.assertEqual(lp(r"C:\飞书备份\我的空间", is_windows=True), "\\\\?\\C:\\飞书备份\\我的空间")

    def test_strip_lp_roundtrip(self):
        for raw in (r"C:\a\b", r"\\srv\share\x"):
            self.assertEqual(strip_lp(lp(raw, is_windows=True)), raw)
        self.assertEqual(strip_lp("/tmp/x"), "/tmp/x")

    def test_path_object_and_mac_identity(self):
        p = Path("/Users/fake/飞书备份/a.pdf")
        self.assertEqual(lp(p, is_windows=False), str(p))
        if sys.platform != "win32":
            self.assertEqual(lp(p), str(p))


class SanitizeRulesTest(unittest.TestCase):
    """names.sanitize 套用两系统统一规则，同时保持原有保证（≤ max_bytes、保留扩展名、不为空）。"""

    def test_sanitize_applies_rules(self):
        self.assertEqual(sanitize("con.txt"), "_con.txt")
        self.assertEqual(sanitize("报告. "), "报告")
        self.assertEqual(sanitize("  周报  "), "周报")
        self.assertEqual(local_filename("docx", "PRN"), "_PRN.pdf")
        self.assertEqual(local_filename("docx", "季度总结."), "季度总结.pdf")
        self.assertEqual(local_filename("file", "aux.zip"), "_aux.zip")

    def test_sanitize_truncation_never_leaves_trailing_dot_or_space(self):
        for filler in (" ", "."):
            name = "长" * 79 + filler + "尾" * 20  # 无扩展名，截断点落在 filler 之后
            s = sanitize(name, max_bytes=240)
            self.assertLessEqual(len(s.encode("utf-8")), 240)
            self.assertFalse(s.endswith((" ", ".")), repr(s[-3:]))

    def test_reserved_prefix_stays_within_limit(self):
        s = sanitize("con." + "x" * 10, max_bytes=8)
        self.assertLessEqual(len(s.encode("utf-8")), 8)
        self.assertTrue(s.startswith("_"))

    def test_sanitize_long_keeps_ext(self):
        s = sanitize("长" * 200 + ".xlsx")
        self.assertTrue(s.endswith(".xlsx"))
        self.assertLessEqual(len(s.encode("utf-8")), 240)


class UniquePathCaseTest(unittest.TestCase):
    def test_unique_path_case_insensitive(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "Report.pdf").write_text("A")
            real = os.path.lexists
            # 模拟大小写敏感的检查：只认完全同名
            exact = lambda p: real(p) and os.path.basename(p) in os.listdir(os.path.dirname(p) or ".")  # noqa: E731
            from unittest import mock
            with mock.patch("os.path.lexists", side_effect=exact), \
                    mock.patch("os.path.exists", side_effect=exact):
                self.assertEqual(unique_path(d / "report.pdf").name, "report-2.pdf")
            self.assertEqual(unique_path(d / "b.pdf").name, "b.pdf")

    def test_unique_path_ignore_self(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "Report.pdf").write_text("A")
            self.assertEqual(unique_path(d / "report.pdf", ignore=d / "Report.pdf").name, "report.pdf")


if __name__ == "__main__":
    unittest.main()
