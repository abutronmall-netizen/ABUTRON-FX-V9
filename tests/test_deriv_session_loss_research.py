import datetime as dt
import importlib.util
import hashlib
import unittest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"ops"))

spec=importlib.util.spec_from_file_location("deriv_shadow", str(Path(__file__).resolve().parents[1]/"ops"/"deriv_session_loss_research.py"))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

def sample():
    t=int(dt.datetime(2026,11,5,9,0,tzinfo=dt.timezone.utc).timestamp())
    key=hashlib.sha256(b"verified").hexdigest()
    row=("6207",key,1234,"Volatility 50 Index.0","BUY",0.2,t,t+1200,
         -10.0,-1.0,0.0,0.0,-11.0)
    ledger={key:("Deriv","DEMO","Volatility 50 Index.0","BUY",1234,
                "CLOSED_RECONCILED",
                dt.datetime.fromtimestamp(t-2,dt.timezone.utc).isoformat(),"d6fc2524ec373b8d1bc278a8be3ec635")}
    return row,ledger

class TestVerifiedNative(unittest.TestCase):
    def test_closed_deriv_entry_hour_sast(self):
        r,l=sample();v=m.check_deal(r,l)
        self.assertEqual(v["hour_sast"],11)
        self.assertEqual(v["pnl_usd"],-11.0)
    def test_cash_components(self):
        r,l=sample(); r=list(r);r[-1]=-5
        with self.assertRaisesRegex(ValueError,"CASH"):m.check_deal(tuple(r),l)
    def test_no_unknown_broker_account(self):
        r,l=sample();r=list(r);r[0]="0178"
        with self.assertRaisesRegex(ValueError,"POSITION"):m.check_deal(tuple(r),l)
    def test_no_unverified_ledger_event(self):
        r,l=sample()
        with self.assertRaisesRegex(ValueError,"LEDGER"):m.check_deal(r,{})
    def test_no_unverified_server_time(self):
        r,l=sample();r=list(r);r[6]+=7200;r[7]+=7200
        with self.assertRaisesRegex(ValueError,"TIME"):m.check_deal(tuple(r),l)
    def test_no_nonfinite_trade(self):
        r,l=sample();r=list(r);r[-1]=float("nan")
        with self.assertRaisesRegex(ValueError,"CASH"):m.check_deal(tuple(r),l)
    def test_no_one_loss_hour_veto(self):
        r,l=sample()
        report=m.analyze([m.check_deal(r,l)])
        self.assertEqual(report["closed_positions"],1)
        self.assertEqual(report["shadow_hours_flagged"],0)
        self.assertFalse(report["production_veto_enabled"])
if __name__=="__main__":unittest.main()