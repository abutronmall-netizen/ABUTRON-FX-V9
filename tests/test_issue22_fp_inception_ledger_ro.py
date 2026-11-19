"""Synthetic account evidence tests for Issue22 broker-native inception audit.

All account identity, timestamps and transactions here are fabricated solely
for offline tests. No production data or login credentials included.
"""
import importlib.util
from pathlib import Path
import unittest

P=Path(__file__).resolve().parents[1]/"ops/issue22/fp_inception_ledger_ro.py"
spec=importlib.util.spec_from_file_location("issue22_fp_inception_ledger",P)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
NOTICE="2026-10-23T18:55:35Z"
NOW=1795109400
FIRST=1792785328
OFFSET=7200

def rows(balance="1030.000000",pending="0"):
    header=[
        "ABUTRON_FP_RESTART_SNAPSHOT_V103","a"*32,"b"*36,
        "FPMarketsSC-Demo","9999999","DEMO",str(NOW),"7200","USD",
        balance,"1040.000000","20.000000","1020.000000","10.000000",
        "1",pending,"3","0",str(NOW+7260),"BROKER_NATIVE_NO_ORDER_FUNCTIONS"
    ]
    pos="P\t10001\tEURUSD.r\t0\t0.10\t1.1000\t1.1010\t1.0950\t1.1150\t10\t0\t1792790000\t1792790200"
    dealrows=[
        "D\t101\t0\t0\t%d\t2\t0\t0\t\t0\t0\t1000\t0\t0\t0"%(FIRST+OFFSET),
        "D\t102\t201\t10001\t%d\t0\t0\t0\tEURUSD.r\t0.10\t1.1\t50\t0\t0\t0"%(FIRST+100+OFFSET),
        "D\t103\t202\t10002\t%d\t1\t1\t0\tEURUSD.r\t0.10\t1.1\t-20\t0\t0\t0"%(FIRST+200+OFFSET),
    ]
    return header+[pos]+dealrows+["END\t1\t"+pending+"\t3"]

def raw(balance="1030.000000",pending="0"):
    return ("\n".join(rows(balance,pending))+"\n").encode()

def broker():
    return {
        "schema":"synthetic.fp_history.v101","history_verified_from_mt5":True,
        "broker":"FP Markets","environment":"DEMO","account_currency":"USD",
        "history_complete_within_selected_window":True,
        "orders_submitted_by_exporter":0,"pending_orders_count":0,
        "history_from_broker_time":FIRST+OFFSET-3600,
        "history_to_broker_time":NOW+OFFSET+180,
        "broker_deals_count":3,"trade_deals_count":2,"closing_deals_count":1,
        "open_positions_count":1,
        "positions":[{"stop_loss":1.095,"take_profit":1.115}],
        "balance_usd":1030.0,
        "total_trade_deal_cashflow_usd":30.0,
        "closing_deal_cashflow_usd":-20.0,
        "native_file_sha256":"c"*64,
    }

class InceptionLedger(unittest.TestCase):
    def check(self,native=None,history=None,notice=NOTICE):
        n=m.parse_native(native if native is not None else raw())
        return m.reconcile(n,history if history is not None else broker(),notice)

    def test_exact_cashflow_reconstruction(self):
        n=m.parse_native(raw())
        self.assertEqual(n["deal_count"],3)
        self.assertEqual(n["trade_deals"],2)
        self.assertEqual(n["closing_deals"],1)
        self.assertEqual(n["nontrading_deals"],1)
        self.assertEqual(str(n["balance"]),"1030.000000")
        self.assertEqual(n["ledger_cashflow"],n["balance"])
        self.assertEqual(n["trade_cashflow"],m.Decimal("30"))

    def test_two_source_inception_account_reconciled_not_certified(self):
        d=self.check()
        self.assertEqual(d["status"],"ACCOUNT_LEVEL_NATIVE_INCEPTION_LEDGER_BALANCE_AND_TRADE_MATCH")
        self.assertEqual(d["difference_usd"],0.0)
        self.assertTrue(d["account_bounded_ledger_strongly_supported"])
        for key in ("broker_account_inception_independently_certified",
                    "broker_historical_intraminute_equity_peaks_certified",
                    "other_linked_accounts_lifetime_certified",
                    "current_execution_ack_or_chandelier_certified",
                    "owner_risk_and_v9_flags_changed"):
            self.assertIs(d[key],False)

    def test_broker_balance_mismatch_rejected(self):
        with self.assertRaisesRegex(m.LedgerEvidenceError,"ACCOUNT_BALANCE"):
            m.parse_native(raw(balance="1031.000000"))

    def test_truncated_native_fence_rejected(self):
        broken=raw().replace(b"END\t1\t0\t3",b"END\t1\t0\t2")
        with self.assertRaisesRegex(m.LedgerEvidenceError,"TRUNCATED_NATIVE"):
            m.parse_native(broken)

    def test_duplicate_broker_ticket_rejected(self):
        b=raw().replace(b"D\t103\t",b"D\t102\t")
        with self.assertRaisesRegex(m.LedgerEvidenceError,"DUPLICATED_BROKER_DEAL"):
            m.parse_native(b)

    def test_wrong_broker_account_schema_rejected(self):
        b=raw().replace(b"FPMarketsSC-Demo",b"FPMarketsSC-Live")
        with self.assertRaisesRegex(m.LedgerEvidenceError,"NATIVE_FP_DEMO_IDENTITY"):
            m.parse_native(b)

    def test_missing_protective_stop_rejected(self):
        b=raw().replace(b"\t1.0950\t1.1150\t",b"\t0.0000\t1.1150\t")
        with self.assertRaisesRegex(m.LedgerEvidenceError,"UNPROTECTED"):
            m.parse_native(b)

    def test_native_pending_order_rejected(self):
        with self.assertRaisesRegex(m.LedgerEvidenceError,"NATIVE_ALLTIME_HISTORY_WINDOW"):
            m.parse_native(raw(pending="1"))

    def test_native_zero_start_required(self):
        b=raw().replace(b"\n0\n"+str(NOW+7260).encode()+b"\n",b"\n500\n"+str(NOW+7260).encode()+b"\n")
        with self.assertRaisesRegex(m.LedgerEvidenceError,"ALLTIME_HISTORY_WINDOW"):
            m.parse_native(b)

    def test_nonfinite_native_money_rejected(self):
        b=raw().replace(b"1030.000000",b"NaN",1)
        with self.assertRaisesRegex(m.LedgerEvidenceError,"BAD_MONETARY_VALUE"):
            m.parse_native(b)

    def test_account_notice_after_earliest_deal_fails(self):
        with self.assertRaisesRegex(m.LedgerEvidenceError,"NATIVE_DEALS_PREDATE"):
            self.check(notice="2026-10-24T00:00:00Z")

    def test_history_window_must_begin_before_notice(self):
        b=broker()
        b["history_from_broker_time"]=FIRST+OFFSET+3600
        with self.assertRaisesRegex(m.LedgerEvidenceError,"SELECTED_WINDOW_DOES_NOT_COVER"):
            self.check(history=b)

    def test_empty_account_history_not_authenticated(self):
        b=broker();b["history_verified_from_mt5"]=False
        with self.assertRaisesRegex(m.LedgerEvidenceError,"FRESH_ACCOUNT_HISTORY_NOT_AUTHENTICATED"):
            self.check(history=b)

    def test_source_deal_count_must_match_exactly(self):
        b=broker();b["broker_deals_count"]=4
        with self.assertRaisesRegex(m.LedgerEvidenceError,"INDEPENDENT_NATIVE_DEAL"):
            self.check(history=b)

    def test_source_trade_cashflow_must_match_exactly(self):
        b=broker();b["total_trade_deal_cashflow_usd"]=30.02
        with self.assertRaisesRegex(m.LedgerEvidenceError,"INDEPENDENT_BROKER_CASHFLOW"):
            self.check(history=b)

    def test_source_closing_cashflow_must_match_exactly(self):
        b=broker();b["closing_deal_cashflow_usd"]=-21.0
        with self.assertRaisesRegex(m.LedgerEvidenceError,"INDEPENDENT_BROKER_CASHFLOW"):
            self.check(history=b)

    def test_source_current_unprotected_position_rejected(self):
        b=broker();b["positions"][0]["stop_loss"]=0
        with self.assertRaisesRegex(m.LedgerEvidenceError,"CURRENT_OPEN_POSITION_UNPROTECTED"):
            self.check(history=b)

    def test_source_repeated_position_count_mismatch(self):
        b=broker();b["positions"]=[]
        with self.assertRaisesRegex(m.LedgerEvidenceError,"FRESH_BROKER_POSITION_ROWS"):
            self.check(history=b)

    def test_reject_naive_notice_time(self):
        with self.assertRaisesRegex(m.LedgerEvidenceError,"UNTRUSTED_NAIVE"):
            self.check(notice="2026-10-23T18:55:35")

    def test_no_live_execution_or_system_mutation(self):
        source=P.read_text()
        for banned in ("OrderSend(","OrderCheck(","TRADE_ACTION_SLTP",
                       "systemctl start","subprocess.run","os.system("):
            self.assertNotIn(banned,source)

if __name__=="__main__":
    unittest.main()
