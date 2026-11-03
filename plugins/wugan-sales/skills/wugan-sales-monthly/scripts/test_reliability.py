"""Synthetic-only regression tests; no customer data, tokens, or live writes."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
import openpyxl
from reliability import atomic_save, digest, resolve_target, validate_raw, validate_funds
from build_report import apply_anomalies, ops_row


def fixture():
    days = []
    for day in range(1,32):
        days.append(dict(date=f"2026-10-{day:02d}", order_count=0, order_total=0,
                         order_refund_total=0, order_total_balance=0, order_refund_balance=0,
                         topup_total=0, topup_count=0, cashier_total_balance=0))
    days[0].update(order_count=1, order_total=30, order_total_balance=30, order_refund_balance=5,
                   order_refund_total=5, topup_total=100, topup_count=1)
    return dict(month="2026-10",start="2026-10-01 00:00:00",end="2026-10-31 23:59:59",time_type="pay_time",
                total_count=0,list=[],report_days=days,big_orders=[],
                ledger=[dict(time="2026-10-01 01:00:00",amount=100,remark="余额充值"),
                        dict(time="2026-10-01 02:00:00",amount=30,remark="购买商品"),
                        dict(time="2026-10-01 03:00:00",amount=5,remark="订单退款")],
                ledger_summary=dict(amount_before=20,increase_amount=105,deductions_amount=30,amount_after=95))


class ValidationTests(unittest.TestCase):
    def test_complete_month(self):
        d=fixture(); validate_raw(d); self.assertEqual(validate_funds(d,{} )["ending"],95)

    def test_missing_or_duplicate_day(self):
        for change in (lambda d:d["report_days"].pop(), lambda d:d["report_days"].__setitem__(1,d["report_days"][0])):
            d=fixture(); change(d)
            with self.assertRaises(ValueError): validate_raw(d)

    def test_wrong_month_and_time_basis(self):
        for key,value in (("month","2026-13"),("start","2026-09-01 00:00:00"),("time_type","create_time")):
            d=fixture(); d[key]=value
            with self.assertRaises(ValueError): validate_raw(d)

    def test_incomplete_sales_or_metadata(self):
        d=fixture(); d["total_count"]=1
        with self.assertRaises(ValueError): validate_raw(d)
        d=fixture(); d["fetch_meta"]={}
        with self.assertRaises(ValueError): validate_raw(d)

    def test_bad_funds(self):
        for change in (lambda d:d["ledger_summary"].update(amount_after=96),
                       lambda d:d["ledger"].pop(),
                       lambda d:d["report_days"][0].update(order_total_balance=31),
                       lambda d:d["ledger"][0].update(remark="新流水类型")):
            d=fixture(); change(d)
            with self.assertRaises(ValueError): validate_funds(d,{})

    def test_refund_not_in_cashier_not_double_deducted(self):
        d=fixture()
        d["ledger"].extend([dict(time="2026-10-02 01:00:00",amount=80000,remark="余额充值"),
                            dict(time="2026-10-03 01:00:00",amount=-80000,remark="商家扣除，理由：充值退款")])
        d["ledger_summary"].update(increase_amount=80105,deductions_amount=80030)
        d["report_days"][1].update(topup_total=80000,topup_count=1)
        rules={"anomaly":{"excluded_balance_pairs":[dict(deduction=dict(time="2026-10-03 01:00:00",amount=-80000),
                      topups=[dict(time="2026-10-02 01:00:00",amount=80000,paid=80000)])]}}
        validate_funds(d,rules)
        _,ex,pending,_=apply_anomalies(d,rules)
        self.assertFalse(pending)
        row=ops_row(d,ex,1,25)
        self.assertEqual(row[7],100)
        self.assertEqual(row[9],25)
        self.assertEqual(row[10],95)
        # Historical fallback when cashier did include the deduction.
        del d["ledger_summary"]; d["report_days"][2]["cashier_total_balance"]=80000
        self.assertEqual(ops_row(d,ex,1,25)[9],25)

    def test_path_never_silently_creates(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError): resolve_target("2026-10",t)
            self.assertEqual(resolve_target("2026-10",t,allow_create=True).parent,Path(t))
            with self.assertRaises(ValueError): resolve_target("2026-10",str(Path(t)/"missing"),allow_create=True)


class AtomicTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.path=Path(self.folder.name)/"year.xlsx"
        self.old=openpyxl.Workbook(); self.old.active["A1"]="old"; self.old.save(self.path)
        self.original=self.path.read_bytes(); self.hash=digest(self.path)
        self.new=openpyxl.Workbook(); self.new.active["A1"]="new"

    def test_success_backup_audit(self):
        backup,audit=atomic_save(self.new,self.path,{"rules_sha256":"fixture"},self.hash)
        self.assertEqual(backup.read_bytes(),self.original)
        record=json.loads(audit.read_text()); self.assertEqual(record["state"],"committed")
        self.assertEqual(record["output_sha256"],digest(self.path))

    def test_failed_save_keeps_original(self):
        with patch.object(self.new,"save",side_effect=OSError("disk full")):
            with self.assertRaises(OSError): atomic_save(self.new,self.path,{},self.hash)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_failed_readback_keeps_original(self):
        def reject(_): raise ValueError("invalid output")
        with self.assertRaises(ValueError): atomic_save(self.new,self.path,{},self.hash,reject)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_replace_failure_keeps_original(self):
        with patch("reliability.os.replace",side_effect=OSError("replace denied")):
            with self.assertRaises(OSError): atomic_save(self.new,self.path,{},self.hash)
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_concurrent_change_not_overwritten(self):
        self.path.write_bytes(b"concurrent edit")
        with self.assertRaisesRegex(ValueError,"WORKBOOK_CHANGED"): atomic_save(self.new,self.path,{},self.hash)
        self.assertEqual(self.path.read_bytes(),b"concurrent edit")


if __name__ == "__main__": unittest.main()
