import datetime as dt
import hashlib
import importlib.util
import unittest
spec=importlib.util.spec_from_file_location("session_loss",str(__import__("pathlib").Path(__file__).resolve().parents[1]/"ops"/"session_loss_research.py"))
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
def events(days=6,each=4,loss=True):
 rows=[]
 for day in range(days):
  for i in range(each):
   t=dt.datetime(2026,11,1+day,9,i,tzinfo=dt.timezone.utc)
   rows.append(dict(broker="FP Markets",account="0512",
    event_id=hashlib.sha1(f"{day}/{i}".encode()).hexdigest(),
    symbol="EURUSD.r",direction="BUY",entry_time=t.isoformat(),
    exit_time=(t+dt.timedelta(minutes=30)).isoformat(),
    pnl="-10" if loss else "10"))
 return rows
class Tests(unittest.TestCase):
 def test_bad_hour_both_windows(self):
  r=m.analyze(events())
  self.assertEqual(r["closed_positions"],24)
  self.assertEqual(r["shadow_hours_flagged"],1)
  self.assertFalse(r["production_veto_enabled"])
  self.assertEqual(r["account_hours"][0]["hour_sast"],11)
 def test_recovery_is_not_blocked(self):
  e=events()
  for row in e[-8:]: row["pnl"]="12"
  self.assertFalse(m.analyze(e)["account_hours"][0]["shadow_avoid_hour"])
 def test_one_loss_is_not_blocked(self):
  self.assertFalse(m.analyze(events(1,1))["account_hours"][0]["shadow_avoid_hour"])
 def test_duplicate_rejected(self):
  e=events(1,2);e[1]["event_id"]=e[0]["event_id"]
  with self.assertRaisesRegex(ValueError,"DUPLICATE"):m.analyze(e)
 def test_fake_pnl_rejected(self):
  e=events(1,1);e[0]["pnl"]="nan"
  with self.assertRaisesRegex(ValueError,"CASHFLOW"):m.analyze(e)
 def test_account_untrusted(self):
  e=events(1,1);e[0]["account"]="6364"
  with self.assertRaisesRegex(ValueError,"ACCOUNT"):m.analyze(e)
if __name__=="__main__":unittest.main()