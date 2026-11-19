"""Issue44 fail-closed DEMO release review: no broker calls or deployment."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
import json

FILE=Path(__file__).resolve().parents[1]/"ops/issue44/release_readiness_ro.py"
spec=importlib.util.spec_from_file_location("issue44_review",FILE)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

BASE={
 "live_on":0,"crm_demo":("CONNECTED",1,1),
 "worker_fresh":True,"broker_history_authentic":True,
 "native_reconciled":True,"all_open_protected":True,
 "pending_orders":0,"owner_status":"BLOCKED_INCOMPLETE_EXPOSURE",
 "owner_blockers":3,"v9_status":"NOT_CERTIFIED_FOR_NEW_ORDERS",
 "v9_blockers":7,"source_sha_pins_verified":True,
 "isolated_qa_original_optionc_verified":True,
 "isolated_qa_no_order":True,"trailing_archive_fresh":True,
 "native_trailing_records":0,"trailing_broker_modification_certified":False,
 "open_positions":8,"stop_risk_usd":650.70,"equity_usd":100695.45,
}

class SourceContract(unittest.TestCase):
 def test_eight_open_positions_inhibit_rolling_upgrade(self):
  r=m.evaluate(BASE)
  self.assertEqual(r["status"],"RELEASE_PROMOTION_INHIBITED")
  self.assertIn("OPEN_PROTECTED_POSITIONS_ROLLING_RESTART_NOT_PROVEN",r["inhibitors"])
 def test_even_flat_source_does_not_authorize_auto_deployment(self):
  r=m.evaluate({**BASE,"open_positions":0})
  self.assertEqual(r["status"],"INSPECTED_CANDIDATE_ONLY_DEPLOYMENT_REQUIRES_REVIEW")
  for k in ("production_promoted","new_order_execution_authorized",
            "live_trading_enabled_by_audit","crm_or_risk_flags_modified",
            "broker_trailing_sltp_amendment_certified"):
   self.assertIs(r[k],False)
 def test_live_on_and_broker_pending_fail_closed(self):
  x=m.evaluate({**BASE,"live_on":1,"pending_orders":1})
  self.assertIn("LIVE_ACCOUNT_EFFECTIVE_ON",x["inhibitors"])
  self.assertIn("PENDING_BROKER_ORDERS",x["inhibitors"])
 def test_missing_protection_and_unverified_history_fail(self):
  x=m.evaluate({**BASE,"all_open_protected":False,
                "broker_history_authentic":False})
  self.assertIn("OPEN_BROKER_POSITION_MISSING_SLTP",x["inhibitors"])
  self.assertIn("BROKER_NATIVE_HISTORY_NOT_AUTHENTIC",x["inhibitors"])
 def test_unattributed_changed_trailing_source_refused(self):
  x=m.evaluate({**BASE,"native_trailing_records":1,
                "trailing_broker_modification_certified":False})
  self.assertIn("UNATTESTED_NATIVE_TRAILING_SOURCE_RECORDS",x["inhibitors"])
 def test_broker_risk_above_20pct_refused(self):
  x=m.evaluate({**BASE,"stop_risk_usd":20500,"equity_usd":100000})
  self.assertIn("OPEN_STOP_RISK_EXCEEDS_20_PERCENT",x["inhibitors"])
 def test_nan_invalid_risk_refused(self):
  x=m.evaluate({**BASE,"stop_risk_usd":float("nan")})
  self.assertIn("CURRENT_EXPOSURE_OR_EQUITY_INVALID",x["inhibitors"])
 def test_bad_qa_or_source_pin_cannot_be_promoted(self):
  x=m.evaluate({**BASE,"source_sha_pins_verified":False,
                "isolated_qa_no_order":False})
  self.assertIn("RELEASE_SOURCES_NOT_SHA_PINNED",x["inhibitors"])
  self.assertIn("ISOLATED_NATIVE_NO_ORDER_QA_NOT_PROVEN",x["inhibitors"])
 def test_source_snapshot_symlink_not_followed(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);real=root/"real.json";real.write_text('{"a":1}')
   link=root/"linked.json";link.symlink_to(real)
   with self.assertRaises(m.ReleaseEvidenceError):m.safe_json(link)
 def test_bounded_current_file_and_sha_validated(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d)/"a.json";p.write_text('{"broker":"DEMO"}')
   import hashlib
   sha=hashlib.sha256(p.read_bytes()).hexdigest()
   self.assertEqual(m.validated_sha(p,sha),sha)
   with self.assertRaises(m.ReleaseEvidenceError):m.validated_sha(p,"f"*64)
 def test_root_private_output_not_run_as_test(self):
  self.assertIn("ROOT_PRIVATE_OUTPUT_REQUIRED",FILE.read_text())
  self.assertIn("new_order_execution_authorized",FILE.read_text())

 def test_sensitive_source_paths_not_displayed(self):
  secret=FileNotFoundError("/var/lib/private/hidden-secret.json")
  self.assertEqual(m.safe_error_code(secret),"REVIEW_FILENOTFOUNDERROR")
  self.assertNotIn("private",m.safe_error_code(secret))

 def test_specific_source_failure_code_remains_safe(self):
  self.assertEqual(m.safe_error_code(m.ReleaseEvidenceError("RECONCILER_EQUITY_NOT_CURRENT")),
                   "RECONCILER_EQUITY_NOT_CURRENT")
  self.assertEqual(m.safe_error_code(m.ReleaseEvidenceError("file /private")),
                   "REVIEW_RELEASEEVIDENCEERROR")

if __name__=="__main__":unittest.main()
