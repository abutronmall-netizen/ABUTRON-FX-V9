import datetime as dt
import importlib.util
import copy
import pathlib
import unittest
s=importlib.util.spec_from_file_location("hour_join",str(pathlib.Path(__file__).resolve().parents[1]/"ops"/"hour_shadow_decisions.py"))
m=importlib.util.module_from_spec(s)
s.loader.exec_module(m)
def fixtures():
 r={"schema":"abutron.v9.broker_hour_loss_shadow.v1",
 "timezone":"Africa/Johannesburg","production_veto_enabled":False,
 "model_promotion_allowed":False,"broker_orders":0,
 "account_hours":[{"account":"0512","hour_sast":19,"shadow_avoid_hour":True,"production_veto_enabled":False}]}
 i={"schema":"abutron.v9.fpmarkets.demo.native.account_trained_m5.v1",
 "trained":True,"model_features":49,"orders_submitted":0,"accounts":{}}
 t=dt.datetime(2026,11,17,17,0,2,tzinfo=dt.timezone.utc)
 for account in ("0512","1636"):
  i["accounts"][account]={"environment":"DEMO","broker":"FP Markets",
   "scope_certified":True,"symbols_current":13,
   "symbols":[{"symbol":f"SYM{x}","model_direction":"BUY",
              "environment":"DEMO","broker":"FP Markets",
              "bar_closed_ts":int(t.timestamp())-2} for x in range(13)]}
 return r,i,t
class Tests(unittest.TestCase):
 def test_19_sast_shadow_hold_only_for_0512(self):
  r,i,t=fixtures();z=m.join(r,i,t)
  self.assertEqual(z["symbols_evaluated"],26)
  self.assertEqual(z["research_would_hold_count"],13)
  self.assertFalse(z["production_veto_enabled"])
  self.assertEqual(z["broker_orders"],0)
 def test_other_hour_does_not_hold(self):
  r,i,t=fixtures();t-=dt.timedelta(hours=1)
  for p in i["accounts"].values():
   for s in p["symbols"]:s["bar_closed_ts"]=int(t.timestamp())-1
  self.assertEqual(m.join(r,i,t)["research_would_hold_count"],0)
 def test_stale_signal_rejected(self):
  r,i,t=fixtures();i["accounts"]["0512"]["symbols"][0]["bar_closed_ts"]-=3000
  with self.assertRaisesRegex(ValueError,"STALE"):m.join(r,i,t)
 def test_cannot_be_live(self):
  r,i,t=fixtures();i["accounts"]["0512"]["environment"]="LIVE"
  with self.assertRaisesRegex(ValueError,"ACCOUNT_FEED"):m.join(r,i,t)
 def test_no_unaudited_veto(self):
  r,i,t=fixtures();r["production_veto_enabled"]=True
  with self.assertRaisesRegex(ValueError,"HOUR_RESEARCH"):m.join(r,i,t)
 def test_duplicate_hour_rejected(self):
  r,i,t=fixtures();r["account_hours"].append(copy.deepcopy(r["account_hours"][0]))
  with self.assertRaisesRegex(ValueError,"DUPLICATE"):m.join(r,i,t)
if __name__=="__main__":unittest.main()