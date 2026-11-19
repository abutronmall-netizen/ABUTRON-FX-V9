"""Issue49: account-bound late chart-overwrite repair source contract.

All IDs below are synthetic, no broker account or production file is used.
"""
import importlib.util
from pathlib import Path
import unittest
import hashlib

SRC=Path(__file__).resolve().parents[1]/"ops/issue44/account_scoped_final_chart.py"
spec=importlib.util.spec_from_file_location("issue49_private_chart_overlay",SRC)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
CID="f"*32
OTHER="e"*32
NEW_CHART="/usr/local/lib/synthetic-demo-only/signed-chandelier.chr"
OLD=m.OLD_PIN

BASE='''from pathlib import Path
import shutil, hashlib
def stage_fp0512_single_writer_release(terminal,cid,server,mode):
 return "ORIGINAL_RESTAGER"
def main(terminal,cid,server,mode):
 stage_fp0512_single_writer_release(terminal,cid,server,mode)
 broker="FP Markets"
 if (broker,server,mode)==("FP Markets","FPMarketsSC-Demo","DEMO") and cid in {"'''+CID+'''","'''+OTHER+'''"}:
  import hashlib
  slot=("0512" if cid=="'''+CID+'''" else "1636")
  expected="'''+OLD+'''"
  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"
  return (expected,str(source))
 return "NON_FP_DEMO_UNCHANGED"
'''
EXPECTED=hashlib.sha256(BASE.encode()).hexdigest()
def patched(source=BASE,**overrides):
    args={"target_connection_id":CID,"new_chart_path":NEW_CHART,
          "expected_original_worker_sha256":EXPECTED,
          "candidate_chart_sha256":m.NEW_ISSUE44_CHART_PIN}
    args.update(overrides)
    return m.apply_fp_demo_single_account_overlay(source,**args)

class Issue49(unittest.TestCase):
 def test_single_original_exact_replacement(self):
  s=patched()
  self.assertEqual(s.count(m.NEW_ISSUE44_CHART_PIN),1)
  self.assertEqual(s.count(OLD),1)
  self.assertIn(NEW_CHART,s)
  self.assertNotIn('Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot',s)

 def test_only_target_demo_switches_native_chandelier(self):
  source=patched()
  d={}
  exec(compile(source,"synthetic_worker.py","exec"),d)
  fake=Path("/sandbox/not-used")
  own=d["main"](fake,CID,"FPMarketsSC-Demo","DEMO")
  other=d["main"](fake,OTHER,"FPMarketsSC-Demo","DEMO")
  live=d["main"](fake,CID,"FPMarketsSC-Live","LIVE")
  self.assertEqual(own,(m.NEW_ISSUE44_CHART_PIN,NEW_CHART))
  self.assertEqual(other,(OLD,"/usr/local/lib/abutron-fp-demo-on-chart-v1/1636/chart01.chr"))
  self.assertEqual(live,"NON_FP_DEMO_UNCHANGED")

 def test_untrusted_existing_worker_pin_rejected(self):
  with self.assertRaisesRegex(m.ChartOverrideError,"WORKER_BASE_SHA"):
   patched(expected_original_worker_sha256="a"*64)

 def test_already_modified_worker_rejected(self):
  s=patched()
  with self.assertRaises(m.ChartOverrideError):
   patched(s,expected_original_worker_sha256=m.sha(s))

 def test_no_wrong_new_chart_digest_accepted(self):
  with self.assertRaisesRegex(m.ChartOverrideError,"INVALID_PRIVATE"):
   patched(candidate_chart_sha256="b"*64)

 def test_unsafe_chart_path_rejected(self):
  for path in ("/tmp/new.chr","/usr/local/lib/../secret.chr","https://evil.chr",
               "/usr/local/lib/a file.chr", "/usr/local/lib/x.py"):
   with self.subTest(path=path):
    with self.assertRaises(m.ChartOverrideError):
     patched(new_chart_path=path)

 def test_private_account_identity_must_be_exact(self):
  for value in ("", "NOT_AN_ACCOUNT", "a"*31, "A"*32, "1"*50):
   with self.subTest(value=value):
    with self.assertRaises(m.ChartOverrideError):
     patched(target_connection_id=value)

 def test_ambiguous_finalizer_rejected(self):
  s=BASE.replace('  slot=("0512"', '  slot=("0512"',1)
  s=s.replace('  slot=("0512"', '  slot=("0512"',1)
  s=s.replace('  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"',
              '  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"\n  source=Path("/usr/local/lib/abutron-fp-demo-on-chart-v1")/slot/"chart01.chr"')
  with self.assertRaises(m.ChartOverrideError):
   patched(s,expected_original_worker_sha256=m.sha(s))

 def test_missing_single_writer_restager_rejected(self):
  s=BASE.replace("stage_fp0512_single_writer_release(", "broken_restager(")
  with self.assertRaises(m.ChartOverrideError):
   patched(s,expected_original_worker_sha256=m.sha(s))

 def test_no_io_or_worker_restart_capabilities(self):
  text=SRC.read_text()
  for forbidden in ("systemctl","OrderSend(","os.replace(","subprocess.run",
                    "os.system(","TRADE_ACTION_SLTP","requests.post("):
   self.assertNotIn(forbidden,text)

if __name__=="__main__":
 unittest.main()
