"""Input validation, reconciliation, and transactional workbook publishing."""
import calendar
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile

VERSION = "0.2.0"
SKILL_ROOT = Path(__file__).resolve().parent.parent


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_target(month, directory=None, workbook=None, allow_create=False):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("BAD_MONTH: expected YYYY-MM")
    config = json.loads((SKILL_ROOT / "config.json").read_text())
    folder = Path(directory or config["report_dir"]).expanduser()
    path = Path(workbook).expanduser() if workbook else folder / f"{month[:4]}年五感销售数据统计.xlsx"
    if not path.parent.is_dir():
        raise ValueError(f"REPORT_DIR_MISSING: {path.parent}; 请确认路径，不自动创建目录")
    if not path.exists() and not allow_create:
        raise ValueError(f"WORKBOOK_MISSING: {path}; 确认新建年度文件后使用 --create")
    if path.suffix.lower() != ".xlsx":
        raise ValueError("WORKBOOK_TYPE: expected .xlsx")
    return path


def money(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("NONFINITE_NUMBER")
    return round(number * 100)


def validate_raw(raw):
    month = raw.get("month", "")
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise ValueError("BAD_MONTH")
    year, mon = map(int, month.split("-"))
    dates = {f"{month}-{i:02d}" for i in range(1, calendar.monthrange(year, mon)[1]+1)}
    if raw.get("start") != f"{month}-01 00:00:00" or raw.get("end") != f"{month}-{len(dates):02d} 23:59:59" or raw.get("time_type") != "pay_time":
        raise ValueError("PERIOD_MISMATCH: require complete month and payment time")
    for key in ("list", "report_days", "ledger", "big_orders"):
        if not isinstance(raw.get(key), list):
            raise ValueError(f"MISSING_DATASET: {key}")
    if int(raw.get("total_count", -1)) != len(raw["list"]):
        raise ValueError("INCOMPLETE_SALES")
    day_list = [r.get("date") for r in raw["report_days"]]
    if len(day_list) != len(dates) or set(day_list) != dates:
        raise ValueError("INCOMPLETE_REPORT_DAYS: missing or duplicate dates")
    for row in raw["list"]:
        for key in ("one_price", "real_num", "real_total", "sum_num", "sum_total", "discount_amount", "refunded_total"):
            money(row[key])
    for row in raw["report_days"]:
        for key in ("order_count", "order_total", "order_refund_total", "order_total_balance", "order_refund_balance", "topup_total", "topup_count"):
            money(row[key])
    for row in raw["ledger"]:
        if str(row.get("time", ""))[:10] not in dates:
            raise ValueError("LEDGER_OUTSIDE_MONTH")
        money(row["amount"])
    for row in raw["big_orders"]:
        if str(row.get("pay_time", ""))[:10] not in dates:
            raise ValueError("ORDER_OUTSIDE_MONTH")
    meta = raw.get("fetch_meta")
    if meta is not None:
        for name, field in (("sales", "list"), ("report", "report_days"), ("ledger", "ledger"), ("orders", "big_orders")):
            part = meta.get(name, {})
            if part.get("complete") is not True or int(part.get("rows", -1)) != len(raw[field]):
                raise ValueError(f"INCOMPLETE_API: {name}")


def validate_funds(raw, rules):
    summary = raw.get("ledger_summary") or {}
    required = ("amount_before", "amount_after", "increase_amount", "deductions_amount")
    if any(k not in summary for k in required):
        raise ValueError("LEDGER_SUMMARY_MISSING")
    before, after, inc, dec = [money(summary[k]) for k in required]
    if before + inc - dec != after:
        raise ValueError("BALANCE_RECON_FAIL: beginning + increases - deductions != ending")
    increases = deductions = refunds = purchases = 0
    for row in raw["ledger"]:
        amount, remark = abs(money(row["amount"])), str(row.get("remark", ""))
        if remark in ("余额充值", "订单退款"):
            increases += amount
            if remark == "订单退款":
                refunds += amount
        elif remark == "购买商品" or remark.startswith("商家扣除"):
            deductions += amount
            if remark == "购买商品":
                purchases += amount
        elif amount:
            raise ValueError(f"UNKNOWN_LEDGER_TYPE: {remark}")
    if (increases, deductions) != (inc, dec):
        raise ValueError("LEDGER_DETAIL_RECON_FAIL")
    paid = sum(money(r["order_total_balance"])-money(r["order_refund_balance"]) for r in raw["report_days"])
    if purchases-refunds != paid:
        raise ValueError("BALANCE_PAY_RECON_FAIL: ledger purchases less refunds != report balance payments")
    # Every confirmed adjustment must actually exist, and a pair must net to zero.
    ledger = {(r["time"], abs(money(r["amount"]))): r for r in raw["ledger"]}
    for pair in (rules.get("anomaly") or {}).get("excluded_balance_pairs") or []:
        if sum(money(t["amount"]) for t in pair["topups"]) != abs(money(pair["deduction"]["amount"])):
            raise ValueError("EXCLUSION_PAIR_UNBALANCED")
        for record in [pair["deduction"]] + pair["topups"]:
            if record["time"].startswith(raw["month"]) and (record["time"], abs(money(record["amount"]))) not in ledger:
                raise ValueError("EXCLUSION_RECORD_MISSING")
    return {"beginning": before/100, "increases": inc/100, "deductions": dec/100,
            "ending": after/100, "balance_payments": paid/100}


def atomic_save(wb, path, provenance, expected_hash, verify=None):
    """Validate a sibling temp file, back up original, then replace atomically."""
    import openpyxl
    path = Path(path)
    backup_dir = path.parent / "_backup"
    backup_dir.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".xlsx", dir=path.parent)
    os.close(fd)
    temporary = Path(temp_name)
    record_path = backup_dir / f"{path.stem}_{stamp}.audit.json"
    try:
        wb.save(temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        check = openpyxl.load_workbook(temporary)
        try:
            if check.sheetnames != wb.sheetnames:
                raise ValueError("OUTPUT_SHEETS_MISMATCH")
            for sheet in check:
                for row in sheet:
                    if any(c.data_type == "e" for c in row):
                        raise ValueError(f"OUTPUT_CELL_ERROR: {sheet.title}")
            if verify:
                verify(check)
        finally:
            check.close()
        current_hash = digest(path) if path.exists() else None
        if current_hash != expected_hash:
            raise ValueError("WORKBOOK_CHANGED: file changed during processing; retry")
        backup = backup_dir / f"{path.stem}_{stamp}.xlsx" if path.exists() else None
        if backup:
            shutil.copy2(path, backup)
        record = dict(provenance, skill_version=VERSION, input_workbook_sha256=current_hash,
                      output_sha256=digest(temporary), backup=str(backup) if backup else None,
                      state="prepared", created_at=dt.datetime.now(dt.timezone.utc).isoformat())
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if (digest(path) if path.exists() else None) != expected_hash:
            raise ValueError("WORKBOOK_CHANGED: source changed before replace")
        os.replace(temporary, path)
        record["state"] = "committed"
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        return backup, record_path
    finally:
        temporary.unlink(missing_ok=True)
