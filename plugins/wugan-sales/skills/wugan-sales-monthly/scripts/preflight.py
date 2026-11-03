"""Run with the same interpreter used for build_report.py; never prints credentials."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reliability import VERSION, resolve_target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    parser.add_argument("--dir")
    parser.add_argument("--workbook")
    parser.add_argument("--create", action="store_true")
    parser.add_argument("--browser-check", help="browser_probe.js 的落盘结果")
    args = parser.parse_args()
    problems = []
    for name in ("openpyxl", "yaml"):
        if importlib.util.find_spec(name) is None:
            problems.append(f"MISSING_DEPENDENCY: {name}; 用当前 Python -m pip install openpyxl pyyaml")
    try:
        path = resolve_target(args.month, args.dir, args.workbook, args.create)
        if path.exists() and not problems:
            import openpyxl
            book = openpyxl.load_workbook(path, read_only=True)
            print(json.dumps({"workbook": str(path), "sheets": book.sheetnames}, ensure_ascii=False))
            book.close()
    except Exception as exc:
        problems.append(f"WORKBOOK_CHECK: {exc}")
    if args.browser_check:
        try:
            probe = json.loads(Path(args.browser_check).read_text())
            if isinstance(probe, str):
                probe = json.loads(probe)
            if not probe.get("page_evaluation") or not probe.get("fetch_available"):
                problems.append("BROWSER_UNSUPPORTED: 需要可执行页面 fetch 的 Playwright MCP，不可用只读 DOM evaluate 代替")
            if not probe.get("logged_in"):
                problems.append("NOT_LOGGED_IN: 请用户在 Playwright 浏览器登录")
            if probe.get("origin") != "https://zhyx.eingdong.com":
                problems.append("WRONG_ORIGIN")
        except (ValueError, OSError, AttributeError) as exc:
            problems.append(f"BROWSER_CHECK_INVALID: {exc}")
    else:
        problems.append("BROWSER_UNVERIFIED: 先用 Playwright MCP 执行 browser_probe.js 并传 --browser-check")
    print(json.dumps({"version": VERSION, "python": sys.executable, "ok": not problems, "problems": problems}, ensure_ascii=False, indent=2))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
